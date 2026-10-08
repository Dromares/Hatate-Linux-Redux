"""poll_single_url_import - confirming Hydrus's downloader actually
finished, rather than merely accepted the URL (DAN-49).

extract_confirmed_hash was already covered (tests/test_urls_and_availability
.py). The polling LOOP around it - lines 68-88, the only uncovered region
in the module at c087466 - was not, and it is the half with the
interesting failure modes: it blocks a real thread for up to a minute, it
has to keep going through a Hydrus error rather than give up on the first
one, and it must be stoppable.

POST /add_urls/add_url answers "accepted for the downloader queue", which
is NOT an import. Treating it as one is the bug this whole module exists
to prevent - see the `hydrus_import_confirmed` REGRESSION line in
tests/test_session.py.

`time.sleep` and `time.monotonic` are both stubbed, so nothing here waits.
The clock is driven by hand, which also makes the timeout arithmetic
observable instead of a wall-clock race.
"""
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from core.hydrus_client import HydrusError
from core.hydrus_import_poll import (
    DEFAULT_INTERVAL, DEFAULT_TIMEOUT, RESOLVED_STATUSES, poll_single_url_import,
)

URL = "http://example.com/post/1"
HASH = "aa" * 32


def _confirmed(status=2, file_hash=HASH):
    return {"url_file_statuses": [{"status": status, "hash": file_hash}]}


def _in_progress():
    return {"url_file_statuses": []}


class _Clock:
    """A monotonic clock that advances only when something sleeps, so the
    loop's own timeout arithmetic is what is under test."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Poll(unittest.TestCase):
    def _run(self, responses, **kwargs):
        """`responses` is a list of get_url_files return values / exceptions."""
        client = MagicMock()
        client.get_url_files.side_effect = responses
        clock = _Clock()
        with patch("core.hydrus_import_poll.time.monotonic", clock.monotonic), \
             patch("core.hydrus_import_poll.time.sleep", clock.sleep):
            result = poll_single_url_import(client, URL, **kwargs)
        return result, client, clock


class TestConfirmation(_Poll):
    def test_an_already_confirmed_import_returns_at_once_without_sleeping(self):
        result, client, clock = self._run([_confirmed()])
        self.assertEqual(result, HASH)
        client.get_url_files.assert_called_once_with(URL)
        self.assertEqual(clock.slept, [])

    def test_it_keeps_checking_until_hydrus_confirms(self):
        result, client, clock = self._run(
            [_in_progress(), _in_progress(), _confirmed()], interval=2.0, timeout=60.0)
        self.assertEqual(result, HASH)
        self.assertEqual(client.get_url_files.call_count, 3)
        self.assertEqual(clock.slept, [2.0, 2.0])

    def test_both_resolved_statuses_count_as_resolved(self):
        """2 = already in database (imported and present). 3 = previously
        deleted, so Hydrus HAS resolved the URL to a real file and is not
        still working on it. Either way the wait is over."""
        self.assertEqual(RESOLVED_STATUSES, (2, 3))
        for status in RESOLVED_STATUSES:
            with self.subTest(status=status):
                result, _client, _clock = self._run([_confirmed(status=status)])
                self.assertEqual(result, HASH)

    def test_status_zero_is_not_a_confirmation(self):
        """"not in database, ready for import" is documented as a rare
        transient state, not a "still downloading" signal - and certainly
        not an import. Treating it as one is how an unimported file gets
        recorded as confirmed."""
        result, client, _clock = self._run(
            [{"url_file_statuses": [{"status": 0, "hash": HASH}]}, _confirmed()],
            interval=0.5)
        self.assertEqual(result, HASH)
        self.assertEqual(client.get_url_files.call_count, 2)

    def test_a_resolved_status_with_no_hash_is_not_a_confirmation(self):
        """The hash IS the deliverable - the caller writes it onto the entry
        as hydrus_hash. A confirmation without one would set None."""
        result, _client, _clock = self._run(
            [{"url_file_statuses": [{"status": 2, "hash": None}]}], timeout=0.0)
        self.assertIsNone(result)


class TestTimeout(_Poll):
    def test_it_gives_up_after_the_timeout_rather_than_blocking_forever(self):
        result, client, clock = self._run(
            [_in_progress()] * 10, timeout=4.0, interval=2.0)
        self.assertIsNone(result)
        # t=0 check, sleep 2 -> t=2 check, sleep 2 -> t=4 check, remaining
        # 0 -> stop. Three checks, two sleeps.
        self.assertEqual(client.get_url_files.call_count, 3)
        self.assertEqual(clock.slept, [2.0, 2.0])

    def test_the_final_sleep_never_overshoots_the_timeout(self):
        """min(interval, remaining) is what keeps a 2s interval from
        turning a 5s timeout into 6s of blocking - which matters because
        this blocks SearchWorker's thread and the user is watching a
        countdown driven by the same numbers."""
        result, _client, clock = self._run(
            [_in_progress()] * 10, timeout=5.0, interval=2.0)
        self.assertIsNone(result)
        self.assertEqual(clock.slept, [2.0, 2.0, 1.0])
        self.assertEqual(sum(clock.slept), 5.0)

    def test_a_zero_timeout_still_checks_once_before_giving_up(self):
        """Worth one request: Hydrus may already have the file, in which
        case there is nothing to wait for."""
        result, client, clock = self._run([_in_progress()], timeout=0.0)
        self.assertIsNone(result)
        client.get_url_files.assert_called_once_with(URL)
        self.assertEqual(clock.slept, [])

    def test_a_confirmation_on_the_very_last_check_is_still_returned(self):
        result, _client, _clock = self._run(
            [_in_progress(), _confirmed()], timeout=2.0, interval=2.0)
        self.assertEqual(result, HASH)

    def test_the_defaults_are_the_ones_shared_with_the_batch_worker(self):
        """workers/hydrus_import_poll_worker.py imports these same two
        names so the manual action and the automatic one cannot drift."""
        self.assertEqual((DEFAULT_TIMEOUT, DEFAULT_INTERVAL), (60.0, 2.0))
        result, client, clock = self._run([_in_progress()] * 40)
        self.assertIsNone(result)
        self.assertEqual(sum(clock.slept), DEFAULT_TIMEOUT)
        self.assertEqual(set(clock.slept), {DEFAULT_INTERVAL})


class TestProgress(_Poll):
    def test_progress_reports_the_remaining_seconds_against_the_timeout(self):
        """Without this the caller has no way to tell "actively working"
        from "stalled" while the function blocks."""
        reported = []
        result, _client, _clock = self._run(
            [_in_progress()] * 10, timeout=6.0, interval=2.0,
            on_progress=lambda remaining, timeout: reported.append((remaining, timeout)))
        self.assertIsNone(result)
        self.assertEqual(reported, [(6.0, 6.0), (4.0, 6.0), (2.0, 6.0), (0.0, 6.0)])

    def test_remaining_never_goes_negative(self):
        """It is rendered straight into a countdown label."""
        reported = []
        self._run([_in_progress()] * 5, timeout=1.0, interval=10.0,
                  on_progress=lambda remaining, timeout: reported.append(remaining))
        self.assertTrue(all(r >= 0.0 for r in reported), reported)

    def test_nothing_is_reported_once_the_import_is_confirmed(self):
        """A countdown tick after the answer arrived would redraw a
        progress bar for work that is already done."""
        reported = []
        result, _client, _clock = self._run(
            [_confirmed()], on_progress=lambda *a: reported.append(a))
        self.assertEqual(result, HASH)
        self.assertEqual(reported, [])

    def test_no_progress_callback_is_a_supported_way_to_call_it(self):
        result, _client, _clock = self._run([_in_progress()], timeout=0.0)
        self.assertIsNone(result)


class TestStopAndErrors(_Poll):
    def test_a_stop_before_the_first_check_asks_hydrus_nothing(self):
        result, client, clock = self._run([_confirmed()], stop_check=lambda: True)
        self.assertIsNone(result)
        client.get_url_files.assert_not_called()
        self.assertEqual(clock.slept, [])

    def test_a_stop_part_way_through_returns_none_immediately(self):
        stop = {"now": False}
        client = MagicMock()

        def _files(url):
            stop["now"] = True
            return _in_progress()

        client.get_url_files.side_effect = _files
        clock = _Clock()
        with patch("core.hydrus_import_poll.time.monotonic", clock.monotonic), \
             patch("core.hydrus_import_poll.time.sleep", clock.sleep):
            result = poll_single_url_import(client, URL, timeout=60.0, interval=1.0,
                                           stop_check=lambda: stop["now"])
        self.assertIsNone(result)
        self.assertEqual(client.get_url_files.call_count, 1)

    def test_a_stop_is_not_reported_as_a_failed_import(self):
        """Both a stop and a timeout answer None, and the docstring is
        explicit that None "isn't necessarily a failure" - the import may
        still be genuinely in progress. Callers must not write an error
        onto the entry from this."""
        result, _client, _clock = self._run([], stop_check=lambda: True)
        self.assertIsNone(result)

    def test_a_hydrus_error_is_retried_rather_than_ending_the_poll(self):
        """The likely cause is Hydrus being momentarily busy with the very
        import being waited on. Giving up on the first error would report
        a perfectly good import as unconfirmed."""
        result, client, _clock = self._run(
            [HydrusError("connection reset"), HydrusError("HTTP 503", 503), _confirmed()],
            interval=0.1)
        self.assertEqual(result, HASH)
        self.assertEqual(client.get_url_files.call_count, 3)

    def test_errors_all_the_way_to_the_timeout_answer_none(self):
        result, _client, clock = self._run(
            [HydrusError("nope")] * 10, timeout=2.0, interval=1.0)
        self.assertIsNone(result)
        self.assertEqual(clock.slept, [1.0, 1.0])

    def test_a_non_hydrus_exception_is_not_swallowed(self):
        """Only HydrusError is a "Hydrus is busy, try again" signal. A
        TypeError from our own code being retried for 60 seconds and then
        reported as "not confirmed in time" would hide a real bug."""
        with self.assertRaises(TypeError):
            self._run([TypeError("a bug, not a busy server")])


if __name__ == "__main__":
    unittest.main()
