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

from bars_collector.model import CollectionError, validate_snapshot
from bars_collector.runner import CollectionCancelled, collect_results, stop_worker
from bars_plugin import access
from plugin_fixtures import parse, run_standard as run
from bars_collector.storage import AlreadyRunning, load_snapshot, write_snapshot, writer_lock

OLD = "2026-09-30T01:00:00Z"
NEW = "2026-10-01T01:00:00Z"


def success():
    return parse("devin")({"overage_balance": 12.5})


def slow():
    time.sleep(5)
    return success()


def sensitive_failure():
    raise ValueError("Secret synthetic token must never escape")


def login_failure():
    raise CollectionError("Login required.", "login_required")


def count_attempt(path):
    """Record one attempt in `path` and return the attempt number."""
    path = Path(path)
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count


def flaky_then_ok(path):
    if count_attempt(path) < 3:
        raise CollectionError("Provider could not be reached. Collection will retry later.")
    return success()


def always_failing(path):
    count_attempt(path)
    raise CollectionError("Provider could not be reached. Collection will retry later.")


def login_failure_counting(path):
    count_attempt(path)
    raise CollectionError("Login required.", "login_required")


def rate_limited_counting(path):
    count_attempt(path)
    raise CollectionError("Provider rate limit reached. Collection will retry later.", retry_after=120)


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
        monthly = parse("codex")({"spend_control": {"individual_limit": {
            "unit": "credit", "used": 25, "limit": 100}}})
        subscription = parse("codex")({"rate_limit": {"primary_window": {"used_percent": 30}}})
        weekly_only = parse("codex")({"rate_limit": {"secondary_window": {"used_percent": 40}}})
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

    def test_transient_failures_retry_with_backoff_inside_the_deadline(self):
        counts = {pid: Path(self.directory.name) / (pid + ".count") for pid in ("flaky", "stuck", "login")}
        started = time.monotonic()
        values = dict(collect_results(["flaky", "stuck", "login"], timeout=6, collectors={
            "flaky": functools.partial(flaky_then_ok, counts["flaky"]),
            "stuck": functools.partial(always_failing, counts["stuck"]),
            "login": functools.partial(login_failure_counting, counts["login"])}))
        elapsed = time.monotonic() - started
        # Two failures, two backoff waits (about 1 s and 2 s, jittered), then success.
        self.assertEqual(values["flaky"]["status"], "ok")
        self.assertEqual(counts["flaky"].read_text(), "3")
        # A persistent transient failure keeps retrying until the budget ends, then reports it.
        self.assertEqual(values["stuck"]["status"], "error")
        self.assertGreaterEqual(int(counts["stuck"].read_text()), 3)
        self.assertLess(elapsed, 6 + 2)
        # A rejected login is not transient: one attempt, no waiting.
        self.assertEqual(values["login"]["status"], "login_required")
        self.assertEqual(counts["login"].read_text(), "1")

    def test_rate_limits_are_not_retried_within_a_run_and_carry_the_pause(self):
        # Asking again is what a 429 forbids: one attempt, no waiting, and the provider's
        # Retry-After travels with the failure so the collector can pause the provider.
        count = Path(self.directory.name) / "limited.count"
        started = time.monotonic()
        values = dict(collect_results(["limited"], timeout=6,
                                      collectors={"limited": functools.partial(rate_limited_counting, count)}))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(count.read_text(), "1")
        self.assertEqual(values["limited"]["status"], "error")
        self.assertEqual(values["limited"]["retry_after"], 120)
        from bars_collector.model import validate_result
        validate_result(values["limited"])
        for bad in (-1, 10 ** 9, True, "60"):
            with self.assertRaises(ValueError):
                validate_result({"status": "error", "message": "x", "last_attempt_at": "2026-10-01T00:00:00Z", "retry_after": bad})

    def test_rate_limit_pauses_escalate_and_a_success_clears_them(self):
        from datetime import datetime, timezone
        from bars_collector.model import RATE_LIMIT_FIRST_PAUSE, RATE_LIMIT_MAX_PAUSE, is_pausing, rate_limit_pause
        from bars_collector.runner import apply_result
        at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        fresh = dict(next_attempt_at=None, last_attempt_at=None)
        self.assertEqual(rate_limit_pause(fresh, 0, at), RATE_LIMIT_FIRST_PAUSE)
        # A provider's own Retry-After is a floor, not a ceiling; the first pause stays the minimum.
        self.assertEqual(rate_limit_pause(fresh, 900, at), 900)
        self.assertEqual(rate_limit_pause(fresh, 10, at), RATE_LIMIT_FIRST_PAUSE)
        # Consecutive limits double the previous pause up to the cap.
        previous = dict(last_attempt_at="2026-10-01T11:00:00Z", next_attempt_at="2026-10-01T11:05:00Z")
        self.assertEqual(rate_limit_pause(previous, 0, at), 2 * RATE_LIMIT_FIRST_PAUSE)
        previous = dict(last_attempt_at="2026-10-01T11:00:00Z", next_attempt_at="2026-10-01T11:40:00Z")
        self.assertEqual(rate_limit_pause(previous, 0, at), RATE_LIMIT_MAX_PAUSE)
        # Through apply_result: a limit records the pause; a success clears it; an ordinary
        # error clears it too, since the provider answered.
        provider = dict(id="p", name="P", enabled=True, status="ok", message=None, fetched_at="2026-10-01T11:00:00Z",
                        last_attempt_at="2026-10-01T11:00:00Z", primary_metric_id="m", secondary_metric_ids=[],
                        metrics=[dict(id="m", label="M", kind="quota", scope="individual", unit="%", used=1, limit=100,
                                      remaining=99, resets_at=None, resets_at_estimated=False, period=None, note=None)],
                        usage_url=None, setup=None)
        snapshot = dict(schema_version=2, generated_at="2026-10-01T11:00:00Z", providers=[provider])
        apply_result(snapshot, "p", dict(status="error", message="Rate limited.", last_attempt_at="2026-10-01T12:00:00Z", retry_after=0))
        self.assertEqual(provider["next_attempt_at"], "2026-10-01T12:05:00Z")
        self.assertEqual(provider["metrics"][0]["used"], 1)  # retained
        self.assertTrue(is_pausing(provider, at))
        self.assertFalse(is_pausing(provider, datetime(2026, 10, 1, 12, 6, tzinfo=timezone.utc)))
        apply_result(snapshot, "p", dict(status="error", message="Rate limited.", last_attempt_at="2026-10-01T12:05:00Z", retry_after=0))
        self.assertEqual(provider["next_attempt_at"], "2026-10-01T12:15:00Z")
        apply_result(snapshot, "p", dict(status="error", message="Unreachable.", last_attempt_at="2026-10-01T12:16:00Z"))
        self.assertIsNone(provider["next_attempt_at"])
        apply_result(snapshot, "p", dict(status="error", message="Rate limited.", last_attempt_at="2026-10-01T12:17:00Z", retry_after=0))
        self.assertEqual(provider["next_attempt_at"], "2026-10-01T12:22:00Z")
        from bars_collector.model import validate_snapshot
        validate_snapshot(snapshot)
        # A success with a pause still recorded is a contradiction the validator refuses.
        provider["status"] = "ok"
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot)
        provider["status"] = "error"
        apply_result(snapshot, "p", dict(status="ok", message=None, fetched_at="2026-10-01T12:30:00Z",
                                         last_attempt_at="2026-10-01T12:30:00Z", primary_metric_id="m",
                                         secondary_metric_ids=[], metrics=provider["metrics"]))
        self.assertIsNone(provider["next_attempt_at"])

    def test_backoff_waits_double_with_jitter_and_stop_before_the_deadline(self):
        from bars_collector.runner import retry_delays
        clock = [100.0]
        waits = []
        with patch("bars_collector.runner.time.sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)):
            for wait in retry_delays(100.0 + 20, jitter=lambda: 0.0, clock=lambda: clock[0]):
                waits.append(wait)
        # 1 + 2 + 4 + 8 = 15 fits in 20 seconds; the next wait of 16 would not.
        self.assertEqual(waits, [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(clock[0], 115.0)
        clock[0] = 0.0
        with patch("bars_collector.runner.time.sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)):
            jittered = list(retry_delays(60.0, jitter=lambda: 1.0, clock=lambda: clock[0]))
        self.assertEqual(jittered, [1.5, 3.0, 6.0, 12.0, 24.0])
        self.assertLessEqual(clock[0], 60)
        self.assertEqual(list(retry_delays(0.5, jitter=lambda: 0.0, clock=lambda: 0.0)), [])

    def test_workers_sanitize_unexpected_errors(self):
        values = dict(collect_results(["claude", "devin"], timeout=5,
                                      collectors={"claude": sensitive_failure, "devin": login_failure}))
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
            with patch("bars_plugin.access.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(CollectionError) as raised:
                    access.http_json("https://vendor.example/usage", {})
                self.assertEqual(raised.exception.status, expected)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("sensitive", str(raised.exception))

    def test_redirects_never_forward_credentials(self):
        self.assertIsNone(access.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

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
