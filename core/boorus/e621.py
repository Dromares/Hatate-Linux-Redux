from __future__ import annotations

import json
import re
from typing import List, Optional

from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of
from ._rating import from_code as rating_from_code

log = get_logger("boorus.e621")

HOSTS = ("e621.net", "e926.net", "e6ai.net")  # e926 = SFW-only mirror, e6ai = AI art

# e621's own field names for each tag category, straight from its public
# JSON API response (a post's "tags" object). "invalid" is intentionally
# excluded - those are tags pending cleanup/removal, not descriptive ones.
CATEGORY_FIELDS = ["general", "species", "character", "copyright", "artist", "lore", "meta"]

POST_ID_RE = re.compile(r"/posts?/(?:show/)?(\d+)")

# e621's three ratings, as its API states them in the post's "rating"
# field. Unlike Danbooru, "s" here means SAFE - e621 never split it into
# general/sensitive - so the two sites' letters must not be shared.
RATING_CODES = {"s": "safe", "q": "questionable", "e": "explicit"}


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def resolve_fetch_url(url: str) -> str:
    """e621 has an official public JSON API - fetch that directly instead
    of scraping HTML. It returns exact, already-categorized tag data plus
    the real file URL in a single response, which is both more reliable
    and simpler than guessing at DOM structure."""
    match = POST_ID_RE.search(url)
    if not match:
        log.debug("Could not find a post ID in %s, falling back to fetching it as-is", url)
        return url
    host = next((h for h in HOSTS if h in url), "e621.net")
    return f"https://{host}/posts/{match.group(1)}.json"


def parse(body: str, url: str) -> List[Tag]:
    post = _load_post(body, url)
    if post is None:
        return []

    tag_groups = post.get("tags") or {}
    if not tag_groups:
        log.debug("No tags object in e621 response for %s (API shape may have changed)", url)
        return []

    tags: List[Tag] = []
    for category in CATEGORY_FIELDS:
        for name in tag_groups.get(category, []) or []:
            tags.append(Tag(name=name, source=TagSource.BOORU, namespace=category))
    return tags


def parse_file_url(body: str, url: str) -> Optional[str]:
    post = _load_post(body, url)
    if post is None:
        return None
    file_info = post.get("file") or {}
    return file_info.get("url")


def parse_preview_url(body: str, url: str) -> Optional[str]:
    """The best picture to show for this post without downloading more
    than needed. Reuses the JSON body already fetched for tags/file_url.

    e621 offers three: a 'sample' (mid-resolution), the 'file' (the
    original), and a 'preview' (a ~150px thumbnail).

    'sample' is the one to want - but it only EXISTS when the original was
    big enough to be worth downscaling. For a smaller post e621 sends
    {"has": false, "url": null}, and returning that null meant the app
    fell back to the search engine's tiny thumbnail and showed a visibly
    blurry match for exactly the posts whose original was small enough to
    have shown in full. So the file itself is the fallback, which by
    definition is only reached when it is already modest in size.

    The 150px preview is last, for a post whose file URL is withheld.
    """
    post = _load_post(body, url)
    if post is None:
        return None
    for key in ("sample", "file", "preview"):
        candidate = (post.get(key) or {}).get("url")
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return None


def parse_rating(body: str, url: str) -> Optional[str]:
    """The post's rating, from the API's own "rating" field."""
    post = _load_post(body, url)
    if post is None:
        return None
    return rating_from_code(post.get("rating"), RATING_CODES)


def parse_dimensions(body: str, url: str):
    """The API reports the post's real width/height directly in its
    "file" object."""
    post = _load_post(body, url)
    if post is None:
        return None, None
    file_info = post.get("file") or {}
    return file_info.get("width"), file_info.get("height")


def _load_post(body: str, url: str) -> Optional[dict]:
    try:
        data = json_of(body)
    except json.JSONDecodeError:
        log.warning("e621 response for %s was not valid JSON (resolve_fetch_url may need updating)", url)
        return None
    # The API nests the post under a "post" key; be defensive in case that
    # ever changes to a bare post object.
    return data.get("post", data) if isinstance(data, dict) else None


def restriction_for_status(status_code: int, body: str, url: str) -> Optional[str]:
    """Why a 401 means "your credentials are wrong", not "the fetch failed".

    e621 answers 401 to EVERY request carrying a bad username or API key,
    including ones that would have worked anonymously. So a typo in the
    settings does not degrade e621 - it breaks it completely, and without
    this the user would see a generic failed fetch on every single post
    with nothing pointing at the credentials they just entered.

    Reported rather than raised so the match survives: the post is fine,
    and clearing the credentials would bring it straight back.
    """
    if status_code != 401:
        return None
    return (
        "e621 rejected the API credentials - check the username and API key in "
        "Settings > Sites (Account > Manage API Access on e621), or clear them to "
        "browse anonymously"
    )


def incomplete_reason(body: str, url: str) -> Optional[str]:
    """Why an e621 post came back with no file to show.

    e621 returns the post either way and simply nulls file.url, so the two
    reasons for that look identical from here and need telling apart -
    only one of them is worth acting on.

    A DELETED post is the common one, and measured: sampling 60 of this
    app's own e621 matches found exactly one withheld file, and it was
    deleted. No account unlocks those, so pointing at the sign-in setting
    would send someone after a fix that cannot work.

    Anything else withheld is the case an account may actually cover, and
    the message says "may" because e621 also gates on per-account content
    settings rather than on the credentials alone.
    """
    post = _load_post(body, url)
    if post is None:
        return None
    if (post.get("file") or {}).get("url"):
        return None  # the post is fully available; it simply has no tags
    if (post.get("flags") or {}).get("deleted"):
        return (
            "This e621 post has been deleted - e621 still describes it but withholds "
            "the file, so there is no picture to preview or download. Signing in does "
            "not bring a deleted post back."
        )
    return (
        "e621 is withholding this post's file. An account may show it - add an e621 "
        "username and API key in Settings > Sites, and check the content settings on "
        "your e621 account - though some posts are withheld from everyone."
    )
