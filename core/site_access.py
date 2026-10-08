"""Per-site request settings: the headers, cookies and credentials a
site needs before it will answer as it answers a browser.

Split out of core/search_engine.py; everything here is re-exported there.
"""
from __future__ import annotations

import base64
import threading
from typing import List, Optional
from urllib.parse import urlparse



from .applog import get_logger
from .config import Settings
from . import net

log = get_logger("search")
# The app's own identity, from core/net.py - here because everything that
# sends a request imports this module.
USER_AGENT = net.USER_AGENT


# Pixiv rejects obvious bot User-Agents outright - our normal descriptive
# UA gets HTTP 403 from its web/AJAX endpoints. Established Pixiv tools
# (pixivpy among them) send a browser UA plus a Referer for exactly this
# reason. Scoped to Pixiv only; every other site keeps the honest,
# descriptive UA that names this project.
PIXIV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.pixiv.net/",
    "Accept": "application/json",
}


# Sankaku answers the SAME post URL differently depending on the
# User-Agent. With this project's descriptive one it bounces every post to
# the browse index, which says nothing about whether the post exists; with
# a browser's it will actually say "No Content" for a post it won't show.
# Confirmed against a live logged-in session on one post, repeatedly:
#
#   app UA      -> /posts                 (no information)
#   browser UA  -> /en/posts/show_empty   ("No Content")
#
# So without this the availability check literally cannot see the answer,
# and dead Sankaku matches survive as "unverifiable".
SANKAKU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://chan.sankakucomplex.com/",
}


# DeviantArt's auth/auth_secure cookies are pasted out of a browser
# session, and this project's own descriptive User-Agent is not what that
# session was issued to. Sites behind bot-protection (DeviantArt is one)
# commonly treat a session cookie replayed under a materially different
# UA as suspicious and cut it short rather than honouring its stated
# lifetime - the same class of problem PIXIV_HEADERS and SANKAKU_HEADERS
# exist for above. Scoped to deviantart.com/fav.me only.
DEVIANTART_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.deviantart.com/",
}


def e621_auth_header(settings) -> Optional[dict]:
    """HTTP Basic auth for e621, when credentials are configured.

    e621 documents an API key for third-party tools rather than expecting
    a scraped session cookie, so this uses that. Both halves are required
    - a username with no key authenticates nothing, and sending a
    half-filled header would turn every e621 request into a 401.
    """
    user = (getattr(settings, "e621_username", "") or "").strip()
    key = (getattr(settings, "e621_api_key", "") or "").strip()
    if not user or not key:
        return None
    token = base64.b64encode(f"{user}:{key}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def headers_for_url(url: str, settings=None) -> Optional[dict]:
    """Site-specific request headers, or None to use the default.

    `settings` is optional because most of these depend only on the host.
    e621's does not: it carries the user's own API key, so it is only
    added when there are settings to read it from.
    """
    if _host_is(url, "pixiv.net"):
        return dict(PIXIV_HEADERS)
    if _host_is(url, "sankakucomplex.com"):
        return dict(SANKAKU_HEADERS)
    if _host_is(url, "deviantart.com") or _host_is(url, "fav.me"):
        return dict(DEVIANTART_HEADERS)
    if _host_is(url, "reddit.com") or _host_is(url, "redd.it"):
        # Reddit asks API clients to name themselves this way, and blocks
        # generic agents more readily. See core/reddit.py.
        from .reddit import USER_AGENT as REDDIT_USER_AGENT
        return {"User-Agent": REDDIT_USER_AGENT}
    if settings is not None and any(
        _host_is(url, h) for h in ("e621.net", "e926.net", "e6ai.net")
    ):
        auth = e621_auth_header(settings)
        if auth:
            # The descriptive User-Agent goes with it: e621 blocks generic
            # ones outright, and an auth header on a blocked request just
            # sends the key somewhere it will be refused.
            return {"User-Agent": USER_AGENT, **auth}
    return None


def parse_cookie_string(raw: str) -> dict:
    """Parses a browser "Cookie:" header value into a dict.

    Accepts what someone can actually copy out of devtools -
    "login=you; pass_hash=abc123" - rather than demanding named fields
    per site. That matters for Sankaku in particular, which has changed
    its auth scheme more than once: a free-form string keeps working
    when the cookie names change, whereas hardcoded field names would
    quietly stop authenticating and look like a login failure.

    Tolerant on purpose: extra whitespace, a stray trailing semicolon,
    and values containing "=" (base64 and JWTs routinely do) are all
    handled, because this is pasted by hand.
    """
    cookies = {}
    # Newlines count as separators too. Selecting several rows in a
    # browser's cookie panel yields one pair per LINE, not the
    # semicolon-joined form a Cookie header uses - and without this,
    # such a paste becomes a single cookie whose value has the remaining
    # pairs buried in it, which authenticates as nothing at all.
    normalized = (raw or "").replace("\r", "\n").replace("\n", ";")
    for part in normalized.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")   # split on the FIRST = only
        name = name.strip()
        value = value.strip()
        if name:
            cookies[name] = value
    return cookies


# A browser's cookie panel truncates long values for display, so text
# selected straight from the table arrives cut off - usually with the
# ellipsis the panel drew. A truncated session cookie is accepted by the
# site as simply invalid: requests succeed, nothing errors, and every
# account-only post silently keeps looking deleted. Worth catching at
# paste time rather than leaving as a mystery.
TRUNCATION_HINTS = ("\u2026", "...")

# Session cookies are long. Anything much shorter is either a different
# kind of cookie or a fragment of one.
SUSPICIOUSLY_SHORT_SESSION_CHARS = 100


# The cookie carrying an Anime-Pictures login. Confirmed by testing an
# account-only post: this name returns the post, every other plausible
# name and the anonymous request return 403. Its value is a JWT, which
# matters for parsing - see animepictures_cookies below.
ANIMEPICTURES_SESSION_COOKIE = "anime_pictures_jwt"


def animepictures_cookies(raw: str) -> Optional[dict]:
    """The user's Anime-Pictures cookies, accepting a bare token too.

    Pasting just the VALUE is the natural mistake to make: a browser's
    cookie panel shows name and value in separate columns, so copying
    "the cookie" gets you the value alone. Worse, the value is a JWT and
    contains no "=" at all, so the name=value parser finds nothing in it
    and the field looks empty - the app then says "no cookies entered"
    about a string the user definitely just entered.

    So a lone token with no separators is paired with the session
    cookie's known name rather than thrown away.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = parse_cookie_string(raw)
    if parsed:
        return parsed
    # Nothing parsed, so there is no "name=" anywhere. Treat it as a bare
    # token only if it really is one - a single run of non-space text.
    if ";" not in raw and len(raw.split()) == 1:
        log.debug("Anime-Pictures cookie pasted as a bare value - assuming %s",
                  ANIMEPICTURES_SESSION_COOKIE)
        return {ANIMEPICTURES_SESSION_COOKIE: raw}
    return None


def cookie_paste_warnings(raw: str, session_name: str = "_sankakuchannel_session") -> List[str]:
    """Problems worth mentioning about a pasted cookie string.

    `session_name` is the cookie that actually carries the login for the
    site being configured, so the advice names the right one - warning a
    user about a Sankaku cookie while they configure Anime-Pictures is
    worse than saying nothing.

    Advisory only - none of these block anything, because an unfamiliar
    cookie layout is far more likely to mean the site changed than that
    the user got it wrong.
    """
    warnings: List[str] = []
    cookies = parse_cookie_string(raw)
    if not cookies:
        return warnings

    for name, value in cookies.items():
        if any(hint in value for hint in TRUNCATION_HINTS):
            warnings.append(
                f"{name} looks truncated - its value contains an ellipsis, which is what the "
                "browser's cookie panel shows when a value is too long to display. "
                "Right-click the row and use Copy Value instead of selecting the text."
            )

    session_cookies = {
        n: v for n, v in cookies.items()
        if "session" in n.lower() or n.lower() == session_name.lower()
    }
    if not session_cookies:
        warnings.append(
            f"No session cookie found. The one carrying your login is normally named "
            f"{session_name}; without it these are only display preferences and "
            "you'll still be treated as logged out."
        )
    else:
        for name, value in session_cookies.items():
            if len(value) < SUSPICIOUSLY_SHORT_SESSION_CHARS and not any(
                hint in value for hint in TRUNCATION_HINTS
            ):
                warnings.append(
                    f"{name} is only {len(value)} characters, which is short for a session "
                    "cookie - check the whole value was copied."
                )
    return warnings


def cookies_for_url(url: str, settings: Settings) -> Optional[dict]:
    """Site-specific auth cookies, when the user has configured them.

    Pixiv: it dropped username/password auth for third-party tools, so a
    PHPSESSID copied from a logged-in browser session is the practical
    way to see works that require login (Pixiv added a per-work
    "logged-in users only" setting in 2025) and R-18 content, which is
    otherwise hidden from anonymous requests.

    Sankaku: adult and account-only posts aren't shown to anonymous
    requests at all - it redirects them to a blank page rather than
    returning an error, so without cookies they look indistinguishable
    from deleted posts and get dropped as dead links.

    Anime-Pictures: some posts are account-only, and its API answers 403
    for them - so without cookies there are no tags, no dimensions and no
    preview at all, rather than a degraded version of them. The substring
    match deliberately covers api.anime-pictures.net too, which is the
    host that actually serves the post data and therefore the one that
    has to be authenticated.

    Returns None rather than an empty dict when nothing is configured, so
    requests doesn't get handed a pointless empty cookie jar."""
    if _host_is(url, "pixiv.net") and settings.pixiv_session_cookie.strip():
        return {"PHPSESSID": settings.pixiv_session_cookie.strip()}
    if _host_is(url, "sankakucomplex.com") and settings.sankaku_cookies.strip():
        parsed = parse_cookie_string(settings.sankaku_cookies)
        return parsed or None
    if _host_is(url, "anime-pictures.net"):
        return animepictures_cookies(settings.animepictures_cookies)
    # deviantart.com covers backend.deviantart.com, which is the host that
    # actually answers with the image URL and therefore the one that has
    # to recognise the session.
    if (_host_is(url, "deviantart.com") or _host_is(url, "fav.me")) \
            and settings.deviantart_cookies.strip():
        return parse_cookie_string(settings.deviantart_cookies) or None
    return None


# DeviantArt reissues auth/auth_secure/userinfo with a fresh sliding expiry
# via Set-Cookie on every response it serves - confirmed live against an
# anonymous request (DAN-122/DAN-123). A browser tab never feels the
# cookie's original expiry because of this; it keeps getting renewed. This
# app's shared Session (core/net.py) deliberately keeps no Set-Cookie for
# ANY host - see that module's docstring - so capturing this is done here,
# from the response object each individual fetch already gets back,
# scoped to these three cookie names and to deviantart.com/fav.me only.
ROTATABLE_DEVIANTART_COOKIES = ("auth", "auth_secure", "userinfo")

# Serializes the read-merge-write of settings.deviantart_cookies. Two
# things can touch that field at once: a second DeviantArt fetch rotating
# it on another QThread (SearchWorker/CandidateFetchWorker both trigger
# fetches), and the Settings dialog's Save on the GUI thread. The lock
# only protects this field's read-merge-write sequence, not the rest of
# Settings.save()'s whole-file write - see gui/settings_dialog.py's
# apply_to_settings for the other half of this: it only overwrites this
# field when the user actually edited the text, so a rotation merged here
# under the lock survives a Save that never touched the cookie box.
_deviantart_cookie_lock = threading.Lock()


def capture_rotated_deviantart_cookies(url: str, response, settings: Settings) -> None:
    """Folds a renewed DeviantArt session cookie back into settings.

    Scoped strictly to deviantart.com/fav.me - every other host's
    Set-Cookie stays discarded exactly as core/net.py's docstring says it
    should; this does not read `response.cookies` at all until the host
    check below passes.

    Only merges when DeviantArt actually served the page as the session we
    sent (2xx, or 304) - never on a 4xx/5xx. A challenge/error page (a 403
    from a bot check, a 429, a 5xx) can carry its own Set-Cookie - often a
    fresh ANONYMOUS session, sometimes even a reset one - and merging that
    over a working session would silently kill it, which is the exact bug
    this feature exists to fix, just self-inflicted instead of DeviantArt's
    doing. A session already configured (`current` non-empty) can pick up
    ANY of the three rotatable names on a genuine 2xx, not only the ones
    the user originally pasted - DeviantArt rotates the set together, and
    a session that gained e.g. a new `userinfo` alongside a renewed `auth`
    is still the user's own session, not one started from nothing. Only an
    entirely unconfigured session (nothing pasted) is left untouched below.

    Best-effort like everything else that touches `response.cookies` here:
    a response with no real cookie jar (a test double, mainly) fails the
    iteration below and is treated as "nothing to capture" rather than a
    hard error, the same as HEAD support and rate-limit headers elsewhere
    in this codebase are best-effort.
    """
    if not (_host_is(url, "deviantart.com") or _host_is(url, "fav.me")):
        return
    status = getattr(response, "status_code", None)
    if status is not None and not (200 <= status < 300 or status == 304):
        # DeviantArt didn't actually serve us the page as our session -
        # a challenge/error page's Set-Cookie is not a rotation of a
        # working session and must never overwrite one.
        return
    try:
        rotated = {c.name: c.value for c in response.cookies
                   if c.name in ROTATABLE_DEVIANTART_COOKIES and c.value}
    except (AttributeError, TypeError):
        return
    if not rotated:
        return

    with _deviantart_cookie_lock:
        current = parse_cookie_string(settings.deviantart_cookies)
        if not current:
            return  # no session configured - don't start persisting one nobody pasted
        if all(current.get(name) == value for name, value in rotated.items()):
            return  # DeviantArt sent the same values back - nothing to save
        current.update(rotated)
        settings.deviantart_cookies = "; ".join(f"{k}={v}" for k, v in current.items())
        settings.save()
        # Names only - never the values, which are live session credentials.
        log.info("DeviantArt renewed its session cookie(s) (%s) - saved the refresh",
                  ", ".join(sorted(rotated)))


def _host_is(url: str, domain: str) -> bool:
    """True when the URL's HOST is `domain` or a subdomain of it.

    These decide who gets sent the user's session cookies, and the URLs
    they judge come from IQDB/SauceNAO results - not trustworthy input.
    A substring test (what this used to be) also matched
    "anime-pictures.net.evil.example" and any URL merely mentioning the
    domain in a query string, handing that host a live login.
    """
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False  # malformed URL - certainly not a host we trust
    return host == domain or host.endswith("." + domain)
