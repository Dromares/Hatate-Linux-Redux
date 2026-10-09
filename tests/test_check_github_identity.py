"""Regression coverage for scripts/check_github_identity.sh.

No real network or real Paperclip broker involved: a throwaway loopback
HTTP server stands in for `POST .../runtime-tools/github/credentials`
(same "every HTTP call is stubbed" rule the rest of this suite follows -
see .github/workflows/tests.yml).

The script classifies the broker's HTTP response directly (DAN-549) rather
than inferring liveness from git `FETCH_HEAD` plus a human-supplied
`--mcp-status` flag (DAN-265's original design, superseded before this PR
was re-authored - see docs/github-identity-flakes.md's "historical"
sections for why). These tests cover the six real exit codes the script's
own header comment documents.
"""

import json
import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_github_identity.sh"


def _make_handler(response_body, status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
            body = json.dumps(response_body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence per-request logging
            pass

    return Handler


class BrokerStub:
    """A loopback HTTP server standing in for the Paperclip broker."""

    def __init__(self, response_body, status=200):
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(response_body, status))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class CheckGithubIdentityTest(unittest.TestCase):
    def run_script(self, broker_url=None, extra_env=None, args=(), clear_env=False):
        env = dict(os.environ) if not clear_env else {"PATH": os.environ["PATH"]}
        if broker_url is not None:
            env["PAPERCLIP_API_KEY"] = "test-api-key"
            env["PAPERCLIP_GITHUB_BROKER_TOKEN"] = "test-broker-token"
            env["PAPERCLIP_GITHUB_BROKER_URL"] = broker_url
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_healthy_when_broker_reports_available(self):
        with BrokerStub({"status": "available", "source": "dedicated"}) as url:
            result = self.run_script(url)

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("HEALTHY", result.stdout)

    def test_no_identity_when_broker_reports_unavailable(self):
        with BrokerStub({"status": "unavailable", "source": "personal"}) as url:
            result = self.run_script(url)

        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("NO-IDENTITY", result.stdout)
        self.assertIn("source=personal", result.stdout)

    def test_lease_busy_on_409(self):
        with BrokerStub({}, status=409) as url:
            result = self.run_script(url)

        self.assertEqual(result.returncode, 2, msg=result.stdout + result.stderr)
        self.assertIn("LEASE-BUSY", result.stdout)

    def test_capability_rejected_on_401(self):
        with BrokerStub({}, status=401) as url:
            result = self.run_script(url)

        self.assertEqual(result.returncode, 3, msg=result.stdout + result.stderr)
        self.assertIn("CAPABILITY-REJECTED", result.stdout)

    def test_capability_rejected_on_403(self):
        with BrokerStub({}, status=403) as url:
            result = self.run_script(url)

        self.assertEqual(result.returncode, 3, msg=result.stdout + result.stderr)
        self.assertIn("CAPABILITY-REJECTED", result.stdout)

    def test_broker_down_when_unreachable(self):
        # Port 1 on loopback: nothing listens there, so curl fails fast.
        result = self.run_script("http://127.0.0.1:1")

        self.assertEqual(result.returncode, 4, msg=result.stdout + result.stderr)
        self.assertIn("BROKER-DOWN", result.stdout)

    def test_inconclusive_on_unrecognized_http_status(self):
        with BrokerStub({}, status=500) as url:
            result = self.run_script(url)

        self.assertEqual(result.returncode, 5, msg=result.stdout + result.stderr)
        self.assertIn("INCONCLUSIVE", result.stdout)

    def test_inconclusive_on_unexpected_argument(self):
        result = self.run_script("http://127.0.0.1:1", args=["--bogus"])

        self.assertEqual(result.returncode, 5, msg=result.stdout + result.stderr)
        self.assertIn("unexpected argument", result.stderr)

    def test_inconclusive_when_required_env_vars_are_missing(self):
        """A missing env var must surface as INCONCLUSIVE (5), the exit
        code the script's own header comment documents for this case -
        not bash's own parameter-expansion exit (1), which would be
        indistinguishable from NO-IDENTITY on this script's contract."""
        result = self.run_script(clear_env=True)

        self.assertEqual(result.returncode, 5, msg=result.stdout + result.stderr)
        self.assertIn("INCONCLUSIVE", result.stderr)
        self.assertIn("PAPERCLIP_GITHUB_BROKER_URL", result.stderr)

    def test_help_prints_usage_and_exits_zero(self):
        result = self.run_script(clear_env=True, args=["--help"])

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("Usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
