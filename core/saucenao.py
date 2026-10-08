"""SauceNAO reverse image search.

Uses the official JSON API (https://saucenao.com/user.php?page=search-api)
when an API key is configured, since it exposes structured tag data
(characters, materials/series, creators, ...). Falls back to scraping the
HTML search page (fewer tags, no key required, subject to stricter rate
limiting) if no key is set.
"""
from __future__ import annotations

import datetime
import json
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import requests
from bs4 import BeautifulSoup

from .applog import get_logger
from .config import SauceNaoSettings
from . import net
from .hard_timeout import HardTimeoutError
from .image_prep import prepare_upload_bytes
from .models import Tag, TagSource
from .paths import CONFIG_DIR
from .progress_ticker import OnTick, ProgressTicker

log = get_logger("saucenao")

def _looks_like_url(href: Optional[str]) -> bool:
    """True only for genuinely absolute URLs - never a bare filename or
    relative path, which isn't safe to hand to a browser opener."""
    if not href:
        return False
    href = href.strip()
    return href.startswith("//") or href.startswith("http://") or href.startswith("https://")


def _safe_int(value) -> Optional[int]:
    """SauceNAO's API isn't always consistent about whether quota fields
    come back as ints or numeric strings (this has caused a real crash -
    arithmetic on an int and a str raises TypeError). Coerces to int if
    at all possible, otherwise None rather than passing through a value
    of an unpredictable type."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("Could not parse SauceNAO quota value %r as an integer", value)
        return None


API_URL = "https://saucenao.com/search.php"
USER_AGENT = net.USER_AGENT

# Maps the SauceNAO JSON result field name to a namespace label.
TAG_TYPE_FIELDS = {
    "character": "ext_urls",  # placeholder; real fields handled in _tags_from_result
}


@dataclass
class SauceNaoMatch:
    url: str
    similarity: float
    title: Optional[str]
    source_name: Optional[str]
    thumb_url: Optional[str] = None
    tags: List[Tag] = field(default_factory=list)


@dataclass
class SauceNaoQuota:
    """SauceNAO's two rate-limit windows, as reported in its own API
    response headers. short = the ~30 second burst window, long = the
    daily quota. Only available when using the JSON API (needs an API
    key) - the HTML scrape path doesn't expose this."""
    short_remaining: Optional[int] = None
    short_limit: Optional[int] = None
    long_remaining: Optional[int] = None
    long_limit: Optional[int] = None


_last_quota: Optional[SauceNaoQuota] = None

# When _last_quota was read. SauceNAO reports how many requests are left
# in the burst window but never says when that window started, so the only
# thing that can be said with confidence is "at THIS moment there were N
# left" - which makes the observation time part of the reading.
_last_quota_at: float = 0.0

# SauceNAO's burst window, which it documents as 30 seconds. Waiting the
# full window from the last observation is deliberately conservative: the
# window may already be part-way through, so this can wait longer than
# strictly needed, and waiting a few seconds too long costs far less than
# a 429 does.
SHORT_WINDOW_SECONDS = 30.0

# Set when SauceNAO itself reports the DAILY allowance is gone, either
# through long_remaining hitting zero or through its error message. Kept
# separate from _last_quota because the error path can report the limit
# without returning usable quota numbers.
_daily_limit_reported = False

# Substrings SauceNAO uses when the daily allowance is spent. Matched
# case-insensitively against header["message"]. Deliberately excludes
# "30 second" / short-window wording - see is_daily_quota_exhausted.
DAILY_LIMIT_MARKERS = ("daily search limit", "daily limit", "search limit exceeded")


def seconds_until_short_window_clears(
    quota: Optional["SauceNaoQuota"], observed_at: float, now: float,
) -> float:
    """How long to hold off before the next SauceNAO request.

    Only the burst window is considered here. The daily allowance running
    out is a different thing entirely - it does not clear by waiting, and
    is_daily_quota_exhausted handles it.

    Returns 0 whenever there is any headroom at all. Pausing at one
    remaining would halve the throughput of a batch to avoid a 429 that
    has not happened, and the retry below covers the case where the
    estimate is wrong.
    """
    if quota is None or quota.short_remaining is None:
        return 0.0          # nothing observed yet - no reason to wait
    if quota.short_remaining > 0:
        return 0.0
    return max(0.0, SHORT_WINDOW_SECONDS - (now - observed_at))


def _sleep_interruptibly(seconds: float, label: str, on_tick: Optional[OnTick],
                         should_stop: Optional[Callable[[], bool]]) -> None:
    """Waits, but lets Stop be Stop.

    The batch worker checks its own stop flag every quarter second while
    pacing between images, so a wait here that ignored it would make
    pressing Stop hang for the rest of the burst window - up to half a
    minute of the button appearing not to work.
    """
    deadline = time.monotonic() + seconds
    with ProgressTicker(label, seconds, on_tick):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (should_stop is not None and should_stop()):
                return
            time.sleep(min(0.25, remaining))


def _wait_for_short_window(on_tick: Optional[OnTick] = None,
                           should_stop: Optional[Callable[[], bool]] = None) -> float:
    """Sleeps out the burst window if the last reading says it is spent.

    The app already paces itself between images, but that gap is a fixed
    delay chosen by the user and knows nothing about SauceNAO's counter -
    at 5 seconds it fires roughly six requests per burst window against a
    limit of seventeen, and collides whenever the window is already part
    used. This uses the number SauceNAO itself returned.
    """
    wait = seconds_until_short_window_clears(_last_quota, _last_quota_at, time.time())
    if wait <= 0:
        return 0.0
    log.info(
        "SauceNAO's %ss burst window is spent - waiting %.1fs before the next request",
        int(SHORT_WINDOW_SECONDS), wait,
    )
    _sleep_interruptibly(wait, "SauceNAO burst limit", on_tick, should_stop)
    return wait


def get_last_quota() -> Optional[SauceNaoQuota]:
    """Returns the most recently observed quota, or None if no JSON API
    search has been made yet this session. This is a live reading from
    SauceNAO itself, only updated by an actual search - there's no
    separate "check my quota" endpoint being polled."""
    return _last_quota


def describe_quota(quota: Optional["SauceNaoQuota"]) -> str:
    """The status-bar reading of SauceNAO's daily allowance.

    Prefers "used / limit" over "N left", because how much of the day's
    allowance is gone is what decides whether to keep searching, and a
    bare remaining count means nothing without knowing the size of the
    window it came from.

    SauceNAO does not always send the limit. When it doesn't, the
    remaining count is still worth showing on its own - it is the number
    that actually runs out. Nothing observed at all is blank rather than
    a zero, since "no search made yet this session" is not "no quota
    left".

    Only the daily (long) window appears here. short_remaining is the
    ~30-second burst limit, which clears by itself and would flicker.
    """
    if quota is None or quota.long_remaining is None:
        return ""
    if quota.long_limit is None:
        return f"SauceNAO: {quota.long_remaining} left today"
    used = quota.long_limit - quota.long_remaining
    return f"SauceNAO: {used}/{quota.long_limit} used today"


def describe_short_window(quota: Optional["SauceNaoQuota"]) -> str:
    """The ~30-second burst window, shown as a tooltip rather than in the
    label itself.

    Kept out of the label deliberately: this one clears by itself within
    seconds, so a status bar showing it would flicker during a batch and
    imply something was wrong. It is still worth being able to look up
    when searches seem to be pausing.
    """
    if quota is None or quota.short_remaining is None:
        return ""
    if quota.short_limit is None:
        return f"{quota.short_remaining} remaining in the current ~30s window"
    used = quota.short_limit - quota.short_remaining
    return f"{used}/{quota.short_limit} used in the current ~30s window"


def is_daily_quota_exhausted() -> bool:
    """Whether SauceNAO's DAILY allowance is spent, as of the last
    response it gave us.

    Only the daily (long) window counts. short_remaining hitting zero is
    the ~30-second burst limit, which clears by itself within seconds -
    treating that as "out of quota" would halt a batch over a delay the
    app is already designed to wait out. Likewise a bare HTTP 429 isn't
    enough on its own, since SauceNAO returns it for the short window
    too and it carries no numbers to tell them apart.
    """
    if _daily_limit_reported:
        return True
    quota = _last_quota
    return quota is not None and quota.long_remaining == 0


def reset_daily_limit_flag() -> None:
    """Clears the remembered "daily limit hit" state - for when the user
    resumes a paused search, since the allowance may have rolled over (or
    they may have upgraded their key) since it was set."""
    global _daily_limit_reported
    _daily_limit_reported = False


def daily_quota_reset_at(now: Optional[float] = None) -> datetime.datetime:
    """The next moment SauceNAO's daily allowance comes back.

    SauceNAO resets the daily quota at 00:00 UTC. Nothing in the API
    response says so - there is no header for it - so this is calendar
    math against the clock, not something read back from SauceNAO itself.
    If that assumption is ever wrong, this is the one place to fix it.
    """
    moment = datetime.datetime.fromtimestamp(
        now if now is not None else time.time(), tz=datetime.timezone.utc)
    today_midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_midnight + datetime.timedelta(days=1)


def seconds_until_daily_quota_resets(now: Optional[float] = None) -> float:
    """How long until the daily allowance resets, for a countdown. Prefer
    daily_quota_reset_at() for anything that outlives the moment it's
    computed - a stored duration goes stale the instant time passes, an
    absolute timestamp doesn't (DAN-486)."""
    moment = now if now is not None else time.time()
    return (daily_quota_reset_at(moment) - datetime.datetime.fromtimestamp(
        moment, tz=datetime.timezone.utc)).total_seconds()


# Where a paused-for-quota batch's state is persisted, so the Queue screen
# can show it on ANY later visit - not just the moment the pause happened,
# which an unattended overnight run gives nobody a chance to see (DAN-486).
QUOTA_PAUSE_FILE = CONFIG_DIR / "saucenao_quota_pause.json"


@dataclass
class QuotaPauseState:
    paused_at: float       # time.time() when the batch stopped
    searched: int          # images searched before pausing
    remaining: int         # images left unsearched
    reset_at: str          # daily_quota_reset_at(), isoformat() - absolute,
                            # so it stays correct no matter how long it sits unread


def record_quota_pause(searched: int, remaining: int) -> None:
    """Persists that a batch paused because the daily quota ran out.
    Called the moment it happens; read back by whatever later shows the
    Queue screen, including a future launch of the app."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        state = QuotaPauseState(
            paused_at=time.time(), searched=searched, remaining=remaining,
            reset_at=daily_quota_reset_at().isoformat(),
        )
        QUOTA_PAUSE_FILE.write_text(json.dumps(state.__dict__), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not persist the quota-pause state: %s", exc)


def clear_quota_pause() -> None:
    """Removes the persisted pause. Called when the user starts a new
    search - at that point they're already acting on it, so a stale banner
    left over from the last pause would be misleading, not helpful."""
    try:
        QUOTA_PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not clear the quota-pause state: %s", exc)


def get_quota_pause_state() -> Optional[QuotaPauseState]:
    """The persisted pause, or None if there isn't one or it can't be
    read - a missing/corrupt file means "nothing to show", the same as
    never having paused at all."""
    try:
        data = json.loads(QUOTA_PAUSE_FILE.read_text(encoding="utf-8"))
        return QuotaPauseState(
            paused_at=float(data["paused_at"]),
            searched=int(data["searched"]),
            remaining=int(data["remaining"]),
            reset_at=str(data["reset_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def describe_quota_pause(state: "QuotaPauseState", now: Optional[float] = None) -> str:
    """The real, known reset time - not a hand-waved "resumes in ~Xh".
    The DAN-470 mockup deliberately avoided inventing a countdown here;
    this only ever reports what daily_quota_reset_at() actually computed.
    """
    try:
        reset_at = datetime.datetime.fromisoformat(state.reset_at)
    except ValueError:
        return (f"SauceNAO paused after {state.searched} searched, "
                f"{state.remaining} left unsearched")
    moment = datetime.datetime.fromtimestamp(
        now if now is not None else time.time(), tz=datetime.timezone.utc)
    remaining_seconds = max(0.0, (reset_at - moment).total_seconds())
    hours, rem = divmod(int(remaining_seconds), 3600)
    minutes = rem // 60
    eta = f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
    return (
        f"Paused: SauceNAO daily quota exhausted - {state.searched} searched, "
        f"{state.remaining} left unsearched. Resets {reset_at.strftime('%H:%M UTC')} "
        f"(in {eta})"
    )


class SauceNaoError(Exception):
    pass


def search(
    image_path: str, settings: SauceNaoSettings, timeout: float = 30.0,
    on_tick: Optional[OnTick] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[SauceNaoMatch]:
    """If given, on_tick(label, remaining, total) fires roughly once a
    second while the request is in flight, purely for UI countdown
    feedback - a single HTTP request has no native progress of its own."""
    if settings.api_key and settings.use_json_api:
        log.debug("Searching SauceNAO (JSON API, db=%s) for %s", settings.dbmask, image_path)
        return _search_json(image_path, settings, timeout, on_tick, should_stop)
    log.debug("Searching SauceNAO (HTML scrape, no API key configured) for %s", image_path)
    return _search_html(image_path, settings, timeout, on_tick)


def _search_json(
    image_path: str, settings: SauceNaoSettings, timeout: float, on_tick: Optional[OnTick] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[SauceNaoMatch]:
    params = {
        "output_type": 2,  # JSON
        "api_key": settings.api_key,
        "db": settings.dbmask,
    }
    def _post():
        try:
            data, filename = prepare_upload_bytes(image_path)
            files = {"file": (filename, data, "application/octet-stream")}
            with ProgressTicker("Waiting on SauceNAO", timeout, on_tick):
                return net.post(API_URL, params=params, files=files,
                                timeout=timeout, deadline=timeout)
        except OSError as exc:
            log.error("Could not read %s for SauceNAO upload: %s", image_path, exc)
            raise SauceNaoError(f"Could not read image: {exc}") from exc
        except HardTimeoutError as exc:
            log.error("SauceNAO JSON request for %s exceeded the hard deadline: %s", image_path, exc)
            raise SauceNaoError(f"SauceNAO request timed out: {exc}") from exc
        except requests.RequestException as exc:
            log.error("SauceNAO JSON request failed: %s", exc)
            raise SauceNaoError(f"SauceNAO request failed: {exc}") from exc

    _wait_for_short_window(on_tick, should_stop)
    resp = _post()

    log.debug("SauceNAO responded HTTP %d", resp.status_code)
    if resp.status_code == 429:
        # Retried once rather than given up on, because giving up is not
        # free: the image falls through to the secondary engine, gets a
        # weaker result, and that result is CACHED and marked searched -
        # so a burst collision costs it its primary-engine answer
        # permanently. One wait of the burst window buys it back.
        #
        # The wait is unconditional here: a 429 means the window really is
        # spent whatever the last reading said, and the reading may be
        # stale or absent.
        log.warning("SauceNAO rate limit hit (429) - waiting out the burst window and retrying once")
        _sleep_interruptibly(SHORT_WINDOW_SECONDS, "SauceNAO burst limit", on_tick, should_stop)
        if should_stop is not None and should_stop():
            raise SauceNaoError("Search stopped while waiting out SauceNAO's rate limit")
        resp = _post()
        log.debug("SauceNAO retry responded HTTP %d", resp.status_code)

    if resp.status_code == 413:
        raise SauceNaoError("SauceNAO rejected the image as too large, even after downscaling")
    if resp.status_code == 429:
        log.warning("SauceNAO still rate limited after waiting the burst window")
        raise SauceNaoError("SauceNAO rate limit reached, slow down the search delay")
    if resp.status_code != 200:
        raise SauceNaoError(f"SauceNAO returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        log.error("SauceNAO returned non-JSON body despite output_type=2")
        raise SauceNaoError("SauceNAO returned invalid JSON") from exc

    header = data.get("header", {})

    if not header:
        # An empty/missing "header" object isn't a normal SauceNAO
        # response shape - status.get("status", 0) would otherwise
        # default to 0 (success) purely because the key is absent,
        # silently treating a malformed response as a confirmed
        # zero-results search rather than a real failure.
        log.warning("SauceNAO JSON response had no header object - treating as a failure, not a confirmed result")
        raise SauceNaoError("SauceNAO's response didn't include the expected header data")

    global _last_quota, _last_quota_at
    _last_quota_at = time.time()
    _last_quota = SauceNaoQuota(
        short_remaining=_safe_int(header.get("short_remaining")),
        short_limit=_safe_int(header.get("short_limit")),
        long_remaining=_safe_int(header.get("long_remaining")),
        long_limit=_safe_int(header.get("long_limit")),
    )
    log.debug(
        "SauceNAO quota: %s/%s (short window), %s/%s (today)",
        _last_quota.short_remaining, _last_quota.short_limit,
        _last_quota.long_remaining, _last_quota.long_limit,
    )

    global _daily_limit_reported
    if _last_quota.long_remaining == 0:
        if not _daily_limit_reported:
            log.warning("SauceNAO's daily search allowance is now spent (long_remaining=0)")
        _daily_limit_reported = True

    if header.get("status", 0) < 0:
        message = header.get("message", "Unknown SauceNAO error")
        # ...and also when the error text says so, since an over-quota
        # response doesn't always carry usable quota numbers.
        if any(marker in str(message).lower() for marker in DAILY_LIMIT_MARKERS):
            log.warning("SauceNAO reported the daily search limit is exhausted: %s", message)
            _daily_limit_reported = True
        log.error("SauceNAO reported an error: %s", message)
        raise SauceNaoError(message)

    matches: List[SauceNaoMatch] = []
    skipped_no_url = 0
    for result in data.get("results", []):
        rheader = result.get("header", {})
        rdata = result.get("data", {})
        similarity = float(rheader.get("similarity", 0))
        # One SauceNAO result can list the SAME image on several sites -
        # ext_urls is a list, and a post mirrored to e621 and Sankaku
        # comes back as one result with two URLs. Keeping only the first
        # threw the other site away, and because the label was taken from
        # SauceNAO's index rather than the URL, the two could disagree:
        # the match would read "e621" but open Sankaku. Each URL becomes
        # its own candidate, so the dropdown offers the same choice
        # SauceNAO itself shows, and per-site filtering applies to each.
        url_list = [u for u in (rdata.get("ext_urls") or []) if _looks_like_url(u)]
        if not url_list:
            skipped_no_url += 1
            continue  # no usable absolute URL for this result, skip it

        tags = _tags_from_result(rdata, settings)
        seen_urls = set()
        for url in url_list:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            matches.append(
                SauceNaoMatch(
                    url=url,
                    similarity=similarity,
                    title=rdata.get("title") or rdata.get("source"),
                    source_name=rheader.get("index_name"),
                    thumb_url=rheader.get("thumbnail"),
                    tags=tags,
                )
            )
        if len(url_list) > 1:
            log.debug(
                "SauceNAO listed one image on %d sites (%s) - each is now its own candidate",
                len(url_list), ", ".join(url_list),
            )

    matches.sort(key=lambda m: -m.similarity)
    log.debug(
        "SauceNAO JSON parsed into %d match(es) (%d skipped for lacking a usable URL)",
        len(matches), skipped_no_url,
    )
    return matches


def _tags_from_result(rdata: dict, settings: SauceNaoSettings) -> List[Tag]:
    """Pull character/material/creator style fields out of a SauceNAO JSON
    result, filtered to the tag types the user enabled in Settings > SauceNAO."""
    field_map = {
        "character": rdata.get("characters"),
        "material": rdata.get("material"),
        "creator": rdata.get("creator") or rdata.get("member_name") or rdata.get("author_name"),
    }

    tags: List[Tag] = []
    for tag_type in settings.tag_types:
        raw = field_map.get(tag_type)
        if not raw:
            continue
        names = raw if isinstance(raw, list) else [n.strip() for n in str(raw).split(",")]
        for name in names:
            name = name.strip()
            if not name:
                continue
            namespace = settings.namespace or tag_type
            tags.append(Tag(name=name, source=TagSource.SEARCH_ENGINE, namespace=namespace))
    return tags


def _search_html(
    image_path: str, settings: SauceNaoSettings, timeout: float, on_tick: Optional[OnTick] = None,
) -> List[SauceNaoMatch]:
    params = {"db": settings.dbmask}
    try:
        data, filename = prepare_upload_bytes(image_path)
        files = {"file": (filename, data, "application/octet-stream")}
        with ProgressTicker("Waiting on SauceNAO", timeout, on_tick):
            resp = net.post(API_URL, params=params, files=files,
                            timeout=timeout, deadline=timeout)
    except OSError as exc:
        log.error("Could not read %s for SauceNAO upload: %s", image_path, exc)
        raise SauceNaoError(f"Could not read image: {exc}") from exc
    except HardTimeoutError as exc:
        log.error("SauceNAO HTML request for %s exceeded the hard deadline: %s", image_path, exc)
        raise SauceNaoError(f"SauceNAO request timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("SauceNAO HTML request failed: %s", exc)
        raise SauceNaoError(f"SauceNAO request failed: {exc}") from exc

    log.debug("SauceNAO (HTML) responded HTTP %d", resp.status_code)
    if resp.status_code == 413:
        raise SauceNaoError("SauceNAO rejected the image as too large, even after downscaling")
    if resp.status_code != 200:
        raise SauceNaoError(f"SauceNAO returned HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "lxml")
    matches: List[SauceNaoMatch] = []
    blocks = soup.select("div.result")
    log.debug("Found %d result block(s) in SauceNAO HTML response", len(blocks))
    for block in blocks:
        if "result-hidden" in (block.get("class") or []):
            continue
        link = block.select_one("div.resulttitle a, div.resultcontentcolumn a")
        sim_el = block.select_one("div.resultsimilarityinfo")
        if not link or not sim_el:
            log.debug("Result block missing link or similarity element, skipping")
            continue
        href = link.get("href", "")
        if not _looks_like_url(href):  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
            log.debug("Result block href %r not a usable absolute URL, skipping", href)
            continue
        try:
            similarity = float(sim_el.get_text(strip=True).replace("%", ""))
        except ValueError:
            similarity = 0.0
        title_el = block.select_one("div.resulttitle")
        img_el = block.select_one("img")
        thumb_url = img_el.get("src") if img_el else None
        if thumb_url and thumb_url.startswith("//"):  # type: ignore[union-attr]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
            thumb_url = "https:" + thumb_url  # type: ignore[operator]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
        matches.append(
            SauceNaoMatch(
                url=href,  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
                similarity=similarity,
                title=title_el.get_text(strip=True) if title_el else None,
                source_name=None,
                thumb_url=thumb_url,  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
                tags=[],
            )
        )

    matches.sort(key=lambda m: -m.similarity)
    log.debug("SauceNAO HTML parsed into %d match(es)", len(matches))
    return matches
