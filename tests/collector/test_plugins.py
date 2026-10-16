"""Provider plugins: discovery, the manifest contract, and containment of faulty plugins."""
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

from bars_collector import __main__ as cli, plugins
from bars_collector.model import validate_snapshot
from bars_collector.runner import FAILURE_MESSAGE, INVALID_MESSAGE, OVERSIZE_MESSAGE, TIMEOUT_MESSAGE, run
from bars_collector.storage import load_snapshot
from plugin_fixtures import ROOT, STANDARD, STANDARD_DIR, STANDARD_IDS, write_plugin

COLLECTOR = ROOT / "collector"
OK_PLUGIN = """
from bars_plugin import metric, result
def collect():
    return result("main", [metric("main", "Main", "quota", "individual", "%", 10, 100, period="day")])
def detect():
    return True
"""


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_standard_plugins_are_discovered_with_their_manifests(self):
        self.assertEqual(STANDARD_IDS, ["claude", "codex", "cursor", "devin"])
        for plugin in STANDARD:
            self.assertTrue(plugin.loadable, plugin.error)
            self.assertTrue(plugin.usage_url.startswith("https://"))
            self.assertEqual(set(plugin.setup), set(plugins.SETUP_FIELDS))
            self.assertTrue(plugin.setup["install_url"].startswith("https://"))

    def test_discovery_sorts_by_id_and_ignores_invalid_names_and_files(self):
        for name in ("zeta", "alpha", "Bad-Name", "9lives", "a" * 33):
            write_plugin(self.root, name, module=OK_PLUGIN)
        (self.root / "stray.py").write_text("")
        (self.root / "file_not_dir").write_text("")
        self.assertEqual([p.id for p in plugins.discover(self.root)], ["alpha", "zeta"])
        self.assertEqual(plugins.discover(self.root / "missing"), [])

    def test_manifest_problems_become_visible_errors_not_crashes(self):
        write_plugin(self.root, "nomanifest", manifest=False, module=OK_PLUGIN)
        write_plugin(self.root, "badjson", manifest="{not json", module=OK_PLUGIN)
        write_plugin(self.root, "wrongid", manifest={"manifest_version": 1, "id": "other", "name": "X"}, module=OK_PLUGIN)
        write_plugin(self.root, "newversion", manifest={"manifest_version": 2, "id": "newversion", "name": "X"}, module=OK_PLUGIN)
        write_plugin(self.root, "nomodule")
        write_plugin(self.root, "badurl", manifest={"manifest_version": 1, "id": "badurl", "name": "X",
                                                    "usage_url": "http://example.com"}, module=OK_PLUGIN)
        write_plugin(self.root, "partsetup", manifest={"manifest_version": 1, "id": "partsetup", "name": "X",
                                                       "setup": {"login_command": "x"}}, module=OK_PLUGIN)
        write_plugin(self.root, "longname", manifest={"manifest_version": 1, "id": "longname", "name": "n" * 33},
                     module=OK_PLUGIN)
        write_plugin(self.root, "extra_keys", manifest={"manifest_version": 1, "id": "extra_keys", "name": "Extra",
                                                        "future_field": {"anything": 1}}, module=OK_PLUGIN)
        found = {p.id: p for p in plugins.discover(self.root)}
        self.assertEqual(found["nomanifest"].error, "Plugin is missing its plugin.json.")
        self.assertEqual(found["nomanifest"].name, "Nomanifest")
        for pid in ("badjson", "wrongid", "newversion", "badurl", "partsetup", "longname"):
            self.assertEqual(found[pid].error, "Plugin has an invalid plugin.json.", pid)
        self.assertEqual(found["nomodule"].error, "Plugin is missing its plugin.py.")
        self.assertTrue(found["extra_keys"].loadable)
        self.assertEqual(found["extra_keys"].name, "Extra")

    def test_oversized_manifest_is_invalid(self):
        write_plugin(self.root, "big", manifest='{"manifest_version": 1, "id": "big", "name": "Big", "pad": "%s"}'
                     % ("x" * plugins.MAX_MANIFEST_BYTES), module=OK_PLUGIN)
        self.assertEqual(plugins.discover(self.root)[0].error, "Plugin has an invalid plugin.json.")


class RunTests(unittest.TestCase):
    """The runner against synthetic plugin directories, through real worker processes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plugins_dir = self.root / "plugins"
        self.output = self.root / "snapshot.json"

    def run_all(self, timeout=20, **kwargs):
        found = plugins.discover(self.plugins_dir)
        return run(self.output, [p.id for p in found], timeout, plugins=found, **kwargs)

    def provider(self, snapshot, pid):
        return next(p for p in snapshot["providers"] if p["id"] == pid)

    def test_provider_list_follows_the_plugins_present(self):
        write_plugin(self.plugins_dir, "beta", module=OK_PLUGIN, name="Beta")
        write_plugin(self.plugins_dir, "alpha", module=OK_PLUGIN, name="Alpha")
        snapshot = self.run_all()
        self.assertEqual([(p["id"], p["name"], p["status"]) for p in snapshot["providers"]],
                         [("alpha", "Alpha", "ok"), ("beta", "Beta", "ok")])
        self.assertEqual(self.provider(snapshot, "alpha")["secondary_metric_ids"], [])
        validate_snapshot(load_snapshot(self.output))
        # Removing a plugin drops its entry; adding one starts with the placeholder; renaming
        # in the manifest shows at once while values are retained.
        import shutil
        shutil.rmtree(self.plugins_dir / "beta")
        write_plugin(self.plugins_dir, "gamma", module=OK_PLUGIN, name="Gamma")
        write_plugin(self.plugins_dir, "alpha", module=OK_PLUGIN, name="Alpha Renamed")
        found = plugins.discover(self.plugins_dir)
        snapshot = run(self.output, ["gamma"], plugins=found)  # Only gamma collects.
        self.assertEqual([p["id"] for p in snapshot["providers"]], ["alpha", "gamma"])
        alpha = self.provider(snapshot, "alpha")
        self.assertEqual((alpha["name"], alpha["status"], len(alpha["metrics"])), ("Alpha Renamed", "ok", 1))
        self.assertEqual(self.provider(snapshot, "gamma")["status"], "ok")

    def test_unloadable_plugin_reports_its_error_and_keeps_retained_values(self):
        write_plugin(self.plugins_dir, "alpha", module=OK_PLUGIN)
        self.run_all()
        (self.plugins_dir / "alpha" / plugins.MANIFEST_NAME).write_text("{broken")
        snapshot = self.run_all()
        alpha = self.provider(snapshot, "alpha")
        self.assertEqual((alpha["status"], alpha["message"]), ("error", "Plugin has an invalid plugin.json."))
        self.assertEqual(len(alpha["metrics"]), 1, "retained values survive a broken manifest")
        self.assertIsNotNone(alpha["fetched_at"])
        self.assertIsNotNone(alpha["last_attempt_at"])

    def test_faulty_plugins_fail_alone_with_fixed_messages(self):
        faults = {
            "syntax": "def collect(:\n",
            "importerror": "import no_such_module_anywhere\n",
            "raises": "def collect():\n    raise RuntimeError('secret-token-in-exception')\n",
            "sysexit": "import sys\ndef collect():\n    sys.exit(0)\n",
            "nocollect": "def detect():\n    return True\n",
            "notdict": "def collect():\n    return ['nope']\n",
            "badshape": "def collect():\n    return {'metrics': 'nope', 'primary_metric_id': None, 'secondary_metric_ids': []}\n",
            "extrakeys": "from bars_plugin import metric, result\ndef collect():\n"
                         "    v = result('m', [metric('m', 'M', 'quota', 'individual', '%', 1, 100)])\n"
                         "    v['name'] = 'Hijacked'; v['enabled'] = False; return v\n",
            "secondary_primary": "def collect():\n    return {'metrics': [{'id': 'm', 'label': 'M', 'kind': 'quota', 'scope': 'individual',"
                                 " 'unit': '%', 'used': 1, 'limit': 100, 'remaining': 99, 'resets_at': None, 'period': None}],"
                                 " 'primary_metric_id': 'm', 'secondary_metric_ids': ['m']}\n",
            "toomany": "from bars_plugin import metric, result\ndef collect():\n"
                       "    return result('m0', [metric('m%d' % i, 'M', 'quota', 'individual', '%', 1, 100) for i in range(40)])\n",
            "unserializable": "def collect():\n    return {'metrics': [], 'primary_metric_id': object(), 'secondary_metric_ids': []}\n",
            "extrametrickey": "from bars_plugin import metric, result\ndef collect():\n"
                              "    v = result('m', [metric('m', 'M', 'quota', 'individual', '%', 1, 100)])\n"
                              "    v['metrics'][0]['raw_response'] = {'access_token': 'secret-leak'}; return v\n",
            "epochreset": "def collect():\n    return {'metrics': [{'id': 'm', 'label': 'M', 'kind': 'quota', 'scope': 'individual',"
                          " 'unit': '%', 'used': 1, 'limit': 100, 'remaining': 99, 'resets_at': 1790899200, 'period': None}],"
                          " 'primary_metric_id': 'm', 'secondary_metric_ids': []}\n",
            "offsetreset": "def collect():\n    return {'metrics': [{'id': 'm', 'label': 'M', 'kind': 'quota', 'scope': 'individual',"
                           " 'unit': '%', 'used': 1, 'limit': 100, 'remaining': 99, 'resets_at': '2026-11-01T08:00:00+08:00', 'period': None}],"
                           " 'primary_metric_id': 'm', 'secondary_metric_ids': []}\n",
            "noquantities": "def collect():\n    return {'metrics': [{'id': 'm', 'label': 'M', 'kind': 'quota', 'scope': 'individual',"
                            " 'unit': '%', 'used': None, 'limit': None, 'remaining': None, 'resets_at': None, 'period': None}],"
                            " 'primary_metric_id': 'm', 'secondary_metric_ids': []}\n",
            "longmessage": "from bars_plugin import CollectionError\ndef collect():\n"
                           "    raise CollectionError('x' * 500)\n",
            "controlchars": "from bars_plugin import CollectionError\ndef collect():\n"
                            "    raise CollectionError('line one\\nsecret-on-line-two', 'login_required')\n",
            "ownmessage": "from bars_plugin import CollectionError\ndef collect():\n"
                          "    raise CollectionError('Sign in to Example first.', 'setup')\n",
            "huge": "def collect():\n    return {'metrics': [], 'primary_metric_id': None, 'secondary_metric_ids': [], 'pad': 'x' * (300 * 1024)}\n",
            "printer": "import sys\nfrom bars_plugin import metric, result\ndef collect():\n"
                       "    print('stdout noise secret-token'); print('stderr noise', file=sys.stderr)\n"
                       "    return result('m', [metric('m', 'M', 'quota', 'individual', '%', 1, 100)])\n",
            "good": OK_PLUGIN,
        }
        for pid, source in faults.items():
            write_plugin(self.plugins_dir, pid, module=source)
        captured = io.StringIO()
        # Transient errors retry with backoff until the deadline, so keep it short here.
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            snapshot = self.run_all(timeout=5)
        self.assertEqual(captured.getvalue(), "", "plugin output never reaches the coordinator's streams")
        statuses = {p["id"]: (p["status"], p["message"]) for p in snapshot["providers"]}
        self.assertEqual(statuses["good"], ("ok", None))
        self.assertEqual(statuses["printer"], ("ok", None))
        for pid in ("syntax", "importerror", "raises", "sysexit"):
            self.assertEqual(statuses[pid], ("error", FAILURE_MESSAGE), pid)
        self.assertEqual(statuses["nocollect"], ("error", "Plugin has no collect function."))
        self.assertEqual(statuses["notdict"], ("error", "Plugin returned an invalid result."))
        for pid in ("badshape", "extrakeys", "secondary_primary", "toomany", "unserializable",
                    "extrametrickey", "epochreset", "offsetreset", "noquantities"):
            self.assertEqual(statuses[pid], ("error", INVALID_MESSAGE), pid)
        self.assertEqual(statuses["huge"], ("error", INVALID_MESSAGE))
        self.assertEqual(statuses["longmessage"], ("error", FAILURE_MESSAGE))
        self.assertEqual(statuses["controlchars"], ("login_required", FAILURE_MESSAGE))
        self.assertEqual(statuses["ownmessage"], ("setup", "Sign in to Example first."))
        text = json.dumps(snapshot)
        self.assertNotIn("secret", text)
        self.assertNotIn("Hijacked", text)
        self.assertNotIn("secret-leak", text)
        self.assertTrue(all(p["enabled"] for p in snapshot["providers"]))
        validate_snapshot(load_snapshot(self.output))

    def test_hanging_plugin_only_costs_its_own_deadline(self):
        write_plugin(self.plugins_dir, "hang", module="import time\ndef collect():\n    time.sleep(30)\n")
        write_plugin(self.plugins_dir, "quick", module=OK_PLUGIN)
        import time
        started = time.monotonic()
        snapshot = self.run_all(timeout=2)
        self.assertLess(time.monotonic() - started, 10)
        statuses = {p["id"]: p["status"] for p in snapshot["providers"]}
        self.assertEqual(statuses, {"hang": "error", "quick": "ok"})

    def test_version_one_cache_is_upgraded_and_reconciled(self):
        write_plugin(self.plugins_dir, "claude", module=OK_PLUGIN, name="Claude")
        write_plugin(self.plugins_dir, "newcomer", module=OK_PLUGIN, name="Newcomer")
        old = {"schema_version": 1, "generated_at": "2026-10-01T00:00:00Z", "providers": [
            {"id": pid, "name": pid.capitalize(), "status": "ok", "message": None, "enabled": pid != "cursor",
             "fetched_at": "2026-10-01T00:00:00Z", "last_attempt_at": "2026-10-01T00:00:00Z",
             "primary_metric_id": "main", "metrics": [
                 {"id": "main", "label": "Main", "kind": "budget", "scope": "unknown", "unit": "USD",
                  "used": 1, "limit": 2, "remaining": 1, "resets_at": None}]}
            for pid in ("claude", "codex", "cursor", "devin")]}
        self.output.write_text(json.dumps(old))
        found = plugins.discover(self.plugins_dir)
        snapshot = run(self.output, [], plugins=found)
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual([p["id"] for p in snapshot["providers"]], ["claude", "newcomer"])
        claude = self.provider(snapshot, "claude")
        self.assertEqual(claude["fetched_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(claude["metrics"][0]["used"], 1)
        self.assertEqual(claude["secondary_metric_ids"], [])
        self.assertIsNone(claude["usage_url"])
        self.assertEqual(self.provider(snapshot, "newcomer")["status"], "setup")
        validate_snapshot(json.loads(self.output.read_text()))

    def test_plugins_see_only_the_allowlisted_environment(self):
        write_plugin(self.plugins_dir, "env", module=(
            "import json, os, pathlib\nfrom bars_plugin import metric, result\ndef collect():\n"
            "    pathlib.Path(os.environ['HOME'], 'seen.json').write_text(json.dumps(sorted(os.environ)))\n"
            "    return result('m', [metric('m', 'M', 'quota', 'individual', '%', 1, 100)])\n"))
        home = self.root / "home"
        home.mkdir()
        with patch.dict(os.environ, {"HOME": str(home), "SYNTHETIC_UNRELATED_SECRET": "x", "PYTHONPATH": os.environ.get("PYTHONPATH", "")}):
            snapshot = self.run_all()
        self.assertEqual(self.provider(snapshot, "env")["status"], "ok")
        seen = json.loads((home / "seen.json").read_text())
        self.assertNotIn("SYNTHETIC_UNRELATED_SECRET", seen)
        self.assertNotIn("PYTHONPATH", seen)
        self.assertIn("HOME", seen)
        from bars_plugin import CHILD_ENVIRONMENT
        self.assertTrue(set(seen) <= set(CHILD_ENVIRONMENT))

    def test_a_result_that_would_overflow_the_snapshot_is_refused_and_values_kept(self):
        # Thirty-two metrics with 200-character labels, units and notes: valid alone (about
        # 21 KiB), but sixty such providers exceed the 1 MiB file bound the readers enforce.
        big = ("from bars_plugin import metric, result\ndef collect():\n"
               "    return result('m0', [metric('m%d' % i, 'L' * 200, 'quota', 'individual', 'U' * 200, 1, 100, note='N' * 200)"
               " for i in range(32)])\n")
        for index in range(60):
            write_plugin(self.plugins_dir, "p%02d" % index, module=big)
        write_plugin(self.plugins_dir, "small", module=OK_PLUGIN)
        snapshot = self.run_all()
        from bars_collector.model import MAX_SNAPSHOT_BYTES, encoded_size
        self.assertLessEqual(encoded_size(snapshot), MAX_SNAPSHOT_BYTES)
        statuses = [p["status"] for p in snapshot["providers"]]
        self.assertIn("ok", statuses)
        refused = [p for p in snapshot["providers"] if p["status"] == "error"]
        self.assertTrue(refused)
        self.assertTrue(all(p["message"] == OVERSIZE_MESSAGE and p["metrics"] == [] and p["fetched_at"] is None
                            for p in refused))
        load_snapshot(self.output)  # Still readable.

    def test_too_many_plugins_keep_the_previous_readable_snapshot(self):
        # Manifests alone can exceed the bound: a plain reconciliation must then fail loudly
        # and leave the last readable file in place rather than publish an unreadable one.
        write_plugin(self.plugins_dir, "small", module=OK_PLUGIN)
        before = self.run_all()
        from bars_collector.plugins import MAX_NAME, MAX_TEXT
        setup = {"install_title": "I" * MAX_TEXT, "install_url": "https://example.com/" + "p" * (MAX_TEXT - 20),
                 "login_command": "c" * MAX_TEXT, "login_step": "s" * MAX_TEXT, "sign_in_again": "a" * MAX_TEXT,
                 "credential_note": "n" * MAX_TEXT}
        for index in range(900):
            write_plugin(self.plugins_dir, "p%03d" % index, module=OK_PLUGIN,
                         manifest={"manifest_version": 1, "id": "p%03d" % index, "name": "N" * MAX_NAME,
                                   "usage_url": "https://example.com/" + "u" * (MAX_TEXT - 20), "setup": setup})
        self.assertTrue(all(p.loadable for p in plugins.discover(self.plugins_dir)))
        from bars_collector.storage import SnapshotTooLarge
        with self.assertRaises(SnapshotTooLarge):
            self.run_all()
        after = load_snapshot(self.output)
        self.assertEqual([p["id"] for p in after["providers"]], [p["id"] for p in before["providers"]])

    def test_a_failure_message_cannot_push_the_snapshot_past_the_bound(self):
        from bars_collector.model import MAX_SNAPSHOT_BYTES, encoded_size
        from bars_collector.runner import apply_result
        snapshot = dict(schema_version=2, generated_at="2026-10-01T00:00:00Z", providers=[])
        for index in range(80):
            provider = dict(id="p%03d" % index, name="N" * 200, enabled=True, status="ok", message=None,
                            fetched_at="2026-10-01T00:00:00Z", last_attempt_at="2026-10-01T00:00:00Z",
                            primary_metric_id="m0", secondary_metric_ids=[], usage_url=None, setup=None,
                            metrics=[dict(id="m%d" % i, label="L" * 200, kind="quota", scope="individual", unit="U" * 200,
                                          used=1, limit=100, remaining=99, resets_at=None, resets_at_estimated=False,
                                          period=None, note="N" * 200) for i in range(14)])
            snapshot["providers"].append(provider)
        # Fill the remaining room with small providers until fewer than 300 bytes are left.
        index = 400
        while MAX_SNAPSHOT_BYTES - encoded_size(snapshot) >= 300:
            slack = MAX_SNAPSHOT_BYTES - encoded_size(snapshot)
            snapshot["providers"].append(dict(id="q%03d" % index, name="n" * min(200, max(1, slack - 300)), enabled=True,
                                              status="setup", message=None, fetched_at=None, last_attempt_at=None,
                                              primary_metric_id=None, secondary_metric_ids=[], usage_url=None,
                                              setup=None, metrics=[]))
            index += 1
        slack = MAX_SNAPSHOT_BYTES - encoded_size(snapshot)
        self.assertTrue(0 <= slack < 300, slack)
        target = snapshot["providers"][0]
        retained = dict(target)
        apply_result(snapshot, target["id"], dict(status="error", message="E" * 190, last_attempt_at="2026-10-02T00:00:00Z"))
        self.assertLessEqual(encoded_size(snapshot), MAX_SNAPSHOT_BYTES)
        self.assertEqual(target, retained)

    def test_results_touch_only_their_provider_entry(self):
        write_plugin(self.plugins_dir, "alpha", module=OK_PLUGIN)
        write_plugin(self.plugins_dir, "beta", module=OK_PLUGIN)
        first = copy.deepcopy(self.run_all(disabled={"beta"}))
        snapshot = self.run_all(disabled={"beta"})
        self.assertEqual(self.provider(snapshot, "beta"), self.provider(first, "beta"))
        self.assertIs(self.provider(snapshot, "beta")["enabled"], False)


class DetectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_detect_runs_each_plugin_in_a_worker_and_defaults_to_not_found(self):
        write_plugin(self.root, "found", module=OK_PLUGIN)
        write_plugin(self.root, "nodetect", module="def collect():\n    return {}\n")
        write_plugin(self.root, "falsy", module="def detect():\n    return 0\ndef collect():\n    return {}\n")
        write_plugin(self.root, "truthy_string", module="def detect():\n    return 'yes'\ndef collect():\n    return {}\n")
        write_plugin(self.root, "raises", module="def detect():\n    raise RuntimeError('x')\n")
        write_plugin(self.root, "exits", module="import os\ndef detect():\n    os._exit(0)\n")
        write_plugin(self.root, "broken")
        found = cli.detect(plugins.discover(self.root), timeout=10)
        self.assertEqual(found, {"broken": False, "exits": False, "falsy": False, "found": True,
                                 "nodetect": False, "raises": False, "truthy_string": False})

    def test_cli_detect_output_is_booleans_only_even_with_noisy_plugins(self):
        write_plugin(self.root, "noisy", module="import sys\ndef detect():\n"
                     "    print('secret-noise'); print('more', file=sys.stderr); return True\n")
        env = dict(os.environ, PYTHONPATH=str(COLLECTOR), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-m", "bars_collector", "--detect", "--plugins", str(self.root)],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"noisy": True})
        self.assertNotIn("secret", result.stdout + result.stderr)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_unknown_provider_is_a_usage_error_and_unknown_disable_is_ignored(self):
        write_plugin(self.root / "plugins", "alpha", module=OK_PLUGIN)
        output = self.root / "snapshot.json"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--output", str(output), "--plugins", str(self.root / "plugins"), "--provider", "nobody"])
        for bad in ("Nobody", "no-body", "../x"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["--output", str(output), "--plugins", str(self.root / "plugins"), "--disable", bad])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--output", str(output), "--plugins", str(self.root / "plugins"),
                             "--disable", "gone", "--disable", "alpha"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "alpha: disabled")

    def test_no_plugins_writes_an_empty_provider_list(self):
        output = self.root / "snapshot.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--output", str(output), "--plugins", str(self.root / "none")])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "")
        self.assertEqual(json.loads(output.read_text())["providers"], [])

    def test_standard_plugins_pass_through_the_command_line(self):
        output = self.root / "snapshot.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--output", str(output), "--plugins", str(STANDARD_DIR)] +
                            [arg for pid in STANDARD_IDS for arg in ("--disable", pid)])
        self.assertEqual(code, 0)
        snapshot = json.loads(output.read_text())
        self.assertEqual([p["id"] for p in snapshot["providers"]], STANDARD_IDS)
        self.assertTrue(all(p["setup"] and p["usage_url"] for p in snapshot["providers"]))


if __name__ == "__main__":
    unittest.main()
