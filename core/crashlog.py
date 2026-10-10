"""Makes a crash say why, in the log the user actually has.

Three crashes were diagnosed during development, and none of them left
anything in app.log - it simply stopped mid-sentence. Each needed a core
dump and a disassembler to explain:

  * a QThread destroyed while still running, which Qt answers with
    qFatal() -> abort. Qt prints the reason, but to stderr.
  * a segfault inside QMessageBox's destructor, calling through a dead
    pointer in a native dialog helper. Nothing is printed at all.
  * an unhandled Python exception in a slot, which PyQt turns into
    qFatal() after printing the traceback - again, to stderr.

The common thread is stderr: a GUI launched from a menu has nowhere for
it to go, and even from a terminal it is gone once the window is closed.
Everything here exists to route those three paths into app.log instead.

install() is best-effort throughout. A diagnostic that can itself throw
during a crash is worse than none.
"""
from __future__ import annotations

import faulthandler
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from .applog import LOG_FILE, get_logger
from .paths import CONFIG_DIR

log = get_logger("crash")

# Kept open for the process's lifetime: faulthandler writes to this file
# descriptor from a signal handler, so it must not be closed or garbage
# collected while it might still be needed.
FAULT_FILE = CONFIG_DIR / "crash.log"
_fault_stream = None
_installed = False

# Proves the app is currently running. Written at startup once the previous
# marker has been checked, and removed at the end of a clean shutdown -
# finding it still there at the next startup means the last run never
# reached that removal, i.e. it crashed, was killed, or the machine lost
# power. A SIGKILL or power cut gives no chance to write anything at the
# moment of the fault itself, so this is the only way to learn about it
# after the fact: absence of a clean exit, not a captured cause (that part
# is crash.log's job, when faulthandler or the exception hooks below got a
# chance to run at all).
RUNNING_MARKER = CONFIG_DIR / "crash.log.running"

# When the last clean shutdown finished, for the interrupted-run banner's
# "last clean exit" (S-01). The running marker cannot say: it is removed
# on a clean exit, so a crash finds it present and the time of the exit
# before it is gone. Written by mark_clean_shutdown; absent until the
# first clean exit after an upgrade, which reads as "not known".
CLEAN_EXIT_FILE = CONFIG_DIR / "last_clean_exit"

# How much of app.log previous_run_stopped_at() reads from the end: enough
# to cover the crashed run's last lines and this run's startup, never the
# whole 2 MB file.
_LOG_TAIL_BYTES = 64_000
_LOG_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[")
_LOG_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
# setup_logging()'s first line of every run: the boundary between the
# crashed run's lines and this one's.
_LOG_RUN_START = "Logging started, writing to"

# How much of crash.log to show in the log viewer. It is appended to for
# the life of CONFIG_DIR, potentially across years of runs; a fault is
# always near the end, so only the tail is worth reading.
CRASH_LOG_TAIL_CHARS = 20_000

# Written at the top of every run's section of crash.log, so one run's
# faults can be told from the next's.
SESSION_START_MARK = "--- session started, faulthandler armed ---"

# What faulthandler prints first for each fault it catches - one per crash,
# however many thread stacks follow it.
FAULT_HEADER = "Fatal Python error:"


def install() -> None:
    """Route unhandled errors - Python, threads, Qt, and hard crashes -
    into the log. Call once, as early in startup as possible."""
    global _installed
    if _installed:
        return
    _installed = True
    _install_faulthandler()
    _install_python_hooks()
    _install_qt_handler()


def check_unclean_shutdown() -> bool:
    """Whether the previous run left the running marker behind - i.e. it
    never reached mark_clean_shutdown(). Call this BEFORE mark_running():
    once this run's own marker is down, every later call would just see
    that instead of the previous run's."""
    return RUNNING_MARKER.exists()


def mark_running() -> None:
    """Drops the running marker. Call once at startup, right after
    check_unclean_shutdown() has already looked for the previous one."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RUNNING_MARKER.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write the running marker: %s", exc)


def mark_clean_shutdown() -> None:
    """Removes the running marker. Call at the very end of a clean
    shutdown, after everything that could still fail (saving the session,
    writing settings) has already run - a marker cleared too early would
    call a crash during that final save "clean"."""
    try:
        RUNNING_MARKER.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not clear the running marker: %s", exc)
    try:
        CLEAN_EXIT_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not record the clean exit: %s", exc)


def get_crash_log_path() -> str:
    return str(FAULT_FILE)


def get_recent_crash_log() -> str:
    """Tail of crash.log, for the log viewer. Unlike app.log there is no
    in-memory ring to read from: a hard crash is exactly the case where
    nothing after the fault gets a chance to run, so this reads whatever
    faulthandler or the exception hooks already flushed to disk."""
    try:
        data = FAULT_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(data) > CRASH_LOG_TAIL_CHARS:
        data = data[-CRASH_LOG_TAIL_CHARS:]
    return data


def faults_in_previous_run(path=None) -> int:
    """How many faults the run BEFORE this one left in crash.log.

    Every run opens its own section with SESSION_START_MARK, so the
    previous run's faults are the ones between the last mark (this run's,
    already written by install()) and the one before it. Zero is a real
    answer, not a failure to read: a SIGKILL or a power cut gives
    faulthandler no chance to write anything, which is exactly the case
    where the app says "nothing survived" and the log has nothing to add.
    """
    try:
        data = Path(path or FAULT_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    sections = data.split(SESSION_START_MARK)
    if len(sections) < 3:
        return 0
    return sum(1 for line in sections[-2].splitlines() if line.startswith(FAULT_HEADER))


def last_clean_exit_age_seconds(path=None):
    """How long ago the last clean shutdown finished, or None if none was
    ever recorded (a fresh install, or the first crash after the record
    began - it cannot be reconstructed, so it is not guessed)."""
    try:
        stamp = float(Path(path or CLEAN_EXIT_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return max(time.time() - stamp, 0.0)


def previous_run_stopped_at(path=None):
    """When the run BEFORE this one last wrote to app.log - the nearest
    thing to "when it stopped" that a crash leaves behind - as epoch
    seconds, or None if app.log cannot say.

    A SIGKILL or a power cut records nothing at the moment of the fault, so
    the last log line is a lower bound, not the instant itself. Read from
    the line before this run's own "Logging started", which setup_logging()
    has already written by the time anything asks.
    """
    try:
        with open(path or LOG_FILE, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(size - _LOG_TAIL_BYTES, 0))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    this_run = None
    for index in range(len(lines) - 1, -1, -1):
        if _LOG_RUN_START in lines[index]:
            this_run = index
            break
    if this_run is None:
        return None
    for line in reversed(lines[:this_run]):
        match = _LOG_STAMP.match(line)
        if match:
            try:
                return time.mktime(time.strptime(match.group(1), _LOG_STAMP_FORMAT))
            except (ValueError, OverflowError):
                return None
    return None


def _install_faulthandler() -> None:
    """Catches what Python cannot: a segfault or abort dumps the C-level
    stack of every thread to crash.log.

    That is the only thing that explains a crash inside Qt itself, where
    there is no Python exception to report - the QMessageBox destructor
    case left literally nothing behind without it.
    """
    global _fault_stream
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _fault_stream = open(FAULT_FILE, "a", buffering=1, encoding="utf-8")
        _fault_stream.write("\n" + SESSION_START_MARK + "\n")
        faulthandler.enable(file=_fault_stream, all_threads=True)
        log.debug("Hard-crash handler armed, writing to %s", FAULT_FILE)
    except Exception as exc:  # pragma: no cover - depends on the filesystem
        log.warning("Could not arm the hard-crash handler: %s", exc)


def _install_python_hooks() -> None:
    """Unhandled exceptions, on the main thread and on worker threads."""
    previous = sys.excepthook

    def handle(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        log.critical("Unhandled exception:\n%s",
                     "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())
        previous(exc_type, exc, tb)

    sys.excepthook = handle

    def handle_thread(args):
        # A worker thread dying silently is how a search stops halfway
        # with no explanation anywhere.
        log.critical(
            "Unhandled exception in thread %s:\n%s",
            getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)).rstrip(),
        )

    try:
        threading.excepthook = handle_thread
    except Exception:  # pragma: no cover - older interpreters
        pass


# Qt's own text for the warning MainWindow._retire_worker provokes every
# time it lets go of a background worker. Matched on the fixed part of the
# string; the class name and "::unnamed" that follow vary per worker.
_WILDCARD_DISCONNECT = "wildcard call disconnects from destroyed signal"


def _is_benign_wildcard_disconnect(message: str) -> bool:
    """Whether this is the expected noise from retiring a worker.

    _retire_worker calls worker.disconnect() to guarantee that a result
    describing a selection the user has moved on from cannot reach the
    GUI. Disconnecting everything at once is the point of that call, and
    Qt warns whenever some of those connections belonged to a signal that
    has already gone - which, for a thread that has just finished, is
    ordinary rather than a fault.

    It was 125 of the 153 warnings in a real session's log: four out of
    five warnings said nothing, which is how a warning that DOES mean
    something gets skipped over. Kept at debug rather than dropped - it is
    still evidence if a teardown ever needs reconstructing.
    """
    return isinstance(message, str) and _WILDCARD_DISCONNECT in message


def _install_qt_handler() -> None:
    """Send Qt's own diagnostics to the log.

    This is the one that matters most. Qt reports things like
    "QThread: Destroyed while thread is still running" through qFatal,
    which prints to stderr and then aborts - so the single line explaining
    the crash was the one line guaranteed not to be kept. Routing it here
    means the log's last entry names the cause.
    """
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:  # pragma: no cover - PyQt is a hard dependency in practice
        return

    # Resolved by NAME, looked up when a message actually arrives, rather
    # than capturing bound methods now: the handler then follows any later
    # change to the logger instead of holding a stale reference to it.
    levels = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "error",
        QtMsgType.QtFatalMsg: "critical",
    }

    def handle(mode, context, message):
        try:
            level = levels.get(mode, "warning")
            if mode == QtMsgType.QtWarningMsg and _is_benign_wildcard_disconnect(message):
                level = "debug"
            emit = getattr(log, level)
            where = ""
            # Qt only fills these in for debug builds, so treat them as a
            # bonus rather than something to rely on.
            if getattr(context, "file", None):
                where = f" ({context.file}:{getattr(context, 'line', '?')})"
            emit("Qt: %s%s", message, where)
            if mode == QtMsgType.QtFatalMsg:
                # qFatal aborts the moment this returns, so anything still
                # sitting in a buffer is lost. Push it out now.
                _flush_log()
        except Exception:
            pass  # never let the crash reporter cause a crash

    qInstallMessageHandler(handle)


def _flush_log() -> None:
    for handler in list(log.handlers) + list(getattr(log.parent, "handlers", [])):
        try:
            handler.flush()
        except Exception:
            pass
