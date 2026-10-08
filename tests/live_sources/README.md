# Live-source checks

Everything in `tests/` proper is deliberately network-free (see
`tests/README.md`: "No network. Every HTTP interaction is mocked."). That
rule is correct for a CI gate, but it also means the suite can go green
forever while a real site, a third-party proxy, or a local dependency
silently changes shape underneath the code that depends on it - DAN-83
for a site (anime-pictures started 403ing this project's CI runner;
nothing in `tests/` could have noticed, because nothing in `tests/` ever
asks the real site anything), DAN-56/57 for a proxy (a real, successful,
non-empty response that was simply the wrong photo), DAN-79 for a local
dependency (Google Lens failing silently when Playwright/Chromium are
absent).

This directory hits real, live subjects with the app's own production
code paths - `core.boorus.fetch_page_info`, `core.google_lens.search`,
`core.lens_browser` - and reports what actually happened. Nothing here
is mocked or patched; a check that needs a degraded environment (the
Google Lens one) uses this runner's own real, currently-missing
Playwright install rather than faking the absence.

## Running it

One entry point runs every check, or one by name:

```bash
python3 -m tests.live_sources.run_checks                      # every check
python3 -m tests.live_sources.run_checks twitter               # one, by name
python3 -m tests.live_sources.run_checks twitter anime_pictures # more than one
python3 -m tests.live_sources.run_checks --list                 # what's available
```

Each check module is also runnable directly (`python3 -m
tests.live_sources.check_twitter`), which is useful while developing a
single check, but `run_checks` is the normal way to invoke this suite.

## Rules

- **Never a CI gate, never run by `run_tests.sh`.** Files here do not
  start with `test_`, so `unittest discover`'s default `test*.py` pattern
  - which both `run_tests.sh` and `.github/workflows/tests.yml` use -
  never finds them. A transient 403, a rate limit, or a site outage must
  never fail a build for a code change that has nothing to do with it.
- **Run by hand or on a schedule, not on every push.** `run_checks`
  exits non-zero on anything other than a clean pass (see Outcomes
  below), so a cron/routine can alert on it without a human reading
  output every time - but should alert differently on a belief breaking
  than on a source being unavailable, which is exactly why those are
  reported separately rather than collapsed into one exit code.
- **Read-only against third-party sites.** These only GET public pages or
  call real local code paths; nothing here writes anywhere.
- **A BELIEF BROKEN here means "go look at this parser, proxy, or
  dependency gate"**, not "revert the last commit" - source rot is not
  something a diff caused.

## Outcomes

Every check is a small set of independently-reported beliefs (see
`harness.py`), each resolving to exactly one of three outcomes - never a
plain pass/fail:

- **PASS** - the belief held, checked against the real subject.
- **BELIEF BROKEN** - the subject answered cleanly and the answer
  contradicts what we assumed. This is the one this suite exists to
  surface.
- **SOURCE UNAVAILABLE** - the subject could not be reached, is rate
  limiting this runner, or (for a pinned real-world subject, like a
  specific tweet) has gone. This says nothing about whether the belief is
  still correct, so it must never be read as a pass *or* as a broken
  belief - and a check must never claim it by inferring "any exception
  means down"; it has to recognise a real signal (a connection error, a
  specific HTTP status, a documented "not found" response) first. Absence
  of an error is not a pass either: a check that cannot reach its subject
  reports unavailable, it never silently succeeds.

`run_checks` prints each belief's outcome as it runs, then one summary
line per check, then exits non-zero unless every belief in every check
requested came back PASS.

## What's here

- `check_anime_pictures.py` (DAN-83) - is the post still reachable with
  its known tags and dimensions, or 403ing/changed-shape.
- `check_google_lens_dependency.py` (DAN-79/80) - with Playwright
  genuinely absent (this runner's permanent state), does the real code
  path refuse loudly and tell the user, or fail the silent DAN-79 way.
  The one check here that needs no network and can never be "the site is
  down" - its subject is this runner's own environment.
- `check_twitter.py` (DAN-56/57) - for a pinned, known multi-photo tweet,
  does `api.fxtwitter.com` still answer with the same media layout, and
  does this app still resolve the *correct* photo of several - not just
  *a* photo. Split into the proxy's own JSON envelope, Twitter's media
  layout underneath it, and the index-to-photo mapping, so a failure
  names which of those three broke.
- `check_rule34us.py` (DAN-59/61/67/68) - does the URL this app rebuilds
  from a Google Lens result title still resolve to a real rule34.us
  page, and is the "Original" file link that page's own markup hands
  back actually fetchable - not just present as a string. Every escape
  this site has produced was a URL/download-SHAPE bug, never an empty
  tag parse, so tag presence is the smaller half of this check, not the
  headline belief.
- `check_hydrus.py` (DAN-193) - read-only. Does the local Hydrus Client
  API still answer the endpoints this app parses (`/get_services`,
  `/get_files/search_files`, `/get_files/file_metadata`,
  `/add_urls/get_url_info`, `/add_urls/get_url_files`) with the exact
  JSON field names and types `core/hydrus_client.py` depends on. The
  connection/auth halves are already loud elsewhere and are only
  asserted cheaply here, as a gate; every other belief is about shape
  drift under a 200, the one half of this contract nothing else in the
  codebase has ever checked against a real client. No escaped defect
  behind this one (yet) - it earns its place on blast radius alone:
  every import and every tag write goes through this contract.

## Adding a check

Build one on `harness.Check`: give it a name, register each belief with
`@CHECK.belief("short, specific description")`, have the belief function
return a short PASS detail string, raise a plain `assert` (or
`AssertionError`) when the belief is wrong, or raise
`harness.SourceUnavailable` when a recognised signal says the subject -
not the belief - is the problem. Keep each belief narrow enough that its
name alone tells a reader what broke without reading the body. If
several beliefs read from one fetch, wrap the fetch in
`harness.memoize_once` so they share it instead of hitting the subject
once per belief. Register the new check's `CHECK` object in
`run_checks.py`'s `CHECKS` dict.
