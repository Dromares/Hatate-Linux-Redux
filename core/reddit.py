"""Turning a subreddit link into the one post a match came from.

Google Lens sometimes answers with a SUBREDDIT - reddit.com/r/Name/ -
rather than the post the picture is in. Handed to Hydrus, that is a
subreddit gallery URL, and Hydrus downloads the whole subreddit.
CONFIRMED on a real image: Lens gave r/YanchaGalAnjouSan/ for a manga
cover, and importing it started downloading everything posted there.

What Lens does give alongside it is the picture's own reddit file,
i.redd.it/<id>.png, as the result's thumbnail. Every image post embeds
its file, so the post is the one in that subreddit whose entry names
that id. Reddit's JSON API answers 403 without an account, but each
subreddit's public RSS feed still works and carries the image ids.

When the post cannot be found - it is older than the feed reaches, or
reddit is rate limiting - the match becomes the direct image link
instead. That still gets the picture, and never the whole subreddit.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import requests

from . import net
from .applog import get_logger

log = get_logger("reddit")

USER_AGENT = "linux:hatate-linux:1.0 (reverse image tagger)"

SUBREDDIT_LISTING_RE = re.compile(
    r"^https?://(?:www\.|old\.|new\.)?reddit\.com/r/([A-Za-z0-9_]+)"
    r"/?(?:(?:hot|new|top|rising|controversial)/?)?(?:[?#].*)?$",
    re.IGNORECASE,
)
MEDIA_RE = re.compile(r"^https?://(?:i|preview)\.redd\.it/([A-Za-z0-9]+)\.([A-Za-z0-9]+)")
_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_LINK_RE = re.compile(r'<link href="([^"]+/comments/[^"]+)"')

# Newest first, then the all-time top: between them they cover what a
# subreddit's visitors - and so Google - are most likely to have seen.
# Each is a single request, cached per subreddit for the session.
FEEDS = ("new/.rss?limit=100", "top/.rss?t=all&limit=100")

_feed_cache: Dict[str, Optional[List[str]]] = {}


def subreddit_of(url: str) -> Optional[str]:
    """The subreddit a listing URL shows, or None if it is not one - a
    post, a user page and every other site are all None."""
    match = SUBREDDIT_LISTING_RE.match((url or "").strip())
    return match.group(1) if match else None


def media_of(url: Optional[str]) -> Optional[tuple]:
    """(id, extension) of a reddit-hosted image URL, or None."""
    match = MEDIA_RE.match((url or "").strip())
    return (match.group(1), match.group(2)) if match else None


def _feed_entries(subreddit: str, feed: str, timeout: float) -> Optional[List[str]]:
    key = f"{subreddit.lower()}/{feed}"
    if key in _feed_cache:
        return _feed_cache[key]
    url = f"https://www.reddit.com/r/{subreddit}/{feed}"
    try:
        resp = net.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                       deadline=timeout)
    except requests.RequestException as exc:
        log.debug("Reddit feed %s failed: %s", url, exc)
        return None                       # not cached - a network fault may clear
    if resp.status_code != 200:
        log.debug("Reddit feed %s returned HTTP %d", url, resp.status_code)
        return None                       # 429s clear too; don't remember them
    entries = _ENTRY_RE.findall(resp.text)
    _feed_cache[key] = entries
    return entries


def find_post(subreddit: str, media_id: str, timeout: float = 15.0) -> Optional[str]:
    """The URL of the post in `subreddit` whose image is `media_id`."""
    for feed in FEEDS:
        for entry in _feed_entries(subreddit, feed, timeout) or []:
            if media_id not in entry:
                continue
            link = _LINK_RE.search(entry)
            if link:
                return link.group(1)
    return None


def resolve_listing(url: str, image_url: Optional[str],
                    timeout: float = 15.0) -> Optional[str]:
    """What to use instead of a subreddit listing URL.

    The post that holds the image if it can be found, else the image's
    own i.redd.it link, else None - meaning there is nothing safe to
    offer, since the listing itself would import the whole subreddit.
    Returns `url` unchanged when it is not a subreddit listing.
    """
    subreddit = subreddit_of(url)
    if subreddit is None:
        return url
    media = media_of(image_url)
    if media is None:
        log.info("Dropping %s: a subreddit, with no image to find the post by", url)
        return None
    media_id, ext = media
    post = find_post(subreddit, media_id, timeout)
    if post:
        log.info("Resolved %s to the post holding its image: %s", url, post)
        return post
    direct = f"https://i.redd.it/{media_id}.{ext}"
    log.info("Could not find the post in r/%s holding %s - using the image link "
             "itself rather than the whole subreddit", subreddit, direct)
    return direct
