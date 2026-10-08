"""Danbooru, via its official JSON API rather than by scraping the page.

Adding ".json" to a post URL returns the post record, which names the
files explicitly:

    file_url          the actual full-resolution image
    large_file_url    what the site DISPLAYS - often downscaled
    preview_file_url  the small thumbnail

That distinction is the whole reason this module stopped scraping. The
HTML approach read a data-file-url attribute and, when it wasn't there,
fell back to the on-page <img> src - which is large_file_url, the
downscaled sample. So "full resolution" silently became the same
downscaled image the preview already used, indistinguishable from
success, and the side-by-side comparison ended up comparing a local
original against a sample of itself.

The API also gives tags already split by category, and the real
dimensions, so nothing has to be inferred from markup that can change.

Falls back to parsing HTML if the JSON isn't available, so an
unrecognised URL shape or an API hiccup degrades rather than breaks.
"""
from __future__ import annotations

import re
from typing import List, Optional


from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of, soup_of
from ._rating import from_code as rating_from_code

log = get_logger("boorus.danbooru")

HOST = "danbooru.donmai.us"

# Both URL forms. Danbooru's current one is /posts/{id}, but SauceNAO's
# index still returns the legacy /post/show/{id} for older entries, and
# matching only the modern form meant those never reached the JSON API:
# they fell back to HTML scraping, which yields no file URL, no preview,
# no dimensions - and, because the restriction check reads the JSON
# record, no Gold-account detection either. So Gold-only posts arrived
# looking like ordinary matches with a search-engine thumbnail.
POST_ID_RE = re.compile(r"/posts?/(?:show/)?(\d+)")

# The API returns tags pre-split by category, which is both more reliable
# and better structured than reading class names off <li> elements.
TAG_FIELDS = {
    "tag_string_artist": "artist",
    "tag_string_character": "character",
    "tag_string_copyright": "copyright",
    "tag_string_meta": "meta",
    "tag_string_general": "general",
}

# Danbooru's own rating vocabulary, from its API docs' "rating" field.
# Note "s" is SENSITIVE here, not "safe" - Danbooru split the old safe
# rating into general/sensitive, while e621 kept "s" meaning safe. That
# collision is why each site keeps its own table instead of sharing one.
RATING_CODES = {
    "g": "general",
    "s": "sensitive",
    "q": "questionable",
    "e": "explicit",
}

# Retained for the HTML fallback path only.
CATEGORY_MAP = {
    "category-0": "general",
    "category-1": "artist",
    "category-3": "copyright",
    "category-4": "character",
    "category-5": "meta",
}


def matches(url: str) -> bool:
    return on_host(url, (HOST,))


def resolve_fetch_url(url: str) -> str:
    """Point the fetch at the JSON representation of the post.

    Danbooru asks clients to identify themselves and to avoid hammering
    per-ID endpoints; one lookup per match the user is already waiting
    on is well within that, and the shared User-Agent in boorus/__init__
    names the app.
    """
    match = POST_ID_RE.search(url)
    if not match:
        log.debug("No post ID in %s - falling back to fetching the page itself", url)
        return url
    return f"https://{HOST}/posts/{match.group(1)}.json"


def _load_post(body: str, url: str) -> Optional[dict]:
    try:
        data = json_of(body)
    except ValueError:
        return None  # HTML, not JSON - the caller falls back to scraping
    if not isinstance(data, dict):
        return None
    if data.get("success") is False or "id" not in data:
        log.debug("Danbooru API returned no usable post for %s: %s",
                  url, str(data.get("message"))[:120])
        return None
    return data


def parse(body: str, url: str) -> List[Tag]:
    post = _load_post(body, url)
    if post is None:
        return _parse_html_tags(body, url)

    tags: List[Tag] = []
    for field, namespace in TAG_FIELDS.items():
        for name in (post.get(field) or "").split():
            tags.append(Tag(name=name, source=TagSource.BOORU, namespace=namespace))
    if not tags:
        log.debug("Danbooru post %s had no tags in any category field", url)
    return tags


def parse_file_url(body: str, url: str) -> Optional[str]:
    """The genuine full-resolution file.

    Deliberately does NOT quietly substitute large_file_url when file_url
    is absent - that's what made the old scraper misleading. Danbooru
    withholds file_url for some posts (deleted or restricted ones), and
    in that case the sample genuinely is the best available, so it IS
    used - but the substitution is logged, because "the comparison looks
    low-res" is otherwise impossible to explain.
    """
    post = _load_post(body, url)
    if post is None:
        return _parse_html_file_url(body, url)

    file_url = post.get("file_url")
    if file_url:
        return file_url

    large = post.get("large_file_url")
    if large:
        log.info(
            "Danbooru gives no full-resolution file_url for %s (restricted or deleted post) - "
            "using the downscaled large_file_url instead, so comparisons will show the sample",
            url,
        )
        return large
    log.debug("Danbooru post %s exposes no usable file URL at all", url)
    return None


def parse_preview_url(body: str, url: str) -> Optional[str]:
    """large_file_url - what the site itself shows. Sharper than the
    search engine's thumbnail, far smaller than the original."""
    post = _load_post(body, url)
    if post is None:
        return _parse_html_preview_url(body, url)
    return post.get("large_file_url") or post.get("preview_file_url")


def parse_dimensions(body: str, url: str):
    """The original's real dimensions - what the size comparison should
    judge against, not the sample's."""
    post = _load_post(body, url)
    if post is None:
        return None, None
    return post.get("image_width"), post.get("image_height")


def parse_restriction(body: str, url: str) -> Optional[str]:
    """Why this post can't be fully viewed, or None if it can.

    Danbooru gates some posts behind a Gold account in two separate ways:

    - BANNED posts, where an artist asked for their work to be taken
      down. Danbooru keeps the post but shows it only to Gold+ accounts.
      Flagged explicitly as is_banned.
    - CENSORED TAGS (loli/shota), which Danbooru's own page describes as
      requiring "a Gold account to view".

    Both look the same to an anonymous client: the post record comes
    back, tags and all, but with no file_url. That's the general test
    used here - it catches whatever other restriction Danbooru adds
    later, without needing to know its name.

    Returns a short human reason so the log can say WHICH kind, since
    "banned" and "censored" have different implications for whether
    getting an account would help.
    """
    post = _load_post(body, url)
    if post is None:
        return None

    if post.get("is_banned"):
        return "banned on Danbooru (artist takedown) - only Gold+ accounts can view it"

    if not post.get("file_url"):
        # The record exists but the file is withheld. For an anonymous
        # request that means a Gold-gated post; large_file_url is
        # normally withheld along with it.
        if not post.get("large_file_url"):
            return "restricted on Danbooru - viewing it needs a Gold account"
        return None
    return None


def parse_rating(body: str, url: str) -> Optional[str]:
    """The post's rating, from the API's own "rating" field.

    JSON only, deliberately. The HTML fallback path is reached when the
    API did not answer, and the rating is not something this parser can
    read off the page with the same confidence as the named field - so
    that path contributes no rating rather than a guessed one.
    """
    post = _load_post(body, url)
    if post is None:
        return None
    return rating_from_code(post.get("rating"), RATING_CODES)


def parse_file_size(body: str, url: str) -> Optional[int]:
    post = _load_post(body, url)
    return post.get("file_size") if post else None


# ----------------------------------------------------------------------
# HTML fallback, used only when the JSON API isn't available.
# ----------------------------------------------------------------------
def _parse_html_tags(html: str, url: str) -> List[Tag]:
    soup = soup_of(html)
    tags: List[Tag] = []
    container = soup.select_one("section#tag-list") or soup.select_one("ul#tag-list")
    if not container:
        log.debug("No #tag-list container found on %s (markup may have changed)", url)
        return tags

    for li in container.select("li"):
        classes = li.get("class") or []  # type: ignore[var-annotated]  # bs4 stub: Tag.get() typed str | list[str] | None regardless of attribute
        namespace = next((CATEGORY_MAP[c] for c in classes if c in CATEGORY_MAP), "general")
        name_el = li.select_one("a.search-tag") or li.select_one("a[href*='tags']")
        if not name_el:
            continue
        name = name_el.get_text(strip=True).replace(" ", "_")
        if name:
            tags.append(Tag(name=name, source=TagSource.BOORU, namespace=namespace))
    return tags


def _parse_html_file_url(html: str, url: str) -> Optional[str]:
    soup = soup_of(html)
    container = soup.select_one("#image-container")
    if container and container.get("data-file-url"):
        return container["data-file-url"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    # NOTE: no #image fallback here on purpose. That element is the
    # downscaled sample, and returning it as the "full resolution file"
    # is exactly the bug this module was rewritten to fix - it made a
    # low-resolution comparison look like a successful one.
    log.debug("No data-file-url on %s and no JSON available - no full-resolution URL", url)
    return None


def _parse_html_preview_url(html: str, url: str) -> Optional[str]:
    soup = soup_of(html)
    img = soup.select_one("#image")
    if img and img.get("src"):
        return img["src"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    return None
