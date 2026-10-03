"""Exercise the installed refresh boundary with isolated fake provider processes."""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import logging
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


refresh = module("bars_refresh", ROOT / "integration/refresh.py")
installer = module("bars_installer", ROOT / "scripts/install.py")


class RefreshIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bars integration ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.package = self.directory / "runtime/bars_collector"
        self.package.mkdir(parents=True)
        (self.package / "__init__.py").write_text("")
        self.publisher = self.directory / "FakePublisher"
        self.publisher.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "p = pathlib.Path(sys.argv[2])\n"
            "event = {'data': json.loads(p.read_text()), 'reload': '--no-reload' not in sys.argv}\n"
            "with (p.parent / 'published.jsonl').open('a') as f: f.write(json.dumps(event) + '\\n')\n"
        )
        self.publisher.chmod(0o700)
        self.config = {"python": sys.executable, "collector_path": str(self.package.parent),
                       "app_executable": str(self.publisher)}
        (self.directory / "installation.json").write_text(json.dumps(self.config))

    def script(self, source):
        (self.package / "__main__.py").write_text(source)

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "integration/refresh.py"),
                               "--support-dir", str(self.directory), *args],
                              capture_output=True, text=True, timeout=10)

    def test_publishes_partial_results_then_requests_final_reload(self):
        self.script("""
import argparse, json, os, pathlib, time
p = argparse.ArgumentParser()
p.add_argument('--output')
p.add_argument('--provider')
args = p.parse_args()
path = pathlib.Path(args.output)
for completed in (1, 2):
    temp = path.with_suffix('.new')
    temp.write_text(json.dumps({'completed': completed, 'provider': args.provider}))
    os.replace(temp, path)
    time.sleep(0.7)
""")
        result = self.invoke("--provider", "cursor")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in (self.directory / "published.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["data"]["completed"] == 1 for e in events))
        self.assertEqual(events[-1], {"data": {"completed": 2, "provider": "cursor"}, "reload": True})
        self.assertTrue(all(not e["reload"] for e in events[:-1]))

    def test_active_refresh_lock_prevents_duplicate_collection(self):
        self.script("raise RuntimeError('Must not run')")
        with (self.directory / "refresh.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.directory / "published.jsonl").exists())

    def wait_for_file(self, name, process):
        path = self.directory / name
        deadline = time.monotonic() + 5
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), name)
        return path

    def wait_for_log(self, text, process):
        path = self.directory / "logs/refresh.log"
        deadline = time.monotonic() + 5
        while (not path.exists() or text not in path.read_text()) and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists())
        self.assertIn(text, path.read_text())

    def assert_waiting_refresh_uses_new_settings(self, initially_disabled, finally_disabled):
        self.config["disabled_providers"] = initially_disabled
        settings = self.directory / "installation.json"
        settings.write_text(json.dumps(self.config))
        self.script("""
import argparse, json, pathlib, time
p = argparse.ArgumentParser()
p.add_argument('--output')
p.add_argument('--provider')
p.add_argument('--disable', action='append', default=[])
args = p.parse_args()
directory = pathlib.Path(args.output).parent
(directory / 'started').touch()
deadline = time.monotonic() + 5
while not (directory / 'release').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
pathlib.Path(args.output).write_text(json.dumps({'cursor_enabled': 'cursor' not in args.disable,
                                               'provider': args.provider}))
""")
        command = [sys.executable, str(ROOT / "integration/refresh.py"),
                   "--support-dir", str(self.directory)]
        scheduled = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        explicit = None
        try:
            self.wait_for_file("started", scheduled)
            explicit = subprocess.Popen(command + ["--wait", "--provider", "cursor"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            self.wait_for_log("Waiting for", explicit)
            self.assertIsNone(explicit.poll())
            # Change settings only after the explicit refresh has started waiting.
            self.config["disabled_providers"] = finally_disabled
            settings.write_text(json.dumps(self.config))
            (self.directory / "release").touch()
            self.assertEqual(scheduled.wait(timeout=10), 0)
            self.assertEqual(explicit.wait(timeout=10), 0)
            events = [json.loads(line) for line in (self.directory / "published.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1], {"data": {"cursor_enabled": "cursor" not in finally_disabled,
                                                   "provider": "cursor"}, "reload": True})
        finally:
            (self.directory / "release").touch()
            for process in (explicit, scheduled):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)

    def test_hide_during_scheduled_refresh_waits_and_uses_latest_settings(self):
        self.assert_waiting_refresh_uses_new_settings([], ["cursor"])

    def test_show_during_scheduled_refresh_waits_and_uses_latest_settings(self):
        self.assert_waiting_refresh_uses_new_settings(["cursor"], [])

    def test_waiting_refresh_times_out_without_collecting(self):
        self.script("raise RuntimeError('Must not run')")
        wrapper = self.directory / "short_wait.py"
        wrapper.write_text("""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('refresh', sys.argv.pop(1))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
refresh.MAX_LOCK_WAIT_SECONDS = 0.1
sys.exit(refresh.main())
""")
        with (self.directory / "refresh.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(wrapper), str(ROOT / "integration/refresh.py"),
                                     "--support-dir", str(self.directory), "--wait"],
                                    capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("Timed out waiting", (self.directory / "logs/refresh.log").read_text())
        self.assertFalse((self.directory / "snapshot.json").exists())

    def test_waiting_refresh_can_be_terminated_without_starting_a_collector(self):
        self.script("raise RuntimeError('Must not run')")
        with (self.directory / "refresh.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            runner = subprocess.Popen([sys.executable, str(ROOT / "integration/refresh.py"),
                                       "--support-dir", str(self.directory), "--wait"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            try:
                self.wait_for_log("Waiting for", runner)
                runner.terminate()
                self.assertEqual(runner.wait(timeout=3), -signal.SIGTERM)
                with (self.directory / "refresh.lock").open("r+b") as other_lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait(timeout=3)
        self.assertFalse((self.directory / "snapshot.json").exists())

    def test_log_symlinks_are_refused_without_changing_the_target(self):
        victim = self.directory / "victim"
        victim.write_text("unchanged")
        logs = self.directory / "logs"
        logs.mkdir()
        (logs / "refresh.log").symlink_to(victim)
        self.script("raise RuntimeError('Must not run')")
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(victim.read_text(), "unchanged")
        self.assertFalse((self.directory / "published.jsonl").exists())

    def test_termination_keeps_the_lock_until_the_collector_exits(self):
        self.script("""
import os, pathlib, signal, sys, time
directory = pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).parent
def stop(signum, frame):
    (directory / 'terminating').touch()
    deadline = time.monotonic() + 5
    while not (directory / 'release').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
(directory / 'collector.pid').write_text(str(os.getpid()))
time.sleep(60)
""")
        runner = subprocess.Popen([sys.executable, str(ROOT / "integration/refresh.py"),
                                   "--support-dir", str(self.directory)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        child = None
        def wait_for(name):
            path = self.directory / name
            deadline = time.monotonic() + 5
            while not path.exists() and runner.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(path.exists(), name)
            return path
        try:
            child = int(wait_for("collector.pid").read_text())
            runner.terminate()
            wait_for("terminating")
            with (self.directory / "refresh.lock").open("r+b") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (self.directory / "release").touch()
            self.assertEqual(runner.wait(timeout=5), 128 + signal.SIGTERM)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            with (self.directory / "refresh.lock").open("r+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse((self.directory / "published.jsonl").exists())
        finally:
            (self.directory / "release").touch()
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if child is not None:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_termination_during_child_startup_still_reaps_the_child(self):
        self.script("import time; time.sleep(60)")
        wrapper = self.directory / "cancel_at_startup.py"
        wrapper.write_text("""
import importlib.util, os, pathlib, signal, sys
spec = importlib.util.spec_from_file_location('refresh', sys.argv.pop(1))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
real_popen = refresh.subprocess.Popen
def spawn(*args, **kwargs):
    child = real_popen(*args, **kwargs)
    (pathlib.Path(__file__).parent / 'startup.pid').write_text(str(child.pid))
    os.kill(os.getpid(), signal.SIGTERM)
    return child
refresh.subprocess.Popen = spawn
sys.exit(refresh.main())
""")
        try:
            result = subprocess.run([sys.executable, str(wrapper), str(ROOT / "integration/refresh.py"),
                                     "--support-dir", str(self.directory)],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
            child = int((self.directory / "startup.pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            pid_path = self.directory / "startup.pid"
            if pid_path.exists():
                try:
                    os.killpg(int(pid_path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_collector_receives_only_the_required_environment(self):
        self.script("""
import json, os, pathlib, sys
for key in ('SYNTHETIC_UNRELATED_SECRET', 'PYTHONINSPECT', 'XDG_DATA_HOME', 'DEVIN_API_URL'):
    assert key not in os.environ, key
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text(json.dumps({'safe_environment': True}))
""")
        with patch.dict(os.environ, {"SYNTHETIC_UNRELATED_SECRET": "synthetic",
                                     "XDG_DATA_HOME": "elsewhere", "DEVIN_API_URL": "https://example.invalid"}):
            result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_collector_never_imports_modules_from_the_working_directory(self):
        # launchd starts the runner in the support directory, and `python -m` puts the
        # working directory ahead of the standard library on sys.path.
        working = self.directory / "working"
        working.mkdir()
        (working / "json.py").write_text("raise SystemExit(9)\n")
        self.script("""
import json, pathlib, sys
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text(json.dumps({'stdlib': True}))
""")
        result = subprocess.run([sys.executable, str(ROOT / "integration/refresh.py"),
                                 "--support-dir", str(self.directory)],
                                cwd=working, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.directory / "snapshot.json").read_text()), {"stdlib": True})

    def test_collector_failure_does_not_publish_old_snapshot_as_fresh(self):
        old = '{"completed": 0}'
        (self.directory / "snapshot.json").write_text(old)
        self.script("raise SystemExit(7)")
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.directory / "snapshot.json").read_text(), old)
        self.assertFalse((self.directory / "published.jsonl").exists())

    def test_uses_installed_cli_path_without_interactive_shell_environment(self):
        cli_directory = self.directory / "cli"
        cli_directory.mkdir()
        cli = cli_directory / "devin"
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o700)
        self.config["path"] = str(cli_directory) + ":/usr/bin:/bin"
        (self.directory / "installation.json").write_text(json.dumps(self.config))
        self.script("""
import json, pathlib, shutil, sys
assert shutil.which('devin') is not None
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text(json.dumps({'cli_found': True}))
""")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.directory / "snapshot.json").read_text()), {'cli_found': True})

    def test_publisher_failure_is_not_reported_as_success(self):
        self.script("""
import pathlib, sys
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text('{}')
""")
        self.publisher.write_text(f"#!{sys.executable}\nraise SystemExit(4)\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.directory / "snapshot.json").exists())

    def test_real_disabled_collector_retries_failed_publication_without_rewriting_snapshot(self):
        self.config["collector_path"] = str(ROOT / "collector")
        self.config["disabled_providers"] = list(refresh.PROVIDERS)
        (self.directory / "installation.json").write_text(json.dumps(self.config))
        working_publisher = self.publisher.read_text()
        self.publisher.write_text(f"#!{sys.executable}\nraise SystemExit(4)\n")
        first = self.invoke()
        self.assertEqual(first.returncode, 1, first.stderr)
        snapshot = self.directory / "snapshot.json"
        saved = snapshot.read_bytes()
        saved_revision = refresh.revision(snapshot)
        self.assertTrue(all(not provider["enabled"] for provider in json.loads(saved)["providers"]))
        # A no-op must also report a repeated publication failure, rather than
        # letting the collector's successful exit hide it.
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(refresh.revision(snapshot), saved_revision)
        self.publisher.write_text(working_publisher)
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(snapshot.read_bytes(), saved)
        self.assertEqual(refresh.revision(snapshot), saved_revision)
        publication = self.directory / "published.jsonl"
        self.assertTrue(publication.exists(), "Saved results were never republished.")
        events = [json.loads(line) for line in publication.read_text().splitlines()]
        self.assertEqual(events, [{"data": json.loads(saved), "reload": True}])
        # The real collector's status line passes the runner's status-only filter.
        self.assertIn("INFO Provider statuses: claude: disabled; codex: disabled; cursor: disabled; devin: disabled.",
                      (self.directory / "logs/refresh.log").read_text())

    def test_timeout_stops_child(self):
        self.script("import time; time.sleep(60)")
        original = refresh.MAX_RUN_SECONDS
        refresh.MAX_RUN_SECONDS = -1
        try:
            result = refresh.collect_and_publish(self.config, self.directory, None,
                                                 logging.getLogger("test.bars"))
        finally:
            refresh.MAX_RUN_SECONDS = original
        self.assertEqual(result, 1)

    def wait_until_zombie(self, pid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                                   capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"):
                return
            time.sleep(0.02)
        self.fail("Synthetic collector never exited")

    def test_stop_process_tolerates_an_exited_unreaped_collector(self):
        # macOS killpg raises PermissionError when only a zombie leader remains.
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        self.wait_until_zombie(process.pid)
        refresh.stop_process(process)
        self.assertEqual(process.returncode, 0)

    def test_timeout_tolerates_a_collector_that_exits_before_it_is_stopped(self):
        self.script("pass")
        real_popen = subprocess.Popen
        test = self

        class LatePoll(real_popen):
            """Reports the collector as running once more after it has exited, unreaped."""
            held = False

            def poll(self):
                if not self.held and "bars_collector" in self.args:
                    self.held = True
                    test.wait_until_zombie(self.pid)
                    return None
                return super().poll()

        original = refresh.MAX_RUN_SECONDS
        refresh.MAX_RUN_SECONDS = -1
        logger = logging.getLogger("test.bars.zombie")
        try:
            with patch.object(refresh.subprocess, "Popen", LatePoll), \
                    self.assertLogs(logger, logging.ERROR) as logs:
                result = refresh.collect_and_publish(self.config, self.directory, None, logger)
        finally:
            refresh.MAX_RUN_SECONDS = original
        self.assertEqual(result, 1)
        self.assertIn("exceeded its time limit", "\n".join(logs.output))

    def record_arguments_collector(self):
        self.script("""
import json, pathlib, sys
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text(json.dumps(sys.argv[1:]))
""")

    def collector_arguments(self):
        return json.loads((self.directory / "snapshot.json").read_text())

    def test_passes_disabled_providers_from_installation_json_on_every_run(self):
        self.record_arguments_collector()
        self.config["disabled_providers"] = ["devin", "codex"]
        (self.directory / "installation.json").write_text(json.dumps(self.config))
        result = self.invoke("--provider", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.collector_arguments()
        self.assertEqual(arguments[arguments.index("--provider"):],
                         ["--provider", "codex", "--disable", "codex", "--disable", "devin"])
        # The app edits the file in place; the next run must see the change.
        self.config["disabled_providers"] = []
        (self.directory / "installation.json").write_text(json.dumps(self.config))
        self.assertEqual(self.invoke().returncode, 0)
        self.assertNotIn("--disable", self.collector_arguments())

    def test_missing_disabled_providers_means_none(self):
        self.record_arguments_collector()
        self.assertNotIn("disabled_providers", self.config)
        self.assertEqual(self.invoke().returncode, 0)
        self.assertNotIn("--disable", self.collector_arguments())

    def test_invalid_disabled_providers_collects_everything_and_logs(self):
        self.record_arguments_collector()
        for value in ("cursor", [1], ["cursor", "unknown"]):
            with self.subTest(value=value):
                self.config["disabled_providers"] = value
                (self.directory / "installation.json").write_text(json.dumps(self.config))
                self.assertEqual(self.invoke().returncode, 0)
                expected = ["--disable", "cursor"] if value == ["cursor", "unknown"] else []
                arguments = self.collector_arguments()
                self.assertEqual(arguments[arguments.index("--output") + 2:], expected)
        log = (self.directory / "logs/refresh.log").read_text()
        self.assertIn("Ignoring invalid disabled_providers", log)
        self.assertIn("Ignoring unknown disabled providers: unknown.", log)

    def test_collector_output_and_snapshot_contents_never_reach_logs(self):
        self.script("""
import json, pathlib, sys
secret = 'synthetic-secret-' + '0123'
print(secret); print(secret, file=sys.stderr)
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text(json.dumps({'used': 98765.43, 'token': secret}))
raise SystemExit(1)
""")
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        log = (self.directory / "logs/refresh.log").read_text()
        for leaked in ("synthetic-secret", "98765"):
            self.assertNotIn(leaked, log + result.stdout + result.stderr)
        self.assertEqual((self.directory / "logs/refresh.log").stat().st_mode & 0o777, 0o600)

    def status_collector(self, stdout, status=0):
        self.script(f"""
import os, pathlib, sys
sys.stdout.buffer.write({stdout!r})
sys.stderr.write('synthetic-secret-stderr')
pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text('{{}}')
raise SystemExit({status})
""")

    def test_provider_statuses_are_logged_with_the_completed_refresh(self):
        self.status_collector(b"claude: ok; codex: setup; cursor: login_required; devin: disabled\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.directory / "logs/refresh.log").read_text().splitlines()
        self.assertIn("INFO Provider statuses: claude: ok; codex: setup; cursor: login_required; devin: disabled.",
                      log[-2])
        self.assertIn("INFO Refresh completed for all providers", log[-1])
        self.assertNotIn("synthetic-secret", "\n".join(log))
        self.assertEqual(list(self.directory.glob("tmp*")), [])

    def test_only_the_exact_status_line_can_reach_the_log(self):
        cases = (
            b"claude: ok; codex: ok 98765.43 synthetic-secret-0123\n",
            b"claude: ok\n2026-01-01 ERROR forged synthetic-secret-0123\n",
            b"claude: ok; claude: ok\n",
            b"synthetic-secret-0123: ok\n",
            "claude: ok; codex: \u00e9rror\n".encode(),
            b"claude: ok; " * 400 + b"devin: ok\n",
        )
        for stdout in cases:
            with self.subTest(stdout=stdout[:60]):
                (self.directory / "logs/refresh.log").unlink(missing_ok=True)
                self.status_collector(stdout)
                result = self.invoke()
                self.assertEqual(result.returncode, 0, result.stderr)
                log = (self.directory / "logs/refresh.log").read_text()
                self.assertIn("WARNING Collector status output was not recognized.", log)
                self.assertNotIn("Provider statuses", log)
                for leaked in ("synthetic-secret", "98765", "forged", "\u00e9"):
                    self.assertNotIn(leaked, log)

    def test_a_failing_log_write_prints_no_traceback(self):
        # launchd copies the runner's stderr into logs/launchd.log.
        self.status_collector(b"claude: ok; codex: ok; cursor: ok; devin: ok\n")
        wrapper = self.directory / "failing_log.py"
        wrapper.write_text("""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('refresh', sys.argv.pop(1))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
def fail(self, record):
    raise OSError('synthetic log failure')
refresh.PrivateRotatingFileHandler.shouldRollover = fail
sys.exit(refresh.main())
""")
        result = subprocess.run([sys.executable, str(wrapper), str(ROOT / "integration/refresh.py"),
                                 "--support-dir", str(self.directory)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue((self.directory / "published.jsonl").exists())

    def test_installation_json_cannot_forge_log_lines(self):
        self.record_arguments_collector()
        self.config["disabled_providers"] = ["evil\n2026-01-01 ERROR forged"]
        (self.directory / "installation.json").write_text(json.dumps(self.config))
        self.assertEqual(self.invoke().returncode, 0)
        log = (self.directory / "logs/refresh.log").read_text()
        self.assertNotIn("forged", log)
        self.assertIn("Ignoring unknown disabled providers: (invalid).", log)

    def test_symlinked_settings_or_lock_run_nothing(self):
        self.script("raise RuntimeError('Must not run')")
        elsewhere = self.directory / "elsewhere.json"
        elsewhere.write_text(json.dumps(self.config))
        (self.directory / "installation.json").unlink()
        (self.directory / "installation.json").symlink_to(elsewhere)
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertIn("(OSError)", result.stderr)  # ELOOP from O_NOFOLLOW
        self.assertFalse((self.directory / "snapshot.json").exists())
        (self.directory / "installation.json").unlink()
        (self.directory / "installation.json").write_text(json.dumps(self.config))
        victim = self.directory / "victim"
        victim.write_text("unchanged")
        (self.directory / "refresh.lock").unlink(missing_ok=True)
        (self.directory / "refresh.lock").symlink_to(victim)
        self.assertEqual(self.invoke().returncode, 1)
        self.assertEqual(victim.read_text(), "unchanged")

    def test_symlinked_support_directory_is_refused(self):
        link = self.directory.parent / (self.directory.name + "-link")
        link.symlink_to(self.directory)
        self.addCleanup(link.unlink)
        self.script("raise RuntimeError('Must not run')")
        result = subprocess.run([sys.executable, str(ROOT / "integration/refresh.py"), "--support-dir", str(link)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("PermissionError", result.stderr)
        self.assertFalse((self.directory / "logs").exists())

    def test_schedule_is_quarter_hour_calendar_with_login_start(self):
        data = installer.launch_agent(self.directory / "bin/refresh", self.directory)
        decoded = plistlib.loads(plistlib.dumps(data))
        self.assertEqual(decoded["StartCalendarInterval"],
                         [{"Minute": 0}, {"Minute": 15}, {"Minute": 30}, {"Minute": 45}])
        self.assertTrue(decoded["RunAtLoad"])
        self.assertNotIn("KeepAlive", decoded)
        self.assertEqual(decoded["ProgramArguments"], [str(self.directory / "bin/refresh")])


class InstallationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bars isolated install ")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "build/Bars.app"
        self.destination = root / "Applications/Bars.app"
        self.support = root / "support"
        self.agents = root / "agents"
        for app, marker in ((self.source, "new"), (self.destination, "old")):
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "local.bars.app", "CFBundleExecutable": "Bars"}))
            (app / "Contents/MacOS/Bars").write_text(marker)
        (self.support / "runtime").mkdir(parents=True)
        (self.support / "runtime/old-marker").write_text("old runtime")
        (self.support / "bin").mkdir()
        (self.support / "bin/refresh").write_text("old launcher")
        (self.support / "installation.json").write_text("old config")
        self.agents.mkdir()
        (self.agents / f"{installer.LABEL}.plist").write_text("old agent")

    def assert_old_installation(self):
        self.assertEqual((self.destination / "Contents/MacOS/Bars").read_text(), "old")
        self.assertEqual((self.support / "runtime/old-marker").read_text(), "old runtime")
        self.assertEqual((self.support / "installation.json").read_text(), "old config")
        self.assertEqual((self.support / "bin/refresh").read_text(), "old launcher")
        self.assertEqual((self.agents / f"{installer.LABEL}.plist").read_text(), "old agent")

    def run_install(self, fake_run, **options):
        # Every filesystem destination is explicit and temporary. No installer
        # subprocess can execute; the only command runner is the test callback.
        with patch.object(installer.subprocess, "run", side_effect=fake_run):
            installer.install(self.source, Path(sys.executable), destination=self.destination,
                              support=self.support, agents=self.agents, **options)

    def detecting_run(self, detected, commands=None):
        def fake_run(args, **kwargs):
            if commands is not None:
                commands.append((args, kwargs))
            if args[1:] == ["-m", "bars_collector", "--detect"]:
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(detected).encode())
            return subprocess.CompletedProcess(args, 0)
        return fake_run

    def install_and_capture(self, fake_run, **options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.run_install(fake_run, **options)
        config = json.loads((self.support / "installation.json").read_text())
        return config, output.getvalue()

    def test_detection_disables_providers_without_logins_and_prints_summary(self):
        commands = []
        detected = {"claude": True, "codex": False, "cursor": True, "devin": False}
        config, output = self.install_and_capture(self.detecting_run(detected, commands))
        self.assertEqual(config["disabled_providers"], ["codex", "devin"])
        # The existing keys stay exactly as they were.
        self.assertEqual(list(config), ["python", "collector_path", "app_executable", "path", "disabled_providers"])
        self.assertEqual(config["collector_path"], str(self.support / "runtime"))
        self.assertIn("  Claude: login found, enabled\n"
                      "  Codex: no login found, disabled\n"
                      "  Cursor: login found, enabled\n"
                      "  Devin: no login found, disabled\n", output)
        self.assertIn(installer.CHANGE_HINT, output)
        args, kwargs = next(c for c in commands if "--detect" in c[0])
        self.assertEqual(args[0], str(Path(sys.executable).absolute()))
        self.assertEqual(kwargs["env"]["PATH"], config["path"])
        self.assertEqual(kwargs["env"]["PYTHONPATH"], str(installer.PROJECT / "collector"))

    def test_explicit_flags_override_detection(self):
        detected = {"claude": True, "codex": False, "cursor": True, "devin": True}
        config, output = self.install_and_capture(self.detecting_run(detected), disable=["devin", "claude"])
        self.assertEqual(config["disabled_providers"], ["claude", "devin"])
        self.assertIn("  Codex: no login found, enabled\n", output)
        self.assertIn("  Devin: login found, disabled\n", output)
        config, _ = self.install_and_capture(self.detecting_run(detected), enable_all=True)
        self.assertEqual(config["disabled_providers"], [])

    def test_detection_closes_stdin_and_filters_the_environment(self):
        commands = []
        with patch.dict(os.environ, {"SYNTHETIC_UNRELATED_SECRET": "synthetic", "PYTHONINSPECT": "1"}):
            self.install_and_capture(self.detecting_run({pid: True for pid in installer.PROVIDERS}, commands))
        _, kwargs = next(c for c in commands if "--detect" in c[0])
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertNotIn("SYNTHETIC_UNRELATED_SECRET", kwargs["env"])
        self.assertNotIn("PYTHONINSPECT", kwargs["env"])

    def test_reinstall_without_flags_keeps_the_existing_selection(self):
        # The dropdown edits disabled_providers in place; an update must not undo that.
        detected = {"claude": True, "codex": True, "cursor": True, "devin": True}
        config, _ = self.install_and_capture(self.detecting_run(detected), disable=["cursor"])
        self.assertEqual(config["disabled_providers"], ["cursor"])
        config, output = self.install_and_capture(self.detecting_run(detected))
        self.assertEqual(config["disabled_providers"], ["cursor"])
        self.assertIn("  Cursor: login found, disabled\n", output)
        config, _ = self.install_and_capture(self.detecting_run(detected), enable_all=True)
        self.assertEqual(config["disabled_providers"], [])

    def test_failed_detection_keeps_every_provider_enabled(self):
        def fake_run(args, **kwargs):
            if "--detect" in args:
                return subprocess.CompletedProcess(args, 1, stdout=b"")
            return subprocess.CompletedProcess(args, 0)
        config, output = self.install_and_capture(fake_run)
        self.assertEqual(config["disabled_providers"], [])
        self.assertIn("  Cursor: login not checked, enabled\n", output)

    def test_install_makes_its_storage_private(self):
        self.support.chmod(0o755)
        (self.support / "logs").mkdir()
        (self.support / "logs/launchd.log").write_text("output from an earlier installation\n")
        (self.support / "logs/launchd.log").chmod(0o644)
        self.install_and_capture(self.detecting_run({pid: True for pid in installer.PROVIDERS}))
        # Each install empties launchd's log, so it cannot grow across reinstalls.
        self.assertEqual((self.support / "logs/launchd.log").stat().st_size, 0)
        for directory in (self.support, self.support / "logs", self.support / "bin"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
        for path in (self.support / "logs/launchd.log", self.support / "installation.json",
                     self.agents / f"{installer.LABEL}.plist"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        self.assertEqual((self.support / "bin/refresh").stat().st_mode & 0o777, 0o700)

    def test_replaced_app_is_unregistered_before_its_staged_copy_is_removed(self):
        calls = []
        def fake_run(args, **kwargs):
            if args[0] == "/usr/bin/pluginkit" and args[1] == "-r" or args[0].endswith("lsregister") and args[1] == "-u":
                bundle = Path(args[2])
                while bundle.suffix != ".app":
                    bundle = bundle.parent
                marker = bundle / "Contents/MacOS/Bars"
                calls.append((args, marker.read_text() if marker.exists() else None))
            return self.detecting_run({pid: True for pid in installer.PROVIDERS})(args, **kwargs)
        self.install_and_capture(fake_run)
        self.assertEqual((self.destination / "Contents/MacOS/Bars").read_text(), "new")
        self.assertEqual(len(calls), 2, calls)
        (pluginkit, pluginkit_marker), (lsregister, lsregister_marker) = calls
        staged = Path(lsregister[2])
        self.assertEqual(staged.name, "old.app")
        self.assertEqual(staged.parent.parent, self.destination.parent)
        self.assertTrue(staged.parent.name.startswith(".bars-install-"))
        self.assertEqual(pluginkit, ["/usr/bin/pluginkit", "-r", str(staged / "Contents/PlugIns/BarsWidget.appex")])
        self.assertEqual(lsregister, [installer.LSREGISTER, "-u", str(staged)])
        # Both ran while the replaced bundle still existed; the staging directory is gone.
        self.assertEqual((pluginkit_marker, lsregister_marker), ("old", "old"))
        self.assertFalse(staged.parent.exists())

    def test_failed_staged_unregistration_still_removes_staging(self):
        def fake_run(args, **kwargs):
            if args[0] == "/usr/bin/pluginkit" and args[1] == "-r" or args[0].endswith("lsregister") and args[1] == "-u":
                raise OSError("synthetic missing tool")
            return self.detecting_run({pid: True for pid in installer.PROVIDERS})(args, **kwargs)
        self.install_and_capture(fake_run)
        self.assertEqual((self.destination / "Contents/MacOS/Bars").read_text(), "new")
        self.assertEqual(list(self.destination.parent.glob(".bars-install-*")), [])

    def test_installer_waits_longer_than_a_refresh_can_hold_its_lock(self):
        import inspect
        default = inspect.signature(installer.refresh_lock).parameters["timeout"].default
        self.assertEqual(default, installer.REFRESH_LOCK_WAIT_SECONDS)
        self.assertEqual(default, 160)
        # The run limit, then a 10 s TERM wait, 3 s KILL reap and 15 s publication.
        self.assertGreater(default, refresh.MAX_RUN_SECONDS + 10 + 3 + 15)

    def test_install_without_write_access_to_the_applications_folder_changes_nothing(self):
        # A standard account cannot write to /Applications: stop before any storage,
        # login lookup or scheduled job is touched, and never ask for elevation.
        commands = []
        parent = self.destination.parent
        parent.chmod(0o555)
        self.addCleanup(parent.chmod, 0o755)
        with self.assertRaises(RuntimeError) as raised:
            self.run_install(self.detecting_run({pid: True for pid in installer.PROVIDERS}, commands))
        self.assertIn(str(parent), str(raised.exception))
        self.assert_old_installation()
        self.assertFalse((self.support / "logs").exists())
        self.assertFalse(any("--check-storage" in args or "--detect" in args or "launchctl" in args[0]
                             for args, _ in commands), commands)

    def test_install_refuses_to_replace_another_accounts_app(self):
        # /Applications/Bars.app belongs to whoever installed it first; renaming it needs write
        # access to the bundle, so a second account must stop before touching anything.
        commands = []
        self.assertTrue(self.destination.exists())
        owner = os.lstat(self.destination).st_uid
        with unittest.mock.patch.object(installer.os, "getuid", return_value=owner + 1):
            with self.assertRaises(RuntimeError) as raised:
                self.run_install(self.detecting_run({pid: True for pid in installer.PROVIDERS}, commands))
        self.assertIn("another user account", str(raised.exception))
        self.assert_old_installation()
        self.assertFalse(any("--check-storage" in args or "--detect" in args or "launchctl" in args[0]
                             for args, _ in commands), commands)

    def test_a_relative_path_entry_is_never_pinned_into_the_scheduled_path(self):
        working = Path(self.temporary.name) / "downloads"
        (working / "relative-bin").mkdir(parents=True)
        cli = working / "relative-bin/devin"
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o700)
        previous = os.getcwd()
        os.chdir(working)
        self.addCleanup(os.chdir, previous)
        with patch.dict(os.environ, {"PATH": "relative-bin:/usr/bin:/bin"}):
            config, _ = self.install_and_capture(self.detecting_run({pid: True for pid in installer.PROVIDERS}))
        entries = config["path"].split(":")
        self.assertTrue(all(os.path.isabs(entry) for entry in entries), entries)
        self.assertNotIn(os.path.realpath(working / "relative-bin"), [os.path.realpath(e) for e in entries])

    def test_install_refuses_a_symlinked_support_directory(self):
        real = self.support.parent / "real-support"
        self.support.rename(real)
        self.support.symlink_to(real)
        with self.assertRaises(RuntimeError):
            self.run_install(self.detecting_run({pid: True for pid in installer.PROVIDERS}))
        self.assertEqual((real / "installation.json").read_text(), "old config")
        self.assertEqual((self.destination / "Contents/MacOS/Bars").read_text(), "old")

    def test_process_lookup_is_limited_to_this_user(self):
        commands = []
        def fake_run(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 1, stdout=b"")
        with patch.object(installer.subprocess, "run", side_effect=fake_run):
            installer.running_processes(Path("/Applications/Bars.app/Contents/MacOS/Bars"))
        self.assertEqual(commands[0][:3], ["/usr/bin/pgrep", "-U", str(os.getuid())])

    def test_terminate_tolerates_processes_it_may_not_signal(self):
        with patch.object(installer.os, "kill", side_effect=PermissionError):
            installer.terminate([4242], timeout=0.1)

    def test_home_comes_from_the_account_not_the_environment(self):
        import pwd
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for value in ("", "/", "relative", "/tmp/elsewhere"):
            with self.subTest(home=value), patch.dict(os.environ, {"HOME": value}):
                self.assertEqual(installer.user_home(), expected)

    def test_refuses_to_run_as_root(self):
        with patch.object(installer.os, "geteuid", return_value=0), \
                patch.object(installer, "install", side_effect=AssertionError("installed")), \
                patch.object(installer, "uninstall", side_effect=AssertionError("uninstalled")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main([]), 1)
            self.assertEqual(installer.main(["--uninstall"]), 1)

    def test_command_line_rejects_conflicting_provider_flags(self):
        for argv in (["--disable", "cursor", "--enable-all"], ["--keep-data"],
                     ["--uninstall", "--disable", "cursor"], ["--disable", "unknown"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    installer.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_command_line_passes_provider_flags(self):
        with patch.object(installer, "install") as fake:
            self.assertEqual(installer.main(["--disable", "cursor", "--disable", "codex", "--no-start"]), 0)
        self.assertEqual(fake.call_args.kwargs, {"start": False, "disable": ["cursor", "codex"], "enable_all": False})
        with patch.object(installer, "install") as fake:
            installer.main(["--enable-all"])
        self.assertEqual(fake.call_args.kwargs["disable"], None)
        self.assertTrue(fake.call_args.kwargs["enable_all"])

    def test_storage_preflight_failure_leaves_previous_installation_untouched(self):
        commands = []
        def fake_run(args, **kwargs):
            commands.append(args)
            if "--check-storage" in args:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install(fake_run)
        self.assert_old_installation()
        self.assertFalse(any("launchctl" in args[0] for args in commands))

    def test_registration_failure_rolls_back_files_and_previous_job(self):
        commands = []
        failed = False
        def fake_run(args, **kwargs):
            nonlocal failed
            commands.append(args)
            if args[0].endswith("lsregister") and not failed:
                failed = True
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install(fake_run)
        self.assert_old_installation()
        self.assertTrue(any("bootstrap" in args for args in commands))

    def test_successful_install_replaces_stale_app_and_widget_processes(self):
        commands = []
        killed = []
        widget = str(self.destination / "Contents/PlugIns/BarsWidget.appex/Contents/MacOS/BarsWidget")
        app = str(self.destination / "Contents/MacOS/Bars")
        def fake_run(args, **kwargs):
            commands.append(args)
            if args[0] == "/usr/bin/pgrep":
                if args[-1] == "^" + re.escape(widget) + "( |$)":
                    return subprocess.CompletedProcess(args, 0, stdout=b"4242\n")
                if args[-1] == "^" + re.escape(app) + "( |$)":
                    return subprocess.CompletedProcess(args, 0, stdout=b"4343\n")
                return subprocess.CompletedProcess(args, 1, stdout=b"")
            return subprocess.CompletedProcess(args, 0)
        def fake_kill(pid, sig):
            killed.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # The process ended after SIGTERM.
        with patch.object(installer.os, "kill", side_effect=fake_kill):
            self.run_install(fake_run)
        self.assertEqual((self.destination / "Contents/MacOS/Bars").read_text(), "new")
        self.assertIn((4242, signal.SIGTERM), killed)
        self.assertIn((4343, signal.SIGTERM), killed)
        self.assertFalse(any(sig == signal.SIGKILL for _, sig in killed))
        register = next(i for i, a in enumerate(commands) if a[0].endswith("lsregister"))
        reload = next(i for i, a in enumerate(commands) if a[-1] == "--reload-widgets")
        relaunch = next(i for i, a in enumerate(commands) if a[0] == "/usr/bin/open")
        self.assertLess(register, reload)
        self.assertLess(register, relaunch)
        self.assertEqual(commands[reload][0], app)
        self.assertEqual(commands[relaunch], ["/usr/bin/open", "-g", str(self.destination)])

    def test_failed_install_leaves_running_processes_alone(self):
        killed = []
        def fake_run(args, **kwargs):
            if args[0] == "/usr/bin/pgrep":
                return subprocess.CompletedProcess(args, 0, stdout=b"4242\n")
            if args[0].endswith("lsregister"):
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with patch.object(installer.os, "kill", side_effect=lambda pid, sig: killed.append(pid)):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_install(fake_run)
        self.assert_old_installation()
        self.assertEqual(killed, [])

    def test_activation_failure_rolls_back_files_and_previous_job(self):
        bootstraps = []
        def fake_run(args, **kwargs):
            if "bootstrap" in args:
                bootstraps.append(args)
                if len(bootstraps) == 1:
                    raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install(fake_run)
        self.assert_old_installation()
        self.assertEqual(len(bootstraps), 2)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bars isolated uninstall ")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.destination = root / "Applications/Bars.app"
        self.support = self.home / "Library/Application Support/Bars"
        self.agents = self.home / "Library/LaunchAgents"
        (self.destination / "Contents/MacOS").mkdir(parents=True)
        (self.destination / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "local.bars.app", "CFBundleExecutable": "Bars"}))
        for name in ("runtime", "bin", "logs", "src"):
            (self.support / name).mkdir(parents=True)
        (self.support / "snapshot.json").write_text("{}")
        (self.support / "installation.json").write_text("{}")
        self.agents.mkdir(parents=True)
        self.plist = self.agents / f"{installer.LABEL}.plist"
        self.plist.write_text("agent")
        self.preferences = self.home / "Library/Preferences"
        self.preferences.mkdir(parents=True)
        self.preferences_file = self.preferences / "local.bars.app.plist"
        self.preferences_file.write_bytes(plistlib.dumps({"NSWindow Frame overview": "synthetic"}))
        self.other_preferences = self.preferences / "com.example.other.plist"
        self.other_preferences.write_text("unrelated")
        # Provider credentials that uninstall must never touch.
        self.credentials = [self.home / ".codex/auth.json", self.home / ".local/share/devin/credentials.toml"]
        for path in self.credentials:
            path.parent.mkdir(parents=True)
            path.write_text("synthetic")
        home = patch.dict(os.environ, {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)

    def uninstall(self, loaded=True, pids=(), defaults="cfprefsd", **options):
        commands, killed = [], []
        widget = "^" + re.escape(str(self.destination / "Contents/PlugIns/BarsWidget.appex/Contents/MacOS/BarsWidget")) + "( |$)"
        def fake_run(args, **kwargs):
            commands.append(args)
            if args[0] == "/usr/bin/defaults":
                # "cfprefsd": deletes the domain's file and succeeds only if it existed.
                # "stale": succeeds but leaves the file. "missing": the tool cannot run.
                if defaults == "missing":
                    raise FileNotFoundError(args[0])
                if defaults == "stale":
                    return subprocess.CompletedProcess(args, 0)
                existed = self.preferences_file.exists()
                self.preferences_file.unlink(missing_ok=True)
                return subprocess.CompletedProcess(args, 0 if existed else 1)
            if args[:2] == ["/bin/launchctl", "print"]:
                return subprocess.CompletedProcess(args, 0 if loaded else 113)
            if args[0] == "/usr/bin/pgrep" and args[-1] == widget and pids:
                return subprocess.CompletedProcess(args, 0, stdout=" ".join(map(str, pids)).encode())
            if args[0] == "/usr/bin/pgrep":
                return subprocess.CompletedProcess(args, 1, stdout=b"")
            return subprocess.CompletedProcess(args, 0)
        def fake_kill(pid, sig):
            killed.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError
        with patch.object(installer.subprocess, "run", side_effect=fake_run), \
                patch.object(installer.os, "kill", side_effect=fake_kill), \
                contextlib.redirect_stderr(io.StringIO()):
            removed = installer.uninstall(destination=self.destination, support=self.support,
                                          agents=self.agents, preferences=self.preferences, **options)
        return removed, commands, killed

    def assert_credentials_untouched(self):
        for path in self.credentials:
            self.assertEqual(path.read_text(), "synthetic")

    def test_another_accounts_app_stops_uninstall_before_any_side_effect(self):
        original = {p.relative_to(self.home): p.read_bytes()
                    for p in self.home.rglob("*") if p.is_file()}
        info = self.destination / "Contents/Info.plist"
        original_info = info.read_bytes()
        for keep_data in (False, True):
            with self.subTest(keep_data=keep_data), \
                    patch.object(installer.os, "getuid", return_value=self.destination.stat().st_uid + 1), \
                    patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout=b"")) as run, \
                    patch.object(installer.os, "kill") as kill:
                with self.assertRaisesRegex(RuntimeError, "belongs to another user account"):
                    installer.uninstall(destination=self.destination, support=self.support,
                                        agents=self.agents, preferences=self.preferences,
                                        keep_data=keep_data)
                run.assert_not_called()
                kill.assert_not_called()
                self.assertEqual(info.read_bytes(), original_info)
                self.assertEqual({p.relative_to(self.home): p.read_bytes()
                                  for p in self.home.rglob("*") if p.is_file()}, original)

    def test_removes_job_processes_app_and_data(self):
        removed, commands, killed = self.uninstall(pids=(4242,))
        service = f"gui/{os.getuid()}/{installer.LABEL}"
        self.assertIn(["/bin/launchctl", "bootout", service], commands)
        self.assertIn((4242, signal.SIGTERM), killed)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.support.exists())
        self.assertTrue(any(a[0] == "/usr/bin/pluginkit" and a[1] == "-r" for a in commands))
        self.assertTrue(any(a[0].endswith("lsregister") and a[1] == "-u" for a in commands))
        self.assertFalse(self.preferences_file.exists())
        self.assertEqual(self.other_preferences.read_text(), "unrelated")
        self.assertIn("Removed the Bars preferences (local.bars.app).", removed)
        self.assertEqual(len(removed), 6)
        self.assert_credentials_untouched()

    def test_unregisters_the_live_app_before_deleting_it(self):
        seen = []
        real_unregister = installer.unregister_app
        def recording_unregister(app):
            seen.append((app, app.exists()))
            real_unregister(app)
        with patch.object(installer, "unregister_app", side_effect=recording_unregister):
            _, commands, _ = self.uninstall()
        self.assertEqual(seen, [(self.destination, True)])
        appex = str(self.destination / "Contents/PlugIns/BarsWidget.appex")
        unregister = [a for a in commands if a[0] == "/usr/bin/pluginkit" or a[0].endswith("lsregister")]
        self.assertEqual(unregister, [["/usr/bin/pluginkit", "-r", appex],
                                      [installer.LSREGISTER, "-u", str(self.destination)]])

    def test_preferences_file_is_removed_when_defaults_leaves_it_or_cannot_run(self):
        for mode in ("stale", "missing"):
            with self.subTest(defaults=mode):
                self.preferences_file.write_bytes(plistlib.dumps({"NSWindow Frame overview": "synthetic"}))
                removed, commands, _ = self.uninstall(defaults=mode, keep_data=True)
                self.assertIn(["/usr/bin/defaults", "delete", "local.bars.app"], commands)
                self.assertFalse(self.preferences_file.exists())
                self.assertIn("Removed the Bars preferences (local.bars.app).", removed)
                self.assertEqual(self.other_preferences.read_text(), "unrelated")

    def test_preferences_link_is_removed_without_touching_its_target(self):
        target = self.home / "elsewhere.plist"
        target.write_text("user file")
        self.preferences_file.unlink()
        self.preferences_file.symlink_to(target)
        self.uninstall(defaults="stale")
        self.assertFalse(self.preferences_file.is_symlink())
        self.assertEqual(target.read_text(), "user file")

    def test_registration_cleanup_failures_are_tolerated(self):
        def fake_run(args, **kwargs):
            if args[0] == "/usr/bin/pluginkit" or args[0].endswith("lsregister"):
                raise OSError("synthetic missing tool")
            if args[0] == "/usr/bin/pgrep":
                return subprocess.CompletedProcess(args, 1, stdout=b"")
            return subprocess.CompletedProcess(args, 1)
        with patch.object(installer.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stderr(io.StringIO()):
            installer.uninstall(destination=self.destination, support=self.support,
                                agents=self.agents, preferences=self.preferences)
        self.assertFalse(self.destination.exists())

    def test_keep_data_keeps_support_directory(self):
        removed, _, _ = self.uninstall(keep_data=True)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.plist.exists())
        self.assertEqual((self.support / "snapshot.json").read_text(), "{}")
        self.assertTrue((self.support / "src").is_dir())
        self.assertIn(f"Kept {self.support} (--keep-data).", removed)
        self.assert_credentials_untouched()

    def test_is_idempotent(self):
        self.uninstall()
        removed, commands, _ = self.uninstall(loaded=False)
        self.assertEqual(removed, [])
        self.assertFalse(any("bootout" in args for args in commands))
        self.assert_credentials_untouched()

    def test_leaves_an_unrelated_application_in_place(self):
        (self.destination / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
        _, commands, killed = self.uninstall(pids=(4242,))
        self.assertTrue((self.destination / "Contents/Info.plist").exists())
        # Its processes are not Bars' to end either.
        self.assertEqual(killed, [])
        self.assertFalse(any(a[0] == "/usr/bin/pluginkit" for a in commands))
        self.assertFalse(self.support.exists())

    def test_symlinked_support_and_app_are_left_with_their_targets(self):
        elsewhere = self.home / "elsewhere"
        (elsewhere / "data").mkdir(parents=True)
        (elsewhere / "data/keep").write_text("user file")
        shutil.rmtree(self.support)
        self.support.symlink_to(elsewhere / "data")
        real_app = self.home / "OtherBars.app"
        self.destination.rename(real_app)
        self.destination.symlink_to(real_app)
        _, commands, _ = self.uninstall()
        self.assertTrue(self.support.is_symlink())
        self.assertEqual((elsewhere / "data/keep").read_text(), "user file")
        self.assertTrue(self.destination.is_symlink())
        self.assertTrue((real_app / "Contents/Info.plist").exists())
        self.assertFalse(any(a[0] == "/usr/bin/pluginkit" for a in commands))
        self.assertFalse(self.plist.exists())
        self.assert_credentials_untouched()

    def test_empty_home_cannot_redirect_removal(self):
        # Defaults come from the password database; a broken HOME changes nothing.
        with patch.dict(os.environ, {"HOME": ""}):
            self.assertEqual(installer.user_home(), Path(installer.pwd.getpwuid(os.getuid()).pw_dir))

    def test_command_line_reports_nothing_to_remove(self):
        output = io.StringIO()
        with patch.object(installer, "uninstall", return_value=[]) as fake, contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["--uninstall", "--keep-data"]), 0)
        self.assertEqual(fake.call_args.kwargs, {"keep_data": True})
        self.assertIn("nothing to remove", output.getvalue())


if __name__ == "__main__":
    unittest.main()
