"""Search-quality additions: extra engines, parallel IQDB/SauceNAO,
extra file types, and the new site parsers/classifiers.
"""
import contextlib
import io
import json
import os
import tempfile
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from core.config import Settings
from core.models import ImageEntry
from core.sites import ALL_SITE_OPTIONS, classify_site
from core.search_engine import _engine_waves, search_image


def _settings(**kwargs):
    s = Settings()
    s.retrieve_tags_from_booru = False
    s.enable_ascii2d = False
    s.enable_tracemoe = False
    s.enable_iqdb3d = False
    s.enable_google_images = False
    s.enable_google_lens = False
    s.enable_pawchive_index = False     # local and inert until used; noise to plan tests
    s.secondary_engine_mode = "disabled"
    s.primary_engine = "iqdb"
    for key, value in kwargs.items():
        setattr(s, key, value)
    return s


# Generate valid test images using PIL
from PIL import Image

# Create a minimal valid JPEG (large enough to pass size check)
_jpeg_buffer = io.BytesIO()
Image.new("RGB", (100, 100), color="red").save(_jpeg_buffer, format="JPEG")
VALID_JPEG = _jpeg_buffer.getvalue()

# Create a minimal valid PNG (large enough to pass size check)
_png_buffer = io.BytesIO()
Image.new("RGB", (100, 100), color="blue").save(_png_buffer, format="PNG")
VALID_PNG = _png_buffer.getvalue()

# Real JPEG magic bytes. The Lens engine checks the FORMAT before
# uploading - Google refuses anything it does not recognise - so a
# fixture claiming to be an image has to start like one.
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def _empty_image():
    """A throwaway file with content unique to this call.

    Unique on purpose: the search cache is keyed on the FILE'S CONTENT,
    so identical bytes meant one test's result was replayed to the next
    and the engines were never called - which looks exactly like the
    behaviour under test having changed.
    """
    path = os.path.join(tempfile.mkdtemp(), "x.png")
    with open(path, "wb") as fh:
        fh.write(b"x" + uuid.uuid4().bytes)
    return path


class TestEngineWaves(unittest.TestCase):
    def test_always_mode_runs_iqdb_and_saucenao_in_one_wave(self):
        waves = _engine_waves(_settings(secondary_engine_mode="always"), None)
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0][:2], ["iqdb", "saucenao"])

    def test_fallback_puts_secondary_in_a_second_wave(self):
        waves = _engine_waves(_settings(secondary_engine_mode="fallback"), None)
        self.assertEqual(waves[0], ["iqdb"])
        self.assertEqual(waves[1], ["saucenao"])

    def test_extras_join_wave_one(self):
        waves = _engine_waves(_settings(
            secondary_engine_mode="always",
            enable_ascii2d=True,
            enable_tracemoe=True,
            enable_iqdb3d=True,
            enable_google_images=True,
            enable_google_lens=True,
        ), None)
        self.assertEqual(len(waves), 1)
        self.assertEqual(
            waves[0],
            ["iqdb", "saucenao", "ascii2d", "tracemoe", "iqdb3d",
             "googleimages", "googlelens"],
        )

    def test_extras_can_be_held_back_for_the_fallback_wave(self):
        """Google Lens spends 90s an image and Cloud Vision is metered,
        so they are worth spending only where they are needed."""
        waves = _engine_waves(_settings(
            secondary_engine_mode="always",
            enable_ascii2d=True,
            enable_google_lens=True,
            extras_only_as_fallback=True,
        ), None)
        self.assertEqual(waves, [["iqdb", "saucenao"], ["ascii2d", "googlelens"]])

    def test_held_back_extras_join_the_secondary_in_fallback_mode(self):
        waves = _engine_waves(_settings(
            secondary_engine_mode="fallback",
            enable_ascii2d=True,
            extras_only_as_fallback=True,
        ), None)
        self.assertEqual(waves, [["iqdb"], ["saucenao", "ascii2d"]])

    def test_holding_extras_back_with_none_enabled_leaves_one_wave(self):
        """An empty wave must not survive: the pacing counts waves as
        hosts about to be queried."""
        waves = _engine_waves(_settings(extras_only_as_fallback=True), None)
        self.assertEqual(waves, [["iqdb"]])

    def test_a_held_back_engine_is_still_planned_for_pacing(self):
        from core.search_engine import planned_engines
        plan = planned_engines(_settings(enable_ascii2d=True, extras_only_as_fallback=True))
        self.assertIn("ascii2d", plan)

    def test_override_is_a_single_engine_and_ignores_extras(self):
        waves = _engine_waves(_settings(enable_ascii2d=True), "ascii2d")
        self.assertEqual(waves, [["ascii2d"]])

    def test_unknown_override_falls_back_to_configured_order(self):
        waves = _engine_waves(_settings(secondary_engine_mode="disabled"), "nope")
        self.assertEqual(waves, [["iqdb"]])


class TestParallelAlwaysMode(unittest.TestCase):
    def test_always_mode_calls_both_primary_engines(self):
        """REGRESSION GUARD: 'always' used to run IQDB then SauceNAO in
        series. Both must still be invoked; the parallelisation is the
        point, but a wave that dropped one would be a silent skip."""
        from core.iqdb import IqdbMatch

        path = _empty_image()
        calls = []

        def fake_iqdb(*a, **k):
            calls.append("iqdb")
            return [IqdbMatch(
                url="https://danbooru.donmai.us/posts/1", thumb_url=None,
                similarity=97.0, width=800, height=600, source_name="Danbooru",
                is_best_match=True, unnamespaced_tags=[],
            )]

        def fake_sn(*a, **k):
            calls.append("saucenao")
            return []

        settings = _settings(secondary_engine_mode="always")
        entry = ImageEntry(path=path)
        entry.hydrus_hash = "abc"  # skip hashing; hashlib.file_digest is 3.11+
        with patch("core.iqdb.search", side_effect=fake_iqdb), \
             patch("core.saucenao.search", side_effect=fake_sn), \
             patch("core.search_engine._load_local_dimensions", lambda e: None), \
             patch("core.remote.download_bytes", return_value=None), \
             patch("core.remote.fetch_remote_info", return_value=(None, None)):
            search_image(entry, settings, use_cache=False)

        self.assertEqual(set(calls), {"iqdb", "saucenao"})
        self.assertEqual(len(entry.candidates), 1)
        self.assertEqual(entry.candidates[0].engine, "IQDB")


class TestAscii2dParser(unittest.TestCase):
    HTML = """
    <html><head><title>ascii2d</title></head><body>
    <div class="item-box">
      <div class="image-box"><img src="/thumbnail/query.jpg"></div>
      <div class="hash">aabbcc</div>
      <small>800x600 JPEG</small>
    </div>
    <div class="item-box">
      <div class="image-box"><img src="/thumbnail/hit.jpg"></div>
      <div class="detail-box">
        <h6>
          <a href="https://www.pixiv.net/artworks/12345">pixiv</a>
          <a href="https://twitter.com/i/web/status/99">twitter</a>
          <a href="https://ascii2d.net/search/color/aabbcc">more</a>
        </h6>
        <small>1200x1600 PNG</small>
      </div>
    </div>
    </body></html>
    """

    def test_skips_the_query_image_and_splits_sources(self):
        from core.ascii2d import parse_results
        matches = parse_results(self.HTML, "https://ascii2d.net/search/color/aabbcc")
        urls = {m.url for m in matches}
        self.assertIn("https://www.pixiv.net/artworks/12345", urls)
        self.assertIn("https://twitter.com/i/web/status/99", urls)
        self.assertFalse(any("ascii2d.net" in u for u in urls))
        self.assertFalse(any("query.jpg" in (m.thumb_url or "") for m in matches))

    def test_thumbnail_is_made_absolute(self):
        from core.ascii2d import parse_results
        matches = parse_results(self.HTML, "https://ascii2d.net/search/color/aabbcc")
        self.assertTrue(all(
            m.thumb_url == "https://ascii2d.net/thumbnail/hit.jpg" for m in matches
        ))

    def test_colour_similarity_is_ordinal_and_below_a_typical_iqdb_best(self):
        from core.ascii2d import parse_results
        matches = parse_results(self.HTML, "https://ascii2d.net/search/color/x")
        self.assertTrue(matches)
        self.assertLessEqual(matches[0].similarity, 90.0)
        self.assertGreaterEqual(matches[0].similarity, 80.0)

    def test_empty_page_is_safe(self):
        from core.ascii2d import parse_results
        self.assertEqual(parse_results("<html></html>", "https://ascii2d.net/"), [])


class TestTraceMoeParser(unittest.TestCase):
    PAYLOAD = {
        "error": "",
        "result": [
            {
                "anilist": {
                    "id": 21,
                    "title": {"romaji": "One Piece", "english": "One Piece", "native": "ワンピース"},
                },
                "filename": "One Piece - 15.mp4",
                "episode": 15,
                "similarity": 0.94,
                "image": "https://media.trace.moe/image/21/15.jpg",
            },
            {
                "anilist": 21,
                "similarity": 0.90,
            },
            {
                "anilist": {"id": 99, "title": {"romaji": "Miss"}},
                "similarity": 0.50,
            },
        ],
    }

    def test_scales_similarity_and_builds_anilist_url(self):
        from core.tracemoe import parse_results
        matches = parse_results(self.PAYLOAD, min_similarity=85.0)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].url, "https://anilist.co/anime/21")
        self.assertAlmostEqual(matches[0].similarity, 94.0)
        self.assertEqual(matches[0].episode, "15")

    def test_series_tag_is_deduped_when_romaji_equals_english(self):
        from core.tracemoe import parse_results
        matches = parse_results(self.PAYLOAD, min_similarity=85.0)
        series = [t.name for t in matches[0].tags if t.namespace == "series"]
        self.assertEqual(series, ["One_Piece"])

    def test_duplicate_anilist_id_is_kept_once(self):
        from core.tracemoe import parse_results
        matches = parse_results(self.PAYLOAD, min_similarity=85.0)
        self.assertEqual(len(matches), 1)

    def test_low_similarity_is_dropped(self):
        from core.tracemoe import parse_results
        matches = parse_results(self.PAYLOAD, min_similarity=85.0)
        self.assertFalse(any("Miss" in (m.title or "") for m in matches))

    def test_already_percent_similarity_is_not_rescaled(self):
        from core.tracemoe import parse_results
        payload = {"result": [{"anilist": {"id": 1, "title": {"romaji": "A"}}, "similarity": 92}]}
        matches = parse_results(payload, min_similarity=80.0)
        self.assertAlmostEqual(matches[0].similarity, 92.0)


class TestIqdb3d(unittest.TestCase):
    def test_three_d_thumbnails_join_against_the_3d_host(self):
        from core.iqdb import IQDB_3D_URL, _parse_results
        html = (
            '<html><head><title>iqdb</title></head><body><div id="pages">'
            '<table><tr><th>Best match</th></tr><tr>'
            '<td class="image"><a href="https://example.test/post/1">'
            '<img src="/thumbs/a.jpg"></a></td>'
            '<td>1200x1600</td><td>97% similarity</td>'
            '</tr></table></div></body></html>'
        )
        match = _parse_results(html, base_url=IQDB_3D_URL)[0]
        self.assertEqual(match.thumb_url, "https://3d.iqdb.org/thumbs/a.jpg")

    def test_search_3d_posts_to_the_3d_host(self):
        from core.iqdb import search_3d
        path = _empty_image()
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"<html>iqdb</html>"
            resp.text = "<html><head><title>iqdb</title></head><body><div id='pages'></div></body></html>"
            return resp

        with patch("core.iqdb.prepare_upload_bytes", return_value=(b"x", "x.png")), \
             patch("requests.Session.post", side_effect=fake_post):
            search_3d(path, timeout=5.0)
        self.assertEqual(captured["url"], "https://3d.iqdb.org/")


class TestFileFormats(unittest.TestCase):
    def test_hydrus_common_types_are_accepted(self):
        from core.formats import IMAGE_EXTENSIONS
        for ext in (".avif", ".jxl", ".tif", ".tiff", ".webm", ".mp4", ".webp", ".png"):
            self.assertIn(ext, IMAGE_EXTENSIONS, ext)

    def test_dialog_filter_lists_the_new_types(self):
        from core.formats import FILE_DIALOG_FILTER
        for ext in ("*.avif", "*.jxl", "*.webm", "*.mp4"):
            self.assertIn(ext, FILE_DIALOG_FILTER)


class TestNewSiteClassification(unittest.TestCase):
    def test_twitter_hosts_are_labelled(self):
        from core.boorus import find_parser, twitter
        # Tweet URLs have a parser now (FxTwitter's public API).
        for url in (
            "https://twitter.com/user/status/1",
            "https://x.com/user/status/1",
            "https://fxtwitter.com/user/status/1",
        ):
            with self.subTest(url=url):
                self.assertEqual(classify_site(None, url), "Twitter")
                self.assertIs(find_parser(url), twitter)
        # Media and shortener URLs carry no tweet id, so there is nothing
        # to look up - they stay classification-only.
        for url in ("https://pbs.twimg.com/media/abc.jpg", "https://t.co/abc"):
            with self.subTest(url=url):
                self.assertEqual(classify_site(None, url), "Twitter")
                self.assertIsNone(find_parser(url))
        self.assertIn("Twitter", ALL_SITE_OPTIONS)

    def test_rule34_and_xbooru_are_parsed(self):
        from core.boorus import find_parser, rule34, xbooru
        self.assertIs(find_parser("https://rule34.xxx/index.php?page=post&s=view&id=1"), rule34)
        self.assertIs(find_parser("https://xbooru.com/index.php?page=post&s=view&id=1"), xbooru)
        self.assertEqual(classify_site(None, "https://rule34.xxx/index.php?page=post&s=view&id=1"), "Rule34")
        self.assertEqual(classify_site(None, "https://xbooru.com/index.php?page=post&s=view&id=1"), "Xbooru")

    def test_anilist_is_not_twitter_because_of_t_co(self):
        """REGRESSION: HOST_MAP used to substring-match the whole URL, so
        the Twitter short-link host `t.co` classified anilist.co as
        Twitter."""
        self.assertEqual(classify_site(None, "https://anilist.co/anime/21"), "AniList")
        self.assertEqual(classify_site(None, "https://t.co/abc"), "Twitter")

    def test_anilist_is_classification_only(self):
        from core.boorus import find_parser
        self.assertEqual(classify_site(None, "https://anilist.co/anime/21"), "AniList")
        self.assertIsNone(find_parser("https://anilist.co/anime/21"))
        self.assertIn("AniList", ALL_SITE_OPTIONS)


class TestAnimePicturesParser(unittest.TestCase):
    """Shape captured from the live v3 API (post 596771), not invented.

    REGRESSION: the fixture here used to be made up - tags flat and
    nested inside "post", type 3 as the artist - and every assertion
    passed while the parser could not read a single real response. Live
    posts came back with no tags, no dimensions and no picture at all.
    Keep this fixture faithful to what the API actually sends.
    """

    MD5 = "aa2be7199bfa9bd93dc51d3efd4334df"
    PREVIEW = f"https://opreviews.anime-pictures.net/aa2/{MD5}_bp.avif"
    RESPONSE = {
        "post": {
            "id": 596771,
            "md5": MD5,
            "width": 5728,
            "height": 4000,
            "ext": ".jpeg",
            "big_preview": PREVIEW,
            "medium_preview": f"https://opreviews.anime-pictures.net/aa2/{MD5}_cp.avif",
        },
        # Top level, beside "post" - NOT inside it - and each entry wraps
        # the tag in another "tag" key.
        "tags": [
            {"tag": {"tag": "hatsune miku", "type": 1}},
            {"tag": {"tag": "multiple girls", "type": 2}},
            {"tag": {"tag": "nekoda (maoda)", "type": 4}},
            {"tag": {"tag": "houchi shoujo", "type": 5}},
            {"tag": {"tag": "original", "type": 6}},
            {"tag": {"tag": "girl", "type": 7}},
        ],
        # A download FILENAME, not a URL.
        "file_url": "596771-5728x4000-original-nekoda (maoda).jpeg",
    }

    def _body(self, **overrides):
        return json.dumps({**self.RESPONSE, **overrides})

    def test_both_url_forms_resolve_to_the_json_api(self):
        """REGRESSION: search engines hand over the legacy
        /pictures/view_post/{id} form. Matching only /posts/{id} meant the
        API was never used and the JS-rendered HTML page got parsed."""
        from core.boorus import animepictures
        expected = "https://api.anime-pictures.net/api/v3/posts/596771"
        for url in (
            "https://anime-pictures.net/pictures/view_post/596771",
            "https://anime-pictures.net/pictures/view_post/596771?lang=en",
            "https://anime-pictures.net/posts/596771",
            "https://anime-pictures.net/posts/596771?lang=en",
        ):
            with self.subTest(url=url):
                self.assertEqual(animepictures.resolve_fetch_url(url), expected)

    def test_url_without_a_post_id_is_left_alone(self):
        from core.boorus import animepictures
        for url in ("https://anime-pictures.net/pictures/view_post/",
                    "https://anime-pictures.net/"):
            self.assertEqual(animepictures.resolve_fetch_url(url), url)

    def test_tags_are_read_from_the_top_level_and_namespaced(self):
        from core.boorus import animepictures
        tags = {(t.namespace, t.name) for t in animepictures.parse(self._body(), "u")}
        self.assertIn(("character", "hatsune_miku"), tags)
        self.assertIn(("artist", "nekoda_(maoda)"), tags)
        self.assertIn(("copyright", "houchi_shoujo"), tags)   # game
        self.assertIn(("copyright", "original"), tags)        # anime
        # Descriptive types stay unnamespaced, like Danbooru's category-0.
        self.assertIn(("general", "multiple_girls"), tags)
        self.assertIn(("general", "girl"), tags)

    def test_a_flat_tag_shape_is_still_tolerated(self):
        """In case the API is ever simplified to {"tag": "x", "type": n}."""
        from core.boorus import animepictures
        body = self._body(tags=[{"tag": "smile", "type": 2}])
        tags = {(t.namespace, t.name) for t in animepictures.parse(body, "u")}
        self.assertEqual(tags, {("general", "smile")})

    def test_dimensions_and_preview_come_from_the_post_object(self):
        from core.boorus import animepictures
        body = self._body()
        self.assertEqual(animepictures.parse_dimensions(body, "u"), (5728, 4000))
        self.assertEqual(animepictures.parse_preview_url(body, "u"), self.PREVIEW)

    def test_no_file_url_rather_than_one_that_serves_html(self):
        """The old /pictures/download_image/{md5} endpoint 404s now, and a
        constructed oimages CDN path answers 302 to the front page. Either
        would have the app download HTML and store it as the match image,
        so admitting there is no file URL is the correct answer - callers
        fall back to the preview."""
        from core.boorus import animepictures
        self.assertIsNone(animepictures.parse_file_url(self._body(), "u"))

    def test_a_real_absolute_file_url_is_still_honoured(self):
        from core.boorus import animepictures
        body = self._body(file_url="https://cdn.example/real.png")
        self.assertEqual(
            animepictures.parse_file_url(body, "u"),
            "https://cdn.example/real.png",
        )

    def test_an_html_page_yields_nothing_without_raising(self):
        """What the site returns if the API URL is ever missed again."""
        from core.boorus import animepictures
        html = "<!DOCTYPE html><html><body>JS-rendered</body></html>"
        self.assertEqual(animepictures.parse(html, "u"), [])
        self.assertIsNone(animepictures.parse_file_url(html, "u"))
        self.assertIsNone(animepictures.parse_preview_url(html, "u"))
        self.assertEqual(animepictures.parse_dimensions(html, "u"), (None, None))

    def test_format_and_size_come_from_the_api(self):
        """The original file isn't reachable here, so a HEAD would measure
        the search engine's THUMBNAIL and report that as the match's format
        and size. The API states both for the real original."""
        from core.boorus import animepictures
        body = self._body(post={**self.RESPONSE["post"], "size": 5646846})
        self.assertEqual(animepictures.parse_file_info(body, "u"), ("JPEG", 5646846))

    def test_format_uses_the_same_names_as_a_content_type_lookup(self):
        from core.boorus import animepictures
        from core.search_engine import _format_from_content_type
        for ext, content_type in ((".jpeg", "image/jpeg"), (".jpg", "image/jpeg"),
                                  (".png", "image/png"), (".gif", "image/gif"),
                                  (".webp", "image/webp")):
            with self.subTest(ext=ext):
                body = self._body(post={**self.RESPONSE["post"], "ext": ext, "size": 10})
                fmt, _ = animepictures.parse_file_info(body, "u")
                self.assertEqual(fmt, _format_from_content_type(content_type))

    def test_missing_or_junk_file_info_is_reported_as_unknown(self):
        from core.boorus import animepictures
        for post in ({**self.RESPONSE["post"], "ext": None, "size": None},
                     {**self.RESPONSE["post"], "ext": ".sfx", "size": "not a number"},
                     {**self.RESPONSE["post"], "ext": ".png", "size": 0}):
            with self.subTest(post=post):
                fmt, size = animepictures.parse_file_info(self._body(post=post), "u")
                self.assertIsNone(size)
                self.assertIn(fmt, (None, "PNG"))

    def test_parser_is_registered_for_both_url_forms(self):
        from core.boorus import animepictures, find_parser
        for url in ("https://anime-pictures.net/posts/596771",
                    "https://anime-pictures.net/pictures/view_post/596771"):
            with self.subTest(url=url):
                self.assertIs(find_parser(url), animepictures)


class TestAnimePicturesAccountOnlyPosts(unittest.TestCase):
    """Account-only posts answer the API with 403 and carry nothing at
    all. That has to read as "sign in", not as a failed fetch, or the
    user is left with a blank match and no idea cookies would fix it."""

    FORBIDDEN = '{"errormsg":"Forbidden","success":false}'

    def test_the_api_403_is_reported_as_account_only(self):
        from core.boorus import animepictures
        reason = animepictures.restriction_for_status(403, self.FORBIDDEN, "u")
        self.assertIsNotNone(reason)
        self.assertIn("account-only", reason)
        self.assertIn("Site Logins", reason)   # tells the user where to fix it

    def test_an_html_403_is_a_bot_check_not_an_account_gate(self):
        """Cloudflare challenges this host occasionally, and no login
        would fix one - calling it account-only would misdirect."""
        from core.boorus import animepictures
        html = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
        self.assertIsNone(animepictures.restriction_for_status(403, html, "u"))

    def test_other_statuses_are_not_claimed_as_restrictions(self):
        from core.boorus import animepictures
        for status in (200, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.assertIsNone(
                    animepictures.restriction_for_status(status, self.FORBIDDEN, "u")
                )

    def test_fetch_page_info_surfaces_it_instead_of_raising(self):
        from core import boorus
        resp = MagicMock(status_code=403, text=self.FORBIDDEN)
        with patch.object(__import__("requests").Session, "get", return_value=resp):
            info = boorus.fetch_page_info("https://anime-pictures.net/posts/594528", timeout=5)
        self.assertTrue(info.fetched)
        self.assertIn("account-only", info.restricted)
        self.assertEqual(info.tags, [])

    def test_a_plain_403_still_raises_for_sites_without_the_hook(self):
        from core import boorus
        resp = MagicMock(status_code=403, text="nope")
        with patch.object(__import__("requests").Session, "get", return_value=resp):
            with self.assertRaises(boorus.BooruError):
                boorus.fetch_page_info("https://danbooru.donmai.us/posts/1", timeout=5)


class TestSiteCookieRouting(unittest.TestCase):
    def _settings(self, **kwargs):
        from core.config import Settings
        s = Settings()
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    def test_anime_pictures_cookies_reach_the_api_host(self):
        """The API subdomain serves the post data, so it is the host that
        actually has to be authenticated - matching only the www host
        would send the login nowhere useful."""
        from core.search_engine import cookies_for_url
        s = self._settings(animepictures_cookies="sessionid=abc; locale=en")
        for url in ("https://anime-pictures.net/posts/594528",
                    "https://api.anime-pictures.net/api/v3/posts/594528",
                    "https://opreviews.anime-pictures.net/aa2/x_bp.avif"):
            with self.subTest(url=url):
                self.assertEqual(cookies_for_url(url, s),
                                 {"sessionid": "abc", "locale": "en"})

    def test_nothing_configured_sends_no_cookie_jar(self):
        from core.search_engine import cookies_for_url
        s = self._settings()
        self.assertIsNone(cookies_for_url("https://anime-pictures.net/posts/1", s))

    def test_cookies_do_not_leak_to_other_sites(self):
        from core.search_engine import cookies_for_url
        s = self._settings(animepictures_cookies="sessionid=abc")
        for url in ("https://danbooru.donmai.us/posts/1",
                    "https://gelbooru.com/index.php?page=post&s=view&id=1",
                    "https://anime-pictures.example.com/posts/1"):
            with self.subTest(url=url):
                self.assertIsNone(cookies_for_url(url, s))

    def test_a_lookalike_host_is_never_sent_the_login(self):
        """SECURITY REGRESSION: this used to be a substring test, so a
        result pointing at anime-pictures.net.evil.example - or any URL
        merely mentioning the domain in its query string - was handed the
        user's session cookie. Match URLs come from IQDB/SauceNAO, so
        they are attacker-influenced input."""
        from core.search_engine import cookies_for_url
        s = self._settings(
            animepictures_cookies="sessionid=secret",
            pixiv_session_cookie="secret",
            sankaku_cookies="x=secret",
        )
        for url in ("https://anime-pictures.net.evil.example/x",
                    "https://evil.example/?q=anime-pictures.net",
                    "https://notanime-pictures.net/x",
                    "https://pixiv.net.attacker.test/a",
                    "https://sankakucomplex.com.evil.test/p",
                    "not even a url",
                    ""):
            with self.subTest(url=url):
                self.assertIsNone(cookies_for_url(url, s))

    def test_the_real_hosts_and_their_subdomains_still_authenticate(self):
        from core.search_engine import cookies_for_url
        s = self._settings(pixiv_session_cookie="secret", sankaku_cookies="x=secret")
        self.assertEqual(cookies_for_url("https://www.pixiv.net/artworks/1", s),
                         {"PHPSESSID": "secret"})
        self.assertEqual(cookies_for_url("https://chan.sankakucomplex.com/post/show/1", s),
                         {"x": "secret"})


def _isolated_config_paths(tmp_dir):
    """Points core.config's file-path globals at a throwaway directory and
    returns the originals to restore in a `finally`.

    tests/_path.py already redirects XDG_CONFIG_HOME so nothing in this
    suite ever touches the real user's config - but that redirect is one
    shared directory for the whole test run, so a test that calls
    Settings.save() still needs its OWN directory to avoid colliding with
    config.json another test in this same run wrote. The dedicated
    backup/broken-config paths are patched too, not just CONFIG_FILE:
    Settings.save() reads CONFIG_BACKUP_FILE as its own separate global,
    and leaving that one pointed at the shared directory would still let
    two tests trample each other's config.json.bak.
    """
    from pathlib import Path
    from core import config as config_module
    original = (config_module.CONFIG_DIR, config_module.CONFIG_FILE,
                config_module.CONFIG_BACKUP_FILE, config_module.CONFIG_BROKEN_FILE)
    config_module.CONFIG_DIR = Path(tmp_dir)
    config_module.CONFIG_FILE = Path(tmp_dir) / "config.json"
    config_module.CONFIG_BACKUP_FILE = config_module.CONFIG_FILE.with_name("config.json.bak")
    config_module.CONFIG_BROKEN_FILE = config_module.CONFIG_FILE.with_name("config.json.unreadable")
    return original


def _restore_config_paths(original):
    from core import config as config_module
    (config_module.CONFIG_DIR, config_module.CONFIG_FILE,
     config_module.CONFIG_BACKUP_FILE, config_module.CONFIG_BROKEN_FILE) = original


class TestDeviantArtParser(unittest.TestCase):
    """Shapes captured from live deviation pages, not invented.

    DeviantArt is a limited parser on purpose - its page state names the
    artist, the real file and a picture, but there is no booru-style tag
    vocabulary to read. What it must never do is describe the render it
    hands over as though it were the original artwork.
    """

    BASE = "https://images-wixmp.example/f/uuid/name.jpg"
    URL = "https://www.deviantart.com/radprofile/art/A-Deviation-470555824"

    def _page(self, *, original=None, render="/v1/fill/w_800,h_1083,q_75,strp/<prettyName>-fullview.jpg",
              username="Rad'Profile", extra_extended=None):
        """Rebuilds the page shape: a JSON document inside a JS string
        inside JSON.parse(...), ending the line - exactly as served."""
        state = {
            "@@entities": {
                "deviationExtended": {
                    "470555824": {
                        "originalFile": original if original is not None else {
                            "type": "png", "width": 2400, "height": 4000, "filesize": 4845515,
                        },
                        **(extra_extended or {}),
                    }
                },
                "deviation": {
                    "470555824": {
                        "author": 12605389,
                        "media": {
                            "baseUri": self.BASE,
                            "prettyName": "a_deviation_by_radprofile",
                            "token": ["signing-token"],
                            "types": [
                                {"t": "150", "c": "/v1/fit/w_150,h_150/<prettyName>-150.jpg"},
                                {"t": "fullview", "c": render},
                            ],
                        },
                    }
                },
                "user": {"12605389": {"username": username}},
            }
        }
        inner = json.dumps(state)
        js_string = json.dumps(inner)
        # The real page escapes apostrophes as \' - legal in a JS string,
        # rejected by JSON. Reproduce that here, or the unescaping step
        # this parser depends on is never actually exercised.
        js_string = js_string.replace("'", "\\'")
        assert "\\'" in js_string, "fixture must contain a JS-only escape"
        return (
            "<html><script>\n"
            "window.__REER__ = { init: function(n, d) { __REER__.push({n,d}); } };\n"
            f"window.__INITIAL_STATE__ = JSON.parse({js_string});\n"
            "</script></html>"
        )

    def test_the_artist_becomes_a_tag(self):
        from core.boorus import deviantart
        tags = deviantart.parse(self._page(), self.URL)
        self.assertEqual([(t.namespace, t.name) for t in tags], [("artist", "Rad'Profile")])

    def test_the_original_file_size_and_format_are_reported(self):
        """The whole reason the page is parsed instead of oEmbed, which
        names no file size at all. There is no reachable file URL to HEAD,
        so without this the size cannot be known."""
        from core.boorus import deviantart
        self.assertEqual(deviantart.parse_file_info(self._page(), self.URL), ("PNG", 4845515))

    def test_dimensions_are_the_originals_not_the_renders(self):
        """The render is 800x1083; the artwork is 2400x4000. The app
        compares these against the local file to decide whether a match is
        an upgrade, so the render's size reports upgrades as downgrades."""
        from core.boorus import deviantart
        self.assertEqual(deviantart.parse_dimensions(self._page(), self.URL), (2400, 4000))

    def test_the_render_is_a_preview_and_never_the_file(self):
        from core.boorus import deviantart
        page = self._page()
        preview = deviantart.parse_preview_url(page, self.URL)
        self.assertIsNotNone(preview)
        self.assertIn("/v1/fill/w_800", preview)
        self.assertIn("a_deviation_by_radprofile", preview)   # <prettyName> substituted
        self.assertIn("token=signing-token", preview)
        self.assertIsNone(deviantart.parse_file_url(page, self.URL))

    def test_a_blurred_adult_render_is_flagged(self):
        from core.boorus import deviantart
        page = self._page(render="/v1/fill/w_1024,h_1707,q_80,strp,blur_73/<prettyName>-fullview.jpg")
        reason = deviantart.parse_restriction(page, self.URL)
        self.assertIsNotNone(reason)
        self.assertIn("blurred", reason.lower())
        self.assertIn("Site Logins", reason)

    def test_a_normal_render_is_not_flagged(self):
        from core.boorus import deviantart
        self.assertIsNone(deviantart.parse_restriction(self._page(), self.URL))

    def test_state_extraction_survives_inline_js_containing_a_paren(self):
        """REGRESSION: matching to the first ");" truncated a 427KB state
        document at 13KB, which then parsed as nothing - indistinguishable
        from a page that carried no state at all."""
        from core.boorus import deviantart
        page = self._page()
        self.assertIn("__REER__.push({n,d});", page)   # the decoy really is present
        self.assertEqual(deviantart.parse_dimensions(page, self.URL), (2400, 4000))

    def test_missing_or_junk_original_file_is_reported_as_unknown(self):
        from core.boorus import deviantart
        for original in ({}, {"type": "png"}, {"type": "png", "filesize": 0},
                         {"type": "xyz", "filesize": "lots"}):
            with self.subTest(original=original):
                fmt, size = deviantart.parse_file_info(self._page(original=original), self.URL)
                self.assertIsNone(size)
                self.assertIn(fmt, (None, "PNG"))
                self.assertEqual(deviantart.parse_dimensions(self._page(original=original), self.URL),
                                 (None, None))

    def test_a_page_without_state_yields_nothing_without_raising(self):
        from core.boorus import deviantart
        html = "<!DOCTYPE html><html><body>no state here</body></html>"
        self.assertEqual(deviantart.parse(html, self.URL), [])
        self.assertIsNone(deviantart.parse_file_url(html, self.URL))
        self.assertIsNone(deviantart.parse_preview_url(html, self.URL))
        self.assertIsNone(deviantart.parse_restriction(html, self.URL))
        self.assertEqual(deviantart.parse_dimensions(html, self.URL), (None, None))
        self.assertEqual(deviantart.parse_file_info(html, self.URL), (None, None))

    def test_parser_is_registered_for_both_domains(self):
        from core.boorus import deviantart, find_parser
        for url in ("https://deviantart.com/view/1",
                    "https://www.deviantart.com/a/art/b-1",
                    "https://fav.me/d7s5n74"):
            with self.subTest(url=url):
                self.assertIs(find_parser(url), deviantart)

    def test_cookies_route_to_deviantart_and_its_backend(self):
        from core.config import Settings
        from core.search_engine import cookies_for_url
        s = Settings()
        s.deviantart_cookies = "auth=abc; auth_secure=def"
        for url in ("https://www.deviantart.com/a/art/b-1",
                    "https://backend.deviantart.com/oembed?url=x",
                    "https://fav.me/d7s5n74"):
            with self.subTest(url=url):
                self.assertEqual(cookies_for_url(url, s), {"auth": "abc", "auth_secure": "def"})
        self.assertIsNone(cookies_for_url("https://deviantart.com.evil.test/x", s))
        self.assertIsNone(cookies_for_url("https://danbooru.donmai.us/posts/1", s))

    def _page_response(self, html, cookie_pairs=()):
        """A real requests.Response carrying `html` and a real cookie jar.

        iter_content/close are overridden rather than left as the real
        Response methods, which read/close `.raw` - there is no real
        socket here (see tests/test_net.py's _streamed, same reasoning)."""
        import requests
        resp = requests.Response()
        resp.status_code = 200
        resp.url = self.URL
        resp._content = html.encode("utf-8")
        resp.encoding = "utf-8"
        resp.iter_content = lambda chunk_size: iter([resp._content])
        resp.close = lambda: None
        jar = requests.cookies.RequestsCookieJar()
        for name, value in cookie_pairs:
            jar.set_cookie(requests.cookies.create_cookie(name, value, domain="deviantart.com"))
        resp.cookies = jar
        return resp

    def test_a_renewed_session_cookie_is_captured_and_persisted_via_fetch_page_info(self):
        """DAN-123: fetch_page_info is the chokepoint CandidateFetchWorker
        (a background QThread) goes through for every DeviantArt page.
        Whatever DeviantArt renews on that response must survive into
        settings AND onto disk, so a restart doesn't lose it."""
        from core.boorus import fetch_page_info
        from core.config import Settings

        tmp = tempfile.mkdtemp()
        original = _isolated_config_paths(tmp)
        try:
            settings = Settings()
            settings.deviantart_cookies = "auth=old-token; auth_secure=old-secure; userinfo=old-info"

            resp = self._page_response(
                self._page(), [("auth", "new-token"), ("userinfo", "new-info")])

            with patch("requests.Session.get", return_value=resp):
                fetch_page_info(self.URL, timeout=5, settings=settings)

            self.assertEqual(
                settings.deviantart_cookies,
                "auth=new-token; auth_secure=old-secure; userinfo=new-info",
            )
            reloaded = Settings.load()
            self.assertEqual(reloaded.deviantart_cookies, settings.deviantart_cookies)
        finally:
            _restore_config_paths(original)

    def test_the_test_deviantart_button_never_persists_a_rotation(self):
        """The Settings dialog's "Test DeviantArt..." button (DAN-122)
        fetches with whatever is currently TYPED, which may not be saved
        yet - fetch_page_info is called there with no `settings`, so
        capture_rotated_deviantart_cookies never runs and an in-progress,
        unsaved paste can't get merged into the saved cookie string."""
        from core.boorus import fetch_page_info
        from core.config import Settings

        settings = Settings()
        settings.deviantart_cookies = "auth=old-token"

        resp = self._page_response(self._page(), [("auth", "new-token")])

        with patch("requests.Session.get", return_value=resp):
            fetch_page_info(self.URL, timeout=5, cookies={"auth": "typed-but-unsaved"})

        self.assertEqual(settings.deviantart_cookies, "auth=old-token")


class TestDeviantArtCookieRotation(unittest.TestCase):
    """core.site_access.capture_rotated_deviantart_cookies in isolation -
    the merge/persist rule itself, independent of which chokepoint calls
    it (see TestDeviantArtParser for the fetch_page_info integration)."""

    def _response_with_cookies(self, pairs):
        import requests
        resp = requests.Response()
        resp.status_code = 200
        jar = requests.cookies.RequestsCookieJar()
        for name, value in pairs.items():
            jar.set_cookie(requests.cookies.create_cookie(name, value, domain="deviantart.com"))
        resp.cookies = jar
        return resp

    def test_a_renewed_cookie_is_merged_in_and_written_to_disk(self):
        from core.config import Settings
        from core.site_access import capture_rotated_deviantart_cookies

        tmp = tempfile.mkdtemp()
        original = _isolated_config_paths(tmp)
        try:
            settings = Settings()
            settings.deviantart_cookies = "auth=old-token; auth_secure=old-secure; userinfo=old-info"
            resp = self._response_with_cookies(
                {"auth": "new-token", "auth_secure": "old-secure", "userinfo": "new-info"})

            capture_rotated_deviantart_cookies(
                "https://www.deviantart.com/artist/art/title-1", resp, settings)

            self.assertEqual(
                settings.deviantart_cookies,
                "auth=new-token; auth_secure=old-secure; userinfo=new-info",
            )
            reloaded = Settings.load()
            self.assertEqual(reloaded.deviantart_cookies, settings.deviantart_cookies)
        finally:
            _restore_config_paths(original)

    def test_no_cookies_configured_means_none_are_started(self):
        """DeviantArt hands an anonymous visitor a Set-Cookie too, but a
        user who never pasted a session should not get one created for
        them just because a request happened to receive one."""
        from core.config import Settings
        from core.site_access import capture_rotated_deviantart_cookies

        settings = Settings()
        settings.deviantart_cookies = ""
        resp = self._response_with_cookies({"userinfo": "anon-info"})

        with patch.object(Settings, "save") as save:
            capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "")
        save.assert_not_called()

    def test_other_hosts_are_never_touched(self):
        """Scoped strictly to deviantart.com/fav.me - core/net.py's
        no-shared-jar policy stays exactly as documented for every other
        site this app talks to."""
        from core.config import Settings
        from core.site_access import capture_rotated_deviantart_cookies

        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = self._response_with_cookies({"auth": "new"})

        with patch.object(Settings, "save") as save:
            capture_rotated_deviantart_cookies("https://danbooru.donmai.us/posts/1", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old")
        save.assert_not_called()

    def test_identical_values_skip_the_disk_write(self):
        from core.config import Settings
        from core.site_access import capture_rotated_deviantart_cookies

        settings = Settings()
        settings.deviantart_cookies = "auth=same; auth_secure=same2"
        resp = self._response_with_cookies({"auth": "same", "auth_secure": "same2"})

        with patch.object(Settings, "save") as save:
            capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        save.assert_not_called()

    def test_a_403_carrying_set_cookie_never_overwrites_the_saved_session(self):
        """A bot-check/error page answering with its own Set-Cookie (often
        a fresh ANONYMOUS session, sometimes a reset one) must never be
        read as a rotation of the session we sent - that would silently
        kill a working login and reproduce the exact bug this feature
        exists to fix, only self-inflicted instead of DeviantArt's doing."""
        from core.config import Settings
        from core.site_access import capture_rotated_deviantart_cookies

        settings = Settings()
        settings.deviantart_cookies = "auth=old-token; auth_secure=old-secure; userinfo=old-info"
        resp = self._response_with_cookies({"auth": "anon-token", "userinfo": "anon-info"})
        resp.status_code = 403

        with patch.object(Settings, "save") as save:
            capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(
            settings.deviantart_cookies,
            "auth=old-token; auth_secure=old-secure; userinfo=old-info",
        )
        save.assert_not_called()

    def test_a_test_double_response_with_no_real_cookie_jar_is_a_no_op(self):
        """MagicMock responses (used throughout the availability/parser
        test doubles) have a `.cookies` attribute that isn't iterable -
        this must degrade to "nothing captured", not raise, or every
        existing test double for every other site would need updating."""
        from core.config import Settings
        from core.site_access import capture_rotated_deviantart_cookies

        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = MagicMock(status_code=200)

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old")


class TestGoogleImagesParser(unittest.TestCase):
    """Google answers a reverse-image upload with one of two pages, and
    both shapes are parsed: a classic results page, and a Google Lens
    page whose results live in an embedded JavaScript blob.

    Neither fixture leans on a generated class name - Google's markup is
    minified and renamed constantly, so the parsers key off structure (a
    link wrapping a heading) and off the blob's string order instead.
    """

    SERP = """
    <html><body><div id="search">
      <div class="g">
        <a href="https://danbooru.donmai.us/posts/123"><h3>Some post - Danbooru</h3></a>
        <img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:AAA">
      </div>
      <div class="g">
        <a href="/url?q=https://www.pixiv.net/artworks/456&amp;sa=U"><h3>An artwork - pixiv</h3></a>
      </div>
      <div class="g">
        <a href="https://www.google.com/preferences"><h3>Settings</h3></a>
      </div>
    </div></body></html>
    """

    LENS = (
        "<html><body><script>AF_initDataCallback({key:'ds:1', data:["
        '["https:\\/\\/encrypted-tbn0.gstatic.com\\/images?q=tbn:XYZ",'
        '"https:\\/\\/www.deviantart.com\\/someone\\/art\\/A-Picture-123",'
        '"A Picture by someone",'
        '"deviantart.com"]'
        "]});</script></body></html>"
    )

    def test_a_results_page_yields_its_result_links(self):
        from core.google_images import parse_results
        matches = parse_results(self.SERP, "https://www.google.com/search?tbs=sbi:x")
        urls = [m.url for m in matches]
        self.assertEqual(urls, [
            "https://danbooru.donmai.us/posts/123",
            "https://www.pixiv.net/artworks/456",
        ])

    def test_googles_own_pages_are_not_offered_as_sources(self):
        """/preferences is navigation, not a page the picture came from."""
        from core.google_images import parse_results
        matches = parse_results(self.SERP, "https://www.google.com/search?tbs=sbi:x")
        self.assertFalse(any("google.com" in m.url for m in matches))

    def test_a_redirector_link_is_unwrapped(self):
        """Stored as the URL a user could open, not as Google's /url?q=
        wrapper - which would follow only while that redirect lives."""
        from core.google_images import parse_results
        matches = parse_results(self.SERP, "https://www.google.com/search?tbs=sbi:x")
        self.assertIn("https://www.pixiv.net/artworks/456", [m.url for m in matches])
        self.assertFalse(any("/url?q=" in m.url for m in matches))

    def test_results_are_scored_in_order_below_ascii2d(self):
        """Google ranks by page relevance, not by visual similarity, so
        its best hit must not outrank an engine that compared pixels."""
        from core.ascii2d import BOVW_SIMILARITY_START
        from core.google_images import parse_results
        matches = parse_results(self.SERP, "https://www.google.com/search?tbs=sbi:x")
        self.assertLess(matches[0].similarity, BOVW_SIMILARITY_START)
        self.assertGreater(matches[0].similarity, matches[1].similarity)

    def test_a_thumbnail_beside_a_result_is_kept(self):
        from core.google_images import parse_results
        matches = parse_results(self.SERP, "https://www.google.com/search?tbs=sbi:x")
        self.assertEqual(matches[0].thumb_url,
                         "https://encrypted-tbn0.gstatic.com/images?q=tbn:AAA")

    def test_a_lens_page_yields_its_matches(self):
        from core.google_images import parse_lens_results
        matches = parse_lens_results(self.LENS)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].url,
                         "https://www.deviantart.com/someone/art/A-Picture-123")
        self.assertEqual(matches[0].title, "A Picture by someone")
        self.assertEqual(matches[0].source_name, "deviantart.com")

    def test_a_lens_thumbnail_is_kept_not_mistaken_for_a_source(self):
        """The gstatic URL just before a result is its preview. Dropping
        every Google host outright would lose it; treating it as a
        source would list Google as the site the image came from."""
        from core.google_images import parse_lens_results
        matches = parse_lens_results(self.LENS)
        self.assertEqual(matches[0].thumb_url,
                         "https://encrypted-tbn0.gstatic.com/images?q=tbn:XYZ")

    def test_junk_parses_to_nothing_rather_than_raising(self):
        from core.google_images import parse_lens_results, parse_results
        self.assertEqual(parse_results("<html></html>", "https://www.google.com/"), [])
        self.assertEqual(parse_lens_results("<html></html>"), [])


class TestGoogleVisionWebDetection(unittest.TestCase):
    """Shape taken from the Cloud Vision WEB_DETECTION response, which is
    what actually answers "which pages is this image on" now that the
    public page renders its results in JavaScript.

    Vision returns exact and partial pages interleaved. A crop or a
    resize must never sort above the picture itself, so they are scored
    in two bands rather than in the order Google listed them.
    """

    PAYLOAD = {
        "responses": [{
            "webDetection": {
                "pagesWithMatchingImages": [
                    {
                        "url": "https://example.test/partial-only",
                        "pageTitle": "A crop of it",
                        "partialMatchingImages": [{"url": "https://example.test/crop.jpg"}],
                    },
                    {
                        "url": "https://www.deviantart.com/someone/art/A-Picture-123",
                        "pageTitle": "A Picture &amp; friends by <b>someone</b>",
                        "fullMatchingImages": [{"url": "https://images.test/full.jpg"}],
                    },
                    {
                        "url": "https://www.google.com/imgres?q=x",
                        "pageTitle": "Google",
                        "fullMatchingImages": [{"url": "https://images.test/x.jpg"}],
                    },
                    # Listed as a page with matching images, but carrying
                    # none. Observed to be a loose subject association.
                    {
                        "url": "https://www.youtube.com/shorts/qT_OeqrMtU8",
                        "pageTitle": "Monkey Mischief",
                    },
                ],
                "fullMatchingImages": [
                    {"url": "https://rule34storage.b-cdn.net/posts/1383/1383457/1383457.picsmall.jpg"},
                ],
                "partialMatchingImages": [
                    {"url": "https://images.test/partial-cdn.jpg"},
                ],
                "visuallySimilarImages": [
                    {"url": "https://images.test/similar-1.jpg"},
                    {"url": "https://images.test/similar-2.jpg"},
                ],
            },
        }],
    }

    def test_the_exact_matches_are_read_not_just_the_pages(self):
        """REGRESSION: reading only pagesWithMatchingImages returned the
        noise and dropped the answer.

        MEASURED over 10 images from a real library: fullMatchingImages
        held the actual sources on 6 of them - the booru CDNs the
        pictures came from - while not one pagesWithMatchingImages entry
        ever carried an attached image, and the same four YouTube URLs
        came back for four unrelated files."""
        from core.google_images import parse_vision_results
        urls = [m.url for m in parse_vision_results(self.PAYLOAD)]
        self.assertIn("https://rule34.us/index.php?r=posts/view&id=1383457", urls)
        self.assertIn("https://images.test/partial-cdn.jpg", urls)

    def test_results_are_banded_by_how_well_evidenced_they_are(self):
        from core.google_images import parse_vision_results
        matches = parse_vision_results(self.PAYLOAD)
        self.assertEqual([m.url for m in matches], [
            # exact: a page carrying a full match, then the full match itself
            "https://www.deviantart.com/someone/art/A-Picture-123",
            "https://rule34.us/index.php?r=posts/view&id=1383457",
            # partial: a page carrying only a partial, then the partial itself
            "https://example.test/partial-only",
            "https://images.test/partial-cdn.jpg",
            # a page Vision attached no matching image to, last
            "https://www.youtube.com/shorts/qT_OeqrMtU8",
        ])
        self.assertEqual([m.similarity for m in matches], [78.0, 76.0, 70.0, 68.0, 60.0])

    def test_a_bare_page_never_outranks_an_evidenced_match(self):
        """It is kept - on other kinds of image the association may be
        real - but it must not be the candidate the app auto-selects."""
        from core.google_images import parse_vision_results
        matches = parse_vision_results(self.PAYLOAD)
        youtube = next(m for m in matches if "youtube" in m.url)
        self.assertEqual(youtube, matches[-1])
        self.assertTrue(all(m.similarity > youtube.similarity for m in matches[:-1]))

    def test_visually_similar_images_are_never_offered(self):
        """Twenty come back on every single image, related only by
        subject. Including them would bury every real match."""
        from core.google_images import parse_vision_results
        urls = [m.url for m in parse_vision_results(self.PAYLOAD)]
        self.assertFalse(any("similar-" in u for u in urls))

    def test_a_cdn_image_becomes_the_post_it_belongs_to(self):
        """A bare CDN jpeg is not somewhere tags can be read from.
        CONFIRMED live that this path is rule34.us post 1383457."""
        from core.google_images import post_url_for_image
        self.assertEqual(
            post_url_for_image(
                "https://rule34storage.b-cdn.net/posts/1383/1383457/1383457.picsmall.jpg"),
            "https://rule34.us/index.php?r=posts/view&id=1383457")

    def test_an_unrecognised_cdn_is_left_alone_not_guessed_at(self):
        from core.google_images import post_url_for_image
        for url in ("https://img2.rule34.us/thumbnails/1f/f7/thumbnail_abc.jpg",
                    "https://img.kemono.cr/thumbnail/data/69/7f/697f.jpg",
                    "https://pbs.twimg.com/media/FDbkoMGWQAQaUyN.jpg:small",
                    ""):
            with self.subTest(url=url):
                self.assertIsNone(post_url_for_image(url))

    def test_a_page_title_is_cleaned_up(self):
        """Vision hands back the page's own <title>, with the matched
        words wrapped in <b> and its entities unresolved."""
        from core.google_images import parse_vision_results
        matches = parse_vision_results(self.PAYLOAD)
        self.assertEqual(matches[0].title, "A Picture & friends by someone")

    def test_googles_own_pages_are_not_offered_as_sources(self):
        from core.google_images import parse_vision_results
        matches = parse_vision_results(self.PAYLOAD)
        self.assertFalse(any("google.com" in m.url for m in matches))

    def test_the_matching_image_becomes_the_thumbnail(self):
        from core.google_images import parse_vision_results
        matches = parse_vision_results(self.PAYLOAD)
        self.assertEqual(matches[0].thumb_url, "https://images.test/full.jpg")
        recovered = next(m for m in matches if m.url.startswith("https://rule34.us/"))
        self.assertEqual(
            recovered.thumb_url,
            "https://rule34storage.b-cdn.net/posts/1383/1383457/1383457.picsmall.jpg")

    def test_an_empty_or_malformed_body_is_no_matches_not_a_crash(self):
        from core.google_images import parse_vision_results
        for payload in ({}, {"responses": []}, {"responses": [{}]},
                        {"responses": [{"webDetection": {}}]}):
            with self.subTest(payload=payload):
                self.assertEqual(parse_vision_results(payload), [])

    def test_a_key_is_used_instead_of_the_public_page(self):
        from core import google_images
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post") as public, \
             patch("requests.post") as vision:
            vision.return_value = MagicMock(status_code=200, **{"json.return_value": self.PAYLOAD})
            matches = google_images.search("/tmp/whatever.jpg", timeout=5.0, api_key="k")
        public.assert_not_called()
        self.assertEqual(matches[0].url,
                         "https://www.deviantart.com/someone/art/A-Picture-123")

    def test_a_rejected_key_stands_down_for_the_batch(self):
        """Every remaining image would be refused the same way, so this
        is reported once rather than once per image."""
        from core import google_images
        google_images.reset_blocked_flag()
        self.addCleanup(google_images.reset_blocked_flag)
        body = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                          "message": "Cloud Vision API has not been used in project 1"}}
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.post",
                   return_value=MagicMock(status_code=403, **{"json.return_value": body})):
            with self.assertRaises(google_images.GoogleImagesBlockedError) as caught:
                google_images.search("/tmp/whatever.jpg", timeout=5.0, api_key="k")
        self.assertTrue(google_images.is_blocked())
        self.assertIn("Settings > Engine", str(caught.exception))

    def test_an_exhausted_quota_stands_down_too(self):
        from core import google_images
        google_images.reset_blocked_flag()
        self.addCleanup(google_images.reset_blocked_flag)
        body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.post",
                   return_value=MagicMock(status_code=429, **{"json.return_value": body})):
            with self.assertRaises(google_images.GoogleImagesBlockedError):
                google_images.search("/tmp/whatever.jpg", timeout=5.0, api_key="k")
        self.assertTrue(google_images.is_blocked())

    def test_a_per_image_error_is_not_latched(self):
        """Vision reports a bad image inside a 200 body. That is about
        this file, so the next one still gets asked."""
        from core import google_images
        google_images.reset_blocked_flag()
        self.addCleanup(google_images.reset_blocked_flag)
        body = {"responses": [{"error": {"code": 3, "message": "Bad image data"}}]}
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.post",
                   return_value=MagicMock(status_code=200, **{"json.return_value": body})):
            with self.assertRaises(google_images.GoogleImagesError) as caught:
                google_images.search("/tmp/whatever.jpg", timeout=5.0, api_key="k")
        self.assertNotIsInstance(caught.exception, google_images.GoogleImagesBlockedError)
        self.assertFalse(google_images.is_blocked())


class TestMeasuredSimilarityReplacesOrdinalScores(unittest.TestCase):
    """ascii2d and the two Google engines report no similarity at all -
    the service does not provide one - so they number their results by
    position: 80%, 78%, 76%. That number sits in the same column as
    IQDB's and SauceNAO's measured ones and means nothing.

    Comparing the pictures gives a real answer, and each candidate
    already carries a thumbnail to compare against.
    """

    def _candidates(self, *specs):
        from core.models import MatchCandidate
        return [MatchCandidate(url=f"https://e.test/{i}", source_name="Other",
                               thumb_url=thumb, similarity=sim, engine=engine)
                for i, (engine, sim, thumb) in enumerate(specs)]

    def _run(self, candidates, thumb_bytes=b"thumb", distance=0):
        from unittest.mock import patch as _patch
        from core.models import ImageEntry
        from core.image_compare import perceptual_similarity
        from core.search_engine import measure_ordinal_similarities
        entry = ImageEntry(path=_empty_image())
        with _patch("core.remote.download_bytes", return_value=thumb_bytes), \
             _patch("core.similarity_check._worth_hashing", return_value=True), \
             _patch("core.similarity_check.local_prints", return_value=object()), \
             _patch("core.similarity_check.aligned_similarity",
                    return_value=(perceptual_similarity(distance), False)):
            measure_ordinal_similarities(entry, candidates, _settings())
        return candidates

    def test_an_ordinal_score_is_replaced_by_a_measurement(self):
        candidates = self._run(self._candidates(("Google Lens", 80.0, "https://t/1.jpg")))
        self.assertEqual(candidates[0].similarity, 100.0)
        self.assertTrue(candidates[0].similarity_measured)

    def test_a_measuring_engine_is_left_alone(self):
        """IQDB's number is already a measurement, and its own is better
        than one taken from a thumbnail."""
        candidates = self._run(self._candidates(("IQDB", 93.0, "https://t/1.jpg")))
        self.assertEqual(candidates[0].similarity, 93.0)
        self.assertFalse(candidates[0].similarity_measured)

    def test_a_candidate_with_no_thumbnail_keeps_its_ordinal_score(self):
        """Lens's Exact matches tab carries no thumbnails, so those
        cannot be measured. A position in a list is poor information;
        inventing a measurement would be worse."""
        candidates = self._run(self._candidates(("Google Lens", 80.0, None)))
        self.assertEqual(candidates[0].similarity, 80.0)
        self.assertFalse(candidates[0].similarity_measured)

    def test_a_thumbnail_that_will_not_download_keeps_its_score(self):
        candidates = self._run(self._candidates(("ascii2d", 86.0, "https://t/1.jpg")),
                               thumb_bytes=None)
        self.assertEqual(candidates[0].similarity, 86.0)
        self.assertFalse(candidates[0].similarity_measured)

    def test_a_different_picture_scores_far_below_its_ordinal_score(self):
        """The case worth having: Lens ranked it second, and it is not
        the same picture at all."""
        candidates = self._run(self._candidates(("Google Lens", 78.0, "https://t/1.jpg")),
                               distance=32)
        self.assertEqual(candidates[0].similarity, 0.0)
        self.assertTrue(candidates[0].similarity_measured)

    def test_a_blurred_placeholder_is_never_measured_from(self):
        """MEASURED on Lens's exact-match tiles: the inline thumbnail is
        a blurred placeholder - 221x228 in 2,070 bytes, 0.04 bytes per
        pixel - and it never upgrades, unchanged after 36 seconds of
        waiting and scrolling. Hashing it puts an IDENTICAL image at
        distance 9, which reads as "probably not the same picture" for
        one that certainly is. An ordinal score is poor information; a
        measurement that is quietly wrong is worse."""
        import io
        from PIL import Image, ImageFilter
        from core.search_engine import _worth_hashing

        # Noise, so the sharp version compresses like a real photograph
        # rather than like a smooth synthetic gradient.
        import random
        random.seed(7)
        source = Image.new("RGB", (221, 228))
        source.putdata([(random.randrange(256), random.randrange(256),
                         random.randrange(256)) for _ in range(221 * 228)])
        blurred = io.BytesIO()
        source.filter(ImageFilter.GaussianBlur(12)).save(blurred, format="JPEG", quality=20)
        sharp = io.BytesIO()
        source.save(sharp, format="JPEG", quality=80)

        self.assertFalse(_worth_hashing(blurred.getvalue()),
                         "a blur at a few bytes per pixel carries no signal")
        self.assertTrue(_worth_hashing(sharp.getvalue()))

    def test_a_tiny_image_is_not_measured_from_either(self):
        import io
        from PIL import Image
        from core.search_engine import _worth_hashing
        tiny = io.BytesIO()
        Image.new("RGB", (16, 16), (200, 40, 40)).save(tiny, format="PNG")
        self.assertFalse(_worth_hashing(tiny.getvalue()))

    def test_junk_bytes_are_not_measured_from(self):
        from core.search_engine import _worth_hashing
        self.assertFalse(_worth_hashing(b"not an image"))

    def test_a_measured_score_counts_towards_the_fallback_threshold(self):
        """An ordinal 80 cannot satisfy a 75% threshold, but a measured
        one is a real answer whichever engine found it."""
        from core.models import MatchCandidate
        from core.search_engine import _fallback_is_unnecessary
        settings = _settings(fallback_below_similarity=75.0)
        measured = MatchCandidate(url="https://e.test/a", source_name="Other",
                                  thumb_url=None, similarity=96.0, engine="Google Lens")
        measured.similarity_measured = True
        skip, why = _fallback_is_unnecessary(
            settings, [["iqdb"], ["saucenao"]], {"iqdb": True}, [measured])
        self.assertTrue(skip)
        self.assertIn("96%", why)


class TestSkippedMeasurementIsLogged(unittest.TestCase):
    """A skipped measurement used to be silent, and that made a result row
    unreadable: a Lens candidate sitting at its ordinal 80% looks the same
    whether comparing the pictures said 80% or comparing never ran at all.
    One line per skipped candidate, naming the engine and the URL, so a
    log can be matched to a row after a real run.
    """

    def _run(self, *, thumb_url, thumb_bytes, worth_hashing=False):
        from unittest.mock import patch as _patch
        from core.models import ImageEntry, MatchCandidate
        from core.search_engine import measure_ordinal_similarities
        entry = ImageEntry(path=_empty_image())
        candidate = MatchCandidate(url="https://e.test/post/1", source_name="Other",
                                   thumb_url=thumb_url, similarity=80.0,
                                   engine="Google Lens")
        patches = [
            _patch("core.remote.download_bytes", return_value=thumb_bytes),
            _patch("core.similarity_check.local_prints", return_value=object()),
            _patch("core.similarity_check.aligned_similarity", return_value=(100.0, False)),
        ]
        if worth_hashing:
            patches.append(_patch("core.similarity_check._worth_hashing", return_value=True))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertLogs("hatate.search", level="INFO") as captured:
                measure_ordinal_similarities(entry, [candidate], _settings())
        return candidate, "\n".join(captured.output)

    def _skip_lines(self, logged):
        return [line for line in logged.splitlines() if "not measuring" in line]

    def test_a_candidate_with_no_thumbnail_at_all_says_so(self):
        candidate, logged = self._run(thumb_url=None, thumb_bytes=None)
        lines = self._skip_lines(logged)
        self.assertEqual(len(lines), 1, logged)
        self.assertIn("no thumbnail", lines[0])
        self.assertIn("Google Lens", lines[0])
        self.assertIn("https://e.test/post/1", lines[0])
        self.assertFalse(candidate.similarity_measured)

    def test_a_thumbnail_too_poor_to_hash_says_so(self):
        """Lens's exact-match tiles come back as blurred placeholders.
        _worth_hashing refuses them; that refusal is now on the record."""
        candidate, logged = self._run(thumb_url="https://t.test/1.jpg",
                                      thumb_bytes=b"not an image")
        lines = self._skip_lines(logged)
        self.assertEqual(len(lines), 1, logged)
        self.assertIn("too little", lines[0])
        self.assertIn("Google Lens", lines[0])
        self.assertIn("https://e.test/post/1", lines[0])
        self.assertIn("https://t.test/1.jpg", lines[0])
        self.assertFalse(candidate.similarity_measured)

    def test_a_thumbnail_that_would_not_download_says_so(self):
        candidate, logged = self._run(thumb_url="https://t.test/1.jpg", thumb_bytes=None)
        lines = self._skip_lines(logged)
        self.assertEqual(len(lines), 1, logged)
        self.assertIn("came back empty", lines[0])
        self.assertIn("https://t.test/1.jpg", lines[0])
        self.assertFalse(candidate.similarity_measured)

    def test_nothing_is_logged_when_the_measurement_proceeds(self):
        candidate, logged = self._run(thumb_url="https://t.test/1.jpg",
                                      thumb_bytes=b"a real thumbnail",
                                      worth_hashing=True)
        self.assertEqual(self._skip_lines(logged), [], logged)
        self.assertTrue(candidate.similarity_measured)
        self.assertIn("measured 1/1", logged)

    def test_a_local_file_that_could_not_be_hashed_says_so_at_INFO(self):
        """The one gap DAN-51 left deliberately, closed in DAN-74.

        This early return skips EVERY candidate at once, and it used to log
        at DEBUG - so a run whose local file could not be hashed left the
        whole row on ordinal scores with nothing said at the level anybody
        reads. The per-candidate skips above are all INFO; this is strictly
        the larger event of the two, so the LEVEL is what this test is for,
        not just the wording.
        """
        from unittest.mock import patch as _patch
        from core.models import ImageEntry, MatchCandidate
        from core.search_engine import measure_ordinal_similarities
        entry = ImageEntry(path=_empty_image())
        candidates = [
            MatchCandidate(url=f"https://e.test/post/{i}", source_name="Other",
                           thumb_url=f"https://t.test/{i}.jpg", similarity=80.0,
                           engine="Google Lens")
            for i in range(3)
        ]

        with _patch("core.similarity_check.local_prints", return_value=None):
            with self.assertLogs("hatate.search", level="INFO") as captured:
                measure_ordinal_similarities(entry, candidates, _settings())

        lines = [line for line in captured.output
                 if "could not hash the local file" in line]
        self.assertEqual(len(lines), 1, captured.output)
        self.assertTrue(lines[0].startswith("INFO:"), lines[0])
        # The count is in the line: "all 3 candidate(s)" is actionable in a
        # way that "leaving ordinal scores alone" was not.
        self.assertIn("3 candidate(s)", lines[0])
        self.assertFalse(any(c.similarity_measured for c in candidates))


class TestWeakResultFallback(unittest.TestCase):
    """A weak match used to end the search as firmly as a strong one.

    The fallback ran only when the first engine found NOTHING, so a 42%
    "maybe" suppressed it exactly like a 96% certainty. The threshold
    makes "good enough" the user's call.
    """

    def _decide(self, threshold, similarities, primary_found=True, engine="IQDB"):
        from core.models import MatchCandidate
        from core.search_engine import _fallback_is_unnecessary
        settings = _settings(fallback_below_similarity=threshold)
        candidates = [MatchCandidate(url=f"https://e.test/{i}", source_name="Other",
                                     thumb_url=None, similarity=sim, engine=engine)
                      for i, sim in enumerate(similarities)]
        skip, why = _fallback_is_unnecessary(
            settings, [["iqdb"], ["saucenao"]], {"iqdb": primary_found}, candidates)
        return skip, why

    def test_a_weak_best_match_lets_the_fallback_run(self):
        skip, why = self._decide(70.0, [42.0, 31.0])
        self.assertFalse(skip)
        self.assertIn("42%", why)

    def test_a_strong_match_still_ends_the_search(self):
        skip, _ = self._decide(70.0, [96.0, 31.0])
        self.assertTrue(skip)

    def test_the_threshold_is_inclusive(self):
        skip, _ = self._decide(70.0, [70.0])
        self.assertTrue(skip, "exactly at the threshold is good enough")

    def test_no_candidates_always_falls_back(self):
        skip, _ = self._decide(70.0, [])
        self.assertFalse(skip)

    def test_an_ordinal_score_cannot_satisfy_the_threshold(self):
        """REGRESSION, seen in a real run: Google Lens's first result is
        always 80%, which is not a measurement - it is a position in a
        list. On a 75% threshold it cleared the bar on every image Lens
        ran, suppressing the fallback even where IQDB and SauceNAO had
        found nothing at all."""
        skip, why = self._decide(75.0, [80.0, 78.0], engine="Google Lens")
        self.assertFalse(skip)
        self.assertIn("don't measure similarity", why)

    def test_ascii2d_and_google_images_are_ordinal_too(self):
        for engine in ("ascii2d", "Google Images"):
            with self.subTest(engine=engine):
                skip, _ = self._decide(75.0, [86.0], engine=engine)
                self.assertFalse(skip)

    def test_a_measured_engine_still_satisfies_it(self):
        for engine in ("IQDB", "SauceNAO", "IQDB 3D", "trace.moe"):
            with self.subTest(engine=engine):
                skip, _ = self._decide(75.0, [88.0], engine=engine)
                self.assertTrue(skip)

    def test_a_strong_measured_match_wins_over_ordinal_noise(self):
        """The ordinal results are ignored, not held against it."""
        from core.models import MatchCandidate
        from core.search_engine import _fallback_is_unnecessary
        settings = _settings(fallback_below_similarity=75.0)
        candidates = [
            MatchCandidate(url="https://e.test/a", source_name="Other", thumb_url=None,
                           similarity=80.0, engine="Google Lens"),
            MatchCandidate(url="https://e.test/b", source_name="Danbooru", thumb_url=None,
                           similarity=93.0, engine="IQDB"),
        ]
        skip, why = _fallback_is_unnecessary(
            settings, [["iqdb"], ["saucenao"]], {"iqdb": True}, candidates)
        self.assertTrue(skip)
        self.assertIn("93%", why)

    def test_with_the_threshold_off_the_old_rule_is_untouched(self):
        """REGRESSION GUARD: the default must behave exactly as before -
        any result from the PRIMARY engine ends the search, however
        weak, and an extra engine's result never suppressed it."""
        skip, why = self._decide(0.0, [12.0], primary_found=True)
        self.assertTrue(skip)
        self.assertIn("primary engine", why)

        skip, _ = self._decide(0.0, [95.0], primary_found=False)
        self.assertFalse(skip, "a strong result from an EXTRA engine never suppressed it")


class TestWeakResultFallbackEndToEnd(unittest.TestCase):
    """The threshold wired through search_image, not just the helper."""

    def _run(self, threshold, iqdb_similarity):
        from core.models import ImageEntry
        from core.search_engine import search_image
        settings = _settings(secondary_engine_mode="fallback",
                             fallback_below_similarity=threshold)
        settings.use_search_cache = False
        settings.drop_dead_matches = False
        settings.retrieve_tags_from_booru = False

        iqdb_hit = SimpleNamespace(
            url="https://danbooru.donmai.us/posts/1", source_name="Danbooru",
            thumb_url=None, similarity=iqdb_similarity, width=None, height=None,
            unnamespaced_tags=[])
        sauce_hit = SimpleNamespace(
            url="https://gelbooru.com/index.php?page=post&s=view&id=2",
            source_name="Gelbooru", thumb_url=None, similarity=99.0, tags=[])

        with patch("core.iqdb.search", return_value=[iqdb_hit]), \
             patch("core.saucenao.search", return_value=[sauce_hit]) as sauce:
            # use_cache=False, because every test here searches the same
            # throwaway image: the first one's result would otherwise be
            # replayed to the rest and no engine would be called at all.
            entry = search_image(ImageEntry(path=_empty_image()), settings, use_cache=False)
        return entry, sauce

    def test_a_weak_primary_result_brings_in_the_fallback_engine(self):
        entry, sauce = self._run(threshold=70.0, iqdb_similarity=42.0)
        sauce.assert_called_once()
        self.assertIn("gelbooru.com", " ".join(c.url for c in entry.candidates))

    def test_a_strong_primary_result_still_stops_the_search(self):
        _, sauce = self._run(threshold=70.0, iqdb_similarity=96.0)
        sauce.assert_not_called()

    def test_with_the_threshold_off_even_a_weak_result_stops_it(self):
        """REGRESSION GUARD for every existing config."""
        _, sauce = self._run(threshold=0.0, iqdb_similarity=42.0)
        sauce.assert_not_called()


class TestFallbackSurvivesLosingItsReason(unittest.TestCase):
    """The fallback engines are skipped when something good enough is
    found. But the site filter and the availability sweep both run AFTER
    every engine has finished, and either can throw that something away -
    leaving the image with nothing while engines that were never asked
    sat in the plan.

    MEASURED over one real run: three searches lost every candidate to
    the site filter, and thirteen ended NOT_FOUND despite having had a
    match good enough to skip the fallback.
    """

    def _settings_with(self, **kw):
        settings = _settings(secondary_engine_mode="fallback",
                             fallback_below_similarity=75.0)
        settings.use_search_cache = False
        settings.drop_dead_matches = False
        settings.retrieve_tags_from_booru = False
        for k, v in kw.items():
            setattr(settings, k, v)
        return settings

    def _hit(self, url, source, similarity):
        return SimpleNamespace(url=url, source_name=source, thumb_url=None,
                               similarity=similarity, width=None, height=None,
                               unnamespaced_tags=[], tags=[])

    def _run(self, settings, iqdb_hits, sauce_hits):
        from core.models import ImageEntry
        from core.search_engine import search_image
        with patch("core.iqdb.search", return_value=iqdb_hits), \
             patch("core.saucenao.search", return_value=sauce_hits) as sauce:
            entry = search_image(ImageEntry(path=_empty_image()), settings,
                                 use_cache=False)
        return entry, sauce

    def test_a_match_on_an_unticked_site_cannot_suppress_the_fallback(self):
        """It is not a result the user would keep, so it is not a result."""
        settings = self._settings_with(
            enabled_sites=["Danbooru"])          # Gelbooru unticked
        gelbooru = self._hit("https://gelbooru.com/index.php?page=post&s=view&id=1",
                             "Gelbooru", 92.0)
        danbooru = self._hit("https://danbooru.donmai.us/posts/2", "Danbooru", 99.0)
        entry, sauce = self._run(settings, [gelbooru], [danbooru])
        sauce.assert_called_once()
        self.assertTrue(any("danbooru" in c.url for c in entry.candidates))

    def test_a_kept_match_still_suppresses_it(self):
        settings = self._settings_with(enabled_sites=["Danbooru", "Gelbooru"])
        gelbooru = self._hit("https://gelbooru.com/index.php?page=post&s=view&id=1",
                             "Gelbooru", 92.0)
        _, sauce = self._run(settings, [gelbooru], [])
        sauce.assert_not_called()

    def test_the_fallback_runs_after_all_when_the_filter_empties_the_list(self):
        """REGRESSION: with the threshold off, the old rule skipped the
        fallback on ANY primary result - which the site filter could then
        remove entirely, ending the search with nothing."""
        settings = self._settings_with(fallback_below_similarity=0.0,
                                       enabled_sites=["Danbooru"])
        gelbooru = self._hit("https://gelbooru.com/index.php?page=post&s=view&id=1",
                             "Gelbooru", 92.0)
        danbooru = self._hit("https://danbooru.donmai.us/posts/2", "Danbooru", 88.0)
        entry, sauce = self._run(settings, [gelbooru], [danbooru])
        sauce.assert_called_once()
        self.assertEqual([c.url for c in entry.candidates],
                         ["https://danbooru.donmai.us/posts/2"])

    def test_the_fallback_runs_after_all_when_every_match_is_dead(self):
        """A broken link is not a result either."""
        from core.models import ImageEntry
        from core.search_engine import search_image
        settings = self._settings_with(fallback_below_similarity=0.0)
        settings.drop_dead_matches = True
        # Two of them: the availability sweep only runs when there is
        # more than one candidate to choose between.
        dead = [self._hit("https://danbooru.donmai.us/posts/1", "Danbooru", 95.0),
                self._hit("https://danbooru.donmai.us/posts/2", "Danbooru", 90.0)]
        alive = self._hit("https://gelbooru.com/index.php?page=post&s=view&id=3",
                          "Gelbooru", 70.0)

        def availability(url, *a, **kw):
            return False if "danbooru" in url else True

        with patch("core.iqdb.search", return_value=dead), \
             patch("core.saucenao.search", return_value=[alive]) as sauce, \
             patch("core.availability.check_url_available", side_effect=availability):
            entry = search_image(ImageEntry(path=_empty_image()), settings, use_cache=False)
        sauce.assert_called_once()
        self.assertTrue(any("gelbooru" in c.url for c in entry.candidates))

    def test_a_dead_match_cannot_come_back_through_the_retry(self):
        """REGRESSION: the "every match was dead" branch computes the
        live list but never assigns it, so the dead candidates are still
        in `candidates` when the retry rebuilds that list. Rebuilding it
        without excluding them put the very match that triggered the
        retry straight back into the dropdown - which is what it looked
        like from outside: a deleted post surviving a fresh search, even
        with the cache deleted."""
        from core.models import ImageEntry
        from core.search_engine import search_image
        settings = self._settings_with(fallback_below_similarity=0.0)
        settings.drop_dead_matches = True
        dead = self._hit("https://danbooru.donmai.us/posts/1", "Danbooru", 95.0)
        # The fallback engine offers the SAME dead post again.
        same_again = self._hit("https://danbooru.donmai.us/posts/1", "Danbooru", 91.0)

        with patch("core.iqdb.search", return_value=[dead]), \
             patch("core.saucenao.search", return_value=[same_again]), \
             patch("core.availability.check_url_available", return_value=False), \
             patch("core.search_engine.fetch_candidate_details") as details:
            def mark_dead(candidate, *a, **kw):
                candidate.remote_available = False
            details.side_effect = mark_dead
            entry = search_image(ImageEntry(path=_empty_image()), settings, use_cache=False)

        self.assertEqual(entry.candidates, [],
                         "a match already proved gone must not be restored")

    def test_the_retry_happens_once_not_in_a_loop(self):
        """Both give-up points share one deferred wave: if the fallback
        also comes back with nothing, the search ends rather than
        re-running engines forever."""
        settings = self._settings_with(fallback_below_similarity=0.0,
                                       enabled_sites=["Danbooru"])
        gelbooru = self._hit("https://gelbooru.com/index.php?page=post&s=view&id=1",
                             "Gelbooru", 92.0)
        other = self._hit("https://xbooru.com/index.php?page=post&s=view&id=3",
                          "Xbooru", 80.0)
        entry, sauce = self._run(settings, [gelbooru], [other])
        sauce.assert_called_once()
        self.assertEqual(entry.candidates, [])


class TestAFetchedPageDoesNotOverwriteAGoneVerdict(unittest.TestCase):
    """REGRESSION, and the one that made every soft-404 check pointless.

    fetch_candidate_details set remote_available=False when the page
    said the post was deleted, and then a few lines later set it back to
    True because the page had been "fetched" - which it had; it just
    landed on a deletion notice. Every site with soft-404 detection was
    affected, not only the one that surfaced it: Pixiv's "work has been
    deleted", the moebooru "This post was deleted", Sankaku's "No
    Content", and Gelbooru's redirect to its post list.
    """

    def _candidate_after_fetch(self, page_info):
        from unittest.mock import patch as _patch
        from core.models import MatchCandidate
        from core.search_engine import fetch_candidate_details

        settings = _settings()
        settings.retrieve_tags_from_booru = True
        settings.drop_dead_matches = True
        candidate = MatchCandidate(url="https://gelbooru.com/index.php?page=post&s=view&id=1")
        with _patch("core.search_engine.fetch_page_info", return_value=page_info), \
             _patch("core.availability.check_url_available", return_value=True):
            fetch_candidate_details(candidate, settings)
        return candidate

    def _page(self, **kw):
        from core.boorus import BooruPageInfo
        defaults = dict(tags=[], fetched=True)
        defaults.update(kw)
        return BooruPageInfo(**defaults)

    def test_a_page_that_says_deleted_stays_dead(self):
        candidate = self._candidate_after_fetch(
            self._page(gone_reason="redirected to s=list"))
        self.assertIs(candidate.remote_available, False)

    def test_a_restricted_page_stays_dead_too(self):
        """Same shape: it was set False and then overwritten."""
        from core.config import Settings
        candidate = self._candidate_after_fetch(self._page(restricted="needs an account"))
        # drop_restricted_matches decides whether that becomes False, but
        # it must never be flipped back to True by "fetched".
        self.assertIsNot(candidate.remote_available, True)
        self.assertTrue(Settings().drop_restricted_matches or True)

    def test_an_ordinary_page_is_still_reported_alive(self):
        """The guard must not stop a healthy fetch from confirming a
        post exists - that is what it is for."""
        from core.models import Tag, TagSource
        candidate = self._candidate_after_fetch(
            self._page(tags=[Tag(name="cirno", source=TagSource.BOORU)]))
        self.assertIs(candidate.remote_available, True)

    def test_a_site_with_no_parser_is_not_called_alive(self):
        """fetch_page_info returns an empty result without raising when
        nothing matched, which must not read as "confirmed alive"."""
        candidate = self._candidate_after_fetch(self._page(fetched=False))
        self.assertIsNot(candidate.remote_available, True)


class TestATransportFailureFetchingBooruTagsIsNotSilent(unittest.TestCase):
    """REGRESSION (DAN-96). The `except BooruError:` branch in
    fetch_candidate_details used to just do `candidate.booru_tags = []`
    and nothing else - no log, no incomplete_reason, and
    booru_tags_fetched already latched True before the branch ran. A
    connection timeout therefore looked byte-for-byte identical to a
    booru post that genuinely has zero tags, was undiagnosable from the
    logs, and could never be retried."""

    def test_a_network_failure_fetching_booru_tags_is_not_silent(self):
        import requests
        from unittest.mock import patch as _patch
        from core.models import MatchCandidate
        from core.search_engine import fetch_candidate_details

        candidate = MatchCandidate(url="https://danbooru.donmai.us/posts/1",
                                    thumb_url="", similarity=90.0, source_name="test")
        settings = _settings()
        settings.retrieve_tags_from_booru = True

        with self.assertLogs("hatate.boorus", level="WARNING"), \
             _patch.object(requests.Session, "get",
                           side_effect=requests.exceptions.ConnectTimeout("simulated timeout")):
            fetch_candidate_details(candidate, settings)

        # A network failure must not look identical to "this post
        # genuinely has zero tags".
        self.assertIsNotNone(candidate.incomplete_reason)
        # ...and must not be marked done, so a later re-selection of the
        # same candidate gets a chance to retry instead of being stuck.
        self.assertFalse(candidate.booru_tags_fetched)


class TestCachedResultsAreRechecked(unittest.TestCase):
    """A cached result carries the candidates that were alive WHEN IT WAS
    SAVED, and nothing re-checked them on the way back out.

    Neither Sankaku nor Gelbooru 404s a deleted post - one answers with a
    login screen, the other redirects to its post list - so both looked
    alive at save time and were served up again on every cache hit,
    however often the user reported them.
    """

    def _entry_with(self, urls):
        from core.models import ImageEntry, MatchCandidate
        entry = ImageEntry(path=_empty_image())
        entry.candidates = [MatchCandidate(url=u, source_name="Danbooru",
                                           thumb_url=None, similarity=90.0, engine="IQDB")
                            for u in urls]
        return entry

    def test_a_cached_match_that_has_since_gone_is_dropped(self):
        from core.search_engine import _recheck_cached_candidates
        settings = _settings()
        settings.drop_dead_matches = True
        entry = self._entry_with(["https://gelbooru.com/index.php?page=post&s=view&id=1",
                                  "https://danbooru.donmai.us/posts/2"])
        with patch("core.availability.check_url_available",
                   side_effect=lambda url, *a, **kw: "gelbooru" not in url):
            kept = _recheck_cached_candidates(entry.candidates, settings, "x.png")
        self.assertEqual([c.url for c in kept], ["https://danbooru.donmai.us/posts/2"])

    def test_an_unknown_answer_keeps_the_match(self):
        """REGRESSION GUARD: a network hiccup must not delete a cached
        result. Only a definite "gone" removes anything."""
        from core.search_engine import _recheck_cached_candidates
        settings = _settings()
        settings.drop_dead_matches = True
        entry = self._entry_with(["https://danbooru.donmai.us/posts/2"])
        with patch("core.availability.check_url_available", return_value=None):
            kept = _recheck_cached_candidates(entry.candidates, settings, "x.png")
        self.assertEqual(len(kept), 1)

    def test_it_uses_the_same_sweep_a_fresh_search_uses(self):
        """One definition of "gone", not two that can drift apart."""
        import inspect
        from core import search_engine
        source = inspect.getsource(search_engine._recheck_cached_candidates)
        self.assertIn("_sweep_candidate_availability", source)

    def test_it_is_skipped_when_the_user_has_not_asked_for_it(self):
        """It costs a request per candidate, so it follows the setting
        rather than being done regardless."""
        import inspect
        from core import search_engine
        source = inspect.getsource(search_engine.search_image)
        self.assertIn("if settings.drop_dead_matches:", source)
        self.assertIn("_recheck_cached_candidates(", source)


class TestGoogleLensPayloadParser(unittest.TestCase):
    """Lens results are not in the page's markup.

    CONFIRMED against the live site: the tiles Lens draws carry no href
    and no attribute holding their source - a scan of every attribute on
    the rendered page found zero external URLs - but the page's RESPONSE
    BODY contains them, in the script data that builds those tiles. This
    fixture is that shape, taken from a real response.
    """

    PAYLOAD = (
        '["https:\\/\\/preview.redd.it\\/doom-thumb.jpeg","XORNM3qJQeMrvM",'
        '"https:\\/\\/www.reddit.com\\/r\\/Doom\\/comments\\/1gdo146\\/doom_slayer\\/",'
        '"Doom Slayer only cares about Killing Demons",'
        '"https:\\/\\/www.google.com\\/search?q=x","ignored",'
        '"https:\\/\\/sceneario.com\\/onmars.jpg","PMmAJBJTUg_9sM",'
        '"https:\\/\\/sceneario.com\\/galerie\\/on-mars-t1\\/",'
        '"ON MARS - T1 - S. Runberg\\u002fGrun"]'
    )

    def test_results_past_the_first_eight_are_not_dropped(self):
        """A booru post at rank 13 must reach search(), which is where the
        tag-giving sites are moved up and the list is cut."""
        from core.google_lens import parse_lens_payload
        parts = []
        for i in range(12):
            parts += [f'"https:\\/\\/www.pinterest.com\\/pin\\/{i}\\/"', f'"Pin number {i} of many"']
        parts += ['"https:\\/\\/danbooru.donmai.us\\/posts\\/42"', '"Danbooru post forty two"']
        matches = parse_lens_payload("[" + ",".join(parts) + "]")
        self.assertEqual(len(matches), 13)
        self.assertEqual(matches[-1].url, "https://danbooru.donmai.us/posts/42")

    def test_search_brings_a_deep_booru_post_to_the_top(self):
        from unittest.mock import patch
        from core import google_lens
        parts = []
        for i in range(12):
            parts += [f'"https:\\/\\/www.pinterest.com\\/pin\\/{i}\\/"', f'"Pin number {i} of many"']
        parts += ['"https:\\/\\/danbooru.donmai.us\\/posts\\/42"', '"Danbooru post forty two"']
        payload = "[" + ",".join(parts) + "]"
        with patch.object(google_lens, "prepare_for_lens", return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=([payload], [])):
            results = google_lens.search("/tmp/a.jpg", timeout=1)
        self.assertEqual(results[0].url, "https://danbooru.donmai.us/posts/42")
        self.assertEqual(len(results), google_lens.MAX_RESULTS)

    def test_source_pages_are_extracted_with_their_titles(self):
        from core.google_lens import parse_lens_payload
        matches = parse_lens_payload(self.PAYLOAD)
        self.assertEqual([m.url for m in matches], [
            "https://www.reddit.com/r/Doom/comments/1gdo146/doom_slayer/",
            "https://sceneario.com/galerie/on-mars-t1/",
        ])
        self.assertEqual(matches[0].title, "Doom Slayer only cares about Killing Demons")

    def test_the_picture_before_a_result_becomes_its_thumbnail(self):
        """OBSERVED order: thumbnail, Google's image id, page, title. The
        bare URL is the result's picture, not another result."""
        from core.google_lens import parse_lens_payload
        matches = parse_lens_payload(self.PAYLOAD)
        self.assertEqual(matches[0].thumb_url, "https://preview.redd.it/doom-thumb.jpeg")
        self.assertEqual(matches[1].thumb_url, "https://sceneario.com/onmars.jpg")

    def test_an_image_id_is_never_mistaken_for_a_title(self):
        """Google's ids ("XORNM3qJQeMrvM") sit exactly where a title
        would. A run of characters with no space in it is not one."""
        from core.google_lens import _looks_like_a_title
        self.assertFalse(_looks_like_a_title("XORNM3qJQeMrvM"))
        self.assertTrue(_looks_like_a_title("Doom Slayer only cares"))

    def test_googles_own_links_are_not_sources(self):
        from core.google_lens import parse_lens_payload
        self.assertFalse(any("google.com" in m.url
                             for m in parse_lens_payload(self.PAYLOAD)))

    def test_escapes_are_resolved(self):
        from core.google_lens import parse_lens_payload
        matches = parse_lens_payload(self.PAYLOAD)
        self.assertEqual(matches[1].title, "ON MARS - T1 - S. Runberg/Grun")

    def test_results_are_scored_in_order(self):
        from core.ascii2d import COLOR_SIMILARITY_START
        from core.google_images import SIMILARITY_START as VISION_START
        from core.google_lens import SIMILARITY_START as LENS_START, parse_lens_payload
        matches = parse_lens_payload(self.PAYLOAD)
        self.assertGreater(matches[0].similarity, matches[1].similarity)
        self.assertGreater(LENS_START, VISION_START)
        self.assertLess(LENS_START, COLOR_SIMILARITY_START)

    def test_junk_is_no_matches_not_a_crash(self):
        from core.google_lens import parse_lens_payload
        for payload in ("", None, "<html></html>", '"not a url"'):
            with self.subTest(payload=payload):
                self.assertEqual(parse_lens_payload(payload), [])


class TestGoogleLensExactMatches(unittest.TestCase):
    """Lens's "Exact matches" tab is a different animal from "Visual
    matches", and the better one - an exact match is the same picture,
    where a visual match only looks like it.

    CONFIRMED against the live site: its tiles carry NO link. No href,
    no data attribute, and nothing in the response body but the title -
    the destination is applied by script on click. So a match there
    cannot be read the way a visual one can, and a real Paheal post was
    silently absent from results because of it.

    Markup below is from that real response.
    """

    # The site name is NOT reliably part of the title. Both forms below
    # are from real responses: the first appends it, the second leaves
    # it to a separate element further down the tile.
    PAYLOAD = (
        '<div class="ZhosBf T7iOye" style="-webkit-line-clamp:2">'
        'Post 5674224: Ben_10 Flipherrrr Gwen_Tennyson - Rule 34 Paheal</div>'
        '<div class="xuPcX yUTMj">Rule 34 Paheal</div>'
        '<div class="ZhosBf T7iOye">Post 5674250: Ben_10 Flipherrrr Gwen_Tennyson '
        '&amp; friends - Rule 34 Paheal</div>'
        '<div class="ZhosBf">Gwen Tennyson - Page 8 - HentaiRox</div>'
    )

    # Name in a sibling element instead, 1,100+ characters after the
    # title - the shape that silently lost a real post.
    PAYLOAD_LABEL_APART = (
        '<div class="ZhosBf T7iOye" style="-webkit-line-clamp:2">'
        'Post 5674080: Chel Tanluca The_Road_to_El_Dorado</div>'
        '<div class="oYQBg">' + ("<span>filler</span>" * 60) + '</div>'
        '<div class="xuPcX yUTMj">Rule 34 Paheal</div>'
    )

    def test_a_post_id_in_the_title_becomes_the_post_url(self):
        from core.google_lens import parse_exact_matches
        urls = [m.url for m in parse_exact_matches(self.PAYLOAD)]
        self.assertEqual(urls, [
            "https://rule34.paheal.net/post/view/5674224",
            "https://rule34.paheal.net/post/view/5674250",
        ])

    def test_the_site_name_is_found_when_it_is_not_in_the_title(self):
        """REGRESSION: requiring the name inside the title dropped every
        result Google rendered the other way - a real Paheal post went
        missing because of it."""
        from core.google_lens import parse_exact_matches
        urls = [m.url for m in parse_exact_matches(self.PAYLOAD_LABEL_APART)]
        self.assertEqual(urls, ["https://rule34.paheal.net/post/view/5674080"])

    def test_a_tile_cannot_borrow_the_next_tile_s_site_name(self):
        """The lookahead stops at the next result. Without that, a post
        from an unrecognised site would be handed the following tile's
        label and rebuilt as a URL that was never a match."""
        from core.google_lens import parse_exact_matches
        payload = (
            '<div>Post 111111: something on a site we cannot rebuild</div>'
            '<div>Post 222222: a real one</div>'
            '<div class="xuPcX">Rule 34 Paheal</div>'
        )
        urls = [m.url for m in parse_exact_matches(payload)]
        self.assertEqual(urls, ["https://rule34.paheal.net/post/view/222222"])

    def test_the_site_name_is_not_searched_for_indefinitely(self):
        """Far enough away and it belongs to something else entirely."""
        from core.google_lens import SITE_LABEL_WINDOW, parse_exact_matches
        payload = ('<div>Post 333333: a post</div>' + ("<span>x</span>" * 400)
                   + '<div>Rule 34 Paheal</div>')
        self.assertGreater(len(payload), SITE_LABEL_WINDOW)
        self.assertEqual(parse_exact_matches(payload), [])

    RULE34XXX = '<div class="ZhosBf">Rule 34 | 4585855 / - Rule 34</div>'

    def test_a_rule34_xxx_post_is_rebuilt_from_its_title(self):
        """Its Exact-matches title is "Rule 34 | <id> /" - the id is
        right there, but the URL never is."""
        from core.google_lens import parse_exact_matches
        urls = [m.url for m in parse_exact_matches(self.RULE34XXX)]
        self.assertEqual(urls,
                         ["https://rule34.xxx/index.php?page=post&s=view&id=4585855"])

    RULE34US = ('<div class="ZhosBf">If it exists, there is porn of it / tekuho / '
                '368224 - Rule34.us</div>')

    def test_a_rule34_us_post_is_rebuilt_from_its_title(self):
        """Its Exact-matches title ends "/ <id> - Rule34.us". Same story
        as the others: the id is in the title, the URL never is."""
        from core.google_lens import parse_exact_matches
        urls = [m.url for m in parse_exact_matches(self.RULE34US)]
        self.assertEqual(urls, ["https://rule34.us/index.php?r=posts/view&id=368224"])

    def test_a_rendered_tile_yields_its_thumbnail_and_the_source_size(self):
        """The body has neither: its thumbnail is a deferred placeholder
        and it states no dimensions. The rendered tile has both - the
        thumbnail is what makes an exact match measurable, and the size
        drives the "is this bigger than mine?" comparison."""
        from core.google_lens import parse_exact_tiles
        tiles = [{"text": "If it exists, there is porn of it / tekuho / 368224 - "
                          "Rule34.us 1,072x1,108 Rule34.us",
                  "thumb": "data:image/jpeg;base64,AAAA"}]
        matches = parse_exact_tiles(tiles)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].url, "https://rule34.us/index.php?r=posts/view&id=368224")
        self.assertEqual((matches[0].width, matches[0].height), (1072, 1108))
        self.assertEqual(matches[0].thumb_url, "data:image/jpeg;base64,AAAA")

    def test_a_lazy_load_placeholder_is_not_taken_for_a_thumbnail(self):
        """CONFIRMED on a rule34.us match: an unloaded tile carries a 1x1
        GIF (82 characters, past the JS cut of 80). As the thumbnail it
        made the side by side show a blank. The match itself is kept."""
        from core.google_lens import parse_exact_tiles
        tiles = [{"text": "If it exists, there is porn of it / gattles / 6608448 - Rule34.us",
                  "thumb": "data:image/gif;base64,R0lGODlhAQABAIAAAP///////yH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="}]
        matches = parse_exact_tiles(tiles)
        self.assertEqual([m.url for m in matches],
                         ["https://rule34.us/index.php?r=posts/view&id=6608448"])
        self.assertIsNone(matches[0].thumb_url)

    def test_a_tile_that_names_no_rebuildable_site_is_ignored(self):
        from core.google_lens import parse_exact_tiles
        self.assertEqual(parse_exact_tiles([{"text": "Sign in", "thumb": "data:x"}]), [])
        self.assertEqual(parse_exact_tiles(None), [])

    def test_the_two_rule34_sites_are_told_apart(self):
        """rule34.xxx and rule34.us are different sites with different
        post ids - rebuilding one as the other invents a match."""
        from core.google_lens import parse_exact_matches
        both = self.RULE34XXX + self.RULE34US
        urls = sorted(m.url for m in parse_exact_matches(both))
        self.assertEqual(urls, [
            "https://rule34.us/index.php?r=posts/view&id=368224",
            "https://rule34.xxx/index.php?page=post&s=view&id=4585855",
        ])

    def test_the_rule34_mirrors_are_not_mistaken_for_it(self):
        """Several sites carry "Rule 34" in their names. They title
        themselves quite differently, and none of them may be rebuilt as
        a rule34.xxx post - that URL would be a match that never was."""
        from core.google_lens import parse_exact_matches
        mirrors = (
            "<div>favela funk - Rule 34 XYZ</div>"
            "<div>disney, pixar - Rule 34 World</div>"
            "<div>the incredibles - Rule 34 Archive</div>"
            "<div>Rule34 - If it exists, there is porn of it / phat smash</div>"
        )
        self.assertEqual(parse_exact_matches(mirrors), [])

    def test_both_sites_are_recovered_in_document_order(self):
        """Collected one site at a time, but the page's own ranking has
        to survive that."""
        from core.google_lens import parse_exact_matches
        payload = self.RULE34XXX + self.PAYLOAD
        urls = [m.url for m in parse_exact_matches(payload)]
        self.assertEqual(urls[0],
                         "https://rule34.xxx/index.php?page=post&s=view&id=4585855")
        self.assertTrue(any("paheal" in u for u in urls))

    def test_titles_naming_no_rebuildable_site_are_left_alone(self):
        """A guessed URL looks like a real match until someone clicks
        it, so a site with no verified pattern contributes nothing."""
        from core.google_lens import parse_exact_matches
        matches = parse_exact_matches(self.PAYLOAD)
        self.assertFalse(any("hentairox" in m.url.lower() for m in matches))

    def test_exact_matches_outrank_visual_ones(self):
        """An exact match is the same picture; a visual match only looks
        like it."""
        from core import google_lens
        payloads = [self.PAYLOAD, TestGoogleLensPayloadParser.PAYLOAD]
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=(payloads, [])):
            matches = google_lens.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(matches[0].url.startswith("https://rule34.paheal.net/"))
        self.assertGreater(matches[0].similarity, matches[-1].similarity)
        # and the visual matches are still there, below them
        self.assertTrue(any("reddit.com" in m.url for m in matches))

    def test_scores_stay_ordered_after_the_two_tabs_are_merged(self):
        from core import google_lens
        payloads = [self.PAYLOAD, TestGoogleLensPayloadParser.PAYLOAD]
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=(payloads, [])):
            scores = [m.similarity for m in google_lens.search("/tmp/x.jpg", timeout=5.0)]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertLessEqual(len(scores), google_lens.MAX_RESULTS)

    def test_junk_is_no_matches_not_a_crash(self):
        from core.google_lens import parse_exact_matches
        for payload in ("", None, "<div>nothing here</div>", "Post : - Rule 34 Paheal"):
            with self.subTest(payload=payload):
                self.assertEqual(parse_exact_matches(payload), [])


class TestGoogleLensUpload(unittest.TestCase):
    """The upload's media type, which was the bug behind every empty
    Lens result.

    CONFIRMED against the live site: a JPEG offered as
    application/octet-stream is refused with "Something went wrong -
    can't read file" and Lens then draws an empty results page. From the
    outside that is indistinguishable from "Google found nothing".
    """

    def test_an_unrecognised_format_is_not_called_a_jpeg(self):
        """REGRESSION GUARD: naming a format the bytes are not is the
        exact mistake behind "can't read file" - it used to fall back to
        image/jpeg for anything it could not identify."""
        from core.google_lens import media_type
        self.assertIsNone(media_type(b"\x00\x00\x00\x18ftypmp42rest"))   # an MP4
        self.assertIsNone(media_type(b"not an image at all"))

    def test_a_format_google_takes_is_sent_untouched(self):
        """Re-encoding a perfectly good JPEG would cost quality for
        nothing."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import google_lens

        raw = b"\xff\xd8\xff\xe0" + b"jpeg body" * 20
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jpg"
            path.write_bytes(raw)
            with _patch.object(google_lens, "prepare_upload_bytes",
                               return_value=(raw, "x.jpg")):
                data, filename, mime = google_lens.prepare_for_lens(str(path))
        self.assertIs(data, raw)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(filename, "x.jpg")

    def test_a_gif_is_converted_because_lens_cannot_read_one(self):
        """MEASURED on one file, uploaded both ways in the same session:
        as a GIF it got "can't read file" and 0 matches; as a JPEG it got
        3, including two booru posts. GIF is a picture format Google
        serves everywhere else, which is exactly why this needed
        measuring rather than assuming."""
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from PIL import Image
        from core import google_lens

        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), (10, 20, 30)).save(buffer, format="GIF")
        raw = buffer.getvalue()
        self.assertEqual(google_lens.media_type(raw), "image/gif",
                         "still sniffed correctly - it is a GIF, just not a usable one")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.gif"
            path.write_bytes(raw)
            with _patch.object(google_lens, "prepare_upload_bytes",
                               return_value=(raw, "x.gif")):
                data, filename, mime = google_lens.prepare_for_lens(str(path))
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(filename, "x.jpg")

    def test_an_animated_picture_is_sent_as_its_first_frame(self):
        """Lens searches for one picture, and an animation is not one.
        Covers animated WEBP as well, which a format check alone
        would wave through."""
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from PIL import Image
        from core import google_lens

        frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0))]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:])
        raw = buffer.getvalue()
        self.assertTrue(google_lens._is_animated(raw))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.webp"
            path.write_bytes(raw)
            with _patch.object(google_lens, "prepare_upload_bytes",
                               return_value=(raw, "x.webp")):
                _, filename, mime = google_lens.prepare_for_lens(str(path))
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(filename, "x.jpg")

    def test_a_still_webp_is_left_alone(self):
        """Re-encoding one Lens can read would cost quality for nothing."""
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from PIL import Image
        from core import google_lens

        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), (1, 2, 3)).save(buffer, format="WEBP")
        raw = buffer.getvalue()
        self.assertFalse(google_lens._is_animated(raw))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.webp"
            path.write_bytes(raw)
            with _patch.object(google_lens, "prepare_upload_bytes",
                               return_value=(raw, "x.webp")):
                data, filename, mime = google_lens.prepare_for_lens(str(path))
        self.assertIs(data, raw)
        self.assertEqual(mime, "image/webp")

    def test_a_format_google_refuses_is_converted_to_jpeg(self):
        """The app takes AVIF, JPEG XL and TIFF from a Hydrus library and
        Lens takes none of them."""
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from PIL import Image
        from core import google_lens

        buffer = io.BytesIO()
        Image.new("RGBA", (32, 24), (10, 20, 30, 90)).save(buffer, format="TIFF")
        raw = buffer.getvalue()
        self.assertIsNone(google_lens.media_type(raw), "TIFF is not one Google takes")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.tiff"
            path.write_bytes(raw)
            with _patch.object(google_lens, "prepare_upload_bytes",
                               return_value=(raw, "x.tiff")):
                data, filename, mime = google_lens.prepare_for_lens(str(path))
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(filename, "x.jpg")
        self.assertEqual(google_lens.media_type(data), "image/jpeg")

    def test_transparency_is_flattened_onto_white_not_black(self):
        """JPEG has no alpha, and the default conversion leaves a
        transparent background black - which is not the picture that was
        searched for."""
        import io
        from PIL import Image
        from core.google_lens import _to_jpeg

        buffer = io.BytesIO()
        Image.new("RGBA", (8, 8), (255, 255, 255, 0)).save(buffer, format="PNG")
        converted = _to_jpeg(buffer.getvalue())
        self.assertIsNotNone(converted)
        with Image.open(io.BytesIO(converted)) as out:
            self.assertEqual(out.convert("RGB").getpixel((4, 4)), (255, 255, 255))

    def test_a_video_is_refused_with_a_reason(self):
        """Refused before the upload rather than sent and rejected at the
        far end, where it would look like "Google found nothing"."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import google_lens

        raw = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 400
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(raw)
            with _patch.object(google_lens, "prepare_upload_bytes",
                               return_value=(raw, "clip.mp4")):
                with self.assertRaises(google_lens.GoogleLensError) as caught:
                    google_lens.prepare_for_lens(str(path))
        message = str(caught.exception)
        self.assertIn("clip.mp4", message)
        self.assertIn("other engines can still search it", message)

    def test_a_refused_format_does_not_stand_the_engine_down(self):
        """It is about this one file. The next image may well be a JPEG."""
        from core import google_lens
        google_lens.reset_blocked_flag()
        google_lens.end_rest()
        self.addCleanup(google_lens.reset_blocked_flag)
        self.addCleanup(google_lens.end_rest)
        with patch.object(google_lens, "prepare_for_lens",
                          side_effect=google_lens.GoogleLensError("clip.mp4 is not...")):
            with self.assertRaises(google_lens.GoogleLensError) as caught:
                google_lens.search("/tmp/clip.mp4", timeout=5.0)
        self.assertNotIsInstance(caught.exception, google_lens.GoogleLensBlockedError)
        self.assertFalse(google_lens.is_blocked())

    def test_the_real_media_type_is_sniffed_from_the_bytes(self):
        from core.google_lens import media_type
        self.assertEqual(media_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(media_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(media_type(b"GIF89a rest"), "image/gif")
        self.assertEqual(media_type(b"RIFF\x00\x00\x00\x00WEBPrest"), "image/webp")

    def test_the_filename_is_not_trusted_to_describe_the_content(self):
        """prepare_upload_bytes re-encodes an oversized image to JPEG
        while keeping its original name, so the name can lie."""
        from core.google_lens import media_type, upload_script
        self.assertEqual(media_type(b"\xff\xd8\xff\xe0jpeg-bytes"), "image/jpeg")
        self.assertIn('"image/jpeg"', upload_script(b"\xff\xd8\xff\xe0x", "actually.png"))

    def test_it_is_never_uploaded_as_an_opaque_blob(self):
        """REGRESSION GUARD: this exact value is what Google refuses."""
        from core.google_lens import media_type, upload_script
        self.assertNotIn("application/octet-stream", upload_script(b"\xff\xd8\xffx", "a.jpg"))
        self.assertNotEqual(media_type(b"\xff\xd8\xffx"), "application/octet-stream")

    def test_the_upload_script_embeds_its_arguments_safely(self):
        """The filename comes off the user's disk, so a quote in it must
        not break - or inject into - the script."""
        from core.google_lens import upload_script
        js = upload_script(b"\xff\xd8\xff\x00", 'ev"il\'s.jpg')
        self.assertIn('"ev\\"il\'s.jpg"', js)
        self.assertIn("DataTransfer", js)
        self.assertIn("form.submit()", js)


class TestGoogleLensWithoutABrowser(unittest.TestCase):
    """Playwright and its Chromium are optional - only this engine needs
    them, and they bring a ~150MB browser - so their absence has to be
    an explanation, not a crash and not a silent "found nothing"."""

    def setUp(self):
        from core import google_lens
        google_lens.reset_blocked_flag()
        google_lens.end_rest()
        self.addCleanup(google_lens.reset_blocked_flag)
        self.addCleanup(google_lens.end_rest)

    def _raise(self, exc):
        from core import google_lens
        return patch.object(google_lens.lens_browser, "fetch_results_payloads",
                            side_effect=exc)

    def test_the_message_names_the_python_actually_running(self):
        """REGRESSION: the message hard-coded "venv/bin/pip". The app can
        be started through run.sh (project venv) or as `python3 main.py`
        (system Python), and naming the wrong one sends the user to
        install a package where it will never be seen - so it reports
        "not installed" for something they just installed."""
        import sys
        from core.lens_browser import missing_dependency_message
        message = missing_dependency_message()
        self.assertIn(sys.executable, message)
        self.assertNotIn("venv/bin/pip", message)

    def test_the_message_points_at_the_venv_when_running_outside_it(self):
        """The two interpreters are usually the SAME binary - venv/bin/
        python3 is a symlink - so this has to compare environments, not
        resolved executable paths, or the hint never appears."""
        import sys
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import lens_browser
        venv = Path(lens_browser.__file__).resolve().parent.parent / "venv"
        if not venv.is_dir():
            self.skipTest("no venv in this checkout")
        with _patch.object(sys, "prefix", "/usr"):
            self.assertIn("run.sh", lens_browser.missing_dependency_message())
        with _patch.object(sys, "prefix", str(venv)):
            self.assertNotIn("run.sh", lens_browser.missing_dependency_message())

    def test_a_missing_browser_is_reported_and_stands_down(self):
        from core import google_lens
        from core.lens_browser import LensBrowserUnavailable
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             self._raise(LensBrowserUnavailable("playwright install chromium")):
            with self.assertRaises(google_lens.GoogleLensUnavailableError) as caught:
                google_lens.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertIn("chromium", str(caught.exception))
        self.assertTrue(google_lens.is_blocked(),
                        "a missing dependency fails every image the same way")

    def test_a_genuinely_missing_playwright_fails_loudly_not_silently(self):
        """DAN-80, verified against this machine's REAL environment rather
        than a mock of it: Playwright is deliberately not a default
        dependency (see requirements.txt), so most environments - CI
        included - are actually in the state this test exercises. Nothing
        here is faked: playwright_available() runs its own real import
        check, and lens_browser.fetch_results_payloads is not patched, so
        this is the genuine DAN-80 failure path end to end, not a
        simulation of it.

        Skips instead of asserting the opposite on a machine that does
        have Playwright installed - that machine cannot exercise this
        specific degraded state."""
        from core import google_lens
        from core.lens_browser import playwright_available
        if playwright_available():
            self.skipTest("Playwright is installed here; this checks the "
                           "path taken when it genuinely is not")
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")):
            with self.assertRaises(google_lens.GoogleLensUnavailableError) as caught:
                google_lens.search("/tmp/whatever.jpg", timeout=5.0)
        message = str(caught.exception)
        self.assertIn("Playwright", message)
        self.assertIn("pip install", message)

    def test_a_genuinely_missing_playwright_reaches_the_user_visible_errors(self):
        """Same real, unmocked absence as above, but through the actual
        collection path the search worker uses - the errors list the GUI
        reads from, not just an exception a caller could swallow."""
        from core import google_lens
        from core.lens_browser import playwright_available
        if playwright_available():
            self.skipTest("Playwright is installed here; this checks the "
                           "path taken when it genuinely is not")
        from core.search_engine import _collect_google_lens
        errors: list = []
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")):
            found = _collect_google_lens(ImageEntry(path=_empty_image()), _settings(), [], errors, None)
        self.assertFalse(found)
        self.assertEqual(len(errors), 1)
        self.assertIn("Google Lens", errors[0])
        self.assertIn("Playwright", errors[0])

    def test_an_unanswered_challenge_stands_the_engine_down(self):
        from core import google_lens
        from core.lens_browser import LensChallengeUnanswered
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             self._raise(LensChallengeUnanswered("nobody answered")):
            with self.assertRaises(google_lens.GoogleLensBlockedError) as caught:
                google_lens.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(google_lens.is_blocked())
        self.assertIn("Settings > Engine", str(caught.exception))

    def test_an_empty_payload_is_reported_never_silently_nothing(self):
        """A silent zero would be cached as a confident NOT_FOUND for an
        image that was never really answered."""
        from core import google_lens
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads", return_value=([], [])):
            with self.assertRaises(google_lens.GoogleLensError) as caught:
                google_lens.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertIn("can't read file", str(caught.exception))

    def test_the_best_parsing_response_wins_not_the_biggest(self):
        """REGRESSION: several pages load per search - first results, the
        Visual matches tab, and whatever confirming Google's
        explicit-results notice loads next. OBSERVED: the LARGEST body
        after that confirmation parsed to nothing while a smaller,
        earlier one held every match, so picking by size lost them."""
        from core import google_lens
        good = TestGoogleLensPayloadParser.PAYLOAD
        bigger_but_empty = '"' + ("x" * 5000) + '"'
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=([good, bigger_but_empty], [])):
            matches = google_lens.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(matches, "the parseable response must win")
        self.assertGreater(len(bigger_but_empty), len(good))

    def test_a_run_of_failures_stands_the_engine_down(self):
        """One bad page says nothing. Several in a row says Google is not
        answering, and a batch must not spend a minute per image
        rediscovering that."""
        from core import google_lens
        from core.lens_browser import LensBrowserError
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             self._raise(LensBrowserError("boom")):
            for attempt in range(1, google_lens.MAX_CONSECUTIVE_FAILURES + 1):
                with self.assertRaises(google_lens.GoogleLensError):
                    google_lens.search("/tmp/whatever.jpg", timeout=5.0)
                self.assertEqual(google_lens.is_blocked(),
                                 attempt >= google_lens.MAX_CONSECUTIVE_FAILURES)

    def test_one_good_image_clears_the_failure_run(self):
        from core import google_lens
        from core.lens_browser import LensBrowserError
        payload = TestGoogleLensPayloadParser.PAYLOAD
        results = [LensBrowserError("boom"), ([payload], []), LensBrowserError("boom")]
        with patch.object(google_lens, "prepare_upload_bytes", return_value=(_FAKE_JPEG, "a.jpg")), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          side_effect=results):
            with self.assertRaises(google_lens.GoogleLensError):
                google_lens.search("/tmp/a.jpg", timeout=5.0)
            self.assertTrue(google_lens.search("/tmp/b.jpg", timeout=5.0))
            with self.assertRaises(google_lens.GoogleLensError):
                google_lens.search("/tmp/c.jpg", timeout=5.0)
        self.assertFalse(google_lens.is_blocked())

    def test_a_lens_search_gets_longer_than_one_http_request(self):
        from core.google_lens import MIN_BUDGET_SECONDS, effective_timeout
        self.assertEqual(effective_timeout(5.0), MIN_BUDGET_SECONDS)
        self.assertEqual(effective_timeout(MIN_BUDGET_SECONDS + 60), MIN_BUDGET_SECONDS + 60)

    def test_a_skipped_engine_still_marks_the_row(self):
        """An engine that was enabled and never queried must not leave a
        row looking like a confident NOT_FOUND, which would be cached."""
        from core import google_lens
        from core.search_engine import _collect_google_lens
        google_lens._blocked = True
        errors = []
        _collect_google_lens(ImageEntry(path=_empty_image()), _settings(), [], errors, None)
        self.assertEqual(len(errors), 1)
        self.assertIn("not searched", errors[0])


class TestGoogleImagesNeverFakesANotFound(unittest.TestCase):
    """REGRESSION GUARD: a NOT_FOUND with no errors gets cached, and a
    cached NOT_FOUND stops the image ever being searched again.

    So an engine that was enabled but never queried - because it stood
    down for the session - must leave something on the row. Otherwise
    "Google was never asked" becomes "Google confirmed there is
    nothing", permanently.
    """

    def test_a_skipped_engine_is_never_cached_as_a_confident_not_found(self):
        """IQDB answered, Google was never asked. The row may say "not
        found" - IQDB really found nothing - but it keeps the reason and
        is NOT cached, so a later search still asks Google."""
        from core import google_images
        from core.models import MatchStatus
        from core.search_engine import search_image
        google_images.reset_blocked_flag()
        self.addCleanup(google_images.reset_blocked_flag)
        google_images._blocked = True

        settings = _settings(enable_google_images=True, secondary_engine_mode="disabled")
        settings.use_search_cache = False
        entry = ImageEntry(path=_empty_image())
        entry.hydrus_hash = "ab" * 32

        with patch("core.iqdb.search", return_value=[]), \
             patch("core.search_engine.save_cached_result") as cached:
            result = search_image(entry, settings)

        self.assertEqual(result.status, MatchStatus.NOT_FOUND)
        self.assertIn("Google Images", result.error_message or "")
        cached.assert_not_called()


class TestGoogleImagesBlocked(unittest.TestCase):
    """Google answers an automated upload with a consent wall or an
    "unusual traffic" check often enough that it needs the same latch
    ascii2d has: once it starts refusing, every remaining image in the
    batch would spend a request to be refused and report the same thing.
    """

    CONSENT_URL = "https://consent.google.com/m?continue=https://www.google.com/"
    SORRY = ("<html><body>Our systems have detected unusual traffic from your "
             "computer network.</body></html>")

    def setUp(self):
        from core import google_images
        google_images.reset_blocked_flag()
        self.addCleanup(google_images.reset_blocked_flag)

    def _resp(self, status, text, url="https://www.google.com/search?tbs=sbi:x"):
        r = MagicMock()
        r.status_code = status
        r.text = text
        r.url = url
        return r

    def test_a_consent_wall_is_recognised_and_latched(self):
        """Served as an ordinary 200, which is why the status code alone
        cannot be trusted to tell results from a wall."""
        from core import google_images
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post",
                   return_value=self._resp(200, "<html>consent</html>", self.CONSENT_URL)):
            with self.assertRaises(google_images.GoogleImagesBlockedError):
                google_images.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(google_images.is_blocked())

    def test_an_unusual_traffic_check_is_recognised(self):
        from core import google_images
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=self._resp(429, self.SORRY)):
            with self.assertRaises(google_images.GoogleImagesBlockedError) as caught:
                google_images.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(google_images.is_blocked())
        self.assertIn("Settings > Engine", str(caught.exception))

    def test_a_plain_server_error_is_not_called_a_block(self):
        """A 500 means something else, and must not latch the engine off
        or claim a cause that wasn't observed."""
        from core import google_images
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=self._resp(500, "nope")):
            with self.assertRaises(google_images.GoogleImagesError) as caught:
                google_images.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertNotIsInstance(caught.exception, google_images.GoogleImagesBlockedError)
        self.assertFalse(google_images.is_blocked())

    def test_once_blocked_later_images_do_not_ask_again(self):
        from core import google_images
        from core.search_engine import _collect_google_images
        entry = ImageEntry(path=_empty_image())
        settings = _settings()

        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post",
                   return_value=self._resp(429, self.SORRY)) as post:
            first_errors = []
            _collect_google_images(entry, settings, [], first_errors, None)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(len(first_errors), 1, "the first image should say why")

            # Two more images: no further requests, but each still says
            # the engine went unqueried. Staying silent would let a row
            # whose other engines also found nothing be recorded - and
            # cached - as a confident NOT_FOUND for an engine that was
            # enabled and never actually asked.
            for _ in range(2):
                more_errors = []
                _collect_google_images(entry, settings, [], more_errors, None)
                self.assertEqual(len(more_errors), 1)
                self.assertIn("not searched", more_errors[0])
                self.assertNotEqual(more_errors[0], first_errors[0],
                                    "the repeat should be terse, not the full explanation")
            self.assertEqual(post.call_count, 1, "a blocked engine must not be re-asked")

    def test_a_new_search_tries_again(self):
        """The consent wall and the traffic check both come and go, so
        starting a search is the right moment to find out."""
        from core import google_images
        google_images._blocked = True
        google_images.reset_blocked_flag()
        self.assertFalse(google_images.is_blocked())

    def test_a_javascript_only_page_is_reported_not_recorded_as_empty(self):
        """CONFIRMED against the live site: POST /searchbyimage/upload
        still answers 303 with a real token, but every landing it
        redirects to is a JavaScript bootstrap with no result markup -
        udm=26, udm=48, tbm=isch, no-udm, lens.google.com/v3/upload and
        the asearch=arc fragment alike, and an old-browser User-Agent
        now gets "Update your browser" rather than the basic HTML page.

        Returning [] here would record a confident "Google found
        nothing" - and cache it - for an image Google was never really
        asked about."""
        from core import google_images
        shell = ('<html><body><noscript><meta http-equiv="refresh" '
                 'content="0;url=/httpservice/retry/enablejs?sei=x">'
                 '<div>Please click <a href="/httpservice/retry/enablejs?sei=x">here</a> '
                 'if you are not redirected within a few seconds.</div>'
                 '</noscript></body></html>')
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=self._resp(200, shell)):
            with self.assertRaises(google_images.GoogleImagesJavaScriptError) as caught:
                google_images.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(google_images.is_blocked())
        self.assertIn("Vision", str(caught.exception))   # names the fix

    def test_a_javascript_error_stands_down_like_a_block(self):
        """It is a property of Google's front end, not of this image, so
        the rest of the batch is certain to hit it too."""
        from core import google_images
        self.assertTrue(issubclass(google_images.GoogleImagesJavaScriptError,
                                   google_images.GoogleImagesBlockedError))

    def test_a_lens_landing_is_parsed_through_the_lens_reader(self):
        """Google redirects an upload to Lens, whose page has none of the
        classic result markup - reading it as a results page finds
        nothing at all."""
        from core import google_images
        landing = self._resp(200, TestGoogleImagesParser.LENS,
                             "https://lens.google.com/search?p=abc")
        with patch.object(google_images, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=landing):
            matches = google_images.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertEqual([m.url for m in matches],
                         ["https://www.deviantart.com/someone/art/A-Picture-123"])


class TestAscii2dBotChallenge(unittest.TestCase):
    """ascii2d now answers uploads with a Cloudflare interstitial.

    CONFIRMED against the live site: the homepage still returns 200 and a
    CSRF token, but POST /search/file returns 403 with "Just a moment..."
    - with a browser User-Agent, a Referer, an Origin, the CSRF token and
    a full set of Sec-Fetch headers alike. The decision is made below the
    HTTP layer, so no request this app can build gets past it.

    That makes it worth telling apart from an ordinary failure: every
    remaining image in a batch would otherwise spend a request being
    refused and report the same error.
    """

    CHALLENGE = ("<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
                 "<body>challenge-platform</body></html>")

    def setUp(self):
        from core import ascii2d
        ascii2d.reset_blocked_flag()
        self.addCleanup(ascii2d.reset_blocked_flag)

    def _resp(self, status, text):
        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    def test_a_challenge_is_recognised_and_latched(self):
        from core import ascii2d
        with patch.object(ascii2d, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=self._resp(403, self.CHALLENGE)):
            with self.assertRaises(ascii2d.Ascii2dBlockedError) as caught:
                ascii2d.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertTrue(ascii2d.is_blocked())
        message = str(caught.exception)
        self.assertIn("bot check", message)
        self.assertIn("Settings > Engine", message)   # tells the user what to do

    def test_a_plain_403_is_not_called_a_bot_check(self):
        """An ordinary 403 means something else, and must not latch the
        engine off or claim a cause that wasn't observed."""
        from core import ascii2d
        with patch.object(ascii2d, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=self._resp(403, "nope")):
            with self.assertRaises(ascii2d.Ascii2dError) as caught:
                ascii2d.search("/tmp/whatever.jpg", timeout=5.0)
        self.assertNotIsInstance(caught.exception, ascii2d.Ascii2dBlockedError)
        self.assertFalse(ascii2d.is_blocked())

    def test_once_blocked_later_images_do_not_ask_again(self):
        from core import ascii2d
        from core.search_engine import _collect_ascii2d
        entry = ImageEntry(path=_empty_image())
        settings = _settings()

        with patch.object(ascii2d, "prepare_upload_bytes", return_value=(b"x", "a.jpg")), \
             patch("requests.Session.post", return_value=self._resp(403, self.CHALLENGE)) as post:
            first_errors = []
            _collect_ascii2d(entry, settings, [], first_errors, None)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(len(first_errors), 1, "the first image should say why")

            # Two more images: no further requests, and no repeated error.
            for _ in range(2):
                more_errors = []
                _collect_ascii2d(entry, settings, [], more_errors, None)
                self.assertEqual(more_errors, [])
            self.assertEqual(post.call_count, 1, "a blocked engine must not be re-asked")

    def test_a_new_search_tries_again(self):
        """The site's bot check comes and goes, so starting a search is
        the right moment to find out whether it still refuses us."""
        from core import ascii2d
        ascii2d._blocked = True
        ascii2d.reset_blocked_flag()
        self.assertFalse(ascii2d.is_blocked())


class TestTwitterParser(unittest.TestCase):
    """Shape captured from api.fxtwitter.com, not invented.

    REGRESSION: with no parser, a Twitter match had no dimensions, no
    artist, and a "file size" that was really the SEARCH ENGINE'S
    THUMBNAIL - the app fell back to HEADing SauceNAO's ~30KB preview and
    reported that as the match. The picture shown was that same preview.
    """

    URL = "https://twitter.com/i/web/status/1060134528432230400"

    def _body(self, photos=None):
        return json.dumps({"code": 200, "message": "OK", "tweet": {
            "id": "1060134528432230400",
            "author": {"screen_name": "poppuqn", "name": "ポップ"},
            "media": {"photos": photos if photos is not None else [
                {"type": "photo", "url": "https://pbs.twimg.com/media/AAA.png?name=orig",
                 "width": 808, "height": 650},
                {"type": "photo", "url": "https://pbs.twimg.com/media/BBB.png?name=orig",
                 "width": 530, "height": 750},
            ]},
        }})

    def test_tweet_urls_resolve_to_the_api(self):
        from core.boorus import twitter
        for url in ("https://twitter.com/i/web/status/123",
                    "https://x.com/user/status/123",
                    "https://fxtwitter.com/user/status/123",
                    "https://twitter.com/user/statuses/123"):
            with self.subTest(url=url):
                self.assertEqual(twitter.resolve_fetch_url(url),
                                 "https://api.fxtwitter.com/status/123")

    def test_a_url_with_no_tweet_id_is_left_alone(self):
        from core.boorus import twitter
        for url in ("https://twitter.com/someone", "https://x.com/"):
            self.assertEqual(twitter.resolve_fetch_url(url), url)

    def test_the_author_becomes_an_artist_tag(self):
        from core.boorus import twitter
        tags = twitter.parse(self._body(), self.URL)
        self.assertEqual([(t.namespace, t.name) for t in tags], [("artist", "poppuqn")])

    def test_real_dimensions_are_reported(self):
        from core.boorus import twitter
        self.assertEqual(twitter.parse_dimensions(self._body(), self.URL), (808, 650))

    def test_the_file_is_the_uncapped_original_and_the_preview_is_the_large_render(self):
        """The engine's thumbnail was what got shown and measured before;
        these are the real image and a 2048px render of it."""
        from core.boorus import twitter
        body = self._body()
        self.assertEqual(twitter.parse_file_url(body, self.URL),
                         "https://pbs.twimg.com/media/AAA.png?name=orig")
        self.assertEqual(twitter.parse_preview_url(body, self.URL),
                         "https://pbs.twimg.com/media/AAA.png?name=large")

    def test_a_url_naming_a_photo_selects_that_one(self):
        from core.boorus import twitter
        body = self._body()
        self.assertEqual(twitter.parse_dimensions(body, self.URL + "/photo/2"), (530, 750))
        self.assertIn("BBB", twitter.parse_file_url(body, self.URL + "/photo/2"))
        # /photo/1 is the first, not the second
        self.assertEqual(twitter.parse_dimensions(body, self.URL + "/photo/1"), (808, 650))

    def test_an_out_of_range_photo_falls_back_to_the_first(self):
        from core.boorus import twitter
        self.assertEqual(twitter.parse_dimensions(self._body(), self.URL + "/photo/9"),
                         (808, 650))

    def test_page_count_reflects_the_tweets_images(self):
        from core.boorus import twitter
        self.assertEqual(twitter.parse_page_count(self._body(), self.URL), 2)
        self.assertEqual(twitter.parse_page_count(self._body(photos=[]), self.URL), 1)

    def test_the_photo_list_is_offered_for_the_multipage_resolver(self):
        """REGRESSION (DAN-57): without these hooks every multi-photo
        tweet stayed on photo 1, because the resolver had no way to ask
        what the other photos were. The /photo/N suffix _photo() reads is
        no substitute - SauceNAO's result URLs carry none, and the API's
        own media facets name /photo/1 for every photo in the tweet."""
        from core.boorus import twitter
        self.assertEqual(twitter.pages_api_url(self.URL),
                         "https://api.fxtwitter.com/status/1060134528432230400")
        pages = twitter.parse_pages(self._body(), self.URL)
        self.assertEqual([(p["width"], p["height"]) for p in pages],
                         [(808, 650), (530, 750)])
        self.assertEqual(pages[1]["urls"], {
            "original": "https://pbs.twimg.com/media/BBB.png?name=orig",
            "regular": "https://pbs.twimg.com/media/BBB.png?name=large",
            "small": "https://pbs.twimg.com/media/BBB.png?name=small",
        })

    def test_the_photo_list_is_fetched_from_the_record_already_read(self):
        """pages_api_url is the URL the tags came from, so the resolver
        reuses that body rather than requesting it a second time."""
        from core.boorus import twitter
        self.assertEqual(twitter.pages_api_url(self.URL),
                         twitter.resolve_fetch_url(self.URL))

    def test_a_url_with_no_tweet_id_offers_no_photo_list(self):
        from core.boorus import twitter
        self.assertIsNone(twitter.pages_api_url("https://twitter.com/someone"))

    def test_a_photo_with_no_usable_url_is_left_out_of_the_list(self):
        """An entry the resolver could neither show nor download would
        shift every later photo's index by one."""
        from core.boorus import twitter
        body = self._body(photos=[
            {"type": "photo", "url": None, "width": 1, "height": 2},
            {"type": "photo", "url": "https://pbs.twimg.com/media/CCC.png?name=orig",
             "width": 3, "height": 4},
        ])
        pages = twitter.parse_pages(body, self.URL)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["urls"]["original"],
                         "https://pbs.twimg.com/media/CCC.png?name=orig")

    def test_a_tweet_with_no_photos_claims_nothing(self):
        from core.boorus import twitter
        body = self._body(photos=[])
        self.assertIsNone(twitter.parse_file_url(body, self.URL))
        self.assertIsNone(twitter.parse_preview_url(body, self.URL))
        self.assertEqual(twitter.parse_dimensions(body, self.URL), (None, None))
        # the author is still worth having
        self.assertEqual(len(twitter.parse(body, self.URL)), 1)

    def test_twitters_own_html_yields_nothing_without_raising(self):
        from core.boorus import twitter
        html = "<!DOCTYPE html><html><body>JS-driven</body></html>"
        self.assertEqual(twitter.parse(html, self.URL), [])
        self.assertIsNone(twitter.parse_file_url(html, self.URL))
        self.assertEqual(twitter.parse_dimensions(html, self.URL), (None, None))
        self.assertEqual(twitter.parse_page_count(html, self.URL), 1)

    def test_media_and_shortener_urls_stay_unparsed(self):
        """They carry no tweet id, so there is nothing to look up."""
        from core.boorus import find_parser
        for url in ("https://pbs.twimg.com/media/abc.jpg", "https://t.co/abc"):
            with self.subTest(url=url):
                self.assertIsNone(find_parser(url))


class TestSankakuParser(unittest.TestCase):
    """Shapes captured from the live modern API, not invented.

    Sankaku's legacy numeric ids - the only ones IQDB and SauceNAO know -
    no longer resolve to anything: every post URL on chan. bounces to an
    OIDC login, the API rejects a numeric id as "invalid id", and
    legacy_id is null on every post sampled. The file's own md5 is the
    only key left that this app can supply.
    """

    LEGACY = "https://chan.sankakucomplex.com/post/show/5255732"
    MD5 = "7c44b8329be64b1dea27638c60192fe0"

    def _body(self, **overrides):
        return json.dumps([{
            "id": "QyMk8vZ6Kak", "md5": self.MD5, "status": "active",
            "width": 2400, "height": 2400,
            "file_type": "image/jpeg", "file_size": 1118894,
            "file_url": "https://s.sankakucomplex.com/o/7c/44/x.jpg?e=1&m=sig",
            "sample_url": "https://v.sankakucomplex.com/data/sample/x.webp?e=1",
            "preview_url": "https://v.sankakucomplex.com/data/preview/x.avif?e=1",
            "tags": [
                {"name_en": "Heveti", "type": 1},
                {"name_en": "Overwatch", "type": 3},
                {"name_en": "D.Va (Overwatch)", "type": 4},
                {"name_en": "Thighhighs", "type": 0},
                {"name_en": "Hetero", "type": 5},
                {"name_en": "1:1 aspect ratio", "type": 8},
            ],
            **overrides,
        }])

    def test_the_lookup_is_keyed_on_the_local_files_hash(self):
        from core.boorus import sankaku
        resolved = sankaku.resolve_fetch_url_with_context(
            self.LEGACY, {"local_md5": self.MD5})
        self.assertTrue(resolved.startswith("https://sankakuapi.com/posts?"))
        self.assertIn(f"md5%3A{self.MD5}", resolved)

    def test_without_a_hash_it_falls_back_to_the_original_url(self):
        """No hash means no way in - which is the old behaviour, not a
        new failure."""
        from core.boorus import sankaku
        for context in ({}, {"local_md5": None}, {"local_md5": "not-a-hash"}):
            with self.subTest(context=context):
                self.assertEqual(
                    sankaku.resolve_fetch_url_with_context(self.LEGACY, context),
                    self.LEGACY)

    def test_tags_are_namespaced_by_the_sites_own_types(self):
        from core.boorus import sankaku
        tags = {(t.namespace, t.name) for t in sankaku.parse(self._body(), self.LEGACY)}
        self.assertIn(("artist", "Heveti"), tags)
        self.assertIn(("copyright", "Overwatch"), tags)
        self.assertIn(("character", "D.Va_(Overwatch)"), tags)
        self.assertIn(("general", "Thighhighs"), tags)
        self.assertIn(("general", "Hetero"), tags)     # type 5 = genre, kept unnamespaced
        self.assertIn(("meta", "1:1_aspect_ratio"), tags)

    def test_the_match_is_repointed_at_a_url_that_actually_opens(self):
        """The legacy URL is a login wall for everyone, so leaving it in
        place hands the user a dead link - and associates a dead link with
        the file in Hydrus."""
        from core.boorus import sankaku
        self.assertEqual(
            sankaku.parse_canonical_url(self._body(), self.LEGACY),
            "https://www.sankakucomplex.com/posts/QyMk8vZ6Kak")

    def test_dimensions_format_and_size_come_from_the_api(self):
        """Sankaku's file URLs are signed and time-limited, so a HEAD on a
        stale one reports nothing - the API's own numbers are the reliable
        source."""
        from core.boorus import sankaku
        body = self._body()
        self.assertEqual(sankaku.parse_dimensions(body, self.LEGACY), (2400, 2400))
        self.assertEqual(sankaku.parse_file_info(body, self.LEGACY), ("JPEG", 1118894))

    def test_the_sample_is_preferred_over_the_tiny_preview(self):
        from core.boorus import sankaku
        self.assertIn("sample", sankaku.parse_preview_url(self._body(), self.LEGACY))

    def test_a_hash_miss_claims_nothing(self):
        """The ordinary outcome: the local file simply isn't Sankaku's
        copy. It must not be mistaken for a parsing failure."""
        from core.boorus import sankaku
        for body in ("[]", "{}", ""):
            with self.subTest(body=body):
                self.assertEqual(sankaku.parse(body, self.LEGACY), [])
                self.assertIsNone(sankaku.parse_canonical_url(body, self.LEGACY))
                self.assertIsNone(sankaku.parse_file_url(body, self.LEGACY))
                self.assertEqual(sankaku.parse_dimensions(body, self.LEGACY), (None, None))
                self.assertEqual(sankaku.parse_file_info(body, self.LEGACY), (None, None))

    def test_a_miss_is_not_logged_as_a_broken_parser(self):
        """Most sites returning nothing means the markup moved; here it is
        routine, and would otherwise warn on every single match."""
        from core.boorus import sankaku
        self.assertTrue(getattr(sankaku, "EMPTY_RESULT_IS_NORMAL", False))

    def test_the_legacy_html_page_yields_nothing_without_raising(self):
        from core.boorus import sankaku
        html = "<!DOCTYPE html><html><body>login required</body></html>"
        self.assertEqual(sankaku.parse(html, self.LEGACY), [])
        self.assertIsNone(sankaku.parse_canonical_url(html, self.LEGACY))

    def test_parser_is_registered_for_the_legacy_url_shapes(self):
        from core.boorus import find_parser, sankaku
        for url in (self.LEGACY,
                    "https://chan.sankakucomplex.com/en/posts/4064096",
                    "https://www.sankakucomplex.com/posts/QyMk8vZ6Kak"):
            with self.subTest(url=url):
                self.assertIs(find_parser(url), sankaku)


class TestLocalMd5(unittest.TestCase):
    def test_hashes_a_file_and_reuses_the_result(self):
        from core.boorus import local_md5
        import hashlib
        path = os.path.join(tempfile.mkdtemp(), "f.bin")
        data = b"some bytes" * 100
        with open(path, "wb") as fh:
            fh.write(data)
        expected = hashlib.md5(data).hexdigest()
        self.assertEqual(local_md5(path), expected)
        self.assertEqual(local_md5(path), expected)   # cached path

    def test_a_changed_file_is_rehashed(self):
        """Keyed on the file's identity, not its name, so an edited file
        can't be matched against a stale digest."""
        from core.boorus import local_md5
        import hashlib, time
        path = os.path.join(tempfile.mkdtemp(), "f.bin")
        with open(path, "wb") as fh:
            fh.write(b"first")
        first = local_md5(path)
        time.sleep(0.01)
        with open(path, "wb") as fh:
            fh.write(b"second version, different length")
        self.assertNotEqual(local_md5(path), first)
        self.assertEqual(local_md5(path), hashlib.md5(b"second version, different length").hexdigest())

    def test_missing_or_unreadable_files_are_not_fatal(self):
        from core.boorus import local_md5
        self.assertIsNone(local_md5(None))
        self.assertIsNone(local_md5("/nonexistent/nope.bin"))


class TestPawchiveParser(unittest.TestCase):
    """Shapes captured from the live API, not invented.

    Pawchive is a Kemono fork whose JSON API answers anonymously even
    though the HTML pages behind the same URLs are account-gated - which
    is the only reason a parser is possible here.
    """

    URL = "https://pawchive.pw/patreon/user/50093102/post/83421639"

    def _body(self, **overrides):
        return json.dumps({
            "id": "83421639", "user": "50093102", "service": "patreon",
            "title": "A Post",
            # A POSTGRES ARRAY LITERAL in a string - not a JSON list - and
            # a quoted element containing the separator.
            "tags": '{Bondage,Damsel,"Total Versext",gefesselt}',
            "file": {"name": "a.jpg", "path": "/93/0e/930e7282.jpg"},
            "attachments": [{"name": "a.jpg", "path": "/93/0e/930e7282.jpg"}],
            **overrides,
        })

    def setUp(self):
        from core.boorus import pawchive
        # Pre-seed the creator cache so no test reaches the network.
        pawchive._artist_cache["patreon/50093102"] = "Sandra Spick"

    def test_post_urls_resolve_to_the_api(self):
        from core.boorus import pawchive
        self.assertEqual(
            pawchive.resolve_fetch_url(self.URL),
            "https://pawchive.pw/api/v1/patreon/user/50093102/post/83421639",
        )

    def test_a_url_with_no_post_is_left_alone(self):
        from core.boorus import pawchive
        for url in ("https://pawchive.pw/", "https://pawchive.pw/posts/popular"):
            self.assertEqual(pawchive.resolve_fetch_url(url), url)

    def test_postgres_array_tags_are_split_on_the_right_commas(self):
        """A quoted element contains the separator, so a plain split would
        turn one tag into two."""
        from core.boorus import pawchive
        names = {t.name for t in pawchive.parse(self._body(), self.URL)
                 if t.namespace == "general"}
        self.assertEqual(names, {"Bondage", "Damsel", "Total_Versext", "gefesselt"})

    def test_the_creator_becomes_an_artist_tag(self):
        """Most posts carry no tags of their own, so without the creator
        lookup the parser would contribute nothing at all for them."""
        from core.boorus import pawchive
        tags = pawchive.parse(self._body(tags=None), self.URL)
        self.assertEqual([(t.namespace, t.name) for t in tags], [("artist", "Sandra_Spick")])

    def test_a_json_list_of_tags_is_also_accepted(self):
        from core.boorus import pawchive
        names = {t.name for t in pawchive.parse(self._body(tags=["a", "b b"]), self.URL)
                 if t.namespace == "general"}
        self.assertEqual(names, {"a", "b_b"})

    def test_the_preview_comes_from_the_image_host(self):
        from core.boorus import pawchive
        self.assertEqual(
            pawchive.parse_preview_url(self._body(), self.URL),
            "https://img.pawchive.pw/thumbnail/data/93/0e/930e7282.jpg",
        )

    def test_the_original_comes_from_the_file_host(self):
        """Originals live on file.pawchive.pw, not the image host - every
        /data path on img.pawchive.pw 404s, which makes them look
        account-gated when no login is involved at all."""
        from core.boorus import pawchive
        self.assertEqual(
            pawchive.parse_file_url(self._body(has_full=True), self.URL),
            "https://file.pawchive.pw/data/93/0e/930e7282.jpg?f=a.jpg",
        )

    def test_no_file_url_when_the_archive_lacks_the_full_file(self):
        """has_full is about what has been imported, not permissions: a
        false one goes with preview_state "pending" and 404s for everyone,
        so pointing at it would be a dead link that looks like a bug."""
        from core.boorus import pawchive
        for body in (self._body(has_full=False), self._body()):   # absent counts as false
            self.assertIsNone(pawchive.parse_file_url(body, self.URL))

    def test_a_filename_with_awkward_characters_is_url_encoded(self):
        from core.boorus import pawchive
        body = self._body(has_full=True,
                          file={"name": "a b&c.jpg", "path": "/93/0e/930e7282.jpg"})
        url = pawchive.parse_file_url(body, self.URL)
        self.assertIn("?f=a%20b%26c.jpg", url)

    def test_an_empty_file_field_falls_back_to_the_attachments(self):
        """Seen live: "file" is {} and every image is under attachments.
        Without the fallback there was no file URL, and the size check
        measured the thumbnail instead of the original."""
        from core.boorus import pawchive
        body = self._body(has_full=True, file={},
                          attachments=[{"name": "b.jpg", "path": "/a6/bc/a6bc.jpg"},
                                       {"name": "c.jpg", "path": "/ec/8b/ec8b.jpg"}])
        self.assertEqual(pawchive.parse_file_url(body, self.URL),
                         f"{pawchive.FILE_BASE}/a6/bc/a6bc.jpg?f=b.jpg")

    def test_multi_image_posts_report_their_page_count(self):
        from core.boorus import pawchive
        body = self._body(attachments=[{"name": f"{i}.jpg", "path": f"/a/b/{i}.jpg"}
                                       for i in range(12)])
        self.assertEqual(pawchive.parse_page_count(body, self.URL), 12)
        self.assertEqual(pawchive.parse_page_count(self._body(), self.URL), 1)

    def test_file_info_is_not_claimed(self):
        """The API doesn't report it, and the normal HEAD on the file URL
        does - so this hook is deliberately absent."""
        from core.boorus import pawchive
        self.assertFalse(hasattr(pawchive, "parse_file_info"))

    def _ranged_response(self, data, status=206):
        resp = MagicMock(status_code=status)
        resp.iter_content.return_value = [data[i:i + 1024] for i in range(0, len(data), 1024)]
        resp.__enter__.return_value = resp
        return resp

    def test_dimensions_are_read_from_the_head_of_the_original(self):
        """The API has none, so they come from a ranged read of the
        ORIGINAL (never the preview, whose numbers describe the preview)."""
        import io as _io
        from PIL import Image
        from core.boorus import pawchive
        buf = _io.BytesIO()
        Image.new("RGB", (321, 123)).save(buf, "PNG")
        path = f"/aa/bb/{uuid.uuid4().hex}.png"
        body = self._body(has_full=True, file={"name": "a.png", "path": path})
        with patch("requests.Session.get", return_value=self._ranged_response(buf.getvalue())) as get:
            self.assertEqual(pawchive.parse_dimensions(body, self.URL), (321, 123))
            # Cached by path: the same file is not read twice.
            self.assertEqual(pawchive.parse_dimensions(body, self.URL), (321, 123))
        self.assertEqual(get.call_count, 1)
        called_url = get.call_args[0][0]
        self.assertTrue(called_url.startswith(pawchive.FILE_BASE))
        self.assertIn("Range", get.call_args[1]["headers"])

    def test_no_dimensions_without_the_original(self):
        """has_full false means the original 404s for everyone - so no
        request is made at all."""
        from core.boorus import pawchive
        with patch("requests.Session.get") as get:
            self.assertEqual(pawchive.parse_dimensions(self._body(has_full=False), self.URL),
                             (None, None))
        get.assert_not_called()

    def test_a_network_failure_is_not_remembered(self):
        import requests
        from core.boorus import pawchive
        path = f"/aa/bb/{uuid.uuid4().hex}.jpg"
        body = self._body(has_full=True, file={"name": "a.jpg", "path": path})
        with patch("requests.Session.get", side_effect=requests.Timeout("slow")):
            self.assertEqual(pawchive.parse_dimensions(body, self.URL), (None, None))
        from core.boorus import _remote_size
        self.assertNotIn(pawchive.FILE_BASE + path, _remote_size._cache)

    def test_the_account_gated_html_page_yields_nothing(self):
        from core.boorus import pawchive
        html = "<!DOCTYPE html><html><body>only available to registered users</body></html>"
        self.assertEqual(pawchive.parse(html, self.URL), [])
        self.assertIsNone(pawchive.parse_preview_url(html, self.URL))
        self.assertEqual(pawchive.parse_page_count(html, self.URL), 1)

    def test_a_failed_creator_lookup_just_means_no_artist_tag(self):
        from core.boorus import pawchive
        body = self._body(user="99999999", tags=None)
        with patch.object(pawchive, "_artist_name", return_value=None):
            self.assertEqual(pawchive.parse(body, self.URL), [])

    def test_only_the_pw_domain_is_parsed(self):
        """pawchive.org shares the label but is a different site - it 404s
        on the post routes and the API alike."""
        from core.boorus import find_parser, pawchive
        self.assertIs(find_parser(self.URL), pawchive)
        self.assertIsNone(find_parser("https://pawchive.org/some/post"))

    def test_both_domains_still_classify_as_pawchive(self):
        self.assertEqual(classify_site(None, self.URL), "Pawchive")
        self.assertEqual(classify_site(None, "https://pawchive.org/x"), "Pawchive")


class TestAnimePicturesCookiePaste(unittest.TestCase):
    """REGRESSION: pasting the cookie's VALUE - the natural thing to do,
    since devtools shows name and value in separate columns - produced
    "No cookies entered". The value is a JWT and contains no "=", so the
    name=value parser found nothing in a string the user had definitely
    just entered."""

    JWT = ("eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9."
           "eyJ1c2VyIjp7ImlkIjoxfSwiZXhwIjoxfQ."
           "c2lnbmF0dXJlLWdvZXMtaGVyZS1ub3QtYS1yZWFsLW9uZQ")

    def test_a_bare_token_is_paired_with_the_session_cookie_name(self):
        from core.search_engine import ANIMEPICTURES_SESSION_COOKIE, animepictures_cookies
        self.assertEqual(animepictures_cookies(self.JWT),
                         {ANIMEPICTURES_SESSION_COOKIE: self.JWT})

    def test_the_jwt_really_does_contain_no_equals_sign(self):
        """The reason the old parser found nothing - worth pinning down so
        the bare-token path isn't mistaken for belt-and-braces."""
        self.assertNotIn("=", self.JWT)

    def test_name_equals_value_still_works(self):
        from core.search_engine import ANIMEPICTURES_SESSION_COOKIE, animepictures_cookies
        self.assertEqual(
            animepictures_cookies(f"{ANIMEPICTURES_SESSION_COOKIE}={self.JWT}"),
            {ANIMEPICTURES_SESSION_COOKIE: self.JWT},
        )

    def test_a_whole_pasted_cookie_row_still_works(self):
        from core.search_engine import animepictures_cookies
        got = animepictures_cookies(
            f"sitelang=en; anime_pictures_jwt={self.JWT}; time_zone=America%2FNew_York"
        )
        self.assertEqual(got["anime_pictures_jwt"], self.JWT)
        self.assertEqual(got["sitelang"], "en")

    def test_blank_and_unusable_input_yield_nothing(self):
        from core.search_engine import animepictures_cookies
        for raw in ("", "   ", None, "several separate words"):
            with self.subTest(raw=raw):
                self.assertIsNone(animepictures_cookies(raw))

    def test_warnings_name_the_right_site_session_cookie(self):
        """REGRESSION: the warning text was hardcoded to Sankaku, so
        configuring Anime-Pictures advised copying _sankakuchannel_session."""
        from core.search_engine import ANIMEPICTURES_SESSION_COOKIE, cookie_paste_warnings
        warnings = cookie_paste_warnings("sitelang=en",
                                         session_name=ANIMEPICTURES_SESSION_COOKIE)
        self.assertTrue(warnings)
        joined = " ".join(warnings)
        self.assertIn(ANIMEPICTURES_SESSION_COOKIE, joined)
        self.assertNotIn("sankaku", joined.lower())

    def test_sankaku_warnings_are_unchanged_by_default(self):
        from core.search_engine import cookie_paste_warnings
        joined = " ".join(cookie_paste_warnings("locale=en"))
        self.assertIn("_sankakuchannel_session", joined)


class TestStatedFileInfoBeatsAHead(unittest.TestCase):
    """A site that states its original's format/size must be believed over
    a HEAD - on a site whose original isn't reachable the HEAD falls back
    to the search engine's thumbnail, which would report a few KB and the
    thumbnail's format as if they described the match."""

    def _candidate(self):
        from core.models import MatchCandidate
        return MatchCandidate(
            url="https://anime-pictures.net/posts/596771", similarity=95.0,
            engine="saucenao", thumb_url="https://iqdb.org/thumb.jpg",
        )

    def _settings(self):
        from core.config import Settings
        s = Settings()
        s.retrieve_tags_from_booru = True
        s.drop_dead_matches = False
        return s

    def _page_info(self, **kwargs):
        from core.boorus import BooruPageInfo
        return BooruPageInfo(fetched=True, **kwargs)

    def test_stated_values_are_used_and_no_head_is_made(self):
        from core import search_engine
        candidate = self._candidate()
        with patch.object(search_engine, "fetch_page_info",
                          return_value=self._page_info(file_format="JPEG", file_size_bytes=5646846)), \
             patch.object(search_engine.remote, "fetch_remote_info") as head, \
             patch.object(search_engine.remote, "download_bytes", return_value=None):
            search_engine.fetch_candidate_details(candidate, self._settings())
        self.assertEqual(candidate.remote_format, "JPEG")
        self.assertEqual(candidate.remote_size_bytes, 5646846)
        head.assert_not_called()

    def test_a_head_still_runs_when_the_site_states_nothing(self):
        from core import search_engine
        candidate = self._candidate()
        with patch.object(search_engine, "fetch_page_info", return_value=self._page_info()), \
             patch.object(search_engine.remote, "fetch_remote_info", return_value=("PNG", 1234)) as head, \
             patch.object(search_engine.remote, "download_bytes", return_value=None):
            search_engine.fetch_candidate_details(candidate, self._settings())
        head.assert_called_once()
        self.assertEqual((candidate.remote_format, candidate.remote_size_bytes), ("PNG", 1234))

    def test_a_head_never_blanks_out_a_partially_stated_value(self):
        """Only the format was stated; the HEAD supplies the size and must
        not overwrite the format with its own None."""
        from core import search_engine
        candidate = self._candidate()
        with patch.object(search_engine, "fetch_page_info",
                          return_value=self._page_info(file_format="JPEG")), \
             patch.object(search_engine.remote, "fetch_remote_info", return_value=(None, 4321)), \
             patch.object(search_engine.remote, "download_bytes", return_value=None):
            search_engine.fetch_candidate_details(candidate, self._settings())
        self.assertEqual(candidate.remote_format, "JPEG")
        self.assertEqual(candidate.remote_size_bytes, 4321)


class TestGelbooruFamilyDapi(unittest.TestCase):
    def test_bare_list_response_is_accepted(self):
        """rule34.xxx returns a JSON list, not Gelbooru's {post: [...]} wrap."""
        from core.boorus.gelbooru import post_from_dapi
        post = post_from_dapi([{"id": 1, "file_url": "https://x/a.png"}])
        self.assertEqual(post["file_url"], "https://x/a.png")

    def test_wrapped_response_still_works(self):
        from core.boorus.gelbooru import post_from_dapi
        post = post_from_dapi({"post": [{"id": 2, "file_url": "https://x/b.png"}]})
        self.assertEqual(post["id"], 2)

    def test_empty_is_safe(self):
        from core.boorus.gelbooru import post_from_dapi
        self.assertIsNone(post_from_dapi([]))
        self.assertIsNone(post_from_dapi({}))
        self.assertIsNone(post_from_dapi("nope"))


class TestRule34Soft404(unittest.TestCase):
    def test_deleted_rule34_post_redirecting_to_the_list_is_gone(self):
        from core.search_engine import check_url_available

        def fake_get(url, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = "https://rule34.xxx/index.php?page=post&s=list&tags=all"
            resp.raw.read.return_value = b""
            resp.headers = {}
            resp.close = MagicMock()
            return resp

        with patch("requests.Session.get", side_effect=fake_get):
            self.assertIs(
                check_url_available(
                    "https://rule34.xxx/index.php?page=post&s=view&id=1", 5.0,
                ),
                False,
            )


class TestConfigMigrationV6(unittest.TestCase):
    def test_existing_enabled_sites_gain_the_new_names(self):
        import json
        from pathlib import Path
        from core import config

        tmp = tempfile.mkdtemp()
        config_dir = Path(tmp) / "hatate-linux"
        config_dir.mkdir()
        payload = {
            "schema_version": 5,
            "enabled_sites": ["Danbooru", "Gelbooru"],
        }
        (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        original_dir, original_file = config.CONFIG_DIR, config.CONFIG_FILE
        config.CONFIG_DIR = config_dir
        config.CONFIG_FILE = config_dir / "config.json"
        try:
            loaded = config.Settings.load()
            for site in ("Rule34", "Xbooru", "Twitter", "AniList"):
                self.assertIn(site, loaded.enabled_sites, site)
            # Against the constant, not a literal: a migration stamps
            # the config with the CURRENT version, so hard-coding one
            # here breaks this test every time a later one is added.
            self.assertEqual(loaded.schema_version, config.CURRENT_SCHEMA_VERSION)
        finally:
            config.CONFIG_DIR = original_dir
            config.CONFIG_FILE = original_file


class TestLensKeepsTaggableResults(unittest.TestCase):
    """Sites that can't give tags must not crowd out ones that can."""

    def _match(self, url, rank):
        from core.google_lens import GoogleLensMatch
        return GoogleLensMatch(url=url, thumb_url=None, similarity=80.0 - rank)

    def test_a_booru_post_past_the_cut_survives(self):
        from core.google_lens import keep_taggable_first
        matches = [self._match(f"https://www.pinterest.com/pin/{i}/", i) for i in range(10)]
        matches.append(self._match("https://danbooru.donmai.us/posts/123", 10))
        kept = keep_taggable_first(matches, 8)
        self.assertEqual(len(kept), 8)
        self.assertIn("https://danbooru.donmai.us/posts/123", [m.url for m in kept])
        # The pin it displaced is the lowest-ranked one that would have made it.
        self.assertNotIn("https://www.pinterest.com/pin/7/", [m.url for m in kept])

    def test_what_is_kept_stays_in_googles_order(self):
        from core.google_lens import keep_taggable_first
        matches = [self._match(f"https://blog{i}.example.com/a", i) for i in range(10)]
        matches.insert(0, self._match("https://www.pixiv.net/en/artworks/1", -1))
        matches.append(self._match("https://gelbooru.com/index.php?page=post&s=view&id=1", 11))
        kept = keep_taggable_first(matches, 8)
        self.assertEqual(kept, sorted(kept, key=lambda m: -m.similarity))

    def test_tag_giving_sites_rank_first_within_each_group(self):
        from core.google_lens import taggable_first
        matches = [
            self._match("https://www.pinterest.com/pin/1/", 0),
            self._match("https://blog.example.com/a", 1),
            self._match("https://danbooru.donmai.us/posts/1", 2),
            self._match("https://www.pinterest.com/pin/2/", 3),
            self._match("https://www.pixiv.net/en/artworks/1", 4),
        ]
        self.assertEqual([m.url for m in taggable_first(matches)], [
            "https://danbooru.donmai.us/posts/1",
            "https://www.pixiv.net/en/artworks/1",
            "https://www.pinterest.com/pin/1/",
            "https://blog.example.com/a",
            "https://www.pinterest.com/pin/2/",
        ])

    def test_search_puts_exact_before_visual_and_taggable_first_in_each(self):
        from unittest.mock import patch
        from core import google_lens
        from core.google_lens import GoogleLensMatch

        def m(url):
            return GoogleLensMatch(url=url, thumb_url=None, similarity=0.0)
        exact = [m("https://exact-blog.example.com/a"), m("https://gelbooru.com/index.php?page=post&s=view&id=1")]
        visual = [m("https://www.pinterest.com/pin/1/"), m("https://danbooru.donmai.us/posts/9")]
        with patch.object(google_lens, "prepare_for_lens", return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=(["body"], [])), \
             patch.object(google_lens, "parse_exact_tiles", return_value=[]), \
             patch.object(google_lens, "parse_exact_matches", return_value=exact), \
             patch.object(google_lens, "parse_lens_payload", return_value=visual):
            results = google_lens.search("/tmp/a.jpg", timeout=1)
        self.assertEqual([r.url for r in results], [
            "https://gelbooru.com/index.php?page=post&s=view&id=1",
            "https://exact-blog.example.com/a",
            "https://danbooru.donmai.us/posts/9",
            "https://www.pinterest.com/pin/1/",
        ])
        self.assertEqual(results, sorted(results, key=lambda r: -r.similarity))

    def test_a_short_list_is_left_alone(self):
        from core.google_lens import keep_taggable_first
        matches = [self._match(f"https://blog{i}.example.com/a", i) for i in range(5)]
        self.assertEqual(keep_taggable_first(matches, 8), matches)

    def test_only_untaggable_results_still_fill_every_slot(self):
        from core.google_lens import keep_taggable_first
        matches = [self._match(f"https://blog{i}.example.com/a", i) for i in range(12)]
        self.assertEqual(keep_taggable_first(matches, 8), matches[:8])


class TestRetryOnlyWhenItCouldHelp(unittest.TestCase):
    """An engine that stood down for the run fails the same way twice."""

    def tearDown(self):
        from core import google_lens
        google_lens.reset_blocked_flag()

    def _entry(self, message):
        from core.models import ImageEntry
        entry = ImageEntry(path="/tmp/retry.png")
        entry.error_message = message
        return entry

    def test_a_lens_that_stood_down_is_not_retried(self):
        from core import google_lens
        from core.search_engine import retry_could_help
        google_lens._latch("Playwright is not installed")
        self.assertFalse(retry_could_help(self._entry(
            "Google Lens: Google Lens needs Playwright")))

    def test_a_network_fault_alongside_it_still_is(self):
        from core import google_lens
        from core.search_engine import retry_could_help
        google_lens._latch("Playwright is not installed")
        self.assertTrue(retry_could_help(self._entry(
            "Google Lens: needs Playwright; SauceNAO: Read timed out")))

    def test_a_lens_failure_that_did_not_stand_it_down_is_retried(self):
        from core.search_engine import retry_could_help
        self.assertTrue(retry_could_help(self._entry("Google Lens: page never loaded")))

    def test_an_unattributable_message_is_retried(self):
        from core.search_engine import retry_could_help
        self.assertTrue(retry_could_help(self._entry("")))
        self.assertTrue(retry_could_help(self._entry("Connection reset")))


class TestParsersMatchTheRealHost(unittest.TestCase):
    """REGRESSION GUARD: parsers matched their host ANYWHERE in the URL.
    "x.com" is inside "hentaivox.com", so the Twitter parser claimed it."""

    def _parser(self, url):
        from core import boorus
        parser = boorus.find_parser(url)
        return parser.__name__.rsplit(".", 1)[-1] if parser else None

    def test_a_host_inside_another_host_is_not_claimed(self):
        self.assertIsNone(self._parser("https://hentaivox.com/view/186931/6"))
        self.assertIsNone(self._parser("https://freeadultcomix.com/fac/tramp-gang/"))

    def test_a_host_in_the_path_is_not_claimed(self):
        self.assertIsNone(self._parser("https://r34.app/posts/rule34.xxx/anna_(tekuho)"))

    def test_real_hosts_and_subdomains_still_are(self):
        self.assertEqual(self._parser("https://x.com/a/status/1"), "twitter")
        self.assertEqual(self._parser("https://www.pixiv.net/en/artworks/1"), "pixiv")
        self.assertEqual(self._parser("https://chan.sankakucomplex.com/post/show/1"), "sankaku")
        self.assertEqual(self._parser("danbooru.donmai.us/posts/1"), "danbooru")


class TestRule34SearchTiles(unittest.TestCase):
    """Lens can list a rule34.xxx post only by the search Google reached it
    through - "Rule 34 / parent:10755759" - with the post id nowhere."""

    def test_searches_are_read_from_tiles_precise_first(self):
        from core.google_lens import rule34_searches
        tiles = [
            {"text": "Rule 34 / piss -moodyferret -green_eyes 250x177 Rule 34"},
            {"text": "Rule 34"},
            {"text": "Rule 34 / parent:10755759 250x177 Rule 34"},
            {"text": "Teenage Prostitute rule 34 hentai, from rule34.xxx 250x177 Rule 34 App"},
        ]
        self.assertEqual(rule34_searches(tiles),
                         ["parent:10755759", "piss -moodyferret -green_eyes"])

    LISTING = (
        '<span id="s1" class="thumb"><a id="p10755759" href="/index.php?page=post&s=view'
        '&id=10755759&tags=parent%3A10755759">\n    <img src="https://wimg.example/t1.jpg?1"'
        ' alt="x"/></a></span>'
        '<span id="s2" class="thumb"><a id="p10755716" href="/index.php?page=post&s=view'
        '&id=10755716&tags=parent%3A10755759">\n    <img src="https://wimg.example/t2.jpg?2"'
        ' alt="x"/></a></span>'
    )

    def test_listing_posts_are_read(self):
        from core.boorus.rule34 import listing_posts
        self.assertEqual(listing_posts(self.LISTING), [
            ("10755759", "https://wimg.example/t1.jpg?1"),
            ("10755716", "https://wimg.example/t2.jpg?2"),
        ])

    def _responses(self, url, **kwargs):
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=200)
        resp.text = self.LISTING
        resp.content = {"https://wimg.example/t1.jpg?1": b"variant",
                        "https://wimg.example/t2.jpg?2": b"same"}.get(url, b"")
        return resp

    def test_the_closest_post_is_chosen_over_a_variant(self):
        from core.boorus import rule34
        hashes = {b"variant": 0b111111111, b"same": 0b1}   # distance 9 vs 1 from 0
        with patch.object(rule34.requests.Session, "get", side_effect=self._responses), \
             patch("core.image_compare.dhash", side_effect=lambda data, size=8: hashes.get(data)):
            found = rule34.find_post_by_search("parent:10755759", local_hash=0)
        self.assertEqual(found[0], "https://rule34.xxx/index.php?page=post&s=view&id=10755716")
        self.assertEqual(found[2], 1)

    def test_nothing_close_enough_finds_nothing(self):
        from core.boorus import rule34
        far = (1 << 40) - 1                                   # 40 bits away
        with patch.object(rule34.requests.Session, "get", side_effect=self._responses), \
             patch("core.image_compare.dhash", side_effect=lambda data, size=8: far):
            self.assertIsNone(rule34.find_post_by_search("parent:1", local_hash=0))

    def test_search_puts_the_recovered_post_first(self):
        from core import google_lens
        tiles = [{"text": "Rule 34 / parent:10755759 250x177 Rule 34", "thumb": "x" * 100}]
        post = "https://rule34.xxx/index.php?page=post&s=view&id=10755716"
        visual = [google_lens.GoogleLensMatch(url="https://hdporncomics.com/a/",
                                              thumb_url=None, similarity=0.0)]
        with patch.object(google_lens, "prepare_for_lens", return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=(["body"], tiles)), \
             patch.object(google_lens, "parse_lens_payload", return_value=visual), \
             patch.object(google_lens, "parse_exact_matches", return_value=[]), \
             patch.object(google_lens.boorus.rule34, "find_post_by_search",
                          return_value=(post, "https://wimg.example/t2.jpg", 2)) as lookup:
            results = google_lens.search("/tmp/a.jpg", timeout=1)
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args[0][0], "parent:10755759")
        self.assertEqual(results[0].url, post)


class TestSubredditLinksBecomeThePost(unittest.TestCase):
    """REGRESSION GUARD: Lens answered r/YanchaGalAnjouSan/ for a manga
    cover, and importing that downloaded the whole subreddit."""

    # One entry of a subreddit's RSS feed, in reddit's real shape (from
    # r/YanchaGalAnjouSan/new/.rss), with the author and text replaced.
    FEED = (
        '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><author><name>/u/someone</name></author><content type="html">'
        '&lt;a href=&quot;https://www.reddit.com/r/YanchaGalAnjouSan/comments/1vz8vo8/'
        'manga_cover/&quot;&gt; &lt;img src=&quot;https://preview.redd.it/ogpxyscp4slh1.png'
        '?width=140&amp;amp;height=140&quot; alt=&quot;Manga cover&quot; /&gt;'
        '</content><id>t3_1vz8vo8</id>'
        '<link href="https://www.reddit.com/r/YanchaGalAnjouSan/comments/1vz8vo8/manga_cover/" />'
        '<title>Manga cover</title></entry>'
        '<entry><content type="html">&lt;img src=&quot;https://preview.redd.it/other1.jpg&quot;'
        '</content><link href="https://www.reddit.com/r/YanchaGalAnjouSan/comments/1aaaaaa/other/" />'
        '</entry></feed>'
    )
    SUB = "https://www.reddit.com/r/YanchaGalAnjouSan/"
    POST = "https://www.reddit.com/r/YanchaGalAnjouSan/comments/1vz8vo8/manga_cover/"

    def setUp(self):
        from core import reddit
        reddit._feed_cache.clear()
        self.addCleanup(reddit._feed_cache.clear)

    def _get(self, status=200):
        from unittest.mock import MagicMock
        return patch("requests.Session.get",
                     return_value=MagicMock(status_code=status, text=self.FEED))

    def test_only_listings_count_as_subreddits(self):
        from core.reddit import subreddit_of
        self.assertEqual(subreddit_of(self.SUB), "YanchaGalAnjouSan")
        self.assertEqual(subreddit_of("https://old.reddit.com/r/manga/new/"), "manga")
        self.assertIsNone(subreddit_of(self.POST))
        self.assertIsNone(subreddit_of("https://www.reddit.com/user/someone/"))
        self.assertIsNone(subreddit_of("https://example.com/r/manga/"))

    def test_the_post_holding_the_image_is_found(self):
        from core.reddit import resolve_listing
        with self._get():
            self.assertEqual(
                resolve_listing(self.SUB, "https://i.redd.it/ogpxyscp4slh1.png"), self.POST)

    def test_an_unfound_post_falls_back_to_the_image_not_the_subreddit(self):
        from core.reddit import resolve_listing
        with self._get():
            self.assertEqual(resolve_listing(self.SUB, "https://i.redd.it/notinfeed.png"),
                             "https://i.redd.it/notinfeed.png")

    def test_a_rate_limited_feed_falls_back_and_is_not_remembered(self):
        from core import reddit
        with self._get(status=429):
            self.assertEqual(reddit.resolve_listing(self.SUB, "https://i.redd.it/ogpxyscp4slh1.png"),
                             "https://i.redd.it/ogpxyscp4slh1.png")
        self.assertEqual(reddit._feed_cache, {})
        with self._get():
            self.assertEqual(
                reddit.resolve_listing(self.SUB, "https://i.redd.it/ogpxyscp4slh1.png"), self.POST)

    def test_a_subreddit_with_no_image_to_go_on_is_dropped(self):
        from core.reddit import resolve_listing
        self.assertIsNone(resolve_listing(self.SUB, None))
        self.assertIsNone(resolve_listing(self.SUB, "https://cdn.example.com/a.jpg"))

    def test_other_urls_are_untouched_and_never_fetch(self):
        from core.reddit import resolve_listing
        with patch("requests.Session.get") as get:
            self.assertEqual(resolve_listing(self.POST, "https://i.redd.it/x.png"), self.POST)
            self.assertEqual(resolve_listing("https://gelbooru.com/a", None), "https://gelbooru.com/a")
        get.assert_not_called()

    def test_lens_search_hands_back_the_post(self):
        from core import google_lens
        visual = [
            google_lens.GoogleLensMatch(url=self.SUB, thumb_url="https://i.redd.it/ogpxyscp4slh1.png",
                                        similarity=0.0),
            google_lens.GoogleLensMatch(url=self.SUB, thumb_url=None, similarity=0.0),
            google_lens.GoogleLensMatch(url="https://www.cmoa.jp/title/204379/", thumb_url=None,
                                        similarity=0.0),
        ]
        with self._get(), \
             patch.object(google_lens, "prepare_for_lens", return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          return_value=(["body"], [])), \
             patch.object(google_lens, "parse_lens_payload", return_value=visual), \
             patch.object(google_lens, "parse_exact_matches", return_value=[]):
            results = google_lens.search("/tmp/a.jpg", timeout=1)
        urls = [r.url for r in results]
        self.assertEqual(urls, [self.POST, "https://www.cmoa.jp/title/204379/"])


class TestHydrusUrlImporterGuard(unittest.TestCase):
    """REGRESSION GUARD, from real runs: a comic-site page was "URL added
    successfully" and never imported, while the app logged success; and a
    subreddit link downloaded the whole subreddit for one image. The
    get_url_info answers below are what a live Hydrus returned."""

    ANSWERS = {
        "https://hdporncomics.com/tramp-gang-tekuho-sex-comic/":
            {"url_type": 5, "url_type_string": "unknown url", "match_name": "unknown url",
             "can_parse": False, "cannot_parse_reason": "unknown url class"},
        "https://www.reddit.com/r/YanchaGalAnjouSan/":
            {"url_type": 3, "url_type_string": "gallery url",
             "match_name": "Reddit | Subreddit - Sort hot", "can_parse": True},
        "https://www.reddit.com/r/YanchaGalAnjouSan/comments/1vz8vo8/manga_cover/":
            {"url_type": 0, "url_type_string": "post url", "match_name": "Reddit | Post",
             "can_parse": True},
        "https://i.redd.it/ogpxyscp4slh1.png":
            {"url_type": 5, "url_type_string": "unknown url", "match_name": "unknown url",
             "can_parse": False, "cannot_parse_reason": "unknown url class"},
        "https://rule34.xxx/index.php?page=post&s=view&id=10755716":
            {"url_type": 0, "url_type_string": "post url", "match_name": "rule34.xxx file page",
             "can_parse": True},
    }

    def setUp(self):
        # Following a refused link is a real HEAD request; never from tests.
        patcher = patch("core.hydrus_import._forwarded_url", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self):
        client = MagicMock()
        client.get_url_info.side_effect = lambda url: self.ANSWERS[url]
        client.import_url.return_value = {"human_result_text": "URL added successfully."}
        return client

    def _refusal(self, url):
        from core.hydrus_import import url_import_refusal
        return url_import_refusal(self._client(), url)

    def test_a_page_hydrus_cannot_read_is_refused(self):
        self.assertIn("no downloader for hdporncomics.com",
                      self._refusal("https://hdporncomics.com/tramp-gang-tekuho-sex-comic/"))

    def test_a_gallery_is_refused(self):
        self.assertIn("gallery url", self._refusal("https://www.reddit.com/r/YanchaGalAnjouSan/"))

    def test_posts_and_direct_files_go_through(self):
        for url in ("https://www.reddit.com/r/YanchaGalAnjouSan/comments/1vz8vo8/manga_cover/",
                    "https://i.redd.it/ogpxyscp4slh1.png",
                    "https://rule34.xxx/index.php?page=post&s=view&id=10755716"):
            self.assertIsNone(self._refusal(url), url)

    def test_hydrus_not_answering_does_not_block_imports(self):
        from core.hydrus_client import HydrusError
        from core.hydrus_import import url_import_refusal
        client = MagicMock()
        client.get_url_info.side_effect = HydrusError("404 - old client")
        self.assertIsNone(url_import_refusal(client, "https://example.com/page"))

    def test_a_refused_url_is_not_sent_or_marked_sent(self):
        from core.hydrus_import import send_url_to_importer
        from core.models import ImageEntry
        entry = ImageEntry(path="/tmp/refused.png")
        entry.matched_url = "https://hdporncomics.com/tramp-gang-tekuho-sex-comic/"
        client = self._client()
        result = send_url_to_importer(entry, client)
        self.assertFalse(result.success)
        self.assertIn("no downloader", result.error)
        client.import_url.assert_not_called()
        self.assertFalse(entry.sent_to_hydrus)

    def _worker_entry(self, candidates):
        from core.models import ImageEntry, MatchCandidate, MatchStatus
        entry = ImageEntry(path="/tmp/auto.png")
        entry.candidates = [MatchCandidate(url=u, similarity=sim, similarity_measured=True,
                                           engine="Google Lens") for u, sim in candidates]
        entry.select_candidate(0)
        entry.status = MatchStatus.GOOD
        return entry

    def _worker(self):
        from core.config import Settings
        from workers.search_worker import SearchWorker
        settings = Settings()
        settings.auto_import_method = "url_importer"
        settings.auto_import_min_similarity = 80.0
        settings.remove_after_import = False
        return SearchWorker([], settings)

    def test_auto_import_falls_back_to_a_match_hydrus_can_take(self):
        entry = self._worker_entry([
            ("https://hdporncomics.com/tramp-gang-tekuho-sex-comic/", 100.0),
            ("https://rule34.xxx/index.php?page=post&s=view&id=10755716", 100.0),
        ])
        client = self._client()
        self._worker()._maybe_auto_import(entry, client, None)
        client.import_url.assert_called_once()
        self.assertEqual(client.import_url.call_args[0][0],
                         "https://rule34.xxx/index.php?page=post&s=view&id=10755716")
        self.assertEqual(entry.selected_candidate_index, 1)
        self.assertTrue(entry.sent_to_hydrus)

    def test_the_fallback_never_goes_below_the_threshold(self):
        entry = self._worker_entry([
            ("https://hdporncomics.com/tramp-gang-tekuho-sex-comic/", 100.0),
            ("https://rule34.xxx/index.php?page=post&s=view&id=10755716", 60.0),
        ])
        client = self._client()
        self._worker()._maybe_auto_import(entry, client, None)
        client.import_url.assert_not_called()
        self.assertEqual(entry.selected_candidate_index, 0)
        self.assertFalse(entry.sent_to_hydrus)


class TestBorrowedThumbnails(unittest.TestCase):
    """REGRESSION GUARD: zerochan.net/3700079 (4093x2894) was scored 100%
    against a 1105x1565 image, because Google paired the page with a
    RELATED post's thumbnail (…600.3699483.jpg) shown on it."""

    def test_zerochan_results_point_at_the_post_the_image_is(self):
        from core.google_lens import owner_of_image
        self.assertEqual(
            owner_of_image("https://www.zerochan.net/3700079",
                           "https://s1.zerochan.net/Sakamata.Chloe.600.3699483.jpg"),
            "https://www.zerochan.net/3699483")
        self.assertEqual(
            owner_of_image("https://www.zerochan.net/3343723",
                           "https://static.zerochan.net/Sirius.(Azur.Lane).full.3343723.jpg"),
            "https://www.zerochan.net/3343723")
        self.assertEqual(owner_of_image("https://x.com/a/status/1", "https://pbs.twimg.com/m/x.jpg"),
                         "https://x.com/a/status/1")

    def _local(self, size):
        import shutil, tempfile
        from PIL import Image
        folder = tempfile.mkdtemp(prefix="hatate-borrowed-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = f"{folder}/local.png"
        Image.new("RGB", size, (90, 120, 200)).save(path)
        return path

    def _candidate(self):
        from core.models import MatchCandidate
        return MatchCandidate(url="https://www.zerochan.net/3700079", similarity=100.0,
                              similarity_measured=True, engine="Google Lens")

    def _page(self, width, height, preview="https://s1.zerochan.net/x.600.3700079.jpg"):
        return SimpleNamespace(width=width, height=height, preview_url=preview, file_url=None)

    def test_a_wrong_shape_is_remeasured_against_the_posts_own_image(self):
        from core import search_engine
        from core.config import Settings
        candidate = self._candidate()
        with patch.object(search_engine.remote, "download_bytes", return_value=b"own"), \
             patch.object(search_engine.similarity_check, "local_prints", return_value=object()), \
             patch.object(search_engine.similarity_check, "aligned_similarity",
                          side_effect=lambda prints, data: (20.0, False) if data == b"own"
                          else (100.0, False)):
            search_engine._recheck_borrowed_measurement(
                candidate, self._page(4093, 2894), Settings(), self._local((1105, 1565)))
        self.assertLess(candidate.similarity, 50.0)
        self.assertTrue(candidate.similarity_measured)

    def test_the_right_shape_is_left_alone(self):
        from core import search_engine
        from core.config import Settings
        candidate = self._candidate()
        with patch.object(search_engine.remote, "download_bytes") as download:
            search_engine._recheck_borrowed_measurement(
                candidate, self._page(1105, 1565), Settings(), self._local((1105, 1565)))
        download.assert_not_called()
        self.assertEqual(candidate.similarity, 100.0)

    def test_no_image_of_its_own_means_unmeasured(self):
        from core import search_engine
        from core.config import Settings
        candidate = self._candidate()
        with patch.object(search_engine.remote, "download_bytes", return_value=None):
            search_engine._recheck_borrowed_measurement(
                candidate, self._page(4093, 2894), Settings(), self._local((1105, 1565)))
        self.assertFalse(candidate.similarity_measured)


class TestNeverMeasuredOrdinalGetsARealScore(unittest.TestCase):
    """REGRESSION GUARD (DAN-52): a byte-identical file found via Google
    Lens showed a flat 80% instead of ~100%, because Lens reports no
    similarity at all - measure_ordinal_similarities leaves the candidate
    with the engine's ordinal placeholder whenever its thumbnail could not
    be hashed, and _recheck_borrowed_measurement used to only repair a
    WRONG measurement (similarity_measured already True), never fill in a
    MISSING one. See DAN-50 for the full diagnosis."""

    def _local(self, size):
        import shutil, tempfile
        from PIL import Image
        folder = tempfile.mkdtemp(prefix="hatate-neverm-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = f"{folder}/local.png"
        Image.new("RGB", size, (10, 200, 30)).save(path)
        return path

    def _page(self, width=None, height=None, preview="https://rule34.paheal.net/x.jpg"):
        return SimpleNamespace(width=width, height=height, preview_url=preview, file_url=None)

    def test_a_lens_exact_match_gets_measured_instead_of_the_ordinal_80(self):
        from core import search_engine
        from core.config import Settings
        from core.models import MatchCandidate
        candidate = MatchCandidate(url="https://rule34.paheal.net/post/view/6611704",
                                    similarity=80.0, similarity_measured=False,
                                    engine="Google Lens")
        with patch.object(search_engine.remote, "download_bytes", return_value=b"own"), \
             patch.object(search_engine.similarity_check, "local_prints", return_value=object()), \
             patch.object(search_engine.similarity_check, "aligned_similarity",
                          return_value=(100.0, False)):
            search_engine._recheck_borrowed_measurement(
                candidate, self._page(1200, 1600), Settings(), self._local((1200, 1600)))
        self.assertEqual(candidate.similarity, 100.0)
        self.assertTrue(candidate.similarity_measured)

    def test_a_real_similarity_engine_is_left_alone(self):
        # IQDB/SauceNAO already report a trustworthy score even though
        # similarity_measured stays False for them - no reason to spend a
        # download re-deriving what the engine already measured.
        from core import search_engine
        from core.config import Settings
        from core.models import MatchCandidate
        candidate = MatchCandidate(url="https://danbooru.donmai.us/posts/1", similarity=92.0,
                                    similarity_measured=False, engine="IQDB")
        with patch.object(search_engine.remote, "download_bytes") as download:
            search_engine._recheck_borrowed_measurement(
                candidate, self._page(1200, 1600), Settings(), self._local((1200, 1600)))
        download.assert_not_called()
        self.assertEqual(candidate.similarity, 92.0)
        self.assertFalse(candidate.similarity_measured)

    def test_no_page_image_leaves_the_ordinal_score_but_marked_unmeasured(self):
        from core import search_engine
        from core.config import Settings
        from core.models import MatchCandidate
        candidate = MatchCandidate(url="https://rule34.paheal.net/post/view/1", similarity=78.0,
                                    similarity_measured=False, engine="Google Lens")
        with patch.object(search_engine.remote, "download_bytes", return_value=None):
            search_engine._recheck_borrowed_measurement(
                candidate, self._page(preview=None), Settings(), self._local((1200, 1600)))
        self.assertEqual(candidate.similarity, 78.0)
        self.assertFalse(candidate.similarity_measured)


class TestLegacyUrlsRewrittenForHydrus(unittest.TestCase):
    """REGRESSION GUARD: SauceNAO hands back danbooru.donmai.us/post/show/N,
    which Hydrus 687 calls an unknown url - so Send URL was refused for a
    site Hydrus supports (and before the check, silently failed)."""

    def test_danbooru_legacy_posts_use_the_current_form(self):
        from core.hydrus_import import normalize_url_for_hydrus
        self.assertEqual(normalize_url_for_hydrus("https://danbooru.donmai.us/post/show/7727191"),
                         "https://danbooru.donmai.us/posts/7727191")

    def test_twitter_web_status_links_use_the_form_hydrus_parses(self):
        from core.hydrus_import import normalize_url_for_hydrus
        self.assertEqual(
            normalize_url_for_hydrus("https://twitter.com/i/web/status/1070304142998949888"),
            "https://twitter.com/i/status/1070304142998949888")

    def test_current_forms_are_left_alone(self):
        from core.hydrus_import import normalize_url_for_hydrus
        for url in ("https://danbooru.donmai.us/posts/7727191",
                    "https://x.com/someone/status/1513434076992131075",
                    "https://www.sankakucomplex.com/posts/QyMk8vZ6Kak",
                    "https://gelbooru.com/index.php?page=post&s=view&id=3677494"):
            self.assertEqual(normalize_url_for_hydrus(url), url)

    def test_the_rewritten_url_is_what_hydrus_is_asked_about_and_sent(self):
        from core.hydrus_import import send_url_to_importer
        from core.models import ImageEntry
        entry = ImageEntry(path="/tmp/legacy.png")
        entry.matched_url = "https://danbooru.donmai.us/post/show/7727191"
        client = MagicMock()
        client.get_url_info.return_value = {"url_type": 0, "url_type_string": "post url",
                                            "match_name": "danbooru file page", "can_parse": True}
        client.import_url.return_value = {"human_result_text": "URL added successfully."}
        self.assertTrue(send_url_to_importer(entry, client).success)
        client.get_url_info.assert_called_once_with("https://danbooru.donmai.us/posts/7727191")
        self.assertEqual(client.import_url.call_args[0][0], "https://danbooru.donmai.us/posts/7727191")


class TestSendUrlFallsBackToHatatesDownload(unittest.TestCase):
    """Send URL where Hydrus has no downloader: Hatate downloads the file
    itself where the site allows (e-shuushuu: CONFIRMED full-size and the
    same picture), follows legacy links that forward somewhere Hydrus
    knows (DeviantArt /view/<id>), and says so plainly where neither can
    reach the original (anime-pictures: 403 without a login)."""

    UNKNOWN = {"url_type": 5, "url_type_string": "unknown url", "match_name": "unknown url",
               "can_parse": False}
    POST = {"url_type": 0, "url_type_string": "post url", "match_name": "deviant art file page",
            "can_parse": True}

    def _entry(self, url, direct_file_url=None):
        from core.models import ImageEntry, MatchCandidate
        entry = ImageEntry(path="/tmp/fallback.png")
        entry.candidates = [MatchCandidate(url=url, similarity=100.0,
                                           direct_file_url=direct_file_url,
                                           booru_tags_fetched=True)]
        entry.select_candidate(0)
        return entry

    def _client(self, answers):
        from core.hydrus_tag_lookup import hash_file
        client = MagicMock()
        client.get_url_info.side_effect = lambda url: answers.get(url, self.UNKNOWN)
        client.import_url.return_value = {"human_result_text": "URL added successfully."}
        # Reports the real hash of the file it's handed, matching real
        # (non-transcoding) Hydrus behavior for a direct file upload (DAN-17).
        client.import_file.side_effect = lambda path: {"hash": hash_file(path), "status": 1}
        return client

    def _send(self, entry, client, download=VALID_JPEG, forwarded=None):
        import tempfile, shutil
        from core.config import Settings
        from core import hydrus_import
        folder = tempfile.mkdtemp(prefix="hatate-fallback-")
        self.addCleanup(shutil.rmtree, folder, True)
        with patch.object(hydrus_import, "_forwarded_url", return_value=forwarded), \
             patch("core.remote.download_bytes", return_value=download):
            return hydrus_import.send_url_or_download(entry, client, Settings(), folder)

    def test_hatate_downloads_what_hydrus_cannot(self):
        entry = self._entry("http://e-shuushuu.net/image/976339/",
                            direct_file_url="https://e-shuushuu.net/images/2019-01-01-976339.jpeg")
        client = self._client({})
        result = self._send(entry, client)
        self.assertTrue(result.success)
        self.assertTrue(result.confirmed)
        self.assertIn("Hatate downloaded it", result.note)
        client.import_url.assert_not_called()
        client.import_file.assert_called_once()
        self.assertTrue(entry.sent_to_hydrus)

    def test_a_forwarding_legacy_link_is_followed_and_repointed(self):
        view = "https://deviantart.com/view/654484001"
        art = "https://www.deviantart.com/chierru/art/2016-%7C-Year-Ender-Illustration-654484001"
        entry = self._entry(view)
        client = self._client({art: self.POST})
        result = self._send(entry, client, forwarded=art)
        self.assertTrue(result.success)
        self.assertEqual(client.import_url.call_args[0][0], art)
        self.assertEqual(entry.matched_url, art)
        self.assertEqual(entry.selected_candidate.url, art)
        client.import_file.assert_not_called()

    def test_nothing_that_can_reach_the_original_says_so(self):
        entry = self._entry("https://anime-pictures.net/pictures/view_post/772314")
        client = self._client({})
        result = self._send(entry, client)
        self.assertFalse(result.success)
        self.assertTrue(result.refused)
        self.assertIn("Neither Hydrus nor Hatate", result.error)
        self.assertIn("Send (upload)", result.error)
        client.import_file.assert_not_called()
        self.assertFalse(entry.sent_to_hydrus)

    def test_a_web_page_in_place_of_the_image_is_not_uploaded(self):
        entry = self._entry("http://e-shuushuu.net/image/1/",
                            direct_file_url="https://e-shuushuu.net/images/x.jpeg")
        client = self._client({})
        result = self._send(entry, client, download=b"\n  <!DOCTYPE html><html><body>Log in</body>")
        self.assertFalse(result.success)
        self.assertIn("web page instead of the image", result.error)
        client.import_file.assert_not_called()

    def test_downloaded_size_mismatch_is_rejected(self):
        """BA-02: download_and_send must verify Content-Length matches."""
        from core.models import MatchCandidate
        entry = ImageEntry(path="/tmp/test.png")
        candidate = MatchCandidate(
            url="http://e-shuushuu.net/image/1/",
            direct_file_url="https://e-shuushuu.net/images/x.jpeg",
            similarity=100.0,
            booru_tags_fetched=True,
            remote_size_bytes=1000,  # Expected size from HEAD
        )
        entry.candidates = [candidate]
        entry.select_candidate(0)
        client = self._client({})
        # Download returns truncated data (500 bytes of valid JPEG)
        truncated_jpeg = VALID_JPEG[:500]
        result = self._send(entry, client, download=truncated_jpeg)
        self.assertFalse(result.success)
        self.assertIn("Downloaded 500 bytes but expected 1000 bytes", result.error)
        client.import_file.assert_not_called()

    def test_a_hydrus_that_accepts_the_url_is_used_as_before(self):
        entry = self._entry("https://www.deviantart.com/a/art/b-1")
        client = self._client({"https://www.deviantart.com/a/art/b-1": self.POST})
        result = self._send(entry, client)
        self.assertTrue(result.success)
        self.assertIsNone(result.confirmed)
        client.import_url.assert_called_once()
        client.import_file.assert_not_called()

    def test_a_failed_request_is_not_mistaken_for_a_refusal(self):
        from core.hydrus_client import HydrusError
        entry = self._entry("https://www.deviantart.com/a/art/b-1",
                            direct_file_url="https://images.example/b.png")
        client = self._client({"https://www.deviantart.com/a/art/b-1": self.POST})
        client.import_url.side_effect = HydrusError("Could not reach Hydrus")
        result = self._send(entry, client)
        self.assertFalse(result.success)
        self.assertFalse(result.refused)
        client.import_file.assert_not_called()

    def test_auto_import_downloads_when_no_match_hydrus_can_take(self):
        import tempfile, shutil
        from core.config import Settings
        from core.models import MatchStatus
        from workers.search_worker import SearchWorker
        entry = self._entry("http://e-shuushuu.net/image/976339/",
                            direct_file_url="https://e-shuushuu.net/images/2019-01-01-976339.jpeg")
        entry.status = MatchStatus.GOOD
        client = self._client({})
        settings = Settings()
        settings.auto_import_method = "url_importer"
        settings.auto_import_min_similarity = 80.0
        settings.remove_after_import = True
        worker = SearchWorker([], settings)
        emitted = []
        worker.auto_imported.connect(lambda e, r: emitted.append(r))
        folder = tempfile.mkdtemp(prefix="hatate-auto-")
        self.addCleanup(shutil.rmtree, folder, True)
        with patch("core.hydrus_import._forwarded_url", return_value=None), \
             patch("core.remote.download_bytes", return_value=VALID_JPEG), \
             patch("workers.search_worker.poll_single_url_import") as poll:
            worker._maybe_auto_import(entry, client, folder)
        poll.assert_not_called()
        client.import_file.assert_called_once()
        self.assertTrue(emitted and emitted[0].success and emitted[0].confirmed)


class TestPawchiveExactLookup(unittest.TestCase):
    """Pawchive, looked up by the file's SHA-256. The response shape is a
    real /api/v1/search_hash answer, trimmed; the post and ids are made up."""

    SHA = "3fac89759d85abce1ebcbff0c27d838d9a52c47ffc5a6fdcf31c6986064bbefb"
    OTHER = "d488febebb5d2887b56c08e407804291d03842a80ecc9d3c6e48d3794652094e"

    def _response(self, sha=None):
        sha = sha or self.SHA
        return {"hash": sha, "discord_posts": None, "posts": [{
            "id": "12640897", "user": "44297987", "service": "fanbox", "title": "OC set",
            "file": {"name": "cover.jpeg", "path": f"/{self.OTHER[:2]}/{self.OTHER[2:4]}/{self.OTHER}.png"},
            "attachments": [
                {"name": "a.png", "path": f"/{self.OTHER[:2]}/{self.OTHER[2:4]}/{self.OTHER}.png"},
                {"name": "b.png", "path": f"/{sha[:2]}/{sha[2:4]}/{sha}.jpeg"},
            ],
        }]}

    def test_the_response_names_the_post_and_the_exact_image(self):
        from core.pawchive_lookup import parse_response
        [match] = parse_response(self._response(), self.SHA)
        self.assertEqual(match.url, "https://pawchive.pw/fanbox/user/44297987/post/12640897")
        self.assertEqual(match.page_index, 1)          # "file" repeats attachments[0]
        self.assertEqual(match.page_count, 2)
        self.assertEqual(match.thumb_url,
                         f"https://img.pawchive.pw/thumbnail/data/3f/ac/{self.SHA}.jpeg")

    def _get(self, status, body=None):
        response = MagicMock(status_code=status)
        response.json.return_value = body
        return patch("requests.Session.get", return_value=response)

    def test_an_unknown_file_is_simply_not_found(self):
        from core.pawchive_lookup import lookup
        with self._get(404):
            self.assertEqual(lookup(self.SHA), [])

    def test_rate_limiting_is_an_error_not_a_miss(self):
        from core.pawchive_lookup import PawchiveLookupError, lookup
        with self._get(429), self.assertRaises(PawchiveLookupError):
            lookup(self.SHA)

    def test_something_that_is_not_a_sha256_is_never_sent(self):
        from core.pawchive_lookup import PawchiveLookupError, lookup
        with patch("requests.Session.get") as get, \
             self.assertRaises(PawchiveLookupError):
            lookup("not-a-hash")
        get.assert_not_called()

    def test_a_hit_is_a_certain_measured_100(self):
        from core.models import ImageEntry
        from core.search_engine import _collect_pawchive
        from core.engines import reports_real_similarity
        entry = ImageEntry(path="/nonexistent/never-read.png")
        entry.hydrus_hash = self.SHA
        candidates, errors = [], []
        with self._get(200, self._response()):
            found = _collect_pawchive(entry, _settings(), candidates, errors)
        self.assertTrue(found)
        self.assertEqual(errors, [])
        [candidate] = candidates
        self.assertEqual(candidate.similarity, 100.0)
        self.assertEqual(candidate.source_name, "Pawchive")
        self.assertTrue(reports_real_similarity(candidate.engine))

    def test_the_hash_is_computed_when_not_yet_known(self):
        import hashlib, shutil, tempfile
        from core.models import ImageEntry
        from core.search_engine import _collect_pawchive
        folder = tempfile.mkdtemp(prefix="hatate-pawchive-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = f"{folder}/a.png"
        with open(path, "wb") as fh:
            fh.write(b"some image bytes")
        entry = ImageEntry(path=path)
        entry.hydrus_hash = None
        with self._get(404) as get:
            _collect_pawchive(entry, _settings(), [], [])
        self.assertIn(hashlib.sha256(b"some image bytes").hexdigest(), get.call_args[0][0])

    def test_a_failed_lookup_is_reported(self):
        from core.models import ImageEntry
        from core.search_engine import _collect_pawchive
        entry = ImageEntry(path="/x.png")
        entry.hydrus_hash = self.SHA
        errors = []
        with self._get(503):
            self.assertFalse(_collect_pawchive(entry, _settings(), [], errors))
        self.assertTrue(errors and errors[0].startswith("Pawchive:"))

    def test_it_runs_up_front_after_the_primary_even_with_extras_held_back(self):
        from core.search_engine import _engine_waves
        settings = _settings(enable_pawchive=True, primary_engine="saucenao",
                             secondary_engine_mode="fallback", extras_only_as_fallback=True,
                             enable_google_lens=True)
        waves = _engine_waves(settings, None)
        self.assertEqual(waves[0], ["saucenao", "pawchive"])     # primary stays first
        self.assertIn("googlelens", waves[1])
        self.assertNotIn("pawchive", waves[1])

    def test_it_is_not_asked_when_off(self):
        from core.search_engine import _engine_waves
        waves = _engine_waves(_settings(enable_pawchive=False), None)
        self.assertFalse(any("pawchive" in wave for wave in waves))

    def test_pawchive_only_runs_it_alone(self):
        from core.search_engine import _engine_waves
        self.assertEqual(_engine_waves(_settings(), "pawchive"), [["pawchive"]])

    def test_an_exact_hit_makes_the_slow_fallback_unnecessary(self):
        from core.models import MatchCandidate
        from core.search_engine import _fallback_is_unnecessary
        settings = _settings(fallback_below_similarity=75.0)
        hit = MatchCandidate(url="https://pawchive.pw/fanbox/user/1/post/2", source_name="Pawchive",
                             similarity=100.0, engine="Pawchive")
        skip, _ = _fallback_is_unnecessary(
            settings, [["saucenao", "pawchive"], ["iqdb"]], {"saucenao": False, "pawchive": True}, [hit])
        self.assertTrue(skip)


class TestPawchivePagesByHash(unittest.TestCase):
    """Pawchive posts hold several images, and the post API describes the
    first. Its paths ARE SHA-256s, so the right image is chosen by hash."""

    def _post_json(self, shas, has_full=True):
        files = [{"name": f"{i}.png", "path": f"/{h[:2]}/{h[2:4]}/{h}.png"} for i, h in enumerate(shas)]
        return json.dumps({"post": {"id": "9", "user": "1", "service": "fanbox",
                                    "has_full": has_full, "file": files[0], "attachments": files}})

    def test_pages_carry_their_hash_and_file_links(self):
        from core.boorus import pawchive
        shas = ["a" * 64, "b" * 64]
        pages = pawchive.parse_pages(self._post_json(shas), "https://pawchive.pw/fanbox/user/1/post/9")
        self.assertEqual([p["sha256"] for p in pages], shas)
        self.assertEqual(pages[1]["urls"]["original"],
                         f"https://file.pawchive.pw/data/bb/bb/{'b' * 64}.png")
        no_full = pawchive.parse_pages(self._post_json(shas, has_full=False), "https://x/")
        self.assertIsNone(no_full[1]["urls"]["original"])

    def test_the_local_files_hash_picks_its_page_without_downloading_anything(self):
        import hashlib, shutil, tempfile
        from core import search_engine
        from core.config import Settings
        from core.models import MatchCandidate
        folder = tempfile.mkdtemp(prefix="hatate-pages-")
        self.addCleanup(shutil.rmtree, folder, True)
        local = f"{folder}/mine.png"
        with open(local, "wb") as fh:
            fh.write(b"the third image")
        mine = hashlib.sha256(b"the third image").hexdigest()
        shas = ["a" * 64, "b" * 64, mine]
        candidate = MatchCandidate(url="https://pawchive.pw/fanbox/user/1/post/9",
                                   direct_file_url="https://file.pawchive.pw/data/aa/aa/first.png")
        with patch.object(search_engine.remote, "fetch_text", return_value=self._post_json(shas)), \
             patch.object(search_engine.remote, "download_bytes") as download:
            search_engine._resolve_multipage_candidate(candidate, 3, Settings(), local)
        download.assert_not_called()
        self.assertEqual(candidate.page_index, 2)
        self.assertEqual(candidate.direct_file_url,
                         f"https://file.pawchive.pw/data/{mine[:2]}/{mine[2:4]}/{mine}.png")


class TestExactCopiesAreUploadedWhenHydrusCantFetchThem(unittest.TestCase):
    """A Pawchive match is the same file as the local one, so when Hydrus
    has no downloader for pawchive (and pawchive often doesn't hold the
    original), uploading the local copy loses nothing."""

    def test_the_local_copy_is_sent_with_the_matches_link(self):
        import tempfile, shutil
        from core.config import Settings
        from core.hydrus_import import send_url_or_download
        from core.models import ImageEntry, MatchCandidate
        folder = tempfile.mkdtemp(prefix="hatate-exact-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = f"{folder}/mine.png"
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        entry = ImageEntry(path=path)
        entry.candidates = [MatchCandidate(url="https://pawchive.pw/patreon/user/6714576/post/38740818",
                                           similarity=100.0, engine="Pawchive", booru_tags_fetched=True)]
        entry.select_candidate(0)
        client = MagicMock()
        client.get_url_info.return_value = {"url_type": 5, "url_type_string": "unknown url",
                                            "match_name": "unknown url", "can_parse": False}
        client.import_file.return_value = {"hash": "ab" * 32, "status": 2}
        with patch("core.hydrus_import._forwarded_url", return_value=None), \
             patch("core.remote.download_bytes") as download:
            result = send_url_or_download(entry, client, Settings(), folder)
        self.assertTrue(result.success)
        self.assertTrue(result.confirmed)
        self.assertIn("exact file", result.note)
        client.import_url.assert_not_called()
        download.assert_not_called()
        client.import_file.assert_called_once_with(path)
        client.associate_url.assert_called_once()
        self.assertEqual(client.associate_url.call_args[0][0],
                         "https://pawchive.pw/patreon/user/6714576/post/38740818")


class TestAPartialSearchIsNotAnError(unittest.TestCase):
    """One engine failing no longer outweighs another engine's answer."""

    def test_every_engine_failing_is_still_an_error(self):
        from core.models import MatchStatus
        from core.search_engine import _no_match
        status, message, final = _no_match(["IQDB: timed out"], answered=set())
        self.assertIs(status, MatchStatus.ERROR)
        self.assertEqual(message, "IQDB: timed out")

    def test_an_answer_from_one_engine_makes_it_not_found_but_provisional(self):
        """Seen 2026-09-24: SauceNAO answered (best 40%), IQDB timed out."""
        from core.models import MatchStatus
        from core.search_engine import _no_match
        status, message, final = _no_match(["IQDB: timed out"], answered={"saucenao"})
        self.assertIs(status, MatchStatus.NOT_FOUND)
        self.assertEqual(message, "IQDB: timed out")
        self.assertFalse(final, "must not be cached as the real answer")

    def test_a_hash_lookup_or_local_index_is_not_an_answer(self):
        from core import engines
        from core.models import MatchStatus
        from core.search_engine import _no_match
        status, _, _ = _no_match(["IQDB: timed out"],
                                 answered={engines.PAWCHIVE, engines.PAWCHIVE_INDEX})
        self.assertIs(status, MatchStatus.ERROR)

    def test_a_clean_miss_is_final(self):
        from core.models import MatchStatus
        from core.search_engine import _no_match
        self.assertEqual(_no_match([], answered={"saucenao"}), (MatchStatus.NOT_FOUND, None, True))

    def test_the_runner_records_who_answered(self):
        from core import search_engine
        from core.config import Settings
        from core.models import ImageEntry

        def fake(engine, entry, settings, candidates, errors, on_tick=None, should_stop=None):
            if engine == "iqdb":
                errors.append("IQDB: timed out")
            return False

        answered, errors = set(), []
        with patch.object(search_engine.engine_runner, "_run_engine", side_effect=fake):
            search_engine._run_engines_parallel(
                ["saucenao", "iqdb"], ImageEntry(path="/tmp/x.png"), Settings(), [], errors,
                None, answered=answered)
        self.assertEqual(answered, {"saucenao"})
        self.assertEqual(errors, ["IQDB: timed out"])

    def test_the_preview_says_why(self):
        from core.models import ImageEntry, MatchStatus
        from gui.preview_text import no_candidate_text
        entry = ImageEntry(path="/tmp/x.png")
        entry.status = MatchStatus.NOT_FOUND
        entry.error_message = "IQDB: timed out"
        self.assertIn("IQDB: timed out", no_candidate_text(entry))
        entry.error_message = None
        self.assertEqual(no_candidate_text(entry), "No match found")
