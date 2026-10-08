"""Site parsers, tag rules, and result caching."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from core.boorus import BooruContentGoneError, BooruError, fetch_page_info
from core.boorus import pixiv, zerochan
from core.config import Settings
from core.models import ImageEntry, MatchStatus, Tag, TagSource
from core.sites import classify_site
from core.tag_rules import (apply_namespace_remap, apply_tag_blacklist,
                            is_tag_blacklisted, with_rating_tag)


AJAX = json.dumps({"error": False, "message": "", "body": {
    "illustId": "75494369", "userName": "Some Artist",
    "width": 2894, "height": 4093,
    "tags": {"tags": [{"tag": "original"}]},
    "urls": {"original": "https://i.pximg.net/img-original/o.png",
             "regular": "https://i.pximg.net/img-master/r.jpg"},
}})


class TestPixivParser(unittest.TestCase):
    """REGRESSION: Pixiv stopped embedding the meta-preload-data blob in
    artwork pages - a logged-out page returns ~93 KB of markup with no
    such element at all, so page scraping yields nothing however robustly
    it's parsed. The data now comes from /ajax/illust/{id}."""

    def test_all_url_forms_resolve_to_the_ajax_api(self):
        for src in ("https://www.pixiv.net/artworks/75494369",
                    "https://www.pixiv.net/en/artworks/75494369",
                    "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=75494369"):
            self.assertEqual(pixiv.resolve_fetch_url(src),
                             "https://www.pixiv.net/ajax/illust/75494369", src)

    def test_extracts_full_resolution_url(self):
        self.assertEqual(pixiv.parse_file_url(AJAX, "u"),
                         "https://i.pximg.net/img-original/o.png")

    def test_preview_differs_from_original(self):
        self.assertNotEqual(pixiv.parse_preview_url(AJAX, "u"), pixiv.parse_file_url(AJAX, "u"))

    def test_extracts_tags_artist_and_dimensions(self):
        tags = {(t.namespace, t.name) for t in pixiv.parse(AJAX, "u")}
        self.assertIn(("creator", "Some_Artist"), tags)   # artist is outside the tag list
        self.assertIn((None, "original"), tags)
        self.assertEqual(pixiv.parse_dimensions(AJAX, "u"), (2894, 4093))

    def test_api_error_yields_nothing_without_raising(self):
        """Pixiv returns this generic text specifically when the session
        cookie is missing or expired."""
        err = json.dumps({"error": True, "message": "An unknown error occurred", "body": []})
        self.assertEqual(pixiv.parse(err, "u"), [])
        self.assertIsNone(pixiv.parse_file_url(err, "u"))

    def test_falls_back_to_legacy_embedded_blob(self):
        """Kept in case Pixiv reverses course or serves the old blob to
        some clients."""
        legacy = {"illust": {"1": {
            "tags": {"tags": [{"tag": "original"}]}, "userName": "An Artist",
            "width": 100, "height": 200,
            "urls": {"original": "https://i.pximg.net/legacy.png"}}}}
        page = ("<html><head><meta id=\"meta-preload-data\" content='"
                + json.dumps(legacy) + "'></head></html>")
        self.assertEqual(pixiv.parse_file_url(page, "u"), "https://i.pximg.net/legacy.png")

    def test_garbage_input_is_safe(self):
        self.assertEqual(pixiv.parse("<html>nope</html>", "u"), [])


class TestPixivHeaders(unittest.TestCase):
    def test_browser_user_agent_sent_to_pixiv_only(self):
        """REGRESSION: our descriptive bot User-Agent got HTTP 403 from
        Pixiv. Scoped narrowly so every other site keeps the honest UA."""
        from core.search_engine import headers_for_url
        h = headers_for_url("https://www.pixiv.net/ajax/illust/1")
        self.assertIn("Mozilla/5.0", h["User-Agent"])
        self.assertEqual(h["Referer"], "https://www.pixiv.net/")
        self.assertIsNone(headers_for_url("https://danbooru.donmai.us/posts/1"))


class TestIqdbThumbnailUrls(unittest.TestCase):
    """REGRESSION: IQDB serves thumbnails as ROOT-relative paths
    ("/danbooru/3/5/7/hash.jpg"). Only the protocol-relative ("//host/")
    case was handled, so every IQDB thumbnail later failed with "No
    scheme supplied" - no preview image and no format/size info for any
    IQDB match."""

    def _html(self, src):
        return ('<html><head><title>iqdb</title></head><body><div id="pages">'
                '<table><tr><th>Best match</th></tr><tr>'
                f'<td class="image"><a href="https://danbooru.donmai.us/posts/1">'
                f'<img src="{src}"></a></td>'
                '<td>1200x1600 [Danbooru]</td><td>97% similarity</td>'
                '</tr></table></div></body></html>')

    def test_root_relative_thumbnail_is_made_absolute(self):
        from core.iqdb import _parse_results
        m = _parse_results(self._html("/danbooru/3/5/7/abc.jpg"))[0]
        self.assertEqual(m.thumb_url, "https://iqdb.org/danbooru/3/5/7/abc.jpg")

    def test_protocol_relative_still_works(self):
        from core.iqdb import _parse_results
        m = _parse_results(self._html("//img3.saucenao.com/x.jpg"))[0]
        self.assertEqual(m.thumb_url, "https://img3.saucenao.com/x.jpg")

    def test_absolute_thumbnail_untouched(self):
        from core.iqdb import _parse_results
        m = _parse_results(self._html("https://example.test/x.jpg"))[0]
        self.assertEqual(m.thumb_url, "https://example.test/x.jpg")


class TestZerochanParser(unittest.TestCase):
    BODY = json.dumps({
        "id": 1, "tags": ["Mangaka: An Artist", "Character: A Char", "Long Hair"],
        "width": 1200, "height": 1600,
        "full": "https://static.zerochan.net/full.jpg",
        "medium": "https://s1.zerochan.net/medium.jpg",
    })

    def test_uses_json_api(self):
        self.assertEqual(zerochan.resolve_fetch_url("https://www.zerochan.net/3793685"),
                         "https://www.zerochan.net/3793685?json")

    def test_extracts_full_resolution_original(self):
        self.assertEqual(zerochan.parse_file_url(self.BODY, "u"),
                         "https://static.zerochan.net/full.jpg")

    def test_preview_is_separate_from_original(self):
        self.assertNotEqual(zerochan.parse_preview_url(self.BODY, "u"),
                            zerochan.parse_file_url(self.BODY, "u"))

    def test_maps_namespaces_to_app_convention(self):
        tags = {(t.namespace, t.name) for t in zerochan.parse(self.BODY, "u")}
        self.assertIn(("creator", "An_Artist"), tags)   # Zerochan calls this "Mangaka"
        self.assertIn(("character", "A_Char"), tags)
        self.assertIn((None, "Long_Hair"), tags)


class TestBooruFetchSemantics(unittest.TestCase):
    def _get(self, status, text=""):
        def _g(*a, **k):
            r = MagicMock()
            r.status_code = status
            r.text = text
            return r
        return _g

    def test_404_raises_content_gone_not_generic_error(self):
        """Lets callers tell a deleted post apart from a transient
        failure, which is what dead-match dropping depends on."""
        with patch("requests.Session.get", side_effect=self._get(404)):
            with self.assertRaises(BooruContentGoneError):
                fetch_page_info("https://danbooru.donmai.us/posts/1", timeout=5.0)

    def test_503_is_a_plain_error_not_gone(self):
        with patch("requests.Session.get", side_effect=self._get(503)):
            with self.assertRaises(BooruError) as ctx:
                fetch_page_info("https://danbooru.donmai.us/posts/1", timeout=5.0)
            self.assertNotIsInstance(ctx.exception, BooruContentGoneError)

    def test_unparsed_site_reports_not_fetched(self):
        """REGRESSION: fetch_page_info returns an empty result WITHOUT
        raising for a site with no parser. Code read that as 'confirmed
        alive', silently disabling dead-link detection for those sites."""
        info = fetch_page_info("https://example.test/thing", timeout=5.0)
        self.assertFalse(info.fetched)


class TestSiteClassification(unittest.TestCase):
    def test_deviantart_recognised(self):
        self.assertEqual(classify_site(None, "https://deviantart.com/view/849957290"),
                         "DeviantArt")

    def test_mangadex_recognised_and_filterable(self):
        """Classification-only: a MangaDex URL is a chapter (many pages),
        not a single image, so it has no parser. Listing it exists purely
        so matches can be unchecked in the Sites menu."""
        from core.sites import ALL_SITE_OPTIONS
        url = "https://mangadex.org/chapter/ccec5d80-f677-4ee4-9ebb-a223039eccf7/"
        self.assertEqual(classify_site(None, url), "MangaDex")
        self.assertIn("MangaDex", ALL_SITE_OPTIONS)

    def test_mangadex_has_no_content_parser(self):
        """Nothing should ever fetch or scrape MangaDex."""
        from core.boorus import find_parser
        self.assertIsNone(find_parser("https://mangadex.org/chapter/abc/"))

    def test_gallery_and_auth_gated_sites_have_no_parser(self):
        """e-Hentai URLs are galleries (many pages), not one image. These
        are classification-only on purpose - this pins that so the absence
        of a parser reads as deliberate, not forgotten.

        Sankaku and Twitter are deliberately NOT in this list any more:
        Sankaku is looked up by file hash against its modern API, and
        tweets through FxTwitter's public one."""
        from core.boorus import find_parser
        from core.sites import ALL_SITE_OPTIONS
        for url, site in (
            ("https://e-hentai.org/g/1234567/abcdef0123/", "e-Hentai"),
            ("https://anilist.co/anime/21", "AniList"),
        ):
            with self.subTest(site=site):
                self.assertEqual(classify_site(None, url), site)
                self.assertIn(site, ALL_SITE_OPTIONS)
                self.assertIsNone(find_parser(url))


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

ESHUUSHUU_URL = "https://e-shuushuu.net/images/331875"


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
DANBOORU_URL = "https://danbooru.donmai.us/posts/5105386"


class TestDanbooruLegacyUrls(unittest.TestCase):
    """SauceNAO's index still returns Danbooru's legacy /post/show/{id}
    URLs for older entries."""

    def test_legacy_url_reaches_the_json_api(self):
        """REGRESSION: the post-ID pattern matched only the modern
        /posts/{id}, so legacy URLs silently fell back to HTML scraping -
        which yields no file URL, no preview, no dimensions, and (because
        the restriction check reads the JSON record) no Gold-account
        detection. Gold-only posts therefore arrived looking like
        ordinary matches carrying a search-engine thumbnail."""
        from core.boorus import danbooru
        self.assertEqual(
            danbooru.resolve_fetch_url("https://danbooru.donmai.us/post/show/2577264"),
            "https://danbooru.donmai.us/posts/2577264.json")

    def test_modern_url_still_works(self):
        from core.boorus import danbooru
        self.assertEqual(
            danbooru.resolve_fetch_url("https://danbooru.donmai.us/posts/5105386?q=x"),
            "https://danbooru.donmai.us/posts/5105386.json")

    def test_restriction_is_detected_for_a_legacy_url(self):
        from core.boorus import danbooru
        body = json.dumps({"id": 2577264, "is_banned": False, "tag_string_general": "1girl"})
        self.assertIsNotNone(
            danbooru.parse_restriction(body, "https://danbooru.donmai.us/post/show/2577264"))

    def test_non_post_urls_are_left_alone(self):
        from core.boorus import danbooru
        url = "https://danbooru.donmai.us/pools/123"
        self.assertEqual(danbooru.resolve_fetch_url(url), url)


class TestMoebooruDimensions(unittest.TestCase):
    """Yande.re and Konachan. Dimensions come from the page already
    fetched for tags, so this costs no extra request - which matters
    because their API refuses anonymous requests from some networks."""

    URL = "https://yande.re/post/show/123456"

    def test_post_register_record(self):
        from core.boorus import moebooru
        html = ('<script>Post.register({"id":123,"width":3500,"height":5000,'
                '"sample_width":1500,"sample_height":2142})</script>')
        self.assertEqual(moebooru.parse_dimensions(html, self.URL), (3500, 5000))

    def test_never_reports_the_sample_size(self):
        """REGRESSION GUARD: Moebooru displays a downscaled sample, and
        the page carries sample_width/actual_preview_width alongside the
        real ones. Reporting those would make a genuinely larger match
        look like a downgrade - the exact opposite of what the size
        comparison exists to tell you."""
        from core.boorus import moebooru
        html = ('<script>Post.register({"id":1,"sample_width":1500,"sample_height":2142,'
                '"actual_preview_width":300,"actual_preview_height":428})</script>'
                '<img id="image" width="1500" height="2142" src="sample.jpg">')
        self.assertEqual(moebooru.parse_dimensions(html, self.URL), (None, None))

    def test_size_label_fallback(self):
        from core.boorus import moebooru
        html = '<div><ul id="stats"><li>Size: 1500x2000</li></ul></div>'
        self.assertEqual(moebooru.parse_dimensions(html, self.URL), (1500, 2000))

    def test_size_label_with_multiplication_sign(self):
        from core.boorus import moebooru
        html = '<div><ul id="stats"><li>Size: 1500\u00d72000</li></ul></div>'
        self.assertEqual(moebooru.parse_dimensions(html, self.URL), (1500, 2000))

    def test_highres_link_text_fallback(self):
        from core.boorus import moebooru
        html = '<a id="highres" href="x.png">PNG (1500x2000, 2.5 MB)</a>'
        self.assertEqual(moebooru.parse_dimensions(html, self.URL), (1500, 2000))

    def test_nothing_available_is_reported_not_guessed(self):
        from core.boorus import moebooru
        with self.assertLogs("hatate.boorus.moebooru", level="DEBUG"):
            self.assertEqual(
                moebooru.parse_dimensions("<html><body>nothing</body></html>", self.URL),
                (None, None))


class TestSharedSizeParsing(unittest.TestCase):
    """The separator between the two numbers is a property of the shared
    Danbooru 1.x lineage, not of any one site - so it's parsed in one
    place rather than fixed repeatedly and forgotten somewhere."""

    def test_accepts_every_separator_these_sites_use(self):
        from core.boorus._sizes import parse_size_label
        for separator in ("x", "X", "\u00d7", "\u2715", "*"):
            with self.subTest(separator=separator):
                self.assertEqual(
                    parse_size_label("Size: 1232%s918" % separator), (1232, 918))

    def test_tolerates_spacing_and_thousands_separators(self):
        from core.boorus._sizes import parse_size_label
        self.assertEqual(parse_size_label("Size:  1,232 \u00d7 918 "), (1232, 918))

    def test_bare_dimensions_need_no_label(self):
        from core.boorus._sizes import parse_bare_dimensions
        self.assertEqual(parse_bare_dimensions("PNG (1500x2000, 2.5 MB)"), (1500, 2000))

    def test_missing_label_is_described_for_the_log(self):
        from core.boorus._sizes import describe_size_context
        self.assertIn("no 'Size:'", describe_size_context("Id: 1 Rating: Safe"))
        self.assertIn("1232 by 918", describe_size_context("Size: 1232 by 918 Rating: Safe"))


class TestGelbooruDimensions(unittest.TestCase):
    """Gelbooru's Data API needs credentials this build doesn't have, so
    dimensions have to come from the post page's Statistics sidebar
    ("Size: 1232x918"). Without them the size comparison silently shows
    nothing, which looks like a missing feature rather than a parse
    failure."""

    URL = "https://gelbooru.com/index.php?page=post&s=view&id=3484690"

    def setUp(self):
        from core.boorus import gelbooru
        # Keep the API out of it - this is about the HTML path.
        gelbooru._api_unauthorized = True
        gelbooru._last_api_post = None

    def tearDown(self):
        from core.boorus import gelbooru
        gelbooru._api_unauthorized = False
        gelbooru._last_api_post = None

    def test_ascii_x_separator(self):
        from core.boorus import gelbooru
        self.assertEqual(
            gelbooru.parse_dimensions("<li>Size: 1232x918</li>", self.URL), (1232, 918))

    def test_multiplication_sign_separator(self):
        """REGRESSION GUARD: the separator renders as ASCII "x" in some
        views and as &times; (U+00D7) in others. Matching only "x" gives
        no dimensions at all, with nothing to indicate why."""
        from core.boorus import gelbooru
        self.assertEqual(
            gelbooru.parse_dimensions("<li>Size: 1232\u00d7918</li>", self.URL), (1232, 918))

    def test_numbers_split_across_elements(self):
        from core.boorus import gelbooru
        html = "<li>Size: <span>1232</span>x<span>918</span></li>"
        self.assertEqual(gelbooru.parse_dimensions(html, self.URL), (1232, 918))

    def test_no_space_after_the_label(self):
        from core.boorus import gelbooru
        self.assertEqual(
            gelbooru.parse_dimensions("<li>Size:1232x918</li>", self.URL), (1232, 918))

    def test_html_entity_form(self):
        from core.boorus import gelbooru
        self.assertEqual(
            gelbooru.parse_dimensions("<li>Size: 1232 &times; 918</li>", self.URL), (1232, 918))

    def test_miss_logs_the_surrounding_text(self):
        """A miss must say what it actually saw around "Size:" - guessing
        the markup from outside has already produced one wrong fix."""
        from core.boorus import gelbooru
        with self.assertLogs("hatate.boorus.gelbooru", level="DEBUG") as captured:
            self.assertEqual(
                gelbooru.parse_dimensions("<li>Size: 1232 by 918</li>", self.URL), (None, None))
        self.assertIn("1232 by 918", "\n".join(captured.output))

    def test_absent_size_is_reported_as_such(self):
        from core.boorus import gelbooru
        with self.assertLogs("hatate.boorus.gelbooru", level="DEBUG") as captured:
            self.assertEqual(gelbooru.parse_dimensions("<li>Id: 1</li>", self.URL), (None, None))
        self.assertIn("no 'Size:'", "\n".join(captured.output))


class TestGelbooruApiRefusal(unittest.TestCase):
    """Gelbooru's Data API now needs an api_key/user_id and answers 401
    without them. Retrying per match costs a guaranteed-failing round
    trip before every HTML fallback."""

    def setUp(self):
        from core.boorus import gelbooru
        gelbooru._api_unauthorized = False
        gelbooru._last_api_post = None

    def tearDown(self):
        from core.boorus import gelbooru
        gelbooru._api_unauthorized = False
        gelbooru._last_api_post = None

    def test_api_is_not_retried_after_a_401(self):
        from unittest.mock import MagicMock, patch
        from core.boorus import gelbooru
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            resp = MagicMock()
            resp.status_code = 401
            resp.json.return_value = {}
            return resp

        html = '<html><a href="https://img4.gelbooru.com/images/a/b/x.gif">Original image</a></html>'
        with patch("requests.Session.get", side_effect=fake_get):
            for post_id in range(5):
                gelbooru._last_api_post = None
                gelbooru.parse_file_url(
                    html, "https://gelbooru.com/index.php?page=post&s=view&id=%d" % post_id)
        self.assertEqual(len(calls), 1)

    def test_html_fallback_still_yields_a_file_url(self):
        """Giving up on the API must not mean giving up on the post."""
        from unittest.mock import MagicMock, patch
        from core.boorus import gelbooru
        html = '<html><a href="https://img4.gelbooru.com/images/a/b/x.gif">Original image</a></html>'
        resp = MagicMock()
        resp.status_code = 401
        resp.json.return_value = {}
        with patch("requests.Session.get", return_value=resp):
            got = gelbooru.parse_file_url(
                html, "https://gelbooru.com/index.php?page=post&s=view&id=1")
        self.assertEqual(got, "https://img4.gelbooru.com/images/a/b/x.gif")


class TestDanbooruJsonApi(unittest.TestCase):
    """Danbooru is read through its JSON API because the API names the
    full-resolution file and the downscaled sample as separate fields.
    Scraping conflated them, which made the side-by-side comparison show
    a sample while claiming to show the original."""

    def test_fetch_url_points_at_the_json_record(self):
        from core.boorus import danbooru
        self.assertEqual(
            danbooru.resolve_fetch_url("https://danbooru.donmai.us/posts/5105386?q=x"),
            "https://danbooru.donmai.us/posts/5105386.json")

    def test_unrecognised_url_is_left_alone(self):
        from core.boorus import danbooru
        url = "https://danbooru.donmai.us/pools/123"
        self.assertEqual(danbooru.resolve_fetch_url(url), url)

    def test_file_url_is_the_original_not_the_sample(self):
        from core.boorus import danbooru
        full = danbooru.parse_file_url(DANBOORU_POST_JSON, DANBOORU_URL)
        preview = danbooru.parse_preview_url(DANBOORU_POST_JSON, DANBOORU_URL)
        self.assertIn("/original/", full)
        self.assertIn("sample", preview)
        self.assertNotEqual(full, preview,
                            "the comparison view depends on these being different files")

    def test_dimensions_are_the_originals(self):
        from core.boorus import danbooru
        self.assertEqual(danbooru.parse_dimensions(DANBOORU_POST_JSON, DANBOORU_URL),
                         (2894, 4093))

    def test_tags_come_back_categorised(self):
        from core.boorus import danbooru
        tags = {t.display for t in danbooru.parse(DANBOORU_POST_JSON, DANBOORU_URL)}
        self.assertIn("artist:someone", tags)
        self.assertIn("character:hakurei_reimu", tags)
        self.assertIn("copyright:touhou", tags)
        self.assertIn("meta:highres", tags)
        self.assertIn("general:1girl", tags)

    def test_restricted_post_falls_back_to_the_sample(self):
        """Danbooru withholds file_url for some posts. The sample is then
        genuinely the best available, so it's used - the point is that
        it's used knowingly rather than by accident."""
        from core.boorus import danbooru
        body = json.dumps({"id": 1, "large_file_url": "https://cdn.donmai.us/sample/x.jpg"})
        self.assertTrue(danbooru.parse_file_url(body, DANBOORU_URL).endswith("sample/x.jpg"))

    def test_html_fallback_never_passes_the_sample_off_as_the_original(self):
        """REGRESSION GUARD: the old scraper returned the on-page <img>
        src when data-file-url was absent. That element is the sample, so
        "full resolution" silently became the same downscaled image as
        the preview - and the comparison view compared a local original
        against a sample of itself."""
        from core.boorus import danbooru
        html = ('<section id="image-container">'
                '<img id="image" src="https://cdn.donmai.us/sample/s.jpg"></section>')
        self.assertIsNone(danbooru.parse_file_url(html, DANBOORU_URL))

    def test_html_fallback_still_uses_a_real_data_file_url(self):
        from core.boorus import danbooru
        html = ('<section id="image-container" '
                'data-file-url="https://cdn.donmai.us/original/o.jpg"></section>')
        self.assertTrue(danbooru.parse_file_url(html, DANBOORU_URL).endswith("original/o.jpg"))

    def test_html_fallback_still_parses_tags(self):
        from core.boorus import danbooru
        html = ('<section id="tag-list"><li class="category-1">'
                '<a class="search-tag">some artist</a></li></section>')
        tags = danbooru.parse(html, DANBOORU_URL)
        self.assertEqual([t.display for t in tags], ["artist:some_artist"])

    def test_error_payload_is_not_mistaken_for_a_post(self):
        from core.boorus import danbooru
        body = json.dumps({"success": False, "message": "not found"})
        self.assertIsNone(danbooru.parse_file_url(body, DANBOORU_URL))
        self.assertEqual(danbooru.parse(body, DANBOORU_URL), [])


class TestDanbooruRestrictedPosts(unittest.TestCase):
    """Danbooru keeps some posts listed but Gold-only: posts BANNED at an
    artist's request, and posts with CENSORED tags. Both come back from
    a search looking like ordinary matches, but their image can never be
    fetched - so there's nothing to preview, compare, download or send."""

    URL = "https://danbooru.donmai.us/posts/123"

    def test_normal_post_is_not_restricted(self):
        from core.boorus import danbooru
        body = json.dumps({
            "id": 123, "is_banned": False,
            "file_url": "https://cdn.donmai.us/original/o.jpg",
            "large_file_url": "https://cdn.donmai.us/sample/s.jpg",
        })
        self.assertIsNone(danbooru.parse_restriction(body, self.URL))

    def test_banned_post_is_detected(self):
        from core.boorus import danbooru
        body = json.dumps({"id": 123, "is_banned": True})
        self.assertIn("banned", danbooru.parse_restriction(body, self.URL))

    def test_banned_flag_wins_even_if_a_file_url_is_present(self):
        """is_banned is the site stating the rule outright; a file URL
        that happens to be there doesn't overrule it."""
        from core.boorus import danbooru
        body = json.dumps({"id": 123, "is_banned": True,
                           "file_url": "https://cdn.donmai.us/original/o.jpg"})
        self.assertIsNotNone(danbooru.parse_restriction(body, self.URL))

    def test_withheld_file_is_detected_without_a_named_flag(self):
        """Censored-tag posts aren't flagged by name - the record simply
        comes back with no file. Testing for the missing file catches
        whatever restriction Danbooru adds next, too."""
        from core.boorus import danbooru
        body = json.dumps({"id": 123, "is_banned": False, "tag_string_general": "1girl"})
        self.assertIn("Gold", danbooru.parse_restriction(body, self.URL))

    def test_tags_are_still_parsed_from_a_restricted_post(self):
        """The image is unavailable; the tags aren't. Someone who turns
        the drop off should still get them."""
        from core.boorus import danbooru
        body = json.dumps({"id": 123, "is_banned": True,
                           "tag_string_general": "1girl solo",
                           "tag_string_artist": "someone"})
        tags = {t.display for t in danbooru.parse(body, self.URL)}
        self.assertIn("artist:someone", tags)

    def test_html_fallback_claims_no_restriction(self):
        """Without the JSON there's no way to tell, and guessing would
        drop good matches."""
        from core.boorus import danbooru
        self.assertIsNone(danbooru.parse_restriction("<html><body>x</body></html>", self.URL))

    def test_toggle_controls_whether_they_are_dropped(self):
        import os
        import tempfile
        from unittest.mock import MagicMock, patch
        from PIL import Image
        from core.config import Settings
        from core.models import ImageEntry
        import core.search_engine as se

        path = os.path.join(tempfile.mkdtemp(), "x.jpg")
        Image.new("RGB", (64, 64), "red").save(path)

        gold_only = json.dumps({"id": 111, "is_banned": True, "tag_string_general": "1girl"})
        normal = json.dumps({
            "id": 222, "is_banned": False, "tag_string_general": "1girl",
            "file_url": "https://cdn.donmai.us/original/ok.jpg",
            "large_file_url": "https://cdn.donmai.us/sample/ok.jpg",
        })
        response = {
            "header": {"status": 0, "long_remaining": 100, "long_limit": 100},
            "results": [{
                "header": {"similarity": "95", "index_name": "danbooru",
                           "thumbnail": "https://img1.saucenao.com/t.jpg"},
                "data": {"ext_urls": ["https://danbooru.donmai.us/posts/111",
                                      "https://danbooru.donmai.us/posts/222"]},
            }],
        }

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.close = MagicMock()
            resp.raw.read.return_value = b""
            resp.url = url
            resp.text = gold_only if "111" in url else normal
            resp.content = b"x"
            return resp

        def run(drop):
            settings = Settings()
            settings.primary_engine = "saucenao"
            settings.secondary_engine_mode = "disabled"
            settings.enable_ascii2d = False
            settings.enable_tracemoe = False
            settings.enable_iqdb3d = False
            settings.retrieve_tags_from_booru = True
            settings.drop_restricted_matches = drop
            settings.saucenao.api_key = "k"
            settings.saucenao.use_json_api = True
            settings.saucenao.min_similarity = 50
            entry = ImageEntry(path=path)
            post = MagicMock()
            post.status_code = 200
            post.json.return_value = response
            with patch("requests.Session.post", return_value=post), \
                 patch("requests.Session.get", side_effect=fake_get), \
                 patch.object(se, "_load_local_dimensions", lambda e: None), \
                 patch.object(se.remote, "download_bytes", return_value=None), \
                 patch.object(se.remote, "fetch_remote_info", return_value=(None, None)):
                se.search_image(entry, settings, use_cache=False)
            return entry

        dropped = run(True)
        self.assertEqual(len(dropped.candidates), 1)
        self.assertTrue(dropped.matched_url.endswith("222"))

        kept = run(False)
        self.assertEqual(len(kept.candidates), 2)
        restricted = [c for c in kept.candidates if c.restricted]
        self.assertEqual(len(restricted), 1)
        self.assertTrue(restricted[0].booru_tags,
                        "tags stay readable even when the image doesn't")


class TestEshuushuuParser(unittest.TestCase):
    """The fixture reproduces the route shapes served by the live page
    for image #331875. The expected tag set below was cross-checked
    against e-shuushuu's own Atom feed for the same image, so it is not
    merely asserting that the parser agrees with itself."""

    def test_extracts_the_real_tag_set(self):
        from core.boorus import eshuushuu
        names = [t.name for t in eshuushuu.parse(ESHUUSHUU_POST_HTML, ESHUUSHUU_URL)]
        self.assertEqual(names, ["Sumomo_KPA", "Cabal_Online", "black_hair", "gloves",
                                 "long_hair", "smile", "tree", "yellow_eyes"])

    def test_tag_page_jump_links_are_not_treated_as_tags(self):
        """Each tag is followed by a /tags/{id} link rendered as a small
        arrow. Selecting it too would add a junk tag per real tag."""
        from core.boorus import eshuushuu
        names = [t.name for t in eshuushuu.parse(ESHUUSHUU_POST_HTML, ESHUUSHUU_URL)]
        self.assertEqual(len(names), 8)
        self.assertFalse([n for n in names if n.strip("\u203a> ").isdigit()])

    def test_namespaces_match_the_atom_feeds_classification(self):
        """Ground truth for image #331875 comes from e-shuushuu\'s own
        Atom feed (Artist=Sumomo KPA, Source=Cabal Online, and six
        Themes), so this checks the description-derived namespaces
        against an independent source rather than against itself."""
        from core.boorus import eshuushuu
        got = {t.name: t.namespace for t in eshuushuu.parse(ESHUUSHUU_POST_HTML, ESHUUSHUU_URL)}
        self.assertEqual(got["Sumomo_KPA"], "creator")
        self.assertEqual(got["Cabal_Online"], "series")
        for theme in ("black_hair", "gloves", "long_hair", "smile", "tree", "yellow_eyes"):
            self.assertIsNone(got[theme], theme)

    def test_character_role_is_distinguished_from_source(self):
        """og:description uses "of" for a character and "from" for a
        source; conflating them would file characters under series:."""
        from core.boorus import eshuushuu
        html = ESHUUSHUU_POST_HTML.replace(
            "by Sumomo KPA from Cabal Online.", "by Sumomo KPA of Cabal Online.")
        got = {t.name: t.namespace for t in eshuushuu.parse(html, ESHUUSHUU_URL)}
        self.assertEqual(got["Cabal_Online"], "character")

    def test_unrecognised_description_yields_flat_tags_not_wrong_ones(self):
        """REGRESSION GUARD: if e-shuushuu changes its description
        wording, tags must degrade to unnamespaced rather than being
        mislabelled - a character filed under creator: in Hydrus is far
        more annoying to undo than a flat tag."""
        from core.boorus import eshuushuu
        html = ESHUUSHUU_POST_HTML.replace(
            "Cute anime artwork by Sumomo KPA from Cabal Online. 800&times;600. "
            "Tagged: black hair, gloves, long hair, smile, tree, yellow eyes.",
            "An entirely different sentence shape.")
        tags = eshuushuu.parse(html, ESHUUSHUU_URL)
        self.assertEqual(len(tags), 8)
        self.assertTrue(all(t.namespace is None for t in tags))

    def test_artist_name_containing_a_preposition(self):
        """An artist called "Fagi of Note" must not be truncated at the
        "of" - the full known tag name is matched, not parsed out."""
        from core.boorus import eshuushuu
        html = ('<html><head><meta property="og:description" content="Cute anime artwork '
                'by Fagi of Note from Touhou. 1&times;1. Tagged: smile."></head><body>'
                '<a href="/search?tags=1">Fagi of Note</a>'
                '<a href="/search?tags=2">Touhou</a>'
                '<a href="/search?tags=3">smile</a></body></html>')
        got = {t.name: t.namespace for t in eshuushuu.parse(html, ESHUUSHUU_URL)}
        self.assertEqual(got["Fagi_of_Note"], "creator")
        self.assertEqual(got["Touhou"], "series")

    def test_short_name_does_not_claim_a_longer_names_role(self):
        """"by Fagimoto" must not make a separate "Fagi" tag a creator."""
        from core.boorus import eshuushuu
        html = ('<html><head><meta property="og:description" content="Cute anime artwork '
                'by Fagimoto. 1&times;1. Tagged: hat."></head><body>'
                '<a href="/search?tags=1">Fagi</a>'
                '<a href="/search?tags=2">Fagimoto</a>'
                '<a href="/search?tags=3">hat</a></body></html>')
        got = {t.name: t.namespace for t in eshuushuu.parse(html, ESHUUSHUU_URL)}
        self.assertEqual(got["Fagimoto"], "creator")
        self.assertIsNone(got["Fagi"])

    def test_regex_metacharacters_in_tag_names(self):
        """Real e-shuushuu tags include ":3" and "^_^"."""
        from core.boorus import eshuushuu
        html = ('<html><head><meta property="og:description" content="Cute anime artwork '
                'by Someone. 1&times;1. Tagged: :3, ^_^."></head><body>'
                '<a href="/search?tags=1">:3</a><a href="/search?tags=2">^_^</a></body></html>')
        tags = eshuushuu.parse(html, ESHUUSHUU_URL)
        self.assertEqual([t.name for t in tags], [":3", "^_^"])
        self.assertTrue(all(t.namespace is None for t in tags))

    def test_file_url_is_the_fullsize_file_not_the_thumbnail(self):
        from core.boorus import eshuushuu
        got = eshuushuu.parse_file_url(ESHUUSHUU_POST_HTML, ESHUUSHUU_URL)
        self.assertEqual(got, "https://cdn.e-shuushuu.net/fullsize/2010-10-07-331875.jpeg")
        self.assertNotIn("/thumbs/", got)

    def test_preview_url_is_the_thumbnail(self):
        from core.boorus import eshuushuu
        got = eshuushuu.parse_preview_url(ESHUUSHUU_POST_HTML, ESHUUSHUU_URL)
        self.assertIn("/thumbs/", got)

    def test_dimensions(self):
        from core.boorus import eshuushuu
        self.assertEqual(eshuushuu.parse_dimensions(ESHUUSHUU_POST_HTML, ESHUUSHUU_URL), (800, 600))

    def test_protocol_relative_urls_are_normalized(self):
        """A //cdn... href would be handed downstream as-is and fail to
        fetch."""
        from core.boorus import eshuushuu
        html = '<a href="//cdn.e-shuushuu.net/fullsize/x.jpg">f</a>'
        self.assertEqual(eshuushuu.parse_file_url(html, ESHUUSHUU_URL),
                         "https://cdn.e-shuushuu.net/fullsize/x.jpg")

    def test_parser_is_registered_for_both_url_forms(self):
        """Post pages are /images/{id} now, but the older /image/{id}
        form still turns up in search-engine results."""
        from core.boorus import find_parser
        from core.boorus import eshuushuu
        self.assertIs(find_parser("https://e-shuushuu.net/images/331875"), eshuushuu)
        self.assertIs(find_parser("https://e-shuushuu.net/image/331875"), eshuushuu)

    def test_empty_page_returns_no_tags_without_raising(self):
        from core.boorus import eshuushuu
        self.assertEqual(eshuushuu.parse("<html><body></body></html>", ESHUUSHUU_URL), [])
        self.assertIsNone(eshuushuu.parse_file_url("<html></html>", ESHUUSHUU_URL))


class TestTagNamespaceRemap(unittest.TestCase):
    def _settings(self, remap):
        s = Settings()
        s.enable_tag_namespace_remap = True
        s.tag_namespace_remap = remap
        return s

    def test_renames_namespace(self):
        out = apply_namespace_remap(
            [Tag("someone", TagSource.BOORU, "artist")], self._settings({"artist": "creator"}))
        self.assertEqual((out[0].namespace, out[0].name), ("creator", "someone"))

    def test_empty_target_strips_namespace_entirely(self):
        """'general:' with a blank target means drop the namespace, so
        general:bikini_top becomes plain bikini_top."""
        out = apply_namespace_remap(
            [Tag("bikini_top", TagSource.BOORU, "general")], self._settings({"general": ""}))
        self.assertIsNone(out[0].namespace)
        self.assertEqual(out[0].display, "bikini_top")

    def test_stripped_namespace_is_none_not_empty_string(self):
        """Must match how 'no namespace' is represented everywhere else,
        or deduplication silently breaks."""
        out = apply_namespace_remap(
            [Tag("x", TagSource.BOORU, "general")], self._settings({"general": ""}))
        self.assertIs(out[0].namespace, None)

    def test_disabled_is_a_true_noop(self):
        tags = [Tag("someone", TagSource.BOORU, "artist")]
        s = Settings()
        s.enable_tag_namespace_remap = False
        self.assertIs(apply_namespace_remap(tags, s), tags)

    def test_original_tags_never_mutated(self):
        original = Tag("someone", TagSource.BOORU, "artist")
        apply_namespace_remap([original], self._settings({"artist": "creator"}))
        self.assertEqual(original.namespace, "artist")


class TestInlineTagEdit(unittest.TestCase):
    """Editing a tag in the list. This lived inside a GUI slot, so none of
    it was reachable without a window - including the rule that a rename
    colliding with an existing tag drops the older one rather than leaving
    the entry with two identical tags."""

    def _edit(self, tags, target, text, settings=None):
        from core.tag_rules import apply_inline_edit
        return apply_inline_edit(tags, target, text, settings or Settings())

    def test_clearing_the_text_deletes_the_tag(self):
        a, b = Tag("keep", TagSource.USER), Tag("gone", TagSource.USER)
        result = self._edit([a, b], b, "")
        self.assertEqual(result.action, "removed")
        self.assertEqual(result.tags, [a])

    def test_whitespace_only_counts_as_cleared(self):
        a = Tag("gone", TagSource.USER)
        self.assertEqual(self._edit([a], a, "   ").action, "removed")

    def test_a_rename_keeps_the_original_source(self):
        """The tag came from a booru; renaming it does not make it a tag
        the user typed."""
        tag = Tag("someone", TagSource.BOORU, "artist")
        self._edit([tag], tag, "artist:somebody")
        self.assertEqual(tag.source, TagSource.BOORU)
        self.assertEqual(tag.display, "artist:somebody")

    def test_a_bare_name_clears_the_namespace(self):
        tag = Tag("someone", TagSource.BOORU, "artist")
        self._edit([tag], tag, "someone")
        self.assertIsNone(tag.namespace)

    def test_only_the_first_colon_splits(self):
        """A name containing a colon must survive intact."""
        from core.tag_rules import split_tag_text
        self.assertEqual(split_tag_text("series:Re:Zero"), ("series", "Re:Zero"))

    def test_an_empty_namespace_side_is_no_namespace(self):
        from core.tag_rules import split_tag_text
        self.assertEqual(split_tag_text(":lonely"), (None, "lonely"))

    def test_the_remap_rules_apply_to_hand_typed_edits(self):
        """Typing artist:someone becomes creator:someone when that rule is
        on, same as a freshly-searched tag would."""
        s = Settings()
        s.enable_tag_namespace_remap = True
        s.tag_namespace_remap = {"artist": "creator"}
        tag = Tag("x", TagSource.BOORU, "general")
        self._edit([tag], tag, "artist:someone", s)
        self.assertEqual(tag.display, "creator:someone")

    def test_renaming_onto_an_existing_tag_drops_the_older_one(self):
        older = Tag("cat", TagSource.BOORU)
        editing = Tag("dog", TagSource.USER)
        result = self._edit([older, editing], editing, "cat")
        self.assertTrue(result.dropped_duplicate)
        self.assertEqual(result.tags, [editing],
                         "the edited tag survives, the older duplicate goes")

    def test_an_ordinary_rename_drops_nothing(self):
        a, b = Tag("cat", TagSource.USER), Tag("dog", TagSource.USER)
        result = self._edit([a, b], b, "wolf")
        self.assertFalse(result.dropped_duplicate)
        self.assertEqual(len(result.tags), 2)

    def test_the_edited_tag_object_is_kept_not_replaced(self):
        """The widget row holds a reference to this exact object; swapping
        in a new one would leave the row pointing at a tag that is no
        longer in the list."""
        tag = Tag("before", TagSource.USER)
        result = self._edit([tag], tag, "after")
        self.assertIs(result.tags[0], tag)


class TestTagBlacklist(unittest.TestCase):
    def _settings(self, patterns):
        s = Settings()
        s.enable_tag_blacklist = True
        s.tag_blacklist = patterns
        return s

    def _booru(self, name, namespace=None):
        return Tag(name, TagSource.BOORU, namespace)

    def test_exact_name_is_dropped(self):
        out = apply_tag_blacklist(
            [self._booru("highres"), self._booru("1girl")], self._settings(["highres"]))
        self.assertEqual([t.name for t in out], ["1girl"])

    def test_bare_pattern_matches_the_name_in_any_namespace(self):
        """A user writing "highres" means the tag, not one particular
        namespaced spelling of it - so it must hit meta:highres too."""
        out = apply_tag_blacklist(
            [self._booru("highres", "meta"), self._booru("1girl")], self._settings(["highres"]))
        self.assertEqual([t.display for t in out], ["1girl"])

    def test_namespaced_pattern_does_not_match_the_unnamespaced_tag(self):
        """The inverse must NOT hold: "meta:highres" is the user being
        specific, so a bare highres tag has to survive it."""
        out = apply_tag_blacklist(
            [self._booru("highres")], self._settings(["meta:highres"]))
        self.assertEqual([t.name for t in out], ["highres"])

    def test_namespace_wildcard_blocks_whole_namespace(self):
        out = apply_tag_blacklist(
            [self._booru("highres", "meta"), self._booru("someone", "artist")],
            self._settings(["meta:*"]))
        self.assertEqual([t.display for t in out], ["artist:someone"])

    def test_namespace_wildcard_leaves_unnamespaced_tags_alone(self):
        """An unnamespaced tag's display form has no colon, so it can
        never match a colon pattern - "meta:*" must not become "block
        everything"."""
        out = apply_tag_blacklist(
            [self._booru("1girl"), self._booru("highres", "meta")], self._settings(["meta:*"]))
        self.assertEqual([t.display for t in out], ["1girl"])

    def test_wildcard_on_names(self):
        out = apply_tag_blacklist(
            [self._booru("bad_id"), self._booru("bad_pixiv_id"), self._booru("badge")],
            self._settings(["bad_*"]))
        self.assertEqual([t.name for t in out], ["badge"])

    def test_underscores_and_spaces_are_equivalent(self):
        """Boorus write bad_id, Hydrus displays "bad id". A pattern
        copied out of either has to match a tag from the other, or it
        silently never fires."""
        self.assertTrue(is_tag_blacklisted(self._booru("bad_id"), ["bad id"]))
        self.assertTrue(is_tag_blacklisted(self._booru("bad id"), ["bad_id"]))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(is_tag_blacklisted(self._booru("HighRes"), ["highres"]))

    def test_blank_lines_are_ignored(self):
        """An empty pattern must not normalize into something that
        matches every tag."""
        out = apply_tag_blacklist(
            [self._booru("1girl")], self._settings(["", "   ", "\n"]))
        self.assertEqual([t.name for t in out], ["1girl"])

    def test_disabled_is_a_true_noop(self):
        tags = [self._booru("highres")]
        s = Settings()
        s.enable_tag_blacklist = False
        s.tag_blacklist = ["highres"]
        self.assertIs(apply_tag_blacklist(tags, s), tags)

    def test_empty_blacklist_is_a_true_noop(self):
        tags = [self._booru("highres")]
        s = Settings()
        s.enable_tag_blacklist = True
        s.tag_blacklist = []
        self.assertIs(apply_tag_blacklist(tags, s), tags)

    def test_surviving_tag_objects_are_not_copied_or_mutated(self):
        kept = self._booru("1girl")
        out = apply_tag_blacklist([self._booru("highres"), kept], self._settings(["highres"]))
        self.assertIs(out[0], kept)

    def test_default_blacklist_spares_ordinary_content_tags(self):
        """The shipped starter list is meant to be safe to switch on, so
        no entry in it may cost a tag describing image content."""
        from core.config import DEFAULT_TAG_BLACKLIST
        s = self._settings(list(DEFAULT_TAG_BLACKLIST))
        content_tags = [
            "1girl", "solo", "long_hair", "blue_eyes", "school_uniform",
            "sitting", "outdoors", "cherry_blossoms", "smile", "highres_monitor",
            "translated_text_focus", "duplicate_layers",
        ]
        survived = [t.name for t in apply_tag_blacklist(
            [self._booru(n) for n in content_tags], s)]
        self.assertEqual(survived, content_tags)

    def test_default_blacklist_catches_what_it_is_for(self):
        from core.config import DEFAULT_TAG_BLACKLIST
        s = self._settings(list(DEFAULT_TAG_BLACKLIST))
        junk = ["highres", "absurdres", "tagme", "bad_id", "commentary_request", "translated"]
        self.assertEqual(apply_tag_blacklist([self._booru(n) for n in junk], s), [])


class TestRatingParsing(unittest.TestCase):
    """Reading the post's rating from what the site actually states.

    The trap this guards is the shared-letter one: Danbooru's "s" is
    SENSITIVE (it split the old safe rating into general/sensitive)
    while e621's "s" is SAFE. One shared table would put the wrong word
    into somebody's library and look like fact doing it.
    """

    DANBOORU_URL = "https://danbooru.donmai.us/posts/5105386"
    E621_URL = "https://e621.net/posts/219935"
    GELBOORU_URL = "https://gelbooru.com/index.php?page=post&s=view&id=123"

    def _danbooru(self, **extra):
        from core.boorus import danbooru
        body = json.dumps({"id": 5105386, "tag_string_general": "1girl", **extra})
        return danbooru.parse_rating(body, self.DANBOORU_URL)

    def _e621(self, **extra):
        from core.boorus import e621
        body = json.dumps({"post": {"id": 219935, "tags": {"general": ["1boy"]}, **extra}})
        return e621.parse_rating(body, self.E621_URL)

    def test_danbooru_s_is_sensitive_and_e621_s_is_safe(self):
        self.assertEqual(self._danbooru(rating="s"), "sensitive")
        self.assertEqual(self._e621(rating="s"), "safe")

    def test_the_shared_codes_agree(self):
        for code, word in (("q", "questionable"), ("e", "explicit")):
            with self.subTest(code=code):
                self.assertEqual(self._danbooru(rating=code), word)
                self.assertEqual(self._e621(rating=code), word)

    def test_danbooru_general_has_no_e621_equivalent(self):
        self.assertEqual(self._danbooru(rating="g"), "general")
        self.assertIsNone(self._e621(rating="g"),
                          "e621 has no 'g' rating - inventing one would be a guess")

    def test_a_word_already_spelled_out_passes_through(self):
        self.assertEqual(self._danbooru(rating="explicit"), "explicit")

    def test_a_post_with_no_rating_field_contributes_nothing(self):
        self.assertIsNone(self._danbooru())
        self.assertIsNone(self._e621())

    def test_a_null_or_unrecognised_rating_is_never_guessed(self):
        for value in (None, "", "   ", "z", 3):
            with self.subTest(value=value):
                self.assertIsNone(self._danbooru(rating=value))
                self.assertIsNone(self._e621(rating=value))

    def test_html_fallback_contributes_no_danbooru_rating(self):
        """The JSON is the only place this parser can read it from with
        confidence, so the scraping path stays silent rather than
        guessing off markup."""
        from core.boorus import danbooru
        self.assertIsNone(danbooru.parse_rating(
            "<html><body><section id='tag-list'></section>Rating: Explicit</body></html>",
            self.DANBOORU_URL))

    def test_gelbooru_reads_the_statistics_sidebar(self):
        """The same block the "Size: WxH" scrape already walks - see
        core/boorus/_sizes.py."""
        from core.boorus import gelbooru
        html = ("<html><body><img id=\"image\" src=\"x.jpg\">"
                "<ul id=\"tag-sidebar\"><li>Id: 3484690</li>"
                "<li>Size: 1232x918</li><li>Rating: Explicit</li></ul></body></html>")
        self.assertEqual(gelbooru.parse_rating(html, self.GELBOORU_URL), "explicit")

    def test_gelbooru_page_without_the_label_says_nothing(self):
        from core.boorus import gelbooru
        html = "<html><body><img id=\"image\" src=\"x.jpg\"><ul id=\"tag-sidebar\"></ul></body></html>"
        self.assertIsNone(gelbooru.parse_rating(html, self.GELBOORU_URL))

    def test_a_post_listing_contributes_no_rating(self):
        """A deleted Gelbooru post redirects to the post LIST, which
        carries a sidebar of its own - its rating would describe whatever
        is on the front page, exactly like its tags would."""
        from core.boorus import gelbooru
        listing = ("<html><body>"
                   + "<div class=\"thumbnail-preview\"></div>" * 3
                   + "<li>Rating: Explicit</li></body></html>")
        self.assertTrue(gelbooru.looks_like_a_listing(listing))
        self.assertIsNone(gelbooru.parse_rating(listing, self.GELBOORU_URL))

    def test_the_gelbooru_family_reads_it_too(self):
        from core.boorus import rule34, safebooru, xbooru
        html = ("<html><body><img id=\"image\" src=\"x.jpg\">"
                "<ul id=\"tag-sidebar\"><li>Rating: Safe</li></ul></body></html>")
        for module in (rule34, safebooru, xbooru):
            with self.subTest(parser=module.__name__):
                self.assertEqual(module.parse_rating(html, self.GELBOORU_URL), "safe")

    # -- moebooru (yande.re / Konachan): the Danbooru 1.x Statistics block --
    #
    # Shaped after the LIVE pages (yande.re/post/show/1000000,
    # konachan.{com,net}/post/show/200000): the rating sits in
    # <div id="stats"> spelled out in words, alongside the "Size: WxH"
    # line _sizes.py already reads, and the <span class="vote-desc">
    # really does follow it inside the same <li>.
    MOEBOORU_URL = "https://yande.re/post/show/1000000"

    MOEBOORU_STATS = """
      <div id="stats" class="vote-container">
        <h5>Statistics</h5>
        <ul>
          <li>Id: 1000000</li>
          <li>Size: 1970x2736</li>
          <li>Rating: Safe <span class="vote-desc"></span></li>
        </ul>
      </div>
    """

    # A post comment is user-supplied prose, and unlike a <script> it DOES
    # reach get_text(). Somebody arguing about the tagging is an ordinary
    # thing to find on one of these pages.
    MOEBOORU_COMMENT = (
        '<div id="comments"><div class="comment">'
        "someone: mis-tagged, Rating: explicit surely?"
        "</div></div>"
    )

    def _moebooru_page(self, *, stats=True, comment_first=False, related=False):
        parts = ["<html><body>"]
        if comment_first:
            parts.append(self.MOEBOORU_COMMENT)
        parts.append('<ul id="tag-sidebar"><li class="tag-type-general">'
                     '<a href="/post?tags=sol">sol</a></li></ul>')
        if stats:
            parts.append(self.MOEBOORU_STATS)
        if related:
            # The page registers a record for every RELATED post too, each
            # with its own rating - which is why the JSON is not the source.
            parts.append('<script>Post.register(999, {"width": 800, '
                         '"height": 600, "rating": "e"});</script>')
        parts.append("</body></html>")
        return "".join(parts)

    def test_moebooru_reads_the_statistics_sidebar(self):
        from core.boorus import moebooru
        self.assertEqual(
            moebooru.parse_rating(self._moebooru_page(), self.MOEBOORU_URL), "safe")

    def test_moebooru_prefers_the_sidebar_over_a_comment_saying_otherwise(self):
        """The reason the hook reads the #stats ELEMENT, not the page text.

        A comment is prose the site's users wrote, and it reaches
        get_text(). Scraping page text would report "explicit" for this
        post because a stranger typed the word - a wrong rating in the
        user's library, wearing the look of fact.

        Note what this is NOT about: the default blacklist yande.re ships
        to every visitor also contains the string "rating:e", but it lives
        in a <script>, and bs4's get_text() omits script text - so it
        cannot reach a page-text scrape and is not the hazard here.
        """
        from core.boorus import moebooru
        page = self._moebooru_page(comment_first=True)
        self.assertLess(page.index("Rating: explicit"), page.index("Rating: Safe"),
                        "the comment must come FIRST, or a page-text scrape would "
                        "find the sidebar anyway and the test proves nothing")
        self.assertEqual(moebooru.parse_rating(page, self.MOEBOORU_URL), "safe")

    def test_moebooru_ignores_a_related_posts_rating(self):
        """Post.register appears once per related post, so the embedded
        JSON cannot say which rating belongs to THIS picture."""
        from core.boorus import moebooru
        self.assertEqual(
            moebooru.parse_rating(self._moebooru_page(related=True),
                                  self.MOEBOORU_URL), "safe")

    def test_moebooru_page_without_the_block_says_nothing(self):
        from core.boorus import moebooru
        self.assertIsNone(
            moebooru.parse_rating(self._moebooru_page(stats=False), self.MOEBOORU_URL))
        self.assertIsNone(moebooru.parse_rating("", self.MOEBOORU_URL))

    def test_moebooru_block_without_the_label_says_nothing(self):
        from core.boorus import moebooru
        html = ('<html><body><div id="stats"><ul><li>Size: 10x10</li></ul>'
                "</div></body></html>")
        self.assertIsNone(moebooru.parse_rating(html, self.MOEBOORU_URL))

    # -- sankaku: its own API field, its own vocabulary --
    SANKAKU_URL = "https://www.sankakucomplex.com/post/12345"

    def _sankaku(self, **extra):
        from core.boorus import sankaku
        body = json.dumps([{"id": "QyMk8vZ6Kak", "tags": [], **extra}])
        return sankaku.parse_rating(body, self.SANKAKU_URL)

    def test_sankaku_s_is_safe_not_sensitive(self):
        """Sankaku is Danbooru 1.x lineage: three ratings, "s" is SAFE.

        Pinned against the live API rather than assumed - its own
        `tags=rating:safe` search returns posts stored as "s", while
        `tags=rating:sensitive` is not a rating search at all. Borrowing
        Danbooru 2's table here would tag every safe Sankaku post
        "sensitive".
        """
        from core.boorus import danbooru, sankaku
        self.assertEqual(self._sankaku(rating="s"), "safe")
        self.assertEqual(danbooru.RATING_CODES["s"], "sensitive",
                         "the two tables must stay different, or this test is moot")
        self.assertNotIn("sensitive", set(sankaku.RATING_CODES.values()))

    def test_sankaku_reads_the_rest_of_its_own_table(self):
        for code, word in (("q", "questionable"), ("e", "explicit")):
            with self.subTest(code=code):
                self.assertEqual(self._sankaku(rating=code), word)

    def test_sankaku_has_no_fourth_rating(self):
        """`tags=rating:g` resolves to a post stored as "s", so "g" is a
        search alias on this site, not a rating it can report. Naming it
        would invent a category Sankaku does not have."""
        self.assertIsNone(self._sankaku(rating="g"))

    def test_sankaku_never_guesses(self):
        for value in (None, "", "   ", "z", 3, {}):
            with self.subTest(value=value):
                self.assertIsNone(self._sankaku(rating=value))

    def test_a_sankaku_hash_miss_contributes_no_rating(self):
        """The ordinary outcome - the local file is not Sankaku's copy -
        and the legacy HTML page, which is a login wall with no rating on
        it at all."""
        from core.boorus import sankaku
        for body in ("[]", "", "<html>Sign in</html>"):
            with self.subTest(body=body[:12]):
                self.assertIsNone(sankaku.parse_rating(body, self.SANKAKU_URL))

    def test_rule34us_states_no_rating_and_so_exposes_no_hook(self):
        """Measured, not assumed: the rule34.us Statistics block holds
        Id/Added by/Created/Score/Size and no Rating line, and the only
        "rating" on the page is a static <meta name="rating"
        content="adult"> served on every page of the site. Reading that
        would stamp one word on every rule34.us match. The hook is
        optional (core/boorus/__init__.py guards it with hasattr), so its
        absence is the correct answer here.
        """
        from core.boorus import rule34us
        self.assertFalse(hasattr(rule34us, "parse_rating"))

        from core.boorus._parsed import soup_of
        from core.boorus._rating import from_label
        page = ('<html><head><meta name="rating" content="adult" /></head>'
                '<body><div style="font-size:1.2em;">Statistics</div>'
                '<li class="general-tag">Id: 6000000</li>'
                '<li class="general-tag">Score: <span>8</span></li>'
                '<li class="general-tag">Size: 411w x 573h</li>'
                "</body></html>")
        self.assertIsNone(from_label(soup_of(page).get_text(" ")),
                          "the page text states no rating - the meta tag is an "
                          "attribute, so even a whole-page scrape finds none")

    def test_fetch_page_info_carries_the_rating_through(self):
        """The hook has to be called and its answer kept, or the setting
        below has nothing to act on."""
        import types
        parser = types.ModuleType("rated")
        parser.matches = lambda url: True
        parser.parse = lambda body, url: [Tag("1girl", TagSource.BOORU, "general")]
        parser.parse_rating = lambda body, url: "explicit"

        resp = MagicMock(status_code=200, text="<html></html>", encoding=None)
        resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        with patch("core.boorus.find_parser", return_value=parser), \
             patch("requests.Session.get", return_value=resp):
            info = fetch_page_info("https://example.test/post/1", 5.0)
        self.assertEqual(info.rating, "explicit")
        self.assertEqual([t.display for t in info.tags], ["general:1girl"],
                         "the rating is kept OUT of the parser's own tag list")

    def test_a_parser_whose_rating_hook_raises_keeps_the_rest(self):
        import types
        parser = types.ModuleType("exploding_rating")
        parser.matches = lambda url: True
        parser.parse = lambda body, url: [Tag("1girl", TagSource.BOORU, "general")]

        def boom(body, url):
            raise RuntimeError("markup moved")
        parser.parse_rating = boom

        resp = MagicMock(status_code=200, text="<html></html>", encoding=None)
        resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        with patch("core.boorus.find_parser", return_value=parser), \
             patch("requests.Session.get", return_value=resp):
            info = fetch_page_info("https://example.test/post/1", 5.0)
        self.assertIsNone(info.rating)
        self.assertEqual(len(info.tags), 1)


class TestRatingTag(unittest.TestCase):
    """Turning that rating into a tag - the part that changes what lands
    in the user's Hydrus, so it is off until asked for."""

    def _tags(self):
        return [Tag("1girl", TagSource.BOORU, "general")]

    def test_off_by_default(self):
        """The whole point of the default: an existing setup's tag output
        must not change because this shipped."""
        tags = self._tags()
        self.assertFalse(Settings().add_rating_tag)
        self.assertIs(with_rating_tag(tags, "explicit", Settings()), tags)

    def test_on_it_adds_exactly_one_namespaced_tag(self):
        s = Settings()
        s.add_rating_tag = True
        out = with_rating_tag(self._tags(), "explicit", s)
        self.assertEqual([t.display for t in out], ["general:1girl", "rating:explicit"])
        self.assertEqual(out[-1].source, TagSource.BOORU)

    def test_a_site_that_states_nothing_contributes_nothing(self):
        s = Settings()
        s.add_rating_tag = True
        tags = self._tags()
        for rating in (None, ""):
            with self.subTest(rating=rating):
                self.assertIs(with_rating_tag(tags, rating, s), tags)

    def test_the_namespace_is_configurable(self):
        s = Settings()
        s.add_rating_tag = True
        s.rating_tag_namespace = "content rating"
        out = with_rating_tag(self._tags(), "safe", s)
        self.assertEqual(out[-1].display, "content rating:safe")

    def test_a_blank_namespace_falls_back_rather_than_going_unnamespaced(self):
        """An unnamespaced "explicit" floating among the general tags is
        not what anyone clearing the box meant."""
        s = Settings()
        s.add_rating_tag = True
        for blank in ("", "   "):
            with self.subTest(namespace=repr(blank)):
                s.rating_tag_namespace = blank
                self.assertEqual(with_rating_tag(self._tags(), "safe", s)[-1].display,
                                 "rating:safe")

    def test_the_original_list_is_never_mutated(self):
        s = Settings()
        s.add_rating_tag = True
        tags = self._tags()
        with_rating_tag(tags, "explicit", s)
        self.assertEqual(len(tags), 1)

    def test_it_is_blacklistable_like_any_other_booru_tag(self):
        s = Settings()
        s.add_rating_tag = True
        s.enable_tag_blacklist = True
        s.tag_blacklist = ["rating:*"]
        out = apply_tag_blacklist(with_rating_tag(self._tags(), "explicit", s), s)
        self.assertEqual([t.display for t in out], ["general:1girl"])

    def test_it_is_remappable_like_any_other_booru_tag(self):
        s = Settings()
        s.add_rating_tag = True
        s.enable_tag_namespace_remap = True
        s.tag_namespace_remap = {"rating": "content rating"}
        out = apply_namespace_remap(with_rating_tag(self._tags(), "safe", s), s)
        self.assertEqual(out[-1].display, "content rating:safe")


class TestRatingTagReachesTheCandidate(unittest.TestCase):
    """End to end through core/search_engine.py - the path that decides
    what the tag panel shows and what gets sent to Hydrus."""

    def _fetch(self, settings, rating="explicit"):
        from core.boorus import BooruPageInfo
        from core.models import MatchCandidate
        from core.search_engine import fetch_candidate_details
        candidate = MatchCandidate(url="https://danbooru.donmai.us/posts/1")
        page_info = BooruPageInfo(
            tags=[Tag("1girl", TagSource.BOORU, "general")],
            rating=rating, fetched=True,
        )
        with patch("core.search_engine.fetch_page_info", return_value=page_info):
            fetch_candidate_details(candidate, settings)
        return [t.display for t in candidate.booru_tags]

    def test_the_default_leaves_the_tag_output_exactly_as_it_was(self):
        self.assertEqual(self._fetch(Settings()), ["general:1girl"])

    def test_turning_it_on_adds_the_rating(self):
        s = Settings()
        s.add_rating_tag = True
        self.assertEqual(self._fetch(s), ["general:1girl", "rating:explicit"])

    def test_a_site_with_no_rating_adds_nothing_even_when_on(self):
        s = Settings()
        s.add_rating_tag = True
        self.assertEqual(self._fetch(s, rating=None), ["general:1girl"])

    def test_the_blacklist_still_gets_the_last_word(self):
        s = Settings()
        s.add_rating_tag = True
        s.enable_tag_blacklist = True
        s.tag_blacklist = ["rating:*"]
        self.assertEqual(self._fetch(s), ["general:1girl"])


class TestSauceNaoMirroredResults(unittest.TestCase):
    """One SauceNAO result can list the same image on several sites -
    ext_urls is a list. Keeping only the first discarded the alternatives,
    and since the label came from SauceNAO's index rather than the URL,
    a match could read "e621" while opening Sankaku."""

    def setUp(self):
        import os, tempfile
        from PIL import Image
        self.path = os.path.join(tempfile.mkdtemp(), "x.jpg")
        Image.new("RGB", (64, 64), "red").save(self.path)

    def _search(self, ext_urls):
        from unittest.mock import MagicMock, patch
        from core.config import SauceNaoSettings
        from core import saucenao
        response = {
            "header": {"status": 0, "long_remaining": 100, "long_limit": 100},
            "results": [{
                "header": {
                    "similarity": "97.16",
                    "index_name": "Index #29: e621.net - 1234.jpg",
                    "thumbnail": "https://img1.saucenao.com/booru/t.jpg",
                },
                "data": {"ext_urls": ext_urls},
            }],
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = response
        with patch("requests.Session.post", return_value=resp):
            return saucenao.search(self.path, SauceNaoSettings(api_key="k", use_json_api=True))

    def test_every_mirrored_url_becomes_a_candidate(self):
        matches = self._search([
            "https://chan.sankakucomplex.com/post/show/12345",
            "https://e621.net/posts/67890",
        ])
        self.assertEqual(len(matches), 2)
        urls = {m.url for m in matches}
        self.assertTrue(any("sankaku" in u for u in urls))
        self.assertTrue(any("e621" in u for u in urls))

    def test_duplicate_urls_are_collapsed(self):
        matches = self._search([
            "https://e621.net/posts/67890",
            "https://e621.net/posts/67890",
        ])
        self.assertEqual(len(matches), 1)

    def test_label_follows_the_url_not_saucenaos_index(self):
        """REGRESSION GUARD: the index said e621 while the first URL was
        Sankaku, so the match displayed one site and opened another."""
        matches = self._search([
            "https://chan.sankakucomplex.com/post/show/12345",
            "https://e621.net/posts/67890",
        ])
        for match in matches:
            label = classify_site(match.source_name, match.url)
            if "sankaku" in match.url:
                self.assertEqual(label, "Sankaku Complex")
            else:
                self.assertEqual(label, "e621")

    def test_result_with_no_usable_url_is_skipped(self):
        self.assertEqual(self._search(["not a url", ""]), [])

    def test_site_filter_keeps_the_matching_url(self):
        """Unchecking a site used to be ineffective for mirrored results:
        the unwanted URL survived behind the other site's label."""
        from unittest.mock import MagicMock, patch
        from core.config import Settings
        from core.models import ImageEntry
        import core.search_engine as se

        settings = Settings()
        settings.primary_engine = "saucenao"
        settings.secondary_engine_mode = "disabled"
        settings.enable_ascii2d = False
        settings.enable_tracemoe = False
        settings.enable_iqdb3d = False
        settings.retrieve_tags_from_booru = False
        settings.saucenao.api_key = "k"
        settings.saucenao.use_json_api = True
        settings.saucenao.min_similarity = 50
        settings.enabled_sites = ["e621"]

        response = {
            "header": {"status": 0, "long_remaining": 100, "long_limit": 100},
            "results": [{
                "header": {"similarity": "97.16",
                           "index_name": "Index #29: e621.net - x.jpg",
                           "thumbnail": "https://img1.saucenao.com/booru/t.jpg"},
                "data": {"ext_urls": ["https://chan.sankakucomplex.com/post/show/12345",
                                      "https://e621.net/posts/67890"]},
            }],
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = response
        entry = ImageEntry(path=self.path)
        with patch("requests.Session.post", return_value=resp), \
             patch.object(se, "_load_local_dimensions", lambda e: None), \
             patch.object(se.remote, "download_bytes", return_value=None), \
             patch.object(se.remote, "fetch_remote_info", return_value=(None, None)):
            se.search_image(entry, settings, use_cache=False)

        self.assertEqual(len(entry.candidates), 1)
        self.assertIn("e621.net", entry.matched_url)
        self.assertEqual(entry.booru_name, "e621")


class TestSearchCacheVersioning(unittest.TestCase):
    """The cache had no format version, so entries written by any older
    build were restored verbatim AND flagged as fully fetched. Fields
    added later (preview_url, direct_file_url, real dimensions) stayed
    permanently empty, and every cached match on every site silently fell
    back to the search engine's own cached thumbnail."""

    def _old_entry(self):
        return {
            "cached_at": 1_700_000_000,
            "status": "good",
            "selected_candidate_index": 0,
            "candidates": [{
                "url": "https://danbooru.donmai.us/posts/5105386",
                "thumb_url": "https://img1.saucenao.com/booru/cached_thumb.jpg",
                "similarity": 96.0,
                "engine": "SauceNAO",
                "engine_tags": [],
                "booru_tags": [{"name": "1girl", "namespace": None}],
                # no direct_file_url / preview_url / dimensions, and no
                # format_version on the entry itself
            }],
        }

    def test_versionless_entry_is_marked_for_refetch(self):
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result
        entry = ImageEntry(path="/tmp/x.jpg")
        self.assertTrue(apply_cached_result(entry, self._old_entry()))
        candidate = entry.candidates[0]
        self.assertFalse(candidate.booru_tags_fetched,
                         "an entry missing fields must not claim to be fully fetched")
        self.assertFalse(candidate.remote_info_fetched)

    def test_versionless_entry_keeps_the_expensive_part(self):
        """The rate-limited engine call is what's costly; the booru page
        read is free. So a stale entry refreshes its details rather than
        forcing a whole new search."""
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result
        entry = ImageEntry(path="/tmp/x.jpg")
        apply_cached_result(entry, self._old_entry())
        candidate = entry.candidates[0]
        self.assertEqual(candidate.url, "https://danbooru.donmai.us/posts/5105386")
        self.assertEqual(candidate.similarity, 96.0)
        self.assertEqual(entry.status.value, "good")

    def test_current_entry_is_not_refetched(self):
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result, CACHE_FORMAT_VERSION
        payload = self._old_entry()
        payload["format_version"] = CACHE_FORMAT_VERSION
        payload["candidates"][0]["direct_file_url"] = "https://cdn.donmai.us/original/o.jpg"
        payload["candidates"][0]["preview_url"] = "https://cdn.donmai.us/sample/s.jpg"

        entry = ImageEntry(path="/tmp/x.jpg")
        apply_cached_result(entry, payload)
        candidate = entry.candidates[0]
        self.assertTrue(candidate.booru_tags_fetched)
        self.assertTrue(candidate.remote_info_fetched)
        self.assertIn("/original/", candidate.direct_file_url)

    def test_saved_entries_carry_the_version(self):
        import importlib, os, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-cachever-")
        import core.paths
        importlib.reload(core.paths)
        import core.search_cache
        importlib.reload(core.search_cache)
        from core.models import ImageEntry, MatchCandidate, MatchStatus

        entry = ImageEntry(path="/tmp/y.jpg")
        entry.status = MatchStatus.GOOD
        entry.candidates = [MatchCandidate(url="https://danbooru.donmai.us/posts/1")]
        entry.select_candidate(0)
        core.search_cache.save_cached_result("aa" * 32, entry)

        cached = core.search_cache.load_cached_result("aa" * 32)
        self.assertEqual(cached["format_version"], core.search_cache.CACHE_FORMAT_VERSION)


class TestPerParserTagInvalidation(unittest.TestCase):
    """CACHE_FORMAT_VERSION is all-or-nothing, so one parser changing its
    tags meant invalidating every site's entries or none. None is what
    happened: 106 entries went on serving 348 MangaDex tags the parser had
    stopped producing."""

    URL = "https://mangadex.org/chapter/2f1d73c5-b3c9-450a-bc5f-fa702940c36f/"

    def _entry(self, tag_version, url=None):
        from core.search_cache import CACHE_FORMAT_VERSION
        return {
            "cached_at": 1_700_000_000,
            "status": "good",
            "format_version": CACHE_FORMAT_VERSION,
            "selected_candidate_index": 0,
            "candidates": [{
                "url": url or self.URL,
                "similarity": 91.0,
                "engine": "SauceNAO",
                "engine_tags": [],
                "booru_tags": [{"name": "Some Manga", "namespace": "series"}],
                "direct_file_url": "https://cdn.example/x.png",
                "preview_url": "https://cdn.example/s.jpg",
                "tag_version": tag_version,
            }],
        }

    def test_a_stale_tag_version_forces_the_tags_to_be_re_read(self):
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result
        entry = ImageEntry(path="/tmp/x.png")
        apply_cached_result(entry, self._entry(tag_version=0))  # written pre-change
        self.assertFalse(entry.candidates[0].booru_tags_fetched)

    def test_a_current_tag_version_is_left_alone(self):
        from core.boorus import mangadex
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result
        entry = ImageEntry(path="/tmp/x.png")
        apply_cached_result(entry, self._entry(tag_version=mangadex.TAG_VERSION))
        self.assertTrue(entry.candidates[0].booru_tags_fetched)

    def test_another_site_is_not_invalidated_by_mangadex_changing(self):
        """The whole point: bumping one parser must not cost every other
        site's entries a re-read."""
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result
        entry = ImageEntry(path="/tmp/x.png")
        apply_cached_result(entry, self._entry(
            tag_version=0, url="https://danbooru.donmai.us/posts/5105386"))
        self.assertTrue(entry.candidates[0].booru_tags_fetched,
                        "Danbooru has never bumped TAG_VERSION, so 0 is current for it")

    def test_the_expensive_half_survives_invalidation(self):
        """Only the booru page is re-read. The rate-limited engine call
        that produced the URL is not repeated."""
        from core.models import ImageEntry
        from core.search_cache import apply_cached_result
        entry = ImageEntry(path="/tmp/x.png")
        apply_cached_result(entry, self._entry(tag_version=0))
        candidate = entry.candidates[0]
        self.assertEqual(candidate.url, self.URL)
        self.assertEqual(candidate.similarity, 91.0)
        # A tag change says nothing about dimensions or file size, so
        # those are not re-fetched.
        self.assertTrue(candidate.remote_info_fetched)

    def test_saving_records_the_parsers_current_tag_version(self):
        import importlib, os, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-tagver-")
        import core.paths
        importlib.reload(core.paths)
        import core.search_cache
        importlib.reload(core.search_cache)
        from core.boorus import mangadex
        from core.models import ImageEntry, MatchCandidate, MatchStatus

        entry = ImageEntry(path="/tmp/y.png")
        entry.status = MatchStatus.GOOD
        entry.candidates = [MatchCandidate(url=self.URL)]
        entry.select_candidate(0)
        core.search_cache.save_cached_result("bb" * 32, entry)

        cached = core.search_cache.load_cached_result("bb" * 32)
        self.assertEqual(cached["candidates"][0]["tag_version"], mangadex.TAG_VERSION)


class TestSearchCache(unittest.TestCase):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
        import importlib
        import core.paths, core.search_cache
        importlib.reload(core.paths)
        importlib.reload(core.search_cache)
        self.cache = core.search_cache

    def test_error_results_are_never_cached(self):
        """A transient failure must be retried, not remembered as a
        settled outcome."""
        e = ImageEntry(path="/tmp/x.jpg")
        e.status = MatchStatus.ERROR
        self.cache.save_cached_result("hash_err", e)
        self.assertIsNone(self.cache.load_cached_result("hash_err"))

    def test_not_found_is_cached(self):
        e = ImageEntry(path="/tmp/y.jpg")
        e.status = MatchStatus.NOT_FOUND
        self.cache.save_cached_result("hash_nf", e)
        self.assertIsNotNone(self.cache.load_cached_result("hash_nf"))

    # -- expiry --------------------------------------------------------
    def _age_entry(self, key, days):
        """Backdates a cached entry so expiry can be tested without
        waiting - the stored timestamp is the only thing that matters."""
        import json
        import time
        path = self.cache._cache_path(key)
        data = json.loads(path.read_text())
        data["cached_at"] = time.time() - days * 86400
        path.write_text(json.dumps(data))

    def _save(self, key, status):
        e = ImageEntry(path="/tmp/z.jpg")
        e.status = status
        self.cache.save_cached_result(key, e)

    def test_fresh_not_found_is_still_served(self):
        self._save("k_fresh", MatchStatus.NOT_FOUND)
        self._age_entry("k_fresh", days=3)
        self.assertIsNotNone(self.cache.load_cached_result("k_fresh", ttl_days=30))

    def test_stale_not_found_expires(self):
        """A negative result is a claim about one moment in time; sites
        index new work constantly, and a cached miss is never rechecked."""
        self._save("k_stale", MatchStatus.NOT_FOUND)
        self._age_entry("k_stale", days=45)
        self.assertIsNone(self.cache.load_cached_result("k_stale", ttl_days=30))

    def test_expired_entry_is_deleted_so_the_cache_self_prunes(self):
        self._save("k_gone", MatchStatus.NOT_FOUND)
        self._age_entry("k_gone", days=45)
        self.cache.load_cached_result("k_gone", ttl_days=30)
        self.assertFalse(self.cache._cache_path("k_gone").exists())

    def test_found_results_never_expire(self):
        """A successful match doesn't go stale the same way, and
        re-running it would spend rate-limited quota for the same answer.
        Dead URLs are handled by availability checking instead."""
        self._save("k_good", MatchStatus.GOOD)
        self._age_entry("k_good", days=9999)
        self.assertIsNotNone(self.cache.load_cached_result("k_good", ttl_days=30))

    def test_ttl_zero_disables_expiry(self):
        self._save("k_never", MatchStatus.NOT_FOUND)
        self._age_entry("k_never", days=9999)
        self.assertIsNotNone(self.cache.load_cached_result("k_never", ttl_days=0))

    def test_entry_without_timestamp_is_treated_as_expired(self):
        """Written before timestamps existed, or corrupt - better to
        re-search once than trust it forever with no way to judge age."""
        import json
        self._save("k_nots", MatchStatus.NOT_FOUND)
        path = self.cache._cache_path("k_nots")
        data = json.loads(path.read_text())
        del data["cached_at"]
        path.write_text(json.dumps(data))
        self.assertIsNone(self.cache.load_cached_result("k_nots", ttl_days=30))


if __name__ == "__main__":
    unittest.main()


class TestE621PreviewFallback(unittest.TestCase):
    """REGRESSION: e621 matches showed a visibly blurry picture.

    The parser returned post["sample"]["url"] directly, but e621 only
    HAS a sample when the original was big enough to be worth downscaling.
    For a smaller post it sends {"has": false, "url": null}, so the parser
    returned None and the app fell back to the search engine's ~150px
    thumbnail - a blurry preview for exactly the posts whose original was
    small enough to have been shown in full.
    """

    def _body(self, sample_url, file_url="https://e621.net/data/f.jpg",
              preview_url="https://e621.net/data/preview/p.jpg"):
        return json.dumps({"post": {
            "id": 219935,
            "file": {"url": file_url, "width": 664, "height": 909, "ext": "jpg"},
            "sample": {"has": sample_url is not None, "url": sample_url,
                       "width": 664, "height": 909},
            "preview": {"url": preview_url, "width": 256, "height": 350},
            "tags": {},
        }})

    def test_the_sample_is_used_when_there_is_one(self):
        from core.boorus import e621
        body = self._body("https://e621.net/data/sample/s.jpg")
        self.assertEqual(e621.parse_preview_url(body, "u"),
                         "https://e621.net/data/sample/s.jpg")

    def test_a_null_sample_falls_back_to_the_original(self):
        """e621 omits the sample only when the original is already modest,
        so the file is both the better picture and a cheap one."""
        from core.boorus import e621
        self.assertEqual(e621.parse_preview_url(self._body(None), "u"),
                         "https://e621.net/data/f.jpg")

    def test_the_tiny_preview_is_the_last_resort(self):
        """For a post whose file URL is withheld, something is still
        better than nothing."""
        from core.boorus import e621
        body = self._body(None, file_url=None)
        self.assertEqual(e621.parse_preview_url(body, "u"),
                         "https://e621.net/data/preview/p.jpg")

    def test_no_usable_url_at_all_is_none(self):
        from core.boorus import e621
        body = self._body(None, file_url=None, preview_url=None)
        self.assertIsNone(e621.parse_preview_url(body, "u"))

    def test_a_relative_or_junk_url_is_not_offered(self):
        from core.boorus import e621
        body = self._body(None, file_url="/data/f.jpg")
        self.assertEqual(e621.parse_preview_url(body, "u"),
                         "https://e621.net/data/preview/p.jpg")


GELBOORU_FAMILY_LI = """
  <li class="tag-type-copyright tag">
    <a href="index.php?page=wiki&amp;s=list&amp;search=fire_emblem">?</a>
    <a href="index.php?page=post&amp;s=list&amp;tags=fire_emblem">fire emblem</a>
    <span>123</span>
  </li>
  <li class="tag-type-artist tag">
    <a href="index.php?page=wiki&amp;s=list&amp;search=someone">?</a>
    <a href="index.php?page=post&amp;s=list&amp;tags=someone">someone</a>
  </li>
  <li class="tag-type-general tag">
    <a href="index.php?page=wiki&amp;s=list&amp;search=1boy">?</a>
    <a href="index.php?page=post&amp;s=list&amp;tags=1boy">1boy</a>
  </li>
"""


class TestGelbooruFamilyTagContainer(unittest.TestCase):
    """REGRESSION: Safebooru, rule34 and Xbooru extracted NO tags at all.

    All three delegate tag parsing to the Gelbooru module, which looked
    only for #tag-list. Gelbooru itself uses that id - its relatives use
    #tag-sidebar. Verified against all four live sites: the li markup
    inside is identical, only the container id differs.
    """

    def _page(self, container_id):
        return f'<html><body><ul id="{container_id}">{GELBOORU_FAMILY_LI}</ul></body></html>'

    def _named(self, tags):
        return {(t.namespace, t.name) for t in tags}

    def test_gelbooru_s_own_tag_list_still_works(self):
        from core.boorus import gelbooru
        tags = self._named(gelbooru.parse(self._page("tag-list"), "u"))
        self.assertIn(("copyright", "fire_emblem"), tags)
        self.assertIn(("artist", "someone"), tags)
        self.assertIn(("general", "1boy"), tags)

    def test_the_relatives_tag_sidebar_is_read_too(self):
        from core.boorus import gelbooru
        tags = self._named(gelbooru.parse(self._page("tag-sidebar"), "u"))
        self.assertEqual(len(tags), 3, "this used to come back empty")
        self.assertIn(("copyright", "fire_emblem"), tags)

    def test_every_family_member_reads_the_sidebar(self):
        from core.boorus import rule34, safebooru, xbooru
        page = self._page("tag-sidebar")
        for module in (safebooru, rule34, xbooru):
            with self.subTest(parser=module.__name__):
                self.assertEqual(len(module.parse(page, "u")), 3)

    def test_the_wiki_link_is_not_mistaken_for_the_tag_name(self):
        """Each li holds a '?' wiki link before the real one."""
        from core.boorus import gelbooru
        names = {t.name for t in gelbooru.parse(self._page("tag-sidebar"), "u")}
        self.assertNotIn("?", names)

    def test_a_page_with_neither_container_yields_nothing(self):
        from core.boorus import gelbooru
        self.assertEqual(gelbooru.parse("<html><body>nope</body></html>", "u"), [])


class TestSafebooruPreview(unittest.TestCase):
    """REGRESSION: Safebooru had no parse_preview_url at all, so every
    match fell back to the search engine's ~150px thumbnail even though
    the page already fetched names a far better picture."""

    PAGE = ('<html><body><img id="image" '
            'src="https://safebooru.org/samples/1/sample_abc.jpg"></body></html>')

    def test_the_page_image_is_used_as_the_preview(self):
        from core.boorus import safebooru
        self.assertEqual(safebooru.parse_preview_url(self.PAGE, "u"),
                         "https://safebooru.org/samples/1/sample_abc.jpg")

    def test_a_page_without_an_image_element_is_none(self):
        from core.boorus import safebooru
        self.assertIsNone(safebooru.parse_preview_url("<html></html>", "u"))


class TestZerochanPreviewSize(unittest.TestCase):
    """REGRESSION: Zerochan's variants are not what their names suggest.
    Measured on a live 2976x4055 post: small 102px, medium 240px, large
    600px, full the original. The parser took "medium" - a 240px
    thumbnail, barely better than the search engine's own."""

    def _body(self, **fields):
        base = {"small": "https://z/s.jpg", "medium": "https://z/m.avif",
                "large": "https://z/l.jpg", "full": "https://z/f.jpg",
                "width": 2976, "height": 4055, "tags": []}
        base.update(fields)
        return json.dumps(base)

    def test_large_is_preferred_over_medium(self):
        from core.boorus import zerochan
        self.assertEqual(zerochan.parse_preview_url(self._body(), "u"), "https://z/l.jpg")

    def test_it_falls_back_when_large_is_absent(self):
        from core.boorus import zerochan
        self.assertEqual(zerochan.parse_preview_url(self._body(large=None), "u"),
                         "https://z/m.avif")
        self.assertEqual(
            zerochan.parse_preview_url(self._body(large=None, medium=None), "u"),
            "https://z/s.jpg")

    def test_the_full_original_is_not_used_as_a_preview(self):
        """That is what parse_file_url is for - 5.5MB on the measured post."""
        from core.boorus import zerochan
        body = self._body(large=None, medium=None, small=None)
        self.assertIsNone(zerochan.parse_preview_url(body, "u"))
        self.assertEqual(zerochan.parse_file_url(self._body(), "u"), "https://z/f.jpg")


MANGADEX_AT_HOME = json.dumps({
    "result": "ok",
    "baseUrl": "https://node1.mangadex.network",
    "chapter": {
        "hash": "abc123hash",
        "data": ["1-aaa.png", "2-bbb.png", "3-ccc.png"],
        "dataSaver": ["1-xxx.jpg", "2-yyy.jpg", "3-zzz.jpg"],
    },
})


class TestMangadexParser(unittest.TestCase):
    """MangaDex chapters, read through the public API.

    Classification-only for a long time because a chapter holds many
    images under one URL. Multi-page support made that obsolete: the
    chapter is a multi-page post, and which page matches is settled by
    hashing against the local file, exactly as for Pixiv.
    """

    URL = "https://mangadex.org/chapter/2f1d73c5-b3c9-450a-bc5f-fa702940c36f/"

    def test_it_claims_chapter_urls(self):
        from core.boorus import mangadex
        self.assertTrue(mangadex.matches(self.URL))
        self.assertTrue(mangadex.matches(self.URL.rstrip("/")))

    def test_a_title_url_is_left_alone(self):
        """A /title/ URL is a whole manga with no page list of its own,
        so it stays classification-only rather than being fetched."""
        from core.boorus import mangadex
        self.assertFalse(mangadex.matches(
            "https://mangadex.org/title/2f1d73c5-b3c9-450a-bc5f-fa702940c36f/some-manga"))

    def test_other_sites_are_not_claimed(self):
        from core.boorus import mangadex
        self.assertFalse(mangadex.matches("https://danbooru.donmai.us/posts/1"))
        self.assertFalse(mangadex.matches("https://notmangadex.org/chapter/"
                                          "2f1d73c5-b3c9-450a-bc5f-fa702940c36f"))
        self.assertFalse(mangadex.matches(""))

    def test_the_registry_routes_chapters_here(self):
        from core.boorus import find_parser
        self.assertEqual(find_parser(self.URL).__name__.rsplit(".", 1)[-1], "mangadex")

    def test_it_fetches_the_api_not_the_page(self):
        """mangadex.org serves a JS front end with nothing useful in the
        markup."""
        from core.boorus import mangadex
        self.assertEqual(
            mangadex.resolve_fetch_url(self.URL),
            "https://api.mangadex.org/at-home/server/2f1d73c5-b3c9-450a-bc5f-fa702940c36f")

    def test_the_page_list_url_is_the_same_response(self):
        """So working out which page matched costs no extra request."""
        from core.boorus import mangadex
        self.assertEqual(mangadex.pages_api_url(self.URL),
                         mangadex.resolve_fetch_url(self.URL))

    def test_page_count(self):
        from core.boorus import mangadex
        self.assertEqual(mangadex.parse_page_count(MANGADEX_AT_HOME, self.URL), 3)

    def test_the_file_url_is_the_full_resolution_page(self):
        from core.boorus import mangadex
        self.assertEqual(
            mangadex.parse_file_url(MANGADEX_AT_HOME, self.URL),
            "https://node1.mangadex.network/data/abc123hash/1-aaa.png")

    def test_the_preview_is_the_saver_image(self):
        """Measured on a real chapter: 182 KB against the original's
        2.4 MB, for the same picture."""
        from core.boorus import mangadex
        self.assertEqual(
            mangadex.parse_preview_url(MANGADEX_AT_HOME, self.URL),
            "https://node1.mangadex.network/data-saver/abc123hash/1-xxx.jpg")

    def test_pages_carry_both_sizes_in_order(self):
        from core.boorus import mangadex
        pages = mangadex.parse_pages(MANGADEX_AT_HOME, self.URL)
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[2]["urls"]["original"],
                         "https://node1.mangadex.network/data/abc123hash/3-ccc.png")
        # "small" is what the resolver hashes, so it must be the cheap one.
        self.assertEqual(pages[2]["urls"]["small"],
                         "https://node1.mangadex.network/data-saver/abc123hash/3-zzz.jpg")

    def test_dimensions_are_absent_rather_than_guessed(self):
        """The API doesn't report them. The resolver reads unknown as "no
        evidence" and falls back to hashing; invented ones would be
        evidence pointing at the wrong page."""
        from core.boorus import mangadex
        page = mangadex.parse_pages(MANGADEX_AT_HOME, self.URL)[0]
        self.assertIsNone(page["width"])
        self.assertIsNone(page["height"])

    def test_a_missing_saver_falls_back_to_the_original(self):
        """Never pair a page with another page's saver image - attaching
        the wrong picture is the exact mistake this mechanism exists to
        prevent."""
        from core.boorus import mangadex
        body = json.dumps({
            "result": "ok", "baseUrl": "https://n.example", "chapter": {
                "hash": "h", "data": ["1-a.png", "2-b.png"], "dataSaver": ["1-x.jpg"]},
        })
        pages = mangadex.parse_pages(body, self.URL)
        self.assertEqual(pages[1]["urls"]["small"], "https://n.example/data/h/2-b.png")
        self.assertEqual(pages[0]["urls"]["small"], "https://n.example/data-saver/h/1-x.jpg")

    def test_an_error_response_yields_nothing(self):
        """A chapter that has been taken down or re-uploaded. Four in ten
        of this app's own MangaDex matches were already 404."""
        from core.boorus import mangadex
        body = json.dumps({"result": "error", "errors": [{"status": 404}]})
        self.assertEqual(mangadex.parse_pages(body, self.URL), [])
        self.assertIsNone(mangadex.parse_file_url(body, self.URL))
        self.assertEqual(mangadex.parse_page_count(body, self.URL), 1)

    def test_no_tags_are_generated(self):
        """Series/volume/chapter/page tagging now happens in the Hydrus
        importer itself, not here. parse() must still exist - it's called
        unconditionally by fetch_page_info - but always returns none."""
        from core.boorus import mangadex
        self.assertEqual(mangadex.parse(MANGADEX_AT_HOME, self.URL), [])

    def test_returning_no_tags_is_not_treated_as_a_broken_parser(self):
        """Every match would otherwise log "markup may have changed" and
        trip the health check into calling MangaDex broken after three."""
        from core.boorus import mangadex
        self.assertTrue(getattr(mangadex, "EMPTY_RESULT_IS_NORMAL", False))

    def test_junk_is_survived(self):
        from core.boorus import mangadex
        for body in ("", "not json", "[]", '{"result":"ok"}',
                     '{"result":"ok","baseUrl":"x","chapter":{}}'):
            with self.subTest(body=body[:20]):
                self.assertEqual(mangadex.parse_pages(body, self.URL), [])


class TestGoneByRedirectNotByWording(unittest.TestCase):
    """Some sites say "deleted" with a redirect and no words at all.

    Gelbooru answers HTTP 200 for a deleted post and bounces to its post
    LIST, so there is no wording in the body for a marker to match - its
    soft-404 marker list is empty. That left a single-candidate Gelbooru
    match with NO check at all: the availability sweep only runs when
    there is more than one candidate to choose between, so a deleted
    post went straight into the dropdown. Deleting the search cache did
    not help, because the match was arriving fresh every time.
    """

    LIST_URL = "https://gelbooru.com/index.php?page=post&s=list&tags=all"
    POST_URL = "https://gelbooru.com/index.php?page=post&s=view&id=6352196"

    def _fetch(self, final_url, body="<html><body>nothing</body></html>"):
        from unittest.mock import MagicMock, patch as _patch
        from core.boorus import fetch_page_info
        from core.search_engine import _redirect_means_gone, _soft_404_markers_for

        response = MagicMock(status_code=200, url=final_url)
        response.text = body
        response.content = body.encode()
        response.headers = {"Content-Type": "text/html"}
        with _patch("requests.Session.get", return_value=response):
            return fetch_page_info(
                self.POST_URL, gone_markers=_soft_404_markers_for(self.POST_URL),
                gone_redirect=_redirect_means_gone,
            )

    def test_a_redirect_to_the_listing_is_reported_as_gone(self):
        info = self._fetch(self.LIST_URL)
        self.assertIsNotNone(info.gone_reason)
        self.assertIn("s=list", info.gone_reason)

    def test_a_post_that_did_not_redirect_is_left_alone(self):
        info = self._fetch(self.POST_URL,
                           '<html><body><img id="image" src="x.jpg"></body></html>')
        self.assertIsNone(info.gone_reason)

    def test_gelbooru_has_no_wording_to_match_on(self):
        """Which is exactly why the redirect had to be the signal."""
        from core.search_engine import _soft_404_markers_for
        self.assertEqual(_soft_404_markers_for(self.POST_URL), ())

    def test_asking_for_a_listing_directly_is_not_gone(self):
        """The fragment has to appear in where we LANDED and not in where
        we started - a list page legitimately contains "s=list" all
        along, and would otherwise report itself deleted."""
        from core.search_engine import _redirect_means_gone
        self.assertIsNone(_redirect_means_gone(self.LIST_URL, self.LIST_URL))
        self.assertIsNotNone(_redirect_means_gone(self.POST_URL, self.LIST_URL))


class TestGelbooruListingIsNotAPost(unittest.TestCase):
    """A deleted Gelbooru post does not 404.

    CONFIRMED against the live site: id=6352196 answers HTTP 200 after
    redirecting to the post LIST, and that list page carries a tag
    sidebar in the very markup this parser looks for. The deleted post
    therefore produced 54 tags belonging to whatever was on the front
    page - byte-identical to the list page's own - and they would have
    been written into the user's library as if they described their
    picture.

    The Gelbooru family (Safebooru, rule34.xxx, Xbooru) all delegate
    their tag parsing here, so all four were exposed.
    """

    LISTING = ("<html><body>"
               + '<div class="thumbnail-preview"><a href="?id=1"><img></a></div>' * 40
               + '<ul id="tag-list">'
               + '<li class="tag-type-artist"><a href="?page=post">koyama_shigeru</a></li>'
               + '<li class="tag-type-character"><a href="?page=post">cirno</a></li>'
               + "</ul></body></html>")

    POST = ('<html><body><img id="image" src="https://img.gelbooru.com/x.jpg">'
            '<ul id="tag-list">'
            '<li class="tag-type-character"><a href="?page=post">hakurei_reimu</a></li>'
            "</ul></body></html>")

    def test_a_listing_is_recognised(self):
        from core.boorus.gelbooru import looks_like_a_listing
        self.assertTrue(looks_like_a_listing(self.LISTING))
        self.assertFalse(looks_like_a_listing(self.POST))

    def test_a_listing_yields_no_tags(self):
        from core.boorus.gelbooru import parse
        self.assertEqual(parse(self.LISTING, "https://gelbooru.com/?id=6352196"), [])

    def test_a_real_post_is_untouched(self):
        from core.boorus.gelbooru import parse
        names = [t.name for t in parse(self.POST, "https://gelbooru.com/?id=1")]
        self.assertEqual(names, ["hakurei_reimu"])

    def test_both_halves_are_required(self):
        """Requiring only "no post image" would call every page whose
        markup shifted a listing and silently drop its tags; requiring
        only the grid would trip on a post showing related thumbnails."""
        from core.boorus.gelbooru import looks_like_a_listing
        # A grid AND a post image: a post, whatever else is on the page.
        self.assertFalse(looks_like_a_listing(
            '<img id="image"><div class="thumbnail-preview"></div>' * 3))
        # Neither: an unrecognised page, not a listing - its tags, if any,
        # are still its own.
        self.assertFalse(looks_like_a_listing("<html><body>nothing here</body></html>"))

    def test_one_stray_thumbnail_is_not_a_listing(self):
        from core.boorus.gelbooru import looks_like_a_listing
        self.assertFalse(looks_like_a_listing('<div class="thumbnail-preview"></div>'))

    def test_the_whole_gelbooru_family_is_covered(self):
        """They delegate tag parsing here, so the guard has to protect
        them too - it was never Gelbooru's problem alone."""
        from core.boorus import rule34, safebooru, xbooru
        for module in (rule34, safebooru, xbooru):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.parse(self.LISTING, "https://x/?id=1"), [])


class TestPahealParser(unittest.TestCase):
    """rule34.paheal.net runs Shimmie2, so no Gelbooru-family parser fits.

    Markup below is trimmed from the real post page for 5674224. Paheal
    is reached through Google Lens rather than IQDB or SauceNAO, neither
    of which indexes it.
    """

    HTML = """
    <html><body>
      <table class="tag_list"><tbody>
        <tr><td class="tag_info_link_cell">
              <a class="tag_info_link" href="https://en.wikipedia.org/wiki/Ben_10">?</a></td>
            <td class="tag_name_cell"><a class="tag_name" href="/post/list/Ben_10/1">Ben 10</a></td></tr>
        <tr><td class="tag_name_cell">
              <a class="tag_name" href="/post/list/Gwen_Tennyson/1">Gwen Tennyson</a></td></tr>
        <tr><td class="tag_name_cell">
              <a class="tag_name" href="/post/list/Flipherrrr/1">Flipherrrr</a></td></tr>
      </tbody></table>
      <img alt='main image' class='shm-main-image' id='main_image'
           src='https://r34i.paheal-cdn.net/90/46/9046769f0d21bb2c74141009d578b2cf'
           data-width='1350' data-height='1688' data-mime='image/jpeg' />
      <tr data-row='Info'><th>Info</th><td>1350x1688 // 262KB // jpg</td></tr>
    </body></html>
    """
    URL = "https://rule34.paheal.net/post/view/5674224"

    def test_it_claims_paheal_urls(self):
        from core.boorus import paheal
        self.assertTrue(paheal.matches(self.URL))
        self.assertFalse(paheal.matches("https://rule34.xxx/index.php?page=post&s=view&id=1"))

    def test_tags_use_the_canonical_underscored_name(self):
        """The link TEXT is the display form with spaces ("Gwen
        Tennyson"); the href carries the site's own canonical name
        ("Gwen_Tennyson"), which is what every other parser produces and
        what Hydrus expects."""
        from core.boorus import paheal
        names = [t.name for t in paheal.parse(self.HTML, self.URL)]
        self.assertEqual(names, ["Ben_10", "Gwen_Tennyson", "Flipherrrr"])

    def test_tags_are_unnamespaced(self):
        """Paheal's tags are flat - it has no categories. That is the
        site being simple, not a gap in this parser."""
        from core.boorus import paheal
        self.assertTrue(all(t.namespace == "" for t in paheal.parse(self.HTML, self.URL)))

    def test_the_wikipedia_info_link_is_not_a_tag(self):
        from core.boorus import paheal
        names = [t.name for t in paheal.parse(self.HTML, self.URL)]
        self.assertFalse(any("wikipedia" in n.lower() for n in names))

    def test_the_original_file_comes_off_the_cdn(self):
        from core.boorus import paheal
        self.assertEqual(
            paheal.parse_file_url(self.HTML, self.URL),
            "https://r34i.paheal-cdn.net/90/46/9046769f0d21bb2c74141009d578b2cf")

    def test_dimensions_come_from_the_image_attributes(self):
        from core.boorus import paheal
        self.assertEqual(paheal.parse_dimensions(self.HTML, self.URL), (1350, 1688))

    def test_the_format_is_taken_but_not_the_rounded_size(self):
        """The page prints "262KB" for 268,096 bytes. A size wrong by
        thousands of bytes is worse than none - it gets compared against
        the local file's exact one - so only the format is taken and the
        size is left to the HEAD request."""
        from core.boorus import paheal
        self.assertEqual(paheal.parse_file_info(self.HTML, self.URL), ("JPEG", None))

    def test_a_page_whose_markup_changed_yields_nothing_not_a_crash(self):
        from core.boorus import paheal
        self.assertEqual(paheal.parse("<html><body>nope</body></html>", self.URL), [])
        self.assertIsNone(paheal.parse_file_url("<html></html>", self.URL))
        self.assertEqual(paheal.parse_dimensions("<html></html>", self.URL), (None, None))
        self.assertEqual(paheal.parse_file_info("<html></html>", self.URL), (None, None))

    def test_it_is_registered_so_matches_actually_reach_it(self):
        """REGRESSION GUARD: a parser module that exists but was never
        added to PARSERS is a parser that silently never runs."""
        from core import boorus
        from core.boorus import paheal
        self.assertIn(paheal, boorus.PARSERS)


class TestRule34UsParser(unittest.TestCase):
    """rule34.us - unrelated to rule34.xxx. Markup trimmed from the real
    post page for 6608448, reached through a Google Lens exact match."""

    HTML = """
    <html><body>
      <ul id="tag-list " class="tag-list-left">
        <div><b>Artist</b></div><li class="artist-tag"><a href="https://rule34.us/index.php?r=posts/index&amp;q=gattles" >gattles</a> <small>615</small></li>
        <div><b>Copyright</b></div><li class="copyright-tag"><a href="https://rule34.us/index.php?r=posts/index&amp;q=cyberpunk_2077" >cyberpunk 2077</a> <small>38176</small></li>
        <div><b>Metadata</b></div><li class="metadata-tag"><a href="https://rule34.us/index.php?r=posts/index&amp;q=hi_res" >hi res</a></li>
        <div><b>Tag</b></div><li class="general-tag"><a href="https://rule34.us/index.php?r=posts/index&amp;q=bunny_ears_%28cosmetic%29" >bunny ears (cosmetic)</a></li>
      </ul>
      <a href="https://img2.rule34.us/images/cb/6f/cb6ffdde43334831d14e3dc0bf51e132.png"><li class="character-tag">Original</li></a>
      <li class="general-tag">Id: 6608448</li>
      <li class="metadata-tag">Added by: <a href="index.php?r=account/profile&id=2">Anonymous</a></li>
      <li class="general-tag">Size: 2039w x 2894h</li>
      <img src="https://img2.rule34.us/images/cb/6f/cb6ffdde43334831d14e3dc0bf51e132.png" height="2894" width="2039">
    </body></html>
    """
    URL = "https://rule34.us/index.php?r=posts/view&id=6608448"
    FILE = "https://img2.rule34.us/images/cb/6f/cb6ffdde43334831d14e3dc0bf51e132.png"

    def test_it_claims_rule34us_and_not_rule34xxx(self):
        from core.boorus import find_parser, rule34us
        self.assertIs(find_parser(self.URL), rule34us)
        self.assertIsNot(find_parser("https://rule34.xxx/index.php?page=post&s=view&id=1"), rule34us)

    def test_tags_come_from_tag_links_with_their_categories(self):
        """The info box reuses the tag li classes ("Id: ...", "Added by"),
        so only a li holding a tag-listing link is a tag."""
        from core.boorus import rule34us
        tags = [(t.namespace, t.name) for t in rule34us.parse(self.HTML, self.URL)]
        self.assertEqual(tags, [
            ("artist", "gattles"), ("copyright", "cyberpunk_2077"),
            ("meta", "hi_res"), ("general", "bunny_ears_(cosmetic)"),
        ])

    def test_the_original_is_the_file_and_the_preview(self):
        from core.boorus import rule34us
        self.assertEqual(rule34us.parse_file_url(self.HTML, self.URL), self.FILE)
        self.assertEqual(rule34us.parse_preview_url(self.HTML, self.URL), self.FILE)

    def test_dimensions_come_from_the_size_line(self):
        from core.boorus import rule34us
        self.assertEqual(rule34us.parse_dimensions(self.HTML, self.URL), (2039, 2894))


class TestRedditParser(unittest.TestCase):
    """Reddit posts through their RSS feed. FEED is a real feed (post
    1hhrx0v) with its comments replaced by one made-up entry."""

    FEED = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/"><category term=" reddit.com" label="r/ reddit.com"/><updated>2026-09-25T03:24:22+00:00</updated><id>/comments/1hhrx0v/.rss</id><link rel="self" href="https://www.reddit.com/comments/1hhrx0v/.rss" type="application/atom+xml" /><link rel="alternate" href="https://www.reddit.com/comments/1hhrx0v/" type="text/html" /><title>Fuu with Miku and Itsuki ~ : reddit.com</title><entry><author><name>/u/CruelAngel94</name><uri>https://www.reddit.com/user/CruelAngel94</uri></author><category term="5ToubunNoHanayome" label="r/5ToubunNoHanayome"/><content type="html">&lt;table&gt; &lt;tr&gt;&lt;td&gt; &lt;a href=&quot;https://www.reddit.com/r/5ToubunNoHanayome/comments/1hhrx0v/fuu_with_miku_and_itsuki/&quot;&gt; &lt;img src=&quot;https://preview.redd.it/m42sxjdyus7e1.jpeg?width=640&amp;amp;crop=smart&amp;amp;auto=webp&amp;amp;s=9513506b3c997974c7b4cdc34416f5312734a1e1&quot; alt=&quot;Fuu with Miku and Itsuki ~&quot; title=&quot;Fuu with Miku and Itsuki ~&quot; /&gt; &lt;/a&gt; &lt;/td&gt;&lt;td&gt; &amp;#32; submitted by &amp;#32; &lt;a href=&quot;https://www.reddit.com/user/CruelAngel94&quot;&gt; /u/CruelAngel94 &lt;/a&gt; &amp;#32; to &amp;#32; &lt;a href=&quot;https://www.reddit.com/r/5ToubunNoHanayome/&quot;&gt; r/5ToubunNoHanayome &lt;/a&gt; &lt;br/&gt; &lt;span&gt;&lt;a href=&quot;https://i.redd.it/m42sxjdyus7e1.jpeg&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt; &amp;#32; &lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/5ToubunNoHanayome/comments/1hhrx0v/fuu_with_miku_and_itsuki/&quot;&gt;[comments]&lt;/a&gt;&lt;/span&gt; &lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content><id>t3_1hhrx0v</id><media:thumbnail url="https://preview.redd.it/m42sxjdyus7e1.jpeg?width=640&amp;crop=smart&amp;auto=webp&amp;s=9513506b3c997974c7b4cdc34416f5312734a1e1" /><link href="https://www.reddit.com/r/5ToubunNoHanayome/comments/1hhrx0v/fuu_with_miku_and_itsuki/" /><updated>2024-12-19T12:33:52+00:00</updated><published>2024-12-19T12:33:52+00:00</published><title>Fuu with Miku and Itsuki ~</title></entry><entry><author><name>/u/someone</name></author><content type="html">&lt;p&gt;nice&lt;/p&gt;</content><id>t1_abc</id><title>/u/someone on Fuu with Miku and Itsuki ~</title></entry></feed>'
    URL = "https://www.reddit.com/r/5ToubunNoHanayome/comments/1hhrx0v/fuu_with_miku_and_itsuki/"

    def test_it_claims_posts_but_not_listings_or_users(self):
        from core.boorus import find_parser, redditpost
        for url in (self.URL, "https://old.reddit.com/r/x/comments/1hhrx0v/",
                    "https://www.reddit.com/comments/1hhrx0v", "https://redd.it/1hhrx0v"):
            with self.subTest(url=url):
                self.assertIs(find_parser(url), redditpost)
        for url in ("https://www.reddit.com/r/5ToubunNoHanayome/",
                    "https://www.reddit.com/user/someone", "https://i.redd.it/m42sxjdyus7e1.jpeg"):
            with self.subTest(url=url):
                self.assertIsNot(find_parser(url), redditpost)

    def test_the_feed_it_fetches_is_the_posts_own(self):
        from core.boorus import redditpost
        self.assertEqual(redditpost.resolve_fetch_url(self.URL),
                         "https://www.reddit.com/comments/1hhrx0v/.rss")

    def test_subreddit_and_title_become_tags_and_the_poster_does_not(self):
        from core.boorus import redditpost
        tags = [(t.namespace, t.name) for t in redditpost.parse(self.FEED, self.URL)]
        self.assertEqual(tags, [("subreddit", "5ToubunNoHanayome"),
                                ("title", "Fuu with Miku and Itsuki ~")])

    def test_the_original_and_the_preview(self):
        from core.boorus import redditpost
        self.assertEqual(redditpost.parse_file_url(self.FEED, self.URL),
                         "https://i.redd.it/m42sxjdyus7e1.jpeg")
        self.assertTrue(redditpost.parse_preview_url(self.FEED, self.URL).startswith(
            "https://preview.redd.it/m42sxjdyus7e1.jpeg?width=640&crop=smart"))

    def test_dimensions_are_read_from_the_original(self):
        from unittest.mock import patch
        from core.boorus import redditpost
        with patch("core.boorus.redditpost._remote_size.image_size",
                   return_value=(2339, 3276)) as size:
            self.assertEqual(redditpost.parse_dimensions(self.FEED, self.URL), (2339, 3276))
        self.assertEqual(size.call_args[0][0], "https://i.redd.it/m42sxjdyus7e1.jpeg")

    def test_a_gallery_is_its_first_image(self):
        """Its [link] is reddit.com/gallery/..., not a file; the preview's
        id is the first image's."""
        from core.boorus import redditpost
        feed = self.FEED.replace("https://i.redd.it/m42sxjdyus7e1.jpeg",
                                 "https://www.reddit.com/gallery/1hhrx0v")
        self.assertEqual(redditpost.parse_file_url(feed, self.URL),
                         "https://i.redd.it/m42sxjdyus7e1.jpeg")

    def test_a_link_to_elsewhere_with_no_preview_says_why(self):
        from core.boorus import redditpost
        import re as _re
        feed = self.FEED.replace("https://i.redd.it/m42sxjdyus7e1.jpeg", "https://youtu.be/x")
        feed = _re.sub(r'<media:thumbnail url="[^"]+" />', "", feed)
        self.assertIsNone(redditpost.parse_file_url(feed, self.URL))
        self.assertIn("something other than a picture",
                      redditpost.incomplete_reason(feed, self.URL))
        self.assertIsNone(redditpost.incomplete_reason(self.FEED, self.URL))

    def test_an_empty_feed_claims_nothing(self):
        from core.boorus import redditpost
        empty = '<?xml version="1.0"?><feed></feed>'
        self.assertEqual(redditpost.parse(empty, self.URL), [])
        self.assertIsNone(redditpost.parse_file_url(empty, self.URL))

    def test_requests_carry_reddits_user_agent(self):
        from core import reddit
        from core.site_access import headers_for_url
        self.assertEqual(headers_for_url(self.URL)["User-Agent"], reddit.USER_AGENT)


class TestParserContract(unittest.TestCase):
    """The registry is duck-typed, so a hook with a misspelled name is not
    an error - it is a hook that never runs, for as long as nobody
    notices. That is the same shape as the bug parser_health exists to
    detect: Safebooru, rule34 and Xbooru returning zero tags for possibly
    as long as they had been supported."""

    def test_every_parser_satisfies_the_protocol(self):
        from core.boorus import PARSERS, SiteParser
        for parser in PARSERS:
            with self.subTest(parser=parser.__name__):
                self.assertIsInstance(parser, SiteParser)

    def test_every_parser_has_the_required_members(self):
        """Spelled out separately from the Protocol check, because
        isinstance on a runtime_checkable Protocol only tests that the
        names EXIST - this says which ones and gives a better failure."""
        from core.boorus import PARSERS
        for parser in PARSERS:
            with self.subTest(parser=parser.__name__):
                self.assertTrue(callable(getattr(parser, "matches", None)))
                self.assertTrue(callable(getattr(parser, "parse", None)))

    def test_no_parser_defines_a_hook_that_is_nearly_a_real_one(self):
        """The actual failure being guarded against: a parser meaning to
        implement parse_preview_url and writing parse_preview_urls gets no
        error, no warning, and no previews - forever."""
        import difflib
        from core.boorus import OPTIONAL_PARSER_HOOKS, PARSERS
        known = set(OPTIONAL_PARSER_HOOKS) | {
            "matches", "parse", "EMPTY_RESULT_IS_NORMAL", "TAG_VERSION",
        }
        for parser in PARSERS:
            for name in dir(parser):
                if name.startswith("_") or name in known:
                    continue
                value = getattr(parser, name)
                # Only names this parser DEFINES. Shared helpers pulled in
                # from _sizes.py land in the module namespace too, and
                # parse_bare_dimensions is a genuine one sitting a couple
                # of characters from parse_dimensions.
                if getattr(value, "__module__", None) != parser.__name__:
                    continue
                close = difflib.get_close_matches(name, known, n=1, cutoff=0.85)
                if close:
                    self.fail(
                        f"{parser.__name__} defines {name!r}, suspiciously close to the "
                        f"hook {close[0]!r} - if that is a typo it would never be called"
                    )

    def test_the_optional_hook_list_matches_what_is_looked_up(self):
        """OPTIONAL_PARSER_HOOKS is documentation, and documentation that
        drifts is worse than none. Every name in it must actually be
        looked up somewhere."""
        import inspect
        from core import search_engine
        from core.boorus import OPTIONAL_PARSER_HOOKS
        import core.boorus as boorus
        sources = inspect.getsource(boorus) + inspect.getsource(search_engine)
        for hook in OPTIONAL_PARSER_HOOKS:
            with self.subTest(hook=hook):
                self.assertIn(hook, sources)


class TestParseIsGuardedLikeEveryOtherHook(unittest.TestCase):
    """parse() was the one hook called bare. A parser that raised there
    lost the whole match instead of just its tags, and one that didn't
    define it at all raised AttributeError and dropped every match for
    its site - which is exactly what happened when MangaDex's tagging was
    removed."""

    def _serve(self, parser):
        """fetch_page_info against a stubbed 200, using the given parser."""
        from unittest.mock import MagicMock, patch
        from core.boorus import fetch_page_info
        resp = MagicMock(status_code=200, text="<html></html>", encoding=None)
        resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        with patch("core.boorus.find_parser", return_value=parser), \
             patch("requests.Session.get", return_value=resp):
            return fetch_page_info("https://example.test/post/1", 5.0)

    def test_a_parser_whose_parse_raises_keeps_the_rest_of_the_match(self):
        import types
        parser = types.ModuleType("exploding")
        parser.matches = lambda url: True

        def boom(body, url):
            raise RuntimeError("markup moved")
        parser.parse = boom
        parser.parse_file_url = lambda body, url: "https://cdn.test/full.jpg"

        info = self._serve(parser)
        self.assertEqual(info.tags, [])
        self.assertEqual(info.file_url, "https://cdn.test/full.jpg",
                         "a tag parsing failure must not cost the file URL")

    def test_a_parser_with_no_parse_at_all_still_yields_its_match(self):
        import types
        parser = types.ModuleType("tagless")
        parser.matches = lambda url: True
        parser.parse_file_url = lambda body, url: "https://cdn.test/full.jpg"

        info = self._serve(parser)
        self.assertEqual(info.tags, [])
        self.assertEqual(info.file_url, "https://cdn.test/full.jpg")

    def test_a_parser_returning_none_is_treated_as_no_tags(self):
        import types
        parser = types.ModuleType("noney")
        parser.matches = lambda url: True
        parser.parse = lambda body, url: None
        self.assertEqual(self._serve(parser).tags, [])


class TestMultipageResolverIsNotPixivOnly(unittest.TestCase):
    """The resolver asks "which of these images is the one I have?", and
    hashing does not care what site the pages came from. It used to be
    hardwired to Pixiv; any parser exposing pages_api_url and parse_pages
    now gets it."""

    def test_mangadex_offers_the_page_list_hooks(self):
        from core.boorus import mangadex
        self.assertTrue(callable(getattr(mangadex, "pages_api_url", None)))
        self.assertTrue(callable(getattr(mangadex, "parse_pages", None)))

    def test_twitter_offers_the_page_list_hooks(self):
        """REGRESSION GUARD (DAN-57): it did not, so a four-photo tweet
        whose fourth photo was the local file was shown as its first."""
        from core.boorus import twitter
        self.assertTrue(callable(getattr(twitter, "pages_api_url", None)))
        self.assertTrue(callable(getattr(twitter, "parse_pages", None)))

    def test_pixiv_still_offers_them(self):
        from core.boorus import pixiv
        self.assertTrue(callable(getattr(pixiv, "pages_api_url", None)))
        self.assertTrue(callable(getattr(pixiv, "parse_pages", None)))

    def test_the_resolver_no_longer_hardcodes_pixiv_for_the_page_list(self):
        """REGRESSION GUARD: it called pixiv.pages_api_url directly, so
        every other site's multi-page post stayed on page 1."""
        import inspect
        from core import search_engine
        src = inspect.getsource(search_engine._resolve_multipage_candidate)
        self.assertNotIn("pixiv.pages_api_url(", src)
        self.assertNotIn("pixiv.parse_pages(", src)
        self.assertIn("find_parser(candidate.url)", src)

    def test_saucenaos_page_hint_stays_pixiv_only(self):
        """Its thumbnail names the page in Pixiv's format specifically.
        Reading another site's thumbnail through it would invent a page
        number out of nothing."""
        import inspect
        from core import search_engine
        src = inspect.getsource(search_engine._resolve_multipage_candidate)
        self.assertIn("parser is pixiv", src)

    def test_a_parser_without_the_hooks_is_left_alone(self):
        """Most sites are single-image, and must fall straight through."""
        from core.boorus import danbooru
        self.assertIsNone(getattr(danbooru, "pages_api_url", None))



class TestSankakuExplainsAnEmptyResult(unittest.TestCase):
    """A hash miss and a broken parser look identical from the outside -
    a match with no tags. 23 of 42 Sankaku lookups in one real session
    missed, and none of them said why."""

    URL = "https://chan.sankakucomplex.com/en/posts/4604859"

    def test_a_hash_miss_says_the_file_is_not_byte_identical(self):
        from core.boorus import sankaku
        reason = sankaku.incomplete_reason("[]", self.URL)
        self.assertIn("byte-identical", reason)
        self.assertIn("re-encode", reason)

    def test_it_says_the_numeric_id_is_not_a_fallback(self):
        """Verified against the live API: /posts/<numeric> answers
        "invalid id" and legacy_id searches return nothing. Someone
        reading the message should not go looking for that route."""
        from core.boorus import sankaku
        self.assertIn("no longer resolve", sankaku.incomplete_reason("[]", self.URL))

    def test_no_hash_to_search_with_is_a_different_message(self):
        """The legacy HTML page, fetched because there was no md5. A
        different problem with a different fix, so not the same wording."""
        from core.boorus import sankaku
        reason = sankaku.incomplete_reason("<html>not json</html>", self.URL)
        self.assertIn("No local file hash", reason)

    def test_a_post_that_is_simply_untagged_gets_no_excuse(self):
        """Finding the post and finding it has no tags is not a miss, and
        inventing a reason would be wrong."""
        from core.boorus import sankaku
        body = json.dumps([{"id": "abc", "tags": []}])
        self.assertIsNone(sankaku.incomplete_reason(body, self.URL))

    def test_an_empty_body_is_still_explained(self):
        from core.boorus import sankaku
        self.assertTrue(sankaku.incomplete_reason("", self.URL))

    def test_the_reason_reaches_the_page_info(self):
        """The hook is optional and looked up by name, so this pins that
        fetch_page_info actually calls it."""
        from unittest.mock import MagicMock, patch
        from core.boorus import fetch_page_info, sankaku
        resp = MagicMock(status_code=200, text="[]", encoding=None)
        resp.headers = {"Content-Type": "application/json"}
        with patch("core.boorus.find_parser", return_value=sankaku), \
             patch("requests.Session.get", return_value=resp):
            info = fetch_page_info(self.URL, 5.0)
        self.assertEqual(info.tags, [])
        self.assertIn("byte-identical", info.incomplete_reason)

    def test_a_parser_without_the_hook_simply_has_no_reason(self):
        from unittest.mock import MagicMock, patch
        from core.boorus import danbooru, fetch_page_info
        resp = MagicMock(status_code=200, text="{}", encoding=None)
        resp.headers = {"Content-Type": "application/json"}
        with patch("core.boorus.find_parser", return_value=danbooru), \
             patch("requests.Session.get", return_value=resp):
            info = fetch_page_info("https://danbooru.donmai.us/posts/1", 5.0)
        self.assertIsNone(info.incomplete_reason)

    def test_a_reason_is_not_asked_for_when_there_ARE_tags(self):
        """It is only ever an explanation for an empty result."""
        from unittest.mock import MagicMock, patch
        from core.boorus import fetch_page_info, sankaku
        body = json.dumps([{"id": "abc", "tags": [{"name": "cat", "type": 0}]}])
        resp = MagicMock(status_code=200, text=body, encoding=None)
        resp.headers = {"Content-Type": "application/json"}
        with patch("core.boorus.find_parser", return_value=sankaku), \
             patch("requests.Session.get", return_value=resp):
            info = fetch_page_info(self.URL, 5.0)
        self.assertTrue(info.tags)
        self.assertIsNone(info.incomplete_reason)

    def test_a_hook_that_raises_does_not_cost_the_match(self):
        import types
        from unittest.mock import MagicMock, patch
        from core.boorus import fetch_page_info
        parser = types.ModuleType("explodes")
        parser.matches = lambda url: True
        parser.parse = lambda body, url: []

        def boom(body, url):
            raise RuntimeError("nope")
        parser.incomplete_reason = boom
        parser.parse_file_url = lambda body, url: "https://cdn/x.jpg"
        resp = MagicMock(status_code=200, text="{}", encoding=None)
        resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        with patch("core.boorus.find_parser", return_value=parser), \
             patch("requests.Session.get", return_value=resp):
            info = fetch_page_info("https://example.test/1", 5.0)
        self.assertIsNone(info.incomplete_reason)
        self.assertEqual(info.file_url, "https://cdn/x.jpg")


class TestBorrowingTagsFromAnotherMatch(unittest.TestCase):
    """MangaDex and Sankaku cannot supply tags at all - by design and by
    hash-miss respectively - and were 94% of the untagged matches measured
    on a real library. Another site's copy of the same picture is usually
    right there in the results with a full tag list."""

    def _candidate(self, url, similarity, tags=None, fetched=False, available=None):
        from core.models import MatchCandidate, Tag, TagSource
        return MatchCandidate(
            url=url, similarity=similarity,
            booru_tags=[Tag(t, TagSource.BOORU) for t in (tags or [])],
            booru_tags_fetched=fetched, remote_available=available,
        )

    def _settings(self, slack=5.0, on=True):
        s = Settings()
        s.borrow_tags_from_other_matches = on
        s.borrow_tags_similarity_slack = slack
        return s

    def _entry(self):
        from core.models import ImageEntry
        return ImageEntry(path="/tmp/b.png")

    def _borrow(self, candidates, settings, chosen=0):
        """Runs the borrow, faking the page fetch as a no-op - candidates
        carry whatever tags the test gave them."""
        from core.tag_borrowing import borrow_tags
        fetched = []
        def fake_fetch(c):
            fetched.append(c.url)
            c.booru_tags_fetched = True
        result = borrow_tags(self._entry(), candidates, chosen, settings, fake_fetch)
        return result, fetched

    def test_a_close_enough_match_lends_its_tags(self):
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        alt = self._candidate("https://gelbooru.com/1", 91.0, ["cat", "hat"], fetched=True)
        lender, _ = self._borrow([top, alt], self._settings())
        self.assertEqual(lender, alt.url)
        self.assertEqual([t.name for t in top.booru_tags], ["cat", "hat"])

    def test_the_chosen_match_stays_chosen(self):
        """It was picked as the best PICTURE; only its tags come from
        elsewhere."""
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        alt = self._candidate("https://gelbooru.com/1", 91.0, ["cat"], fetched=True)
        candidates = [top, alt]
        self._borrow(candidates, self._settings())
        self.assertIs(candidates[0], top)

    def test_where_the_tags_came_from_is_recorded(self):
        """Letting them pass as the chosen match's own would misreport
        the tag list."""
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        alt = self._candidate("https://gelbooru.com/1", 91.0, ["cat"], fetched=True)
        self._borrow([top, alt], self._settings())
        self.assertEqual(top.tags_borrowed_from, "https://gelbooru.com/1")

    def test_a_much_weaker_match_may_not_lend(self):
        """The whole guard: a distant match may be a different picture,
        and its tags would be worse than none."""
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        alt = self._candidate("https://gelbooru.com/1", 62.0, ["wrong"], fetched=True)
        lender, _ = self._borrow([top, alt], self._settings(slack=5.0))
        self.assertIsNone(lender)
        self.assertEqual(top.booru_tags, [])

    def test_zero_slack_allows_only_an_equal_or_better_match(self):
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        near = self._candidate("https://gelbooru.com/1", 92.9, ["cat"], fetched=True)
        lender, _ = self._borrow([top, near], self._settings(slack=0.0))
        self.assertIsNone(lender)

        equal = self._candidate("https://gelbooru.com/2", 93.0, ["cat"], fetched=True)
        lender, _ = self._borrow([self._candidate("https://mangadex.org/c", 93.0), equal],
                                 self._settings(slack=0.0))
        self.assertEqual(lender, equal.url)

    def test_a_match_with_its_own_tags_borrows_nothing(self):
        top = self._candidate("https://danbooru.donmai.us/1", 93.0, ["own"], fetched=True)
        alt = self._candidate("https://gelbooru.com/1", 93.0, ["other"], fetched=True)
        lender, fetched = self._borrow([top, alt], self._settings())
        self.assertIsNone(lender)
        self.assertEqual(fetched, [], "it must not spend a fetch it does not need")

    def test_a_dead_match_is_not_asked(self):
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        gone = self._candidate("https://gelbooru.com/1", 93.0, available=False)
        lender, fetched = self._borrow([top, gone], self._settings())
        self.assertIsNone(lender)
        self.assertEqual(fetched, [])

    def test_a_candidate_with_no_similarity_is_skipped(self):
        """An unknown is not treated as good enough - that is how an
        unrelated picture's tags get attached."""
        from core.models import MatchCandidate, Tag, TagSource
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        unknown = MatchCandidate(url="https://x/1", similarity=None, booru_tags_fetched=True,
                                 booru_tags=[Tag("cat", TagSource.BOORU)])
        lender, _ = self._borrow([top, unknown], self._settings())
        self.assertIsNone(lender)

    def test_the_strongest_eligible_match_is_asked_first(self):
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        weaker = self._candidate("https://gelbooru.com/weak", 90.0, ["a"], fetched=True)
        stronger = self._candidate("https://gelbooru.com/strong", 92.0, ["b"], fetched=True)
        lender, _ = self._borrow([top, weaker, stronger], self._settings())
        self.assertEqual(lender, stronger.url)

    def test_only_one_alternative_is_fetched(self):
        """Rescuing a tagless match must not turn one search into a chain
        of page fetches."""
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        a = self._candidate("https://gelbooru.com/1", 92.0)   # unfetched, will have no tags
        b = self._candidate("https://gelbooru.com/2", 91.0)
        lender, fetched = self._borrow([top, a, b], self._settings())
        self.assertIsNone(lender)
        self.assertEqual(len(fetched), 2, "it walks the eligible ones but fetches each once")

    def test_the_setting_turns_it_off(self):
        top = self._candidate("https://mangadex.org/chapter/x", 93.0)
        alt = self._candidate("https://gelbooru.com/1", 93.0, ["cat"], fetched=True)
        lender, fetched = self._borrow([top, alt], self._settings(on=False))
        self.assertIsNone(lender)
        self.assertEqual(fetched, [])

    def test_a_chosen_match_with_no_similarity_lends_nothing(self):
        """Nothing to measure against, so nothing may be compared to it."""
        from core.models import MatchCandidate
        top = MatchCandidate(url="https://mangadex.org/c", similarity=None)
        alt = self._candidate("https://gelbooru.com/1", 93.0, ["cat"], fetched=True)
        lender, _ = self._borrow([top, alt], self._settings())
        self.assertIsNone(lender)


class TestParsedOnce(unittest.TestCase):
    """Every hook of a parser reads one parse of the page."""

    def test_one_soup_per_page_across_every_hook(self):
        from unittest.mock import patch as _patch
        import bs4
        from core.boorus import rule34us
        html = TestRule34UsParser.HTML + "<!-- %s -->" % id(self)   # a body no other test used
        real = bs4.BeautifulSoup
        with _patch("core.boorus._parsed.BeautifulSoup", side_effect=real) as built:
            rule34us.parse(html, TestRule34UsParser.URL)
            rule34us.parse_file_url(html, TestRule34UsParser.URL)
            rule34us.parse_preview_url(html, TestRule34UsParser.URL)
            rule34us.parse_dimensions(html, TestRule34UsParser.URL)
        self.assertEqual(built.call_count, 1)

    def test_json_is_decoded_once_and_a_bad_body_keeps_its_error_type(self):
        import json as _json
        from unittest.mock import patch as _patch
        from core.boorus._parsed import json_of
        body = '{"id": %d}' % id(self)
        with _patch("core.boorus._parsed.json.loads", side_effect=_json.loads) as loads:
            self.assertEqual(json_of(body), json_of(body))
        self.assertEqual(loads.call_count, 1)
        for _ in range(2):
            with self.assertRaises(_json.JSONDecodeError):
                json_of("not json %d" % id(self))

    def test_the_page_list_reuses_the_record_just_fetched(self):
        """Pawchive's page list IS its post API record - fetching it again
        was a second request for a body already in hand."""
        from unittest.mock import patch as _patch
        from core import search_engine
        from core.boorus import BooruPageInfo
        from core.config import Settings
        from core.models import MatchCandidate
        url = "https://pawchive.pw/patreon/user/1/post/2"
        api = "https://pawchive.pw/api/v1/patreon/user/1/post/2"
        record = ('{"id": "2", "has_full": true, "file": {}, "attachments": ['
                  '{"name": "a.jpg", "path": "/aa/bb/' + "a" * 64 + '.jpg"},'
                  '{"name": "b.jpg", "path": "/cc/dd/' + "b" * 64 + '.jpg"}]}')
        info = BooruPageInfo(page_count=2, fetched_url=api, body=record, fetched=True)
        candidate = MatchCandidate(url=url, source_name="Pawchive", similarity=90.0, engine="x")
        with _patch.object(search_engine.remote, "fetch_text") as fetch:
            search_engine._resolve_multipage_candidate(candidate, 2, Settings(), "/nonexistent",
                                                       info)
        fetch.assert_not_called()


# CONFIRMED live capture of https://reactor.cc/post/5351071 (DAN-344), trimmed
# to the taglist, the post's own image, and the start of the "Еще на тему"
# ("more on this topic") related-posts widget that follows it on the real
# page - kept specifically so a parser change that widened its tag match
# past the `<strong class="taglist">` block would pick up THAT widget's
# entries too and fail test_does_not_pick_up_the_related_posts_widget below.
JOYREACTOR_POST_HTML = (
    '<strong class="taglist"><b><a title="Rebecca (Edgerunners)" '
    'data-ids="2431870,2026302,105865,753" data-tag-id="2431870" '
    'href="https://reactor.cc/tag/Rebecca+%28Edgerunners%29">Rebecca (Edgerunners)</a>'
    '&nbsp;</b><b><a title="Cyberpunk Edgerunners" data-ids="2026302,105865,753" '
    'data-tag-id="2026302" href="https://reactor.cc/tag/Cyberpunk+Edgerunners">'
    'Cyberpunk Edgerunners</a>&nbsp;</b><b><a title="Cyberpunk 2077" '
    'data-ids="105865,753" data-tag-id="105865" '
    'href="https://reactor.cc/tag/Cyberpunk+2077">Cyberpunk 2077</a>&nbsp;</b>'
    '<b><a title="Игры" data-ids="753" data-tag-id="753" '
    'href="https://reactor.cc/tag/%D0%98%D0%B3%D1%80%D1%8B">Игры</a>'
    '&nbsp;</b><b><a title="Игровая эротика" '
    'data-ids="622267,753" data-tag-id="622267" '
    'href="https://reactor.cc/tag/%D0%98%D0%B3%D1%80%D0%BE%D0%B2%D0%B0%D1%8F+%D1%8D%D1%80%D0%BE%D1%82%D0%B8%D0%BA%D0%B0">'
    'Игровая эротика</a>&nbsp;</b>'
    '<b><a title="celtisart" data-ids="1676753" data-tag-id="1676753" '
    'href="https://reactor.cc/tag/celtisart">celtisart</a>&nbsp;</b></strong>'
    '<div class="post_content"><div><p><div class="image">'
    '<a href="//img10.reactor.cc/pics/post/full/Rebecca-%28Edgerunners%29-Cyberpunk-'
    'Edgerunners-Cyberpunk-2077-%D0%98%D0%B3%D1%80%D1%8B-7630589.jpeg" '
    'class="prettyPhotoLink" rel="prettyPhoto">'
    '<img src="//img10.reactor.cc/pics/post/Rebecca-%28Edgerunners%29-Cyberpunk-'
    'Edgerunners-Cyberpunk-2077-%D0%98%D0%B3%D1%80%D1%8B-7630589.jpeg" width="811" '
    'height="1120" alt="Rebecca (Edgerunners),Cyberpunk Edgerunners,Cyberpunk 2077,'
    'Игры,Игровая эротика,celtisart" '
    'title="Rebecca (Edgerunners),Cyberpunk Edgerunners,Cyberpunk 2077,'
    'Игры,Игровая эротика,celtisart"/>'
    '</a></div></p></div><div class="image"><br/>'
    '<a href="javascript:" class="more_link">Подробнее</a>'
    '<span class="more_content"><br/><br/> Rebecca (Edgerunners),Cyberpunk Edgerunners,'
    'Cyberpunk 2077,Игры,Игровая эротика,'
    'celtisart </span></div>'
    '<div class="mainheader">Еще на тему</div>'
    '<div class="blog_results"><div class="blog_pic_results">'
    '<div class="blog_pic_result"><a href="/tag/Rebecca+%28Edgerunners%29">'
    '<img src="//img2.reactor.cc/pics/avatar/tag/2431870" alt="Rebecca (Edgerunners)"/></a>'
    '<h3><a href="/tag/Rebecca+%28Edgerunners%29"> Rebecca (Edgerunners)(1446) </a></h3></div>'
    '<div class="blog_pic_result"><a href="/tag/Cyberpunk+Edgerunners">'
    '<img src="//img2.reactor.cc/pics/avatar/tag/2026302" alt="Cyberpunk Edgerunners"/></a>'
    '<h3><a href="/tag/Cyberpunk+Edgerunners"> Cyberpunk Edgerunners(2590) </a></h3></div>'
)

JOYREACTOR_URL = "https://reactor.cc/post/5351071"
JOYREACTOR_TAG_NAMES = [
    "Rebecca (Edgerunners)", "Cyberpunk Edgerunners", "Cyberpunk 2077",
    "Игры", "Игровая эротика",
    "celtisart",
]
JOYREACTOR_FULL_IMAGE_URL = (
    "https://img10.reactor.cc/pics/post/full/Rebecca-%28Edgerunners%29-Cyberpunk-"
    "Edgerunners-Cyberpunk-2077-%D0%98%D0%B3%D1%80%D1%8B-7630589.jpeg"
)

# CONFIRMED shape of a live capture of https://reactor.cc/post/5300000
# (DAN-480): the post's own media is a <video class="video_gif"> (an
# animated GIF/webm/mp4), which has no prettyPhotoLink of its own, followed
# by id="comment_list" where a commenter's own attached image IS wrapped in
# the same <a ... class="prettyPhotoLink"> markup a post's image would be.
# An unscoped search over the whole page falls through to that comment's
# image and hands it back as if it were the post's file.
JOYREACTOR_GIF_POST_HTML = (
    '<strong class="taglist"><b><a title="Ирландские Танцы" '
    'data-ids="1" data-tag-id="1" href="https://reactor.cc/tag/dance">'
    'Ирландские Танцы</a>&nbsp;</b></strong>'
    '<div class="post_content"><div><p><div class="image">'
    '<video class="video_gif" autoplay loop muted>'
    '<source src="//img2.reactor.cc/pics/post/full/dance-5300000.webm" type="video/webm"/>'
    '<source src="//img2.reactor.cc/pics/post/full/dance-5300000.mp4" type="video/mp4"/>'
    '</video></div></p></div></div>'
    '<div id="comment_list">'
    '<div class="comment" id="comment26174819">'
    '<div class="image"><a href="//img2.reactor.cc/pics/comment/full/'
    'unrelated-4660114.jpeg" class="prettyPhotoLink" rel="prettyPhoto">'
    '<img src="//img2.reactor.cc/pics/comment/unrelated-4660114.jpeg"/>'
    '</a></div></div></div>'
)
JOYREACTOR_GIF_URL = "https://reactor.cc/post/5300000"

# Same shape, but the post itself DOES carry its own prettyPhotoLink image -
# pins that scoping to before id="comment_list" still returns the post's
# own image rather than merely happening to land on it by match order.
JOYREACTOR_POST_WITH_TRAILING_COMMENT_IMAGE_HTML = (
    '<strong class="taglist"></strong>'
    '<div class="post_content"><div><p><div class="image">'
    '<a href="//img10.reactor.cc/pics/post/full/own-image.jpeg" '
    'class="prettyPhotoLink" rel="prettyPhoto">'
    '<img src="//img10.reactor.cc/pics/post/own-image.jpeg"/>'
    '</a></div></p></div></div>'
    '<div id="comment_list">'
    '<div class="comment" id="comment1">'
    '<div class="image"><a href="//img2.reactor.cc/pics/comment/full/'
    'unrelated.jpeg" class="prettyPhotoLink" rel="prettyPhoto">'
    '<img src="//img2.reactor.cc/pics/comment/unrelated.jpeg"/>'
    '</a></div></div></div>'
)


class TestJoyreactorParser(unittest.TestCase):
    """reactor.cc/joyreactor.com/joyreactor.cc, built from a live capture
    of reactor.cc/post/5351071 (DAN-344) - the post DAN-308/DAN-342 showed
    Google Lens finding correctly, which then got dropped from the final
    result list purely for having no boorus/ parser to make it taggable."""

    def test_matches_all_three_hosts_with_a_post_id(self):
        from core.boorus import joyreactor
        for host in ("reactor.cc", "joyreactor.com", "joyreactor.cc", "www.reactor.cc"):
            with self.subTest(host=host):
                self.assertTrue(joyreactor.matches(f"https://{host}/post/5351071"))

    def test_does_not_match_a_non_post_url_on_the_same_host(self):
        from core.boorus import joyreactor
        self.assertFalse(joyreactor.matches("https://reactor.cc/tag/celtisart"))
        self.assertFalse(joyreactor.matches("https://reactor.cc/"))

    def test_does_not_match_an_unrelated_host(self):
        from core.boorus import joyreactor
        self.assertFalse(joyreactor.matches("https://pinterest.com/post/5351071"))

    def test_extracts_every_real_tag_in_order(self):
        from core.boorus import joyreactor
        tags = joyreactor.parse(JOYREACTOR_POST_HTML, JOYREACTOR_URL)
        self.assertEqual([t.name for t in tags], JOYREACTOR_TAG_NAMES)
        self.assertTrue(all(t.source == TagSource.BOORU for t in tags))

    def test_does_not_pick_up_the_related_posts_widget(self):
        """The fixture's trailing 'Еще на тему' block names the same tags
        again, outside </strong> - this pins that parse() stops at the
        taglist element rather than scanning the whole page for
        data-tag-id, which would double every tag."""
        from core.boorus import joyreactor
        tags = joyreactor.parse(JOYREACTOR_POST_HTML, JOYREACTOR_URL)
        self.assertEqual(len(tags), len(JOYREACTOR_TAG_NAMES))

    def test_no_taglist_yields_no_tags_without_raising(self):
        from core.boorus import joyreactor
        self.assertEqual(joyreactor.parse("<html></html>", JOYREACTOR_URL), [])
        self.assertEqual(joyreactor.parse("", JOYREACTOR_URL), [])

    def test_file_url_is_the_clean_full_resolution_copy_not_the_watermarked_one(self):
        """The embedded <img> src is the watermarked display copy; the
        <a class=prettyPhotoLink> it sits inside links the clean full/
        original - CONFIRMED as the live pair on this real post."""
        from core.boorus import joyreactor
        file_url = joyreactor.parse_file_url(JOYREACTOR_POST_HTML, JOYREACTOR_URL)
        self.assertEqual(file_url, JOYREACTOR_FULL_IMAGE_URL)
        self.assertNotIn("/pics/post/full/", "")  # sanity: constant isn't empty
        self.assertIn("/pics/post/full/", file_url)

    def test_does_not_mistake_the_more_toggle_for_a_second_image(self):
        """The fixture's second <div class="image"> is the 'Подробнее'
        (more) text toggle, not another picture - parse_file_url must
        still return the one real image, not None or a wrong match."""
        from core.boorus import joyreactor
        self.assertEqual(joyreactor.parse_file_url(JOYREACTOR_POST_HTML, JOYREACTOR_URL),
                         JOYREACTOR_FULL_IMAGE_URL)

    def test_no_image_yields_none_without_raising(self):
        from core.boorus import joyreactor
        self.assertIsNone(joyreactor.parse_file_url("<html></html>", JOYREACTOR_URL))

    def test_an_already_absolute_href_is_passed_through_unchanged(self):
        """Every other fixture's prettyPhotoLink href is protocol-relative
        ("//..."), so the branch that leaves an already-absolute URL alone
        was never exercised (97% branch coverage, missing 82->84 before
        this test). Pins that the "https:" + url rewrite is conditional,
        not unconditionally prepended."""
        from core.boorus import joyreactor
        body = (
            '<strong class="taglist"></strong>'
            '<a href="https://img10.reactor.cc/pics/post/full/already-absolute.jpeg" '
            'class="prettyPhotoLink" rel="prettyPhoto">'
            '<img src="https://img10.reactor.cc/pics/post/already-absolute.jpeg"/></a>'
        )
        self.assertEqual(
            joyreactor.parse_file_url(body, JOYREACTOR_URL),
            "https://img10.reactor.cc/pics/post/full/already-absolute.jpeg")

    def test_video_post_does_not_return_a_commenters_image_as_its_file(self):
        """REGRESSION (DAN-480): the post's own media is a <video> with no
        prettyPhotoLink of its own; the only prettyPhotoLink on the whole
        page belongs to a comment's attached image, inside
        id="comment_list". Before the fix, the unscoped search fell
        through to that comment's image and returned a stranger's picture
        as the post's "file". FAILS against the parent commit (returns the
        comment image's URL instead of None)."""
        from core.boorus import joyreactor
        self.assertIsNone(
            joyreactor.parse_file_url(JOYREACTOR_GIF_POST_HTML, JOYREACTOR_GIF_URL))

    def test_post_with_its_own_image_ignores_a_later_comment_image(self):
        """Pins that the fix scopes to BEFORE id="comment_list" rather than
        merely happening to match the post's own anchor first - the post's
        own prettyPhotoLink must win even though a comment's prettyPhotoLink
        also exists later on the same page."""
        from core.boorus import joyreactor
        file_url = joyreactor.parse_file_url(
            JOYREACTOR_POST_WITH_TRAILING_COMMENT_IMAGE_HTML, JOYREACTOR_URL)
        self.assertEqual(file_url, "https://img10.reactor.cc/pics/post/full/own-image.jpeg")

    def test_registered_and_found_by_find_parser(self):
        """The actual fix this ticket is about: a reactor.cc match now
        resolves to a parser at all, which is what makes
        google_lens._is_taggable (and keep_taggable_first) treat it as
        taggable instead of dropping it for Pinterest/DeviantArt/Reddit
        noise - see TestKeepTaggableFirstSurvivesReactorMatch below."""
        from core.boorus import find_parser, joyreactor
        self.assertIs(find_parser(JOYREACTOR_URL), joyreactor)


def _make_match(url: str):
    from core.google_lens import GoogleLensMatch
    return GoogleLensMatch(url=url, thumb_url=None, similarity=50.0, title=url)


class TestKeepTaggableFirstSurvivesReactorMatch(unittest.TestCase):
    """DAN-344's actual fix - the reactor.cc match is now taggable at all -
    is necessary, but NOT sufficient on its own to satisfy the ticket's
    acceptance criterion ("this match survives to the final result
    list"), for the exact real post DAN-342 captured. Both tests below
    replay the same regression shape; only the number of OTHER taggable
    matches ranked ahead of reactor's position differs, which is the
    variable that decides the outcome."""

    def test_becoming_taggable_is_enough_when_fewer_than_8_already_rank_ahead(self):
        """REGRESSION: before this ticket's parser, find_parser(reactor_url)
        was None, so _is_taggable was False and keep_taggable_first never
        considered this match taggable regardless of how few competitors
        existed - it only ever got a slot by raw rank, same as any other
        untaggable result. FAILS against the parent commit (no joyreactor
        parser); passes with the parser above, for the common case where
        the taggable pool doesn't already fill the cap."""
        from core.google_lens import keep_taggable_first, taggable_first

        taggable_sites = ["https://www.pixiv.net/artworks/{}".format(i) for i in range(3)]
        untaggable_noise = ["https://www.pinterest.com/pin/{}".format(i) for i in range(20)]
        matches = ([_make_match(u) for u in taggable_sites]
                   + [_make_match(u) for u in untaggable_noise]
                   + [_make_match(JOYREACTOR_URL)])

        kept = keep_taggable_first(taggable_first(matches), 8)

        self.assertIn(JOYREACTOR_URL, [m.url for m in kept])

    def test_becoming_taggable_now_rescues_a_visual_match_behind_8_taggable_exacts(self):
        """DAN-357 closes the gap this test used to pin. Replayed from the
        exact shape DAN-342's live capture recorded: reactor.cc/post/5351071
        is a taggable VISUAL match ranked behind 8 already-taggable EXACT
        matches (Pixiv, DeviantArt) that alone fill MAX_RESULTS=8. Before
        DAN-357, `matches = taggable_first(exact) + taggable_first(visual)`
        put every exact match ahead of every visual one, so
        keep_taggable_first's `taggable[:limit]` was always 8 exact
        matches and reactor - a VISUAL match - could never survive, at any
        rank. RESERVED_VISUAL_SLOTS fixes that structurally: a couple of
        slots are set aside for the best-ranked taggable visual matches,
        so this one now survives instead of losing its slot to a
        same-group competitor with nothing to do with it."""
        from core.google_lens import (RESERVED_VISUAL_SLOTS, keep_taggable_first,
                                       taggable_first)

        taggable_exact_sites = (["https://www.pixiv.net/artworks/{}".format(i) for i in range(4)]
                          + ["https://www.deviantart.com/x/art/y-{}".format(i) for i in range(4)])
        untaggable_noise = ["https://www.pinterest.com/pin/{}".format(i) for i in range(50)]
        exact = ([_make_match(u) for u in taggable_exact_sites]
                 + [_make_match(u) for u in untaggable_noise[:25]])
        visual = ([_make_match(JOYREACTOR_URL)]
                  + [_make_match(u) for u in untaggable_noise[25:]])
        self.assertEqual(len(exact) + len(visual), 59)

        matches = taggable_first(exact) + taggable_first(visual)
        kept = keep_taggable_first(matches, 8, visual_start=len(exact),
                                    reserve_visual=RESERVED_VISUAL_SLOTS)

        self.assertIn(JOYREACTOR_URL, [m.url for m in kept],
                       "reactor.cc/post/5351071 (DAN-308/DAN-342) must survive now "
                       "that a visual slot is reserved - if this fails, the "
                       "reservation isn't reaching the exact/visual boundary")
        self.assertEqual(len(kept), 8)
        self.assertTrue(all(_is_taggable_url(m.url) for m in kept))

    def test_reservation_leaves_the_under_cap_case_unchanged(self):
        """Blast-radius pin: RESERVED_VISUAL_SLOTS must only bite once the
        taggable pool already exceeds `limit`. Here only 4 matches are
        taggable (well under the limit of 8), so every taggable match is
        kept with or without a reservation - the fill-the-rest behaviour
        for untaggable matches must also be identical either way."""
        from core.google_lens import (RESERVED_VISUAL_SLOTS, keep_taggable_first,
                                       taggable_first)

        exact = ([_make_match("https://www.pixiv.net/artworks/{}".format(i)) for i in range(3)]
                 + [_make_match("https://www.pinterest.com/pin/{}".format(i)) for i in range(10)])
        visual = ([_make_match(JOYREACTOR_URL)]
                  + [_make_match("https://www.pinterest.com/pin/v{}".format(i)) for i in range(10)])
        matches = taggable_first(exact) + taggable_first(visual)
        self.assertEqual(len(matches), 24)

        without_reservation = keep_taggable_first(matches, 8)
        with_reservation = keep_taggable_first(matches, 8, visual_start=len(exact),
                                                reserve_visual=RESERVED_VISUAL_SLOTS)

        self.assertEqual([m.url for m in with_reservation], [m.url for m in without_reservation])


def _is_taggable_url(url: str) -> bool:
    from core import boorus
    return boorus.find_parser(url) is not None
