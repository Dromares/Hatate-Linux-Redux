"""Construction + parse harness for core/boorus/ (DAN-19, Stage B).

One parametrized table, one runner. Per adapter: instantiate the module,
assert it claims its own URL and NOT a foreign one (the cheap construction
check - this is the class of bug the DAN-6 settings-dialog P0 belonged to),
then feed it ONE recorded response and assert the parsed fields.

No live network anywhere. The Gelbooru-family adapters (rule34, xbooru,
gelbooru) memoize an API response in a module-level ``_last_api_post``
global. That state persists between test cases and would leak into each
other, so this harness resets every such global in setUp AND tearDown
(Finding 2 from DAN-13) and seeds a recorded post directly into the memo
so the API-first path is exercised offline. See TestModuleGlobalLeak for
the explicit proof that the reset happens.

Fixtures are recorded shapes of each site's real response (tag-list markup
for the HTML sites, the documented Data-API / JSON body for the JSON sites),
not invented fields the parser does not read.
"""
import json
import unittest
from dataclasses import dataclass
from typing import Optional, Tuple

from . import _path  # noqa: F401

# _path must run BEFORE the first core.* import: core.paths reads
# XDG_CONFIG_HOME once at import time, and _path redirects it to a throwaway
# directory. Without that redirect a test that calls search_image would read
# (and write) the user's OWN search cache - and a stale entry in it would be
# fed back to an unrelated test. Every other test module does this first.
from core.boorus import (animepictures, danbooru, deviantart, e621, eshuushuu,
                         gelbooru, mangadex, moebooru, pawchive, pixiv, rule34,
                         sankaku, safebooru, twitter, xbooru, zerochan)
from core.config import Settings
from core.models import ImageEntry, MatchStatus, Tag, TagSource

# The Gelbooru-family modules that carry a module-level API memo. rule34 and
# xbooru own their own _last_api_post; gelbooru's is exercised here only so
# the leak-reset is total (it would otherwise contaminate a later case).
_MEMO_MODULES = (rule34, xbooru)


def _named(tags):
    return {(t.namespace, t.name) for t in tags}


# -- recorded responses ----------------------------------------------------
#
# Gelbooru-family tag-list markup (verified shape: each <li> holds a '?'
# wiki link BEFORE the real tag link; the parser must pick the real one).
GELBOORU_FAMILY_LI = """
  <li class="tag-type-copyright tag">
    <a href="index.php?page=wiki&amp;s=list&amp;search=fire_emblem">?</a>
    <a href="index.php?page=post&amp;s=list&amp;tags=fire_emblem">fire emblem</a>
    <span>123</span>
  </li>
  <li class="tag-type-artist tag">
    <a href="index.php?page=post&amp;s=list&amp;tags=someone">someone</a>
  </li>
  <li class="tag-type-general tag">
    <a href="index.php?page=post&amp;s=list&amp;tags=1boy">1boy</a>
  </li>
"""

# rule34 / xbooru tag page: the relatives use #tag-sidebar (not #tag-list).
GELBOORU_FAMILY_TAGS_PAGE = (
    "<html><body><ul id=\"tag-sidebar\">" + GELBOORU_FAMILY_LI + "</ul></body></html>"
)

# rule34 / xbooru HTML-fallback page: no API post, so the file/preview/dims
# hooks must recover from the page itself.
GELBOORU_FAMILY_HTML_PAGE = (
    "<html><body>"
    "<ul id=\"tag-sidebar\">" + GELBOORU_FAMILY_LI + "</ul>"
    '<a href="https://img2.rule34.xxx/images/a/b/real.png">Original image</a>'
    '<img id="image" src="https://img2.rule34.xxx/samples/a/b/real.jpg">'
    "<li>Size: 1500x2000</li>"
    "</body></html>"
)

# A recorded Gelbooru-family Data-API post (the dict post_from_dapi returns
# for a single post). Only the fields the parser reads are present.
GELBOORU_FAMILY_API_POST = {
    "id": 10755716,
    "file_url": "https://img2.rule34.xxx/images/a/b/api.png",
    "sample_url": "https://img2.rule34.xxx/samples/a/b/api.jpg",
    "width": 1500,
    "height": 2000,
    "rating": "s",
}

# e621: the public JSON API body (post nested under "post", tags keyed by
# e621's own category field names).
E621_BODY = json.dumps({
    "post": {
        "id": 219935,
        "file": {"url": "https://e621.net/data/abc/real.png",
                 "width": 664, "height": 909, "ext": "png"},
        "sample": {"has": True, "url": "https://e621.net/data/sample/s.jpg",
                   "width": 664, "height": 909},
        "preview": {"url": "https://e621.net/data/preview/p.jpg",
                    "width": 256, "height": 350},
        "tags": {
            "general": ["1boy", "fire_emblem"],
            "artist": ["someone"],
            "character": ["unit_x"],
            "copyright": ["fire_emblem_8"],
        },
    }
})

# moebooru (yande.re / konachan): the tag sidebar, the #highres original
# link, the #image sample, the embedded Post.register record, and the
# #stats Statistics block that states the rating in words.
#
# The comment block is NOT decoration: a post comment is user-supplied
# prose that DOES reach get_text(), so reading the rating off the page
# text instead of the #stats element would report "explicit" here because
# a stranger typed the word. Keeping it in the recorded shape means this
# row catches that on its own. (yande.re also serves a blacklist script
# containing "rating:e", but get_text() omits script text, so that is not
# the hazard - see tests/test_parsers_and_tags.TestRatingParsing.)
MOEBOORU_PAGE = """
<html><body>
<div id="comments"><div class="comment">
  someone: mis-tagged, Rating: explicit surely?
</div></div>
<ul id="tag-sidebar">
  <li class="tag-type-general"><a href="/post?tags=sol">sol</a></li>
  <li class="tag-type-character"><a href="/post?tags=unit_x">unit x</a></li>
  <li class="tag-type-artist"><a href="/post?tags=artist_name">artist name</a></li>
</ul>
<a id="highres" href="https://cdn.yande.re/images/a/b/real.png">PNG (1500x2000, 2.5 MB)</a>
<img id="image" src="https://cdn.yande.re/sample/a/b/sample.jpg">
<div id="stats" class="vote-container">
  <h5>Statistics</h5>
  <ul>
    <li>Id: 123</li>
    <li>Size: 1500x2000</li>
    <li>Rating: Questionable <span class="vote-desc"></span></li>
  </ul>
</div>
<script>Post.register(123, {"width": 1500, "height": 2000});</script>
</body></html>
"""

# safebooru: pure HTML (deliberately no Data-API path, to avoid gelbooru's
# 401 latch). The page states the file, the sample, and the original size.
SAFEBOORU_PAGE = """
<html><body>
<ul id="tag-sidebar">
  <li class="tag-type-general tag"><a href="index.php?page=post&amp;s=list&amp;tags=1boy">1boy</a></li>
  <li class="tag-type-artist tag"><a href="index.php?page=post&amp;s=list&amp;tags=someone">someone</a></li>
  <li class="tag-type-copyright tag"><a href="index.php?page=post&amp;s=list&amp;tags=fire_emblem">fire emblem</a></li>
</ul>
<a href="https://img4.safebooru.org/images/a/b/real.png">Original image</a>
<img id="image" src="https://img4.safebooru.org/samples/a/b/real.jpg">
<li>Size: 1152x1440</li>
</body></html>
"""

GELBOORU_FAMILY_TAGS = frozenset({
    ("copyright", "fire_emblem"),
    ("artist", "someone"),
    ("general", "1boy"),
})

E621_TAGS = frozenset({
    ("general", "1boy"),
    ("general", "fire_emblem"),
    ("artist", "someone"),
    ("character", "unit_x"),
    ("copyright", "fire_emblem_8"),
})

MOEBOORU_TAGS = frozenset({
    ("general", "sol"),
    ("character", "unit_x"),
    ("artist", "artist_name"),
})

# -- recorded fixtures reused from the existing suite (not invented here) --
# Pixiv /ajax/illust/{id} body (test_parsers_and_tags.AJAX).
PIXIV_AJAX = json.dumps({"error": False, "message": "", "body": {
    "illustId": "75494369", "userName": "Some Artist",
    "width": 2894, "height": 4093,
    "tags": {"tags": [{"tag": "original"}]},
    "urls": {"original": "https://i.pximg.net/img-original/o.png",
             "regular": "https://i.pximg.net/img-master/r.jpg"},
}})

# Danbooru JSON API post (test_parsers_and_tags.DANBOORU_POST_JSON).
DANBOORU_POST_JSON = json.dumps({
    "id": 5105386,
    "image_width": 2894, "image_height": 4093, "file_size": 3500000,
    "file_url": "https://cdn.donmai.us/original/b7/7e/b77e69be.jpg",
    "large_file_url": "https://cdn.donmai.us/sample/b7/7e/sample-b77e69be.jpg",
    "preview_file_url": "https://cdn.donmai.us/preview/b7/7e/b77e69be.jpg",
    "tag_string_general": "1girl solo long_hair",
    "tag_string_artist": "someone",
    "tag_string_character": "hakurei_reimu",
    "tag_string_copyright": "touhou",
    "tag_string_meta": "highres",
})

# e-shuushuu post page for image #331875 (test_parsers_and_tags.
# ESHUUSHUU_POST_HTML) - its tag set was cross-checked against the site's
# own Atom feed, so the expected namespaces below are ground truth.
ESHUUSHUU_POST_HTML = """
<html><head>
<meta property="og:image" content="https://cdn.e-shuushuu.net/thumbs/2010-10-07-331875.webp">
<meta property="og:description" content="Cute anime artwork by Sumomo KPA from Cabal Online. 800&times;600. Tagged: black hair, gloves, long hair, smile, tree, yellow eyes.">
</head><body>
<main class="some-class-that-may-change">
  <a href="https://cdn.e-shuushuu.net/fullsize/2010-10-07-331875.jpeg"><img
     src="https://cdn.e-shuushuu.net/thumbs/2010-10-07-331875.webp"></a>
  <section><h2>Tags</h2>
    <a href="https://e-shuushuu.net/search?tags=2457">Sumomo KPA</a><a href="https://e-shuushuu.net/tags/2457">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=12878">Cabal Online</a><a href="https://e-shuushuu.net/tags/12878">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=181">black hair</a><a href="https://e-shuushuu.net/tags/181">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=1343">gloves</a><a href="https://e-shuushuu.net/tags/1343">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=46">long hair</a><a href="https://e-shuushuu.net/tags/46">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=143">smile</a><a href="https://e-shuushuu.net/tags/143">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=126">tree</a><a href="https://e-shuushuu.net/tags/126">&rsaquo;</a>
    <a href="https://e-shuushuu.net/search?tags=12524">yellow eyes</a><a href="https://e-shuushuu.net/tags/12524">&rsaquo;</a>
  </section>
  <dl><dt>Dimensions:</dt><dd>800 &times; 600</dd></dl>
</main></body></html>
"""

# Zerochan JSON API body (recorded shape of a real post, same fields the
# existing suite's _body() helper feeds the parser).
ZEROCHAN_BODY = json.dumps({
    "tags": ["Artist: Some Artist", "Character: Unit X", "Series: Fire Emblem",
             "1boy", "fire_emblem"],
    "small": "https://cdn.zerochan.net/small/s.webp",
    "medium": "https://cdn.zerochan.net/medium/m.webp",
    "large": "https://cdn.zerochan.net/large/l.webp",
    "full": "https://cdn.zerochan.net/full/f.png",
    "width": 2976, "height": 4055,
})

# MangaDex at-home server response (test_parsers_and_tags.MANGADEX_AT_HOME).
MANGADEX_AT_HOME = json.dumps({
    "result": "ok",
    "baseUrl": "https://node1.mangadex.network",
    "chapter": {
        "hash": "abc123hash",
        "data": ["1-aaa.png", "2-bbb.png", "3-ccc.png"],
        "dataSaver": ["1-xxx.jpg", "2-yyy.jpg", "3-zzz.jpg"],
    },
})

PIXIV_TAGS = frozenset({
    (None, "original"),
    ("creator", "Some_Artist"),
})
DANBOORU_TAGS = frozenset({
    ("general", "1girl"),
    ("general", "solo"),
    ("general", "long_hair"),
    ("artist", "someone"),
    ("character", "hakurei_reimu"),
    ("copyright", "touhou"),
    ("meta", "highres"),
})
ESHUUSHUU_TAGS = frozenset({
    ("creator", "Sumomo_KPA"),
    ("series", "Cabal_Online"),
    (None, "black_hair"),
    (None, "gloves"),
    (None, "long_hair"),
    (None, "smile"),
    (None, "tree"),
    (None, "yellow_eyes"),
})
ZEROCHAN_TAGS = frozenset({
    ("creator", "Some_Artist"),
    ("character", "Unit_X"),
    ("series", "Fire_Emblem"),
    (None, "1boy"),
    (None, "fire_emblem"),
})

# DeviantArt: the page embeds window.__INITIAL_STATE__ = JSON.parse("...") -
# a JSON document inside a JS string literal, so the state is double-encoded.
_DEVART_STATE = {
    "@@entities": {
        "user": {"7": {"username": "someArtist"}},
        "deviation": {"123": {"id": 123, "author": 7,
                              "media": {"baseUri": "https://a.deviantart.com/Preview",
                                        "prettyName": "art", "token": ["tok123"],
                                        "types": [{"t": "fullview",
                                                   "c": "/v1/<prettyName>~mp5/?key=9"}]}}},
        "deviationExtended": {"123": {"id": 123,
                                      "originalFile": {"type": "jpg", "filesize": 268096,
                                                       "width": 3000, "height": 2000}}},
    }
}
DEVIANART_BODY = ("window.__INITIAL_STATE__ = JSON.parse("
                  + json.dumps(json.dumps(_DEVART_STATE)) + ");\n")

# Twitter/X: the fxtwitter API body for a photo tweet. The photo URL carries
# ?name=orig, which the parser rewrites to the size it wants.
TWITTER_BODY = json.dumps({
    "tweet": {"author": {"screen_name": "someArtist"},
              "media": {"photos": [{"url": "https://pbs.twimg.com/media/abc.jpg?name=orig",
                                    "width": 1200, "height": 1600}]}}})

# Sankaku: the hash-search API answers a LIST of posts; the first is the one.
SANKAKU_BODY = json.dumps([{
    "id": "555",
    "tags": [{"name_en": "someArtist", "type": 1},
             {"name_en": "a game", "type": 3},
             {"name_en": "character_x", "type": 4},
             {"name_en": "long hair", "type": 2}],
    "file_url": "https://sankaku.example/file/a.png",
    "sample_url": "https://sankaku.example/sample/a.jpg",
    "width": 1920, "height": 1080,
    # "s" here is SAFE - Sankaku is Danbooru 1.x lineage, unlike
    # Danbooru's own "s" (sensitive). See sankaku.RATING_CODES.
    "rating": "s",
}])

# Pawchive: the Kemono-style API record. "tags" is a Postgres ARRAY LITERAL in
# a string (quoted elements may contain commas); "has_full" says whether the
# archive holds the original; the file path is a leading-"/data/..." path.
PAWCHIVE_BODY = json.dumps({
    "id": 154701291, "service": "pixiv", "user": "6714576",
    "tags": '{Bondage,"Total Versext",gefesselt}',
    "has_full": True,
    "file": {"path": "/data/pixiv/6714576/post/154701291/"
                    "deadbeef00000000000000000000000000000000000000000000000000000000.png",
             "name": "art.png"},
})

# Anime-Pictures: the v3 API record - tags as {"tag": {"tag", "type"}} pairs,
# the original only when it is an absolute URL (the API's own file_url is a
# bare filename, which the parser must refuse).
ANIMEPICTURES_BODY = json.dumps({
    "id": 33000000, "width": 2048, "height": 1152, "ext": "jpg", "size": 512000,
    "file_url": "https://cdn.anime-pictures.net/images/a/b/real.jpg",
    "big_preview": "https://cdn.anime-pictures.net/preview/a/b/big.jpg",
    "tags": [{"tag": {"tag": "someArtist", "type": 4}},
             {"tag": {"tag": "character_x", "type": 1}},
             {"tag": {"tag": "long hair", "type": 2}}],
})

DA_TAGS = frozenset({("artist", "someArtist")})
TWITTER_TAGS = frozenset({("artist", "someArtist")})
SANKAKU_TAGS = frozenset({
    ("artist", "someArtist"),
    ("copyright", "a_game"),
    ("character", "character_x"),
    ("general", "long_hair"),
})
PAWCHIVE_TAGS = frozenset({
    ("general", "Bondage"),
    ("general", "Total_Versext"),
    ("general", "gefesselt"),
    ("artist", "someArtist"),
})
ANIMEPICTURES_TAGS = frozenset({
    ("artist", "someArtist"),
    ("character", "character_x"),
    ("general", "long_hair"),
})


@dataclass(frozen=True)
class _Row:
    label: str
    module: object
    url: str
    url_not_matched: str
    response: str
    tags: frozenset
    file_url: Optional[str] = None
    preview_url: Optional[str] = None
    dimensions: Optional[Tuple[int, int]] = None
    # (module, url, post_or_None) to seed into that module's _last_api_post for
    # the Gelbooru-family API-first hooks. None => do not seed (no API memo:
    # e621/moebooru are pure JSON/HTML). A seeded (..., None) post means "the
    # API returned nothing" and forces the HTML fallback WITHOUT any network
    # call. NOTE safebooru has no memo of its own: its parse_file_url delegates
    # to gelbooru's API-first path, so its row seeds GELBOORU's memo.
    memo: Optional[Tuple[object, str, Optional[dict]]] = None
    # Pawchive's creator name comes from a second (profile) request that is
    # cached in a module-level LRUCache. Seeding that cache keeps the artist
    # tag offline - the same reason the Gelbooru-family rows seed the memo.
    artist_cache: Optional[Tuple[str, str]] = None
    # Sankaku repoints the match at the modern post URL once the hash has
    # identified the post (parse_canonical_url); assert it when the row names one.
    canonical_url: Optional[str] = None
    # The post's age rating, for the rows whose site states one. Only
    # meaningful per-site: the SAME letter means different things on
    # different boorus (see core/boorus/_rating.py), which is exactly what
    # a shared table like this one would otherwise paper over.
    rating: Optional[str] = None


# The parametrized table. Adding the 27th adapter is one more _Row here.
ADAPTER_ROWS = [
    _Row(
        label="rule34 via Data-API post (memo hit)",
        module=rule34,
        url="https://rule34.xxx/index.php?page=post&s=view&id=10755716",
        url_not_matched="https://gelbooru.com/index.php?page=post&s=view&id=1",
        response=GELBOORU_FAMILY_TAGS_PAGE,
        tags=GELBOORU_FAMILY_TAGS,
        file_url="https://img2.rule34.xxx/images/a/b/api.png",
        preview_url="https://img2.rule34.xxx/samples/a/b/api.jpg",
        dimensions=(1500, 2000),
        memo=(rule34, "https://rule34.xxx/index.php?page=post&s=view&id=10755716",
              dict(GELBOORU_FAMILY_API_POST)),
    ),
    _Row(
        label="rule34 HTML fallback (API returned nothing)",
        module=rule34,
        url="https://rule34.xxx/index.php?page=post&s=view&id=10755716",
        url_not_matched="https://gelbooru.com/index.php?page=post&s=view&id=1",
        response=GELBOORU_FAMILY_HTML_PAGE,
        tags=GELBOORU_FAMILY_TAGS,
        file_url="https://img2.rule34.xxx/images/a/b/real.png",
        preview_url="https://img2.rule34.xxx/samples/a/b/real.jpg",
        dimensions=(1500, 2000),
        memo=(rule34, "https://rule34.xxx/index.php?page=post&s=view&id=10755716", None),
    ),
    _Row(
        label="xbooru via Data-API post (memo hit)",
        module=xbooru,
        url="https://xbooru.com/index.php?page=post&s=view&id=42",
        url_not_matched="https://gelbooru.com/index.php?page=post&s=view&id=1",
        response=GELBOORU_FAMILY_TAGS_PAGE,
        tags=GELBOORU_FAMILY_TAGS,
        file_url="https://img2.rule34.xxx/images/a/b/api.png",
        preview_url="https://img2.rule34.xxx/samples/a/b/api.jpg",
        dimensions=(1500, 2000),
        memo=(xbooru, "https://xbooru.com/index.php?page=post&s=view&id=42",
              dict(GELBOORU_FAMILY_API_POST)),
    ),
    _Row(
        label="e621 JSON API body",
        module=e621,
        url="https://e621.net/posts/219935",
        url_not_matched="https://gelbooru.com/index.php?page=post&s=view&id=1",
        response=E621_BODY,
        tags=E621_TAGS,
        file_url="https://e621.net/data/abc/real.png",
        preview_url="https://e621.net/data/sample/s.jpg",
        dimensions=(664, 909),
    ),
    _Row(
        label="moebooru tag-sidebar page",
        module=moebooru,
        url="https://yande.re/post/show/123",
        url_not_matched="https://e621.net/posts/219935",
        response=MOEBOORU_PAGE,
        tags=MOEBOORU_TAGS,
        file_url="https://cdn.yande.re/images/a/b/real.png",
        preview_url="https://cdn.yande.re/sample/a/b/sample.jpg",
        dimensions=(1500, 2000),
        # Off the #stats block, NOT the "rating:e" in the blacklist script
        # the recorded page carries ahead of it.
        rating="questionable",
    ),
    _Row(
        label="safebooru pure-HTML page",
        module=safebooru,
        url="https://safebooru.org/index.php?page=post&s=view&id=7",
        url_not_matched="https://gelbooru.com/index.php?page=post&s=view&id=1",
        response=SAFEBOORU_PAGE,
        tags=GELBOORU_FAMILY_TAGS,
        file_url="https://img4.safebooru.org/images/a/b/real.png",
        preview_url="https://img4.safebooru.org/samples/a/b/real.jpg",
        dimensions=(1152, 1440),
        # safebooru.parse_file_url delegates to gelbooru's API-first path, so
        # seed GELBOORU's memo with a None post: the API "returned nothing",
        # which forces the HTML fallback offline (no live safebooru API call).
        memo=(gelbooru, "https://safebooru.org/index.php?page=post&s=view&id=7", None),
    ),
    _Row(
        label="pixiv /ajax/illust JSON body (recorded)",
        module=pixiv,
        url="https://www.pixiv.net/artworks/75494369",
        url_not_matched="https://e621.net/posts/219935",
        response=PIXIV_AJAX,
        tags=PIXIV_TAGS,
        file_url="https://i.pximg.net/img-original/o.png",
        preview_url="https://i.pximg.net/img-master/r.jpg",
        dimensions=(2894, 4093),
    ),
    _Row(
        label="danbooru JSON API post (recorded)",
        module=danbooru,
        url="https://danbooru.donmai.us/posts/5105386",
        url_not_matched="https://e621.net/posts/219935",
        response=DANBOORU_POST_JSON,
        tags=DANBOORU_TAGS,
        file_url="https://cdn.donmai.us/original/b7/7e/b77e69be.jpg",
        preview_url="https://cdn.donmai.us/sample/b7/7e/sample-b77e69be.jpg",
        dimensions=(2894, 4093),
    ),
    _Row(
        label="eshuushuu post page (recorded, Atom-verified namespaces)",
        module=eshuushuu,
        url="https://e-shuushuu.net/image/331875",
        url_not_matched="https://danbooru.donmai.us/posts/1",
        response=ESHUUSHUU_POST_HTML,
        tags=ESHUUSHUU_TAGS,
        file_url="https://cdn.e-shuushuu.net/fullsize/2010-10-07-331875.jpeg",
        preview_url="https://cdn.e-shuushuu.net/thumbs/2010-10-07-331875.webp",
        dimensions=(800, 600),
    ),
    _Row(
        label="zerochan JSON API body (recorded shape)",
        module=zerochan,
        url="https://www.zerochan.net/123456",
        url_not_matched="https://e621.net/posts/219935",
        response=ZEROCHAN_BODY,
        tags=ZEROCHAN_TAGS,
        file_url="https://cdn.zerochan.net/full/f.png",
        preview_url="https://cdn.zerochan.net/large/l.webp",
        dimensions=(2976, 4055),
    ),
    _Row(
        label="mangadex at-home server response (recorded; parse is tag-free by design)",
        module=mangadex,
        url="https://mangadex.org/chapter/2f1d73c5-b3c9-450a-bc5f-fa702940c36f/",
        url_not_matched="https://mangadex.org/title/2f1d73c5-b3c9-450a-bc5f-fa702940c36f/some-manga",
        response=MANGADEX_AT_HOME,
        tags=frozenset(),
        file_url="https://node1.mangadex.network/data/abc123hash/1-aaa.png",
        preview_url="https://node1.mangadex.network/data-saver/abc123hash/1-xxx.jpg",
        # mangadex exposes no parse_dimensions: the API reports none, and the
        # resolver reads unknown as "no evidence" (hashing) rather than a guess.
    ),
    _Row(
        label="deviantart __INITIAL_STATE__ (recorded double-encoded shape)",
        module=deviantart,
        url="https://www.deviantart.com/someArtist/art/Art-123",
        url_not_matched="https://www.pixiv.net/artworks/1",
        response=DEVIANART_BODY,
        tags=DA_TAGS,
        # file_url is None by design: DeviantArt serves only renders (every
        # render path carries /v1/), and the original 403s even with the
        # page's own token. preview is the best render it will actually serve.
        preview_url="https://a.deviantart.com/Preview/v1/art~mp5/?key=9?token=tok123",
        dimensions=(3000, 2000),
    ),
    _Row(
        label="twitter/X fxtwitter API body (recorded shape)",
        module=twitter,
        url="https://x.com/someArtist/status/123456",
        url_not_matched="https://reddit.com/comments/abc123",
        response=TWITTER_BODY,
        tags=TWITTER_TAGS,
        file_url="https://pbs.twimg.com/media/abc.jpg?name=orig",
        preview_url="https://pbs.twimg.com/media/abc.jpg?name=large",
        dimensions=(1200, 1600),
    ),
    _Row(
        label="sankaku hash-search API (recorded shape)",
        module=sankaku,
        url="https://www.sankakucomplex.com/post/12345",
        url_not_matched="https://gelbooru.com/index.php?page=post&s=view&id=1",
        response=SANKAKU_BODY,
        tags=SANKAKU_TAGS,
        file_url="https://sankaku.example/file/a.png",
        preview_url="https://sankaku.example/sample/a.jpg",
        dimensions=(1920, 1080),
        canonical_url="https://www.sankakucomplex.com/posts/555",
        rating="safe",
    ),
    _Row(
        label="pawchive Kemono API record (recorded shape; creator cached)",
        module=pawchive,
        url="https://pawchive.pw/pixiv/user/6714576/post/154701291",
        url_not_matched="https://e621.net/posts/1",
        response=PAWCHIVE_BODY,
        tags=PAWCHIVE_TAGS,
        file_url=("https://file.pawchive.pw/data/data/pixiv/6714576/post/"
                  "154701291/"
                  "deadbeef00000000000000000000000000000000000000000000000000000000.png"
                  "?f=art.png"),
        preview_url=("https://img.pawchive.pw/thumbnail/data/data/pixiv/"
                     "6714576/post/154701291/"
                     "deadbeef00000000000000000000000000000000000000000000000000000000.png"),
        # pawchive exposes no offline dimensions (it reads the file header over
        # a Range request) - left unasserted on purpose, like mangadex.
        artist_cache=("pixiv/6714576", "someArtist"),
    ),
    _Row(
        label="anime-pictures v3 API record (recorded shape)",
        module=animepictures,
        url="https://www.anime-pictures.net/pictures/view_post/33000000",
        url_not_matched="https://www.pixiv.net/artworks/1",
        response=ANIMEPICTURES_BODY,
        tags=ANIMEPICTURES_TAGS,
        file_url="https://cdn.anime-pictures.net/images/a/b/real.jpg",
        preview_url="https://cdn.anime-pictures.net/preview/a/b/big.jpg",
        dimensions=(2048, 1152),
    ),
]


class TestBoorusConstructionAndParse(unittest.TestCase):
    """The parametrized harness: construction + parse over recorded fixtures."""

    def setUp(self):
        # Leak handling (DAN-13 Finding 2): clear every module-level API memo
        # before each case so a stale post from a previous case cannot leak in.
        for module in _MEMO_MODULES:
            module._last_api_post = None
        from core.boorus import gelbooru
        gelbooru._last_api_post = None
        gelbooru._api_unauthorized = False
        # Pawchive's LRU creator cache is the same kind of module-level
        # state: a stale name from a previous case would tag the wrong creator.
        pawchive._artist_cache.clear()

    def tearDown(self):
        # ...and clear them again afterwards, so this file can never contaminate
        # the tests that run after it.
        for module in _MEMO_MODULES:
            module._last_api_post = None
        from core.boorus import gelbooru
        gelbooru._last_api_post = None
        gelbooru._api_unauthorized = False
        pawchive._artist_cache.clear()

    def test_every_row(self):
        for row in ADAPTER_ROWS:
            with self.subTest(adapter=row.label):
                mod = row.module

                # Construction: the module exposes the parser contract and
                # classifies URLs correctly (claims its own, rejects a foreign one).
                self.assertTrue(callable(getattr(mod, "matches", None)),
                                f"{mod.__name__} has no matches()")
                self.assertTrue(callable(getattr(mod, "parse", None)),
                                f"{mod.__name__} has no parse()")
                self.assertTrue(mod.matches(row.url),
                                f"{mod.__name__} should claim {row.url}")
                self.assertFalse(mod.matches(row.url_not_matched),
                                 f"{mod.__name__} wrongly claimed {row.url_not_matched}")

                # Seed the recorded API post into whichever module's memo the
                # row names (offline - no network, even for safebooru whose
                # file_url path delegates into gelbooru's API-first code).
                if row.memo is not None:
                    memo_mod, memo_url, memo_post = row.memo
                    memo_mod._last_api_post = (memo_url, memo_post)
                # Seed pawchive's LRU creator cache (offline profile lookup).
                if row.artist_cache is not None:
                    pawchive._artist_cache[row.artist_cache[0]] = row.artist_cache[1]

                # Parse: one recorded response -> asserted fields.
                self.assertEqual(_named(mod.parse(row.response, row.url)), row.tags,
                                 f"{mod.__name__} parsed tags wrong")
                if row.file_url is not None:
                    self.assertEqual(mod.parse_file_url(row.response, row.url),
                                     row.file_url, f"{mod.__name__} file_url")
                if row.preview_url is not None:
                    self.assertEqual(mod.parse_preview_url(row.response, row.url),
                                     row.preview_url, f"{mod.__name__} preview_url")
                if row.dimensions is not None:
                    self.assertEqual(mod.parse_dimensions(row.response, row.url),
                                     row.dimensions, f"{mod.__name__} dimensions")
                if row.canonical_url is not None:
                    self.assertEqual(mod.parse_canonical_url(row.response, row.url),
                                     row.canonical_url, f"{mod.__name__} canonical_url")
                if row.rating is not None:
                    self.assertEqual(mod.parse_rating(row.response, row.url),
                                     row.rating, f"{mod.__name__} rating")


class TestModuleGlobalLeak(unittest.TestCase):
    """Prove the module-level API memo is reset between cases (Finding 2).

    test_01 deliberately leaves a poisoned memo behind; test_02 (running
    after it, forced by the 01/02 prefixes) asserts it was cleared by
    tearDown. Without the reset, test_02 would read the stale post.
    """

    def setUp(self):
        rule34._last_api_post = None

    def tearDown(self):
        rule34._last_api_post = None

    def test_01_poison_the_memo(self):
        # Simulate a previous case that fetched an API post and left it behind.
        rule34._last_api_post = (
            "https://rule34.xxx/index.php?page=post&s=view&id=999",
            {"file_url": "STALE-LEAK"},
        )

    def test_02_memo_was_cleared_by_teardown(self):
        self.assertIsNone(rule34._last_api_post,
                          "a stale _last_api_post leaked into the next case")

    def test_seeded_memo_is_used_offline(self):
        # A seeded memo is returned by the API-first hooks with no network:
        # this is what lets the parse harness exercise the API path offline.
        url = "https://rule34.xxx/index.php?page=post&s=view&id=5"
        rule34._last_api_post = (url, dict(GELBOORU_FAMILY_API_POST))
        try:
            self.assertEqual(
                rule34.parse_file_url("<html></html>", url),
                "https://img2.rule34.xxx/images/a/b/api.png")
            self.assertEqual(rule34.parse_dimensions("<html></html>", url),
                             (1500, 2000))
        finally:
            rule34._last_api_post = None


class TestDecideStatus(unittest.TestCase):
    """DAN-13 Finding 1: _decide_status had zero coverage (lines 552-560
    never executed). Cover both the GOOD and the POOR branches, including
    the 'larger than local' width/height comparison that is the app's core
    user-facing signal."""

    def _entry(self, n_tags, local=None, match=None):
        tags = [Tag(name=f"tag{i}", source=TagSource.BOORU, namespace="general")
                for i in range(n_tags)]
        kw = {}
        if local:
            kw["local_width"], kw["local_height"] = local
        if match:
            kw["match_width"], kw["match_height"] = match
        return ImageEntry(path="x.jpg", tags=tags, **kw)

    def test_few_tags_is_poor(self):
        from core.search_engine import _decide_status
        entry = self._entry(2)  # below min_tags_for_good (5)
        self.assertEqual(_decide_status(entry, Settings()), MatchStatus.POOR)

    def test_enough_tags_and_no_size_info_is_good(self):
        from core.search_engine import _decide_status
        entry = self._entry(5)
        self.assertEqual(_decide_status(entry, Settings()), MatchStatus.GOOD)

    def test_enough_tags_but_match_is_much_larger_is_poor(self):
        from core.search_engine import _decide_status
        # width gain 100% >= min_width_gain_percent (10) -> review manually.
        entry = self._entry(5, local=(1000, 1000), match=(2000, 2000))
        self.assertEqual(_decide_status(entry, Settings()), MatchStatus.POOR)

    def test_enough_tags_and_only_slightly_larger_is_good(self):
        from core.search_engine import _decide_status
        # width gain 5% < 10 and no height gain -> not worth flagging. This is
        # the branch DAN-13 flagged as never-executed (the GOOD return after
        # the size comparison).
        entry = self._entry(5, local=(1000, 1000), match=(1050, 1050))
        self.assertEqual(_decide_status(entry, Settings()), MatchStatus.GOOD)
