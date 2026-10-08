"""The availability verdict has to survive a restart, without ever
turning "nobody checked" into "confirmed gone".

"Check Match Availability" spends one HTTP request per candidate, and the
search-time sweep spends the same again on every candidate it has no
answer for. The answer used to be dropped by the candidate codec, so
every launch re-bought every verdict.

Saving it introduces the opposite hazard, which is the more dangerous
one: `remote_available` is tri-state, and `False` deletes matches.
Everything downstream - core/ranking.py, ImageEntry.
drop_unavailable_candidates, core/tag_borrowing.py - is written so that
only an explicit `False` means gone, so a missing or unreadable stored
value must come back as `None`, never as a bool.
"""
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from core.config import Settings
from core.models import ImageEntry, MatchCandidate
from core.search_cache import (
    AVAILABILITY_VERDICT_TTL_DAYS, _candidate_from_dict, _candidate_to_dict,
)

URL = "https://danbooru.donmai.us/posts/1"


def _roundtrip(candidate: MatchCandidate) -> MatchCandidate:
    """Through JSON, not just the dicts: the cache and the session are
    both files, and a value that cannot be encoded would sail through a
    dict-only test."""
    return _candidate_from_dict(json.loads(json.dumps(_candidate_to_dict(candidate))))


def _checked(available, age_days: float = 0.0) -> MatchCandidate:
    c = MatchCandidate(url=URL, similarity=90.0)
    c.record_availability(available)
    if c.availability_checked_at is not None and age_days:
        c.availability_checked_at -= age_days * 86400
    return c


class TestRecordAvailability(unittest.TestCase):
    """The verdict and its date are written together so they cannot
    disagree - an undated verdict is one the expiry check cannot reason
    about."""

    def test_a_definite_verdict_is_dated(self):
        for verdict in (True, False):
            c = _checked(verdict)
            self.assertIs(c.remote_available, verdict)
            self.assertIsNotNone(c.availability_checked_at)
            self.assertAlmostEqual(c.availability_checked_at, time.time(), delta=60)

    def test_an_inconclusive_check_clears_the_date(self):
        """A timeout leaves the candidate in exactly the state of one
        nobody ever checked, so it must not carry a date either."""
        c = _checked(True)
        c.record_availability(None)
        self.assertIsNone(c.remote_available)
        self.assertIsNone(c.availability_checked_at)


class TestTheVerdictSurvivesARestart(unittest.TestCase):
    """One codec, so the search cache and the session store both get
    this - the same shape as the similarity_measured fix."""

    def test_a_live_verdict_is_still_live_after_a_reload(self):
        self.assertIs(_roundtrip(_checked(True)).remote_available, True)

    def test_a_gone_verdict_is_still_gone_after_a_reload(self):
        self.assertIs(_roundtrip(_checked(False)).remote_available, False)

    def test_the_date_survives_too(self):
        original = _checked(True)
        self.assertAlmostEqual(
            _roundtrip(original).availability_checked_at,
            original.availability_checked_at, places=3,
        )

    def test_a_restored_gone_match_is_still_dropped(self):
        """The point of saving the verdict: with drop_dead_matches off, a
        confirmed-gone match is kept WITH its verdict, and used to come
        back after a restart looking live."""
        entry = ImageEntry(path="/x.jpg")
        entry.candidates = [_roundtrip(_checked(True)), _roundtrip(_checked(False))]
        self.assertEqual(entry.drop_unavailable_candidates(), 1)
        self.assertEqual([c.remote_available for c in entry.candidates], [True])

    def test_a_restriction_reason_survives(self):
        """A gated post (Danbooru Gold) is not a deleted one, and this is
        the only thing that says which."""
        c = _checked(False)
        c.restricted = "restricted on Danbooru - viewing it needs a Gold account"
        self.assertEqual(_roundtrip(c).restricted, c.restricted)


class TestUnknownNeverCollapsesToGone(unittest.TestCase):
    """The trap. `False` deletes matches, so every path that cannot
    produce a real verdict has to produce `None`."""

    def test_an_unchecked_candidate_reloads_as_unchecked(self):
        restored = _roundtrip(MatchCandidate(url=URL, similarity=90.0))
        self.assertIsNone(restored.remote_available)
        self.assertIsNone(restored.availability_checked_at)

    def test_an_entry_written_before_the_verdict_was_saved_reads_as_unknown(self):
        """The honest reading, and the pre-existing behaviour: such an
        entry never held a verdict, so it gets re-checked."""
        self.assertIsNone(_candidate_from_dict({"url": URL, "similarity": 88.0}).remote_available)

    def test_a_stored_null_reads_as_unknown(self):
        self.assertIsNone(_candidate_from_dict(
            {"url": URL, "remote_available": None, "availability_checked_at": time.time()},
        ).remote_available)

    def test_an_undated_verdict_is_discarded(self):
        """Not kept as an unbounded verdict - that is the thing the
        expiry window exists to prevent, and unknown costs one HEAD."""
        for verdict in (True, False):
            self.assertIsNone(_candidate_from_dict(
                {"url": URL, "remote_available": verdict},
            ).remote_available)

    def test_a_non_bool_verdict_is_discarded(self):
        """A hand-edited or corrupt file must not be able to talk the
        codec into a truthiness test."""
        for junk in ("gone", 0, 1, [], {}):
            self.assertIsNone(_candidate_from_dict(
                {"url": URL, "remote_available": junk,
                 "availability_checked_at": time.time()},
            ).remote_available)


class TestTheVerdictExpires(unittest.TestCase):
    """In both directions. A site can restore a deleted post, and a live
    link can die - before this was saved at all, every launch re-swept
    and so noticed both."""

    def test_a_fresh_verdict_is_trusted(self):
        for verdict in (True, False):
            self.assertIs(
                _roundtrip(_checked(verdict, age_days=AVAILABILITY_VERDICT_TTL_DAYS - 1))
                .remote_available,
                verdict,
            )

    def test_a_stale_verdict_reads_as_unknown(self):
        for verdict in (True, False):
            restored = _roundtrip(_checked(verdict, age_days=AVAILABILITY_VERDICT_TTL_DAYS + 1))
            self.assertIsNone(restored.remote_available)
            self.assertIsNone(restored.availability_checked_at)


class TestTheSweepStopsRebuyingAnsweredVerdicts(unittest.TestCase):
    """What the persistence is actually for: core/availability.py's sweep
    already short-circuits on a known verdict, so a restored one makes
    the re-check free."""

    def _sweep(self, candidates):
        from core import availability
        with patch.object(availability, "check_url_available",
                          MagicMock(return_value=True)) as check:
            availability._sweep_candidate_availability(candidates, Settings(), "x.jpg")
        return check

    def test_a_restored_verdict_costs_no_request(self):
        check = self._sweep([_roundtrip(_checked(True)), _roundtrip(_checked(False))])
        check.assert_not_called()

    def test_an_unknown_verdict_is_still_checked(self):
        """The other half - expiry and a first run both have to reach the
        network, or the sweep would never answer anything."""
        check = self._sweep([
            _roundtrip(MatchCandidate(url=URL, similarity=90.0)),
            _roundtrip(_checked(False, age_days=AVAILABILITY_VERDICT_TTL_DAYS + 1)),
        ])
        self.assertEqual(check.call_count, 2)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before PyQt6, or Qt wants a display
try:
    from PyQt6.QtCore import QThread  # noqa: F401 - import probe only
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestTheManualCheckStillOverwrites(unittest.TestCase):
    """Saving the verdict must not make "Check Match Availability"
    a no-op. The user asking again is a request for what the site says
    now, however recent the saved answer is."""

    def _recheck(self, candidate, answer):
        from workers.availability_worker import AvailabilityWorker
        entry = ImageEntry(path="/x.jpg")
        entry.candidates = [candidate]
        worker = AvailabilityWorker(entry, Settings())
        # run() called directly rather than start()ed: it needs no event
        # loop, and this asserts on the candidate rather than the signal.
        with patch("workers.availability_worker.check_url_available",
                   MagicMock(return_value=answer)):
            worker.run()
        return candidate

    def test_a_saved_live_verdict_can_be_overwritten_with_gone(self):
        rechecked = self._recheck(_roundtrip(_checked(True)), False)
        self.assertIs(rechecked.remote_available, False)

    def test_a_saved_gone_verdict_can_be_overwritten_with_live(self):
        """A restored post stops being a dead link the moment the user
        asks again - it does not need the expiry window to elapse."""
        rechecked = self._recheck(_roundtrip(_checked(False)), True)
        self.assertIs(rechecked.remote_available, True)

    def test_an_overwrite_redates_the_verdict(self):
        old = _roundtrip(_checked(False, age_days=5))
        before = old.availability_checked_at
        self.assertGreater(self._recheck(old, True).availability_checked_at, before)


if __name__ == "__main__":
    unittest.main()
