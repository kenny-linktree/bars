"""Provider enablement (--disable) and network-free login detection (--detect)."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bars_collector import __main__ as cli, adapters, parsers
from bars_collector.model import CollectionError, validate_snapshot
from bars_collector.runner import run
from bars_collector.storage import load_snapshot

COLLECTOR = Path(__file__).resolve().parents[2] / "collector"
OLD = "2026-09-30T01:00:00Z"
NEW = "2026-10-01T01:00:00Z"


def ok(date):
    return dict(status="ok", message=None, fetched_at=date, last_attempt_at=date,
                **parsers.devin({"overage_balance": 12.5}))


class DisableTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "snapshot.json"

    def test_disabled_provider_keeps_retained_entry_and_is_marked(self):
        run(self.output, ["devin"], results=iter([("devin", ok(OLD))]))
        before = copy.deepcopy(load_snapshot(self.output)["providers"][3])

        def results():
            yield "claude", ok(NEW)
        run(self.output, ["claude", "devin"], results=results(), disabled={"devin"})
        after = load_snapshot(self.output)["providers"][3]
        self.assertIs(after.pop("enabled"), False)
        before.pop("enabled", None)
        self.assertEqual(after, before)
        self.assertEqual(load_snapshot(self.output)["providers"][0]["fetched_at"], NEW)
        self.assertIs(load_snapshot(self.output)["providers"][0]["enabled"], True)

    def test_disabled_provider_worker_never_runs(self):
        with patch("bars_collector.runner.collect_results", side_effect=AssertionError("worker started")):
            snapshot = run(self.output, ["cursor"], disabled={"cursor"})
        self.assertIs(snapshot["providers"][2]["enabled"], False)

    def test_never_collected_disabled_provider_keeps_placeholder(self):
        run(self.output, ["cursor"], results=iter([]), disabled={"cursor"})
        cursor = load_snapshot(self.output)["providers"][2]
        self.assertEqual((cursor["status"], cursor["message"], cursor["fetched_at"], cursor["metrics"]),
                         ("setup", "Waiting for first collection.", None, []))
        self.assertEqual([p["id"] for p in load_snapshot(self.output)["providers"]],
                         ["claude", "codex", "cursor", "devin"])

    def test_re_enabling_restores_the_flag_and_collection(self):
        run(self.output, ["devin"], results=iter([]), disabled={"devin"})
        run(self.output, ["devin"], results=iter([("devin", ok(NEW))]))
        devin = load_snapshot(self.output)["providers"][3]
        self.assertIs(devin["enabled"], True)
        self.assertEqual(devin["fetched_at"], NEW)

    def test_unchanged_flags_and_no_work_do_not_rewrite(self):
        run(self.output, ["cursor"], results=iter([]), disabled={"cursor"})
        stamp = self.output.stat().st_mtime_ns
        content = self.output.read_bytes()
        run(self.output, ["cursor"], results=iter([]), disabled={"cursor"})
        self.assertEqual((self.output.stat().st_mtime_ns, self.output.read_bytes()), (stamp, content))

    def test_cli_disabled_provider_is_a_successful_no_op(self):
        output = io.StringIO()
        with patch("bars_collector.runner.collect_results", side_effect=AssertionError("worker started")), \
                contextlib.redirect_stdout(output):
            code = cli.main(["--output", str(self.output), "--provider", "cursor", "--disable", "cursor"])
        self.assertEqual(code, 0)
        self.assertIn("cursor: disabled", output.getvalue())

    def test_cli_passes_repeated_disable_to_runner(self):
        with patch.object(cli, "run", return_value=load_snapshot(self.output)) as fake, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--output", str(self.output), "--disable", "codex", "--disable", "cursor"])
        self.assertEqual(fake.call_args.kwargs["disabled"], ["codex", "cursor"])
        self.assertEqual(fake.call_args.args[1], ["claude", "codex", "cursor", "devin"])

    def test_validator_accepts_missing_enabled_and_rejects_non_boolean(self):
        run(self.output, ["devin"], results=iter([("devin", ok(OLD))]))
        snapshot = load_snapshot(self.output)
        for p in snapshot["providers"]:
            del p["enabled"]  # Snapshots written before providers could be disabled.
        validate_snapshot(snapshot)
        snapshot["providers"][1]["enabled"] = "false"
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot)

    def test_old_snapshot_without_enabled_is_loaded_and_upgraded(self):
        run(self.output, ["devin"], results=iter([("devin", ok(OLD))]))
        snapshot = json.loads(self.output.read_text())
        for p in snapshot["providers"]:
            del p["enabled"]
        self.output.write_text(json.dumps(snapshot))
        run(self.output, ["claude"], results=iter([("claude", ok(NEW))]))
        self.assertTrue(all(p["enabled"] is True for p in load_snapshot(self.output)["providers"]))


class DetectTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()

    def environment(self):
        # PATH holds only the fake CLI directory plus system tools for the Keychain check.
        return dict(os.environ, HOME=str(self.home), PATH=f"{self.bin}:/usr/bin:/bin",
                    PYTHONPATH=str(COLLECTOR), PYTHONDONTWRITEBYTECODE="1")

    def fake_keychain(self, present):
        def fake_run(args, **kwargs):
            self.assertEqual(args[:3], ["/usr/bin/security", "find-generic-password", "-s"])
            # Detection must never ask for the secret itself.
            self.assertNotIn("-w", args)
            self.assertNotIn("-g", args)
            return subprocess.CompletedProcess(args, 0 if args[3] in present else 44)
        return patch.object(adapters.subprocess, "run", side_effect=fake_run)

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_empty_home_detects_nothing_locally(self):
        with patch.dict(os.environ, self.environment()), self.fake_keychain(set()):
            self.assertEqual(adapters.detect(), {"claude": False, "codex": False, "cursor": False, "devin": False})

    def test_detects_every_provider_from_local_material(self):
        self.write(".codex/auth.json", json.dumps({"tokens": {"access_token": "a", "account_id": "b"}}))
        self.write(".local/share/devin/credentials.toml", 'windsurf_api_key = "synthetic"\n')
        cli_path = self.bin / "devin"
        cli_path.write_text("#!/bin/sh\nexit 1\n")
        cli_path.chmod(0o700)
        with patch.dict(os.environ, self.environment()), \
                self.fake_keychain({"Claude Code-credentials", "cursor-access-token"}):
            result = adapters.detect()
        self.assertEqual(list(result), ["claude", "codex", "cursor", "devin"])
        self.assertEqual(result, {"claude": True, "codex": True, "cursor": True, "devin": True})

    def test_setup_states_are_missing_and_login_required_states_are_found(self):
        # An API-key-only Codex login is a setup state; Devin without its CLI is too.
        self.write(".codex/auth.json", json.dumps({"OPENAI_API_KEY": "synthetic"}))
        self.write(".local/share/devin/credentials.toml", 'windsurf_api_key = "synthetic"\n')
        with patch.dict(os.environ, self.environment()), self.fake_keychain(set()):
            self.assertEqual(adapters.detect(), {"claude": False, "codex": False, "cursor": False, "devin": False})
        # Unreadable login material still means the provider is in use.
        self.write(".codex/auth.json", "{not json")
        self.write(".local/share/devin/credentials.toml", "windsurf_api_key = [\n")
        (self.bin / "devin").write_text("#!/bin/sh\n")
        (self.bin / "devin").chmod(0o700)
        with patch.dict(os.environ, self.environment()), self.fake_keychain(set()):
            result = adapters.detect()
        self.assertEqual((result["codex"], result["devin"]), (True, True))

    def test_deeply_nested_codex_login_is_unreadable_not_a_crash(self):
        # json raises RecursionError, not ValueError, for very deep nesting. CPython 3.14
        # parses 100,000 levels, so use the deepest file within the 1 MiB read limit.
        depth = adapters.MAX_BYTES // 2
        self.write(".codex/auth.json", "[" * depth + "]" * depth)
        with patch.dict(os.environ, self.environment()):
            with self.assertRaises(CollectionError) as raised:
                adapters.codex_credentials()
            self.assertEqual(raised.exception.status, "login_required")
            with self.fake_keychain(set()):
                self.assertTrue(adapters.detect()["codex"])

    def test_cli_prints_one_json_object_without_secrets_or_network(self):
        self.write(".codex/auth.json", json.dumps({"tokens": {"access_token": "synthetic-secret-token",
                                                              "account_id": "synthetic-account"}}))
        sitecustomize = self.home / "nonet"
        sitecustomize.mkdir()
        # Any socket creation, lookup or connection in the detect process fails the test.
        (sitecustomize / "sitecustomize.py").write_text(
            "import os, sys\n"
            "def _hook(event, args):\n"
            "    if event.startswith('socket.'):\n"
            "        sys.stderr.write('network used: ' + event + '\\n'); sys.stderr.flush(); os._exit(99)\n"
            "sys.addaudithook(_hook)\n")
        env = self.environment()
        env["PYTHONPATH"] = f"{sitecustomize}:{COLLECTOR}"
        result = subprocess.run([sys.executable, "-m", "bars_collector", "--detect"], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(list(data), ["claude", "codex", "cursor", "devin"])
        self.assertTrue(all(isinstance(value, bool) for value in data.values()))
        self.assertEqual((data["codex"], data["devin"]), (True, False))
        self.assertNotIn("synthetic", result.stdout + result.stderr)

    def test_detect_rejects_collection_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--detect", "--disable", "cursor"])


if __name__ == "__main__":
    unittest.main()
