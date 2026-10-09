"""Wake-payload headroom detector (DAN-926): flag issues approaching the
Linux spawn ceiling before a seat dies on them (`spawn E2BIG`, DAN-920).

## Mechanism (proved on DAN-920, not re-derived here)

The platform injects the per-run continuation state as
`PAPERCLIP_WAKE_PAYLOAD_JSON` -- the JSON-serialized
`contextSnapshot.paperclipWake` object -- into the spawned `claude` process's
environment. `execve()` rejects a spawn once any *single* argv/envp string
exceeds Linux's `MAX_ARG_STRLEN = 32 * PAGE_SIZE` (131,072 bytes on this
host; confirmed via `os.sysconf('SC_PAGE_SIZE') * 32`). This is **not**
`ARG_MAX` (2 MiB total argv+env) -- total argv+env on a healthy spawn runs
about 15 KB, 0.7% of `ARG_MAX`; the single oversized env string is the only
thing that ever gets near its own ceiling. Reproduce directly:
`/proc/<pid>/environ`, split on NUL, find the `PAPERCLIP_WAKE_PAYLOAD_JSON=`
entry, compare `len(value.encode())` to the sysconf ceiling above.

## Two measurements, used by availability

1. **Direct (ground truth).** `GET /api/heartbeat-runs/{id}` (the per-run
   detail endpoint) returns `contextSnapshot.paperclipWake` in full.
   `json.dumps(that_value, separators=(",", ":"))` byte length lands within
   ~1.2% of the real `PAPERCLIP_WAKE_PAYLOAD_JSON` env value, measured
   against this very script's own run (33,301 computed vs 32,897 actual) --
   close enough to use as ground truth. Not every run carries the key: it is
   only populated for continuation-style wakes (`issue_commented` and
   similar). A run woken by e.g. `issue_unblock_requested` has no
   `paperclipWake` key in its `contextSnapshot` at all -- treat that as "no
   direct measurement available", never as zero.

   **The bulk list lies, same shape as the `executionPolicy` and
   `access.grants` traps already on record (DAN-348, DAN-497).** Confirmed
   fresh here on 2026-10-09: `GET /api/companies/{id}/heartbeat-runs` returns
   a gutted 5-key `contextSnapshot` (`issueId`, `taskId`, `taskKey`,
   `wakeReason`, `wakeSource`, `wakeTriggerDetail`) for every row -- no
   `paperclipWake`, no `executionContinuation` -- with nothing in the
   response signalling the omission. The bulk list is still useful here
   purely as an `issueId` index to find *which* run to fetch in full; it can
   never supply the direct measurement itself.

2. **Proxy (estimate), used when no qualifying run exists** -- the case that
   actually matters, since the point is to catch a dangerous issue *before*
   its next wake, not after: `len(description) + sum(len(comment.body) for
   each comment)`, UTF-8 byte lengths. Must come from the **per-issue** `GET
   /api/issues/{id}`, never the bulk `GET /api/companies/{id}/issues` list --
   that list truncates `description` to 1,200 bytes
   (`descriptionTruncated: true`) on 947 of 1,000 rows sampled on this
   company's board 2026-10-09, which would silently undercount exactly the
   long-lived, heavily-discussed issues this detector exists to catch.

## The DAN-920/DAN-781 inflation factor does not generalize -- measured, not assumed

The objective's calibration point (`proxy=97,956` -> `real=132,721`, factor
~1.35x) is a single data point. Two more, measured here with the *direct*
method against each issue's own most recent qualifying run (2026-10-09):

| issue | proxy (desc+comments) | real (direct) | factor |
|---|---|---|---|
| DAN-781 | 97,956 | 133,877 | 1.37x |
| DAN-647 | 64,971 | 76,225 | 1.17x |
| DAN-926 (this ticket's own run) | 9,574 | 33,682 | 3.52x |

The factor is not constant -- it ranges 1.17x-3.52x across these three, and
gets *worse* (higher) for *smaller* issues. That is the signature of a
mostly fixed per-run overhead (instructions bundle, workspace/environment
block, task markdown -- everything that rides along regardless of the
issue's own history) plus a roughly-linear marginal cost per proxy byte, not
a clean multiplier. A least-squares fit over these three points gives
`real ~= 18,300 + 1.10 * proxy` (residuals -15%..+17% on this sample --
noisy, but not biased toward over- or under-estimating).

Per the objective's own fallback instruction ("if you can measure the
payload directly instead of proxying it, do that and drop the factor"):
this script drops the factor. It uses the *direct* measurement whenever a
qualifying run is reachable, and otherwise reports the **raw proxy byte
count itself**, uncorrected, against the objective's own calibration anchor
(~97,000 raw-proxy bytes ~ the DAN-781 danger point) -- it does not try to
project a "real payload estimate" from the proxy, because the factor above
shows that projection would be unreliable in exactly the regime (small,
history-light issues) where getting it wrong matters least acutely but most
often. Ratios reported from the proxy path therefore run *optimistic* for
small issues: DAN-926's own 9,574-byte proxy read as 7.3% of ceiling here
while its real payload was already 25.7%. Treat a proxy-only WARN as a
floor, not a ceiling, and prefer a `direct` row over a `proxy` row whenever
one is available for the same issue.

## Thresholds

WARN at >=70% of the 131,072-byte ceiling, ALARM at >=90%, applied to
whichever of the two measurements above is available (direct preferred).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
MAX_ARG_STRLEN = 32 * PAGE_SIZE  # Linux execve() per-string ceiling -- NOT ARG_MAX

WARN_RATIO = 0.70
ALARM_RATIO = 0.90

NON_TERMINAL_STATUSES = "todo,in_progress,in_review,blocked"

# Only pay for a per-run detail fetch (the only way to get a direct
# measurement) once the cheap proxy already looks substantial -- keeps the
# sweep's API-call count proportional to candidates-near-danger, not to the
# whole open-issue count. Set below WARN_RATIO (not at it) because the
# factor table above shows the proxy reads *optimistic*: DAN-647's current
# proxy sits at 49.6% while its real payload was already past WARN.
DIRECT_MEASURE_PROXY_RATIO = 0.30


class Severity:
    OK = "ok"
    WARN = "warn"
    ALARM = "alarm"


def severity_for(ratio: float) -> str:
    if ratio >= ALARM_RATIO:
        return Severity.ALARM
    if ratio >= WARN_RATIO:
        return Severity.WARN
    return Severity.OK


def proxy_bytes(description: Optional[str], comments: list[dict[str, Any]]) -> int:
    total = len((description or "").encode("utf-8"))
    for c in comments:
        total += len((c.get("body") or "").encode("utf-8"))
    return total


def direct_bytes(run: Optional[dict[str, Any]]) -> Optional[int]:
    """Serialize `contextSnapshot.paperclipWake` the way the platform does.

    Returns None when the run has no `paperclipWake` key at all (not every
    wake reason populates it) rather than treating an absent key as zero --
    a missing measurement must never silently read as "safe".
    """
    if not run:
        return None
    snapshot = run.get("contextSnapshot") or {}
    if "paperclipWake" not in snapshot:
        return None
    payload = snapshot["paperclipWake"]
    if payload is None:
        return None
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


@dataclass
class Headroom:
    issue_id: str
    identifier: str
    status: str
    proxy_measured: int
    direct_measured: Optional[int]
    direct_run_id: Optional[str]

    @property
    def measured_bytes(self) -> int:
        """The best available number: direct when we have it, else proxy."""
        return self.direct_measured if self.direct_measured is not None else self.proxy_measured

    @property
    def method(self) -> str:
        return "direct" if self.direct_measured is not None else "proxy"

    @property
    def ratio(self) -> float:
        return self.measured_bytes / MAX_ARG_STRLEN

    @property
    def severity(self) -> str:
        return severity_for(self.ratio)


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip issues/runs API.

    Kept separate from `sweep` so tests can swap in a fake that never
    touches the network.
    """

    def __init__(self, api_base: str, api_key: str, run_id: Optional[str] = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.api_base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        if self.run_id:
            req.add_header("X-Paperclip-Run-Id", self.run_id)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None

    def list_candidate_issues(self, company_id: str) -> list[dict[str, Any]]:
        """Index only -- bulk list, truncated description, used for ids/status."""
        path = f"/api/companies/{company_id}/issues?status={NON_TERMINAL_STATUSES}&limit=1000"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        """Per-issue detail -- the only source of an untruncated description."""
        return self._request("GET", f"/api/issues/{issue_id}")

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def list_recent_runs(self, company_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Index only -- bulk list, gutted contextSnapshot, used as an issueId lookup."""
        path = f"/api/companies/{company_id}/heartbeat-runs?limit={limit}"
        result = self._request("GET", path)
        return result.get("runs", []) if isinstance(result, dict) else result

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Per-run detail -- the only source of `contextSnapshot.paperclipWake`."""
        try:
            return self._request("GET", f"/api/heartbeat-runs/{run_id}")
        except urllib.error.HTTPError:
            return None


def latest_run_id_for_issue(runs: list[dict[str, Any]], issue_id: str) -> Optional[str]:
    """Newest run (by createdAt) whose lean contextSnapshot.issueId matches."""
    candidates = [
        r for r in runs if ((r.get("contextSnapshot") or {}).get("issueId")) == issue_id
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return candidates[0]["id"]


def sweep(client: PaperclipClient, company_id: str) -> list[Headroom]:
    """Compute headroom for every open issue, direct measurement preferred."""
    candidates = client.list_candidate_issues(company_id)
    recent_runs = client.list_recent_runs(company_id)

    results: list[Headroom] = []
    for candidate in candidates:
        issue = client.get_issue(candidate["id"])
        comments = client.list_comments(issue["id"])
        proxy = proxy_bytes(issue.get("description"), comments)

        direct: Optional[int] = None
        direct_run_id: Optional[str] = None
        if proxy / MAX_ARG_STRLEN >= DIRECT_MEASURE_PROXY_RATIO:
            run_id = latest_run_id_for_issue(recent_runs, issue["id"])
            if run_id is not None:
                run = client.get_run(run_id)
                measured = direct_bytes(run)
                if measured is not None:
                    direct = measured
                    direct_run_id = run_id

        results.append(
            Headroom(
                issue_id=issue["id"],
                identifier=issue.get("identifier", issue["id"]),
                status=issue.get("status", ""),
                proxy_measured=proxy,
                direct_measured=direct,
                direct_run_id=direct_run_id,
            )
        )

    results.sort(key=lambda h: h.measured_bytes, reverse=True)
    return results


def format_report(headroom: list[Headroom]) -> str:
    lines = [
        f"{'identifier':<12} {'status':<12} {'method':<7} {'bytes':>8} {'% ceiling':>10} severity",
        "-" * 70,
    ]
    for h in headroom:
        lines.append(
            f"{h.identifier:<12} {h.status:<12} {h.method:<7} {h.measured_bytes:>8} "
            f"{h.ratio * 100:>9.1f}% {h.severity}"
        )
    return "\n".join(lines)


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    args = parser.parse_args(argv)

    if not args.company_id:
        parser.error("--company-id is required (or set PAPERCLIP_COMPANY_ID)")
    if not args.api_base:
        parser.error("--api-base is required (or set PAPERCLIP_API_URL)")

    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not api_key:
        parser.error("PAPERCLIP_API_KEY must be set in the environment")

    client = PaperclipClient(
        _normalize_api_base(args.api_base), api_key, os.environ.get("PAPERCLIP_RUN_ID")
    )
    headroom = sweep(client, args.company_id)

    alarms = [h for h in headroom if h.severity == Severity.ALARM]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "identifier": h.identifier,
                        "issueId": h.issue_id,
                        "status": h.status,
                        "method": h.method,
                        "measuredBytes": h.measured_bytes,
                        "ratio": h.ratio,
                        "severity": h.severity,
                        "directRunId": h.direct_run_id,
                    }
                    for h in headroom
                ],
                indent=2,
            )
        )
        print(f"ACTIONABLE: {'yes' if alarms else 'no'}", file=sys.stderr)
    else:
        print(format_report(headroom))
        print()
        print(f"ACTIONABLE: {'yes' if alarms else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
