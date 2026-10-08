"""URL handling and dead-link detection.

This area produced the most bugs in development, largely because sites
signal "gone" and "blocked" in ways that are easy to conflate.
"""
import unittest
from unittest.mock import MagicMock, patch

import requests

from . import _path  # noqa: F401

from core.config import Settings
from core.models import MatchCandidate
from core.hydrus_import import normalize_url_for_hydrus
from core.search_engine import (
    _page_says_available, check_url_available, cookies_for_url, referer_for_candidate,
)
from core.hydrus_import_poll import extract_confirmed_hash


def _resp(status, body=b""):
    def _get(*a, **k):
        r = MagicMock()
        r.status_code = status
        r.raw.read.return_value = body
        r.close = MagicMock()
        return r
    return _get


class TestAvailabilityDetection(unittest.TestCase):
    """The central rule: only a definite 404/410 counts as gone. Anything
    ambiguous must stay unknown, because dropping a match over a network
    hiccup is far worse than keeping a dead one."""

    def test_404_and_410_are_gone(self):
        for code in (404, 410):
            with patch("requests.Session.get", side_effect=_resp(code)):
                self.assertIs(check_url_available("https://x.test/1", 5.0), False)

    def test_success_is_available(self):
        for code in (200, 301, 302):
            with patch("requests.Session.get", side_effect=_resp(code)):
                self.assertIs(check_url_available("https://x.test/1", 5.0), True)

    def test_403_is_unknown_not_gone(self):
        """A 403 is usually hotlink protection or a bot check - the post
        is normally still there."""
        with patch("requests.Session.get", side_effect=_resp(403)):
            self.assertIsNone(check_url_available("https://x.test/1", 5.0))

    def test_server_errors_are_unknown(self):
        for code in (500, 502, 503):
            with patch("requests.Session.get", side_effect=_resp(code)):
                self.assertIsNone(check_url_available("https://x.test/1", 5.0))

    def test_network_failure_is_unknown(self):
        with patch("requests.Session.get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(check_url_available("https://x.test/1", 5.0))

    def test_missing_url_is_safe(self):
        self.assertIsNone(check_url_available(None, 5.0))


class TestStructurallyDeadUrls(unittest.TestCase):
    """Some URLs cannot resolve for anyone, and that is knowable from the
    URL alone - no request needed.

    Sankaku changed the shape of its post ids; the numeric ones every
    search engine still indexes stopped resolving. CONFIRMED against its
    API, which answers "invalid id" for each. Knowing this offline
    matters in two places a network check does not reach: an engine's
    result can be refused on the spot, and a result cached before this
    was understood can be cleaned up when replayed.
    """

    def test_a_numeric_sankaku_id_is_dead(self):
        from core.search_engine import url_is_structurally_dead
        for url in ("https://chan.sankakucomplex.com/en/posts/4704392",
                    "https://chan.sankakucomplex.com/en/posts/4541326",
                    "https://chan.sankakucomplex.com/post/show/371628"):
            with self.subTest(url=url):
                self.assertTrue(url_is_structurally_dead(url))

    def test_a_modern_sankaku_id_is_not(self):
        """It may still be gone, but only the API can say so."""
        from core.search_engine import url_is_structurally_dead
        self.assertFalse(url_is_structurally_dead(
            "https://www.sankakucomplex.com/posts/09azv5E4Dak"))

    def test_other_sites_are_never_called_dead(self):
        """Getting this wrong deletes good matches with no request made
        and nothing to notice it by."""
        from core.search_engine import url_is_structurally_dead
        for url in ("https://danbooru.donmai.us/posts/123",
                    "https://gelbooru.com/index.php?page=post&s=view&id=1",
                    "https://rule34.xxx/index.php?page=post&s=view&id=4585855",
                    "https://rule34.paheal.net/post/view/5674224", "", None):
            with self.subTest(url=url):
                self.assertFalse(url_is_structurally_dead(url))

    def test_such_a_match_never_becomes_a_candidate(self):
        from core.models import MatchCandidate
        from core.search_engine import _append_candidate
        candidates = []
        _append_candidate(candidates, "https://chan.sankakucomplex.com/en/posts/4704392",
                          "Sankaku Complex", None, 90.0, "IQDB", [])
        _append_candidate(candidates, "https://danbooru.donmai.us/posts/1",
                          "Danbooru", None, 88.0, "IQDB", [])
        self.assertEqual([c.url for c in candidates],
                         ["https://danbooru.donmai.us/posts/1"])
        self.assertTrue(all(isinstance(c, MatchCandidate) for c in candidates))

    def test_a_cached_result_is_cleaned_of_them(self):
        """REGRESSION: these were cached back when Sankaku's pages still
        answered, and a cache hit replays candidates without re-checking
        anything - so they kept arriving in the dropdown."""
        from core.models import MatchCandidate
        from core.search_engine import drop_structurally_dead
        cached = [
            MatchCandidate(url="https://chan.sankakucomplex.com/en/posts/4541326"),
            MatchCandidate(url="https://danbooru.donmai.us/posts/1"),
        ]
        self.assertEqual([c.url for c in drop_structurally_dead(cached, "x.png")],
                         ["https://danbooru.donmai.us/posts/1"])


class TestSankakuPostExists(unittest.TestCase):
    """Sankaku's page can no longer answer "is this post there".

    CONFIRMED against the live site: /en/posts/<id> redirects an
    anonymous visitor to login.sankakucomplex.com with HTTP 200, and
    www.sankakucomplex.com serves a JavaScript shell with no title. A
    dead post and a live one look identical, so every one of them was
    reported as available. Its API still answers, and says outright.
    """

    def _api(self, status_code, payload=None, exc=None):
        from unittest.mock import MagicMock, patch as _patch
        response = MagicMock(status_code=status_code)
        response.json.return_value = payload if payload is not None else {}
        return _patch("requests.Session.get", side_effect=exc) if exc else \
            _patch("requests.Session.get", return_value=response)

    def test_a_numeric_id_is_dead_whatever_became_of_the_post(self):
        """Those ids stopped resolving when Sankaku changed their format,
        so the URL cannot be opened by anyone."""
        from core.search_engine import sankaku_post_exists
        body = {"success": False, "code": "invalid id"}
        with self._api(400, body):
            self.assertIs(
                sankaku_post_exists("https://chan.sankakucomplex.com/en/posts/3934951", 5.0),
                False)

    def test_a_live_post_with_a_picture_is_available(self):
        from core.search_engine import sankaku_post_exists
        post = {"id": "y0abg167r2o", "status": "active",
                "file_url": "https://v.sankakucomplex.com/x.jpg"}
        with self._api(200, post):
            self.assertIs(
                sankaku_post_exists("https://www.sankakucomplex.com/posts/y0abg167r2o", 5.0),
                True)

    def test_a_post_whose_picture_is_withheld_counts_as_gone(self):
        """CONFIRMED on /posts/y0abg167r2o: status "active", but every
        file URL null and redirect_to_signup true. The post is there and
        its picture is not - nothing to fetch, nothing to look at."""
        from core.search_engine import sankaku_post_exists
        post = {"id": "y0abg167r2o", "status": "active", "file_url": None,
                "sample_url": None, "preview_url": None, "redirect_to_signup": True}
        with self._api(200, post):
            self.assertIs(
                sankaku_post_exists("https://www.sankakucomplex.com/posts/y0abg167r2o", 5.0),
                False)

    def test_a_deleted_status_counts_as_gone(self):
        from core.search_engine import sankaku_post_exists
        post = {"id": "abc", "status": "deleted",
                "file_url": "https://v.sankakucomplex.com/x.jpg"}
        with self._api(200, post):
            self.assertIs(
                sankaku_post_exists("https://www.sankakucomplex.com/posts/abc", 5.0), False)

    def test_a_network_failure_is_unknown_never_gone(self):
        """REGRESSION GUARD: calling a hiccup "deleted" throws away good
        matches, and drop_dead_matches would remove them for good."""
        import requests
        from core.search_engine import sankaku_post_exists
        with self._api(None, exc=requests.ConnectionError("no route")):
            self.assertIsNone(
                sankaku_post_exists("https://www.sankakucomplex.com/posts/abc", 5.0))

    def test_rate_limiting_is_unknown_too(self):
        from core.search_engine import sankaku_post_exists
        with self._api(429, {}):
            self.assertIsNone(
                sankaku_post_exists("https://www.sankakucomplex.com/posts/abc", 5.0))

    def test_other_sites_are_left_to_the_ordinary_check(self):
        """None means "not mine" - the caller then does what it always
        did, so nothing else changes behaviour."""
        from core.search_engine import sankaku_post_exists
        for url in ("https://danbooru.donmai.us/posts/1",
                    "https://gelbooru.com/index.php?page=post&s=view&id=1",
                    "https://rule34.paheal.net/post/view/5674224", ""):
            with self.subTest(url=url):
                self.assertIsNone(sankaku_post_exists(url, 5.0))

    def test_both_url_shapes_are_recognised(self):
        from core.search_engine import SANKAKU_POST_ID_RE
        cases = {
            "https://www.sankakucomplex.com/posts/y0abg167r2o": "y0abg167r2o",
            "https://chan.sankakucomplex.com/en/posts/3934951": "3934951",
            "https://chan.sankakucomplex.com/post/show/1234": "1234",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                found = SANKAKU_POST_ID_RE.search(url)
                self.assertIsNotNone(found, url)
                self.assertEqual(found.group(1), expected)


class TestSoftRedirectDetection(unittest.TestCase):
    """Some sites answer a missing post by redirecting to a generic
    "nothing here" page instead of returning 404, so the request finishes
    with a healthy HTTP 200 and a status-code check calls it alive.
    Sankaku lands those on /posts/show_empty."""

    def _get(self, status, final_url, body=b""):
        def _inner(*a, **k):
            resp = MagicMock()
            resp.status_code = status
            resp.url = final_url
            resp.raw.read.return_value = body
            resp.close = MagicMock()
            return resp
        return _inner

    def test_sankaku_empty_page_is_gone(self):
        post = "https://chan.sankakucomplex.com/en/posts/abc123"
        with patch("requests.Session.get",
                   side_effect=self._get(200, "https://chan.sankakucomplex.com/en/posts/show_empty")):
            self.assertIs(check_url_available(post, 5.0), False)

    def test_sankaku_not_found_redirect_on_another_subdomain(self):
        """CONFIRMED from a live check: a post showing nothing redirects
        to /errors/not_found on the WWW host, not the chan host the
        request went to. That page has no title and none of the "No
        Content" wording the logged-in browser view shows, so the body
        markers can't see it - the landing path is the only signal."""
        post = "https://chan.sankakucomplex.com/post/show/4762937"
        with patch("requests.Session.get",
                   side_effect=self._get(200, "https://www.sankakucomplex.com/errors/not_found")):
            self.assertIs(check_url_available(post, 5.0), False)

    def test_live_sankaku_post_survives_the_not_found_rule(self):
        post = "https://chan.sankakucomplex.com/post/show/4762937"
        with patch("requests.Session.get", side_effect=self._get(200, post)):
            self.assertIs(_page_says_available(post, 5.0), True)

    def test_live_sankaku_post_is_available(self):
        post = "https://chan.sankakucomplex.com/en/posts/abc123"
        with patch("requests.Session.get", side_effect=self._get(200, post)):
            self.assertIs(_page_says_available(post, 5.0), True)

    def test_matches_regardless_of_locale_prefix_or_trailing_slash(self):
        post = "https://chan.sankakucomplex.com/posts/abc123"
        for landing in ("https://chan.sankakucomplex.com/posts/show_empty",
                        "https://chan.sankakucomplex.com/en/posts/show_empty/",
                        "https://chan.sankakucomplex.com/ja/posts/show_empty"):
            with self.subTest(landing=landing):
                with patch("requests.Session.get", side_effect=self._get(200, landing)):
                    self.assertIs(check_url_available(post, 5.0), False)

    def test_rule_does_not_leak_to_other_sites(self):
        """The rule is keyed on the ORIGINAL host, so another site whose
        URL happens to contain the same path isn't judged by Sankaku's
        behaviour."""
        with patch("requests.Session.get",
                   side_effect=self._get(200, "https://danbooru.donmai.us/posts/show_empty")):
            self.assertIs(check_url_available("https://danbooru.donmai.us/posts/1", 5.0), True)

    def test_network_failure_still_unknown_for_sankaku(self):
        """REGRESSION GUARD: an unreachable site must never be confused
        with a deleted post, or a hiccup would silently delete matches."""
        with patch("requests.Session.get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(
                check_url_available("https://chan.sankakucomplex.com/en/posts/x", 5.0))

    def test_sankaku_is_enrolled_for_checking(self):
        """Sankaku's parser identifies posts by file hash, so it says
        nothing at all about a post whose file this app doesn't have a
        copy of - the availability check stays the only thing that would
        ever notice one of those had gone."""
        from core.search_engine import _should_availability_check
        self.assertTrue(_should_availability_check("https://chan.sankakucomplex.com/en/posts/x"))
        self.assertTrue(_should_availability_check("https://sankakucomplex.com/post/show/1"))

    def test_non_string_final_url_is_handled(self):
        with patch("requests.Session.get", side_effect=self._get(200, None)):
            self.assertIs(_page_says_available("https://chan.sankakucomplex.com/en/posts/x", 5.0), True)


class TestGelbooruDeletedPosts(unittest.TestCase):
    """VERIFIED against the live site: requesting a Gelbooru post that no
    longer exists is answered with a server-side redirect to the post
    LIST - HTTP 200, a perfectly valid page, full of thumbnails. Both a
    status check and any "does this look like a real page" heuristic call
    it alive. The only giveaway is that it stopped being a post view."""

    def _get(self, final_url, status=200):
        def _inner(*a, **k):
            resp = MagicMock()
            resp.status_code = status
            resp.url = final_url
            resp.raw.read.return_value = b""
            resp.close = MagicMock()
            return resp
        return _inner

    def test_deleted_post_redirecting_to_the_list_is_gone(self):
        dead = "https://gelbooru.com/index.php?page=post&s=view&id=4154084"
        with patch("requests.Session.get",
                   side_effect=self._get("https://gelbooru.com/index.php?page=post&s=list&tags=all")):
            self.assertIs(check_url_available(dead, 5.0), False)

    def test_live_post_is_available(self):
        live = "https://gelbooru.com/index.php?page=post&s=view&id=14748461"
        with patch("requests.Session.get", side_effect=self._get(live)):
            self.assertIs(check_url_available(live, 5.0), True)

    def test_a_list_url_does_not_report_itself_gone(self):
        """REGRESSION GUARD: the marker is "s=list", which a list URL
        contains from the start. Without requiring that the fragment
        appear only in the DESTINATION, such a URL would flag itself."""
        listing = "https://gelbooru.com/index.php?page=post&s=list&tags=all"
        with patch("requests.Session.get", side_effect=self._get(listing)):
            self.assertIs(check_url_available(listing, 5.0), True)

    def test_safebooru_behaves_the_same(self):
        dead = "https://safebooru.org/index.php?page=post&s=view&id=1"
        with patch("requests.Session.get",
                   side_effect=self._get("https://safebooru.org/index.php?page=post&s=list&tags=all")):
            self.assertIs(check_url_available(dead, 5.0), False)

    def test_network_failure_is_still_unknown(self):
        with patch("requests.Session.get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(check_url_available(
                "https://gelbooru.com/index.php?page=post&s=view&id=1", 5.0))


# Trimmed from the ACTUAL page source of an empty Sankaku post, captured
# while logged in. The redirect to /posts/show_empty happens in the
# browser after delivery, so the response itself keeps the requested URL
# and returns HTTP 200 - the body is the only evidence there is.
SANKAKU_EMPTY_PAGE = b"""<!DOCTYPE html>
<html lang="en" data-current-user-theme="dark">
  <head>
    <title>No Content | Sankaku</title>
    <link rel="icon" href="//chan.sankakucomplex.com/images/favicon.png">
    <link rel="alternate" hreflang="en" href="https://chan.sankakucomplex.com/en/posts/show_empty" />
    <link rel="alternate" hreflang="ja" href="https://chan.sankakucomplex.com/ja/en/posts/show_empty" />
  </head>
  <body class="en">
    <h2 id="page-title"><a href="/">Sankaku</a>/<a href="https://chan.sankakucomplex.com/en/posts/show_empty">No Content</a></h2>
    <div><h3>Nothing is visible to you here.</h3></div>
  </body>
</html>"""

SANKAKU_LIVE_PAGE = b"""<!DOCTYPE html><html><head>
<title>rainbow dash spitfire | Sankaku</title>
<link rel="alternate" hreflang="en" href="https://chan.sankakucomplex.com/en/posts/5960112" />
</head><body><div id="post-content">
<img src="//is.sankakucomplex.com/data/ab/cd/abcd.jpg"></div></body></html>"""


class TestSankakuEmptyBody(unittest.TestCase):
    """Sankaku keeps the requested URL and answers HTTP 200 even when the
    post shows nothing, so neither the status code nor the landing URL
    reveals anything - the markers below are taken from the real page."""

    URL = "https://chan.sankakucomplex.com/post/show/5960112"

    def _get(self, body, url=None):
        def _inner(*a, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = url or self.URL
            resp.raw.read.return_value = body
            resp.close = MagicMock()
            return resp
        return _inner

    def test_real_empty_page_is_detected(self):
        with patch("requests.Session.get", side_effect=self._get(SANKAKU_EMPTY_PAGE)):
            self.assertIs(check_url_available(self.URL, 5.0), False)

    def test_real_post_page_is_not_flagged(self):
        """A live post's hreflang links carry its own id and its title is
        the post's - neither can collide with the empty page's markers."""
        with patch("requests.Session.get", side_effect=self._get(SANKAKU_LIVE_PAGE)):
            self.assertIs(_page_says_available(self.URL, 5.0), True)

    def test_head_alone_is_enough(self):
        """Two of the three markers are in <head>, so detection doesn't
        depend on the scan reaching past the site's large navigation and
        news carousel."""
        # Cut at </head> so this genuinely tests head-only detection.
        # (A fixed byte count would be wrong: the fixture here is far
        # shorter than the real page, where the <h3> sits after the whole
        # navigation and news carousel.)
        head_only = SANKAKU_EMPTY_PAGE[:SANKAKU_EMPTY_PAGE.index(b"</head>")]
        self.assertNotIn(b"Nothing is visible", head_only)
        with patch("requests.Session.get", side_effect=self._get(head_only)):
            self.assertIs(check_url_available(self.URL, 5.0), False)

    def test_markers_appear_early_in_the_document(self):
        """The body scan is bounded; a marker beyond that limit would
        never be seen on a page this heavy."""
        from core.search_engine import SOFT_404_MARKERS, SOFT_404_SCAN_BYTES
        lowered = SANKAKU_EMPTY_PAGE.decode().lower()
        for marker in SOFT_404_MARKERS["sankakucomplex.com"][:2]:
            with self.subTest(marker=marker):
                position = lowered.find(marker)
                self.assertNotEqual(position, -1)
                self.assertLess(position, SOFT_404_SCAN_BYTES)

    def test_unrecognised_body_leaves_the_match_alone(self):
        """Failure mode stays "no improvement", never "drops good
        matches"."""
        with patch("requests.Session.get", side_effect=self._get(b"<html>something else</html>")):
            self.assertIs(_page_says_available(self.URL, 5.0), True)


class TestAvailabilityLoggingIsDiagnosable(unittest.TestCase):
    """The "available" outcome used to log nothing, so a check that
    passed and a check that never really ran were indistinguishable in
    the log - which made "why wasn't this match dropped?" impossible to
    answer from a log alone."""

    def _get(self, body, url):
        def _inner(*a, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = url
            resp.raw.read.return_value = body
            resp.close = MagicMock()
            return resp
        return _inner

    def test_success_says_the_body_was_scanned(self):
        url = "https://chan.sankakucomplex.com/post/show/4762937"
        with self.assertLogs("hatate.search", level="DEBUG") as captured:
            with patch("requests.Session.get", side_effect=self._get(b"<title>art | Sankaku</title>", url)):
                self.assertIs(_page_says_available(url, 5.0), True)
        joined = "\n".join(captured.output)
        self.assertIn("scanned", joined)
        self.assertIn("marker", joined)

    def test_marker_miss_reports_the_page_title(self):
        """A marker miss has two very different causes - the page really
        is a live post, or something else was served (bot check, age
        gate, login wall). Without the title they're indistinguishable
        in a log, which makes "why wasn't this dropped?" unanswerable."""
        url = "https://chan.sankakucomplex.com/post/show/4762937"
        with self.assertLogs("hatate.search", level="DEBUG") as captured:
            with patch("requests.Session.get",
                       side_effect=self._get(b"<html><head><title>Just a moment...</title>", url)):
                self.assertIs(_page_says_available(url, 5.0), True)
        self.assertIn("Just a moment", "\n".join(captured.output))

    def test_marker_miss_reports_a_body_that_is_not_html(self):
        """An undecoded or binary response has no title at all, which is
        itself the diagnosis."""
        from core.search_engine import _page_title
        self.assertIn("no title", _page_title("\x1f\x8b\x08\x00binary"))

    def test_success_says_when_a_host_has_no_markers(self):
        url = "https://danbooru.donmai.us/posts/1"
        with self.assertLogs("hatate.search", level="DEBUG") as captured:
            with patch("requests.Session.get", side_effect=self._get(b"", url)):
                self.assertIs(check_url_available(url, 5.0), True)
        self.assertIn("no body markers", "\n".join(captured.output))


class TestCookiePaste(unittest.TestCase):
    """Cookies are pasted by hand out of a browser's cookie panel, so the
    parser has to cope with how that text actually arrives - and say
    something when a paste plainly went wrong, because a bad session
    cookie fails silently: requests succeed, nothing errors, and every
    account-only post just keeps looking deleted."""

    # Shaped like the real thing: a long percent-encoded session value
    # plus the display-preference cookies Sankaku sets alongside it.
    SESSION_VALUE = "u2uE0z3bhSvINqZNshdcgIHQwE61Cz%2BYUfEixCnNaF4dNHAj" + "A" * 570
    REAL_PASTE = (
        "_sankakuchannel_session=" + SESSION_VALUE + "; "
        "c_ins_pos=%7B%22top%22%3A108%2C%22left%22%3A104%7D; "
        "hide_ci=1; locale=en; mode=view; theme=1; v=0"
    )

    def test_real_paste_parses_completely(self):
        from core.search_engine import parse_cookie_string
        cookies = parse_cookie_string(self.REAL_PASTE)
        self.assertEqual(
            set(cookies),
            {"_sankakuchannel_session", "c_ins_pos", "hide_ci", "locale", "mode", "theme", "v"},
        )
        self.assertEqual(cookies["_sankakuchannel_session"], self.SESSION_VALUE)

    def test_percent_encoding_is_left_alone(self):
        """The browser stores the encoded form and the site expects it
        back unchanged - decoding here would corrupt the session."""
        from core.search_engine import parse_cookie_string
        cookies = parse_cookie_string("_sankakuchannel_session=u2uE%2BYUf%3D%3D")
        self.assertEqual(cookies["_sankakuchannel_session"], "u2uE%2BYUf%3D%3D")

    def test_newline_separated_paste(self):
        """REGRESSION GUARD: selecting several rows in a cookie panel
        gives one pair per LINE, not the semicolon-joined header form.
        Splitting on ";" alone turned that into a single cookie with the
        rest of the pairs buried in its value."""
        from core.search_engine import parse_cookie_string
        cookies = parse_cookie_string("_sankakuchannel_session=abc\nlocale=en\ntheme=1")
        self.assertEqual(cookies, {"_sankakuchannel_session": "abc", "locale": "en", "theme": "1"})

    def test_crlf_and_mixed_separators(self):
        from core.search_engine import parse_cookie_string
        cookies = parse_cookie_string("_sankakuchannel_session=abc;\r\n locale=en \n theme=1")
        self.assertEqual(cookies, {"_sankakuchannel_session": "abc", "locale": "en", "theme": "1"})

    def test_clean_paste_produces_no_warnings(self):
        from core.search_engine import cookie_paste_warnings
        self.assertEqual(cookie_paste_warnings(self.REAL_PASTE), [])

    def test_truncated_value_is_flagged(self):
        """The panel truncates long values on screen, so selecting the
        displayed text copies an ellipsis and a cut-off session."""
        from core.search_engine import cookie_paste_warnings
        warnings = cookie_paste_warnings("_sankakuchannel_session=u2uE0z3bhSvINqZ\u2026")
        self.assertTrue(any("truncated" in w for w in warnings))

    def test_missing_session_cookie_is_flagged(self):
        from core.search_engine import cookie_paste_warnings
        warnings = cookie_paste_warnings("locale=en; theme=1; mode=view")
        self.assertTrue(any("session cookie" in w for w in warnings))

    def test_short_session_value_is_flagged(self):
        from core.search_engine import cookie_paste_warnings
        warnings = cookie_paste_warnings("_sankakuchannel_session=abc123")
        self.assertTrue(any("short" in w for w in warnings))

    def test_warnings_never_block_anything(self):
        """Advisory only - an unfamiliar layout far more likely means the
        site changed than that the user got it wrong, so the cookies are
        still sent."""
        from core.search_engine import cookies_for_url
        settings = Settings()
        settings.sankaku_cookies = "locale=en"
        self.assertEqual(
            cookies_for_url("https://chan.sankakucomplex.com/post/show/1", settings),
            {"locale": "en"},
        )


# Trimmed from the ACTUAL pages of a deleted Moebooru post
# (yande.re/post/show/516284) and the live post beside it. Both return
# HTTP 200 at the requested URL and list full tags and dimensions - a
# deleted post keeps its whole page. Only the sentence differs.
YANDERE_DELETED = (
    b"<html><head><title>... | #516284 | yande.re</title>"
    b'<meta property="og:image" content="https://assets.yande.re/assets/stubs/explicit-caa.png">'
    b"</head><body>"
    b"This user name doesn't exist. If you want to create a new account, just verify..."
    b"<div>This post was deleted. Reason: . MD5: ffa93b4481cfbaffdee9406bd572d20b</div>"
    b"<div>Size: 4960x3507</div></body></html>"
)

YANDERE_LIVE = (
    b"<html><head><title>slyvia | #516285 | yande.re</title>"
    b'<meta property="og:image" content="https://files.yande.re/sample/7748/sample.jpg">'
    b"</head><body>"
    b"This user name doesn't exist. If you want to create a new account, just verify..."
    b"<div>This image has been resized. Click on the View larger version link...</div>"
    b"<div>Size: 2700x4800</div></body></html>"
)


class TestMoebooruDeletedPosts(unittest.TestCase):
    """A deleted Yande.re/Konachan post keeps its entire page - every
    tag, its dimensions, HTTP 200 at the requested URL - and differs
    only by one sentence. Nothing structural gives it away."""

    def _get(self, body, url):
        def _inner(*a, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = url
            resp.headers = {"Content-Type": "text/html"}
            resp.raw.read.return_value = body
            resp.close = MagicMock()
            return resp
        return _inner

    def test_deleted_post_is_detected(self):
        url = "https://yande.re/post/show/516284"
        with patch("requests.Session.get", side_effect=self._get(YANDERE_DELETED, url)):
            self.assertIs(check_url_available(url, 5.0), False)

    def test_live_post_is_not_condemned_by_the_hidden_login_template(self):
        """REGRESSION GUARD: every Moebooru page renders a block of hidden
        template strings ("This user name doesn't exist...") whether or
        not they apply. A marker drawn from that block would report every
        Moebooru match as deleted - verified against a real live post
        that it does NOT contain the deletion sentence."""
        url = "https://yande.re/post/show/516285"
        with patch("requests.Session.get", side_effect=self._get(YANDERE_LIVE, url)):
            self.assertIs(check_url_available(url, 5.0), True)

    def test_the_placeholder_image_is_a_second_signal(self):
        """A deleted post shows a stub image where the live one links the
        real file. Independent of the wording, so a rephrasing on the
        site's side doesn't take detection with it."""
        url = "https://yande.re/post/show/516284"
        body = YANDERE_DELETED.replace(b"This post was deleted.", b"")
        self.assertNotIn(b"This post was deleted", body)
        with patch("requests.Session.get", side_effect=self._get(body, url)):
            self.assertIs(check_url_available(url, 5.0), False)

    def test_both_signals_are_actually_registered(self):
        """REGRESSION GUARD: these markers already existed but sat behind
        a DUPLICATE dict key, so the later entry silently replaced them.
        Worse, the hosts weren't enrolled for availability checking at
        all - the markers were never consulted either way."""
        from core.search_engine import SOFT_404_MARKERS
        for host in ("yande.re", "konachan.com", "konachan.net"):
            with self.subTest(host=host):
                markers = SOFT_404_MARKERS[host]
                self.assertIn("this post was deleted", markers)
                self.assertIn("/assets/stubs/", markers)

    def test_moebooru_hosts_are_enrolled_for_checking(self):
        from core.search_engine import _should_availability_check
        for url in ("https://yande.re/post/show/1",
                    "https://konachan.com/post/show/1",
                    "https://konachan.net/post/show/1"):
            with self.subTest(url=url):
                self.assertTrue(_should_availability_check(url))

    def test_network_failure_is_still_unknown(self):
        with patch("requests.Session.get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(check_url_available("https://yande.re/post/show/1", 5.0))


class TestSankakuLegacyUrlRewrite(unittest.TestCase):
    """SauceNAO's index still hands out Sankaku's deprecated
    /post/show/{id} route, which Sankaku no longer maps - following it
    lands on the browse index instead of the post. The link is broken by
    FORMAT, independently of whether the post exists."""

    def test_legacy_route_is_rewritten(self):
        from core.sites import canonicalize_url
        self.assertEqual(
            canonicalize_url("https://chan.sankakucomplex.com/post/show/4100422"),
            "https://chan.sankakucomplex.com/en/posts/4100422")

    def test_query_string_is_dropped_with_the_old_route(self):
        from core.sites import canonicalize_url
        self.assertEqual(
            canonicalize_url("https://chan.sankakucomplex.com/post/show/4100422?tags=x"),
            "https://chan.sankakucomplex.com/en/posts/4100422")

    def test_modern_route_is_untouched(self):
        from core.sites import canonicalize_url
        url = "https://chan.sankakucomplex.com/en/posts/4100422"
        self.assertEqual(canonicalize_url(url), url)

    def test_other_sites_are_untouched(self):
        """Danbooru also serves /post/show/{id} and handles it fine -
        rewriting URLs for sites that don't need it would be meddling."""
        from core.sites import canonicalize_url
        for url in ("https://danbooru.donmai.us/post/show/2577264",
                    "https://gelbooru.com/index.php?page=post&s=view&id=1",
                    "https://yande.re/post/show/123"):
            with self.subTest(url=url):
                self.assertEqual(canonicalize_url(url), url)

    def test_empty_and_junk_are_safe(self):
        from core.sites import canonicalize_url
        self.assertEqual(canonicalize_url(""), "")
        self.assertEqual(canonicalize_url("not a url"), "not a url")


class TestReactorFullResUrlRewrite(unittest.TestCase):
    """JoyReactor/reactor.cc serve the watermarked copy of a post's file
    by default; the unwatermarked, full-quality original is the same
    path with a "full/" segment inserted. CONFIRMED live pair used
    throughout: img10.reactor.cc/pics/post/egoswans-... (watermarked) vs
    .../pics/post/full/egoswans-... (clean)."""

    LOW = ("https://img10.reactor.cc/pics/post/egoswans-Rebecca-(Edgerunners)-"
           "Cyberpunk-Edgerunners-Cyberpunk-2077-8192103.jpeg")
    HIGH = ("https://img10.reactor.cc/pics/post/full/egoswans-Rebecca-(Edgerunners)-"
            "Cyberpunk-Edgerunners-Cyberpunk-2077-8192103.jpeg")

    def test_reactor_cc_is_rewritten_to_full(self):
        from core.sites import canonicalize_url
        self.assertEqual(canonicalize_url(self.LOW), self.HIGH)

    def test_joyreactor_com_host_is_also_rewritten(self):
        from core.sites import canonicalize_url
        self.assertEqual(
            canonicalize_url("https://joyreactor.com/pics/post/a-1234.jpg"),
            "https://joyreactor.com/pics/post/full/a-1234.jpg")

    def test_joyreactor_cc_host_is_also_rewritten(self):
        from core.sites import canonicalize_url
        self.assertEqual(
            canonicalize_url("https://joyreactor.cc/pics/post/a-1234.jpg"),
            "https://joyreactor.cc/pics/post/full/a-1234.jpg")

    def test_already_full_is_left_alone(self):
        from core.sites import canonicalize_url
        self.assertEqual(canonicalize_url(self.HIGH), self.HIGH)

    def test_non_pics_post_path_is_left_alone(self):
        from core.sites import canonicalize_url
        url = "https://reactor.cc/post/1234567"
        self.assertEqual(canonicalize_url(url), url)

    def test_unrelated_host_with_a_similar_path_is_left_alone(self):
        """The path shape alone isn't enough - only a reactor.cc/
        joyreactor.com/joyreactor.cc host gets rewritten, exactly as
        canonicalize_url leaves any other unrecognised URL untouched."""
        from core.sites import canonicalize_url
        url = "https://example.com/pics/post/a-1234.jpg"
        self.assertEqual(canonicalize_url(url), url)


class TestReactorFullResFallback(unittest.TestCase):
    """canonicalize_url rewrites a JoyReactor/reactor.cc match to its
    full/ file before anyone has confirmed that file exists. If the
    availability check then finds it definitively gone (404/410), the
    fallback here hands the candidate back its plain (watermarked) URL
    instead of reporting the whole match dead over a rewrite that didn't
    pan out - the graceful-fallback half of the full-res rewrite."""

    FULL = "https://img10.reactor.cc/pics/post/full/egoswans-8192103.jpeg"
    PLAIN = "https://img10.reactor.cc/pics/post/egoswans-8192103.jpeg"

    def test_a_confirmed_dead_full_url_falls_back_to_plain(self):
        from core.availability import _reactor_full_fallback
        candidate = MatchCandidate(url=self.FULL)
        result = _reactor_full_fallback(candidate, False)
        self.assertIsNone(result, "unknown - the fallback URL itself hasn't been checked")
        self.assertEqual(candidate.url, self.PLAIN)

    def test_available_is_left_alone(self):
        from core.availability import _reactor_full_fallback
        candidate = MatchCandidate(url=self.FULL)
        self.assertTrue(_reactor_full_fallback(candidate, True))
        self.assertEqual(candidate.url, self.FULL, "no fallback needed - nothing to undo")

    def test_unknown_is_left_alone(self):
        from core.availability import _reactor_full_fallback
        candidate = MatchCandidate(url=self.FULL)
        self.assertIsNone(_reactor_full_fallback(candidate, None))
        self.assertEqual(candidate.url, self.FULL)

    def test_a_dead_non_reactor_candidate_is_reported_dead_as_normal(self):
        from core.availability import _reactor_full_fallback
        candidate = MatchCandidate(url="https://danbooru.donmai.us/posts/1")
        self.assertFalse(_reactor_full_fallback(candidate, False))
        self.assertEqual(candidate.url, "https://danbooru.donmai.us/posts/1")

    def test_a_dead_plain_reactor_url_is_reported_dead_as_normal(self):
        """Only a full/ URL gets the fallback - a plain URL that's
        actually gone (never rewritten, or already the fallback) has
        nothing lower to fall back to."""
        from core.availability import _reactor_full_fallback
        candidate = MatchCandidate(url=self.PLAIN)
        self.assertFalse(_reactor_full_fallback(candidate, False))
        self.assertEqual(candidate.url, self.PLAIN)


class TestIndexRedirectDetection(unittest.TestCase):
    """Being bounced to a site's browse index means the request never
    reached a post."""

    LEGACY = "https://chan.sankakucomplex.com/post/show/4100422"

    def test_bounce_to_index_is_detected(self):
        from core.search_engine import _redirect_to_index
        for landing in ("https://chan.sankakucomplex.com/en/posts",
                        "https://chan.sankakucomplex.com/en/posts/",
                        "https://chan.sankakucomplex.com/posts",
                        "https://chan.sankakucomplex.com/"):
            with self.subTest(landing=landing):
                self.assertIsNotNone(_redirect_to_index(self.LEGACY, landing))

    def test_a_real_post_is_never_flagged(self):
        """REGRESSION GUARD: the index is "/en/posts" and a post is
        "/en/posts/4100422". A substring test would report every
        successful post load as gone - failing in the most destructive
        direction possible, silently deleting good matches."""
        from core.search_engine import _redirect_to_index
        self.assertIsNone(_redirect_to_index(
            self.LEGACY, "https://chan.sankakucomplex.com/en/posts/4100422"))

    def test_rule_is_scoped_to_the_originating_host(self):
        from core.search_engine import _redirect_to_index
        self.assertIsNone(_redirect_to_index(
            "https://danbooru.donmai.us/posts/1", "https://danbooru.donmai.us/posts"))

    def test_index_bounce_is_unverifiable_rather_than_gone(self):
        """REGRESSION: this used to return False, i.e. "deleted", and the
        match was dropped. Sankaku now bounces EVERY post URL - id 100 and
        id 38,000,000 alike - to a login page or its index, because the
        legacy site sits behind SSO. Reading that as deletion deleted
        every Sankaku match regardless of whether it still existed.

        None means unverifiable, which is what the caller needs to hear:
        it keeps the match rather than discarding it on no evidence."""
        with patch("requests.Session.get", side_effect=self._get_index()):
            self.assertIsNone(_page_says_available(self.LEGACY, 5.0))

    def _get_index(self):
        def _inner(*a, **k):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = "https://chan.sankakucomplex.com/en/posts"
            resp.headers = {"Content-Type": "text/html"}
            resp.raw.read.return_value = b""
            resp.close = MagicMock()
            return resp
        return _inner


class TestSankakuAuthenticatedChecks(unittest.TestCase):
    """Sankaku hides adult and account-only posts from anonymous
    requests by redirecting them to the same empty page it uses for
    deleted ones. So without login cookies a perfectly good post is
    indistinguishable from a dead one - and gets dropped."""

    def _settings(self, cookies=""):
        settings = Settings()
        settings.sankaku_cookies = cookies
        return settings

    def _check_one(self):
        """The worker's per-candidate logic, lifted out so it runs
        without a Qt event loop."""
        import ast
        from core.hydrus_import import normalize_url_for_hydrus
        from core.models import MatchCandidate
        from core.search_engine import (
            cookies_for_url, has_soft_404_detection, referer_for_candidate,
        )
        src = open("workers/availability_worker.py").read()
        cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef))
        fn = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_check_one")
        self.captured = {}

        def fake_check(target, timeout, referer=None, cookies=None, **kwargs):
            self.captured["target"] = target
            self.captured["cookies"] = cookies
            return True

        ns = {
            "normalize_url_for_hydrus": normalize_url_for_hydrus,
            "has_soft_404_detection": has_soft_404_detection,
            "referer_for_candidate": referer_for_candidate,
            "cookies_for_url": cookies_for_url,
            "check_url_available": fake_check,
            "MatchCandidate": MatchCandidate,
        }
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
        return ns["_check_one"]

    def test_cookies_are_parsed_from_a_pasted_string(self):
        from core.search_engine import cookies_for_url
        settings = self._settings("login=myname; pass_hash=abc; sess=eyJhbGc=OiJI")
        got = cookies_for_url("https://chan.sankakucomplex.com/en/posts/x", settings)
        self.assertEqual(got["login"], "myname")
        self.assertEqual(got["sess"], "eyJhbGc=OiJI",
                         "values containing '=' (JWTs, base64) must survive intact")

    def test_cookies_are_not_sent_to_other_sites(self):
        from core.search_engine import cookies_for_url
        settings = self._settings("login=myname; pass_hash=abc")
        self.assertIsNone(cookies_for_url("https://danbooru.donmai.us/posts/1", settings))

    def test_manual_check_sends_cookies(self):
        """REGRESSION GUARD: the search-time sweep authenticated but the
        manual "Check Match Availability" action did not, so running it
        by hand deleted the very posts a search had just kept."""
        from core.models import MatchCandidate
        check_one = self._check_one()
        candidate = MatchCandidate(url="https://chan.sankakucomplex.com/en/posts/abc")
        worker = type("W", (), {"settings": self._settings("login=myname; pass_hash=abc")})()
        check_one(worker, candidate)
        self.assertIsNotNone(self.captured["cookies"])
        self.assertEqual(self.captured["cookies"]["login"], "myname")

    def test_manual_check_sends_nothing_when_unconfigured(self):
        from core.models import MatchCandidate
        check_one = self._check_one()
        candidate = MatchCandidate(url="https://chan.sankakucomplex.com/en/posts/abc")
        check_one(type("W", (), {"settings": self._settings()})(), candidate)
        self.assertIsNone(self.captured["cookies"])

    def test_sankaku_is_checked_on_its_post_page(self):
        """The redirect only happens on the post page - a file URL would
        just fail ambiguously and report "unknown"."""
        from core.search_engine import has_soft_404_detection
        self.assertTrue(has_soft_404_detection("https://chan.sankakucomplex.com/en/posts/x"))
        self.assertTrue(has_soft_404_detection("https://www.pixiv.net/artworks/1"))
        self.assertFalse(has_soft_404_detection("https://danbooru.donmai.us/posts/1"))


class TestPixivUrlNormalisation(unittest.TestCase):
    def test_legacy_url_rewritten_to_modern(self):
        """REGRESSION: Hydrus's downloader silently failed on Pixiv's
        legacy member_illust.php URLs, so imports never confirmed and
        entries were never removed from the list."""
        self.assertEqual(
            normalize_url_for_hydrus(
                "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=112526890"),
            "https://www.pixiv.net/artworks/112526890",
        )

    def test_modern_url_untouched(self):
        url = "https://www.pixiv.net/artworks/112526890"
        self.assertEqual(normalize_url_for_hydrus(url), url)

    def test_non_pixiv_untouched(self):
        # Not danbooru's /post/show/: that legacy form IS rewritten, since
        # Hydrus only parses /posts/ (see _HYDRUS_URL_REWRITES).
        url = "https://gelbooru.com/index.php?page=post&s=view&id=123"
        self.assertEqual(normalize_url_for_hydrus(url), url)


class TestRule34UsUrlNormalisation(unittest.TestCase):
    """REGRESSION (DAN-67/DAN-68): Hydrus's rule34.us url class matches
    r=posts/view but not the percent-encoded r=posts%2Fview that Google
    Lens's "Exact matches" tab can hand back, so the file never imported
    and never confirmed."""

    def test_encoded_slash_is_decoded(self):
        self.assertEqual(
            normalize_url_for_hydrus("https://rule34.us/index.php?r=posts%2Fview&id=12345"),
            "https://rule34.us/index.php?r=posts/view&id=12345",
        )

    def test_lowercase_encoded_slash_is_decoded(self):
        self.assertEqual(
            normalize_url_for_hydrus("https://rule34.us/index.php?r=posts%2fview&id=12345"),
            "https://rule34.us/index.php?r=posts/view&id=12345",
        )

    def test_already_decoded_url_untouched(self):
        url = "https://rule34.us/index.php?r=posts/view&id=12345"
        self.assertEqual(normalize_url_for_hydrus(url), url)

    def test_other_params_and_hosts_untouched(self):
        # Substring "rule34.us" appears in the host, but it isn't rule34.us
        # or a subdomain of it - must not be touched.
        url = "https://notrule34.us/index.php?r=posts%2Fview&id=1"
        self.assertEqual(normalize_url_for_hydrus(url), url)

    def test_non_rule34_url_with_encoded_slash_untouched(self):
        url = "https://gelbooru.com/index.php?r=posts%2Fview&id=1"
        self.assertEqual(normalize_url_for_hydrus(url), url)


class TestReferer(unittest.TestCase):
    def test_pixiv_uses_bare_origin(self):
        """Pixiv's CDN rejects the specific artwork page as a Referer;
        it wants the bare origin."""
        c = MatchCandidate(url="https://www.pixiv.net/artworks/1",
                           preview_url="https://i.pximg.net/img-master/x.jpg")
        self.assertEqual(referer_for_candidate(c, c.preview_url), "https://www.pixiv.net/")

    def test_other_sites_use_their_post_page(self):
        c = MatchCandidate(url="https://gelbooru.com/index.php?id=1",
                           preview_url="https://img3.gelbooru.com/s.jpg")
        self.assertEqual(referer_for_candidate(c, c.preview_url), c.url)

    def test_no_referer_for_unrelated_host(self):
        """Never send a booru Referer to the search engine's own CDN."""
        c = MatchCandidate(url="https://danbooru.donmai.us/posts/1",
                           thumb_url="https://img.saucenao.com/t.jpg")
        self.assertIsNone(referer_for_candidate(c, c.thumb_url))


class TestPixivCookieScoping(unittest.TestCase):
    def test_cookie_sent_only_to_pixiv(self):
        """A session cookie is a credential - it must never leak to any
        other host."""
        s = Settings()
        s.pixiv_session_cookie = "  SESSIONVALUE  "
        self.assertEqual(
            cookies_for_url("https://www.pixiv.net/artworks/1", s),
            {"PHPSESSID": "SESSIONVALUE"},          # also trims whitespace
        )
        for other in ("https://danbooru.donmai.us/posts/1",
                      "https://deviantart.com/view/1",
                      "https://img3.saucenao.com/x.jpg"):
            self.assertIsNone(cookies_for_url(other, s), f"cookie leaked to {other}")

    def test_nothing_sent_when_unconfigured(self):
        self.assertIsNone(cookies_for_url("https://www.pixiv.net/artworks/1", Settings()))


class TestHydrusImportConfirmation(unittest.TestCase):
    def test_empty_status_list_means_still_in_progress(self):
        self.assertIsNone(extract_confirmed_hash({"url_file_statuses": []}))

    def test_status_2_is_confirmed(self):
        self.assertEqual(
            extract_confirmed_hash({"url_file_statuses": [{"status": 2, "hash": "abc"}]}),
            "abc",
        )

    def test_status_0_is_not_confirmed(self):
        """Status 0 is a documented transient state, not a finished import."""
        self.assertIsNone(
            extract_confirmed_hash({"url_file_statuses": [{"status": 0, "hash": None}]}))

    def test_malformed_response_is_safe(self):
        self.assertIsNone(extract_confirmed_hash({}))


if __name__ == "__main__":
    unittest.main()


class TestAvailabilityChecksUseSiteHeaders(unittest.TestCase):
    """REGRESSION: Sankaku answers the same post URL differently depending
    on the User-Agent.

    With this project's descriptive one it bounces every post to the browse
    index - which says nothing - while a browser's gets the real "No
    Content" page for a post it won't show. Confirmed repeatedly against a
    live logged-in session:

        app UA      -> /posts                 (no information)
        browser UA  -> /en/posts/show_empty   ("No Content")

    So the check could not see the answer, and dead Sankaku matches
    survived as unverifiable - a link that opens on "nothing is visible to
    you here".
    """

    def _capture(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured["headers"] = kwargs.get("headers") or {}
            resp = MagicMock()
            resp.status_code = 200
            resp.url = url
            resp.headers = {"Content-Type": "text/html"}
            resp.raw.read.return_value = b""
            resp.close = MagicMock()
            return resp
        return captured, fake_get

    def test_sankaku_is_checked_with_a_browser_user_agent(self):
        captured, fake_get = self._capture()
        with patch("requests.Session.get", side_effect=fake_get):
            _page_says_available("https://chan.sankakucomplex.com/en/posts/3797441", 5.0)
        self.assertIn("Mozilla/5.0", captured["headers"].get("User-Agent", ""))

    def test_other_sites_keep_the_honest_descriptive_agent(self):
        """The browser UA is a workaround for one site's behaviour, not a
        licence to misrepresent this app everywhere."""
        from core.search_engine import USER_AGENT
        captured, fake_get = self._capture()
        with patch("requests.Session.get", side_effect=fake_get):
            check_url_available("https://danbooru.donmai.us/posts/1", 5.0)
        self.assertEqual(captured["headers"].get("User-Agent"), USER_AGENT)

    def test_an_explicit_referer_still_wins(self):
        captured, fake_get = self._capture()
        with patch("requests.Session.get", side_effect=fake_get):
            _page_says_available("https://chan.sankakucomplex.com/en/posts/1", 5.0,
                                referer="https://example.test/from")
        self.assertEqual(captured["headers"].get("Referer"), "https://example.test/from")

    def test_headers_for_url_covers_the_right_sites(self):
        from core.search_engine import headers_for_url
        self.assertIn("Mozilla/5.0",
                      (headers_for_url("https://chan.sankakucomplex.com/en/posts/1") or {})
                      .get("User-Agent", ""))
        self.assertIn("Mozilla/5.0",
                      (headers_for_url("https://www.pixiv.net/artworks/1") or {})
                      .get("User-Agent", ""))
        self.assertIn("Mozilla/5.0",
                      (headers_for_url("https://www.deviantart.com/artist/art/title-1") or {})
                      .get("User-Agent", ""))
        self.assertIn("Mozilla/5.0",
                      (headers_for_url("https://www.fav.me/d123456") or {})
                      .get("User-Agent", ""))
        self.assertIsNone(headers_for_url("https://danbooru.donmai.us/posts/1"))
        self.assertIsNone(headers_for_url(""))

    def test_deviantart_is_fetched_with_a_browser_user_agent(self):
        """DeviantArt's pasted session cookies were issued to a browser,
        not to this app's own descriptive UA - replaying them under a
        mismatched identity is a plausible reason a pasted session dies
        far sooner than the cookie's own stated lifetime (DAN-122)."""
        captured, fake_get = self._capture()
        with patch("requests.Session.get", side_effect=fake_get):
            check_url_available("https://www.deviantart.com/artist/art/title-1", 5.0)
        self.assertIn("Mozilla/5.0", captured["headers"].get("User-Agent", ""))
        self.assertEqual(captured["headers"].get("Referer"), "https://www.deviantart.com/")

    def test_a_renewed_session_cookie_is_captured_during_an_availability_check(self):
        """DAN-123: deviantart.com/fav.me is in AVAILABILITY_CHECK_HOSTS,
        so the up-front availability sweep (and the manual re-check
        action) fetch it too, cookies attached - a second real chokepoint
        besides fetch_page_info where DeviantArt can hand back a renewed
        session."""
        import tempfile
        from pathlib import Path
        from core import config as config_module
        from core.config import Settings

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = url
            resp.headers = {"Content-Type": "text/html"}
            resp.raw.read.return_value = b""
            resp.close = MagicMock()
            jar = requests.cookies.RequestsCookieJar()
            jar.set_cookie(requests.cookies.create_cookie(
                "auth", "new-token", domain="deviantart.com"))
            resp.cookies = jar
            return resp

        # Isolated from tests/_path.py's shared throwaway config dir: this
        # test calls a real settings.save(), and giving it its own
        # directory keeps that write from racing another test's config.json
        # in the same run.
        tmp = Path(tempfile.mkdtemp())
        original = (config_module.CONFIG_DIR, config_module.CONFIG_FILE,
                    config_module.CONFIG_BACKUP_FILE, config_module.CONFIG_BROKEN_FILE)
        config_module.CONFIG_DIR = tmp
        config_module.CONFIG_FILE = tmp / "config.json"
        config_module.CONFIG_BACKUP_FILE = config_module.CONFIG_FILE.with_name("config.json.bak")
        config_module.CONFIG_BROKEN_FILE = config_module.CONFIG_FILE.with_name("config.json.unreadable")
        try:
            settings = Settings()
            settings.deviantart_cookies = "auth=old-token; auth_secure=old-secure"
            url = "https://www.deviantart.com/artist/art/title-1"
            with patch("requests.Session.get", side_effect=fake_get):
                check_url_available(
                    url, 5.0, cookies=cookies_for_url(url, settings), settings=settings)

            self.assertEqual(settings.deviantart_cookies, "auth=new-token; auth_secure=old-secure")
        finally:
            (config_module.CONFIG_DIR, config_module.CONFIG_FILE,
             config_module.CONFIG_BACKUP_FILE, config_module.CONFIG_BROKEN_FILE) = original

    def test_the_no_content_page_is_still_read_as_gone(self):
        """The whole point of seeing it: show_empty means Sankaku won't
        show that post, so the match should give way to the next best."""
        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.url = "https://chan.sankakucomplex.com/en/posts/show_empty"
            resp.headers = {"Content-Type": "text/html"}
            resp.raw.read.return_value = b""
            resp.close = MagicMock()
            return resp
        with patch("requests.Session.get", side_effect=fake_get):
            self.assertIs(
                check_url_available("https://chan.sankakucomplex.com/en/posts/3797441", 5.0),
                False)


class TestE621Authentication(unittest.TestCase):
    """e621 gates some posts behind an account. Unlike the cookie sites
    here it documents a real API key, so that is what this sends."""

    def _settings(self, user="", key=""):
        from core.config import Settings
        s = Settings()
        s.e621_username = user
        s.e621_api_key = key
        return s

    def test_credentials_become_basic_auth(self):
        import base64
        from core.search_engine import e621_auth_header
        header = e621_auth_header(self._settings("me", "secret"))
        self.assertEqual(
            header["Authorization"],
            "Basic " + base64.b64encode(b"me:secret").decode())

    def test_half_filled_credentials_send_nothing(self):
        """A username with no key authenticates nothing, and a half-filled
        header would turn every e621 request into a 401 - which is worse
        than staying anonymous."""
        from core.search_engine import e621_auth_header
        self.assertIsNone(e621_auth_header(self._settings("me", "")))
        self.assertIsNone(e621_auth_header(self._settings("", "secret")))
        self.assertIsNone(e621_auth_header(self._settings()))

    def test_whitespace_only_credentials_send_nothing(self):
        from core.search_engine import e621_auth_header
        self.assertIsNone(e621_auth_header(self._settings("  ", "  ")))

    def test_the_header_reaches_e621_urls(self):
        from core.search_engine import headers_for_url
        for host in ("e621.net", "e926.net", "e6ai.net"):
            with self.subTest(host=host):
                headers = headers_for_url(f"https://{host}/posts/1.json",
                                          self._settings("me", "secret"))
                self.assertIn("Authorization", headers)

    def test_the_descriptive_user_agent_goes_with_it(self):
        """e621 blocks generic agents outright, and an auth header on a
        blocked request just sends the key somewhere it is refused."""
        from core.search_engine import headers_for_url
        headers = headers_for_url("https://e621.net/posts/1.json",
                                  self._settings("me", "secret"))
        self.assertIn("hatate-linux", headers["User-Agent"])

    def test_no_credentials_means_no_auth_header(self):
        from core.search_engine import headers_for_url
        self.assertIsNone(headers_for_url("https://e621.net/posts/1.json", self._settings()))

    def test_the_key_is_never_sent_to_another_site(self):
        """These URLs come from IQDB and SauceNAO results, so they are not
        trustworthy input - a lookalike host must not be handed the key."""
        from core.search_engine import headers_for_url
        s = self._settings("me", "secret")
        for url in ("https://danbooru.donmai.us/posts/1",
                    "https://e621.net.evil.example/posts/1",
                    "https://evil.example/?to=e621.net"):
            with self.subTest(url=url):
                headers = headers_for_url(url, s) or {}
                self.assertNotIn("Authorization", headers)

    def test_a_subdomain_of_e621_is_still_e621(self):
        from core.search_engine import headers_for_url
        headers = headers_for_url("https://static1.e621.net/posts/1",
                                  self._settings("me", "secret")) or {}
        self.assertIn("Authorization", headers)

    def test_omitting_settings_keeps_the_old_behaviour(self):
        """Callers that have no settings to hand must still work."""
        from core.search_engine import headers_for_url
        self.assertIsNone(headers_for_url("https://e621.net/posts/1.json"))


class TestE621ExplainsWhatWentWrong(unittest.TestCase):
    def test_a_rejected_key_is_reported_as_such(self):
        """e621 answers 401 to EVERY request carrying a bad key, so a typo
        breaks e621 entirely - the message has to point at the settings."""
        from core.boorus import e621
        reason = e621.restriction_for_status(401, "", "https://e621.net/posts/1")
        self.assertIn("rejected the API credentials", reason)
        self.assertIn("clear them", reason)

    def test_other_statuses_are_not_claimed_as_an_auth_problem(self):
        from core.boorus import e621
        for status in (403, 404, 500, 200):
            with self.subTest(status=status):
                self.assertIsNone(e621.restriction_for_status(status, "", "u"))

    def test_a_withheld_file_suggests_signing_in(self):
        import json as _json
        from core.boorus import e621
        body = _json.dumps({"post": {"id": 1, "file": {"url": None}, "tags": {}}})
        reason = e621.incomplete_reason(body, "https://e621.net/posts/1")
        self.assertIn("withholding", reason)
        self.assertIn("Settings > Sites", reason)

    def test_a_deleted_post_is_not_blamed_on_being_logged_out(self):
        """MEASURED: sampling 60 of one library's e621 matches found a
        single withheld file, and it was deleted. Pointing at the sign-in
        setting would send someone after a fix that cannot work."""
        import json as _json
        from core.boorus import e621
        body = _json.dumps({"post": {"id": 1, "file": {"url": None},
                                     "flags": {"deleted": True}, "tags": {}}})
        reason = e621.incomplete_reason(body, "https://e621.net/posts/1")
        self.assertIn("deleted", reason)
        self.assertNotIn("Settings > Sites", reason)

    def test_a_live_withheld_post_still_points_at_the_account(self):
        import json as _json
        from core.boorus import e621
        body = _json.dumps({"post": {"id": 1, "file": {"url": None},
                                     "flags": {"deleted": False}, "tags": {}}})
        self.assertIn("Settings > Sites", e621.incomplete_reason(body, "https://e621.net/posts/1"))

    def test_an_available_post_gets_no_excuse(self):
        """A post that is fully available and simply has no tags is not a
        login problem, and saying it was would be wrong."""
        import json as _json
        from core.boorus import e621
        body = _json.dumps({"post": {"id": 1, "file": {"url": "https://x/y.jpg"}, "tags": {}}})
        self.assertIsNone(e621.incomplete_reason(body, "https://e621.net/posts/1"))

    def test_an_unreadable_body_claims_nothing(self):
        from core.boorus import e621
        self.assertIsNone(e621.incomplete_reason("not json", "https://e621.net/posts/1"))


class TestE621CredentialsRoundTrip(unittest.TestCase):
    def test_they_survive_a_save_and_load(self):
        import importlib, os, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-e621-")
        import core.paths
        importlib.reload(core.paths)
        import core.config
        importlib.reload(core.config)
        s = core.config.Settings()
        self.assertEqual(s.e621_username, "")
        s.e621_username = "me"
        s.e621_api_key = "secret"
        s.save()
        loaded = core.config.Settings.load()
        self.assertEqual(loaded.e621_username, "me")
        self.assertEqual(loaded.e621_api_key, "secret")

    def test_the_config_file_stays_owner_only(self):
        """It now holds an e621 API key alongside the other secrets."""
        import importlib, os, stat, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-e621-perm-")
        import core.paths
        importlib.reload(core.paths)
        import core.config
        importlib.reload(core.config)
        s = core.config.Settings()
        s.e621_api_key = "secret"
        s.save()
        mode = stat.S_IMODE(os.stat(core.paths.CONFIG_FILE).st_mode)
        self.assertEqual(mode & 0o077, 0, "the config carries credentials and must not be group/world readable")
