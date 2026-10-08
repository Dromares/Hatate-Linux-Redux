"""Reddit posts, via each post's public RSS feed.

Reddit's JSON API answers 403 to an anonymous client, and old.reddit's
HTML does not carry the image. CONFIRMED: a post's own feed -
reddit.com/comments/{id}/.rss - answers anonymously with everything a
match needs: the post's first entry names the ORIGINAL (i.redd.it) as
its "[link]", a preview.redd.it thumbnail, the subreddit, the title and
the poster. A post that no longer exists answers 404, which
fetch_page_info already reads as gone.

Reddit rate-limits this hard - one request left the allowance at zero
for the next 20-odd seconds - so core/net.py waits out the reset it
announces rather than spending the next request on a 429.

What a post gives, and what it doesn't:

  * The picture: the original from i.redd.it (an imgur direct link is
    taken too). A GALLERY links to reddit.com/gallery/{id}, not a file;
    its feed shows only the first image, whose i.redd.it original has
    the same id as the thumbnail - so a gallery match is its first image.
  * Dimensions: none in the feed; read from the first bytes of the
    original, which i.redd.it serves by Range (see _remote_size.py).
  * Tags: `subreddit:` and `title:`. The poster is NOT tagged as the
    creator - on Reddit it is as often a reposter as the artist.
"""
from __future__ import annotations

import html
import re
from typing import List, Optional

from ..applog import get_logger
from ..models import Tag, TagSource
from ..reddit import USER_AGENT as REDDIT_USER_AGENT
from . import _remote_size
from ._host import on_host

log = get_logger("boorus.reddit")

HOSTS = ("reddit.com", "redd.it")

# A post: /r/{sub}/comments/{id}/..., /comments/{id}, or the redd.it/{id}
# short link. Not a subreddit listing, a user page or a gallery page.
POST_ID_RE = re.compile(r"(?:/comments/|^https?://(?:www\.)?redd\.it/)([a-z0-9]{3,12})(?:[/?#]|$)",
                        re.IGNORECASE)
_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_CATEGORY_RE = re.compile(r'<category term="([^"]+)"')
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_CONTENT_RE = re.compile(r'<content type="html">(.*?)</content>', re.S)
_THUMB_RE = re.compile(r'<media:thumbnail url="([^"]+)"')
_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')
_DIRECT_IMAGE_RE = re.compile(
    r"^https?://(?:i\.redd\.it|i\.imgur\.com)/[A-Za-z0-9_-]+\.(?:jpe?g|png|gif|webp)$",
    re.IGNORECASE)
_PREVIEW_ID_RE = re.compile(r"^https?://preview\.redd\.it/([A-Za-z0-9]+)\.([A-Za-z0-9]+)")


def matches(url: str) -> bool:
    return on_host(url, HOSTS) and post_id(url) is not None


def post_id(url: str) -> Optional[str]:
    found = POST_ID_RE.search((url or "").strip())
    return found.group(1).lower() if found else None


def resolve_fetch_url(url: str) -> str:
    pid = post_id(url)
    return f"https://www.reddit.com/comments/{pid}/.rss" if pid else url


def parse(body: str, url: str) -> List[Tag]:
    entry = _post_entry(body)
    if entry is None:
        return []
    tags: List[Tag] = []
    subreddit = _first(_CATEGORY_RE, entry)
    if subreddit:
        tags.append(Tag(name=subreddit, source=TagSource.BOORU, namespace="subreddit"))
    title = _first(_TITLE_RE, entry)
    if title:
        tags.append(Tag(name=html.unescape(title).strip(), source=TagSource.BOORU,
                        namespace="title"))
    return tags


def parse_file_url(body: str, url: str) -> Optional[str]:
    entry = _post_entry(body)
    if entry is None:
        return None
    content = html.unescape(_first(_CONTENT_RE, entry) or "")
    link = _first(_LINK_RE, content)
    if link and _DIRECT_IMAGE_RE.match(link):
        return link
    # A gallery, or a link the feed only shows as a thumbnail: the preview
    # is of the (first) image, and its original has the same id.
    preview = _preview(entry)
    found = _PREVIEW_ID_RE.match(preview or "")
    if found:
        return f"https://i.redd.it/{found.group(1)}.{found.group(2)}"
    return None


def parse_preview_url(body: str, url: str) -> Optional[str]:
    entry = _post_entry(body)
    return _preview(entry) if entry is not None else None


def parse_dimensions(body: str, url: str):
    file_url = parse_file_url(body, url)
    if not file_url or not on_host(file_url, ("i.redd.it",)):
        return None, None
    return _remote_size.image_size(file_url, REDDIT_USER_AGENT)


def incomplete_reason(body: str, url: str) -> Optional[str]:
    if _post_entry(body) is None:
        return "Reddit's feed for this post was empty"
    if parse_file_url(body, url) is None:
        return "the post links to something other than a picture Reddit hosts"
    return None


# -- internals --------------------------------------------------------
def _post_entry(body: str) -> Optional[str]:
    """The post's own entry - the first; the rest are comments."""
    found = _ENTRY_RE.search(body or "")
    if not found:
        return None
    entry = found.group(1)
    # The first entry is always the post (id t3_...), but be sure.
    return entry if "<id>t3_" in entry else None


def _preview(entry: str) -> Optional[str]:
    thumb = _first(_THUMB_RE, entry)
    return html.unescape(thumb) if thumb else None


def _first(pattern: "re.Pattern", text: str) -> Optional[str]:
    found = pattern.search(text or "")
    return found.group(1) if found else None

