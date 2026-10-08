"""Match ranking: recommending the most USEFUL match, not merely the
most similar.

The dropdown's first entry is selected by default, so it decides which
match a user actually gets. Sorting purely by similarity means a match
on a site this app cannot read - contributing no tags, no file URL and
no dimensions - can be recommended over one that would have supplied
sixty tags and four times the resolution, on the strength of half a
percentage point.
"""
import unittest

from . import _path  # noqa: F401
from core.models import MatchCandidate
from core.ranking import (
    MAX_QUALITY_BONUS, candidate_score, quality_bonus, rank_candidates, score_breakdown,
)

LOCAL = (1000, 1500)

PARSEABLE = "https://danbooru.donmai.us/posts/1"
ALSO_PARSEABLE = "https://gelbooru.com/index.php?page=post&s=view&id=1"
NO_PARSER = "https://chan.sankakucomplex.com/en/posts/1"


def candidate(url, similarity, width=None, height=None, available=None):
    match = MatchCandidate(url=url, similarity=similarity)
    match.width, match.height = width, height
    match.remote_available = available
    return match


class TestSimilarityStaysDominant(unittest.TestCase):
    """The whole design rests on this: recommending the WRONG picture is
    far worse than recommending a correct one from a mediocre source."""

    def test_a_clearly_better_similarity_is_never_overturned(self):
        ranked = rank_candidates(
            [candidate(NO_PARSER, 98.0), candidate(PARSEABLE, 92.0, 4000, 6000)], *LOCAL)
        self.assertIn("sankaku", ranked[0].url)

    def test_the_bonus_is_bounded(self):
        """However good a source is, it cannot invent similarity."""
        best_possible = candidate(PARSEABLE, 50.0, 99999, 99999)
        self.assertLessEqual(candidate_score(best_possible, *LOCAL), 50.0 + MAX_QUALITY_BONUS)

    def test_bonus_never_exceeds_the_cap_for_any_input(self):
        for width, height in ((2000, 3000), (40000, 60000), (1, 1)):
            with self.subTest(size=(width, height)):
                self.assertLessEqual(
                    quality_bonus(candidate(PARSEABLE, 90.0, width, height), *LOCAL),
                    MAX_QUALITY_BONUS)


class TestQualityBreaksNearTies(unittest.TestCase):
    def test_a_readable_site_wins_a_near_tie(self):
        ranked = rank_candidates(
            [candidate(NO_PARSER, 98.0), candidate(PARSEABLE, 97.5, 4000, 6000)], *LOCAL)
        self.assertIn("donmai", ranked[0].url)

    def test_resolution_decides_between_equally_readable_sites(self):
        ranked = rank_candidates(
            [candidate(ALSO_PARSEABLE, 98.0, 1000, 1500),
             candidate(PARSEABLE, 98.0, 4000, 6000)], *LOCAL)
        self.assertIn("donmai", ranked[0].url)

    def test_a_smaller_match_earns_no_resolution_bonus(self):
        smaller = quality_bonus(candidate(PARSEABLE, 98.0, 500, 750), *LOCAL)
        larger = quality_bonus(candidate(PARSEABLE, 98.0, 4000, 6000), *LOCAL)
        self.assertLess(smaller, larger)


class TestUnknownsAreNotPenalised(unittest.TestCase):
    """Only some engines report dimensions. Marking a candidate down for
    its engine's reporting habits would rank by provenance, not quality."""

    def test_missing_dimensions_score_the_same_as_equal_dimensions(self):
        unknown = quality_bonus(candidate(PARSEABLE, 98.0), *LOCAL)
        same_size = quality_bonus(candidate(PARSEABLE, 98.0, 1000, 1500), *LOCAL)
        self.assertEqual(unknown, same_size)

    def test_missing_local_dimensions_are_handled(self):
        self.assertIsInstance(quality_bonus(candidate(PARSEABLE, 98.0, 4000, 6000), None, None), float)


class TestDeadMatchesSink(unittest.TestCase):
    def test_confirmed_dead_ranks_below_everything(self):
        ranked = rank_candidates(
            [candidate(PARSEABLE, 99.0, 4000, 6000, available=False),
             candidate(ALSO_PARSEABLE, 80.0)], *LOCAL)
        self.assertIs(ranked[-1].remote_available, False)

    def test_unknown_availability_is_not_treated_as_dead(self):
        """A timeout must not demote a perfectly good match."""
        ranked = rank_candidates(
            [candidate(PARSEABLE, 99.0, 4000, 6000, available=None),
             candidate(ALSO_PARSEABLE, 80.0)], *LOCAL)
        self.assertIn("donmai", ranked[0].url)


class TestScoreBreakdown(unittest.TestCase):
    """score_breakdown() exists so a caller that cannot read this
    module's comments - the MCP get_candidates tool - can still SAY why
    one candidate outranked another (DAN-707)."""

    def test_the_total_matches_candidate_score(self):
        c = candidate(PARSEABLE, 92.0, 4000, 6000)
        breakdown = score_breakdown(c, *LOCAL)
        self.assertEqual(breakdown["score"], candidate_score(c, *LOCAL))

    def test_a_parseable_site_reports_its_bonus(self):
        breakdown = score_breakdown(candidate(PARSEABLE, 90.0), *LOCAL)
        self.assertTrue(breakdown["has_parser"])
        self.assertGreater(breakdown["parser_bonus"], 0.0)

    def test_an_unparseable_site_reports_none(self):
        # NO_PARSER is sankaku, which now has one (just not one that can
        # fetch tags without cookies) - a genuinely unrecognised domain
        # is what find_parser() actually returns None for.
        breakdown = score_breakdown(candidate("https://unknown.example/post/1", 90.0), *LOCAL)
        self.assertFalse(breakdown["has_parser"])
        self.assertEqual(breakdown["parser_bonus"], 0.0)

    def test_the_quality_bonus_is_capped_the_same_as_candidate_score_uses(self):
        breakdown = score_breakdown(candidate(PARSEABLE, 90.0, 8000, 12000), *LOCAL)
        self.assertEqual(breakdown["quality_bonus_cap"], MAX_QUALITY_BONUS)
        self.assertLessEqual(breakdown["quality_bonus"], MAX_QUALITY_BONUS)

    def test_a_dead_match_reports_the_penalty_was_applied(self):
        breakdown = score_breakdown(candidate(PARSEABLE, 99.0, available=False), *LOCAL)
        self.assertTrue(breakdown["unavailable_penalty_applied"])
        self.assertGreater(breakdown["unavailable_penalty"], 0.0)

    def test_an_available_match_reports_no_penalty(self):
        breakdown = score_breakdown(candidate(PARSEABLE, 99.0, available=True), *LOCAL)
        self.assertFalse(breakdown["unavailable_penalty_applied"])
        self.assertEqual(breakdown["unavailable_penalty"], 0.0)


class TestOrderingIsStable(unittest.TestCase):
    def test_genuine_ties_keep_their_original_order(self):
        """Otherwise the recommendation would shuffle between runs for no
        visible reason."""
        first = candidate(PARSEABLE, 98.0, 2000, 3000)
        second = candidate(PARSEABLE + "?x", 98.0, 2000, 3000)
        ranked = rank_candidates([first, second], *LOCAL)
        self.assertIs(ranked[0], first)

    def test_a_single_candidate_is_returned_unchanged(self):
        only = [candidate(PARSEABLE, 98.0)]
        self.assertIs(rank_candidates(only, *LOCAL), only)

    def test_empty_list(self):
        self.assertEqual(rank_candidates([], *LOCAL), [])


if __name__ == "__main__":
    unittest.main()
