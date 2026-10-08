"""A local index of chosen pawchive artists, for finding EDITED copies.

The exact lookup (core/pawchive_lookup.py) asks pawchive about a file's
SHA-256, which only finds byte-identical copies. A resized or re-saved
copy has a different hash - and pawchive offers no search by picture.
So this keeps a picture index of its own, for the artists the user picks:
for every image, its post, artist and SHA-256 (read straight off its
path, which IS the hash), and two fingerprints of its thumbnail.

Indexing an artist costs a listing request per 50 posts and a ~33 KB
thumbnail per image, at one request a second - MEASURED on pawchive's
most-favourited artist: 83 posts, 115 images, about two minutes. A
refresh only fetches what is new. The whole site is out of reach on
purpose: its site-wide listing stops before 100,000 posts, so a full
crawl would walk all ~95,000 artists one by one.

Searching is local and costs no request: the fingerprints are held in
memory and compared against the local file's.

Two fingerprints, because each answers a different question:

  * the 256-bit (16x16) difference hash RANKS and SCORES. The 64-bit one
    cannot tell an image's variants apart: MEASURED on a real rule34.xxx
    family, the parent and two edited siblings all hashed to distance 0
    at 8x8, while at 16x16 the true match was 2 and the nearest variant 9.
  * the 64-bit one is stored too, since it is the hash the rest of the
    app measures with, at no extra download.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

import requests
from PIL import Image

from .applog import get_logger
from .image_compare import dhash
from .paths import CONFIG_DIR
from .pawchive_lookup import POST_URL, THUMBNAIL_URL, USER_AGENT, files_of

log = get_logger("pawchive_index")

INDEX_DB = CONFIG_DIR / "pawchive_index.db"
CREATORS_CACHE = CONFIG_DIR / "pawchive_creators.json"
API = "https://pawchive.pw/api/v1"

PAGE_SIZE = 50                      # posts per listing page - the API's own size
REQUEST_DELAY = 1.0                 # seconds between requests: an archive, not a CDN
MAX_RETRIES = 3
RETRY_WAIT = 30.0                   # after a 429 that names no Retry-After
CREATORS_CACHE_MAX_AGE = 7 * 24 * 3600
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".jfif")

# Distance on the 256-bit hash -> a percentage that reads like the rest of
# the app, where 90+ means "the same picture". MEASURED on real pawchive
# posts and real library files:
#   same picture, re-saved losslessly or resized to 50%       2  (96.7%)
#   resized to 30% and saved as a low-quality JPEG            5  (91.7%)
#   edited variants of one picture (a set's 004/006/007/008)  11-14 (81-84%)
#   cropped 3% on each side                                   28
#   the nearest DIFFERENT picture, in the same set            41
#   nearest other image among one artist's 1,951              96
#   unrelated pictures, typically                             ~128
# The 90% line at 6 keeps variants below it, and so below auto-import.
SIMILARITY_ANCHORS = ((0, 100.0), (6, 90.0), (24, 70.0), (128, 0.0))
# Past this it is a different picture. 32 rather than 24 so that a light
# crop still surfaces - at about 65%, a candidate to review that can
# neither satisfy a fallback threshold nor be auto-imported - while
# leaving a clear margin below the nearest different picture measured (41).
MAX_MATCH_DISTANCE = 32
MAX_RESULTS = 5

_SHA256_IN_PATH_RE = re.compile(r"/([0-9a-f]{64})\.[A-Za-z0-9]+$")
_CREATOR_URL_RE = re.compile(r"pawchive\.pw/([A-Za-z0-9_.-]+)/user/([^/?#\s]+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    service     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    name        TEXT,
    added_at    REAL,
    indexed_at  REAL,
    post_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, user_id)
);
CREATE TABLE IF NOT EXISTS images (
    service     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    post_id     TEXT NOT NULL,
    page_index  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL,
    title       TEXT,
    dhash8      TEXT,               -- hex; NULL when pawchive had no thumbnail
    dhash16     TEXT,
    PRIMARY KEY (service, user_id, post_id, page_index)
);
CREATE INDEX IF NOT EXISTS images_by_sha256 ON images (sha256);
"""


@dataclass
class Creator:
    service: str
    user_id: str
    name: Optional[str]
    indexed_at: Optional[float]
    post_count: int
    image_count: int
    unfingerprinted: int            # images pawchive had no thumbnail for

    @property
    def label(self) -> str:
        return f"{self.name or self.user_id} ({self.service})"


@dataclass
class IndexMatch:
    url: str
    thumb_url: str
    page_index: int
    distance: int                   # on the 256-bit hash; 0 for an exact copy
    similarity: float
    exact: bool                     # same SHA-256: the very same file
    artist: Optional[str]
    title: Optional[str]


@dataclass
class CrawlResult:
    service: str
    user_id: str
    name: Optional[str] = None
    posts: int = 0
    images: int = 0
    new: int = 0
    unfingerprinted: int = 0
    stopped: bool = False
    error: Optional[str] = None


def similarity_for(distance: int) -> float:
    if distance <= 0:
        return 100.0
    for (low_d, low_pct), (high_d, high_pct) in zip(SIMILARITY_ANCHORS, SIMILARITY_ANCHORS[1:], strict=False):
        if distance <= high_d:
            return round(low_pct + (high_pct - low_pct) * (distance - low_d) / (high_d - low_d), 1)
    return 0.0


def fingerprints(source) -> Tuple[Optional[int], Optional[int]]:
    """(64-bit, 256-bit) difference hashes of a path, bytes or image,
    decoding it once. dhash closes what it is given, so each gets a copy."""
    try:
        if isinstance(source, bytes):
            image = Image.open(io.BytesIO(source))
        elif isinstance(source, Image.Image):
            image = source  # type: ignore[assignment]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
        else:
            image = Image.open(source)
        with image:
            gray = image.convert("L")
    except (OSError, ValueError) as exc:
        log.debug("Could not read an image to fingerprint: %s", exc)
        return None, None
    return dhash(gray.copy(), 8), dhash(gray, 16)


def creator_from_url(text: str) -> Optional[Tuple[str, str]]:
    """(service, user id) from any pawchive artist or post link."""
    match = _CREATOR_URL_RE.search(text or "")
    return (match.group(1), match.group(2)) if match else None


# ----------------------------------------------------------------------
# The database
# ----------------------------------------------------------------------
_generation = 0                     # bumped on every write, to invalidate the cache
_generation_lock = threading.Lock()


def _bump():
    global _generation
    with _generation_lock:
        _generation += 1


class PawchiveIndex:
    def __init__(self, path=None):
        self.path = Path(path or INDEX_DB)
        self._cache_lock = threading.Lock()
        self._cache_key = None
        self._rows: list = []

    @contextmanager
    def _connect(self):
        """A connection that commits on success and is always closed -
        sqlite3's own context manager commits but leaves it open."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- artists --------------------------------------------------------
    def creators(self) -> List[Creator]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT c.service, c.user_id, c.name, c.indexed_at, c.post_count,
                       COUNT(i.sha256), SUM(CASE WHEN i.sha256 IS NOT NULL AND i.dhash16 IS NULL
                                            THEN 1 ELSE 0 END)
                FROM creators c LEFT JOIN images i
                  ON i.service = c.service AND i.user_id = c.user_id
                GROUP BY c.service, c.user_id
                ORDER BY LOWER(COALESCE(c.name, c.user_id))
            """).fetchall()
        return [Creator(r[0], r[1], r[2], r[3], r[4] or 0, r[5] or 0, r[6] or 0) for r in rows]

    def add_creator(self, service: str, user_id: str, name: Optional[str] = None) -> bool:
        """Adds an artist to index. False if it was already there."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO creators (service, user_id, name, added_at) VALUES (?,?,?,?)",
                (service, str(user_id), name, time.time()))
            if name:
                conn.execute("UPDATE creators SET name = ? WHERE service = ? AND user_id = ?",
                             (name, service, str(user_id)))
            added = cur.rowcount > 0
        _bump()
        return added

    def remove_creator(self, service: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM images WHERE service = ? AND user_id = ?", (service, str(user_id)))
            conn.execute("DELETE FROM creators WHERE service = ? AND user_id = ?", (service, str(user_id)))
        _bump()

    def known_pages(self, service: str, user_id: str) -> set:
        with self._connect() as conn:
            return {(r[0], r[1]) for r in conn.execute(
                "SELECT post_id, page_index FROM images WHERE service = ? AND user_id = ?",
                (service, str(user_id)))}

    def add_images(self, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO images
                    (service, user_id, post_id, page_index, sha256, path, title, dhash8, dhash16)
                VALUES (?,?,?,?,?,?,?,?,?)""", rows)
        _bump()

    def finish_creator(self, service: str, user_id: str, name: Optional[str], posts: int) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE creators SET name = COALESCE(?, name), indexed_at = ?, post_count = ?
                WHERE service = ? AND user_id = ?""",
                (name, time.time(), posts, service, str(user_id)))
        _bump()

    def image_count(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM images WHERE dhash16 IS NOT NULL").fetchone()[0]

    # -- searching ------------------------------------------------------
    def _fingerprinted_rows(self) -> list:
        """Every fingerprinted image, held in memory between writes."""
        if not self.path.exists():
            return []
        with self._cache_lock:
            if self._cache_key != _generation:
                key = _generation
                with self._connect() as conn:
                    self._rows = [
                        (int(r[0], 16), r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                        for r in conn.execute("""
                            SELECT i.dhash16, i.service, i.user_id, i.post_id, i.page_index,
                                   i.path, i.title, c.name
                            FROM images i LEFT JOIN creators c
                              ON c.service = i.service AND c.user_id = i.user_id
                            WHERE i.dhash16 IS NOT NULL""")
                    ]
                self._cache_key = key
            return self._rows

    def is_empty(self) -> bool:
        return not self._fingerprinted_rows()

    def search(self, local_hash16: Optional[int], sha256: Optional[str] = None,
               limit: int = MAX_RESULTS,
               max_distance: int = MAX_MATCH_DISTANCE) -> List[IndexMatch]:
        """The indexed images most like the local one, best first, one per
        post. A row with the same SHA-256 is the very same file."""
        best: dict[tuple, tuple] = {}                # post key -> (distance, row)
        if local_hash16 is not None:
            for row in self._fingerprinted_rows():
                distance = (local_hash16 ^ row[0]).bit_count()
                if distance > max_distance:
                    continue
                key = (row[1], row[2], row[3])
                if key not in best or distance < best[key][0]:
                    best[key] = (distance, row)

        exact_keys = set()
        if sha256 and self.path.exists():
            with self._connect() as conn:
                for r in conn.execute("""
                        SELECT i.dhash16, i.service, i.user_id, i.post_id, i.page_index,
                               i.path, i.title, c.name
                        FROM images i LEFT JOIN creators c
                          ON c.service = i.service AND c.user_id = i.user_id
                        WHERE i.sha256 = ?""", (sha256.lower(),)):
                    key = (r[1], r[2], r[3])
                    best[key] = (0, (int(r[0], 16) if r[0] else 0, *r[1:]))
                    exact_keys.add(key)

        ranked = sorted(best.items(), key=lambda item: (item[1][0], item[0] not in exact_keys))
        matches = []
        for key, (distance, row) in ranked[:limit]:
            _, service, user_id, post_id, page_index, path, title, artist = row
            matches.append(IndexMatch(
                url=POST_URL.format(service=service, user=user_id, post=post_id),
                thumb_url=THUMBNAIL_URL.format(path=path),
                page_index=page_index, distance=distance,
                similarity=similarity_for(distance), exact=key in exact_keys,
                artist=artist, title=title,
            ))
        return matches


# ----------------------------------------------------------------------
# Finding artists
# ----------------------------------------------------------------------
def find_creators(query: str, *, refresh: bool = False, cache_path=None,
                  session=None, limit: int = 50) -> List[dict]:
    """Artists whose name contains `query`, most-favourited first.

    Needs pawchive's full creator list - ~15 MB, ~95,000 artists - which is
    downloaded once and kept for a week rather than fetched per search.
    """
    cache = Path(cache_path or CREATORS_CACHE)
    creators = None
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < CREATORS_CACHE_MAX_AGE:
        try:
            creators = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            creators = None
    if creators is None:
        session = session or requests.Session()
        resp = session.get(f"{API}/creators", headers={"User-Agent": USER_AGENT}, timeout=120)
        resp.raise_for_status()
        creators = resp.json()
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(creators), encoding="utf-8")
        except OSError as exc:
            log.debug("Could not cache pawchive's creator list: %s", exc)

    needle = (query or "").strip().lower()
    if not needle:
        return []
    found = [c for c in creators if isinstance(c, dict)
             and needle in (str(c.get("name") or "") + " " + str(c.get("public_id") or "")).lower()]
    found.sort(key=lambda c: (-(c.get("favorited") or 0), str(c.get("name") or "").lower()))
    return found[:limit]


# ----------------------------------------------------------------------
# Indexing
# ----------------------------------------------------------------------
class _Fetcher:
    """One polite HTTP client: a gap between requests, and backing off
    when told to."""

    def __init__(self, session, delay: float, should_stop: Callable[[], bool]):
        self.session = session
        self.delay = delay
        self.should_stop = should_stop
        self._last = 0.0

    def _pause(self, seconds: float) -> bool:
        """Sleeps in small steps so Stop is honoured quickly. False if stopped."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.should_stop():
                return False
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))
        return not self.should_stop()

    def get(self, url: str, params=None, timeout: float = 30.0):
        for attempt in range(MAX_RETRIES + 1):
            if not self._pause(self._last + self.delay - time.monotonic()):
                return None
            try:
                resp = self.session.get(url, params=params, timeout=timeout,
                                        headers={"User-Agent": USER_AGENT})
            except requests.RequestException as exc:
                self._last = time.monotonic()
                log.debug("pawchive request failed (%s): %s", url, exc)
                if attempt == MAX_RETRIES:
                    raise
                continue
            self._last = time.monotonic()
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                try:
                    wait = float(resp.headers.get("Retry-After") or RETRY_WAIT)
                except ValueError:
                    wait = RETRY_WAIT
                log.info("pawchive is rate limiting - waiting %.0fs", wait)
                if not self._pause(wait):
                    return None
                continue
            return resp
        return None


def index_creator(index: PawchiveIndex, service: str, user_id: str, *,
                  session=None, delay: float = REQUEST_DELAY,
                  should_stop: Optional[Callable[[], bool]] = None,
                  on_progress: Optional[Callable[[str, int, int], None]] = None) -> CrawlResult:
    """Indexes (or refreshes) one artist. Safe to stop at any point: what
    was fingerprinted is kept, and the next run carries on from there."""
    should_stop = should_stop or (lambda: False)
    progress = on_progress or (lambda message, done, total: None)
    fetch = _Fetcher(session or requests.Session(), delay, should_stop)
    result = CrawlResult(service=service, user_id=str(user_id))

    try:
        profile = fetch.get(f"{API}/{service}/user/{user_id}/profile")
        if profile is None:
            result.stopped = True
            return result
        if profile.status_code == 200:
            result.name = (profile.json() or {}).get("name")
        elif profile.status_code == 404:
            result.error = "pawchive has no such artist"
            return result
        index.add_creator(service, str(user_id), result.name)

        posts: list[dict] = []
        offset = 0
        while True:
            progress(f"Listing {result.name or user_id}'s posts", len(posts), 0)
            resp = fetch.get(f"{API}/{service}/user/{user_id}/posts", params={"o": offset})
            if resp is None:
                result.stopped = True
                return result
            if resp.status_code != 200:
                result.error = f"listing posts failed: HTTP {resp.status_code}"
                return result
            batch = resp.json() or []
            posts.extend(p for p in batch if isinstance(p, dict))
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        result.posts = len(posts)

        known = index.known_pages(service, str(user_id))
        todo = []
        for post in posts:
            post_id = str(post.get("id") or "")
            for page_index, file in enumerate(files_of(post)):
                path = file["path"]
                if not path.lower().endswith(IMAGE_EXTENSIONS):
                    continue
                result.images += 1
                if (post_id, page_index) not in known:
                    todo.append((post_id, page_index, path, post.get("title")))

        rows = []
        try:
            for done, (post_id, page_index, path, title) in enumerate(todo):
                progress(f"Fingerprinting {result.name or user_id}", done, len(todo))
                resp = fetch.get(THUMBNAIL_URL.format(path=path))
                if resp is None:
                    result.stopped = True
                    break
                h8 = h16 = None
                if resp.status_code == 200:
                    h8, h16 = fingerprints(resp.content)
                if h16 is None:
                    result.unfingerprinted += 1
                sha = _SHA256_IN_PATH_RE.search(path)
                rows.append((service, str(user_id), post_id, page_index,
                             sha.group(1) if sha else "", path, title,
                             f"{h8:016x}" if h8 is not None else None,
                             f"{h16:064x}" if h16 is not None else None))
                result.new += 1
                if len(rows) >= 25:
                    index.add_images(rows)
                    rows = []
        finally:
            # Whatever was fingerprinted is kept however this ends - a
            # network failure halfway through an artist costs nothing done.
            index.add_images(rows)
        if not result.stopped:
            index.finish_creator(service, str(user_id), result.name, result.posts)
            progress(f"Indexed {result.name or user_id}", len(todo), len(todo))
    except (requests.RequestException, ValueError) as exc:
        result.error = str(exc)
    log.info("Pawchive index: %s/%s (%s) - %d posts, %d images, %d new, %d without a "
             "thumbnail%s%s", service, user_id, result.name, result.posts, result.images,
             result.new, result.unfingerprinted, " - stopped" if result.stopped else "",
             f" - {result.error}" if result.error else "")
    return result
