"""The three background workers the coverage run found effectively
untested - availability, upscale check and pawchive indexing (DAN-49).

At c087466 these sat at 26%, 33% and 36%: constructed nowhere, `run()`
never entered. That matters more for a QThread than for a plain function,
because an exception escaping `run()` does not surface as a failed call -
it prints on a background thread and the GUI simply waits for a signal
that never arrives.

Each worker is driven by calling `run()` DIRECTLY on the test's own
thread, not via `start()`. That is deliberate:

  * the signal emissions are then delivered synchronously at emit time,
    so a test can assert on order and payload without pumping a Qt event
    loop (see the long note in tests/test_file_hash_worker.py about
    queued cross-thread connections);
  * `run()` is the whole behaviour under test - the threading is Qt's, and
    tests/test_hydrus_import_poll_worker.py already covers the real
    start/stop path where the concurrency itself is the subject.

Everything these workers call out to is stubbed. No network, and no real
image processing beyond a handful of tiny in-memory PNGs.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QThread  # noqa: F401 - import probe only
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


def _settings(**kwargs):
    from core.config import Settings
    settings = Settings()
    for name, value in kwargs.items():
        setattr(settings, name, value)
    return settings


def _entry(path="/tmp/hatate-dan49/picture.png", **kwargs):
    from core.models import ImageEntry
    entry = ImageEntry(path=path)
    for name, value in kwargs.items():
        setattr(entry, name, value)
    return entry


def _candidate(url="http://example.com/post/1", **kwargs):
    from core.models import MatchCandidate
    return MatchCandidate(url=url, **kwargs)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestAvailabilityWorker(unittest.TestCase):
    """workers/availability_worker.py - the manual "check these matches are
    still there" action. Its whole purpose is to let a definite 404 drop a
    match, so a false "gone" here costs the user a good match."""

    def _worker(self, candidates, **settings_kwargs):
        from workers.availability_worker import AvailabilityWorker
        entry = _entry(candidates=list(candidates))
        worker = AvailabilityWorker(entry, _settings(**settings_kwargs))
        return entry, worker

    def _run(self, worker, availability, soft_404=(), cookies=None):
        """Drives run() with check_url_available stubbed. `availability` is
        either a single verdict or a {url: verdict} mapping."""
        progress, finished = [], []
        worker.progress.connect(lambda done, total: progress.append((done, total)))
        worker.finished_checking.connect(
            lambda entry, gone, checked: finished.append((entry, gone, checked)))

        def _available(target, timeout, **kwargs):
            if isinstance(availability, dict):
                return availability.get(target)
            return availability

        module = "workers.availability_worker"
        with patch(f"{module}.check_url_available", side_effect=_available) as check, \
             patch(f"{module}.has_soft_404_detection", side_effect=lambda u: u in soft_404), \
             patch(f"{module}.cookies_for_url", return_value=cookies), \
             patch(f"{module}.referer_for_candidate", return_value="http://referer/"):
            worker.run()
        return check, progress, finished

    def test_a_definite_404_is_recorded_as_gone_and_counted(self):
        entry, worker = self._worker([_candidate("http://example.com/post/1")])
        _check, progress, finished = self._run(worker, False)
        self.assertIs(entry.candidates[0].remote_available, False)
        self.assertEqual(finished, [(entry, 1, 1)])
        self.assertEqual(progress, [(1, 1)])

    def test_an_alive_post_is_recorded_and_not_counted_as_gone(self):
        entry, worker = self._worker([_candidate()])
        _check, _progress, finished = self._run(worker, True)
        self.assertIs(entry.candidates[0].remote_available, True)
        self.assertEqual(finished[0][1:], (0, 1))

    def test_an_ambiguous_answer_stays_unknown_and_is_not_counted_as_gone(self):
        """The policy the whole availability layer rests on: only a definite
        404/410 may drop a match. A timeout, a 403 or a 5xx comes back as
        None, and None must not be reported as gone - that is how a network
        hiccup would delete good matches from the user's list."""
        entry, worker = self._worker([_candidate()])
        _check, _progress, finished = self._run(worker, None)
        self.assertIsNone(entry.candidates[0].remote_available)
        self.assertEqual(finished[0][1:], (0, 1))

    def test_the_direct_file_url_is_checked_where_the_site_has_no_soft_404(self):
        """A post page can still render perfectly after its image is
        removed, so the file is the more meaningful check by default."""
        entry, worker = self._worker([
            _candidate("http://example.com/post/1",
                       direct_file_url="http://cdn.example.com/full.png")])
        check, _progress, _finished = self._run(worker, True)
        self.assertEqual(check.call_args[0][0], "http://cdn.example.com/full.png")

    def test_the_post_page_is_checked_for_a_soft_404_site(self):
        """REGRESSION shape: for Pixiv the "this post was deleted" text
        only ever appears on the POST PAGE. Its old CDN file URL fails
        ambiguously (403/timeout), which correctly-but-uselessly reports
        unknown - so a deleted Pixiv post would never be detected."""
        page = "http://example.com/post/1"
        entry, worker = self._worker([
            _candidate(page, direct_file_url="http://cdn.example.com/full.png")])
        with patch("workers.availability_worker.normalize_url_for_hydrus",
                   side_effect=lambda u: u):
            check, _progress, _finished = self._run(worker, True, soft_404={page})
        self.assertEqual(check.call_args[0][0], page)

    def test_the_post_page_is_normalised_before_the_soft_404_decision(self):
        """normalize_url_for_hydrus is what turns Pixiv's legacy
        member_illust.php form into the modern /artworks/ one; the soft-404
        host list is matched against the normalised URL, not the raw one."""
        entry, worker = self._worker([_candidate("http://example.com/legacy?id=1")])
        with patch("workers.availability_worker.normalize_url_for_hydrus",
                   return_value="http://example.com/artworks/1") as normalize:
            check, _progress, _finished = self._run(
                worker, True, soft_404={"http://example.com/artworks/1"})
        normalize.assert_called_once_with("http://example.com/legacy?id=1")
        self.assertEqual(check.call_args[0][0], "http://example.com/artworks/1")

    def test_a_candidate_with_no_url_at_all_falls_back_without_normalising(self):
        entry, worker = self._worker([_candidate("", direct_file_url="http://cdn/x.png")])
        check, _progress, finished = self._run(worker, True)
        self.assertEqual(check.call_args[0][0], "http://cdn/x.png")
        self.assertEqual(finished[0][1:], (0, 1))

    def test_login_cookies_are_sent_with_every_check(self):
        """REGRESSION: without them Sankaku serves account-only and adult
        posts as a blank page, indistinguishable from a deleted one - so a
        logged-out check reports good posts as dead and this action then
        deletes them from the list. The search-time sweep sends cookies,
        and the manual check must not be able to disagree with it."""
        entry, worker = self._worker([_candidate()])
        check, _progress, _finished = self._run(worker, True, cookies={"session": "abc"})
        self.assertEqual(check.call_args[1]["cookies"], {"session": "abc"})

    def test_the_configured_search_timeout_and_a_referer_are_passed_through(self):
        entry, worker = self._worker([_candidate()], search_timeout=11.5)
        check, _progress, _finished = self._run(worker, True)
        self.assertEqual(check.call_args[0][1], 11.5)
        self.assertEqual(check.call_args[1]["referer"], "http://referer/")

    def test_every_candidate_is_checked_and_progress_reaches_the_total(self):
        candidates = [_candidate(f"http://example.com/post/{i}") for i in range(5)]
        entry, worker = self._worker(candidates)
        check, progress, finished = self._run(worker, {
            "http://example.com/post/0": False,
            "http://example.com/post/1": False,
            "http://example.com/post/2": True,
        })
        self.assertEqual(check.call_count, 5)
        # Concurrent, so completion order is not fixed - but the count is.
        self.assertEqual(sorted(progress), [(i, 5) for i in range(1, 6)])
        self.assertEqual(finished[0][1:], (2, 5))

    def test_a_check_that_raises_is_counted_as_done_without_taking_the_run_down(self):
        """One host behaving badly must not cost the user the other four
        answers, and must not leave the GUI waiting for a signal that
        never comes."""
        candidates = [_candidate(f"http://example.com/post/{i}") for i in range(3)]
        entry, worker = self._worker(candidates)
        calls = {"n": 0}

        def _boom(target, timeout, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the socket layer gave up")
            return False

        with patch("workers.availability_worker.check_url_available", side_effect=_boom), \
             patch("workers.availability_worker.has_soft_404_detection", return_value=False), \
             patch("workers.availability_worker.cookies_for_url", return_value=None), \
             patch("workers.availability_worker.referer_for_candidate", return_value=None):
            progress, finished = [], []
            worker.progress.connect(lambda d, t: progress.append((d, t)))
            worker.finished_checking.connect(
                lambda e, g, c: finished.append((e, g, c)))
            worker.run()

        self.assertEqual(len(progress), 3)
        # The raising one is counted as checked but cannot be counted gone.
        self.assertEqual(finished[0][1:], (2, 3))

    def test_no_candidates_still_finishes_rather_than_hanging(self):
        """max_workers=0 is a ValueError from ThreadPoolExecutor, which is
        why the worker count is clamped with max(1, total). An empty entry
        is reachable from the GUI: the action is not disabled for a row
        that was never searched."""
        entry, worker = self._worker([])
        _check, progress, finished = self._run(worker, True)
        self.assertEqual(progress, [])
        self.assertEqual(finished, [(entry, 0, 0)])

    def test_stop_ends_the_run_and_still_emits_finished(self):
        """The GUI's lifecycle waits on finished_checking. A stop that
        swallowed it would leave the action spinning forever."""
        candidates = [_candidate(f"http://example.com/post/{i}") for i in range(4)]
        entry, worker = self._worker(candidates)
        worker.stop()
        self.assertTrue(worker._stop_requested)
        _check, _progress, finished = self._run(worker, True)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][0], entry)
        # Nothing was recorded, because nothing was consumed.
        self.assertEqual(finished[0][1:], (0, 0))

    def test_the_reactor_full_fallback_is_NOT_applied_on_this_path(self):
        """Documents a real asymmetry rather than asserting it is right.

        DAN-45's canonicalize_url rewrites a JoyReactor/reactor.cc match to
        its full/ (unwatermarked) file before anything has confirmed that
        file exists, and BOTH search-time paths undo the rewrite when the
        check comes back definitively gone - core/availability.py:126 and
        core/search_engine.py:179 call _reactor_full_fallback. This manual
        worker does not, so for a rewritten reactor URL it reports the
        match dead where a search would have fallen back to the
        watermarked original and reported unknown.

        Pinned as current behaviour (this issue is tests-only); flagged on
        DAN-49 for a decision rather than silently fixed here.
        """
        url = "http://joyreactor.com/pics/post/full/art-12345"
        entry, worker = self._worker([_candidate(url)])
        with patch("workers.availability_worker.normalize_url_for_hydrus",
                   side_effect=lambda u: u):
            _check, _progress, finished = self._run(worker, False)
        self.assertIs(entry.candidates[0].remote_available, False)
        self.assertEqual(entry.candidates[0].url, url, "the URL was not rewritten back")
        self.assertEqual(finished[0][1:], (1, 1))

        # And the search-time behaviour it differs from, so the comparison
        # is evidence rather than a claim in a docstring.
        from core.availability import _reactor_full_fallback
        candidate = _candidate(url)
        self.assertIsNone(_reactor_full_fallback(candidate, False))
        self.assertEqual(candidate.url, "http://joyreactor.com/pics/post/art-12345")


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestUpscaleCheckWorker(unittest.TestCase):
    """workers/upscale_check_worker.py - runs three heuristics per entry and
    emits one result each. The heuristics themselves are covered by
    tests/test_upscale_detect.py; what is covered here is the wiring,
    which is where a batch can quietly lose an entry."""

    def _run(self, entries, comparison=None, consistency=None, metadata=None,
             dimensions=(800, 600), open_raises=None):
        from workers.upscale_check_worker import UpscaleCheckWorker
        from core.upscale_detect import SelfConsistencyResult

        worker = UpscaleCheckWorker(entries)
        checked, finished = [], []
        worker.entry_checked.connect(lambda entry, result: checked.append((entry, result)))
        worker.finished_all.connect(lambda: finished.append(True))

        image = MagicMock()
        image.__enter__ = MagicMock(return_value=MagicMock(size=dimensions))
        image.__exit__ = MagicMock(return_value=False)

        module = "workers.upscale_check_worker"
        with patch(f"{module}.Image.open",
                   side_effect=open_raises or (lambda path: image)) as opened, \
             patch(f"{module}.compare_to_source", return_value=comparison) as compare, \
             patch(f"{module}.detect_naive_upscale",
                   return_value=consistency or SelfConsistencyResult()) as naive, \
             patch(f"{module}.check_upscaler_metadata", return_value=metadata) as meta:
            worker.run()
        return {"checked": checked, "finished": finished, "opened": opened,
                "compare": compare, "naive": naive, "meta": meta}

    def test_one_result_is_emitted_per_entry_and_then_finished_all(self):
        entries = [_entry(f"/tmp/a{i}.png") for i in range(3)]
        out = self._run(entries)
        self.assertEqual([entry for entry, _ in out["checked"]], entries)
        self.assertEqual(out["finished"], [True])

    def test_an_empty_batch_still_says_it_finished(self):
        out = self._run([])
        self.assertEqual(out["checked"], [])
        self.assertEqual(out["finished"], [True])

    def test_missing_local_dimensions_are_read_off_the_file_and_kept(self):
        """They are cached on the entry because every later heuristic and
        the size-difference column need them."""
        entry = _entry("/tmp/a.png")
        self.assertIsNone(entry.local_width)
        self._run([entry], dimensions=(1234, 567))
        self.assertEqual((entry.local_width, entry.local_height), (1234, 567))

    def test_already_known_dimensions_are_not_re_read_from_disk(self):
        entry = _entry("/tmp/a.png", local_width=100, local_height=50)
        out = self._run([entry])
        out["opened"].assert_not_called()
        self.assertEqual((entry.local_width, entry.local_height), (100, 50))

    def test_an_unreadable_file_is_logged_and_the_batch_carries_on(self):
        """A missing or corrupt file is a normal thing to find in a folder
        somebody has been reorganising. It must cost that row's dimensions,
        not the other rows' results."""
        entries = [_entry("/tmp/gone.png"), _entry("/tmp/also-gone.png")]
        out = self._run(entries, open_raises=lambda path: (_ for _ in ()).throw(OSError("nope")))
        self.assertEqual(len(out["checked"]), 2)
        self.assertEqual(out["finished"], [True])
        self.assertIsNone(entries[0].local_width)

    def test_the_source_comparison_runs_only_when_both_sizes_are_known(self):
        from core.models import MatchCandidate
        from core.upscale_detect import SourceComparisonResult
        comparison = SourceComparisonResult(
            flagged=True, local_size=(1600, 1200), source_size=(800, 600), message="2x")

        entry = _entry("/tmp/a.png", local_width=1600, local_height=1200)
        entry.candidates = [MatchCandidate(url="http://example.com/1", width=800, height=600)]
        entry.select_candidate(0)
        out = self._run([entry], comparison=comparison)
        out["compare"].assert_called_once_with(1600, 1200, 800, 600)
        self.assertIs(out["checked"][0][1].source_comparison, comparison)

    def test_a_candidate_that_reports_no_dimensions_skips_the_comparison(self):
        """REGRESSION shape from the ranking work: a candidate must not be
        judged on dimensions its engine simply does not report. Comparing
        against 0 or None would flag every such match as an upscale."""
        from core.models import MatchCandidate
        entry = _entry("/tmp/a.png", local_width=1600, local_height=1200)
        entry.candidates = [MatchCandidate(url="http://example.com/1")]
        entry.select_candidate(0)
        out = self._run([entry])
        out["compare"].assert_not_called()
        self.assertIsNone(out["checked"][0][1].source_comparison)

    def test_no_selected_candidate_skips_the_comparison_but_not_the_rest(self):
        """The other two heuristics need only the local file, so an entry
        with no match at all is still worth checking."""
        entry = _entry("/tmp/a.png", local_width=1600, local_height=1200)
        out = self._run([entry], metadata="Topaz Gigapixel AI")
        out["compare"].assert_not_called()
        result = out["checked"][0][1]
        self.assertIsNone(result.source_comparison)
        self.assertEqual(result.metadata_signature, "Topaz Gigapixel AI")

    def test_unreadable_dimensions_do_not_stop_the_other_two_heuristics(self):
        entry = _entry("/tmp/gone.png")
        out = self._run([entry], metadata="waifu2x",
                        open_raises=lambda path: (_ for _ in ()).throw(OSError("nope")))
        out["naive"].assert_called_once_with("/tmp/gone.png")
        out["meta"].assert_called_once_with("/tmp/gone.png")
        self.assertEqual(out["checked"][0][1].metadata_signature, "waifu2x")

    def test_all_three_heuristics_land_in_the_one_emitted_result(self):
        from core.upscale_detect import SelfConsistencyResult
        consistency = SelfConsistencyResult(confidence="likely", message="clean 2x round trip")
        out = self._run([_entry("/tmp/a.png", local_width=10, local_height=10)],
                        consistency=consistency, metadata="Real-ESRGAN")
        result = out["checked"][0][1]
        self.assertIs(result.self_consistency, consistency)
        self.assertEqual(result.metadata_signature, "Real-ESRGAN")
        self.assertIsNone(result.source_comparison)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestPawchiveIndexWorker(unittest.TestCase):
    """workers/pawchive_index_worker.py - minutes of polite requests per
    artist, so stopping mid-way and surviving a failure are the behaviours
    that matter."""

    def _worker(self, jobs, index_path="/tmp/hatate-dan49-index.db"):
        from workers.pawchive_index_worker import PawchiveIndexWorker
        return PawchiveIndexWorker(jobs, index_path=index_path)

    def _run(self, worker, index_creator):
        results, progress, finished = [], [], []
        worker.creator_done.connect(results.append)
        worker.progress.connect(lambda m, d, t: progress.append((m, d, t)))
        worker.finished_all.connect(lambda: finished.append(True))
        module = "workers.pawchive_index_worker"
        with patch(f"{module}.PawchiveIndex") as index_cls, \
             patch(f"{module}.index_creator", side_effect=index_creator) as indexer:
            worker.run()
        return {"results": results, "progress": progress, "finished": finished,
                "index_cls": index_cls, "indexer": indexer}

    def _result(self, service="fa", user_id="1", **kwargs):
        from core.pawchive_index import CrawlResult
        return CrawlResult(service=service, user_id=user_id, **kwargs)

    def test_each_job_is_indexed_in_order_and_reported(self):
        worker = self._worker([("fa", "1"), ("fa", "2")])
        out = self._run(worker, lambda index, service, user_id, **kw:
                        self._result(service, user_id, posts=3))
        self.assertEqual([(r.service, r.user_id) for r in out["results"]],
                         [("fa", "1"), ("fa", "2")])
        self.assertEqual(out["finished"], [True])

    def test_the_index_is_opened_at_the_path_the_worker_was_given(self):
        """The dialog passes a path in tests and None in the app; opening
        the real user index from a test would write into their database."""
        worker = self._worker([("fa", "1")], index_path="/tmp/somewhere-else.db")
        out = self._run(worker, lambda *a, **kw: self._result())
        out["index_cls"].assert_called_once_with("/tmp/somewhere-else.db")

    def test_progress_from_the_crawler_is_forwarded_to_the_gui(self):
        def _index(index, service, user_id, should_stop=None, on_progress=None):
            on_progress("fetching page 1", 1, 4)
            on_progress("fingerprinting", 2, 4)
            return self._result(service, user_id)

        worker = self._worker([("fa", "1")])
        out = self._run(worker, _index)
        self.assertEqual(out["progress"], [("fetching page 1", 1, 4),
                                          ("fingerprinting", 2, 4)])

    def test_request_stop_is_visible_to_the_crawler_mid_job(self):
        """index_creator is documented as safe to stop at any point, but
        only if it is actually told - the worker passes its own flag in as
        should_stop rather than checking between jobs alone."""
        seen = []

        def _index(index, service, user_id, should_stop=None, on_progress=None):
            seen.append(should_stop())
            worker.request_stop()
            seen.append(should_stop())
            return self._result(service, user_id, stopped=True)

        worker = self._worker([("fa", "1"), ("fa", "2")])
        out = self._run(worker, _index)
        self.assertEqual(seen, [False, True])
        # The second job is never started once the flag is set.
        self.assertEqual(out["indexer"].call_count, 1)
        self.assertEqual(out["finished"], [True])

    def test_a_stop_before_the_run_starts_indexes_nothing(self):
        worker = self._worker([("fa", "1")])
        worker.request_stop()
        out = self._run(worker, lambda *a, **kw: self._result())
        out["indexer"].assert_not_called()
        self.assertEqual(out["results"], [])
        self.assertEqual(out["finished"], [True])

    def test_a_crash_mid_batch_still_emits_finished_all(self):
        """"A background tool must not be able to take the app down" - and
        the half of that which is easy to get wrong is the finally: without
        it the dialog's progress bar never stops and the Close button
        stays disabled."""
        worker = self._worker([("fa", "1"), ("fa", "2")])
        out = self._run(worker, lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("pawchive changed its API again")))
        self.assertEqual(out["results"], [])
        self.assertEqual(out["finished"], [True])

    def test_a_crash_after_one_success_keeps_the_result_already_reported(self):
        calls = {"n": 0}

        def _index(index, service, user_id, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return self._result(service, user_id, posts=7)

        worker = self._worker([("fa", "1"), ("fa", "2"), ("fa", "3")])
        out = self._run(worker, _index)
        self.assertEqual([r.user_id for r in out["results"]], ["1"])
        self.assertEqual(out["finished"], [True])

    def test_the_job_list_is_copied_so_the_caller_cannot_mutate_it_mid_run(self):
        jobs = [("fa", "1")]
        worker = self._worker(jobs)
        jobs.append(("fa", "2"))
        out = self._run(worker, lambda *a, **kw: self._result())
        self.assertEqual(out["indexer"].call_count, 1)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestPawchiveFindWorker(unittest.TestCase):
    """The artist search half - one signal carrying (results, error), so a
    failure has to arrive as text rather than as a silent empty list."""

    def _run(self, query, find, cache_path="/tmp/hatate-dan49-creators.json"):
        from workers.pawchive_index_worker import PawchiveFindWorker
        worker = PawchiveFindWorker(query, cache_path=cache_path)
        emitted = []
        worker.done.connect(lambda found, error: emitted.append((found, error)))
        with patch("workers.pawchive_index_worker.find_creators",
                   side_effect=find) as finder:
            worker.run()
        return emitted, finder

    def test_results_are_emitted_with_no_error(self):
        creators = [{"name": "somebody", "service": "fa", "id": "1"}]
        emitted, finder = self._run("some", lambda q, cache_path=None: creators)
        self.assertEqual(emitted, [(creators, None)])
        finder.assert_called_once_with("some", cache_path="/tmp/hatate-dan49-creators.json")

    def test_a_failure_is_emitted_as_text_beside_an_empty_list(self):
        """A bare empty list would read in the dialog as "no such artist",
        which is a different and wrong answer from "the 15 MB creator list
        could not be downloaded"."""
        emitted, _finder = self._run("some", lambda q, cache_path=None: (
            _ for _ in ()).throw(RuntimeError("connection reset")))
        found, error = emitted[0]
        self.assertEqual(found, [])
        self.assertEqual(error, "connection reset")

    def test_no_matches_is_an_empty_list_and_still_no_error(self):
        emitted, _finder = self._run("nobody", lambda q, cache_path=None: [])
        self.assertEqual(emitted, [([], None)])


if __name__ == "__main__":
    unittest.main()
