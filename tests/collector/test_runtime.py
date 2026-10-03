from contextlib import contextmanager
import copy
import functools
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from bars_collector import adapters, parsers
from bars_collector.model import CollectionError, validate_snapshot
from bars_collector.runner import CollectionCancelled, collect_results, run, stop_worker
from bars_collector.storage import AlreadyRunning, load_snapshot, write_snapshot, writer_lock

OLD = "2026-09-30T01:00:00Z"
NEW = "2026-10-01T01:00:00Z"


def success():
    return parsers.devin({"overage_balance": 12.5})


def slow():
    time.sleep(5)
    return success()


def sensitive_failure():
    raise ValueError("Secret synthetic token must never escape")


def login_failure():
    raise CollectionError("Login required.", "login_required")


def child_command(pid_path):
    # The child ignores TERM so the forced process-group cleanup is exercised.
    return [sys.executable, "-c", "import os,signal,sys,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(20)", str(pid_path)]


def subprocess_collector(pid_path):
    subprocess.run(child_command(pid_path), check=True)
    return success()


def orphaning_collector(pid_path):
    command = child_command(pid_path)
    os.posix_spawn(command[0], command, os.environ)
    deadline = time.monotonic() + 5
    while not Path(pid_path).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return success()


def delayed_success():
    time.sleep(1.5)
    return success()


def exit_without_reporting():
    # Ends the worker without a result, so it can sit unreaped while results are handled.
    time.sleep(0.2)
    os._exit(0)


def exited_group_leader(pid_path=None):
    """A worker group leader that exits at once, optionally leaving a TERM-ignoring child."""
    os.setsid()
    if pid_path is not None:
        command = child_command(pid_path)
        os.posix_spawn(command[0], command, os.environ)
        deadline = time.monotonic() + 5
        while not Path(pid_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    os._exit(0)


def synthetic_coordinator(pid_paths):
    collectors = dict(zip(("claude", "codex", "cursor", "devin"),
                          (functools.partial(subprocess_collector, path) for path in pid_paths)))
    try:
        list(collect_results(list(collectors), timeout=20, collectors=collectors))
    except CollectionCancelled:
        raise SystemExit(143)


def competing_writer(path, connection):
    try:
        with writer_lock(path):
            connection.send("entered")
    except AlreadyRunning:
        connection.send("busy")
    finally:
        connection.close()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "private" / "snapshot.json"

    def ok(self, date=OLD):
        return dict(status="ok", message=None, fetched_at=date, last_attempt_at=date, **success())

    def test_each_result_publishes_before_the_next_completes(self):
        def results():
            yield "devin", self.ok()
            stored = load_snapshot(self.output)
            self.assertEqual(stored["providers"][3]["fetched_at"], OLD)
            self.assertIsNone(stored["providers"][0]["fetched_at"])
            yield "claude", self.ok(NEW)
        run(self.output, ["devin", "claude"], results=results())
        self.assertEqual(load_snapshot(self.output)["providers"][0]["fetched_at"], NEW)

    def test_failure_preserves_values_and_original_fetch_age(self):
        run(self.output, ["devin"], results=iter([("devin", self.ok())]))
        previous = load_snapshot(self.output)["providers"][3]
        run(self.output, ["devin"], results=iter([("devin", dict(
            status="login_required", message="Login required.", last_attempt_at=NEW))]))
        current = load_snapshot(self.output)["providers"][3]
        for key in ("metrics", "fetched_at", "primary_metric_id"):
            self.assertEqual(current[key], previous[key])
        self.assertEqual(current["last_attempt_at"], NEW)
        self.assertEqual(current["status"], "login_required")

    def test_single_provider_retains_others_exactly(self):
        run(self.output, ["claude"], results=iter([("claude", self.ok())]))
        previous = copy.deepcopy(load_snapshot(self.output)["providers"][0])
        run(self.output, ["devin"], results=iter([("devin", self.ok(NEW))]))
        self.assertEqual(load_snapshot(self.output)["providers"][0], previous)

    def test_allowance_changes_replace_metrics_but_failed_attempts_retain_them(self):
        monthly = parsers.codex({"spend_control": {"individual_limit": {
            "unit": "credit", "used": 25, "limit": 100}}})
        subscription = parsers.codex({"rate_limit": {"primary_window": {"used_percent": 30}}})
        weekly_only = parsers.codex({"rate_limit": {"secondary_window": {"used_percent": 40}}})
        for value in (monthly, subscription, weekly_only, monthly):
            success_value = dict(status="ok", message=None, fetched_at=OLD, last_attempt_at=OLD, **value)
            run(self.output, ["codex"], results=iter([("codex", success_value)]))
            previous = validate_snapshot(load_snapshot(self.output))["providers"][1]
            self.assertEqual(previous["metrics"], value["metrics"])
            self.assertEqual(previous["primary_metric_id"], value["primary_metric_id"])
            run(self.output, ["codex"], results=iter([("codex", dict(
                status="error", message="Usage unavailable.", last_attempt_at=NEW))]))
            retained = load_snapshot(self.output)["providers"][1]
            for field in ("metrics", "primary_metric_id", "fetched_at"):
                self.assertEqual(retained[field], previous[field])

    def test_atomic_failure_keeps_old_file_and_cleans_temp(self):
        run(self.output, ["devin"], results=iter([("devin", self.ok())]))
        old_bytes = self.output.read_bytes()
        with patch("bars_collector.storage.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                run(self.output, ["devin"], results=iter([("devin", self.ok(NEW))]))
        self.assertEqual(self.output.read_bytes(), old_bytes)
        self.assertEqual(list(self.output.parent.glob(".snapshot.json.*")), [])

    def test_output_and_lock_permissions_are_private(self):
        run(self.output, ["devin"], results=iter([("devin", self.ok())]))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.output.parent / "snapshot.json.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.parent.stat().st_mode & 0o777, 0o700)

    def test_competing_process_cannot_enter_writer_lock(self):
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(False)
        with writer_lock(self.output):
            process = context.Process(target=competing_writer, args=(self.output, send))
            process.start()
            send.close()
            self.assertTrue(receive.poll(5))
            self.assertEqual(receive.recv(), "busy")
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        receive.close()
        with writer_lock(self.output):
            pass

    def test_corrupt_cache_is_not_overwritten(self):
        self.output.parent.mkdir()
        self.output.write_text('{"schema_version": 999}')
        with self.assertRaises(ValueError):
            run(self.output, ["devin"], results=iter([("devin", self.ok())]))
        self.assertEqual(json.loads(self.output.read_text()), {"schema_version": 999})

    def test_worker_deadline_and_fast_provider_independence(self):
        started = time.monotonic()
        stream = collect_results(["claude", "devin"], timeout=1.5, collectors={"claude": slow, "devin": success})
        pid, value = next(stream)
        self.assertEqual(pid, "devin")
        self.assertEqual(value["status"], "ok")
        self.assertLess(time.monotonic() - started, 1.5)
        rest = list(stream)
        self.assertEqual(rest[0][0], "claude")
        self.assertEqual(rest[0][1]["status"], "error")
        self.assertLess(time.monotonic() - started, 4)

    def test_workers_sanitize_unexpected_errors(self):
        values = dict(collect_results(["claude", "devin"], collectors={"claude": sensitive_failure, "devin": login_failure}))
        self.assertNotIn("Secret", json.dumps(values))
        self.assertEqual(values["claude"]["status"], "error")
        self.assertEqual(values["devin"]["status"], "login_required")

    def assert_descendant_exited(self, path):
        self.assertTrue(path.exists(), "Synthetic subprocess never started")
        pid = int(path.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        # Keep a failing regression test from leaving its synthetic sleeper alive.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        self.fail("Collector left its subprocess descendant alive")

    def test_worker_timeout_kills_stubborn_subprocess_descendant(self):
        path = Path(self.directory.name) / "child.pid"
        values = list(collect_results(["devin"], timeout=3,
                      collectors={"devin": functools.partial(subprocess_collector, path)}))
        self.assertEqual(values[0][1]["status"], "error")
        self.assert_descendant_exited(path)

    def test_success_also_kills_leftover_subprocess_descendant(self):
        path = Path(self.directory.name) / "child.pid"
        values = list(collect_results(["devin"], timeout=5,
                      collectors={"devin": functools.partial(orphaning_collector, path)}))
        self.assertEqual(values[0][1]["status"], "ok")
        self.assert_descendant_exited(path)

    def test_closing_results_cleans_unfinished_subprocess(self):
        path = Path(self.directory.name) / "child.pid"
        stream = collect_results(["claude", "devin"], timeout=10,
                                 collectors={"claude": success,
                                             "devin": functools.partial(subprocess_collector, path)})
        try:
            self.assertEqual(next(stream)[0], "claude")
            deadline = time.monotonic() + 5
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stream.close()
        self.assert_descendant_exited(path)

    def test_a_worker_that_exits_before_cleanup_loses_no_results(self):
        # Regression: macOS killpg raises PermissionError for a group whose only member
        # is an exited, unreaped leader. The pauses let each later worker reach that
        # state before the coordinator handles it.
        stream = collect_results(["claude", "codex", "devin"], timeout=10,
                                 collectors={"claude": success, "codex": exit_without_reporting,
                                             "devin": delayed_success})
        values = {}
        for pid, value in stream:
            values[pid] = value["status"]
            time.sleep(1)
        self.assertEqual(values, {"claude": "ok", "codex": "error", "devin": "ok"})

    def exited_worker(self, pid_path=None):
        process = multiprocessing.get_context("spawn").Process(target=exited_group_leader, args=(pid_path,))
        process.start()
        # Wait without reaping: the exited leader stays a zombie until stop_worker reaps it.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(process.pid)],
                                   capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"):
                return process
            time.sleep(0.02)
        process.kill()
        process.join(2)
        self.fail("Synthetic worker never exited")

    def test_stop_worker_tolerates_an_exited_unreaped_leader(self):
        process = self.exited_worker()
        stop_worker(process)
        self.assertEqual(process.exitcode, 0)

    def test_stop_worker_kills_a_child_left_by_an_exited_leader(self):
        path = Path(self.directory.name) / "child.pid"
        process = self.exited_worker(path)
        stop_worker(process)
        self.assertEqual(process.exitcode, 0)
        self.assert_descendant_exited(path)

    def test_sigterm_coordinator_cleans_all_worker_subprocesses(self):
        paths = [Path(self.directory.name) / (str(index) + ".pid") for index in range(4)]
        coordinator = multiprocessing.get_context("spawn").Process(target=synthetic_coordinator, args=(paths,))
        coordinator.start()
        try:
            deadline = time.monotonic() + 8
            while not all(path.exists() for path in paths) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(all(path.exists() for path in paths))
            started = time.monotonic()
            os.kill(coordinator.pid, signal.SIGTERM)
            coordinator.join(timeout=5)
            self.assertEqual(coordinator.exitcode, 143)
            self.assertLess(time.monotonic() - started, 5)
            for path in paths:
                self.assert_descendant_exited(path)
        finally:
            if coordinator.is_alive():
                coordinator.kill()
                coordinator.join(2)
            for path in paths:
                if path.exists():
                    try:
                        os.kill(int(path.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_http_errors_never_include_sensitive_body_or_url(self):
        for code, expected in ((401, "login_required"), (403, "login_required"), (429, "error"), (500, "error")):
            error = urllib.error.HTTPError("https://secret.example/private", code, "sensitive message", {}, None)
            with patch("bars_collector.adapters.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(CollectionError) as raised:
                    adapters.http_json("https://vendor.example/usage", {})
                self.assertEqual(raised.exception.status, expected)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("sensitive", str(raised.exception))

    def test_redirects_never_forward_credentials(self):
        self.assertIsNone(adapters.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_snapshot_rejects_nonfinite_amounts_and_bad_primary(self):
        run(self.output, ["devin"], results=iter([("devin", self.ok())]))
        snapshot = load_snapshot(self.output)
        snapshot["providers"][3]["metrics"][0]["remaining"] = float("nan")
        with self.assertRaises(CollectionError):
            validate_snapshot(snapshot)
        snapshot = load_snapshot(self.output)
        snapshot["providers"][3]["primary_metric_id"] = "absent"
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot)
        snapshot = load_snapshot(self.output)
        snapshot["providers"][3]["metrics"][0]["period"] = "fortnight"
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot)
        # Caches written before the period field remain valid.
        snapshot = load_snapshot(self.output)
        del snapshot["providers"][3]["metrics"][0]["period"]
        validate_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
