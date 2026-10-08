"""Shared machinery for live-source checks.

Everything in `tests/` proper is network-free by design (see
`tests/README.md`), which is correct for a CI gate but means the suite
can go green forever while a real site, proxy, or local dependency
quietly changes shape underneath the code that depends on it - DAN-83
for a site, DAN-56/57 for a third-party proxy, DAN-79 for a local
dependency. This module is the common shape every check in this
directory is built from, so a failure here always says the same three
things: what we believed, what we saw instead, and whether that is even
this check's fault to report.

Three outcomes, never two:

  * PASS - the belief held.
  * BELIEF_BROKEN - we got a clean answer from the subject and it
    contradicts what we assumed. This is the one this suite exists to
    surface: our own code or our own assumption about an external
    contract is now wrong.
  * SOURCE_UNAVAILABLE - the subject couldn't be reached, is rate
    limiting us, or (for a pinned real-world subject like a tweet) has
    gone - none of which says anything about whether our belief is
    still correct. Raised deliberately by a check, from a positive
    signal it recognised (a connection error, a specific HTTP status, a
    documented "not found" shape) - never inferred by a bare `except
    Exception` guessing that any failure must mean "down". A silent
    catch-all here would let a real break hide behind a plausible-looking
    "the site must be down today".

A check that cannot reach its subject must report SOURCE_UNAVAILABLE and
nothing else - never a silent PASS (that is how DAN-83 escaped) and
never BELIEF_BROKEN (that would send someone to fix a parser that is
not the problem).
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Callable, List


class Outcome(Enum):
    PASS = "PASS"
    BELIEF_BROKEN = "BELIEF BROKEN"
    SOURCE_UNAVAILABLE = "SOURCE UNAVAILABLE"


class SourceUnavailable(Exception):
    """Raise from inside a belief function to report the SUBJECT, not our
    code, as the problem. See the module docstring - this must be raised
    deliberately from a recognised signal, never inferred from a bare
    except.

    `evidence` carries whatever justified the call: a status code, a
    response body, a connection-error message.
    """

    def __init__(self, detail: str, evidence: str = ""):
        super().__init__(detail)
        self.detail = detail
        self.evidence = evidence


@dataclass
class Belief:
    name: str
    fn: Callable[[], str]


@dataclass
class BeliefReport:
    name: str
    outcome: Outcome
    detail: str
    evidence: str = ""

    def print(self) -> None:
        print(f"  [{self.outcome.value}] {self.name}: {self.detail}")
        if self.evidence:
            for line in self.evidence.rstrip().splitlines():
                print(f"      {line}")


def _run_belief(belief: Belief) -> BeliefReport:
    try:
        detail = belief.fn()
    except SourceUnavailable as exc:
        return BeliefReport(belief.name, Outcome.SOURCE_UNAVAILABLE, exc.detail, exc.evidence)
    except AssertionError as exc:
        # The ordinary way a check states "I looked, and the belief does
        # not hold" - a plain `assert belief, "what broke and what we saw"`.
        return BeliefReport(belief.name, Outcome.BELIEF_BROKEN, str(exc), "")
    except Exception as exc:  # noqa: BLE001 - *unexpected* is still BELIEF_BROKEN, not a pass
        # Anything else is itself news: a check author anticipated
        # connection errors and known "not found" shapes (both raised as
        # SourceUnavailable above) and ordinary wrong-value assertions.
        # Landing here means the subject answered with a shape nobody
        # anticipated at all, which is exactly the kind of silent drift
        # this suite exists to catch - so it is reported, with the full
        # traceback as evidence, rather than swallowed.
        return BeliefReport(
            belief.name, Outcome.BELIEF_BROKEN,
            f"unexpected {type(exc).__name__}: {exc}", traceback.format_exc(),
        )
    return BeliefReport(belief.name, Outcome.PASS, detail or "ok")


class Check:
    """One live-source check: a named subject plus the beliefs held about
    it. Each belief is declared, run, and reported independently, so a
    failure names exactly which belief broke rather than the check as a
    whole going red.
    """

    def __init__(self, name: str):
        self.name = name
        self._beliefs: List[Belief] = []

    def belief(self, name: str):
        """Decorator registering a belief. The wrapped function returns a
        short string describing what held (the PASS detail), or raises
        AssertionError / SourceUnavailable / anything else per the module
        docstring."""

        def register(fn: Callable[[], str]) -> Callable[[], str]:
            self._beliefs.append(Belief(name, fn))
            return fn

        return register

    def run(self) -> List[BeliefReport]:
        print(f"=== {self.name} ===")
        reports = [_run_belief(b) for b in self._beliefs]
        for report in reports:
            report.print()
        return reports


def overall(reports: List[BeliefReport]) -> Outcome:
    """The check's single headline outcome: a broken belief outranks an
    unavailable source, which outranks a clean pass - a real break is
    always worth surfacing even if another belief in the same check
    happened to hit a timeout."""
    if any(r.outcome is Outcome.BELIEF_BROKEN for r in reports):
        return Outcome.BELIEF_BROKEN
    if any(r.outcome is Outcome.SOURCE_UNAVAILABLE for r in reports):
        return Outcome.SOURCE_UNAVAILABLE
    return Outcome.PASS


def exit_code(reports: List[BeliefReport]) -> int:
    """0 only if every belief passed outright. A routine alerting on this
    alone cannot tell BELIEF_BROKEN from SOURCE_UNAVAILABLE - that
    distinction lives in the printed outcomes (see `overall`), which is
    what a human or a smarter caller reads instead of just the exit
    status."""
    return 0 if overall(reports) is Outcome.PASS else 1


def memoize_once(fn: Callable[[], object]) -> Callable[[], object]:
    """Cache fn's single outcome - success or exception - across calls.

    Several beliefs in the same check often read from one fetch (the
    anime-pictures and Twitter checks both do). Without this each belief
    would repeat the same live HTTP request, which is exactly the kind of
    needless load on a real third-party site this suite should not add -
    and worse, a transient blip could make one belief see
    SOURCE_UNAVAILABLE while another, refetching a moment later, saw a
    clean 200, reporting two different outcomes for what was really one
    event.
    """
    sentinel = object()
    state = {"value": sentinel, "error": None}

    @wraps(fn)
    def wrapper():
        if state["value"] is sentinel and state["error"] is None:
            try:
                state["value"] = fn()
            except Exception as exc:  # noqa: BLE001 - replayed verbatim below
                state["error"] = exc
        if state["error"] is not None:
            raise state["error"]
        return state["value"]

    return wrapper
