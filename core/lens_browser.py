"""Drives a real Chromium so Google Lens results can be read.

QtWebEngine cannot do this job. CONFIRMED against the live site: its
Chromium fork fails to run Google's front-end - the page renders its
tabs and then stops, its own script throwing, and the request that
fetches the results goes out with an empty session id. A stock Chromium
runs the same page correctly, so this drives one through Playwright
instead.

Three things had to be true before any result came back, and all three
were found the hard way:

  * The uploaded file needs its REAL media type. Handing Google a JPEG
    labelled application/octet-stream gets "Something went wrong - can't
    read file", which looks exactly like an empty results page from the
    outside. This was the real cause of every "no results" before it.
  * The browser profile has to have been used. A brand-new profile is
    challenged with a CAPTCHA almost every time; after a couple of
    ordinary searches, it goes through.
  * The results live in the page's RESPONSE BODY, not in its DOM. The
    tiles Lens draws carry no link - their source URL never becomes an
    href or an attribute - so the body is captured as it arrives and
    read from there.

The browser is headed. Headless is challenged immediately, and a
visible window is also what lets the user answer a challenge when one
does appear.

Playwright is an optional dependency: this is the only thing that needs
it, and it brings a ~150MB browser with it.
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import desktop_attention
from .applog import get_logger
from .google_images import GOOGLE_URL
from .paths import CONFIG_DIR

log = get_logger("lens_browser")

PROFILE_DIR = CONFIG_DIR / "lens_profile"

# Ordinary searches run once per session before the first image. A
# profile with no history behind it is challenged nearly every time.
WARMUP_SEARCHES = (
    "https://www.google.com/search?q=weather&hl=en",
    "https://www.google.com/search?q=news&hl=en",
)

RESULTS_URL_RE = re.compile(r"vsrid=|/sorry/")
# Hosts that mean a response actually carries results rather than chrome.
SOURCE_HINT_RE = re.compile(
    r"https?:(?:\\?/){2}(?!\w*\.?google|gstatic|googleusercontent|www\.w3\.org|schema\.org)"
    r"[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)

# The Lens window's own identity - its Wayland app id and X11 WM_CLASS -
# so the desktop can tell it apart from the user's own browser. CONFIRMED
# on KDE Plasma 6 (Wayland): the Lens window asks for attention during a
# search, focus-stealing prevention turns that into "demands attention",
# and an auto-hide panel then stays up until the window is clicked. With
# a class of its own, core/desktop_attention.py can keep just this window
# off the taskbar except while a robot check needs the user.
WINDOW_CLASS = "hatate-lens"

# How long to leave a challenge on screen for the user to answer. It is
# their window and their CAPTCHA; the only limit is that a batch left
# unattended should not wait forever.
CHALLENGE_WAIT_MS = 180_000
CHALLENGE_POLL_MS = 1000
RECAPTCHA_SELECTOR = "iframe[src*='recaptcha'], iframe[title*='reCAPTCHA']"

# A search area at least this wide and tall is the whole picture for
# practical purposes - Lens reports 98% x 98% for an uncropped image, and
# dragging for two percent would cost a re-search for nothing.
WHOLE_ENOUGH = 0.95
# Dragged slightly past the corner so the handle lands on the edge rather
# than a pixel short of it. Lens clamps to the image.
DRAG_OVERSHOOT_PX = 8

# Google hides the matches for an explicit image behind a confirmation -
# "These results may be explicit" with a control to go on. OBSERVED as a
# plain <div> with no role and no href, so it is found by its words.
# Clicking it is the same choice the user would make by hand, on their
# own picture; without it the engine simply gets nothing back for a
# large part of a library like this one.
# Waiting is on RESULTS, not the clock. Every step used to sleep a fixed
# 6-8 seconds, and a control that wasn't on the page cost a click timeout
# on top: MEASURED over 161 searches, 57s on average, 79 of them spending
# 8s failing to click an "Exact matches" tab that wasn't there and up to
# 16s more trying four explicit-results labels one timeout at a time.
# Now a step waits until the response it caused has arrived and gone
# quiet, never longer than the fixed wait it replaces.
SETTLE_POLL_MS = 250
SETTLE_QUIET_MS = 1500
AFTER_RESULTS_MAX_MS = 6000
AFTER_WIDEN_MAX_MS = 7000
AFTER_TAB_MAX_MS = 7000
AFTER_REVEAL_MAX_MS = 8000
# The exact-match tiles are drawn a moment after their response lands.
TILE_RENDER_MAX_MS = 2000
# A control is looked for once, not waited for: absent is the common case.
CLICK_TIMEOUT_MS = 5000

EXPLICIT_REVEAL_LABELS = (
    "See exact matches",
    "Show explicit results",
    "See results",
    "Show results",
)


# Which of Lens's two optional dependencies is absent. They are installed
# by different commands, so they are reported as different things - see
# LensBrowserUnavailable.missing.
MISSING_PLAYWRIGHT = "playwright"
MISSING_CHROMIUM = "chromium"


class LensBrowserError(Exception):
    pass


class LensBrowserUnavailable(LensBrowserError):
    """Playwright or its Chromium is not installed.

    `missing` names WHICH of the two, because the fix is a different
    command for each - installing the package does not fetch the browser
    and vice versa - and a message that offers both leaves the user
    guessing which one they are actually short of. One of
    MISSING_PLAYWRIGHT, MISSING_CHROMIUM, or None when the browser
    simply would not start for some other reason.
    """

    def __init__(self, message: str, missing: Optional[str] = None):
        super().__init__(message)
        self.missing = missing


class LensChallengeUnanswered(LensBrowserError):
    """Google asked for a robot check and it went unanswered."""


@dataclass
class _Job:
    image_bytes: bytes
    filename: str
    timeout: float
    whole_image: bool = True
    retried: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    payloads: List[str] = field(default_factory=list)
    tiles: List[dict] = field(default_factory=list)
    error: Optional[Exception] = None


def missing_dependency_message() -> str:
    """How to install Playwright, for the interpreter actually in use.

    Names sys.executable rather than "venv/bin/pip". The app can be
    started either through run.sh (which uses the project venv) or as
    `python3 main.py` (which uses the system Python), and a message that
    assumes the wrong one sends the user to install a package where it
    will not be seen - reporting "not installed" for something they just
    installed.
    """
    lines = [
        "Google Lens needs Playwright and a Chromium build, and they are not installed "
        f"for the Python running this app ({sys.executable}). Install them with:",
        f"    {sys.executable} -m pip install playwright",
        f"    {sys.executable} -m playwright install chromium",
        "then restart.",
    ]
    # Compared by environment, not by interpreter path: venv/bin/python3
    # is usually a SYMLINK to the same system binary, so resolving the
    # two executables makes an app running outside the venv look like one
    # running inside it, and the hint never appears.
    venv_dir = Path(__file__).resolve().parent.parent / "venv"
    if venv_dir.is_dir() and Path(sys.prefix).resolve() != venv_dir.resolve():
        lines.append(
            f"There is also a virtualenv at {venv_dir}, which ./run.sh uses. If "
            "Playwright is installed there, launching with ./run.sh is the easiest fix."
        )
    return "\n".join(lines)


def missing_chromium_message() -> str:
    """Playwright is here; the browser it drives is not.

    A separate message from the one above on purpose. The two failures
    look identical from the outside - Lens finds nothing - and read
    identically in the log, but "pip install playwright" is the wrong
    advice for this one and sends a user to reinstall a package they
    already have.
    """
    return "\n".join([
        "Google Lens has Playwright installed for the Python running this app "
        f"({sys.executable}), but the Chromium build it drives is missing. Install it "
        "with:",
        f"    {sys.executable} -m playwright install chromium",
        "then restart. It is about 150MB, and nothing else in this app needs it.",
    ])


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _browsers_root() -> Optional[Path]:
    """Where Playwright keeps its downloaded browsers, or None if unknown.

    Only the cases this can be sure about. PLAYWRIGHT_BROWSERS_PATH wins
    when it is set to a directory; "0" means the browsers live inside the
    installed package, which is a layout this does not try to read. With
    nothing set it is the per-user cache, which is where `playwright
    install` puts them on Linux.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override == "0":
        return None
    if override:
        return Path(override)
    if sys.platform.startswith("linux"):
        cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(cache) / "ms-playwright"
    return None


def chromium_installed() -> Optional[bool]:
    """Whether a headed Chromium has been downloaded. None = cannot tell.

    Deliberately three-valued. Reporting a missing browser that is
    actually there would send the user to fix a problem they do not
    have, so anything this cannot read for certain - an unfamiliar
    layout, an unreadable directory - answers None and the caller carries
    on and lets the real launch decide.

    `chromium_headless_shell-*` does not count: that build has no window,
    and this engine is headed because headless is challenged on sight.
    """
    root = _browsers_root()
    if root is None:
        return None
    try:
        if not root.is_dir():
            # `playwright install` creates this directory. Absent with a
            # default layout means nothing has been installed at all.
            return False
        return any(child.name.startswith("chromium-") and child.is_dir()
                   for child in root.iterdir())
    except OSError:
        return None


@dataclass(frozen=True)
class DependencyStatus:
    """What Google Lens is short of, if anything."""

    missing: Optional[str] = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.missing is None


def check_dependencies() -> DependencyStatus:
    """Both of Lens's optional dependencies, before a search is attempted.

    Cheap enough to call from a settings checkbox: an import and a
    directory listing, no browser started.
    """
    if not playwright_available():
        return DependencyStatus(MISSING_PLAYWRIGHT, missing_dependency_message())
    if chromium_installed() is False:
        return DependencyStatus(MISSING_CHROMIUM, missing_chromium_message())
    return DependencyStatus()


def classify_launch_failure(error: BaseException) -> Optional[DependencyStatus]:
    """Read a failed launch for the one cause that is the user's to fix.

    The browser directory probe above cannot see every layout, so a
    missing Chromium can still reach the launch. Playwright says so
    plainly when it does - "Executable doesn't exist at ...", with its
    own `playwright install` hint attached - and that sentence is the
    only reliable signal available at this point. None for every other
    failure: a profile already in use or a window that would not open is
    not a missing dependency and must not be reported as one.
    """
    text = str(error)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return DependencyStatus(MISSING_CHROMIUM, missing_chromium_message())
    return None


class LensBrowser:
    """A single Chromium, driven from its own thread.

    Playwright's objects belong to the thread that made them, and search
    engines run on a pool of threads, so every request is handed to this
    one thread rather than touching the browser from wherever it came
    from.
    """

    def __init__(self):
        self._jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._warmed = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="lens-browser", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        with self._lock:
            if self._thread is None:
                return
            self._jobs.put(None)
            self._thread.join(timeout=15)
            self._thread = None

    def fetch(self, image_bytes: bytes, filename: str, timeout: float,
              whole_image: bool = True) -> tuple:
        """Upload one image; return its response bodies and result tiles."""
        # Both dependencies, not just the import: a Chromium that was
        # never downloaded used to be found only by launching a browser
        # and failing, thirty seconds into the first image.
        status = check_dependencies()
        if not status.ok:
            raise LensBrowserUnavailable(status.message, status.missing)
        self.start()
        job = _Job(image_bytes, filename, timeout, whole_image)
        self._jobs.put(job)
        # Generous: a challenge is answered by a person, and the browser
        # side ends the job either way.
        if not job.done.wait(CHALLENGE_WAIT_MS / 1000.0 + timeout + 120):
            raise LensBrowserError("The browser never answered")
        if job.error:
            raise job.error
        return job.payloads, job.tiles

    # -- the browser thread --------------------------------------------

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright
        retry: Optional[_Job] = None
        try:
            with sync_playwright() as pw:
                while True:
                    context = self._launch(pw)
                    try:
                        retry = self._serve(context, retry)
                    finally:
                        try:
                            context.close()
                        except Exception:             # noqa: BLE001 - it may be gone already
                            pass
                    if retry is None:
                        return                        # asked to stop
                    # The window was closed under us. A fresh one, and the
                    # search it interrupted runs again there.
        except Exception as exc:                      # noqa: BLE001
            log.exception("Lens browser thread stopped: %s", exc)
            if retry is not None and not retry.done.is_set():
                retry.error = _unavailable(exc)
                retry.done.set()
            self._drain(exc)

    def _drain(self, error: Exception) -> None:
        """Fail every waiting job when the browser thread cannot run.

        Without this a caller blocks until its own timeout for a browser
        that is never going to answer.
        """
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is None:
                continue
            job.error = _unavailable(error)
            job.done.set()

    def _launch(self, pw):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        log.info("Starting Chromium for Google Lens (profile: %s)", PROFILE_DIR)
        try:
            return self._launch_context(pw)
        except Exception as exc:                       # noqa: BLE001
            if "already in use" in str(exc) or "Opening in existing browser" in str(exc):
                raise LensBrowserError(
                    f"Another Chromium is already using the Lens profile at "
                    f"{PROFILE_DIR}. Close it - or, if a previous run left it behind, "
                    "quit that browser window - and search again."
                ) from exc
            raise

    def _launch_context(self, pw):
        return pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,      # headless is challenged on sight
            locale="en-US",
            # Playwright launches with --enable-automation, which sets
            # navigator.webdriver. MEASURED: true in this window as it was
            # launched. reCAPTCHA reads it, and answering the check in a
            # browser that says it is automated only brings another one -
            # on 2026-09-25 every check went unanswered, one of them on
            # first opening Google before anything was searched.
            ignore_default_args=["--enable-automation"],
            args=[f"--class={WINDOW_CLASS}", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )

    def _serve(self, context, first: Optional[_Job] = None) -> Optional[_Job]:
        """Runs jobs until asked to stop (returns None) or until the browser
        window is closed under us (returns the job it interrupted, to be
        run again in a fresh one - once).

        CONFIRMED: a user closing the window after an unanswered robot
        check left every later search failing on "Target page, context or
        browser has been closed", as a raw traceback, until restart."""
        page = context.pages[0] if context.pages else context.new_page()
        job = first
        while True:
            if job is None:
                job = self._jobs.get()
            if job is None:
                return None
            try:
                job.payloads, job.tiles = self._one(page, job)
            except Exception as exc:                  # noqa: BLE001
                if _browser_gone(exc) and not job.retried:
                    job.retried = True
                    log.info("The Lens browser window was closed - opening a new one "
                             "and running this search again")
                    return job
                job.error = exc if isinstance(exc, LensBrowserError) else LensBrowserError(
                    _first_line(exc))
            job.done.set()
            job = None

    SAFESEARCH_URL = "https://www.google.com/safesearch"

    def _turn_off_safesearch_blur(self, page) -> None:
        """Set SafeSearch to Off, once, in this browser's own profile.

        Not about what results Google returns - about what its result
        TILES contain. With SafeSearch on "Blur" (the default), the
        thumbnail Lens puts in each tile is a blurred placeholder:
        MEASURED at 2,070 bytes for 221x228, 0.04 bytes per pixel, and
        it never sharpens. Hashing that put an IDENTICAL image at 78%.
        With SafeSearch off the same tile carries a real 13,418-byte
        thumbnail at 0.27 bytes per pixel, and the comparison means
        something.

        The choice is stored in the profile, so this runs once per
        install rather than once per image.
        """
        try:
            page.goto(self.SAFESEARCH_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            radios = page.locator("[role=radio]")
            for index in range(radios.count()):
                radio = radios.nth(index)
                label = (radio.inner_text() or "").split("\n")[0].strip().lower()
                if label != "off":
                    continue
                if radio.get_attribute("aria-checked") == "true":
                    return                      # already set from a previous run
                radio.click(timeout=8000)
                page.wait_for_timeout(3000)
                log.info("Turned Google's SafeSearch blur off - its result thumbnails are "
                         "blurred placeholders otherwise, which cannot be compared against")
                return
        except Exception as exc:                # noqa: BLE001 - a nicety, never fatal
            log.debug("Could not set SafeSearch (%s) - carrying on", exc)

    def _warm_up(self, page) -> None:
        if self._warmed:
            return
        self._warmed = True
        self._turn_off_safesearch_blur(page)
        for url in WARMUP_SEARCHES:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
            except Exception as exc:                  # noqa: BLE001
                if _browser_gone(exc):
                    raise
                log.debug("Warm-up navigation failed (harmless): %s", exc)
                continue
            # A fresh browser is the likeliest thing of all to be checked.
            self._wait_out_challenge(page, "on first opening Google")

    def _is_challenged(self, page) -> bool:
        """Google's robot check: its /sorry/ page, or a reCAPTCHA in the
        page itself."""
        try:
            if "/sorry/" in (page.url or ""):
                return True
            return page.locator(RECAPTCHA_SELECTOR).count() > 0
        except Exception as exc:                      # noqa: BLE001
            if _browser_gone(exc):
                raise
            return False

    def _wait_out_challenge(self, page, where: str) -> None:
        """If Google is asking for a robot check, call the user and wait.

        Checked after every step that loads something, not only after the
        upload. CONFIRMED: a check arriving anywhere else - opening Google
        for the first time, clicking a tab or the explicit-results notice
        - sat in the window unannounced, with nothing asking for the user,
        while the search carried on around it."""
        if not self._is_challenged(page):
            return
        log.info("Google is asking for a robot check (%s) - waiting for the user to "
                 "answer it", where)
        # The one time the window does need the user.
        desktop_attention.ask_for_input(WINDOW_CLASS)
        try:
            page.bring_to_front()
            waited = 0
            while self._is_challenged(page):
                if waited >= CHALLENGE_WAIT_MS:
                    raise LensChallengeUnanswered(
                        "the robot check in the browser window went unanswered")
                page.wait_for_timeout(CHALLENGE_POLL_MS)
                waited += CHALLENGE_POLL_MS
        finally:
            desktop_attention.quiet(WINDOW_CLASS)
        log.info("The robot check was answered - carrying on")

    def _search_area(self, page):
        """The region Lens is searching, as (left, top, right, bottom).

        Read from the crop handles' aria-labels, which state it outright:
        "top left corner of search area: left 9%, top 4%". Every
        percentage is measured from the image's left/top edge, whatever
        edge the label names - "right 87%" is an edge 87% across, not 87%
        in from the right.

        NOT read from the URL. MEASURED against the live site: the
        `vsint` parameter carries a region too, and it says the whole
        image even while the handles say 9%-87%. An earlier version
        trusted it and therefore never once noticed a crop.
        """
        found = {}
        for element in page.query_selector_all("[aria-label*='corner of search area']"):
            label = element.get_attribute("aria-label") or ""
            corner = next((c for c in ("top left", "bottom right") if label.startswith(c)), None)
            box = element.bounding_box()
            if not corner or not box:
                continue
            percentages = [int(v) for v in re.findall(r"(\d+)%", label)]
            if len(percentages) != 2:
                continue
            found[corner] = {
                "element": element,
                "cx": box["x"] + box["width"] / 2,
                "cy": box["y"] + box["height"] / 2,
                "x": percentages[0] / 100,
                "y": percentages[1] / 100,
            }
        if "top left" not in found or "bottom right" not in found:
            return None
        return found

    def _widen_search_area(self, page, job: _Job, bodies: List[str]) -> bool:
        """Drag the crop back out to the whole picture.

        Lens picks a region of the upload and searches only that. Usually
        it is the whole image, but it can settle on one detail and answer
        about that instead - OBSERVED at 78% x 93% and smaller.

        There is no URL to rewrite, so this moves the handles the way a
        person would: the two corner handles and the percentages they
        state give the image's rectangle, and each is dragged to the
        matching corner of it.
        """
        if not job.whole_image:
            return False
        area = self._search_area(page)
        if area is None:
            return False

        top_left, bottom_right = area["top left"], area["bottom right"]
        width = bottom_right["x"] - top_left["x"]
        height = bottom_right["y"] - top_left["y"]
        if width <= 0 or height <= 0:
            return False
        if width >= WHOLE_ENOUGH and height >= WHOLE_ENOUGH:
            return False            # already the whole picture, near enough

        # Solve for the image's on-screen rectangle from two handles whose
        # positions AND percentages are both known.
        image_w = (bottom_right["cx"] - top_left["cx"]) / width
        image_h = (bottom_right["cy"] - top_left["cy"]) / height
        image_x = top_left["cx"] - image_w * top_left["x"]
        image_y = top_left["cy"] - image_h * top_left["y"]

        log.info("Lens cropped the search to %.0f%% x %.0f%% of the image - dragging it "
                 "back out to the whole picture", width * 100, height * 100)
        self._drag(page, top_left["element"], image_x - DRAG_OVERSHOOT_PX,
                   image_y - DRAG_OVERSHOOT_PX)
        # The handles are re-rendered by the first drag, so they have to
        # be looked up again rather than reused.
        again = self._search_area(page)
        if again is not None:
            self._drag(page, again["bottom right"]["element"],
                       image_x + image_w + DRAG_OVERSHOOT_PX,
                       image_y + image_h + DRAG_OVERSHOOT_PX)
        self._settle(page, bodies, AFTER_WIDEN_MAX_MS)
        self._wait_out_challenge(page, "after widening the search area")

        return True

    def _drag(self, page, element, to_x: float, to_y: float) -> None:
        box = element.bounding_box()
        if not box:
            return
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(to_x, to_y, steps=12)
        page.mouse.up()
        page.wait_for_timeout(1200)

    def _settle(self, page, bodies: List[str], max_ms: int, need_new: bool = True) -> None:
        """Waits until the results a step asked for have arrived.

        Done when a new response has come in and nothing further has for
        SETTLE_QUIET_MS - or, with need_new False, when nothing has for
        that long at all. Never longer than max_ms, the fixed wait this
        replaces, so a step is never slower than it was."""
        start, seen = len(bodies), len(bodies)
        waited = quiet = 0
        while waited < max_ms:
            page.wait_for_timeout(SETTLE_POLL_MS)
            waited += SETTLE_POLL_MS
            if self._is_challenged(page):
                return          # the caller calls the user straight away
            if len(bodies) != seen:
                seen, quiet = len(bodies), 0
                continue
            quiet += SETTLE_POLL_MS
            if quiet >= SETTLE_QUIET_MS and (seen > start or not need_new):
                return

    def _click_if_present(self, locator) -> bool:
        """Clicks the control if the page has it - without waiting out a
        timeout for one it doesn't, which is the common case."""
        try:
            if locator.count() == 0:
                return False
            locator.first.click(timeout=CLICK_TIMEOUT_MS)
            return True
        except Exception as exc:                       # noqa: BLE001 - absent is normal
            if _browser_gone(exc):
                raise
            log.debug("Could not click %s (%s)", locator, _first_line(exc))
            return False

    def _reveal_explicit_results(self, page, bodies: List[str]) -> bool:
        """Click past the "these results may be explicit" confirmation."""
        for label in EXPLICIT_REVEAL_LABELS:
            if not self._click_if_present(page.get_by_text(label, exact=True)):
                continue
            log.info("Confirmed Google's explicit-results notice (%r)", label)
            self._settle(page, bodies, AFTER_REVEAL_MAX_MS)
            self._wait_out_challenge(page, "after the explicit-results notice")
            return True
        return False

    def _collect_tiles(self, page) -> List[dict]:
        """The rendered result tiles: their text and their thumbnail.

        Read from the DOM rather than the response body, because the
        body's thumbnails are deferred placeholders. This is the only
        way an Exact-matches result gets a picture to be compared
        against.
        """
        from .google_lens import EXACT_TILES_JS
        waited = 0
        while True:
            try:
                tiles = page.evaluate(EXACT_TILES_JS)
            except Exception as exc:               # noqa: BLE001 - a missing tab is normal
                if _browser_gone(exc):
                    raise
                log.debug("Could not read the result tiles (%s)", exc)
                return []
            found = [t for t in (tiles or []) if isinstance(t, dict) and t.get("thumb")]
            if found or waited >= TILE_RENDER_MAX_MS:
                return found
            page.wait_for_timeout(SETTLE_POLL_MS * 2)
            waited += SETTLE_POLL_MS * 2

    def _one(self, page, job: _Job) -> tuple:
        from .google_lens import upload_script

        self._warm_up(page)
        # Nothing here needs the user, so the window keeps to itself -
        # until and unless Google asks for a robot check below.
        desktop_attention.quiet(WINDOW_CLASS)
        bodies: List[str] = []

        def on_response(response):
            try:
                if response.request.resource_type not in ("document", "xhr", "fetch"):
                    return
                if "/search" not in response.url:
                    return
                body = response.text()
            except Exception:      # a body that cannot be read is not a failure
                return
            if SOURCE_HINT_RE.search(body):
                bodies.append(body)

        page.on("response", on_response)
        tiles: List[dict] = []
        # Bound before the try: an exception on the way to the widen must
        # not leave the return statement below reaching for a name that
        # was never assigned.
        widened_from = 0
        try:
            page.goto(GOOGLE_URL, wait_until="domcontentloaded", timeout=int(job.timeout * 1000))
            page.evaluate(upload_script(job.image_bytes, job.filename))
            page.wait_for_url(RESULTS_URL_RE, timeout=int(job.timeout * 1000))

            self._wait_out_challenge(page, "after the upload")

            self._settle(page, bodies, AFTER_RESULTS_MAX_MS, need_new=False)
            # Everything captured so far belongs to the CROPPED search.
            # Anything arriving after this point is the widened one, and
            # is preferred - but only if it arrives. Clearing the list
            # outright meant a widen whose re-search was not captured
            # left the image with NOTHING, which is worse than answering
            # from the crop.
            widened_from = len(bodies)
            if not self._widen_search_area(page, job, bodies):
                widened_from = 0
            # BOTH result tabs, because they hold different things and
            # only one of them was ever opened before. "Visual matches"
            # lists pictures that look alike and embeds their URLs.
            # "Exact matches" lists the SAME picture - the booru posts
            # this app is actually after - and was reached only when
            # Google happened to show its explicit-results notice, whose
            # confirmation link goes there. A rule34.xxx post sitting on
            # that tab was therefore invisible on every image that
            # notice did not appear for.
            for tab in ("Visual matches", "Exact matches"):
                # Absent when that tab is already the one showing - the
                # explicit-results confirmation lands on Exact matches.
                if self._click_if_present(page.get_by_role("link", name=tab)):
                    self._settle(page, bodies, AFTER_TAB_MAX_MS)
                    self._wait_out_challenge(page, f"after opening {tab}")
                else:
                    log.debug("No %s tab to click", tab)
                self._reveal_explicit_results(page, bodies)
                if tab == "Exact matches":
                    tiles = self._collect_tiles(page)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:                          # noqa: BLE001
                pass
            # Clears whatever attention the search itself asked for.
            desktop_attention.quiet(WINDOW_CLASS)

        # Every response is handed back, not just the biggest. Clicking
        # through the explicit-results notice loads a further page, and
        # OBSERVED: the largest body afterwards was one that parsed to
        # nothing while a smaller, earlier one held all the matches.
        # Picking by size threw the answer away; the caller picks by
        # what actually parses.
        #
        # Responses from after the crop was widened win, since they are
        # about the whole picture - but if the widened search produced
        # none that were captured, the cropped ones are still better than
        # returning nothing at all.
        return bodies[widened_from:] or bodies, tiles


def _unavailable(error: BaseException) -> LensBrowserUnavailable:
    """A browser that would not start, said in the most useful terms.

    A Chromium that was never downloaded arrives here as an ordinary
    launch failure with Playwright's own "Executable doesn't exist" in
    it. Passing that through verbatim buried the one line the user can
    act on under a call log, so it is recognised and replaced with the
    install command; anything else keeps its original text.
    """
    dependency = classify_launch_failure(error)
    if dependency is not None:
        return LensBrowserUnavailable(dependency.message, dependency.missing)
    return LensBrowserUnavailable(f"The browser could not be started: {error}")


def _browser_gone(exc: BaseException) -> bool:
    """Whether Playwright is saying the window, page or browser is gone."""
    return type(exc).__name__ == "TargetClosedError" or "has been closed" in str(exc)


def _first_line(exc: BaseException) -> str:
    """Playwright's messages run to a call log of many lines; the first
    one is the part worth showing."""
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


_browser = LensBrowser()


def fetch_results_payloads(image_bytes: bytes, filename: str, timeout: float,
                           whole_image: bool = True) -> tuple:
    """(response bodies, rendered result tiles) for one image."""
    return _browser.fetch(image_bytes, filename, timeout, whole_image)


def shutdown() -> None:
    _browser.shutdown()
