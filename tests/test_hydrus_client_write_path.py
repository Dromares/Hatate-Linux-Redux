"""The four HydrusClient methods that WRITE into the user's Hydrus library,
plus the two readers the URL-import confirmation poll depends on (DAN-49).

Why this module exists: the coverage run at c087466 put
`core/hydrus_client.py` at 55%, and every uncovered region was on the
write side - `import_file`, `associate_url`, `import_url`, `add_tags`,
`_resolve_to_service_key`, `download_file`, `get_url_info`,
`get_url_files`. These are the oldest methods in the file and the only
ones whose failure mode is a wrong write into somebody's real database,
so "never executed by a test" is the wrong state for them to be in.

The sharpest edge is `_resolve_to_service_key`: it decides which Hydrus
tag service receives a write, `HydrusSettings.tag_service_key` defaults
to empty, and `import_url`'s `service_keys_to_additional_tags` (unlike
`add_tags`) accepts ONLY a real hex key. Both the empty default and the
name-to-key lookup are pinned below.

Everything here stubs at one of two seams, deliberately:

  * `patch.object(client, "_post"/"_get")` when the question is "what
    request did this method construct" - the request body is the contract
    with Hydrus and the thing a reword can silently break.
  * `patch("core.hydrus_client.requests")` when the method bypasses those
    wrappers and calls `requests` itself (`download_file`, `get_thumbnail`)
    - there the header/timeout/verify/stream arguments ARE the behaviour.

No network, no real Hydrus, no PyQt6 needed.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from core.config import HydrusSettings
from core.hydrus_client import HydrusClient, HydrusError, _looks_like_service_key


def _client(**kwargs) -> HydrusClient:
    settings = HydrusSettings(**{"access_key": "ACCESS", **kwargs})
    return HydrusClient(settings)


def _response(status=200, json_body=None, content=b"", text="nope"):
    """A stand-in for requests.Response with only what _handle reads."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.content = content
    if json_body is None:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_body
    return resp


class TestImportFile(unittest.TestCase):
    """POST /add_files/add_file - the raw-bytes upload."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hatate-dan49-")
        self.path = os.path.join(self.tmp, "picture.png")
        with open(self.path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nnot really a png but bytes are bytes")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_the_file_bytes_are_the_whole_request_body(self):
        """NOT multipart/form-data. An earlier version of this wrapper sent
        one and Hydrus rejected every import; the docstring on the method
        records that, so the shape is worth pinning rather than trusting."""
        client = _client()
        with patch.object(client, "_post", return_value={"status": 1}) as post:
            client.import_file(self.path)
        (path,), kwargs = post.call_args
        self.assertEqual(path, "/add_files/add_file")
        with open(self.path, "rb") as fh:
            self.assertEqual(kwargs["raw_data"], fh.read())
        self.assertEqual(kwargs["extra_headers"], {"Content-Type": "application/octet-stream"})
        # A JSON body alongside raw bytes would make _post send the JSON
        # branch instead and drop the upload entirely.
        self.assertIsNone(kwargs.get("json_body"))

    def test_the_hydrus_response_is_returned_verbatim(self):
        client = _client()
        answer = {"status": 1, "hash": "aa" * 32, "note": ""}
        with patch.object(client, "_post", return_value=answer):
            self.assertEqual(client.import_file(self.path), answer)

    def test_an_unreadable_local_file_is_refused_before_any_request(self):
        client = _client()
        with patch.object(client, "_post") as post:
            with self.assertRaises(HydrusError) as caught:
                client.import_file(os.path.join(self.tmp, "does-not-exist.png"))
        post.assert_not_called()
        self.assertIn("Could not read local file", str(caught.exception))

    def test_hydrus_answering_200_with_a_refusal_status_is_still_a_failure(self):
        """The one that matters: /add_files/add_file answers HTTP 200 even
        when it did not import the file. 4 = failed, 7 = vetoed by an
        import option. Reading only the HTTP status would report both of
        those to the user as a successful import."""
        for status in (4, 7):
            with self.subTest(status=status):
                client = _client()
                with patch.object(client, "_post",
                                  return_value={"status": status, "note": "mime not allowed"}):
                    with self.assertRaises(HydrusError) as caught:
                        client.import_file(self.path)
                self.assertIn("mime not allowed", str(caught.exception))

    def test_a_refusal_with_no_note_still_says_something(self):
        client = _client()
        with patch.object(client, "_post", return_value={"status": 4}):
            with self.assertRaises(HydrusError) as caught:
                client.import_file(self.path)
        self.assertIn("no details given", str(caught.exception))

    def test_the_success_statuses_are_not_treated_as_refusals(self):
        """1 imported, 2 already had it, 3 previously deleted. None of the
        three is a failure the user needs raising at them - and 2 in
        particular is the normal case for a re-import."""
        for status in (1, 2, 3):
            with self.subTest(status=status):
                client = _client()
                with patch.object(client, "_post", return_value={"status": status}):
                    self.assertEqual(client.import_file(self.path)["status"], status)

    def test_a_response_with_no_status_at_all_is_passed_through(self):
        client = _client()
        with patch.object(client, "_post", return_value={}):
            self.assertEqual(client.import_file(self.path), {})


class TestAssociateUrl(unittest.TestCase):
    """POST /add_urls/associate_url - records where a file we uploaded
    ourselves came from. Downloads nothing."""

    def test_the_url_field_is_url_to_add_not_url(self):
        """REGRESSION: Hydrus's JSON parsing silently ignores an unknown
        key here and then reports "Did not find any URLs to add or
        delete!" - so sending "url" looks like it worked and associates
        nothing. The field name is the entire contract."""
        client = _client()
        with patch.object(client, "_post", return_value={}) as post:
            client.associate_url("http://example.com/post/1", "aa" * 32)
        path, body = post.call_args[0]
        self.assertEqual(path, "/add_urls/associate_url")
        self.assertEqual(body["url_to_add"], "http://example.com/post/1")
        self.assertNotIn("url", body)

    def test_a_hash_is_sent_as_a_one_element_hashes_list(self):
        client = _client()
        with patch.object(client, "_post", return_value={}) as post:
            client.associate_url("http://example.com/post/1", "bb" * 32)
        self.assertEqual(post.call_args[0][1]["hashes"], ["bb" * 32])

    def test_no_hash_means_no_hashes_key_rather_than_an_empty_one(self):
        """An empty "hashes" list is a request to associate the URL with
        nothing; omitting the key is what lets Hydrus apply it to the
        file it just imported."""
        client = _client()
        with patch.object(client, "_post", return_value={}) as post:
            client.associate_url("http://example.com/post/1")
        self.assertNotIn("hashes", post.call_args[0][1])

    def test_a_failure_reaches_the_caller(self):
        client = _client()
        with patch.object(client, "_post", side_effect=HydrusError("403 text", 403)):
            with self.assertRaises(HydrusError) as caught:
                client.associate_url("http://example.com/post/1")
        self.assertEqual(caught.exception.status_code, 403)


class TestImportUrl(unittest.TestCase):
    """POST /add_urls/add_url - hands the URL to Hydrus's own downloader."""

    def test_the_minimal_request_sends_the_url_and_no_page_side_effects(self):
        client = _client()
        with patch.object(client, "_post", return_value={}) as post:
            client.import_url("http://example.com/post/1")
        path, body = post.call_args[0]
        self.assertEqual(path, "/add_urls/add_url")
        self.assertEqual(body, {"url": "http://example.com/post/1",
                                "show_destination_page": False})

    def test_a_destination_page_is_only_named_when_asked_for(self):
        client = _client()
        with patch.object(client, "_post", return_value={}) as post:
            client.import_url("http://example.com/post/1", show_destination_page=True,
                              destination_page_name="hatate")
        body = post.call_args[0][1]
        self.assertTrue(body["show_destination_page"])
        self.assertEqual(body["destination_page_name"], "hatate")

    def test_no_tags_means_no_tag_service_lookup_at_all(self):
        """Resolving a service key costs a /get_services round trip. With
        nothing to tag there is nothing to resolve."""
        client = _client()
        with patch.object(client, "_post", return_value={}) as post, \
             patch.object(client, "_resolve_to_service_key") as resolve:
            client.import_url("http://example.com/post/1", tags=[])
        resolve.assert_not_called()
        self.assertNotIn("service_keys_to_additional_tags", post.call_args[0][1])

    def test_tags_go_under_the_resolved_hex_key(self):
        client = _client()
        with patch.object(client, "_post", return_value={}) as post, \
             patch.object(client, "_resolve_to_service_key", return_value="6c6f63616c2074616773"):
            client.import_url("http://example.com/post/1", tags=["cat", "dog"])
        self.assertEqual(post.call_args[0][1]["service_keys_to_additional_tags"],
                         {"6c6f63616c2074616773": ["cat", "dog"]})

    def test_an_explicit_service_key_argument_wins_over_the_setting(self):
        client = _client(tag_service_key="from settings")
        with patch.object(client, "_post", return_value={}), \
             patch.object(client, "_resolve_to_service_key", return_value="ab") as resolve:
            client.import_url("http://example.com/x", tags=["t"], service_key="explicit")
        resolve.assert_called_once_with("explicit")

    def test_the_configured_service_is_used_when_the_caller_names_none(self):
        client = _client(tag_service_key="my other tags")
        with patch.object(client, "_post", return_value={}), \
             patch.object(client, "_resolve_to_service_key", return_value="ab") as resolve:
            client.import_url("http://example.com/x", tags=["t"])
        resolve.assert_called_once_with("my other tags")

    def test_the_empty_default_setting_falls_back_to_my_tags(self):
        """HydrusSettings.tag_service_key defaults to "" - the single most
        common real configuration, since the user never has to touch it.
        Empty must become Hydrus's default local service name, not be
        passed on as an empty key."""
        client = _client()
        self.assertEqual(client.settings.tag_service_key, "")
        with patch.object(client, "_post", return_value={}), \
             patch.object(client, "_resolve_to_service_key", return_value="ab") as resolve:
            client.import_url("http://example.com/x", tags=["t"])
        resolve.assert_called_once_with("my tags")

    def test_an_unresolvable_service_imports_the_file_without_the_tags(self):
        """add_url has no name-based fallback (add_tags does). Failing the
        whole import over an unresolvable tag service would cost the user
        the file to save them the tags."""
        client = _client()
        with patch.object(client, "_post", return_value={"human_result_text": "ok"}) as post, \
             patch.object(client, "_resolve_to_service_key", return_value=None):
            result = client.import_url("http://example.com/x", tags=["t"])
        body = post.call_args[0][1]
        self.assertNotIn("service_keys_to_additional_tags", body)
        self.assertEqual(body["url"], "http://example.com/x")
        self.assertEqual(result, {"human_result_text": "ok"})

    def test_names_are_never_sent_under_the_keys_field(self):
        """The failure this guards: service_keys_to_additional_tags is
        documented as keys-only, and a name there is not rejected loudly -
        it is a write aimed at a service that does not exist."""
        client = _client(tag_service_key="my tags")
        with patch.object(client, "_post", return_value={}) as post, \
             patch.object(client, "get_services", return_value={}):
            client.import_url("http://example.com/x", tags=["t"])
        self.assertNotIn("service_keys_to_additional_tags", post.call_args[0][1])

    def test_the_response_is_returned_to_the_caller(self):
        client = _client()
        answer = {"human_result_text": '"http://example.com/x" URL added successfully.',
                  "normalised_url": "http://example.com/x"}
        with patch.object(client, "_post", return_value=answer):
            self.assertEqual(client.import_url("http://example.com/x"), answer)


class TestResolveToServiceKey(unittest.TestCase):
    """Which Hydrus tag service a write is aimed at."""

    def test_something_that_already_looks_like_a_key_is_passed_straight_through(self):
        client = _client()
        with patch.object(client, "list_tag_services") as listing:
            self.assertEqual(client._resolve_to_service_key("6c6f63616c2074616773"),
                             "6c6f63616c2074616773")
        listing.assert_not_called()

    def test_a_service_name_is_looked_up_against_get_services(self):
        client = _client()
        with patch.object(client, "list_tag_services",
                          return_value=[("aabb", "my tags"), ("ccdd", "downloader tags")]):
            self.assertEqual(client._resolve_to_service_key("downloader tags"), "ccdd")

    def test_the_name_match_ignores_case_and_surrounding_space(self):
        """Users type service names by hand into Settings."""
        client = _client()
        with patch.object(client, "list_tag_services", return_value=[("aabb", " My Tags ")]):
            self.assertEqual(client._resolve_to_service_key("  my tags"), "aabb")

    def test_an_unknown_name_resolves_to_nothing_rather_than_a_guess(self):
        client = _client()
        with patch.object(client, "list_tag_services", return_value=[("aabb", "my tags")]):
            self.assertIsNone(client._resolve_to_service_key("a service that isn't there"))

    def test_an_unreachable_hydrus_resolves_to_nothing_rather_than_raising(self):
        """import_url treats None as "import without the extra tags". A
        raise here would take the file import down with the lookup."""
        client = _client()
        with patch.object(client, "list_tag_services", side_effect=HydrusError("unreachable")):
            self.assertIsNone(client._resolve_to_service_key("my tags"))

    def test_the_empty_string_resolves_to_nothing(self):
        """_looks_like_service_key("") is False (0 % 2 == 0 would otherwise
        make an empty string a valid even-length hex key), so the empty
        default cannot be sent as a service key by accident."""
        self.assertFalse(_looks_like_service_key(""))
        client = _client()
        with patch.object(client, "list_tag_services", return_value=[("aabb", "my tags")]):
            self.assertIsNone(client._resolve_to_service_key(""))

    def test_hydrus_own_default_local_key_is_recognised_as_a_key(self):
        """Hydrus's default local tag service key is the hex encoding of
        the literal text "local tags" - 20 characters, not 64. A length
        check of 64 would send it down the name-lookup path and fail."""
        self.assertTrue(_looks_like_service_key("local tags".encode().hex()))

    def test_an_odd_length_or_non_hex_value_is_treated_as_a_name(self):
        self.assertFalse(_looks_like_service_key("abc"))       # odd length
        self.assertFalse(_looks_like_service_key("my tags"))   # not hex


class TestAddTags(unittest.TestCase):
    """POST /add_tags/add_tags, and the key-vs-name fallback that makes it
    work without knowing which of the two the user configured."""

    def _sent(self, post):
        return [call[0][1] for call in post.call_args_list]

    def test_a_hex_looking_service_tries_the_keys_field_first(self):
        client = _client(tag_service_key="6c6f63616c2074616773")
        with patch.object(client, "_post", return_value={}) as post:
            client.add_tags("aa" * 32, ["cat"])
        self.assertEqual(post.call_count, 1)
        path, body = post.call_args[0]
        self.assertEqual(path, "/add_tags/add_tags")
        self.assertEqual(body, {"hash": "aa" * 32,
                                "service_keys_to_tags": {"6c6f63616c2074616773": ["cat"]}})

    def test_a_name_looking_service_tries_the_names_field_first(self):
        client = _client(tag_service_key="my tags")
        with patch.object(client, "_post", return_value={}) as post:
            client.add_tags("aa" * 32, ["cat"])
        self.assertEqual(post.call_args[0][1],
                         {"hash": "aa" * 32, "service_names_to_tags": {"my tags": ["cat"]}})

    def test_the_empty_default_setting_writes_to_my_tags(self):
        """The default configuration. An empty service key would be a
        write with no destination."""
        client = _client()
        self.assertEqual(client.settings.tag_service_key, "")
        with patch.object(client, "_post", return_value={}) as post:
            client.add_tags("aa" * 32, ["cat"])
        self.assertEqual(post.call_args[0][1]["service_names_to_tags"], {"my tags": ["cat"]})

    def test_an_explicit_service_key_argument_wins_over_the_setting(self):
        client = _client(tag_service_key="from settings")
        with patch.object(client, "_post", return_value={}) as post:
            client.add_tags("aa" * 32, ["cat"], service_key="explicit name")
        self.assertEqual(post.call_args[0][1]["service_names_to_tags"], {"explicit name": ["cat"]})

    def test_a_wrong_first_guess_is_retried_with_the_other_field(self):
        """Correctness here comes from the fallback, not the guess - even
        Hydrus's own built-in local service has a short, unusual hex key,
        so a value CANNOT be classified with certainty."""
        client = _client(tag_service_key="deadbeef")
        with patch.object(client, "_post",
                          side_effect=[HydrusError("could not parse", 400), {"ok": True}]) as post:
            result = client.add_tags("aa" * 32, ["cat"])
        self.assertEqual(result, {"ok": True})
        first, second = self._sent(post)
        self.assertIn("service_keys_to_tags", first)
        self.assertIn("service_names_to_tags", second)
        # Same hash and same tags both times - only the field name changes.
        self.assertEqual(first["hash"], second["hash"])
        self.assertEqual(list(first["service_keys_to_tags"].values()),
                         list(second["service_names_to_tags"].values()))

    def test_a_name_that_is_really_a_key_also_recovers(self):
        client = _client(tag_service_key="my tags")
        with patch.object(client, "_post",
                          side_effect=[HydrusError("not found", 400), {"ok": True}]) as post:
            client.add_tags("aa" * 32, ["cat"])
        first, second = self._sent(post)
        self.assertIn("service_names_to_tags", first)
        self.assertIn("service_keys_to_tags", second)

    def test_both_attempts_failing_raises_the_second_error_chained_to_the_first(self):
        """The second failure is the one describing the fallback the user
        ended on; the first is kept as __cause__ so the log has both."""
        client = _client(tag_service_key="my tags")
        first_exc = HydrusError("first went wrong", 400)
        second_exc = HydrusError("second went wrong", 500)
        with patch.object(client, "_post", side_effect=[first_exc, second_exc]):
            with self.assertRaises(HydrusError) as caught:
                client.add_tags("aa" * 32, ["cat"])
        self.assertIs(caught.exception, second_exc)
        self.assertIs(caught.exception.__cause__, first_exc)

    def test_an_empty_tag_list_is_still_sent_rather_than_silently_dropped(self):
        """Not a no-op by accident: add_tags has no empty-list guard, and a
        caller relying on one would be relying on behaviour that is not
        there. Pinned so a later "optimisation" is a deliberate change."""
        client = _client(tag_service_key="my tags")
        with patch.object(client, "_post", return_value={}) as post:
            client.add_tags("aa" * 32, [])
        self.assertEqual(post.call_args[0][1]["service_names_to_tags"], {"my tags": []})


class TestDownloadFile(unittest.TestCase):
    """GET /get_files/file streamed to disk - the one write that lands on
    the user's filesystem rather than in Hydrus."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hatate-dan49-dl-")
        self.dest = os.path.join(self.tmp, "out.bin")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _resp(self, status=200, chunks=(b"abc", b"def")):
        resp = MagicMock()
        resp.status_code = status
        resp.iter_content.return_value = list(chunks)
        return resp

    def test_the_url_carries_the_file_id_and_the_access_key_is_a_header(self):
        """Not a query parameter: an access key in a URL ends up in logs
        and in Hydrus's own request log."""
        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.get.return_value = self._resp()
            client.download_file(77, self.dest)
        url = requests_mod.get.call_args[0][0]
        kwargs = requests_mod.get.call_args[1]
        self.assertEqual(url, "http://127.0.0.1:45869/get_files/file?file_id=77")
        self.assertEqual(kwargs["headers"]["Hydrus-Client-API-Access-Key"], "ACCESS")
        self.assertNotIn("ACCESS", url)

    def test_the_body_is_streamed_and_written_whole(self):
        """stream=True is why a multi-hundred-megabyte file does not have
        to be held in memory first; iter_content is the paired half."""
        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.get.return_value = self._resp(chunks=(b"one", b"two", b"three"))
            client.download_file(1, self.dest)
        self.assertTrue(requests_mod.get.call_args[1]["stream"])
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"onetwothree")

    def test_the_configured_timeout_and_ssl_setting_are_honoured(self):
        client = _client(timeout=7.5, verify_ssl=False)
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.get.return_value = self._resp()
            client.download_file(1, self.dest)
        kwargs = requests_mod.get.call_args[1]
        self.assertEqual(kwargs["timeout"], 7.5)
        self.assertIs(kwargs["verify"], False)

    def test_a_non_200_raises_and_leaves_no_partial_file_behind(self):
        """A truncated or error-body file at dest_path would be read later
        as a real download."""
        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.get.return_value = self._resp(status=404)
            with self.assertRaises(HydrusError) as caught:
                client.download_file(5, self.dest)
        self.assertIn("404", str(caught.exception))
        self.assertIn("file 5", str(caught.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_an_unreachable_hydrus_becomes_a_hydrus_error(self):
        client = _client()

        class Boom(Exception):
            pass

        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Boom
            requests_mod.get.side_effect = Boom("connection refused")
            with self.assertRaises(HydrusError) as caught:
                client.download_file(9, self.dest)
        self.assertIn("connection refused", str(caught.exception))
        # A transport failure has no HTTP status to report.
        self.assertIsNone(caught.exception.status_code)

    def test_the_file_path_url_helper_is_what_download_uses(self):
        client = _client(api_url="https://hydrus.example:1234")
        self.assertEqual(client.get_file_path_url(3),
                         "https://hydrus.example:1234/get_files/file?file_id=3")


class TestUrlInfoReaders(unittest.TestCase):
    """The two GETs the URL-import confirmation poll is built on."""

    def test_get_url_info_asks_the_classification_endpoint(self):
        client = _client()
        answer = {"url_type": 0, "url_class": "danbooru post", "match_name": "danbooru",
                  "can_parse": True, "normalised_url": "http://example.com/post/1"}
        with patch.object(client, "_get", return_value=answer) as get:
            self.assertEqual(client.get_url_info("http://example.com/post/1"), answer)
        self.assertEqual(get.call_args[0],
                         ("/add_urls/get_url_info", {"url": "http://example.com/post/1"}))

    def test_get_url_files_asks_the_database_state_endpoint(self):
        """The distinction the confirmation poll rests on: add_url's own
        response only says the request was ACCEPTED. This one reflects
        what is actually in the database."""
        client = _client()
        answer = {"url_file_statuses": [{"status": 2, "hash": "aa" * 32}]}
        with patch.object(client, "_get", return_value=answer) as get:
            self.assertEqual(client.get_url_files("http://example.com/post/1"), answer)
        self.assertEqual(get.call_args[0],
                         ("/add_urls/get_url_files", {"url": "http://example.com/post/1"}))

    def test_neither_reader_swallows_a_hydrus_error(self):
        """poll_single_url_import catches HydrusError itself and retries;
        it can only do that if the error actually arrives."""
        client = _client()
        for method in ("get_url_info", "get_url_files"):
            with self.subTest(method=method):
                with patch.object(client, "_get", side_effect=HydrusError("boom", 500)):
                    with self.assertRaises(HydrusError):
                        getattr(client, method)("http://example.com/x")


class TestRequestPlumbing(unittest.TestCase):
    """_get/_post themselves: headers, timeouts and the failure translation
    every method above inherits."""

    def test_no_access_key_means_no_access_key_header(self):
        """Hydrus can be configured without one; sending an empty header
        is a different request from sending none."""
        client = HydrusClient(HydrusSettings(access_key=""))
        self.assertEqual(client._headers(), {})

    def test_a_get_sends_the_key_timeout_and_ssl_setting(self):
        client = _client(timeout=3.0, verify_ssl=False)
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.get.return_value = _response(json_body={"ok": 1})
            self.assertEqual(client._get("/verify_access_key", {"a": "b"}), {"ok": 1})
        args, kwargs = requests_mod.get.call_args
        self.assertEqual(args[0], "http://127.0.0.1:45869/verify_access_key")
        self.assertEqual(kwargs["params"], {"a": "b"})
        self.assertEqual(kwargs["headers"]["Hydrus-Client-API-Access-Key"], "ACCESS")
        self.assertEqual(kwargs["timeout"], 3.0)
        self.assertIs(kwargs["verify"], False)

    def test_a_raw_post_sends_bytes_as_data_and_never_as_json(self):
        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.post.return_value = _response(json_body={})
            client._post("/add_files/add_file", raw_data=b"\x00\x01",
                         extra_headers={"Content-Type": "application/octet-stream"})
        kwargs = requests_mod.post.call_args[1]
        self.assertEqual(kwargs["data"], b"\x00\x01")
        self.assertNotIn("json", kwargs)
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(kwargs["headers"]["Hydrus-Client-API-Access-Key"], "ACCESS")

    def test_a_json_post_with_no_body_sends_an_empty_object(self):
        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.post.return_value = _response(json_body={})
            client._post("/x")
        self.assertEqual(requests_mod.post.call_args[1]["json"], {})

    def test_an_unreachable_hydrus_names_the_url_it_could_not_reach(self):
        class Boom(Exception):
            pass

        client = _client(api_url="http://elsewhere:1")
        for method, call in (("get", lambda c: c._get("/x")),
                             ("post", lambda c: c._post("/x", {}))):
            with self.subTest(method=method):
                with patch("core.hydrus_client.requests") as requests_mod:
                    requests_mod.RequestException = Boom
                    getattr(requests_mod, method).side_effect = Boom("no route")
                    with self.assertRaises(HydrusError) as caught:
                        call(client)
                self.assertIn("http://elsewhere:1", str(caught.exception))
                self.assertIn("no route", str(caught.exception))

    def test_a_401_and_a_403_are_told_apart_by_status_not_by_wording(self):
        with self.assertRaises(HydrusError) as unauthorized:
            HydrusClient._handle("/x", _response(status=401))
        self.assertEqual(unauthorized.exception.status_code, 401)
        self.assertIn("access key", str(unauthorized.exception))

        with self.assertRaises(HydrusError) as forbidden:
            HydrusClient._handle("/x", _response(status=403))
        self.assertEqual(forbidden.exception.status_code, 403)
        self.assertIn("permission", str(forbidden.exception))

    def test_a_200_with_an_unparseable_body_is_an_empty_dict_not_a_crash(self):
        """Several Hydrus write endpoints answer 200 with an empty body."""
        self.assertEqual(HydrusClient._handle("/x", _response(status=200)), {})

    def test_a_200_with_json_returns_the_parsed_body(self):
        self.assertEqual(HydrusClient._handle("/x", _response(json_body={"a": 1})), {"a": 1})


class TestThumbnailReader(unittest.TestCase):
    """get_thumbnail bypasses _get too, and answers None rather than
    raising - callers draw a placeholder instead of failing a row."""

    def test_the_bytes_come_back_for_a_200(self):
        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Exception
            requests_mod.get.return_value = _response(content=b"JPEGDATA")
            self.assertEqual(client.get_thumbnail("aa" * 32), b"JPEGDATA")
        self.assertEqual(requests_mod.get.call_args[1]["params"], {"hash": "aa" * 32})

    def test_a_non_200_and_a_transport_failure_both_answer_none(self):
        class Boom(Exception):
            pass

        client = _client()
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Boom
            requests_mod.get.return_value = _response(status=500)
            self.assertIsNone(client.get_thumbnail("aa" * 32))
        with patch("core.hydrus_client.requests") as requests_mod:
            requests_mod.RequestException = Boom
            requests_mod.get.side_effect = Boom("refused")
            self.assertIsNone(client.get_thumbnail("aa" * 32))


class TestServiceAndSearchReaders(unittest.TestCase):
    """The readers the settings dialog and Query Hydrus lean on."""

    def test_tag_services_are_filtered_by_hydrus_own_type_label(self):
        client = _client()
        services = {
            "aa": {"name": "my tags", "type_pretty": "local tag service"},
            "bb": {"name": "public tag repo", "type_pretty": "tag repository"},
            "cc": {"name": "my files", "type_pretty": "local file domain"},
            "dd": {"name": "favourites", "type_pretty": "local rating like service"},
        }
        with patch.object(client, "_get", return_value={"services": services}):
            self.assertEqual(sorted(client.list_tag_services()),
                             [("aa", "my tags"), ("bb", "public tag repo")])

    def test_a_service_with_no_name_falls_back_to_its_key(self):
        client = _client()
        with patch.object(client, "_get",
                          return_value={"services": {"aa": {"type_pretty": "local tag service"}}}):
            self.assertEqual(client.list_tag_services(), [("aa", "aa")])

    def test_a_response_with_no_services_key_is_an_empty_mapping(self):
        client = _client()
        with patch.object(client, "_get", return_value={}):
            self.assertEqual(client.get_services(), {})

    def test_search_files_json_encodes_the_tag_list(self):
        client = _client()
        with patch.object(client, "_get", return_value={"file_ids": [1, 2]}) as get:
            self.assertEqual(client.search_files(["cat", "dog"]), [1, 2])
        params = get.call_args[0][1]
        self.assertEqual(params["tags"], '["cat", "dog"]')
        self.assertEqual(params["file_sort_type"], 0)

    def test_file_metadata_becomes_hydrus_file_rows_with_their_current_tags(self):
        client = _client()
        payload = {"metadata": [{
            "file_id": 4, "hash": "aa" * 32, "mime": "image/png", "width": 10, "height": 20,
            "tags": {"aa": {"display_tags": {"0": ["cat", "blue"], "1": ["pending"]}}},
        }]}
        with patch.object(client, "_get", return_value=payload):
            rows = client.get_file_metadata([4])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].file_id, rows[0].hash, rows[0].mime), (4, "aa" * 32, "image/png"))
        # Only status "0" (current) tags - "1" is pending, not on the file.
        self.assertEqual(rows[0].tags, ["blue", "cat"])

    def test_a_hydrus_file_with_no_tags_gets_an_empty_list_not_none(self):
        from core.hydrus_client import HydrusFile
        self.assertEqual(HydrusFile(file_id=1, hash="aa").tags, [])

    def test_verify_access_key_is_what_test_connection_asks(self):
        client = _client()
        with patch.object(client, "_get", return_value={"basic_permissions": [0]}) as get:
            self.assertTrue(client.test_connection())
        self.assertEqual(get.call_args[0][0], "/verify_access_key")
        with patch.object(client, "_get", return_value={}):
            self.assertFalse(client.test_connection())


if __name__ == "__main__":
    unittest.main()
