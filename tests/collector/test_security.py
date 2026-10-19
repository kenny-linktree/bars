"""Trust claims: fixed destinations, refused redirects, verified TLS, bounded input,
minimal child environments and no credential material in results or messages."""
import base64
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from bars_collector import __main__ as cli
from bars_collector.model import CollectionError
from bars_plugin import access
from plugin_fixtures import STANDARD_DIR, parse, plugin, run_standard as run

CLAUDE_TOKEN = "synthetic-claude-token"
CODEX_TOKEN = "synthetic-codex-token"
CODEX_ACCOUNT = "synthetic-codex-account"
CURSOR_SUBJECT = "user|synthetic-cursor-subject"
DEVIN_TOKEN = "synthetic-devin-key"
DEVIN_ORG = "org-synthetic"


def b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


CURSOR_TOKEN = "e30." + b64({"sub": CURSOR_SUBJECT}) + ".synthetic-signature"
SECRETS = (CLAUDE_TOKEN, CODEX_TOKEN, CODEX_ACCOUNT, CURSOR_TOKEN, DEVIN_TOKEN)


class Recorder(http.server.BaseHTTPRequestHandler):
    """Answers /redirect/<code> with a redirect to /landing and records every landing."""

    def do_GET(self):
        server = self.server
        if self.path.startswith("/redirect/"):
            self.send_response(int(self.path.rsplit("/", 1)[1]))
            self.send_header("Location", "http://127.0.0.1:%d/landing" % server.server_port)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/landing":
            server.landings.append(dict(self.headers))
            self.reply(b'{"ok": 1}')
        elif self.path == "/large":
            self.reply(b'{"padding": "' + b"x" * access.MAX_BYTES + b'"}')
        else:
            self.reply(b'{"ok": 1}')

    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        self.server.landings = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # Talk to the loopback server directly, whatever proxy this machine uses.
        environment = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        environment["NO_PROXY"] = "*"
        proxies = patch.dict(os.environ, environment, clear=True)
        proxies.start()
        self.addCleanup(proxies.stop)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.server.server_port, path)

    def test_every_redirect_status_is_refused_without_forwarding_credentials(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                with self.assertRaises(CollectionError) as raised:
                    access.http_json(self.url("/redirect/%d" % code), {"Authorization": "Bearer " + CLAUDE_TOKEN})
                self.assertEqual(raised.exception.status, "error")
                self.assertIn("redirect", str(raised.exception))
                self.assertNotIn(CLAUDE_TOKEN, str(raised.exception))
        self.assertEqual(self.server.landings, [])

    def test_cleartext_is_allowed_only_to_the_exact_loopback_host(self):
        for url in ("http://127.0.0.1.example.com/usage", "http://127.0.0.1@remote.example/usage",
                    "http://user:pw@127.0.0.1/usage", "http://localhost/usage", "http://remote.example/usage",
                    "ftp://127.0.0.1/x", "https://", "", None):
            with self.subTest(url=url):
                self.assertFalse(access.secure_destination(url))
                with self.assertRaises(CollectionError):
                    access.http_json(url, {"Authorization": "Bearer " + CLAUDE_TOKEN})
        self.assertTrue(access.secure_destination("http://127.0.0.1:8080/usage"))
        self.assertTrue(access.secure_destination("https://api.example.com/usage"))
        self.assertEqual(self.server.landings, [])

    def test_rate_limit_headers_are_read_in_every_standard_form(self):
        from email.message import Message
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone
        from bars_plugin import rate_limit_pause_seconds
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        def headers(**values):
            m = Message()
            for key, value in values.items():
                m[key.replace("_", "-")] = value
            return m
        self.assertEqual(rate_limit_pause_seconds(headers(), now), 0)
        self.assertEqual(rate_limit_pause_seconds(None, now), 0)
        # RFC 9110 Retry-After: delay-seconds, HTTP-date, and the unusable forms.
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After="120"), now), 120)
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After="0"), now), 0)
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After=format_datetime(now + timedelta(seconds=300), usegmt=True)), now), 300)
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After="Thu, 01 Jan 2015 00:00:00 GMT"), now), 0)
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After="-5"), now), 0)
        # IETF draft: structured RateLimit field or RateLimit-Reset in seconds.
        self.assertEqual(rate_limit_pause_seconds(headers(RateLimit="limit=100, remaining=0; reset=45"), now), 45)
        self.assertEqual(rate_limit_pause_seconds(headers(RateLimit_Reset="90"), now), 90)
        # X- variants: delta-seconds or a Unix timestamp.
        self.assertEqual(rate_limit_pause_seconds(headers(X_RateLimit_Reset="30"), now), 30)
        self.assertEqual(rate_limit_pause_seconds(headers(X_RateLimit_Reset=str(int(now.timestamp()) + 600)), now), 600)
        self.assertEqual(rate_limit_pause_seconds(headers(X_Rate_Limit_Reset=str(int(now.timestamp()) - 600)), now), 0)
        # The IETF draft's structured form: a quoted policy item with r= and t= parameters.
        self.assertEqual(rate_limit_pause_seconds(headers(RateLimit='"default";r=0;t=3600'), now), 3600)
        self.assertEqual(rate_limit_pause_seconds(headers(RateLimit='"burst";r=2;t=5, "daily";r=0;t=7200'), now), 5)
        # Malformed values never raise and never restore in-run retries: they read as "no figure".
        for bad in ("\u00b2", "9" * 5000, "1e3", "٣", "t=", "NaN"):
            self.assertEqual(rate_limit_pause_seconds(headers(Retry_After=bad), now), 0, bad)
            self.assertEqual(rate_limit_pause_seconds(headers(RateLimit=bad), now), 0, bad)
            self.assertEqual(rate_limit_pause_seconds(headers(X_RateLimit_Reset=bad), now), 0, bad)
        # Retry-After wins when several are present; an unusable Retry-After falls through.
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After="10", X_RateLimit_Reset="500"), now), 10)
        self.assertEqual(rate_limit_pause_seconds(headers(Retry_After="soon", X_RateLimit_Reset="500"), now), 500)

    def test_oversized_response_is_rejected(self):
        with self.assertRaises(CollectionError) as raised:
            access.http_json(self.url("/large"), {})
        self.assertIn("too large", str(raised.exception))

    def test_one_request_per_call_with_neutral_user_agent(self):
        self.assertEqual(access.http_json(self.url("/landing"), {}), {"ok": 1})
        self.assertEqual(len(self.server.landings), 1)
        self.assertEqual(self.server.landings[0]["User-Agent"], "Bars/0.1")


class OpenerTests(unittest.TestCase):
    def test_https_requires_certificate_and_hostname_verification(self):
        handlers = access.opener().handlers
        https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        context = https[0]._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(h) for h in redirects], [access.NoRedirect])

    def test_verification_survives_a_replaced_default_context_factory(self):
        with patch.object(ssl, "_create_default_https_context", ssl._create_unverified_context):
            handler = next(h for h in access.opener().handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)


class EndpointTests(unittest.TestCase):
    """Each credential reaches only its own provider's fixed endpoint, once."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        (self.home / ".codex").mkdir()
        (self.home / ".codex/auth.json").write_text(json.dumps(
            {"tokens": {"access_token": CODEX_TOKEN, "account_id": CODEX_ACCOUNT}}))
        devin = self.home / ".local/share/devin"
        devin.mkdir(parents=True)
        (devin / "credentials.toml").write_text('windsurf_api_key = "%s"\n' % DEVIN_TOKEN)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        cli_path = self.bin / "devin"
        cli_path.write_text('#!/bin/sh\nenv > "$HOME/devin-env"\nprintf \'{"org_id": "%s"}\\n\'\n' % DEVIN_ORG)
        cli_path.chmod(0o700)
        environment = dict(os.environ, HOME=str(self.home), PATH="%s:/usr/bin:/bin" % self.bin,
                           SYNTHETIC_UNRELATED_SECRET="must-not-reach-cli",
                           XDG_DATA_HOME=str(self.home / "elsewhere"), DEVIN_API_URL="https://elsewhere.example")
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.requests = []

    def fake_opener(self, body, status=200, headers=None):
        recorder = self.requests
        import email.message
        import urllib.error

        class Opener:
            def open(self, request, timeout):
                recorder.append((request, timeout))
                if status != 200:
                    message = email.message.Message()
                    for key, value in (headers or {}).items():
                        message[key] = value
                    raise urllib.error.HTTPError(request.full_url, status, "synthetic", message, io.BytesIO(b""))
                return io.BytesIO(json.dumps(body).encode())
        return Opener()

    def keychain(self, service, **kwargs):
        return {plugin("claude").KEYCHAIN_SERVICE: json.dumps({"claudeAiOauth": {"accessToken": CLAUDE_TOKEN}}),
                plugin("cursor").KEYCHAIN_SERVICE: CURSOR_TOKEN}[service]

    def login_files(self):
        paths = (self.home / ".codex/auth.json", self.home / ".local/share/devin/credentials.toml")
        return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def collect(self, pid, body, status=200, headers=None):
        self.requests.clear()
        logins = self.login_files()
        with patch.object(plugin("claude"), "keychain", side_effect=self.keychain), \
                patch.object(plugin("cursor"), "keychain", side_effect=self.keychain), \
                patch.object(access, "opener", return_value=self.fake_opener(body, status, headers)):
            result = plugin(pid).collect()
        self.assertEqual(self.login_files(), logins, "login files are read, never written")
        # Devin asks two fixed endpoints (shared balance and quotas, then this person's draw).
        self.assertEqual(len(self.requests), 2 if pid == "devin" else 1, "one request per endpoint, no retries")
        for request, timeout in self.requests:
            self.assertEqual(timeout, 12)
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(result))
        return self.requests[0][0]

    def test_429_and_503_with_retry_after_carry_the_pause_for_every_standard_plugin(self):
        # One request path serves all standard plugins, so the rate-limit handling is shared.
        from bars_plugin.values import CollectionError
        for pid in ("claude", "codex", "cursor", "devin"):
            for code, header, expected in ((429, {"Retry-After": "90"}, 90), (429, {}, 0),
                                           (503, {"Retry-After": "120"}, 120)):
                with self.subTest(pid=pid, code=code, header=header):
                    with self.assertRaises(CollectionError) as caught:
                        self.collect(pid, None, status=code, headers=header)
                    self.assertEqual(caught.exception.status, "error")
                    self.assertEqual(caught.exception.retry_after, expected)
            # A 503 without Retry-After stays an ordinary transient error (retried in-run).
            with self.assertRaises(CollectionError) as caught:
                self.collect(pid, None, status=503, headers={})
            self.assertIsNone(caught.exception.retry_after)
            # A malformed Retry-After on a 429 is still a rate limit, with no figure.
            with self.assertRaises(CollectionError) as caught:
                self.collect(pid, None, status=429, headers={"Retry-After": "\u00b2"})
            self.assertEqual(caught.exception.retry_after, 0)

    def test_devin_surfaces_a_rate_limit_on_its_second_endpoint(self):
        # The personal-usage request is best effort for transient errors only: a rate limit
        # there must reach the collector, or the endpoint would be asked again every minute.
        from bars_plugin.values import CollectionError
        calls = []

        def answer(url, headers, timeout=12):
            calls.append(url)
            if url.endswith("/usage"):
                return {"overage_balance": 5, "weekly_percentage": 1}
            raise CollectionError("Provider rate limit reached. Collection will retry later.", retry_after=900)
        with patch.object(plugin("devin"), "http_json", side_effect=answer), \
                patch.object(plugin("devin"), "access_token", return_value="synthetic"), \
                patch.object(plugin("devin"), "command", return_value=json.dumps({"org_id": "org_synthetic"})), \
                patch.object(plugin("devin"), "cli", return_value="/usr/bin/true"):
            with self.assertRaises(CollectionError) as caught:
                plugin("devin").collect()
        self.assertEqual(caught.exception.retry_after, 900)
        self.assertEqual(len(calls), 2)

    def assert_only_own_secrets(self, request, own):
        headers = request.header_items()
        for secret in SECRETS:
            with self.subTest(secret=secret):
                # Credentials and account IDs never travel in the URL, and each of the
                # provider's own values travels in exactly one header.
                self.assertNotIn(secret, request.full_url)
                carriers = [name for name, value in headers if secret in value]
                self.assertEqual(len(carriers), 1 if secret in own else 0, carriers)

    def test_claude(self):
        request = self.collect("claude", {"five_hour": {"utilization": 10}})
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + CLAUDE_TOKEN)
        self.assert_only_own_secrets(request, {CLAUDE_TOKEN})

    def test_codex(self):
        request = self.collect("codex", {"rate_limit": {"secondary_window": {"used_percent": 5}}})
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + CODEX_TOKEN)
        self.assert_only_own_secrets(request, {CODEX_TOKEN, CODEX_ACCOUNT})

    def test_cursor(self):
        request = self.collect("cursor", {"individualUsage": {"plan": {"used": 1, "limit": 2}}})
        self.assertEqual(request.full_url, "https://cursor.com/api/usage-summary")
        self.assertIn("WorkosCursorSessionToken=", request.get_header("Cookie"))
        self.assert_only_own_secrets(request, {CURSOR_TOKEN})

    def test_devin_uses_fixed_host_and_minimal_cli_environment(self):
        request = self.collect("devin", {"overage_balance": 1})
        self.assertEqual(request.full_url, "https://app.devin.ai/api/%s/billing/quota/usage" % DEVIN_ORG)
        second = self.requests[1][0]
        self.assertEqual(second.full_url, "https://app.devin.ai/api/%s/billing/quota/my-on-demand-usage" % DEVIN_ORG)
        self.assert_only_own_secrets(second, {DEVIN_TOKEN})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + DEVIN_TOKEN)
        self.assert_only_own_secrets(request, {DEVIN_TOKEN})
        cli_environment = dict(line.split("=", 1) for line in (self.home / "devin-env").read_text().splitlines()
                               if "=" in line)
        self.assertEqual(cli_environment["HOME"], str(self.home))
        self.assertTrue(cli_environment["PATH"].startswith(str(self.bin)))
        for withheld in ("SYNTHETIC_UNRELATED_SECRET", "XDG_DATA_HOME", "DEVIN_API_URL", "PYTHONPATH"):
            self.assertNotIn(withheld, cli_environment)

    def test_devin_rejects_an_organization_that_could_alter_the_url(self):
        for organization in ("../evil", "a/b", "x?y", "a.example.com#", ""):
            with self.subTest(organization=organization):
                (self.bin / "devin").write_text("#!/bin/sh\nprintf '%s\\n'\n" % json.dumps({"org_id": organization}))
                with patch.object(access, "opener", side_effect=AssertionError("request sent")):
                    with self.assertRaises(CollectionError) as raised:
                        plugin("devin").collect()
                self.assertEqual(raised.exception.status, "login_required")


class CredentialToolTests(unittest.TestCase):
    def test_tool_output_is_bounded_before_the_process_finishes(self):
        script = "import os, time; os.write(1, b'x' * %d); time.sleep(5)" % (access.MAX_BYTES + 1)
        with self.assertRaises(CollectionError) as raised:
            access.command([sys.executable, "-c", script], timeout=2)
        self.assertEqual(str(raised.exception), "Login tool returned an invalid response.")

    def test_tool_deadline_terminates_and_reaps_the_process(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            script = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            with self.assertRaises(CollectionError) as raised:
                access.command([sys.executable, "-c", script, str(pid_path)], timeout=1)
            self.assertEqual(str(raised.exception), "Credential lookup timed out.")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_path.read_text()), 0)

    def test_tools_get_no_stdin_and_only_allowlisted_environment(self):
        calls = []
        real_popen = subprocess.Popen

        def fake_run(args, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(args, 0, stdout=b"value", stderr=b"")
        def recording_popen(args, **kwargs):
            calls.append(kwargs)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            return real_popen(args, **kwargs)
        with patch.dict(os.environ, {"SYNTHETIC_UNRELATED_SECRET": "x"}), \
                patch.object(access.subprocess, "run", side_effect=fake_run), \
                patch.object(access.subprocess, "Popen", side_effect=recording_popen):
            self.assertEqual(access.command([sys.executable, "-c", "print('value')"]), "value")
            access.keychain_entry_exists("service")
        for kwargs in calls:
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("SYNTHETIC_UNRELATED_SECRET", kwargs["env"])
            self.assertTrue(set(kwargs["env"]) <= set(access.CHILD_ENVIRONMENT))

    def test_keychain_lookups_are_read_queries_for_the_fixed_items(self):
        calls = []
        with patch.object(access, "command", side_effect=lambda args, **kwargs: calls.append(args) or "x"), \
                patch.object(access.subprocess, "run",
                             side_effect=lambda args, **kwargs: calls.append(args) or
                             subprocess.CompletedProcess(args, 0)):
            for service in (plugin("claude").KEYCHAIN_SERVICE, plugin("cursor").KEYCHAIN_SERVICE):
                access.keychain(service)
                access.keychain_entry_exists(service)
        self.assertEqual(calls, [
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials"],
            ["/usr/bin/security", "find-generic-password", "-s", "cursor-access-token", "-w"],
            ["/usr/bin/security", "find-generic-password", "-s", "cursor-access-token"],
        ])

    def test_tool_failure_messages_omit_tool_output(self):
        script = "import sys; print('synthetic-secret'); print('synthetic-secret', file=sys.stderr); sys.exit(44)"
        with self.assertRaises(CollectionError) as raised:
            access.command([sys.executable, "-c", script])
        self.assertNotIn("synthetic", str(raised.exception))

    def security_exiting(self, status):
        """Run every /usr/bin/security lookup as a synthetic tool that exits with `status`."""
        real_popen = subprocess.Popen

        def fake_popen(args, **kwargs):
            self.assertEqual(args[0], "/usr/bin/security")
            script = "import sys; print('synthetic-secret'); sys.exit(%d)" % status
            return real_popen([sys.executable, "-c", script], **kwargs)
        return patch.object(access.subprocess, "Popen", side_effect=fake_popen)

    def test_missing_keychain_item_is_a_setup_state_matching_detection(self):
        cases = (("claude", plugin("claude").collect, "Claude Code login was not found. Install Claude Code and sign in."),
                 ("cursor", plugin("cursor").collect, "Cursor login was not found. Install the Cursor CLI and run agent login."))
        for pid, collect, message in cases:
            with self.subTest(provider=pid), self.security_exiting(access.SECURITY_ITEM_NOT_FOUND), \
                    patch.object(access, "opener", side_effect=AssertionError("request sent")):
                with self.assertRaises(CollectionError) as raised:
                    collect()
                self.assertEqual(raised.exception.status, "setup")
                self.assertEqual(str(raised.exception), message)
                # --detect reports the same missing item as no login found.
                self.assertFalse(plugin(pid).detect())

    def test_other_keychain_failures_still_require_login(self):
        # For example 36 (locked Keychain), 51 (access denied) and 128 (prompt cancelled).
        for status in (1, 36, 51, 128):
            for collect in (plugin("claude").collect, plugin("cursor").collect):
                with self.subTest(status=status, collector=collect.__module__), self.security_exiting(status), \
                        patch.object(access, "opener", side_effect=AssertionError("request sent")):
                    with self.assertRaises(CollectionError) as raised:
                        collect()
                    self.assertEqual(raised.exception.status, "login_required")
                    self.assertNotIn("synthetic", str(raised.exception))

    def test_deeply_nested_provider_response_is_invalid(self):
        depth = access.MAX_BYTES // 2  # Deep enough for RecursionError on every interpreter.
        body = b"[" * depth + b"]" * depth
        with patch("bars_plugin.access.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(body)
            with self.assertRaises(CollectionError) as raised:
                access.http_json("https://vendor.example/usage", {})
        self.assertEqual(str(raised.exception), "Provider returned an invalid usage response.")

    def test_tokens_must_be_visible_ascii(self):
        for value in ("", "a b", "a\r\nX-Injected: 1", "a\x00b", "a\x7fb", "caf\u00e9", "x" * 32769, None, 7):
            with self.subTest(value=value):
                with self.assertRaises(CollectionError) as raised:
                    access.token(value)
                self.assertEqual(raised.exception.status, "login_required")
        self.assertEqual(access.token("sk-ant_0.9~|x"), "sk-ant_0.9~|x")


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_snapshot_symlink_is_neither_read_nor_followed(self):
        target = self.directory / "elsewhere.json"
        run(target, ["devin"], results=iter([("devin", dict(
            status="ok", message=None, fetched_at="2026-10-01T00:00:00Z", last_attempt_at="2026-10-01T00:00:00Z",
            **parse("devin")({"overage_balance": 1})))]))
        original = target.read_bytes()
        output = self.directory / "snapshot.json"
        output.symlink_to(target)
        with self.assertRaises(OSError):
            run(output, ["devin"], results=iter([]))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--output", str(output), "--plugins", str(STANDARD_DIR), "--provider", "devin"]), 1)
        self.assertTrue(output.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_lock_symlink_is_refused(self):
        target = self.directory / "victim"
        target.write_text("unchanged")
        (self.directory / "snapshot.json.lock").symlink_to(target)
        with self.assertRaises(OSError):
            run(self.directory / "snapshot.json", ["devin"], results=iter([]))
        self.assertEqual(target.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
