"""DAN-79/80 live-source check: with Playwright genuinely absent, does the
real Google Lens code path actually tell the user, or does it fail the
way DAN-79 did - silently, with every image in the batch looking
searched when Lens never ran at all?

Needs no network and nothing can be down for it: the "live source" here
is this runner's own Python environment, which project notes confirm is
PERMANENTLY missing Playwright and its Chromium build. That makes this
the one check in this suite that can assert the real, unpatched
degraded-dependency path at zero cost and with no flakiness from a third
party - see README.md in this directory for the never-a-gate rules this
still follows regardless.

DAN-80's regression test (tests/test_lens_dependency_alerts.py) patches
`lens_browser.playwright_available` / `chromium_installed` /
`fetch_results_payloads` to FAKE the degraded state - it proves the
alert code is internally self-consistent, not that the real code path
through a real absent import still reaches it. Nothing here is patched:
every belief below calls the real `core.lens_browser` /
`core.google_lens` functions exactly as a real search would, and the
first belief exists specifically to prove that this runner really is in
the state the rest of the check assumes - if Playwright ever becomes
importable here, that belief is what goes BELIEF_BROKEN first, rather
than the later ones silently testing nothing.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import _path  # noqa: F401

from core import engine_alerts, google_lens, lens_browser
from .harness import Check

CHECK = Check("google_lens_dependency")


@CHECK.belief("this runner has no Playwright (the documented degraded state)")
def _playwright_genuinely_absent() -> str:
    assert not lens_browser.playwright_available(), (
        "playwright_available() returned True - this runner is no longer missing "
        "Playwright, so the rest of this check is not exercising the real degraded "
        "path it assumes. Either Playwright was installed here, or this check needs "
        "retargeting at whichever dependency this environment is actually short of."
    )
    return "import playwright.sync_api fails, as expected"


@CHECK.belief("the real fetch path refuses to start, naming Playwright specifically")
def _fetch_raises_unavailable() -> str:
    try:
        lens_browser.fetch_results_payloads(b"not a real image", "probe.jpg", 1.0)
    except lens_browser.LensBrowserUnavailable as exc:
        assert exc.missing == lens_browser.MISSING_PLAYWRIGHT, (
            f"LensBrowserUnavailable.missing was {exc.missing!r}, expected "
            f"{lens_browser.MISSING_PLAYWRIGHT!r} - the dependency gate in "
            "core/lens_browser.py no longer identifies which piece is missing."
        )
        assert "pip install playwright" in str(exc), (
            f"the error message no longer names the fix. Got: {exc!r}"
        )
        return "LensBrowserUnavailable(missing='playwright') with its install command"
    else:
        raise AssertionError(
            "fetch_results_payloads returned instead of raising - with Playwright "
            "genuinely absent it should refuse before ever touching the dummy bytes "
            "given to it. This is the DAN-79 shape: a missing dependency that looks "
            "like an ordinary search that simply found nothing."
        )


@CHECK.belief("a real search surfaces a user-visible alert, not just a log line")
def _search_announces_to_the_user() -> str:
    """Exercises core.google_lens.search() end to end - the actual call a
    real image search makes - with a real temp JPEG on disk, and listens
    on the real core.engine_alerts channel gui/main_window.py subscribes
    to in production. Subscribing a plain function here is not a patch:
    it is the same extension point the GUI uses, just with this check
    standing in for the message box so the alert's content can be
    asserted on instead of only eyeballed.
    """
    from PIL import Image

    captured = []
    engine_alerts.reset()
    engine_alerts.subscribe(captured.append)
    try:
        with tempfile.TemporaryDirectory(prefix="hatate-lens-check-") as tmp:
            image_path = str(Path(tmp) / "probe.jpg")
            Image.new("RGB", (4, 4), color=(120, 60, 200)).save(image_path, format="JPEG")

            try:
                google_lens.search(image_path, timeout=1.0)
            except google_lens.GoogleLensUnavailableError:
                pass
            else:
                raise AssertionError(
                    "google_lens.search() returned instead of raising "
                    "GoogleLensUnavailableError, with Playwright genuinely absent."
                )
    finally:
        engine_alerts.unsubscribe(captured.append)

    assert captured, (
        "search() raised correctly but core.engine_alerts never delivered anything - "
        "this is exactly the DAN-79 failure mode: the engine stands down but nothing "
        "tells the user, so a batch finishes looking like Lens searched every image."
    )
    alert = captured[0]
    assert alert.key == "google-lens:playwright-not-installed", (
        f"alert key was {alert.key!r} - the once-per-session de-dup in "
        "core/engine_alerts.py keys on this string, and core/google_lens.py's "
        "ALERT_KEYS no longer agrees with it."
    )
    assert "Playwright package" in alert.body and "not installed" in alert.body, (
        f"alert body no longer names what is missing. Got: {alert.body!r}"
    )
    assert "pip install playwright" in alert.remedy, (
        f"alert remedy no longer carries the fix command. Got: {alert.remedy!r}"
    )
    return f"delivered: {alert.title!r} / key={alert.key!r}"


if __name__ == "__main__":
    from .harness import exit_code

    sys.exit(exit_code(CHECK.run()))
