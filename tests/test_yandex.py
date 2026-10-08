"""core/yandex.py - reading the results page, keeping only post pages, and
standing down when Yandex refuses. No request reaches Yandex."""
import html
import json
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401


def _site(url, domain="example.com", w=1000, h=1400):
    return {
        "title": "t", "url": url + "&utm_medium=organic&utm_source=yandexsmartcamera"
        if "?" in url else url + "?utm_medium=organic&utm_source=yandexsmartcamera",
        "domain": domain,
        "thumb": {"url": "//avatars.mds.yandex.net/i?id=abc&n=13", "width": 148, "height": 90},
        "originalImage": {"url": "https://img.example/1.jpg", "width": w, "height": h},
    }


def _page(sites):
    """A results page shaped like Yandex's: other data-state blobs first,
    then the one carrying cbirSites, HTML-escaped as served."""
    other = html.escape(json.dumps({"form": {"action": "/images/search"}}), quote=True)
    state = html.escape(json.dumps({"initialState": {"cbirSites": {"sites": sites}}}), quote=True)
    return (f'<html><div data-state="{other}"></div>'
            f'<div class="CbirSites" data-state="{state}"></div></html>')


SITES = [
    _site("https://ru.pinterest.com/pin/267753140320940932/", "ru.pinterest.com"),
    _site("https://rule34.us/index.php?r=posts%2Findex&q=dirtynicky", "rule34.us"),
    _site("https://rule34.us/index.php?r=posts%2Fview&id=757670", "rule34.us"),
    _site("https://safebooru.org/index.php?page=post&s=list&tags=baku-p", "safebooru.org"),
    _site("https://kagamihara.donmai.us/posts/6307169?q=1girl", "kagamihara.donmai.us"),
    _site("https://telegra.ph/Some-post-01-01", "telegra.ph"),
    _site("https://xbooru.com/index.php?page=post&s=view&id=855502", "xbooru.com"),
]


class TestReadingTheResultsPage(unittest.TestCase):
    def test_finds_the_sites_block_among_the_others(self):
        from core import yandex
        self.assertEqual(len(yandex.parse_sites(_page(SITES))), len(SITES))

    def test_a_page_with_no_block_is_not_an_empty_answer(self):
        from core import yandex
        self.assertIsNone(yandex.parse_sites("<html><div data-state=\"{}\"></div></html>"))
        self.assertEqual(yandex.parse_sites(_page([])), [])

    def test_keeps_only_post_pages_the_app_can_read(self):
        """Pinterest and Telegraph carry no tags; a tag search or a post
        list is not the post, even on a site with a parser."""
        from core import yandex
        urls = [m.url for m in yandex.keep_post_pages(yandex.parse_sites(_page(SITES)))]
        self.assertEqual(len(urls), 3)
        self.assertIn("rule34.us", urls[0])
        self.assertIn("757670", urls[0])
        self.assertTrue(urls[1].startswith("https://danbooru.donmai.us/posts/6307169"))
        self.assertIn("855502", urls[2])
        self.assertFalse(any("utm_" in u for u in urls))

    def test_scored_by_position_among_the_kept(self):
        from core import yandex
        matches = yandex.keep_post_pages(yandex.parse_sites(_page(SITES)))
        self.assertEqual([m.similarity for m in matches], [80.0, 78.0, 76.0])
        self.assertTrue(matches[0].thumb_url.startswith("https://avatars.mds.yandex.net/"))
        self.assertEqual((matches[0].width, matches[0].height), (1000, 1400))

    def test_the_same_post_twice_is_one_row(self):
        from core import yandex
        twice = [SITES[2], SITES[2]]
        self.assertEqual(len(yandex.keep_post_pages(twice)), 1)


class _Resp:
    def __init__(self, url="https://yandex.com/images/search", text="", payload=None, status=200):
        self.url, self.text, self._payload, self.status_code = url, text, payload, status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


UPLOAD_OK = _Resp(payload={"blocks": [{"params": {"cbirId": "1617445/abc"}}]})
CAPTCHA = _Resp(url="https://yandex.com/showcaptcha?cc=1&retpath=x", text="<html>captcha</html>")


class TestStandingDown(unittest.TestCase):
    def setUp(self):
        from core import yandex
        yandex.reset_blocked_flag()
        yandex.end_rest()
        self.addCleanup(yandex.reset_blocked_flag)
        self.addCleanup(yandex.end_rest)

    def _search(self, post, get=None):
        from core import yandex
        with patch.object(yandex, "prepare_upload", return_value=b"jpeg"), \
             patch("core.yandex.requests.Session.post", return_value=post), \
             patch("core.yandex.requests.Session.get", return_value=get):
            return yandex.search("/tmp/x.jpg", timeout=5)

    def test_a_good_answer(self):
        matches = self._search(UPLOAD_OK, _Resp(text=_page(SITES)))
        self.assertEqual(len(matches), 3)

    def test_a_captcha_rests_yandex_through_a_new_search(self):
        from core import yandex
        with self.assertRaises(yandex.YandexBlockedError) as caught:
            self._search(CAPTCHA)
        yandex.reset_blocked_flag()                  # what starting a search does
        self.assertTrue(yandex.is_blocked())
        self.assertIn(yandex.resting_until(), str(caught.exception))

    def test_a_captcha_on_the_results_page_counts_too(self):
        from core import yandex
        with self.assertRaises(yandex.YandexBlockedError):
            self._search(UPLOAD_OK, CAPTCHA)
        self.assertTrue(yandex.is_resting())

    def test_the_rest_ends(self):
        from core import yandex
        with self.assertRaises(yandex.YandexBlockedError):
            self._search(CAPTCHA)
        later = __import__("time").monotonic() + yandex.CHALLENGE_REST_SECONDS + 1
        with patch("core.yandex.time.monotonic", return_value=later):
            self.assertFalse(yandex.is_blocked())

    def test_a_changed_page_stands_down_after_a_run_of_them(self):
        from core import yandex
        broken = _Resp(text="<html>no results block</html>")
        for _ in range(yandex.MAX_CONSECUTIVE_FAILURES - 1):
            with self.assertRaises(yandex.YandexError) as caught:
                self._search(UPLOAD_OK, broken)
            self.assertNotIsInstance(caught.exception, yandex.YandexBlockedError)
        with self.assertRaises(yandex.YandexBlockedError):
            self._search(UPLOAD_OK, broken)
        self.assertTrue(yandex.is_blocked())
        self.assertFalse(yandex.is_resting())        # not a captcha - a new search retries
        yandex.reset_blocked_flag()
        self.assertFalse(yandex.is_blocked())

    def test_a_success_clears_the_run_of_failures(self):
        from core import yandex
        broken = _Resp(text="<html></html>")
        for _ in range(yandex.MAX_CONSECUTIVE_FAILURES - 1):
            with self.assertRaises(yandex.YandexError):
                self._search(UPLOAD_OK, broken)
        self._search(UPLOAD_OK, _Resp(text=_page([])))
        with self.assertRaises(yandex.YandexError) as caught:
            self._search(UPLOAD_OK, broken)
        self.assertNotIsInstance(caught.exception, yandex.YandexBlockedError)

    def test_a_resting_yandex_says_so_on_the_row(self):
        """With no note, a row the other engines also found nothing for
        would be cached as a confident NOT_FOUND."""
        from core import engine_runner, yandex
        from core.config import Settings
        from core.models import ImageEntry
        with self.assertRaises(yandex.YandexBlockedError):
            self._search(CAPTCHA)
        errors = []
        engine_runner._collect_yandex(ImageEntry(path="/tmp/x.jpg"), Settings(), [], errors, None)
        self.assertIn("resting after a captcha", errors[0])
        self.assertTrue(engine_runner.retry_could_help(
            ImageEntry(path="/tmp/x.jpg", error_message="IQDB: timed out; " + errors[0])))
        self.assertFalse(engine_runner.retry_could_help(
            ImageEntry(path="/tmp/x.jpg", error_message=errors[0])))


class TestTakingPart(unittest.TestCase):
    def test_off_by_default(self):
        from core import engines
        from core.config import Settings
        from core.engine_runner import planned_engines
        self.assertNotIn(engines.YANDEX, planned_engines(Settings()))

    def test_an_extra_engine_when_on(self):
        from core import engines
        from core.config import Settings
        from core.engine_runner import _engine_waves
        s = Settings()
        s.enable_yandex = True
        self.assertIn(engines.YANDEX, _engine_waves(s, None)[0])
        s.extras_only_as_fallback = True
        self.assertNotIn(engines.YANDEX, _engine_waves(s, None)[0])
        self.assertIn(engines.YANDEX, _engine_waves(s, None)[-1])

    def test_its_score_is_ordinal(self):
        from core import engines
        self.assertFalse(engines.reports_real_similarity("Yandex"))


if __name__ == "__main__":
    unittest.main()
