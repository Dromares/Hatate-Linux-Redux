"""core/site_access.py - per-site request headers, cookies and credentials.

Imports directly from core.site_access rather than through its re-export
in core.search_engine, so a broken re-export doesn't hide a real
regression here - this file is meant to catch breakage in the module
itself, independent of how (or whether) anything else re-exports it.

No network: nothing here issues a real request. Settings.save() calls
only ever touch the throwaway XDG_CONFIG_HOME tests/_path.py points at,
never a real config file, and nothing here talks to a live Hydrus client.

Covers: _host_is's subdomain/lookalike/malformed-URL matching,
headers_for_url and cookies_for_url's per-site dispatch (including the
DAN-122 DeviantArt browser User-Agent), e621_auth_header's missing/partial
credential handling, parse_cookie_string's tolerant parsing,
animepictures_cookies' bare-token fallback, cookie_paste_warnings'
truncation/short-cookie advisories, and capture_rotated_deviantart_cookies'
merge/persist rule (DAN-123), including the non-2xx no-clobber regression
and a non-iterable response.cookies degrading to a no-op.

Not covered here: the two real call sites that invoke
capture_rotated_deviantart_cookies (core.boorus.fetch_page_info and
core.availability._page_says_available) - see test_search_quality.py's
TestDeviantArtParser/TestDeviantArtCookieRotation for that wiring and for
the Settings-dialog reconciliation (apply_to_settings not clobbering a
live rotation), which is test_gui_smoke.py's territory. Also not covered:
Pixiv/Sankaku's HEADERS dict contents beyond what headers_for_url exposes,
and thread-safety of _deviantart_cookie_lock under real concurrent
QThreads (nothing here spins up threads).
"""
import unittest
from unittest.mock import MagicMock

import requests

from . import _path  # noqa: F401
from core.config import Settings
from core.site_access import (
    ANIMEPICTURES_SESSION_COOKIE,
    ROTATABLE_DEVIANTART_COOKIES,
    USER_AGENT,
    _host_is,
    animepictures_cookies,
    capture_rotated_deviantart_cookies,
    cookie_paste_warnings,
    cookies_for_url,
    e621_auth_header,
    headers_for_url,
    parse_cookie_string,
)


class TestHostIs(unittest.TestCase):
    def test_exact_host_matches(self):
        self.assertTrue(_host_is("https://deviantart.com/x", "deviantart.com"))

    def test_subdomain_matches(self):
        self.assertTrue(_host_is("https://www.deviantart.com/x", "deviantart.com"))

    def test_different_host_does_not_match(self):
        self.assertFalse(_host_is("https://danbooru.donmai.us/posts/1", "deviantart.com"))

    def test_lookalike_suffix_host_does_not_match(self):
        """A substring test (what this used to be) matched
        "deviantart.com.evil.example" - that hands a lookalike host the
        user's live session cookie."""
        self.assertFalse(_host_is("https://deviantart.com.evil.example/x", "deviantart.com"))

    def test_domain_mentioned_in_a_query_string_does_not_match(self):
        self.assertFalse(_host_is("https://evil.example/?u=deviantart.com", "deviantart.com"))

    def test_malformed_bracketed_host_is_not_a_match(self):
        """urlparse(...).hostname raises ValueError for a URL it can't
        make sense of as IPv6 - these URLs come from IQDB/SauceNAO
        results, not trustworthy input, so this must degrade to "not a
        match" rather than propagate."""
        self.assertFalse(_host_is("http://[::1", "deviantart.com"))
        self.assertFalse(_host_is("http://[bad]:1/", "deviantart.com"))

    def test_empty_url_is_not_a_match(self):
        self.assertFalse(_host_is("", "deviantart.com"))


class TestHeadersForUrl(unittest.TestCase):
    def test_pixiv_gets_a_browser_header_set(self):
        headers = headers_for_url("https://www.pixiv.net/ajax/illust/1")
        self.assertIn("Chrome", headers["User-Agent"])
        self.assertEqual(headers["Referer"], "https://www.pixiv.net/")

    def test_sankaku_gets_a_browser_user_agent(self):
        headers = headers_for_url("https://chan.sankakucomplex.com/en/posts/1")
        self.assertIn("Chrome", headers["User-Agent"])

    def test_deviantart_gets_a_browser_user_agent(self):
        """DAN-122: a session cookie replayed under this app's own
        descriptive User-Agent is a plausible reason DeviantArt would cut
        it short - the mitigation is a precedented browser UA + Referer,
        same as Pixiv/Sankaku."""
        headers = headers_for_url("https://www.deviantart.com/artist/art/title-1")
        self.assertIn("Chrome", headers["User-Agent"])
        self.assertNotEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["Referer"], "https://www.deviantart.com/")

    def test_fav_me_shortlinks_get_the_same_deviantart_headers(self):
        headers = headers_for_url("https://www.fav.me/d123456")
        self.assertIn("Chrome", headers["User-Agent"])

    def test_reddit_gets_its_own_declared_user_agent(self):
        from core import reddit
        headers = headers_for_url("https://www.reddit.com/r/test/comments/1/x/")
        self.assertEqual(headers["User-Agent"], reddit.USER_AGENT)

    def test_unrecognized_host_gets_no_override(self):
        self.assertIsNone(headers_for_url("https://danbooru.donmai.us/posts/1"))

    def test_empty_url_gets_no_override(self):
        self.assertIsNone(headers_for_url(""))

    def test_lookalike_deviantart_host_gets_no_browser_headers(self):
        self.assertIsNone(headers_for_url("https://deviantart.com.evil.example/x"))

    def test_lookalike_pixiv_host_gets_no_browser_headers(self):
        """DAN-177: headers_for_url used to substring-match "pixiv.net",
        which also matched a lookalike suffix host."""
        self.assertIsNone(headers_for_url("https://pixiv.net.evil.example/x"))

    def test_pixiv_mentioned_in_a_query_string_gets_no_browser_headers(self):
        self.assertIsNone(headers_for_url("https://evil.example/?u=pixiv.net"))

    def test_lookalike_sankaku_host_gets_no_browser_headers(self):
        self.assertIsNone(
            headers_for_url("https://sankakucomplex.com.evil.example/x")
        )

    def test_sankaku_mentioned_in_a_query_string_gets_no_browser_headers(self):
        self.assertIsNone(
            headers_for_url("https://evil.example/?u=sankakucomplex.com")
        )


class TestE621AuthHeader(unittest.TestCase):
    """e621 credentials - both halves required, per the module's own
    docstring: a half-filled header turns every request into a 401."""

    def _settings(self, user="", key=""):
        s = Settings()
        s.e621_username = user
        s.e621_api_key = key
        return s

    def test_both_halves_present_makes_a_basic_auth_header(self):
        import base64
        header = e621_auth_header(self._settings("me", "secret"))
        self.assertEqual(
            header["Authorization"],
            "Basic " + base64.b64encode(b"me:secret").decode("ascii"),
        )

    def test_missing_credential_sends_nothing(self):
        self.assertIsNone(e621_auth_header(self._settings("me", "")))
        self.assertIsNone(e621_auth_header(self._settings("", "secret")))
        self.assertIsNone(e621_auth_header(self._settings()))

    def test_whitespace_only_credential_counts_as_missing(self):
        self.assertIsNone(e621_auth_header(self._settings("  ", "  ")))

    def test_the_header_reaches_every_e621_family_host(self):
        s = self._settings("me", "secret")
        for host in ("e621.net", "e926.net", "e6ai.net", "static1.e621.net"):
            with self.subTest(host=host):
                headers = headers_for_url(f"https://{host}/posts/1.json", s) or {}
                self.assertIn("Authorization", headers)

    def test_missing_credential_means_no_auth_header_from_headers_for_url(self):
        self.assertIsNone(headers_for_url("https://e621.net/posts/1.json", self._settings()))

    def test_omitting_settings_entirely_is_the_same_as_missing_credentials(self):
        self.assertIsNone(headers_for_url("https://e621.net/posts/1.json"))

    def test_the_key_is_never_sent_to_a_lookalike_host(self):
        s = self._settings("me", "secret")
        for url in ("https://e621.net.evil.example/posts/1", "https://evil.example/?to=e621.net"):
            with self.subTest(url=url):
                headers = headers_for_url(url, s) or {}
                self.assertNotIn("Authorization", headers)


class TestParseCookieString(unittest.TestCase):
    def test_semicolon_separated_pairs(self):
        self.assertEqual(
            parse_cookie_string("login=you; pass_hash=abc123"),
            {"login": "you", "pass_hash": "abc123"},
        )

    def test_newlines_count_as_separators(self):
        """A multi-row selection out of a browser's cookie panel comes as
        one pair per LINE, not semicolon-joined - without this a paste
        like that becomes a single cookie with the rest buried in its
        value, authenticating as nothing."""
        self.assertEqual(
            parse_cookie_string("login=you\npass_hash=abc123"),
            {"login": "you", "pass_hash": "abc123"},
        )

    def test_crlf_newlines_also_count_as_separators(self):
        self.assertEqual(
            parse_cookie_string("login=you\r\npass_hash=abc123"),
            {"login": "you", "pass_hash": "abc123"},
        )

    def test_stray_whitespace_and_trailing_semicolon_are_tolerated(self):
        self.assertEqual(
            parse_cookie_string("  login=you ; pass_hash=abc123 ; "),
            {"login": "you", "pass_hash": "abc123"},
        )

    def test_only_the_first_equals_sign_splits_name_from_value(self):
        """Base64 and JWTs routinely contain '=' themselves."""
        self.assertEqual(
            parse_cookie_string("token=abc=def=="),
            {"token": "abc=def=="},
        )

    def test_a_pair_with_an_empty_name_is_dropped(self):
        self.assertEqual(
            parse_cookie_string("=orphanvalue; name2=value2"),
            {"name2": "value2"},
        )

    def test_empty_string_yields_no_cookies(self):
        self.assertEqual(parse_cookie_string(""), {})

    def test_none_yields_no_cookies(self):
        self.assertEqual(parse_cookie_string(None), {})


class TestAnimePicturesCookies(unittest.TestCase):
    def test_a_normal_name_value_paste_parses_as_cookies(self):
        self.assertEqual(
            animepictures_cookies("sessionid=abc; locale=en"),
            {"sessionid": "abc", "locale": "en"},
        )

    def test_a_bare_jwt_token_is_paired_with_the_known_session_cookie_name(self):
        """Pasting just the VALUE is the natural mistake: the cookie panel
        shows name/value in separate columns, and a JWT value contains no
        '=' at all, so the name=value parser finds nothing in it."""
        token = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
        self.assertEqual(
            animepictures_cookies(token),
            {ANIMEPICTURES_SESSION_COOKIE: token},
        )

    def test_empty_string_is_no_cookies_at_all(self):
        self.assertIsNone(animepictures_cookies(""))
        self.assertIsNone(animepictures_cookies(None))

    def test_multiple_words_with_no_equals_sign_is_not_treated_as_a_bare_token(self):
        self.assertIsNone(animepictures_cookies("this is not a cookie"))

    def test_a_semicolon_with_no_equals_sign_is_not_treated_as_a_bare_token(self):
        self.assertIsNone(animepictures_cookies("not-a-cookie; also-not-one"))


class TestCookiePasteWarnings(unittest.TestCase):
    def test_a_clean_paste_has_no_warnings(self):
        self.assertEqual(
            cookie_paste_warnings("_sankakuchannel_session=" + "a" * 120), [])

    def test_a_truncated_value_is_flagged(self):
        warnings = cookie_paste_warnings("_sankakuchannel_session=abc…")
        self.assertTrue(any("truncated" in w for w in warnings))

    def test_an_ascii_ellipsis_is_also_flagged(self):
        warnings = cookie_paste_warnings("_sankakuchannel_session=abc...")
        self.assertTrue(any("truncated" in w for w in warnings))

    def test_no_session_cookie_at_all_is_flagged(self):
        warnings = cookie_paste_warnings("locale=en; theme=1")
        self.assertTrue(any("No session cookie found" in w for w in warnings))

    def test_a_suspiciously_short_session_cookie_is_flagged(self):
        warnings = cookie_paste_warnings("_sankakuchannel_session=abc123")
        self.assertTrue(any("short" in w for w in warnings))

    def test_a_custom_session_name_is_used_in_the_advice(self):
        """session_name names the cookie that actually carries the login
        for the site being configured - warning about a Sankaku cookie
        while configuring Anime-Pictures would be worse than nothing."""
        warnings = cookie_paste_warnings(
            "locale=en", session_name=ANIMEPICTURES_SESSION_COOKIE)
        self.assertTrue(any(ANIMEPICTURES_SESSION_COOKIE in w for w in warnings))

    def test_a_string_with_no_parseable_cookies_has_no_warnings(self):
        """Advisory only - an unfamiliar layout more likely means the
        site changed than that the user got it wrong, so this is silent
        rather than guessing."""
        self.assertEqual(cookie_paste_warnings("not a cookie string at all"), [])
        self.assertEqual(cookie_paste_warnings(""), [])


class TestCookiesForUrl(unittest.TestCase):
    def test_pixiv_session_cookie_becomes_phpsessid(self):
        s = Settings()
        s.pixiv_session_cookie = "abc123"
        self.assertEqual(
            cookies_for_url("https://www.pixiv.net/artworks/1", s), {"PHPSESSID": "abc123"})

    def test_no_pixiv_cookie_configured_means_none(self):
        self.assertIsNone(cookies_for_url("https://www.pixiv.net/artworks/1", Settings()))

    def test_sankaku_cookies_are_parsed(self):
        s = Settings()
        s.sankaku_cookies = "_sankakuchannel_session=abc; locale=en"
        self.assertEqual(
            cookies_for_url("https://chan.sankakucomplex.com/post/show/1", s),
            {"_sankakuchannel_session": "abc", "locale": "en"},
        )

    def test_anime_pictures_cookies_cover_the_api_subdomain_too(self):
        """api.anime-pictures.net is the host that actually serves post
        data and therefore the one that has to be authenticated."""
        s = Settings()
        s.animepictures_cookies = "sessionid=abc"
        for url in ("https://anime-pictures.net/posts/1", "https://api.anime-pictures.net/posts/1"):
            with self.subTest(url=url):
                self.assertEqual(cookies_for_url(url, s), {"sessionid": "abc"})

    def test_deviantart_and_fav_me_share_cookies(self):
        s = Settings()
        s.deviantart_cookies = "auth=abc; auth_secure=def"
        for url in ("https://www.deviantart.com/x", "https://www.fav.me/d1"):
            with self.subTest(url=url):
                self.assertEqual(cookies_for_url(url, s), {"auth": "abc", "auth_secure": "def"})

    def test_no_credentials_configured_anywhere_means_none(self):
        self.assertIsNone(cookies_for_url("https://danbooru.donmai.us/posts/1", Settings()))

    def test_a_lookalike_host_never_gets_the_cookie(self):
        s = Settings()
        s.deviantart_cookies = "auth=secret"
        self.assertIsNone(cookies_for_url("https://deviantart.com.evil.example/x", s))


class TestCaptureRotatedDeviantartCookies(unittest.TestCase):
    """DAN-122/DAN-123: DeviantArt reissues auth/auth_secure/userinfo with
    a fresh sliding expiry on every response, and core/net.py keeps no
    Set-Cookie for any host - so without this, a pasted session always
    expired on its ORIGINAL lifetime instead of the renewed one a browser
    keeps getting."""

    def _response(self, pairs, status=200):
        resp = requests.Response()
        resp.status_code = status
        jar = requests.cookies.RequestsCookieJar()
        for name, value in pairs.items():
            jar.set_cookie(requests.cookies.create_cookie(name, value, domain="deviantart.com"))
        resp.cookies = jar
        return resp

    def test_a_renewed_cookie_is_merged_and_saved_on_a_2xx(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=old-token; auth_secure=old-secure; userinfo=old-info"
        resp = self._response({"auth": "new-token", "auth_secure": "old-secure"})

        capture_rotated_deviantart_cookies(
            "https://www.deviantart.com/artist/art/title-1", resp, settings)

        self.assertEqual(
            settings.deviantart_cookies,
            "auth=new-token; auth_secure=old-secure; userinfo=old-info",
        )

    def test_a_304_also_counts_as_a_genuine_response_and_merges(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=old-token"
        resp = self._response({"auth": "new-token"}, status=304)

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=new-token")

    def test_a_non_2xx_response_never_overwrites_the_saved_session(self):
        """Cloud's PR #42 review finding: a 403/429/5xx (a bot check, the
        exact class DAN-122's browser UA exists to avoid) can carry its
        own Set-Cookie - often a fresh ANONYMOUS session - and merging
        that over a live one would silently kill it. Confirmed this fails
        against the pre-review code, which ran the merge before any
        status check at all."""
        settings = Settings()
        settings.deviantart_cookies = "auth=old-token; auth_secure=old-secure; userinfo=old-info"
        resp = self._response({"auth": "anon-token", "userinfo": "anon-info"}, status=403)

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(
            settings.deviantart_cookies,
            "auth=old-token; auth_secure=old-secure; userinfo=old-info",
        )

    def test_a_5xx_response_never_overwrites_the_saved_session(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=old-token"
        resp = self._response({"auth": "anon-token"}, status=503)

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old-token")

    def test_no_session_configured_means_nothing_gets_started(self):
        """An anonymous fetch gets a Set-Cookie too, but nobody asked for
        a session to be created out of nothing."""
        settings = Settings()
        settings.deviantart_cookies = ""
        resp = self._response({"userinfo": "anon-info"})

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "")

    def test_identical_values_are_a_no_op(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=same; auth_secure=same2"
        resp = self._response({"auth": "same", "auth_secure": "same2"})

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=same; auth_secure=same2")

    def test_fav_me_is_treated_the_same_as_deviantart_com(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = self._response({"auth": "new"})

        capture_rotated_deviantart_cookies("https://www.fav.me/d1", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=new")

    def test_other_hosts_are_never_touched_and_cookies_are_never_even_read(self):
        """Scoped strictly to deviantart.com/fav.me per the module's own
        docstring: this must not read response.cookies at all until the
        host check passes."""

        class _ExplodingCookies:
            @property
            def cookies(self):
                raise AssertionError("response.cookies read for a non-DeviantArt host")

        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = _ExplodingCookies()
        resp.status_code = 200

        capture_rotated_deviantart_cookies("https://danbooru.donmai.us/posts/1", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old")

    def test_a_non_iterable_cookie_jar_degrades_to_a_no_op(self):
        """A response whose .cookies isn't really iterable (a bare Mock
        attribute, or None) must not raise - it should look like "nothing
        to capture", the same as every other best-effort response read
        in this codebase."""
        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = MagicMock(status_code=200)
        resp.cookies = None

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old")

    def test_a_cookie_object_with_no_name_attribute_degrades_to_a_no_op(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = MagicMock(status_code=200)
        resp.cookies = [object()]  # no .name/.value - AttributeError inside the comprehension

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old")

    def test_a_response_with_no_rotatable_cookie_names_is_a_no_op(self):
        """A 2xx DeviantArt response that happens to carry no auth/
        auth_secure/userinfo at all (just e.g. a locale cookie) has
        nothing to rotate - must not touch the saved session."""
        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = self._response({"locale": "en"})

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=old")

    def test_rotation_only_touches_the_three_named_cookies(self):
        settings = Settings()
        settings.deviantart_cookies = "auth=old"
        resp = self._response({"auth": "new", "unrelated_cookie": "ignored"})
        self.assertNotIn("unrelated_cookie", ROTATABLE_DEVIANTART_COOKIES)

        capture_rotated_deviantart_cookies("https://www.deviantart.com/", resp, settings)

        self.assertEqual(settings.deviantart_cookies, "auth=new")


if __name__ == "__main__":
    unittest.main()
