"""trace.moe anime-scene search (https://trace.moe).

Identifies which anime a screenshot is from, which episode, and the
timestamp. Complements IQDB/SauceNAO, which are weak on frames taken
from video.

Official JSON API: POST https://api.trace.moe/search
Docs: https://soruly.github.io/trace.moe-api/

Anonymous use is rate-limited (a per-minute cap plus a monthly quota),
so this engine is off by default - a Hydrus tagging batch would burn
the monthly allowance on images that are not screenshots. When it does
run, results below `min_similarity` (default 85%) are dropped; false
positives are common under that.

A match's URL is the AniList page, not a booru post. There is no
full-resolution source image to download - the value is the series tag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import requests

from .applog import get_logger
from . import net
from .hard_timeout import HardTimeoutError
from .image_prep import prepare_upload_bytes
from .models import Tag, TagSource
from .progress_ticker import OnTick, ProgressTicker

log = get_logger("tracemoe")

API_URL = "https://api.trace.moe/search"
USER_AGENT = net.USER_AGENT

DEFAULT_MIN_SIMILARITY = 85.0


@dataclass
class TraceMoeMatch:
    url: str
    similarity: float
    thumb_url: Optional[str] = None
    title: Optional[str] = None
    episode: Optional[str] = None
    tags: List[Tag] = field(default_factory=list)


class TraceMoeError(Exception):
    pass


def search(
    image_path: str,
    timeout: float = 30.0,
    on_tick: Optional[OnTick] = None,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> List[TraceMoeMatch]:
    """Upload an image to trace.moe and return parsed matches."""
    log.debug("Uploading %s to trace.moe", image_path)
    try:
        data, filename = prepare_upload_bytes(image_path)
        files = {"image": (filename, data, "application/octet-stream")}
        with ProgressTicker("Waiting on trace.moe", timeout, on_tick):
            resp = net.post(API_URL, params={"cutBorders": "1", "anilistInfo": "1"},
                            files=files, timeout=timeout, deadline=timeout)
    except OSError as exc:
        log.error("Could not read %s for trace.moe upload: %s", image_path, exc)
        raise TraceMoeError(f"Could not read image: {exc}") from exc
    except HardTimeoutError as exc:
        log.error("trace.moe request for %s exceeded the hard deadline: %s", image_path, exc)
        raise TraceMoeError(f"trace.moe request timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("trace.moe request failed for %s: %s", image_path, exc)
        raise TraceMoeError(f"trace.moe request failed: {exc}") from exc

    if resp.status_code == 429:
        raise TraceMoeError("trace.moe rate limit reached (HTTP 429)")
    if resp.status_code == 402:
        raise TraceMoeError("trace.moe monthly quota exhausted (HTTP 402)")
    if resp.status_code != 200:
        raise TraceMoeError(f"trace.moe returned HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TraceMoeError("trace.moe returned non-JSON") from exc

    error = payload.get("error") or ""
    if error:
        raise TraceMoeError(f"trace.moe: {error}")

    matches = parse_results(payload, min_similarity=min_similarity)
    log.debug("trace.moe parsed into %d match(es) at or above %.0f%%", len(matches), min_similarity)
    return matches


def parse_results(payload: dict, min_similarity: float = DEFAULT_MIN_SIMILARITY) -> List[TraceMoeMatch]:
    """Turn a trace.moe JSON body into matches. Public for tests."""
    matches: List[TraceMoeMatch] = []
    seen_ids = set()
    for raw in payload.get("result") or []:
        if not isinstance(raw, dict):
            continue
        similarity = _similarity_percent(raw.get("similarity"))
        if similarity is None or similarity < min_similarity:
            continue
        anilist = raw.get("anilist")
        anilist_id, title, tags = _anilist_fields(anilist)
        if anilist_id is None:
            continue
        if anilist_id in seen_ids:
            continue
        seen_ids.add(anilist_id)

        episode = _episode_label(raw.get("episode"))
        if episode:
            tags.append(Tag(name=f"episode {episode}", source=TagSource.SEARCH_ENGINE, namespace="meta"))

        matches.append(TraceMoeMatch(
            url=f"https://anilist.co/anime/{anilist_id}",
            similarity=similarity,
            thumb_url=raw.get("image") or None,
            title=title,
            episode=episode,
            tags=tags,
        ))
    matches.sort(key=lambda m: -m.similarity)
    return matches


def _similarity_percent(value) -> Optional[float]:
    """trace.moe reports similarity as 0-1. Tolerate a 0-100 value in
    case a proxy or an older response already scaled it."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= number <= 1.0:
        return number * 100.0
    if 1.0 < number <= 100.0:
        return number
    return None


def _anilist_fields(anilist) -> tuple:
    """Returns (id, display_title, tags). anilist is either an int id
    (anilistInfo off) or an object (anilistInfo on)."""
    if isinstance(anilist, int):
        return anilist, None, []
    if not isinstance(anilist, dict):
        return None, None, []
    anilist_id_raw = anilist.get("id")
    try:
        anilist_id = int(anilist_id_raw) if anilist_id_raw is not None else None
    except (TypeError, ValueError):
        return None, None, []
    if anilist_id is None:
        return None, None, []

    titles = anilist.get("title") or {}
    romaji = (titles.get("romaji") or "").strip()
    english = (titles.get("english") or "").strip()
    native = (titles.get("native") or "").strip()
    display = romaji or english or native or None

    tags: List[Tag] = []
    seen = set()
    for name in (romaji, english):
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(Tag(name=name.replace(" ", "_"), source=TagSource.SEARCH_ENGINE, namespace="series"))
    return anilist_id, display, tags


def _episode_label(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return str(value).strip() or None
