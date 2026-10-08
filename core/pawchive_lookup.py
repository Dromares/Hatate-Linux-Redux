"""Pawchive exact-file lookup, by SHA-256.

Pawchive (pawchive.pw, a Kemono fork archiving Patreon and Fanbox) is
indexed by neither SauceNAO nor IQDB, so its posts never came up at all.
It needs no index of our own for exact copies, though, because it keeps
one itself. CONFIRMED against the live site:

  * every file is stored under its own SHA-256 - the path of
    /3f/ac/3fac8975…bbefb.jpeg IS the hash of that file's bytes (checked
    by downloading two and hashing them);
  * /api/v1/search_hash/<sha256> answers, anonymously, with every post
    that contains the file, and 404s for a hash it has never seen.

This app already knows each file's SHA-256 (Hydrus names files by it,
and the hash worker verifies or computes it), so the lookup is one small
request per image, with no upload. A sample of 200 files from a real
library found 8 on pawchive this way - images SauceNAO and IQDB had
never placed.

The limit is in the name: it finds byte-identical copies only. A resized
or re-saved copy has a different hash and is not found.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional

import requests

from . import net
from .applog import get_logger

log = get_logger("pawchive_lookup")

API_URL = "https://pawchive.pw/api/v1/search_hash/{}"
POST_URL = "https://pawchive.pw/{service}/user/{user}/post/{post}"
THUMBNAIL_URL = "https://img.pawchive.pw/thumbnail/data{path}"
USER_AGENT = net.USER_AGENT

# The same file can sit in several posts (a creator reposting to both
# Patreon and Fanbox, a set and its variants). A few is useful; dozens
# would only bury the rest of the results.
MAX_POSTS = 5

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PawchiveLookupError(Exception):
    pass


@dataclass
class PawchiveMatch:
    url: str
    title: Optional[str]
    thumb_url: Optional[str]
    page_index: int            # which image of the post is this file, 0-based
    page_count: int


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_of(post: dict) -> List[dict]:
    """A post's files in the site's own order, "file" first. "file" is
    normally attachments[0] repeated, so paths are de-duplicated."""
    seen, files = set(), []
    for entry in [post.get("file")] + list(post.get("attachments") or []):
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str) and path.startswith("/") and path not in seen:
                seen.add(path)
                files.append(entry)
    return files


def parse_response(body: dict, sha256: str) -> List[PawchiveMatch]:
    """The posts in a search_hash answer, each pointing at the file."""
    matches: List[PawchiveMatch] = []
    for post in (body or {}).get("posts") or []:
        if not isinstance(post, dict):
            continue
        service, user, post_id = post.get("service"), post.get("user"), post.get("id")
        if not (service and user and post_id):
            continue
        files = files_of(post)
        index = next((i for i, f in enumerate(files) if sha256 in f["path"]), 0)
        path = files[index]["path"] if files else None
        matches.append(PawchiveMatch(
            url=POST_URL.format(service=service, user=user, post=post_id),
            title=post.get("title") or None,
            thumb_url=THUMBNAIL_URL.format(path=path) if path else None,
            page_index=index,
            page_count=max(len(files), 1),
        ))
        if len(matches) >= MAX_POSTS:
            break
    return matches


def lookup(sha256: str, timeout: float = 15.0) -> List[PawchiveMatch]:
    """Every pawchive post holding exactly this file; [] if none does."""
    sha256 = (sha256 or "").strip().lower()
    if not SHA256_RE.match(sha256):
        raise PawchiveLookupError(f"not a SHA-256: {sha256[:16]!r}")
    try:
        resp = net.get(API_URL.format(sha256), timeout=timeout, deadline=timeout)
    except requests.RequestException as exc:
        raise PawchiveLookupError(f"could not reach pawchive.pw: {exc}") from exc
    if resp.status_code == 404:
        return []                              # the site's answer for "never seen it"
    if resp.status_code == 429:
        raise PawchiveLookupError("pawchive.pw is rate limiting - slow the search delay down")
    if resp.status_code != 200:
        raise PawchiveLookupError(f"pawchive.pw answered HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise PawchiveLookupError("pawchive.pw sent something that isn't JSON") from exc
    return parse_response(body, sha256)
