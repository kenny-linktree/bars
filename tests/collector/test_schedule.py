"""The private collection schedule: intervals, due times, recovery and its state file."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bars_collector import schedule
from bars_collector.model import MAX_SNAPSHOT_BYTES, empty_provider, encoded_size, validate_snapshot
from bars_collector.plugins import Plugin
from bars_collector.schedule import (BASELINE_INTERVAL, DUE_TOLERANCE, MAX_INTERVAL, RECOVERY_SECONDS,
                                     RECOVERY_SUCCESSES, Schedule, faster, slower)
from bars_plugin.values import MAX_RETRY_AFTER
from plugin_fixtures import ROOT

T0 = 1_800_000_000  # 2027-01-15T08:00:00Z


def refresh_module():
    spec = importlib.util.spec_from_file_location("bars_refresh_for_schedule", ROOT / "integration/refresh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyTests(unittest.TestCase):
    def test_every_provider_starts_at_the_one_minute_baseline(self):
        plan = Schedule()
        self.assertTrue(plan.is_due("alpha", T0))
        self.assertEqual(plan.interval("alpha"), BASELINE_INTERVAL)
        plan.reserve("alpha", T0)
        self.assertEqual(plan.record("alpha", "ok", T0, scheduled=True), None)
        self.assertEqual(plan.due("alpha"), T0 + 60)
        # The runner fires each minute after a jitter of up to 5 seconds; a run that starts a
        # little earlier in the next minute is still due, a run in the same minute is not.
        self.assertTrue(plan.is_due("alpha", T0 + 55))
        self.assertFalse(plan.is_due("alpha", T0 + 30))
        self.assertFalse(plan.is_due("alpha", T0 + 60 - DUE_TOLERANCE - 1))

    def test_rate_limits_double_one_providers_interval_up_to_an_hour(self):
        plan = Schedule(draw=lambda: 0.0)
        plan.record("beta", "ok", T0, scheduled=True)
        intervals, changes = [], []
        for step in range(8):
            changes.append(plan.record("alpha", "rate_limited", T0 + step, scheduled=True, attempted=T0 + step))
            intervals.append(plan.interval("alpha"))
        self.assertEqual(intervals, [120, 240, 480, 960, 1920, 3600, 3600, 3600])
        self.assertEqual(changes, ["slower"] * 6 + [None, None])
        # Another provider is unaffected.
        self.assertEqual(plan.interval("beta"), BASELINE_INTERVAL)
        self.assertEqual(plan.due("beta"), T0 + 60)
        self.assertEqual([slower(i) for i in (60, 1920, 3600)], [120, 3600, 3600])

    def test_a_rate_limit_waits_the_longest_of_pause_interval_and_hint_plus_positive_jitter(self):
        plan = Schedule(draw=lambda: 0.0)
        # The snapshot's first pause (5 minutes) is longer than the doubled interval.
        plan.record("alpha", "rate_limited", T0, scheduled=True, attempted=T0 + 3, pause=300)
        self.assertEqual(plan.due("alpha"), T0 + 3 + 300)
        # The doubled interval wins over a shorter pause.
        plan = Schedule({"alpha": dict(interval=960, started=T0, due=T0, successes=0, since=T0)}, draw=lambda: 0.0)
        plan.record("alpha", "rate_limited", T0, scheduled=True, attempted=T0, pause=300)
        self.assertEqual(plan.due("alpha"), T0 + 1920)
        # Jitter only lengthens, by at most a tenth of the wait and at most five minutes.
        plan = Schedule(draw=lambda: 0.999999)
        plan.record("alpha", "rate_limited", T0, scheduled=True, attempted=T0, pause=300)
        self.assertEqual(plan.due("alpha"), T0 + 300 + 29)
        plan = Schedule(draw=lambda: 0.999999)
        plan.record("alpha", "rate_limited", T0, scheduled=True, attempted=T0, pause=7200)
        self.assertEqual(plan.due("alpha"), T0 + 7200 + 299)
        # A provider's day-long figure is kept, never pushed past the 24-hour bound.
        plan = Schedule(draw=lambda: 0.999999)
        plan.record("alpha", "rate_limited", T0, scheduled=True, attempted=T0, pause=MAX_RETRY_AFTER)
        self.assertEqual(plan.due("alpha"), T0 + MAX_RETRY_AFTER)
        self.assertFalse(plan.is_due("alpha", T0 + MAX_RETRY_AFTER - DUE_TOLERANCE - 1))
        self.assertTrue(plan.is_due("alpha", T0 + MAX_RETRY_AFTER))

    def test_other_failures_neither_slow_nor_speed_a_provider(self):
        plan = Schedule({"alpha": dict(interval=240, started=T0, due=T0, successes=5, since=T0)})
        for index, failure in enumerate(("error", "timeout", "invalid", "login_required", "setup")):
            self.assertIsNone(plan.record("alpha", failure, T0 + RECOVERY_SECONDS + index, scheduled=True))
        entry = plan.entries["alpha"]
        self.assertEqual((entry["interval"], entry["successes"], entry["since"]), (240, 5, T0))
        self.assertEqual(entry["due"], T0 + RECOVERY_SECONDS + 4 + 240)

    def test_sustained_scheduled_successes_shorten_the_interval_cautiously(self):
        plan = Schedule({"alpha": dict(interval=240, started=T0, due=T0, successes=0, since=T0)})
        # Twelve successes within an hour are not enough: the span must reach six hours.
        for step in range(RECOVERY_SUCCESSES):
            self.assertIsNone(plan.record("alpha", "ok", T0 + 240 * step, scheduled=True))
        self.assertEqual(plan.interval("alpha"), 240)
        # Explicit successes never count toward recovery.
        for step in range(30):
            plan.record("alpha", "ok", T0 + RECOVERY_SECONDS + step, scheduled=False)
        self.assertEqual(plan.interval("alpha"), 240)
        self.assertEqual(plan.entries["alpha"]["successes"], RECOVERY_SUCCESSES)
        # The next scheduled success after six hours shortens it by one step and restarts the window.
        self.assertEqual(plan.record("alpha", "ok", T0 + RECOVERY_SECONDS, scheduled=True), "faster")
        self.assertEqual(plan.interval("alpha"), 180)
        self.assertEqual(plan.due("alpha"), T0 + RECOVERY_SECONDS + 180)
        self.assertEqual((plan.entries["alpha"]["successes"], plan.entries["alpha"]["since"]), (0, T0 + RECOVERY_SECONDS))
        # A rate limit restarts the window and doubles again.
        plan.record("alpha", "rate_limited", T0 + RECOVERY_SECONDS + 60, scheduled=True, attempted=T0 + RECOVERY_SECONDS + 60)
        self.assertEqual(plan.interval("alpha"), 360)
        self.assertEqual(plan.entries["alpha"]["successes"], 0)

    def test_recovery_window_starts_with_the_first_scheduled_success(self):
        for failure in (None, "login_required"):
            with self.subTest(failure=failure):
                plan = Schedule(draw=lambda: 0.0)
                plan.record("alpha", "rate_limited", T0, scheduled=True, pause=300)
                first_success = T0 + RECOVERY_SECONDS
                if failure:
                    plan.record("alpha", failure, first_success - 120, scheduled=True)
                for step in range(12):
                    plan.record("alpha", "ok", first_success + step * 120, scheduled=True)
                self.assertEqual(plan.interval("alpha"), 120)
                self.assertIsNone(plan.record("alpha", "ok", first_success + RECOVERY_SECONDS - 1,
                                              scheduled=True))
                self.assertEqual(plan.record("alpha", "ok", first_success + RECOVERY_SECONDS,
                                             scheduled=True), "faster")
                self.assertEqual(plan.interval("alpha"), 60)

    def test_recovery_steps_are_whole_minutes_and_stop_at_the_baseline(self):
        self.assertEqual([faster(i) for i in (3600, 3240, 2940, 900, 780, 720, 120, 60)],
                         [3240, 2940, 2640, 780, 720, 660, 60, 60])
        interval, steps = MAX_INTERVAL, 0
        while interval > BASELINE_INTERVAL:
            interval, steps = faster(interval), steps + 1
            self.assertEqual(interval % 60, 0)
            self.assertGreaterEqual(interval, BASELINE_INTERVAL)
        self.assertLess(steps, 40)
        # A whole recovery from the cap, through scheduled successes alone.
        plan = Schedule({"alpha": dict(interval=MAX_INTERVAL, started=T0, due=T0, successes=0, since=T0)})
        at = T0
        while plan.interval("alpha") > BASELINE_INTERVAL:
            at += plan.interval("alpha")
            plan.record("alpha", "ok", at, scheduled=True)
            self.assertLess(at - T0, 60 * 86400)
        self.assertEqual(plan.interval("alpha"), BASELINE_INTERVAL)

    def test_stored_times_the_clock_cannot_have_produced_never_strand_a_provider(self):
        entry = dict(interval=960, started=T0, due=T0 + 960, successes=3, since=T0)
        plan = Schedule({"alpha": dict(entry)})
        self.assertFalse(plan.is_due("alpha", T0 + 100))
        # The clock was set back an hour: the provider is due, and keeps its learned interval.
        self.assertTrue(plan.is_due("alpha", T0 - 3600))
        plan.record("alpha", "ok", T0 - 3600, scheduled=True)
        self.assertEqual(plan.interval("alpha"), 960)
        self.assertEqual(plan.due("alpha"), T0 - 3600 + 960)
        # The recovery window restarted instead of waiting for the clock to catch up.
        self.assertEqual((plan.entries["alpha"]["since"], plan.entries["alpha"]["successes"]), (T0 - 3600, 1))
        # A due time further ahead than any wait this module records counts as due.
        far = Schedule({"alpha": dict(entry, due=T0 + MAX_RETRY_AFTER + 3600)})
        self.assertTrue(far.is_due("alpha", T0 + 100))
        held = Schedule({"alpha": dict(entry, due=T0 + MAX_RETRY_AFTER)})
        self.assertFalse(held.is_due("alpha", T0 + 100))

    def test_diagnostics_use_only_the_grammar_the_refresh_runner_logs(self):
        pattern = refresh_module()._SCHEDULE_ENTRY
        lines = [schedule.attempt_line("alpha", "ok", 60, T0 + 60),
                 schedule.attempt_line("a_1", "rate_limited", 3600, T0 + MAX_RETRY_AFTER, 86400, "slower"),
                 schedule.attempt_line("alpha", "ok", 180, T0, None, "faster"),
                 schedule.attempt_line("alpha", "timeout", 60, T0),
                 schedule.skip_line("alpha", "not_due", 120, T0),
                 schedule.skip_line("alpha", "already_attempted", 60, None),
                 schedule.skip_line("alpha", "rate_limited", 60, T0),
                 schedule.state_line("not_saved")]
        lines += [schedule.state_line(problem) for problem in schedule.PROBLEMS]
        for line in lines:
            self.assertRegex(line, pattern)
            self.assertIsNotNone(pattern.fullmatch(line), line)
        self.assertEqual(lines[0], "schedule: alpha attempted outcome=ok interval=60s next=2027-01-15T08:01:00Z")


class StateFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = schedule.schedule_path(Path(directory.name) / "snapshot.json")

    def test_state_persists_across_processes(self):
        plan = Schedule(draw=lambda: 0.0)
        plan.record("alpha", "rate_limited", T0, scheduled=True, attempted=T0, pause=300)
        plan.record("beta", "ok", T0, scheduled=True)
        self.assertTrue(plan.save(self.path))
        self.assertEqual(self.path.name, "snapshot.json.schedule")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored, {"version": 1, "providers": {
            "alpha": {"interval": 120, "started": T0, "due": T0 + 300, "successes": 0, "since": T0,
                      "pause_until": T0 + 300},
            "beta": {"interval": 60, "started": T0, "due": T0 + 60, "successes": 1, "since": T0}}})
        # Another interpreter reads the same schedule.
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "collector"))
        output = subprocess.run([sys.executable, "-c", "import sys; from bars_collector import schedule; "
                                 "plan, problem = schedule.load(sys.argv[1]); "
                                 "print(plan.interval('alpha'), plan.due('alpha'), plan.is_due('alpha', %d), problem)" % (T0 + 120),
                                 str(self.path)], env=environment, capture_output=True, text=True, timeout=20, check=True)
        self.assertEqual(output.stdout.split(), ["120", str(T0 + 300), "False", "None"])
        self.assertEqual(list(self.path.parent.glob(".snapshot.json.schedule.*")), [])

    def test_large_supported_provider_set_keeps_learned_intervals(self):
        ids = ["p%03d" % index for index in range(700)]
        snapshot = dict(schema_version=2, generated_at=schedule.instant(T0),
                        providers=[empty_provider(Plugin(pid, pid, "/synthetic/plugins")) for pid in ids])
        validate_snapshot(snapshot)
        self.assertLess(encoded_size(snapshot), MAX_SNAPSHOT_BYTES)
        plan = Schedule({pid: dict(interval=3600, started=T0, due=T0 + 3600,
                                   pause_until=T0 + 3600, successes=0, since=T0) for pid in ids})
        self.assertTrue(plan.save(self.path))
        loaded, problem = schedule.load(self.path)
        self.assertIsNone(problem)
        self.assertEqual(loaded.entries, plan.entries)
        self.assertFalse(loaded.is_due(ids[0], T0 + 60))

    def test_an_oversized_save_preserves_the_previous_readable_state(self):
        plan = Schedule()
        plan.record("alpha", "ok", T0, scheduled=True)
        self.assertTrue(plan.save(self.path))
        before = self.path.read_bytes()
        plan.record("beta", "ok", T0, scheduled=True)
        with patch.object(schedule, "MAX_STATE_BYTES", len(before)):
            self.assertFalse(plan.save(self.path))
        self.assertEqual(self.path.read_bytes(), before)
        loaded, problem = schedule.load(self.path)
        self.assertIsNone(problem)
        self.assertEqual(set(loaded.entries), {"alpha"})

    def test_missing_and_damaged_state_fall_back_to_the_baseline(self):
        plan, problem = schedule.load(self.path)
        self.assertEqual((plan.entries, problem), ({}, None))
        good = {"interval": 120, "started": T0, "due": T0 + 120, "successes": 0, "since": T0}
        cases = {
            b"{": "invalid",
            b"[]": "unsupported",
            json.dumps({"version": 2, "providers": {}}).encode(): "unsupported",
            json.dumps({"version": 1, "providers": []}).encode(): "invalid",
            b" " * (schedule.MAX_STATE_BYTES + 1): "unreadable",
        }
        for content, expected in cases.items():
            with self.subTest(content=content[:30]):
                self.path.write_bytes(content)
                plan, problem = schedule.load(self.path)
                self.assertEqual((plan.entries, problem), ({}, expected))
        # Invalid entries are dropped one by one; the valid ones stay.
        bad_entries = {"b1": dict(good, interval=True), "b2": dict(good, interval=90), "b3": dict(good, interval=7200),
                       "b4": dict(good, extra=1), "b5": dict(good, due="soon"), "b6": dict(good, successes=-1),
                       "B7": dict(good), "b8": dict(good, started=10 ** 13),
                       "b9": dict(good, pause_until=True), "b10": dict(good, pause_until=T0 - 1),
                       "b11": dict(good, pause_until=T0 + 121)}
        self.path.write_text(json.dumps({"version": 1, "providers": dict(bad_entries, alpha=good)}))
        plan, problem = schedule.load(self.path)
        self.assertEqual((list(plan.entries), problem), (["alpha"], "invalid"))
        # Neither a link nor anything but a regular file is read.
        self.path.unlink()
        target = self.path.parent / "target"
        target.write_text(json.dumps({"version": 1, "providers": {"alpha": good}}))
        self.path.symlink_to(target)
        self.assertEqual(schedule.load(self.path)[1], "unreadable")
        Schedule().save(self.path)
        self.assertFalse(self.path.is_symlink())
        self.assertIn("alpha", target.read_text())
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(schedule.load(self.path)[1], "unreadable")

    def test_the_state_file_holds_only_ids_times_and_counters(self):
        plan = Schedule()
        plan.record("alpha", "ok", T0, scheduled=True)
        plan.save(self.path)
        stored = json.loads(self.path.read_text())
        self.assertEqual(set(stored), {"version", "providers"})
        for entry in stored["providers"].values():
            self.assertEqual(set(entry), schedule.FIELDS)
            self.assertTrue(all(isinstance(value, int) for value in entry.values()))

    def test_forgetting_removed_providers(self):
        plan = Schedule()
        for pid in ("alpha", "beta"):
            plan.record(pid, "ok", T0, scheduled=True)
        self.assertTrue(plan.prune({"alpha"}))
        self.assertFalse(plan.prune({"alpha"}))
        self.assertEqual(list(plan.entries), ["alpha"])


if __name__ == "__main__":
    unittest.main()
