"""core/boorus/xbooru.py - its Data API client and HTML fallbacks (DAN-74).

58% at 4c1acd9, missing 50, 57, 65-67, 74-76 and 80-99. The gap is
specific: tests/test_boorus_harness.py seeds `_last_api_post` directly so
the API-first path can be exercised offline, which is the right thing for
a construction harness but leaves `_fetch_post_from_api` itself - the URL
it builds, and every way the request can fail - plus all three HTML
fallbacks unexecuted.

Both halves matter for the same reason. xbooru has its own module rather
than reusing gelbooru's precisely because Gelbooru's Data API 401s without
credentials and latches, and this host's API is still public: so the one
thing that must not break is that a failed or malformed API answer falls
back to scraping the page instead of returning nothing. A post that
arrives with no file URL and no dimensions cannot be compared against the
local file at all, and the row just looks like a weaker match.

No network. `net.get` is patched everywhere, and the module-level memo
`_last_api_post` is reset in setUp and tearDown - the same leak discipline
tests/test_boorus_harness.py applies (DAN-13 finding 2), and necessary
here because a memo left behind would serve a later test the previous
test's post.
"""
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401

import requests

from core.boorus import xbooru

POST_URL = "https://xbooru.com/index.php?page=post&s=view&id=987654"

# Recorded shape of the fallback page: the "Original image" link, the
# displayed sample under #image, and the Statistics sidebar's Size label.
HTML_PAGE = (
    "<html><body>"
    '<a href="https://xbooru.com//images/a/b/original.png">Original image</a>'
    '<img id="image" src="https://xbooru.com//samples/a/b/sample.jpg">'
    "<li>Size: 1536x2048</li>"
    "<ul id=\"tag-sidebar\">"
    '  <li class="tag-type-artist tag">'
    '    <a href="index.php?page=wiki&amp;s=list&amp;search=someone">?</a>'
    '    <a href="index.php?page=post&amp;s=list&amp;tags=someone">someone</a>'
    "  </li>"
    "</ul>"
    "</body></html>"
)

# xbooru and rule34 answer the Data API with a BARE LIST of posts, not
# Gelbooru's {"post": [...]} envelope.
API_BODY = [{
    "file_url": "https://xbooru.com//images/a/b/api-original.png",
    "sample_url": "https://xbooru.com//samples/a/b/api-sample.jpg",
    "width": "3000",
    "height": "4000",
}]


class _FakeResponse:
    def __init__(self, status_code=200, body=None, raises=None):
        self.status_code = status_code
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


class _XbooruCase(unittest.TestCase):
    def setUp(self):
        xbooru._last_api_post = None

    def tearDown(self):
        xbooru._last_api_post = None

    def _api(self, *responses):
        """Patches net.get to return the given responses in order."""
        queue = list(responses)
        return patch.object(xbooru.net, "get", side_effect=lambda *a, **k: queue.pop(0))


class TestMatches(_XbooruCase):
    def test_it_claims_its_own_host(self):
        self.assertTrue(xbooru.matches(POST_URL))

    def test_it_does_not_claim_a_relative(self):
        # The construction check that matters most for this family: these
        # three modules parse near-identical markup, so a host predicate
        # that is too loose silently routes another site's posts here.
        self.assertFalse(xbooru.matches("https://rule34.xxx/index.php?id=1"))
        self.assertFalse(xbooru.matches("https://gelbooru.com/index.php?id=1"))
        self.assertFalse(xbooru.matches("https://notxbooru.com/index.php?id=1"))


class TestFetchPostFromApi(_XbooruCase):
    def test_the_api_url_asks_for_json_and_the_post_id_from_the_page_url(self):
        with self._api(_FakeResponse(body=API_BODY)) as get:
            xbooru._get_api_post(POST_URL)

        api_url = get.call_args.args[0]
        self.assertIn("page=dapi&s=post&q=index", api_url)
        self.assertIn("json=1", api_url)
        self.assertIn("id=987654", api_url)
        self.assertTrue(api_url.startswith("https://xbooru.com/index.php?"))
        self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"],
                         xbooru.USER_AGENT)

    def test_a_bare_list_body_is_unwrapped(self):
        # xbooru's actual shape. A caller that only handled Gelbooru's
        # {"post": [...]} envelope got nothing from a perfectly good 200.
        with self._api(_FakeResponse(body=API_BODY)):
            post = xbooru._get_api_post(POST_URL)
        self.assertEqual(post["file_url"], API_BODY[0]["file_url"])

    def test_an_empty_list_body_yields_no_post(self):
        with self._api(_FakeResponse(body=[])):
            self.assertIsNone(xbooru._get_api_post(POST_URL))

    def test_a_url_with_no_post_id_makes_no_request_at_all(self):
        with patch.object(xbooru.net, "get") as get:
            self.assertIsNone(xbooru._fetch_post_from_api(
                "https://xbooru.com/index.php?page=post&s=list"))
        get.assert_not_called()

    def test_a_network_failure_yields_no_post_rather_than_raising(self):
        with patch.object(xbooru.net, "get",
                          side_effect=requests.RequestException("connection reset")):
            self.assertIsNone(xbooru._fetch_post_from_api(POST_URL))

    def test_a_non_200_yields_no_post(self):
        with self._api(_FakeResponse(status_code=503)):
            self.assertIsNone(xbooru._fetch_post_from_api(POST_URL))

    def test_a_body_that_is_not_json_yields_no_post(self):
        # A Cloudflare interstitial answers 200 with HTML.
        with self._api(_FakeResponse(body=None, raises=ValueError("not json"))):
            self.assertIsNone(xbooru._fetch_post_from_api(POST_URL))


class TestApiMemo(_XbooruCase):
    def test_the_same_url_is_only_requested_once(self):
        # Four parse hooks are called per post (tags, rating, file URL,
        # preview, dimensions). Without the memo that is a request each.
        with self._api(_FakeResponse(body=API_BODY)) as get:
            xbooru.parse_file_url(HTML_PAGE, POST_URL)
            xbooru.parse_preview_url(HTML_PAGE, POST_URL)
            xbooru.parse_dimensions(HTML_PAGE, POST_URL)

        self.assertEqual(get.call_count, 1)

    def test_a_failed_lookup_is_also_memoised_rather_than_retried(self):
        # Deliberate: the alternative is three more requests to a host
        # that just refused one, per post, for a whole result page.
        with patch.object(xbooru.net, "get",
                          side_effect=requests.RequestException("down")) as get:
            xbooru.parse_file_url(HTML_PAGE, POST_URL)
            xbooru.parse_preview_url(HTML_PAGE, POST_URL)

        self.assertEqual(get.call_count, 1)
        self.assertEqual(xbooru._last_api_post, (POST_URL, None))

    def test_a_different_url_is_looked_up_again(self):
        other = "https://xbooru.com/index.php?page=post&s=view&id=11111"
        with self._api(_FakeResponse(body=API_BODY),
                       _FakeResponse(body=[{"file_url": "https://x/other.png"}])) as get:
            first = xbooru.parse_file_url(HTML_PAGE, POST_URL)
            second = xbooru.parse_file_url(HTML_PAGE, other)

        self.assertEqual(get.call_count, 2)
        self.assertNotEqual(first, second)


class TestApiFirstThenHtml(_XbooruCase):
    """The API answer wins where it has one; the page is the fallback."""

    def test_the_api_file_url_is_preferred_over_the_page_link(self):
        with self._api(_FakeResponse(body=API_BODY)):
            self.assertEqual(xbooru.parse_file_url(HTML_PAGE, POST_URL),
                             API_BODY[0]["file_url"])

    def test_the_page_link_is_used_when_the_api_gives_nothing(self):
        with self._api(_FakeResponse(status_code=403)):
            self.assertEqual(xbooru.parse_file_url(HTML_PAGE, POST_URL),
                             "https://xbooru.com//images/a/b/original.png")

    def test_the_page_link_is_used_when_the_api_post_has_no_file_url(self):
        with self._api(_FakeResponse(body=[{"width": "10", "height": "10"}])):
            self.assertEqual(xbooru.parse_file_url(HTML_PAGE, POST_URL),
                             "https://xbooru.com//images/a/b/original.png")

    def test_no_file_url_anywhere_is_None_rather_than_an_error(self):
        with self._api(_FakeResponse(status_code=403)):
            self.assertIsNone(xbooru.parse_file_url("<html></html>", POST_URL))

    def test_the_api_sample_url_is_preferred_over_the_displayed_image(self):
        with self._api(_FakeResponse(body=API_BODY)):
            self.assertEqual(xbooru.parse_preview_url(HTML_PAGE, POST_URL),
                             API_BODY[0]["sample_url"])

    def test_the_displayed_image_is_the_preview_fallback(self):
        with self._api(_FakeResponse(status_code=403)):
            self.assertEqual(xbooru.parse_preview_url(HTML_PAGE, POST_URL),
                             "https://xbooru.com//samples/a/b/sample.jpg")

    def test_no_preview_anywhere_is_None(self):
        with self._api(_FakeResponse(status_code=403)):
            self.assertIsNone(xbooru.parse_preview_url("<html></html>", POST_URL))


class TestDimensions(_XbooruCase):
    def test_the_api_dimensions_are_returned_as_ints(self):
        # The API answers them as strings. Handing strings back would make
        # every size comparison against the local file wrong or raise.
        with self._api(_FakeResponse(body=API_BODY)):
            self.assertEqual(xbooru.parse_dimensions(HTML_PAGE, POST_URL), (3000, 4000))

    def test_non_numeric_api_dimensions_fall_back_to_the_page(self):
        with self._api(_FakeResponse(body=[{"width": "wide", "height": "tall"}])):
            self.assertEqual(xbooru.parse_dimensions(HTML_PAGE, POST_URL), (1536, 2048))

    def test_a_missing_height_falls_back_to_the_page(self):
        with self._api(_FakeResponse(body=[{"width": "3000"}])):
            self.assertEqual(xbooru.parse_dimensions(HTML_PAGE, POST_URL), (1536, 2048))

    def test_the_size_label_is_read_when_the_api_gives_nothing(self):
        # Reported as the ORIGINAL file's size, not the displayed sample's
        # - the distinction is what decides whether a match is an upgrade.
        with self._api(_FakeResponse(status_code=503)):
            self.assertEqual(xbooru.parse_dimensions(HTML_PAGE, POST_URL), (1536, 2048))

    def test_no_dimensions_anywhere_is_a_None_pair(self):
        with self._api(_FakeResponse(status_code=503)):
            self.assertEqual(xbooru.parse_dimensions("<html></html>", POST_URL),
                             (None, None))


class TestParse(_XbooruCase):
    def test_tags_come_from_the_shared_gelbooru_markup_reader(self):
        tags = xbooru.parse(HTML_PAGE, POST_URL)
        # Namespaced from the <li> class, and kept in the booru's own
        # spelling ("artist", not Hydrus's "creator") - core/tag_colors.py
        # is what maps the two onto one colour.
        self.assertEqual({(t.namespace, t.name) for t in tags}, {("artist", "someone")})

    def test_an_unparseable_page_yields_no_tags_rather_than_raising(self):
        # Logged at DEBUG naming the URL, because "markup may differ from
        # Gelbooru's" is the expected way this breaks and there would
        # otherwise be nothing to go on.
        self.assertEqual(xbooru.parse("<html><body>nothing here</body></html>",
                                      POST_URL), [])

    def test_the_rating_comes_off_the_page_at_no_extra_request(self):
        html = HTML_PAGE.replace("<li>Size:", "<li>Rating: Explicit</li><li>Size:")
        with patch.object(xbooru.net, "get") as get:
            self.assertEqual(xbooru.parse_rating(html, POST_URL), "explicit")
        get.assert_not_called()

    def test_the_memo_is_reset_between_tests(self):
        # The explicit proof, as in tests/test_boorus_harness.py: without
        # it a later test would be served an earlier one's post and pass
        # for the wrong reason.
        self.assertIsNone(xbooru._last_api_post)


if __name__ == "__main__":
    unittest.main()
