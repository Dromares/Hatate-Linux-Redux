"""Google Lens reverse image search, through a real browser engine.

This is the search you get by dropping an image into images.google.com,
and on the kind of art this app deals with it is the strongest of the
lot - it routinely places a picture the booru engines and Cloud Vision
both miss. It is a separate engine from Google Images (Cloud Vision) on
purpose: different index, different requirements, its own toggle.

Getting at it costs more than any other engine here. CONFIRMED against
the live site:

  * The upload still works without a browser. POST /searchbyimage/upload
    answers 303 with a real result token.
  * The page it redirects to contains no results at all - a ~92KB
    JavaScript bootstrap. Every surface tried (udm=26, udm=48, tbm=isch,
    no-udm, lens.google.com/v3/upload, the asearch=arc fragment) is the
    same shell, and an old-browser User-Agent gets "Update your browser"
    rather than the basic HTML page it used to serve. That is what the
    Cloud Vision engine exists to work around.
  * Rendering that page in QtWebEngine sometimes reaches Google's
    "unusual traffic" check with a CAPTCHA instead - seen with a fresh
    profile and with a persistent one warmed up on google.com. It is
    not what happens every time: real runs from a normal desktop got no
    check at all and simply took their time.
  * The page is SLOW. MEASURED from a real run: it reaches the results
    URL in about a second and then leaves its body empty for well over
    half a minute while its JavaScript fetches the results. Being
    impatient with it is the single easiest way to break this engine -
    an earlier version gave up after twelve seconds and re-uploaded,
    and the log then showed the page it had abandoned fetching its
    results moments later.

So this engine does not try to look like something it isn't. Nothing
here spoofs an automation signal or works around the check. When Google
challenges, the renderer puts the page in front of the user to answer
themselves, once, and the profile keeps the cookie for the rest of the
run. A challenge the user dismisses stands the engine down for the
session rather than asking again on every image.

The upload happens INSIDE the browser, not over plain HTTP, and that is
not incidental. Google ties a visual search to the session that created
it: uploading with one client and then opening the result token in
another gets "this visual search has expired", which is exactly what
uploading with requests and rendering in QtWebEngine produced. So the
image is handed to the page and posted from there, and one session owns
both halves.

The rendering itself belongs to the GUI - a browser engine has to live
on the main thread - so this module holds no Qt. The GUI installs a
renderer with set_renderer() at startup; without one this engine
reports that it cannot run rather than pretending it found nothing.
"""
from __future__ import annotations

import base64
import html as html_module
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from PIL import Image

from . import boorus, engine_alerts, lens_browser, reddit
from .applog import get_logger
from .google_images import SEARCH_BY_IMAGE_URL, host_label, is_google_host
from .image_prep import prepare_upload_bytes
from .progress_ticker import OnTick, ProgressTicker

log = get_logger("google_lens")

# Above Cloud Vision's 78 - Lens is the stronger index of the two - and
# below ascii2d's colour search at 86, because like every engine here
# that scrapes rather than measures, Lens reports no similarity at all.
# Its ordering is Google's relevance ranking, so these stay ordinal.
SIMILARITY_START = 80.0
SIMILARITY_STEP = 2.0
SIMILARITY_FLOOR = 50.0
MAX_RESULTS = 8

# A page can hold 8+ taggable EXACT matches on its own (DAN-342's capture
# of reactor.cc/post/5351071 had 8 ranked ahead of it), which left no
# room in MAX_RESULTS for any taggable VISUAL match, however relevant -
# not a rare case, a structural one. Reserving a couple of slots for the
# best-ranked taggable visual matches fixes that without raising
# MAX_RESULTS (each kept match costs a fetch in
# measure_ordinal_similarities, so the cap itself stays put). Small on
# purpose: this only needs to stop total crowd-out, not hand visual
# matches parity with exact ones.
RESERVED_VISUAL_SLOTS = 2

# The image is carried into the page as a base64 string inside a script,
# so it is re-encoded smaller than the other engines' 7MB budget - a
# 7MB file would mean a ~9.5MB line of JavaScript. Plenty of detail is
# left for matching, and Google downsamples anything larger anyway.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# The least time a Lens search gets, whatever the general search timeout
# is set to. MEASURED from a real run: the page reaches its results URL
# in about a second, then leaves its body empty for well over half a
# minute while its JavaScript fetches the results. The general timeout
# is sized for one HTTP request, so here it is a floor, not a ceiling.
MIN_BUDGET_SECONDS = 90.0

# Read out of the rendered page rather than off its markup. Lens's class
# names are generated and change constantly, but a result is always an
# anchor to somewhere that isn't Google, and the JS DOM has already
# resolved every relative href and lazy-loaded thumbnail for us. Kept
# deliberately dumb - it collects, and Python decides what counts.
# The Exact matches tab's tiles, read from the RENDERED page.
#
# Its results carry no link and no thumbnail in the response body - the
# image there is a 1x1 placeholder with data-deferred="1", filled in by
# script after render. So the body gives a title and nothing else, and
# the title is all the earlier parsing had to work with.
#
# The rendered DOM has the real thing: a small base64 JPEG per tile,
# alongside the title and the source's dimensions. That thumbnail is
# what makes an exact match MEASURABLE against the local file - which
# matters more here than anywhere, because the exact tab is where the
# booru posts are.
EXACT_TILES_JS = r"""
(function () {
  var out = [];
  var images = document.querySelectorAll('img');
  for (var i = 0; i < images.length; i++) {
    var img = images[i], src = img.src || '';
    if (src.length < 80) continue;              // a placeholder, not a picture
    var node = img, text = '';
    for (var up = 0; up < 8 && node; up++) {
      node = node.parentElement;
      if (!node) break;
      var t = (node.innerText || '').replace(/\s+/g, ' ').trim();
      if (t.length > 3) { text = t.slice(0, 300); break; }
    }
    if (text) out.push({text: text, thumb: src});
  }
  return out;
})()
"""


_UPLOAD_JS = """
(function (b64, name, action, mime) {
  var bin = atob(b64), buf = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  var dt = new DataTransfer();
  dt.items.add(new File([buf], name, {type: mime}));

  var form = document.createElement('form');
  form.method = 'POST';
  form.action = action;
  form.enctype = 'multipart/form-data';
  var input = document.createElement('input');
  input.type = 'file';
  input.name = 'encoded_image';
  form.appendChild(input);
  document.body.appendChild(form);
  input.files = dt.files;
  form.submit();
  return true;
})(%s, %s, %s, %s)
"""


# Magic bytes, because the filename cannot be trusted to describe the
# content: prepare_upload_bytes re-encodes an oversized image to JPEG
# while keeping its original name.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
)


def media_type(image_bytes: bytes) -> Optional[str]:
    """The media type of these bytes, or None if they are not a picture
    in a format Google takes.

    THIS MATTERS. CONFIRMED against the live site: a JPEG offered as
    application/octet-stream is refused with "Something went wrong -
    can't read file", and Lens then renders an empty results page. From
    the outside that is indistinguishable from "Google found nothing",
    and it was the real cause of every empty Lens result before it.

    Which is also why an unrecognised file is None rather than being
    called a JPEG and hoped for. Naming a format the bytes are not is
    the exact mistake that failure was.
    """
    head = image_bytes[:16]
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


# What Lens actually reads. GIF is NOT here, despite being a picture
# format Google serves everywhere else. MEASURED on one file, uploaded
# both ways in the same session:
#
#   as GIF   1,477,525 bytes  ->  "can't read file", 0 matches
#   as JPEG    192,900 bytes  ->  no error, 3 matches
#
# So a GIF is converted like any format Lens cannot read. An ANIMATED
# picture of any type gets the same treatment for the same reason: Lens
# is searching for one image, and a still frame is what it can use.
LENS_READS = frozenset({"image/jpeg", "image/png", "image/webp", "image/bmp"})


def prepare_for_lens(image_path: str) -> tuple:
    """(bytes, filename, media type) for an upload Google will accept.

    The app takes AVIF, JPEG XL, TIFF and video from a Hydrus library,
    and Lens takes none of them - nor GIF, nor anything animated.
    Anything it cannot read is re-encoded to JPEG here; anything that
    cannot be re-encoded - a video, most obviously - is refused with a
    reason rather than sent and silently rejected at the far end.
    """
    data, filename = prepare_upload_bytes(image_path, max_bytes=MAX_UPLOAD_BYTES)
    mime = media_type(data)
    if mime in LENS_READS and not _is_animated(data):
        return data, filename, mime

    converted = _to_jpeg(data)
    if converted is None:
        if mime is not None:
            # It IS a picture Lens could read, just not one it can be
            # given - an animation whose first frame would not decode.
            return data, filename, mime
        raise GoogleLensError(
            f"{os.path.basename(image_path)} is not in a format Google Lens accepts, and "
            "it could not be converted to one. Video and JPEG XL are the usual reasons; "
            "the other engines can still search it."
        )
    log.info("Converted %s to JPEG for Google Lens (%d -> %d bytes)",
             os.path.basename(image_path), len(data), len(converted))
    return converted, os.path.splitext(filename)[0] + ".jpg", "image/jpeg"


def _is_animated(data: bytes) -> bool:
    """Whether these bytes hold more than one frame.

    Lens is searching for one picture. An animation is not one, and the
    first frame is what it can actually use - which is also why an
    animated WEBP gets converted even though a still WEBP does not.
    """
    try:
        from PIL import Image
    except ImportError:                                # pragma: no cover
        return False
    try:
        with Image.open(io.BytesIO(data)) as opened:
            return getattr(opened, "n_frames", 1) > 1
    except Exception:                                  # noqa: BLE001
        return False


def _to_jpeg(data: bytes) -> Optional[bytes]:
    """The same picture as a JPEG, or None if it is not a picture at all.

    Pillow is asked rather than the file extension: a Hydrus library
    names files by hash, so the extension is whatever the importer
    decided and not necessarily what the bytes are.
    """
    try:
        from PIL import Image
    except ImportError:                                # pragma: no cover
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.seek(0)      # the first frame of an animation
            # Flattened onto white: JPEG has no alpha, and the default
            # conversion leaves a transparent background black.
            if opened.mode in ("RGBA", "LA", "P"):
                rgba = opened.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
            else:
                flat = opened.convert("RGB")
            buffer = io.BytesIO()
            flat.save(buffer, format="JPEG", quality=90)
    except Exception as exc:                           # noqa: BLE001 - any decode failure
        log.debug("Could not convert an upload to JPEG: %s", exc)
        return None
    return buffer.getvalue()


def upload_script(image_bytes: bytes, filename: str) -> str:
    """The JavaScript that uploads one image from inside the page.

    Arguments go in through json.dumps rather than string interpolation:
    a filename is attacker-influenced in the sense that it comes off the
    user's disk, and a stray quote in it would otherwise turn into
    broken - or injected - script.
    """
    return _UPLOAD_JS % (
        json.dumps(base64.b64encode(image_bytes).decode("ascii")),
        json.dumps(filename),
        json.dumps(SEARCH_BY_IMAGE_URL),
        json.dumps(media_type(image_bytes) or "image/jpeg"),
    )


# Full sentences off Google's block page, not loose words. A real Lens
# results page is ~600KB of markup and script, and OBSERVED to contain
# "captcha" and "not a robot" somewhere in it - matching on those flagged
# a perfectly good page as a challenge and hung the search waiting for
# the user to answer something that was not there. The URL (/sorry/) is
# the reliable signal; this is only a backstop for a check served
# somewhere else.
@dataclass
class GoogleLensMatch:
    url: str
    thumb_url: Optional[str]
    similarity: float
    title: Optional[str] = None
    source_name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class GoogleLensError(Exception):
    pass


class GoogleLensUnavailableError(GoogleLensError):
    """No browser engine is available to render the results page.

    Either PyQt6-WebEngine is not installed, or this is running without
    a GUI. Latched: it is a property of the installation, so every
    remaining image would fail the same way.
    """


ENGINE_NAME = "Google Lens"
# One key per CONDITION, so the once-per-session rule in
# core/engine_alerts.py counts the missing dependency rather than the
# images that ran into it.
ALERT_KEYS = {
    lens_browser.MISSING_PLAYWRIGHT: "google-lens:playwright-not-installed",
    lens_browser.MISSING_CHROMIUM: "google-lens:chromium-not-installed",
}
UNKNOWN_ALERT_KEY = "google-lens:browser-unavailable"


def announce_unavailable(exc: lens_browser.LensBrowserUnavailable) -> bool:
    """Ask for a user-visible warning that Lens cannot run at all.

    Only for the unavailable case, never for an ordinary failed search:
    this one is a property of the installation, so it will not come right
    on the next image and the batch will otherwise finish looking as
    though Lens had searched every one.
    """
    missing = getattr(exc, "missing", None)
    if missing == lens_browser.MISSING_PLAYWRIGHT:
        body = ("Google Lens was skipped: the Playwright package it needs is not "
                "installed, so no browser could be started. Every image in this run "
                "was searched by the other engines only.")
    elif missing == lens_browser.MISSING_CHROMIUM:
        body = ("Google Lens was skipped: Playwright is installed but the Chromium "
                "browser it drives is not, so no browser could be started. Every "
                "image in this run was searched by the other engines only.")
    else:
        body = (f"Google Lens was skipped: its browser could not be started.\n\n{exc}")
    return engine_alerts.raise_alert(engine_alerts.EngineAlert(
        key=ALERT_KEYS.get(missing or "", UNKNOWN_ALERT_KEY),
        engine=ENGINE_NAME,
        title="Google Lens cannot run",
        body=body,
        # The exception's own text is the install command for the missing
        # piece, already written for the interpreter actually running -
        # see lens_browser.missing_dependency_message.
        remedy=str(exc) if missing else "",
    ))


class GoogleLensBlockedError(GoogleLensError):
    """Google challenged the request and the challenge went unanswered.

    Latched for the session, like ascii2d's Cloudflare block: once
    Google is challenging, asking again for every remaining image would
    spend a request each to be refused and report the same thing.
    """


_blocked = False

# How many images in a row may fail before the engine stands down. A
# batch must not spend the better part of a minute per image learning
# the same thing - but one bad page is not evidence of anything, so it
# takes a run of them.
MAX_CONSECUTIVE_FAILURES = 3
_consecutive_failures: List[int] = []


# After a robot check goes unanswered, Lens rests this long before asking
# Google again. Starting a search used to clear the stand-down at once,
# and re-searching a single image is exactly that - so every re-search
# went straight back to Google and met a fresh challenge. Seen
# 2026-09-24 as "an endless cycle of captchas".
CHALLENGE_REST_SECONDS = 30 * 60
_resting_until: Optional[float] = None       # time.monotonic()

# The least time between the starts of two Lens searches, however they
# were started - a batch, or one re-search after another. Google
# challenges on how often it is asked; the browser steps got much faster,
# and this keeps the rate it is asked at no higher than before.
MIN_SECONDS_BETWEEN_SEARCHES = 45.0
_next_turn: Optional[float] = None           # time.monotonic()
_turn_lock = threading.Lock()


def is_blocked() -> bool:
    return _blocked or is_resting()


def is_resting() -> bool:
    return _resting_until is not None and time.monotonic() < _resting_until


def resting_until() -> str:
    """When the rest ends, as a local clock time, e.g. "21:41"."""
    # Snapshot into a local before checking is_resting(): another thread can
    # clear _resting_until (reset_blocked_flag) between that check and the
    # subtraction below, which would otherwise crash on None - time.monotonic().
    until = _resting_until
    if until is None or not is_resting():
        return ""
    left = until - time.monotonic()
    return time.strftime("%H:%M", time.localtime(time.time() + left))


def reset_blocked_flag() -> None:
    """Called whenever a search starts. Clears a stand-down - but not a
    rest after an unanswered robot check, which is the point of it."""
    global _blocked
    _blocked = False
    _consecutive_failures.clear()


def end_rest() -> None:
    """Lets Lens ask Google again now. For tests, and anything that gives
    the user a way to say so explicitly."""
    global _resting_until, _next_turn
    _resting_until = None
    _next_turn = None


def wait_for_turn(on_tick: Optional[OnTick] = None,
                  should_stop=None) -> None:
    """Holds a search until MIN_SECONDS_BETWEEN_SEARCHES have passed since
    the last one started. The turn is claimed before waiting, so two
    searches arriving together are spaced out rather than both let go."""
    global _next_turn
    with _turn_lock:
        now = time.monotonic()
        start_at = max(now, _next_turn or now)
        _next_turn = start_at + MIN_SECONDS_BETWEEN_SEARCHES
    wait = start_at - now
    if wait <= 0:
        return
    log.info("Pacing Google Lens: waiting %.0fs so Google isn't asked too often", wait)
    while True:
        left = start_at - time.monotonic()
        if left <= 0 or (should_stop is not None and should_stop()):
            return
        if on_tick:
            on_tick("Pacing Google Lens", left, wait)
        time.sleep(min(1.0, left))


def _latch(message: str, error_type=GoogleLensBlockedError) -> GoogleLensError:
    global _blocked
    _blocked = True
    return error_type(message)


def _count_failure(message: str) -> GoogleLensError:
    """Report a failed image, standing the engine down after a run of them.

    One image failing says nothing - a page can go wrong. Several in a
    row says Google is not answering this browser at all, and every
    remaining image would spend the same half-minute finding that out.
    """
    _consecutive_failures.append(1)
    if len(_consecutive_failures) >= MAX_CONSECUTIVE_FAILURES:
        return _latch(
            f"{message} That has now happened to {len(_consecutive_failures)} images in a "
            "row, so Google Lens is standing down for the rest of this run. Start the "
            "search again to retry it, or use the Google Images engine, which reaches "
            "Google through the Cloud Vision API instead."
        )
    return GoogleLensError(message)


def search(image_path: str, timeout: float = 30.0,
           on_tick: Optional[OnTick] = None,
           whole_image: bool = True) -> List[GoogleLensMatch]:
    """Upload an image to Google and read the rendered Lens results."""
    try:
        data, filename, _ = prepare_for_lens(image_path)
    except OSError as exc:
        log.error("Could not read %s for the Lens upload: %s", image_path, exc)
        raise GoogleLensError(f"Could not read image: {exc}") from exc

    log.debug("Uploading and rendering Lens results for %s", image_path)
    try:
        with ProgressTicker("Waiting on Google Lens", effective_timeout(timeout), on_tick):
            payloads, tiles = lens_browser.fetch_results_payloads(
                data, filename, effective_timeout(timeout), whole_image=whole_image)
    except lens_browser.LensBrowserUnavailable as exc:
        # The log entry stays where it was - core/engine_runner.py logs
        # this, and it is what a bug report is read from - but it is no
        # longer the only place it appears. A stood-down engine is
        # indistinguishable from one that searched and found nothing, so
        # the row said "not searched" and the user read it as "no match".
        # Told once per session, on the GUI thread, by whoever subscribed.
        announce_unavailable(exc)
        raise _latch(str(exc), GoogleLensUnavailableError) from exc
    except lens_browser.LensChallengeUnanswered as exc:
        global _resting_until
        _resting_until = time.monotonic() + CHALLENGE_REST_SECONDS
        log.error("Google Lens challenge went unanswered: %s - resting until %s",
                  exc, resting_until())
        # A rest, not a stand-down as well: the message promises Lens back
        # at a time, and a batch runs for hours. Latched, it stayed off
        # until the search was restarted - on 2026-09-25 it skipped every
        # image for an hour after its rest had ended.
        raise GoogleLensBlockedError(
            "Google asked to check you aren't a robot in the browser window and the "
            "check wasn't answered, so Lens is resting until "
            f"{resting_until()} - asking again straight away just brings another "
            "check. Turn Google Lens off under Settings > Engine to stop it altogether."
        ) from exc
    except lens_browser.LensBrowserError as exc:
        log.error("Google Lens browser failed for %s: %s", image_path, exc)
        raise _count_failure(f"the browser could not read the results ({exc})") from exc

    if not payloads:
        raise _count_failure(
            "Google returned a results page with nothing in it. If the browser window "
            "showed \"can't read file\", this image is one Google will not accept."
        )

    # Several pages are loaded per search - the first results, the
    # Visual matches tab, and whatever confirming the explicit-results
    # notice loads next - so the one that parses best wins. Choosing by
    # size instead threw real matches away: OBSERVED, the largest body
    # after that confirmation parsed to nothing while a smaller earlier
    # one held every match.
    visual: List[GoogleLensMatch] = []
    exact: List[GoogleLensMatch] = []
    # The rendered tiles first: they carry the same exact matches the
    # response body does, plus the thumbnail and dimensions the body
    # leaves out. The body is the fallback for anything they missed.
    for match in parse_exact_tiles(tiles):
        if all(match.url != existing.url for existing in exact):
            exact.append(match)
    for payload in payloads:
        parsed = parse_lens_payload(payload)
        if len(parsed) > len(visual):
            visual = parsed
        for match in parse_exact_matches(payload):
            if all(match.url != existing.url for existing in exact):
                exact.append(match)

    for match in _recover_rule34_searches(tiles, data):
        if all(match.url != existing.url for existing in exact):
            exact.insert(0, match)

    # Exact matches first: they are the same picture, where a visual
    # match only looks like it. Within each, sites that give tags go
    # ahead of the rest - see taggable_first. Both are then scored by
    # position, so the numbers stay ordinal and comparable.
    visual = [m for m in visual if all(m.url != e.url for e in exact)]
    exact, visual = _resolve_subreddits(exact, visual)
    matches = taggable_first(exact) + taggable_first(visual)
    for rank, match in enumerate(matches):
        match.similarity = max(SIMILARITY_START - rank * SIMILARITY_STEP, SIMILARITY_FLOOR)
    matches = keep_taggable_first(matches, MAX_RESULTS, visual_start=len(exact),
                                   reserve_visual=RESERVED_VISUAL_SLOTS)

    _consecutive_failures.clear()
    log.debug("Google Lens parsed %d response(s) into %d match(es) (%d exact)",
              len(payloads), len(matches), len(exact))
    return matches


# An exact-match tile for a rule34.xxx page Google reached through a
# search: "Rule 34 / parent:10755759 250x177 Rule 34". The search is
# group 1. See boorus/rule34.py, find_post_by_search.
_RULE34_SEARCH_TILE_RE = re.compile(
    r"^Rule 34 / (.+?)(?: \d[\d,]*x\d[\d,]*)?(?: Rule 34)?$")
# Searches that name one post's family or the post itself come first -
# they are the ones whose first page is sure to hold it.
_PRECISE_TAG_RE = re.compile(r"\b(?:parent|id|md5|pool):", re.IGNORECASE)
MAX_RULE34_SEARCHES = 3


def rule34_searches(tiles) -> List[str]:
    """The rule34.xxx searches named by exact-match tiles, precise first."""
    found: List[str] = []
    for tile in tiles or []:
        if not isinstance(tile, dict):
            continue
        hit = _RULE34_SEARCH_TILE_RE.match(str(tile.get("text") or "").strip())
        if hit and hit.group(1).strip() not in found:
            found.append(hit.group(1).strip())
    return ([q for q in found if _PRECISE_TAG_RE.search(q)]
            + [q for q in found if not _PRECISE_TAG_RE.search(q)])


def _recover_rule34_searches(tiles, image_bytes: bytes) -> List["GoogleLensMatch"]:
    """rule34.xxx posts Lens listed only by the search they were found in.

    Stops at the first one found: they are all exact matches of the same
    picture, and each search costs a page plus a page of thumbnails.
    """
    from .image_compare import dhash

    searches = rule34_searches(tiles)[:MAX_RULE34_SEARCHES]
    if not searches:
        return []
    local_hash = dhash(image_bytes, boorus.rule34.LISTING_HASH_SIZE)
    for tags in searches:
        found = boorus.rule34.find_post_by_search(tags, local_hash)
        if found:
            url, thumb, distance = found
            log.info("Recovered %s from Lens's \"Rule 34 / %s\" exact match "
                     "(distance %d)", url, tags, distance)
            return [GoogleLensMatch(url=url, thumb_url=thumb, similarity=0.0,
                                    title=f"Rule 34 / {tags}", source_name="rule34.xxx")]
    return []


def _resolve_subreddits(exact, visual):
    """Swaps each subreddit listing for the post its picture is in.

    A subreddit URL imported into Hydrus downloads the whole subreddit;
    see core/reddit.py. What cannot be resolved safely is dropped, and a
    URL that resolves to one already listed is not listed twice.
    """
    seen = set()
    out = ([], [])
    for group, kept in zip((exact, visual), out, strict=True):
        for match in group:
            url = reddit.resolve_listing(match.url, match.thumb_url)
            if url is None or url in seen:
                continue
            match.url = url
            seen.add(url)
            kept.append(match)
    return out


def _is_taggable(match: "GoogleLensMatch") -> bool:
    return boorus.find_parser(match.url) is not None


def taggable_first(matches: List["GoogleLensMatch"]) -> List["GoogleLensMatch"]:
    """Results from sites boorus/ can read tags from, ahead of the rest.

    Stable: each half keeps Google's order. Google ranks by relevance to
    a web searcher, which puts Pinterest and blogs on top; for this app a
    booru post is the more useful answer, since it is the one that tags
    the image. Reordering only moves which UNMEASURED ordinal score a
    result gets - measure_ordinal_similarities replaces those with real
    ones, and only a measured score can reach the auto-import threshold.
    """
    return ([m for m in matches if _is_taggable(m)]
            + [m for m in matches if not _is_taggable(m)])


def keep_taggable_first(
    matches: List["GoogleLensMatch"],
    limit: int,
    visual_start: int = 0,
    reserve_visual: int = 0,
) -> List["GoogleLensMatch"]:
    """Cuts the list to `limit` without letting untaggable sites crowd out
    ones that give tags.

    Lens ranks by what Google thinks is relevant, and the top of that is
    often Pinterest, blogs and wallpaper hosts - a link, but nothing
    boorus/ can read tags from. Cutting at `limit` in Google's order threw
    away a Danbooru or Pixiv post sitting at rank 12, which is the one
    result that would have tagged the image. So results from a site with
    a parser take the slots first, and the rest fill whatever is left.

    What is kept stays in the order it was given (and keeps the ordinal
    score its rank gave it) - this decides WHICH results survive, not how
    they rank. Still needed after taggable_first: a taggable VISUAL match
    sits behind every exact one, and must not lose its slot to an
    untaggable exact match.

    `visual_start`/`reserve_visual` close a second, structural gap the
    above doesn't: with 8+ taggable EXACT matches (indices < visual_start),
    `taggable[:limit]` is entirely exact matches, so no taggable VISUAL
    match (index >= visual_start) can ever survive, regardless of rank -
    see DAN-357/reactor.cc/post/5351071. Reserving up to `reserve_visual`
    slots for the best-ranked taggable visual matches guarantees them a
    chance without touching the common case: when the taggable pool
    already fits under `limit`, every taggable match is kept either way,
    so the result is unchanged.
    """
    if len(matches) <= limit:
        return matches
    taggable = [i for i, m in enumerate(matches) if _is_taggable(m)]
    taggable_visual = [i for i in taggable if i >= visual_start]
    kept = set(taggable_visual[:reserve_visual])
    for i in taggable:
        if len(kept) >= limit:
            break
        kept.add(i)
    for i in range(len(matches)):
        if len(kept) >= limit:
            break
        kept.add(i)
    return [m for i, m in enumerate(matches) if i in kept]


def effective_timeout(timeout: float) -> float:
    """The budget a Lens search actually gets.

    The general search timeout is sized for one HTTP request; this is a
    browser loading a JavaScript application, and MEASURED from a real
    run that takes well over half a minute. The setting is a floor.
    """
    return max(timeout, MIN_BUDGET_SECONDS)


_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.){2,400})"')


# Lens's "Exact matches" tab is a different animal from "Visual
# matches". It is server-rendered HTML, and CONFIRMED against the live
# site: its result tiles carry NO link at all - no href, no data
# attribute, and nothing in the response body but the title text. The
# destination is applied by script when the tile is clicked, so there is
# no URL to extract the way there is on the visual tab.
#
# That tab is also where the best matches are, being exact ones. What it
# does give is a title, and some sites put the post's id right in it -
# so where a title names a site whose post URL can be built from an id,
# the match is recovered from that. Each entry is verified by fetching
# the URL it builds and checking the post is really there; guessing a
# pattern for a site is not good enough, because a wrong URL looks like
# a real match until someone clicks it.
#
# The site's name is NOT reliably part of the title. MEASURED across two
# real responses: one rendered "Post 5674224: Ben_10 ... - Rule 34
# Paheal" with the name appended, the other rendered "Post 5674080: Chel
# Tanluca ..." with the name in a separate element 1,162 characters
# later. Requiring it in the title silently dropped every result of the
# second kind - which is how a real Paheal post went missing. So the
# name is looked for in the tile FOLLOWING the title instead, stopping
# at the next result so one tile cannot borrow its neighbour's site.
SITE_LABEL_WINDOW = 2500

@dataclass(frozen=True)
class _ExactSite:
    """A site whose Exact-matches title carries its post id.

    `pattern` captures that id. `label` is what Lens calls the site, and
    is only needed when the pattern alone could belong to more than one
    site - it is then looked for around the title as well. A pattern
    distinctive enough to identify the site on its own leaves it empty.
    """
    pattern: "re.Pattern[str]"
    template: str       # post URL, with one {} for the id
    host: str
    label: str = ""


EXACT_MATCH_SITES = (
    # "Post 5674224: Ben_10 Flipherrrr Gwen_Tennyson - Rule 34 Paheal".
    # Needs the label: "Post <id>:" is a shape other sites could share.
    _ExactSite(re.compile(r"Post (\d{3,9}):([^<\n]{0,200})"),
               "https://rule34.paheal.net/post/view/{}",
               "rule34.paheal.net", label="Rule 34 Paheal"),
    # "Rule 34 | 4585855 / - Rule 34". Self-identifying: the mirrors that
    # also carry "Rule 34" in their names title themselves quite
    # differently ("favela funk - Rule 34 XYZ", "... - Rule 34 World"),
    # none of them with a pipe and a bare id.
    _ExactSite(re.compile(r"Rule 34 \| (\d{3,9}) ?/"),
               "https://rule34.xxx/index.php?page=post&s=view&id={}",
               "rule34.xxx"),
    # "If it exists, there is porn of it / tekuho / 368224 - Rule34.us".
    # Self-identifying too: the id is the last segment before a site name
    # no other mirror shares, and its shape is nothing like rule34.xxx's.
    _ExactSite(re.compile(r"/ ?(\d{3,9}) ?- ?Rule34\.us"),
               "https://rule34.us/index.php?r=posts/view&id={}",
               "rule34.us"),
)

_TAG_RE = re.compile(r"<[^>]+>")


def parse_exact_matches(payload: str) -> List[GoogleLensMatch]:
    """Recover matches from the Exact matches tab's titles.

    Public for tests. Returns nothing for a page whose titles name no
    site this can rebuild a URL for, which is the common case - it adds
    to the visual matches rather than replacing them.
    """
    body = payload or ""
    found: List[tuple] = []
    seen = set()

    for site in EXACT_MATCH_SITES:
        for hit in site.pattern.finditer(body):
            if site.label and not _label_is_near(body, hit, site):
                continue
            url = site.template.format(hit.group(1))
            if url in seen:
                continue
            seen.add(url)
            title = html_module.unescape(_TAG_RE.sub("", hit.group(0))).strip()
            found.append((hit.start(), url, title, site.host))

    # Document order, so the page's own ranking survives being collected
    # one site at a time.
    found.sort(key=lambda row: row[0])
    return [
        GoogleLensMatch(url=url, thumb_url=None, similarity=0.0,   # ranked by the caller
                        title=title, source_name=host)
        for _, url, title, host in found
    ]


# "1,072x1,108" on a tile, next to the title.
TILE_DIMENSIONS_RE = re.compile(r"([\d,]{2,10})\s*[x\u00d7]\s*([\d,]{2,10})")


def parse_exact_tiles(tiles) -> List[GoogleLensMatch]:
    """Matches from the RENDERED exact-match tiles.

    The same titles parse_exact_matches reads out of the response body,
    but with the tile's thumbnail and stated dimensions attached - which
    the body does not carry. The thumbnail is what lets an exact match
    be compared against the local file instead of being given a made-up
    score, and the exact tab is where most real matches are.
    """
    found: List[GoogleLensMatch] = []
    seen = set()
    for tile in tiles or []:
        if not isinstance(tile, dict):
            continue
        text = str(tile.get("text") or "")
        thumb = str(tile.get("thumb") or "")
        if _is_placeholder_thumb(thumb):
            # Still a match - only the picture is withheld, so the post
            # page's own image is used instead of a blank.
            thumb = ""
        for site in EXACT_MATCH_SITES:
            hit = site.pattern.search(text)
            if not hit:
                continue
            if site.label and site.label.lower() not in text.lower():
                continue
            url = site.template.format(hit.group(1))
            if url in seen:
                continue
            seen.add(url)
            width, height = _tile_dimensions(text, hit.end())
            found.append(GoogleLensMatch(
                url=url,
                # A real thumbnail, PROVIDED SafeSearch is not set to
                # blur - with it on, the tile carries a 0.04 byte/pixel
                # placeholder instead, which hashes an identical image
                # to 78%. The browser turns that off once per profile;
                # _worth_hashing refuses anything that still looks like
                # a placeholder, so a stale setting cannot produce a
                # wrong number.
                thumb_url=thumb or None,
                similarity=0.0,          # ranked by the caller
                title=hit.group(0).strip(),
                source_name=site.host,
                # The size Google states for the source, which IS worth
                # having: it drives the "is this bigger than mine?"
                # comparison without fetching anything.
                width=width,
                height=height,
            ))
            break
    return found


# A lazy-loaded tile that hadn't loaded yet carries a 1x1 GIF as a data
# URI - 82 characters, past EXACT_TILES_JS's 80-character cut. CONFIRMED
# on a rule34.us match: it became the match's thumbnail, and the side by
# side showed a single transparent pixel. Decoded rather than judged by
# length, so only an image that really is a speck is refused.
PLACEHOLDER_MAX_SIDE = 4


def _is_placeholder_thumb(src: str) -> bool:
    if not src.startswith("data:") or "," not in src:
        return False
    try:
        with Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))) as im:
            return max(im.size) <= PLACEHOLDER_MAX_SIDE
    except (ValueError, OSError):
        return False            # not an image we can read - leave it to _worth_hashing


def _tile_dimensions(text: str, after: int):
    """The source's size, as the tile states it after the title."""
    found = TILE_DIMENSIONS_RE.search(text, after)
    if not found:
        return None, None
    try:
        return int(found.group(1).replace(",", "")), int(found.group(2).replace(",", ""))
    except ValueError:
        return None, None


def _label_is_near(body: str, hit, site: "_ExactSite") -> bool:
    """Whether the site's name appears in or just after this title.

    The name is NOT reliably part of the title. MEASURED across two real
    responses: one rendered "Post 5674224: Ben_10 ... - Rule 34 Paheal"
    with the name appended, the other rendered "Post 5674080: Chel
    Tanluca ..." with the name in a separate element 1,162 characters
    later. The search stops at the next result so one tile cannot borrow
    its neighbour's site.
    """
    window = body[hit.end():hit.end() + SITE_LABEL_WINDOW]
    neighbour = site.pattern.search(window)
    if neighbour:
        window = window[:neighbour.start()]
    label = site.label.lower()
    return label in hit.group(0).lower() or label in window.lower()


# A Lens result is a picture and a page it appears on - not necessarily
# the page it is the subject of. CONFIRMED on a real image: Google's own
# record paired zerochan's "Sakamata.Chloe.600.3699483.jpg" with the page
# zerochan.net/3700079, because post pages show related posts. 3699483
# was the picture searched for (same 1105x1565); 3700079 was a different
# one. Where a site names the post in its image filenames, the post the
# picture belongs to can be read straight off it.
_IMAGE_OWNERS = (
    (re.compile(r"^https?://(?:www\.)?zerochan\.net/(\d+)(?:[/?#]|$)"),
     re.compile(r"^https?://(?:s\d+|static)\.zerochan\.net/[^?#]*\.(\d+)\.(?:jpe?g|png|gif|webp)"
                r"(?:[?#]|$)", re.IGNORECASE),
     "https://www.zerochan.net/{}"),
)


def owner_of_image(page_url: str, image_url: Optional[str]) -> str:
    """The post `image_url` belongs to, when it is on `page_url`'s site but
    names a different post; otherwise `page_url` unchanged."""
    if not image_url:
        return page_url
    for page_re, image_re, template in _IMAGE_OWNERS:
        page = page_re.match(page_url)
        image = image_re.match(image_url)
        if page and image and page.group(1) != image.group(1):
            log.debug("Lens paired %s with %s - using the image's own post", image_url, page_url)
            return template.format(image.group(1))
    return page_url


def parse_lens_payload(payload: str) -> List[GoogleLensMatch]:
    """Turn a Lens results response body into matches. Public for tests."""
    literals = [_unescape(m.group(1)) for m in _LITERAL_RE.finditer(payload or "")]
    matches: List[GoogleLensMatch] = []
    seen = set()
    previous_url: Optional[str] = None

    for index, literal in enumerate(literals):
        if not literal.startswith(("http://", "https://")):
            continue
        host = host_label(literal) or ""
        if not host or is_google_host(host):
            continue
        url = literal.split("#", 1)[0]
        following = literals[index + 1].strip() if index + 1 < len(literals) else ""

        if not _looks_like_a_title(following):
            # No title of its own: this is the picture belonging to the
            # result that comes next.
            previous_url = url
            continue
        url = owner_of_image(url, previous_url)
        if url in seen:
            previous_url = None
            continue
        seen.add(url)
        matches.append(GoogleLensMatch(
            url=url,
            thumb_url=previous_url,
            similarity=max(SIMILARITY_START - len(matches) * SIMILARITY_STEP, SIMILARITY_FLOOR),
            title=following,
            source_name=host,
        ))
        previous_url = None
    # Every result, not the first MAX_RESULTS. Capping here threw away a
    # booru post sitting below Google's first eight before search() ever
    # saw it - and search() is where the tag-giving sites are moved up
    # and the list is cut, so it has to be handed the whole list.
    return matches


def _looks_like_a_title(text: str) -> bool:
    """A page title, as opposed to a URL or one of Google's own tokens.

    Google's image ids ("XORNM3qJQeMrvM") sit where a title would and
    have no spaces, so a single wordy run is not accepted as one.
    """
    if not (3 <= len(text) <= 200):
        return False
    if text.startswith(("http://", "https://", "/", "data:")):
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    return " " in text


def _unescape(literal: str) -> str:
    """JSON-escaped slashes and unicode escapes back to plain text.

    Done by hand rather than with json.loads: these literals are pulled
    out of a much larger body one at a time, and a single one carrying
    an escape json cannot parse must not throw away the whole page.
    """
    text = literal.replace("\\/", "/").replace('\\"', '"')

    def _replace(match):
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return match.group(0)

    return re.sub(r"\\u([0-9a-fA-F]{4})", _replace, text)
