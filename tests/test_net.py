"""core/net.py - the shared HTTP layer. No network: Session methods are stubbed."""
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from . import _path  # noqa: F401


def _streamed(chunks, delay=0.0, status=200):
    """A real Response whose body arrives in chunks, `delay` apart."""
    resp = requests.Response()
    resp.status_code = status

    def iter_content(size):
        for chunk in chunks:
            time.sleep(delay)
            yield chunk
    resp.iter_content = iter_content
    resp.close = MagicMock()
    return resp


class TestSessions(unittest.TestCase):
    def setUp(self):
        from core import net
        net.close_all()

    def test_one_session_per_host_reused_across_requests(self):
        from core import net
        a = net.session_for("https://rule34.us/index.php?id=1")
        self.assertIs(a, net.session_for("https://RULE34.us/other"))
        self.assertIsNot(a, net.session_for("https://gelbooru.com/"))

    def test_cookies_a_response_sets_are_not_kept(self):
        """Each request carries only the cookies its caller passes."""
        from core import net
        from requests.cookies import MockRequest
        session = net.session_for("https://example.com/")
        cookie = requests.cookies.create_cookie("ddg", "1", domain="example.com")
        request = MockRequest(requests.Request("GET", "https://example.com/").prepare())
        # The policy is what extract_cookies consults for every Set-Cookie.
        self.assertFalse(session.cookies._policy.set_ok(cookie, request))

    def test_the_app_user_agent_is_the_default_and_a_caller_can_override_it(self):
        from core import net
        with patch("requests.Session.get", return_value=MagicMock(status_code=200)) as get:
            net.get("https://a.example/", timeout=5)
            net.get("https://a.example/", timeout=5, headers={"User-Agent": "Browser"})
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["User-Agent"], net.USER_AGENT)
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["User-Agent"], "Browser")


class TestDeadline(unittest.TestCase):
    def test_a_body_that_trickles_past_the_deadline_is_abandoned_and_closed(self):
        from core import net
        from core.hard_timeout import HardTimeoutError
        resp = _streamed([b"x"] * 50, delay=0.05)
        started = time.monotonic()
        with patch("requests.Session.get", return_value=resp):
            with self.assertRaises(HardTimeoutError):
                net.get("https://slow.example/", timeout=5, deadline=0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        resp.close.assert_called()

    def test_a_body_within_the_deadline_is_read_in_full(self):
        from core import net
        resp = _streamed([b"ab", b"cd"])
        with patch("requests.Session.get", return_value=resp) as get:
            out = net.get("https://fast.example/", timeout=5, deadline=5)
        self.assertEqual(out.content, b"abcd")
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_the_per_read_timeout_never_exceeds_what_is_left(self):
        from core import net
        with patch("requests.Session.post", return_value=_streamed([b""])) as post:
            net.post("https://x.example/", timeout=60, deadline=10)
        self.assertLessEqual(post.call_args.kwargs["timeout"], 10)


class TestRetryOn429(unittest.TestCase):
    def _429(self, retry_after=None):
        resp = MagicMock(status_code=429)
        resp.headers = {} if retry_after is None else {"Retry-After": retry_after}
        return resp

    def test_a_get_waits_out_a_short_retry_after_once(self):
        from core import net
        ok = MagicMock(status_code=200)
        with patch("requests.Session.get", side_effect=[self._429("1"), ok]) as get, \
             patch("core.net.time.sleep") as sleep:
            self.assertIs(net.get("https://a.example/", timeout=5), ok)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_only_once(self):
        from core import net
        with patch("requests.Session.get", side_effect=[self._429("1"), self._429("1")]) as get, \
             patch("core.net.time.sleep"):
            self.assertEqual(net.get("https://a.example/", timeout=5).status_code, 429)
        self.assertEqual(get.call_count, 2)

    def test_a_long_retry_after_is_respected_not_sat_through(self):
        from core import net
        with patch("requests.Session.get", return_value=self._429("120")) as get, \
             patch("core.net.time.sleep") as sleep:
            net.get("https://a.example/", timeout=5)
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_an_upload_is_never_repeated(self):
        """A POST is an upload or a search - repeating it costs quota."""
        from core import net
        with patch("requests.Session.post", return_value=self._429("1")) as post, \
             patch("core.net.time.sleep"):
            net.post("https://a.example/", timeout=5)
        self.assertEqual(post.call_count, 1)


class TestHostRateLimitHeaders(unittest.TestCase):
    """Reddit: one request left x-ratelimit-remaining at 0.0 with a reset
    in 22s, and the next was refused - so the next one waits instead."""

    def setUp(self):
        from core import net
        net.close_all()
        self.addCleanup(net.close_all)

    def _resp(self, remaining, reset):
        resp = MagicMock(status_code=200)
        resp.headers = {"x-ratelimit-remaining": remaining, "x-ratelimit-reset": reset}
        return resp

    def test_a_spent_allowance_makes_the_next_request_wait_for_the_reset(self):
        from core import net
        with patch("requests.Session.get", return_value=self._resp("0.0", "5")), \
             patch("core.net.time.sleep") as sleep:
            net.get("https://www.reddit.com/a", timeout=5)
            sleep.assert_not_called()
            net.get("https://www.reddit.com/b", timeout=5)
        self.assertAlmostEqual(sleep.call_args[0][0], 5, delta=0.5)

    def test_other_hosts_are_not_held_up(self):
        from core import net
        with patch("requests.Session.get", return_value=self._resp("0.0", "5")), \
             patch("core.net.time.sleep") as sleep:
            net.get("https://www.reddit.com/a", timeout=5)
            net.get("https://i.redd.it/x.jpg", timeout=5)
        sleep.assert_not_called()

    def test_allowance_left_means_no_wait(self):
        from core import net
        with patch("requests.Session.get", return_value=self._resp("59.0", "40")), \
             patch("core.net.time.sleep") as sleep:
            net.get("https://www.reddit.com/a", timeout=5)
            net.get("https://www.reddit.com/b", timeout=5)
        sleep.assert_not_called()

    def test_a_long_reset_fails_the_request_rather_than_stalling(self):
        from core import net
        with patch("requests.Session.get", return_value=self._resp("0.0", "600")), \
             patch("core.net.time.sleep"):
            net.get("https://www.reddit.com/a", timeout=5)
            with self.assertRaises(net.RateLimited):
                net.get("https://www.reddit.com/b", timeout=5)
        import requests as _requests
        self.assertTrue(issubclass(net.RateLimited, _requests.RequestException),
                        "callers already handle RequestException as a failed fetch")


class TestIqdbBudget(unittest.TestCase):
    def test_a_short_general_timeout_is_raised_to_iqdbs_floor(self):
        """IQDB took 8-42s per search when measured; a 15s general
        timeout failed every one of them."""
        from core import iqdb
        with patch("core.iqdb.prepare_upload_bytes", return_value=(b"x", "x.png")), \
             patch("requests.Session.post",
                   return_value=MagicMock(status_code=200, text="<html></html>")) as post:
            try:
                iqdb.search("/tmp/x.png", timeout=15)
            except iqdb.IqdbError:
                pass                     # the stub's page isn't a results page; not the point
        self.assertGreater(post.call_args.kwargs["timeout"], iqdb.MIN_BUDGET_SECONDS - 1)


if __name__ == "__main__":
    unittest.main()
