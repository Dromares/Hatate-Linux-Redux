"""JoyReactor / reactor.cc, via the post page's own markup.

CONFIRMED against a live post (reactor.cc/post/5351071, captured while
investigating DAN-344): a post's tags sit in one unambiguous element,
`<strong class="taglist">`, as a run of `<a title="Name" ...
data-tag-id="N">Name</a>` entries - the site's user-facing tag chips,
immediately after the poster's byline and before the post body. Nothing
else on the page uses that class. Lower in the page, a "Еще на тему"
("More on this topic") block links to OTHER posts' tags, which is why
tags are read from inside `<strong class="taglist">` specifically rather
than by scanning the page for any `data-tag-id`.

The post's own image sits in the one `<a ... class="prettyPhotoLink">`
anchor - the full-quality, unwatermarked copy one path segment over from
the watermarked one actually embedded (see REACTOR_PICS_POST_RE in
core/sites.py, which rewrites candidate URLs the same way independently
of this parser). A post can carry a "Подробнее" ("More") toggle in a
second `div class="image"`, which is text, not another picture - picked
up correctly here because the file-url pattern keys on the
prettyPhotoLink class rather than that wrapper div.

CONFIRMED against a live GIF/video post (reactor.cc/post/5300000,
investigating DAN-480): a commenter's own attached image is wrapped in
the exact same `<a ... class="prettyPhotoLink">` markup as a post's
image, inside the comment thread that follows the post body. A post
whose own media is a `<video class="video_gif">` has no prettyPhotoLink
of its own, so an unscoped search over the whole page falls through to
a comment's image and returns a stranger's picture as if it were the
post's file. `parse_file_url` therefore stops at `id="comment_list"`
(the comment thread's own container, confirmed to start right after the
post body on every captured page) the same way `parse` already stops at
`</strong>` for tags - a post with no prettyPhotoLink before that point
correctly yields None rather than substituting a comment's image.

reactor.cc, joyreactor.com and joyreactor.cc are the same site behind
different hostnames (core/sites.py's REACTOR_HOSTS); all three serve the
same /post/{id} markup.
"""
from __future__ import annotations

import html
import re
from typing import List, Optional

from ..models import Tag, TagSource
from ._host import on_host

HOSTS = ("reactor.cc", "joyreactor.com", "joyreactor.cc")

POST_ID_RE = re.compile(r"/post/(\d+)(?:[/?#]|$)", re.IGNORECASE)
_TAGLIST_RE = re.compile(r'<strong class="taglist">(.*?)</strong>', re.S)
_TAG_RE = re.compile(r'<a\s+title="([^"]*)"[^>]*data-tag-id="\d+"', re.S)
_FILE_URL_RE = re.compile(r'<a href="([^"]+)"\s+class="prettyPhotoLink"', re.S)
_COMMENT_LIST_MARKER = 'id="comment_list"'


def matches(url: str) -> bool:
    return on_host(url, HOSTS) and post_id(url) is not None


def post_id(url: str) -> Optional[str]:
    found = POST_ID_RE.search((url or "").strip())
    return found.group(1) if found else None


def parse(body: str, url: str) -> List[Tag]:
    taglist = _TAGLIST_RE.search(body or "")
    if not taglist:
        return []
    return [Tag(name=html.unescape(name), source=TagSource.BOORU)
            for name in _TAG_RE.findall(taglist.group(1))]


def parse_file_url(body: str, url: str) -> Optional[str]:
    body = body or ""
    comment_start = body.find(_COMMENT_LIST_MARKER)
    post_html = body if comment_start == -1 else body[:comment_start]
    found = _FILE_URL_RE.search(post_html)
    if not found:
        return None
    file_url = html.unescape(found.group(1))
    if file_url.startswith("//"):
        file_url = "https:" + file_url
    return file_url
