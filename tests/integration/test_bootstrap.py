"""Exercise scripts/bootstrap.sh against throwaway repositories and a temporary HOME.

The real build and installer are replaced by stubs inside a temporary Git repository,
so these tests clone, update and pass arguments without touching /Applications,
launchd or the account's real Application Support directory.
"""
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts/bootstrap.sh"

STUB_BUILD = """#!/bin/bash
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$ROOT/.build/native/Release/Bars.app"
printf '%s\\n' "$*" "${BARS_BUILD_PYTHON:-}" > "$HOME/build-record"
"""

STUB_INSTALL = """import json, os, sys
with open(os.path.join(os.environ["HOME"], "install-record.json"), "w") as f:
    json.dump({"argv": sys.argv[1:], "python": sys.executable, "marker": MARKER}, f)
"""


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=Bars Test", "-c", "user.email=test@example.invalid",
                    "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def tools_available():
    try:
        return (subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0
                and subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True).returncode == 0
                and shutil.which("git") is not None)
    except OSError:
        return False


class BootstrapSyntaxTests(unittest.TestCase):
    def test_parses_as_posix_sh(self):
        for shell in ("/bin/sh", "/bin/dash"):
            if Path(shell).exists():
                with self.subTest(shell=shell):
                    subprocess.run([shell, "-n", str(BOOTSTRAP)], check=True)

    def test_default_repository_and_entry_point(self):
        text = BOOTSTRAP.read_text()
        self.assertIn('DEFAULT_REPO_URL="https://github.com/kenny-linktree/bars.git"', text)
        self.assertIn("BARS_REPO_URL overrides it", text)
        self.assertTrue(text.rstrip().endswith('main "$@" </dev/null'))


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bars bootstrap ")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.home.mkdir()
        self.support = self.home / "Library/Application Support/Bars"
        self.source = self.support / "src"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "calls"

    def environment(self, **extra):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"}
        env.update(extra)
        return env

    def run_bootstrap(self, *args, script=None, piped=False, **extra):
        script = script or BOOTSTRAP
        if piped:
            # Equivalent to curl -fsSL URL | sh -s -- ARGS
            return subprocess.run(["/bin/sh", "-s", "--", *args], input=script.read_bytes(),
                                  env=self.environment(**extra), capture_output=True, timeout=120)
        return subprocess.run(["/bin/sh", str(script), *args], env=self.environment(**extra),
                              capture_output=True, timeout=120)

    def make_repository(self):
        work = Path(self.temporary.name) / "work"
        (work / "scripts").mkdir(parents=True)
        build = work / "scripts/build.sh"
        build.write_text(STUB_BUILD)
        build.chmod(0o755)
        (work / "scripts/install.py").write_text("MARKER = 1\n" + STUB_INSTALL)
        git("init", "-q", cwd=work)
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", "stub", cwd=work)
        bare = Path(self.temporary.name) / "bars.git"
        git("clone", "-q", "--bare", str(work), str(bare), cwd=work)
        return work, bare

    def record(self):
        return json.loads((self.home / "install-record.json").read_text())

    @unittest.skipUnless(tools_available(), "Command Line Tools and git are required")
    def test_clones_builds_and_passes_installer_arguments(self):
        _, bare = self.make_repository()
        result = self.run_bootstrap("--disable", "cursor", "--no-start", piped=True,
                                    BARS_REPO_URL=bare.as_uri())
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue((self.source / ".git").is_dir())
        self.assertEqual(stat.S_IMODE(self.support.stat().st_mode), 0o700)
        build_args, build_python = (self.home / "build-record").read_text().splitlines()
        self.assertEqual(build_args, "--toolchain clt")
        record = self.record()
        built = str(self.source / ".build/native/Release/Bars.app")
        self.assertEqual(record["argv"], ["--app", built, "--python", build_python, "--disable", "cursor", "--no-start"])
        # /usr/bin/python3 is an xcrun shim, so the running interpreter reports its real path.
        self.assertTrue(Path(record["python"]).name.startswith("python3"))
        # The Command Line Tools' Python is preferred when present.
        self.assertEqual(build_python, "/usr/bin/python3")
        output = result.stdout.decode()
        self.assertIn("Edit Widgets", output)
        self.assertIn("large widget", output)
        self.assertIn("--uninstall", output)

    @unittest.skipUnless(tools_available(), "Command Line Tools and git are required")
    def test_rerun_fast_forwards_and_discards_local_changes(self):
        work, bare = self.make_repository()
        self.assertEqual(self.run_bootstrap(BARS_REPO_URL=bare.as_uri()).returncode, 0)
        self.assertEqual(self.record()["marker"], 1)
        (work / "scripts/install.py").write_text("MARKER = 2\n" + STUB_INSTALL)
        git("commit", "-q", "-am", "update", cwd=work)
        git("push", "-q", str(bare), "HEAD:main", cwd=work)
        (self.source / "scripts/install.py").write_text("raise SystemExit('local edit must be discarded')\n")
        result = self.run_bootstrap("--enable-all", BARS_REPO_URL=bare.as_uri())
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.record()["marker"], 2)
        self.assertEqual(self.record()["argv"][-1], "--enable-all")

    @unittest.skipUnless(tools_available(), "Command Line Tools and git are required")
    def test_build_only_does_not_install(self):
        _, bare = self.make_repository()
        result = self.run_bootstrap("--build-only", BARS_REPO_URL=bare.as_uri())
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue((self.home / "build-record").exists())
        self.assertFalse((self.home / "install-record.json").exists())

    @unittest.skipUnless(tools_available(), "Command Line Tools and git are required")
    def test_refuses_existing_non_git_directory(self):
        self.source.mkdir(parents=True)
        (self.source / "keep").write_text("user file")
        result = self.run_bootstrap(BARS_REPO_URL="file:///nonexistent.git")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"is not a Git checkout", result.stderr)
        self.assertEqual((self.source / "keep").read_text(), "user file")

    @unittest.skipUnless(tools_available(), "Command Line Tools and git are required")
    def test_update_ignores_git_environment_pointing_at_another_repository(self):
        work, bare = self.make_repository()
        self.assertEqual(self.run_bootstrap(BARS_REPO_URL=bare.as_uri()).returncode, 0)
        other = Path(self.temporary.name) / "unrelated"
        other.mkdir()
        git("init", "-q", cwd=other)
        (other / "work.txt").write_text("committed")
        git("add", ".", cwd=other)
        git("commit", "-q", "-m", "unrelated", cwd=other)
        (other / "work.txt").write_text("uncommitted user work")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=other, capture_output=True, text=True).stdout
        result = self.run_bootstrap(BARS_REPO_URL=bare.as_uri(), GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((other / "work.txt").read_text(), "uncommitted user work")
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=other, capture_output=True, text=True).stdout, head)
        self.assertEqual(self.record()["marker"], 1)

    def test_rejects_option_like_repository_and_branch(self):
        for extra in ({"BARS_REPO_URL": "--upload-pack=touch pwned"}, {"BARS_REF": "--orphan"},
                      {"BARS_REF": "main;touch pwned"}, {"BARS_REF": "../main"}):
            with self.subTest(extra=extra):
                result = self.run_bootstrap(**extra)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(self.support.exists())

    def test_rejects_unusable_home(self):
        for home in ("", "/", "relative/home"):
            with self.subTest(home=home):
                result = self.run_bootstrap("--uninstall", HOME=home)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b"HOME", result.stderr)

    def test_refuses_root_before_any_install_or_uninstall_action(self):
        identity = self.bin / "id"
        identity.write_text("#!/bin/sh\nprintf '0\\n'\n")
        identity.chmod(0o700)
        for args in ((), ("--uninstall",)):
            result = self.run_bootstrap(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"not as root", result.stderr)
            self.assertFalse(self.support.exists())

    def test_refuses_to_clone_through_a_symlinked_support_directory(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        self.support.parent.mkdir(parents=True)
        self.support.symlink_to(elsewhere)
        result = self.run_bootstrap(BARS_REPO_URL="file:///nonexistent.git")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not a link", result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_invalid_install_options_fail_before_fetch_or_build(self):
        for args in (("--disable", "unknown"), ("--disable",), ("--unknown",),
                     ("--enable-all", "--disable", "cursor"), ("--keep-data",)):
            with self.subTest(args=args):
                result = self.run_bootstrap(*args, BARS_REPO_URL="file:///nonexistent.git")
                self.assertEqual(result.returncode, 1)
                self.assertFalse(self.support.exists())

    def test_standard_account_stops_before_fetching_or_building(self):
        # README: a standard account changes nothing. The check runs before fetch_source.
        applications = Path(self.temporary.name) / "applications"
        applications.mkdir(mode=0o555)
        try:
            result = self.run_bootstrap(BARS_REPO_URL="file:///nonexistent.git",
                                        BARS_APPLICATIONS_DIR=str(applications))
        finally:
            applications.chmod(0o755)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"cannot write to /Applications", result.stderr)
        self.assertFalse(self.support.exists())

    def test_help_does_nothing(self):
        result = self.run_bootstrap("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"--uninstall", result.stdout)
        self.assertFalse(self.support.exists())

    def test_documented_download_command_never_executes_a_failed_download(self):
        readme = (ROOT / "README.md").read_text()
        command = next(line for line in readme.splitlines()
                       if line.startswith("curl -fsSL") and "bars-bootstrap.sh" in line)
        (self.home / "Downloads").mkdir()
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\n"
                        "while [ \"$#\" -gt 0 ]; do\n"
                        "  if [ \"$1\" = -o ]; then shift; output=$1; fi\n"
                        "  shift\n"
                        "done\n"
                        "printf '%s\\n' 'touch \"$HOME/should-not-exist\"' > \"$output\"\n"
                        "exit 18\n")
        curl.chmod(0o700)
        result = subprocess.run(["/bin/sh", "-c", command], env=self.environment(),
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "should-not-exist").exists())

    def test_uninstall_uses_the_checkout_installer(self):
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "scripts/install.py").write_text("MARKER = 0\n" + STUB_INSTALL)
        result = self.run_bootstrap("--uninstall", "--keep-data", piped=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.record()["argv"], ["--uninstall", "--keep-data"])

    def test_uninstall_rejects_install_options(self):
        result = self.run_bootstrap("--uninstall", "--disable", "cursor")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"accepts only --keep-data", result.stderr)

    def inline_fallback_script(self, app, failing=(), other_owner=False):
        # Same script with the fixed /Applications and lsregister paths pointed at a
        # temporary bundle and a stub. Every system tool it runs is a recording stub, so
        # the real preferences domain and registrations are never touched.
        text = BOOTSTRAP.read_text()
        lsregister = ('LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/'
                      'LaunchServices.framework/Support/lsregister"')
        self.assertEqual(text.count('APP="/Applications/Bars.app"'), 1)
        self.assertEqual(text.count(lsregister), 1)
        if other_owner:
            # Simulate another owner only for the app; do not chown files or run as root.
            # All other file tests retain the shell's actual filesystem behavior.
            text = text.replace('[ ', 'file_test ')
            text = text.replace('main "$@" </dev/null', '''
file_test() {
  if command [ "$#" = 3 ] && command [ "$1" = -O ] && command [ "$2" = "$APP" ]; then
    return 1
  fi
  if command [ "$#" = 4 ] && command [ "$1" = ! ] && command [ "$2" = -O ] && command [ "$3" = "$APP" ]; then
    return 0
  fi
  command [ "$@"
}
main "$@" </dev/null''')
        script = Path(self.temporary.name) / "bootstrap-under-test.sh"
        script.write_text(text.replace('APP="/Applications/Bars.app"', f'APP="{app}"')
                          .replace(lsregister, f'LSREGISTER="{self.bin}/lsregister"'))
        for tool in ("launchctl", "pkill", "pluginkit", "lsregister", "defaults"):
            stub = self.bin / tool
            # Each call also records whether the app still existed at that moment.
            stub.write_text(f'#!/bin/sh\nif [ -e "{app}" ]; then state=present; else state=absent; fi\n'
                            f'echo "{tool} $* [app $state]" >> "{self.calls}"\n'
                            f'exit {1 if tool in failing else 0}\n')
            stub.chmod(0o755)
        return script

    def make_installed_state(self, identifier="local.bars.app"):
        app = Path(self.temporary.name) / "Applications/Bars.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))
        plist = self.home / "Library/LaunchAgents/local.bars.collector.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("agent")
        (self.support / "logs").mkdir(parents=True)
        (self.support / "snapshot.json").write_text("{}")
        credentials = self.home / ".codex/auth.json"
        credentials.parent.mkdir()
        credentials.write_text("synthetic")
        return app, plist, credentials

    def assert_other_owner_refused(self, checkout):
        app, _, _ = self.make_installed_state()
        preferences = self.home / "Library/Preferences/local.bars.app.plist"
        preferences.parent.mkdir(parents=True)
        preferences.write_bytes(plistlib.dumps({"NSWindow Frame overview": "synthetic"}))
        if checkout:
            (self.source / "scripts").mkdir(parents=True)
            (self.source / "scripts/install.py").write_text("MARKER = 0\n" + STUB_INSTALL)
        script = self.inline_fallback_script(app, other_owner=True)
        root = Path(self.temporary.name)
        original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        for options in ((), ("--keep-data",)):
            with self.subTest(options=options):
                result = self.run_bootstrap("--uninstall", *options, script=script)
                self.assertEqual(result.returncode, 1, result.stderr.decode())
                self.assertIn(b"belongs to another user account", result.stderr)
                self.assertFalse(self.calls.exists())
                self.assertFalse((self.home / "install-record.json").exists())
                self.assertEqual({p.relative_to(root): p.read_bytes()
                                  for p in root.rglob("*") if p.is_file()}, original)

    def test_inline_uninstall_refuses_another_accounts_app_before_any_side_effect(self):
        self.assert_other_owner_refused(checkout=False)

    def test_uninstall_refuses_another_accounts_app_before_dispatching_to_checkout(self):
        self.assert_other_owner_refused(checkout=True)

    def test_inline_uninstall_without_checkout(self):
        app, plist, credentials = self.make_installed_state()
        result = self.run_bootstrap("--uninstall", script=self.inline_fallback_script(app))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(app.exists())
        self.assertFalse(plist.exists())
        self.assertFalse(self.support.exists())
        self.assertEqual(credentials.read_text(), "synthetic")
        calls = self.calls.read_text()
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/local.bars.collector", calls)
        self.assertIn(f"pkill -U {os.getuid()} -f ^{app}/Contents/", calls)
        # Registrations are removed while the bundle still exists, then the preferences.
        lines = calls.splitlines()
        self.assertEqual(lines[-3:], [
            f"pluginkit -r {app}/Contents/PlugIns/BarsWidget.appex [app present]",
            f"lsregister -u {app} [app present]",
            "defaults delete local.bars.app [app absent]",
        ])
        self.assertIn(b"Removed the Bars preferences (local.bars.app).", result.stdout)

    def test_inline_uninstall_leaves_app_symlink_and_target(self):
        app, plist, credentials = self.make_installed_state()
        target = app.with_name("OtherBars.app")
        app.rename(target)
        app.symlink_to(target)
        result = self.run_bootstrap("--uninstall", script=self.inline_fallback_script(app, other_owner=True))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(app.is_symlink())
        self.assertTrue((target / "Contents/Info.plist").is_file())
        self.assertFalse(plist.exists())
        self.assertFalse(self.support.exists())
        self.assertEqual(credentials.read_text(), "synthetic")
        calls = self.calls.read_text()
        for tool in ("pkill", "pluginkit", "lsregister"):
            self.assertNotIn(tool, calls)

    def test_inline_uninstall_cleans_up_when_app_is_already_absent(self):
        app, plist, credentials = self.make_installed_state()
        shutil.rmtree(app)
        result = self.run_bootstrap("--uninstall", script=self.inline_fallback_script(app, other_owner=True))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(plist.exists())
        self.assertFalse(self.support.exists())
        self.assertEqual(credentials.read_text(), "synthetic")
        calls = self.calls.read_text()
        for tool in ("pkill", "pluginkit", "lsregister"):
            self.assertNotIn(tool, calls)

    def test_inline_uninstall_removes_a_preferences_file_that_defaults_left(self):
        app, _, _ = self.make_installed_state()
        preferences = self.home / "Library/Preferences/local.bars.app.plist"
        preferences.parent.mkdir(parents=True)
        preferences.write_bytes(plistlib.dumps({"NSWindow Frame overview": "synthetic"}))
        other = self.home / "Library/Preferences/com.example.other.plist"
        other.write_text("unrelated")
        script = self.inline_fallback_script(app, failing=("pluginkit", "lsregister", "defaults"))
        result = self.run_bootstrap("--uninstall", "--keep-data", script=script)
        # Failed registration and defaults steps are tolerated.
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(app.exists())
        self.assertFalse(preferences.exists())
        self.assertEqual(other.read_text(), "unrelated")
        self.assertTrue((self.support / "snapshot.json").exists())
        self.assertIn(b"Removed the Bars preferences (local.bars.app).", result.stdout)

    def test_inline_uninstall_reports_no_preferences_when_none_exist(self):
        app, _, _ = self.make_installed_state()
        result = self.run_bootstrap("--uninstall", script=self.inline_fallback_script(app, failing=("defaults",)))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn(b"preferences", result.stdout)

    def test_inline_uninstall_leaves_a_symlinked_support_directory(self):
        app, plist, _ = self.make_installed_state()
        elsewhere = Path(self.temporary.name) / "elsewhere"
        self.support.rename(elsewhere)
        self.support.symlink_to(elsewhere)
        result = self.run_bootstrap("--uninstall", script=self.inline_fallback_script(app))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.support.is_symlink())
        self.assertTrue((elsewhere / "snapshot.json").exists())

    def test_inline_uninstall_keeps_data_and_unrelated_apps(self):
        app, plist, _ = self.make_installed_state(identifier="com.example.other")
        result = self.run_bootstrap("--uninstall", "--keep-data", script=self.inline_fallback_script(app))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(app.exists())
        self.assertFalse(plist.exists())
        self.assertTrue((self.support / "snapshot.json").exists())
        self.assertIn(b"not a Bars build", result.stdout)
        # Another application's registrations are left alone.
        calls = self.calls.read_text()
        self.assertNotIn("pluginkit", calls)
        self.assertNotIn("lsregister", calls)


if __name__ == "__main__":
    unittest.main()
