"""The one place outbound HTTP goes through.

Every engine and parser used to call requests.get/post/head directly,
which meant three things were each somebody's job and nobody's:

  * Connections were never reused. requests.get opens a Session, does
    one request and throws the pool away, so every fetch from a booru
    paid a fresh TCP and TLS handshake. Here there is one Session per
    host, kept for the life of the process.
  * A "hard" deadline abandoned a thread rather than stopping anything
    (see core/hard_timeout.py): IQDB giving up at 15s left the upload
    running on in the background. With `deadline`, the body is read in
    the CALLING thread and the connection is closed the moment time is
    up - so it really stops. Waiting for the response to begin is still
    bounded by the per-read `timeout`, which is all requests can offer.
  * A 429 was a failure everywhere but the few modules that handled it.
    GET and HEAD now wait out one short Retry-After and try again.
  * A site that says how many requests it has left (x-ratelimit-remaining
    and -reset, as Reddit does) is taken at its word: once it says none,
    the next request to that host waits for the reset instead of spending
    itself on a 429. MEASURED on Reddit's anonymous RSS: one request left
    the allowance at 0.0, reset in 22-25 seconds, and the very next one
    was refused.

Cookies a response sets are NOT kept. Each request carries exactly the
cookies its caller passes, as it did before - a shared jar would let one
fetch's anti-bot cookie change what the next one sees, which is a
behaviour change nobody asked for.

The Session methods are what tests stub (requests.Session.get etc.),
since that is the call that actually leaves the process.
"""
from __future__ import annotations

import http.cookiejar
import threading
import time
from typing import Dict, Mapping, Optional, cast
from urllib.parse import urlparse

import requests

from .applog import get_logger
from .hard_timeout import HardTimeoutError

log = get_logger("net")

# The app's own identity. Sites that need to see a browser (ascii2d,
# Google) or ask for a specific format (Reddit) keep their own strings.
USER_AGENT = "hatate-linux/1.0 (+https://github.com/nostrenz/hatate-iqdb-tagger)"

# A 429 is retried once, and only when the wait it asks for is short: a
# long Retry-After is a rate limit to respect, not to sit through.
RETRY_AFTER_DEFAULT = 2.0
RETRY_AFTER_MAX = 10.0

CHUNK_BYTES = 64 * 1024

# The longest a request waits for a host's rate-limit window to reset.
# Past this it is refused at once, as a failed fetch - a search must not
# stall for minutes on one site.
MAX_HOST_WAIT = 30.0


class RateLimited(requests.RequestException):
    """The host said it has no requests left, and not for a while."""


_sessions: Dict[str, requests.Session] = {}
_resume_at: Dict[str, float] = {}             # host -> time.monotonic() it reopens
_lock = threading.Lock()


def session_for(url: str) -> requests.Session:
    """The shared Session for this URL's host."""
    host = (urlparse(url).netloc or "").lower()
    with _lock:
        session = _sessions.get(host)
        if session is None:
            session = requests.Session()
            session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            _sessions[host] = session
        return session


def close_all() -> None:
    """Closes every pooled connection - for shutdown, and for tests."""
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
        _resume_at.clear()
    for session in sessions:
        session.close()


def get(url: str, **kwargs) -> requests.Response:
    return request("get", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("post", url, **kwargs)


def head(url: str, **kwargs) -> requests.Response:
    return request("head", url, **kwargs)


def request(method: str, url: str, *, timeout: float, deadline: Optional[float] = None,
            retry_429: bool = True, **kwargs) -> requests.Response:
    """Sends one request through the host's shared Session.

    `timeout` is requests' own: per connect and per read. `deadline`, in
    seconds from now, bounds the whole exchange - a body still arriving
    when it passes is abandoned and HardTimeoutError raised, the same
    error callers already handle. The response comes back fully read
    either way, so .text and .content work as usual.
    """
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("User-Agent", USER_AGENT)
    host = (urlparse(url).netloc or "").lower()
    _wait_for_host(host)
    started = time.monotonic()
    send = getattr(session_for(url), method)

    def _once() -> requests.Response:
        if deadline is None:
            return send(url, headers=headers, timeout=timeout, **kwargs)
        left = deadline - (time.monotonic() - started)
        if left <= 0:
            raise HardTimeoutError(f"Timed out after {deadline:.0f}s")
        resp = send(url, headers=headers, timeout=min(timeout, left), stream=True, **kwargs)
        _read_within(resp, started, deadline)
        return resp

    resp = _once()
    _note_rate_limit(host, resp)
    if retry_429 and method in ("get", "head") and getattr(resp, "status_code", None) == 429:
        wait = _retry_after(resp)
        spare = None if deadline is None else deadline - (time.monotonic() - started)
        if wait is not None and (spare is None or wait < spare):
            log.info("%s answered 429 - retrying once in %.0fs", urlparse(url).netloc, wait)
            resp.close()
            time.sleep(wait)
            resp = _once()
    return resp


def _wait_for_host(host: str) -> None:
    with _lock:
        resume = _resume_at.get(host)
    if resume is None:
        return
    wait = resume - time.monotonic()
    if wait <= 0:
        return
    if wait > MAX_HOST_WAIT:
        raise RateLimited(f"{host} has no requests left for another {wait:.0f}s")
    log.info("%s has no requests left - waiting %.0fs for it to reset", host, wait)
    time.sleep(wait)


def _note_rate_limit(host: str, resp) -> None:
    """Remembers when a host that has run out will take requests again."""
    headers = getattr(resp, "headers", None)
    if not isinstance(headers, dict) and not hasattr(headers, "get"):
        return
    headers = cast(Mapping[str, str], headers)
    remaining_raw = headers.get("x-ratelimit-remaining")
    reset_raw = headers.get("x-ratelimit-reset")
    if remaining_raw is None or reset_raw is None:
        return
    try:
        remaining = float(remaining_raw)
        reset = float(reset_raw)
    except (TypeError, ValueError):
        return
    with _lock:
        if remaining < 1:
            _resume_at[host] = time.monotonic() + max(0.0, reset)
        else:
            _resume_at.pop(host, None)


def _read_within(resp, started: float, deadline: float) -> None:
    """Reads a streamed body into the response, or closes it at the deadline."""
    if not isinstance(resp, requests.Response):
        return                           # a test double - nothing to stream
    chunks = []
    try:
        for chunk in resp.iter_content(CHUNK_BYTES):
            chunks.append(chunk)
            if time.monotonic() - started > deadline:
                raise HardTimeoutError(
                    f"Timed out after {deadline:.0f}s (the server may be trickling data, "
                    "keeping the connection alive without actually finishing)")
    except BaseException:
        resp.close()
        raise
    resp._content = b"".join(chunks)
    resp._content_consumed = True


def _retry_after(resp) -> Optional[float]:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if raw is None:
        return RETRY_AFTER_DEFAULT
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return None                      # an HTTP date: a long wait, not a blip
    return wait if 0 <= wait <= RETRY_AFTER_MAX else None
