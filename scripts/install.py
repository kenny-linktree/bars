#!/usr/bin/env python3
"""Install or uninstall Bars and its per-user scheduled collector on this Mac."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

LABEL = "local.bars.collector"
BUNDLE_IDENTIFIER = "local.bars.app"
PROJECT = Path(__file__).resolve().parents[1]
PROVIDERS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "devin": "Devin"}
MINIMUM_PYTHON = (3, 9)
# The same allowlist is used by the installed refresh runner and credential tools.
CHILD_ENVIRONMENT = (
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "__CF_USER_TEXT_ENCODING", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")
WIDGET_EXTENSION = "Contents/PlugIns/BarsWidget.appex"
# A refresh can hold its lock for about 148 seconds: its 120-second run limit plus the
# collector cleanup and publication timeouts in integration/refresh.py.
REFRESH_LOCK_WAIT_SECONDS = 160
CHANGE_HINT = ("To change providers later, use the Bars dropdown, or re-run the installer "
               "with --enable-all or --disable ID (repeatable).")


def user_home() -> Path:
    """The account's home from the password database, not $HOME.

    Every path the installer writes or removes derives from this, so an empty or
    unusual HOME cannot redirect them. The native app resolves its storage the same way.
    """
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute() or home == Path("/"):
        raise RuntimeError("This account has no usable home directory.")
    return home


def is_private_directory(path: Path) -> bool:
    """A real directory (not a symlink) owned by this user."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def private_directory(path: Path) -> None:
    """Create `path` or tighten an existing one to 0700, refusing anything but a real directory this user owns."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not is_private_directory(path):
        raise RuntimeError(f"{path} must be a directory owned by you, not a link or another user's directory.")
    os.chmod(path, 0o700, follow_symlinks=False)


def launch_agent(refresh: Path, support: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(refresh)],
        "RunAtLoad": True,
        "StartCalendarInterval": [{"Minute": minute} for minute in (0, 15, 30, 45)],
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "ThrottleInterval": 30,
        "WorkingDirectory": str(support),
        "StandardOutPath": str(support / "logs/launchd.log"),
        "StandardErrorPath": str(support / "logs/launchd.log"),
    }


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def running_processes(executable: Path) -> list[int]:
    """PIDs whose command line starts with this executable path."""
    pattern = "^" + re.escape(str(executable)) + "( |$)"
    # Only this user's processes: another account's Bars is not ours to end.
    result = subprocess.run(["/usr/bin/pgrep", "-U", str(os.getuid()), "-f", pattern],
                            capture_output=True, check=False)
    pids = []
    for token in (result.stdout or b"").split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def terminate(pids: list[int], timeout: float = 5.0) -> None:
    """End processes that still run code from a replaced bundle."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        still = []
        for pid in remaining:
            try:
                os.kill(pid, 0)
                still.append(pid)
            except (ProcessLookupError, PermissionError):
                continue
        remaining = still
        if remaining:
            time.sleep(0.1)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def restart_user_interfaces(destination: Path, executable: Path, *, relaunch_app: bool) -> list[str]:
    """Replace stale processes so the widget and the menu bar dropdown show the installed code.

    macOS keeps a WidgetKit extension process alive across bundle replacement and keeps
    serving timelines from the old binary, which is how the widget and the dropdown drifted
    apart after earlier installs. Returns warnings; none of these steps undo the installation.
    """
    warnings = []
    widget = destination / "Contents/PlugIns/BarsWidget.appex/Contents/MacOS/BarsWidget"
    terminate(running_processes(widget))
    terminate(running_processes(executable))
    try:
        reload = subprocess.run([str(executable), "--reload-widgets"], check=False, timeout=20)
        if reload.returncode:
            warnings.append("The widget timeline reload request failed; the widget updates at its next scheduled reload.")
    except (OSError, subprocess.TimeoutExpired):
        warnings.append("The widget timeline reload request could not run; the widget updates at its next scheduled reload.")
    if relaunch_app:
        opened = subprocess.run(["/usr/bin/open", "-g", str(destination)], check=False)
        if opened.returncode:
            warnings.append(f"Bars could not be relaunched automatically. Open {destination} to show the menu bar item.")
    return warnings


@contextmanager
def refresh_lock(support: Path, timeout: float = REFRESH_LOCK_WAIT_SECONDS):
    """Wait for any running refresh, then hold its lock so none starts meanwhile."""
    descriptor = os.open(support / "refresh.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("A refresh is still running; try again in a minute.")
                time.sleep(0.2)
        yield


def detect_logins(python: Path, path: str) -> dict[str, bool] | None:
    """Ask the collector which providers have local login material. None if it fails.

    Runs the source checkout's collector through the chosen interpreter with the same
    PATH the scheduled job receives, so the Devin CLI lookup matches collection.
    """
    env = {key: os.environ[key] for key in CHILD_ENVIRONMENT if key in os.environ}
    env.update(HOME=str(user_home()), PATH=path, PYTHONPATH=str(PROJECT / "collector"),
               PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run([str(python), "-m", "bars_collector", "--detect"], env=env,
                                cwd=str(PROJECT / "collector"), capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=60, check=False)
        data = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(pid), bool) for pid in PROVIDERS):
        return None
    return {pid: data[pid] for pid in PROVIDERS}


def choose_disabled(detected: dict[str, bool] | None, disable: list[str] | None,
                    enable_all: bool, previous: list[str] | None = None) -> list[str]:
    """Explicit flags win. Without flags, a reinstall keeps the existing selection, which the
    dropdown may have edited; only a first install disables providers without local login
    material."""
    if enable_all:
        return []
    if disable is not None:
        return [pid for pid in PROVIDERS if pid in disable]
    if previous is not None:
        return [pid for pid in PROVIDERS if pid in previous]
    if detected is None:
        return []  # Keep every provider visible rather than hiding one by mistake.
    return [pid for pid in PROVIDERS if not detected[pid]]


def previous_disabled(config_path: Path) -> list[str] | None:
    """The `disabled_providers` list of an existing installation, or None when there is none."""
    try:
        previous = json.loads(config_path.read_bytes()).get("disabled_providers")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(previous, list) and all(isinstance(pid, str) for pid in previous):
        return previous
    return None


def provider_summary(detected: dict[str, bool] | None, disabled: list[str]) -> list[str]:
    lines = []
    for pid, name in PROVIDERS.items():
        login = "login not checked" if detected is None else ("login found" if detected[pid] else "no login found")
        lines.append(f"  {name}: {login}, {'disabled' if pid in disabled else 'enabled'}")
    return lines


def register_app(app: Path) -> None:
    subprocess.run([LSREGISTER, "-f", str(app)], check=True)
    subprocess.run(["/usr/bin/pluginkit", "-a", str(app / WIDGET_EXTENSION)], check=True)


def unregister_app(app: Path) -> None:
    """Best effort: make the widget gallery and Launch Services forget a bundle before
    it is deleted, so no stale registration names a path that no longer exists."""
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    for args in (["/usr/bin/pluginkit", "-r", str(app / WIDGET_EXTENSION)], [LSREGISTER, "-u", str(app)]):
        try:
            subprocess.run(args, check=False, timeout=30, **quiet)
        except (OSError, subprocess.SubprocessError):
            pass


def remove_staged_apps(app_stage: Path) -> None:
    # A replaced bundle keeps its registrations when it is renamed into staging.
    for name in ("old.app", "new.app"):
        staged = app_stage / name
        if staged.is_dir() and not staged.is_symlink():
            unregister_app(staged)
    shutil.rmtree(app_stage)


def install(app: Path, python: Path, *, start: bool = True,
            disable: list[str] | None = None, enable_all: bool = False,
            destination: Path | None = None, support: Path | None = None,
            agents: Path | None = None) -> None:
    """Install Bars. With neither `disable` nor `enable_all`, providers without local
    login material are disabled; a disabled provider still appears in the snapshot."""
    if sys.platform != "darwin":
        raise RuntimeError("Bars installation requires macOS.")
    app = app.resolve()
    # Keep Homebrew's stable symlink rather than pinning a removable Cellar version.
    python = python.expanduser().absolute()
    destination = destination or Path("/Applications/Bars.app")
    support = support or user_home() / "Library/Application Support/Bars"
    agents = agents or user_home() / "Library/LaunchAgents"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != BUNDLE_IDENTIFIER:
        raise RuntimeError("The input is not a Bars application bundle.")
    # A standard account cannot write to /Applications. Stop before touching storage,
    # logins or the scheduled job; Bars never asks for an administrator password.
    if not os.access(destination.parent, os.W_OK | os.X_OK):
        raise RuntimeError(f"This account cannot write to {destination.parent}. "
                           "Install Bars from an account that can, such as an administrator account.")
    # Renaming a bundle needs write access to the bundle itself, so a second account cannot
    # replace one installed by the first. Say so instead of failing midway with EACCES.
    if destination.exists() and os.lstat(destination).st_uid != os.getuid():
        raise RuntimeError(f"{destination} belongs to another user account on this Mac. "
                           "Install and update Bars from the account that installed it, "
                           "or have that account uninstall it first.")
    package = PROJECT / "collector/bars_collector"
    if not package.is_dir():
        raise RuntimeError("Collector package is missing.")
    version_check = subprocess.run([str(python), "-c", "import sys; sys.exit(sys.version_info < %r)" % (MINIMUM_PYTHON,)],
                                   check=False)
    if version_check.returncode:
        raise RuntimeError(f"{python} is not Python {'.'.join(map(str, MINIMUM_PYTHON))} or later.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    # Check shared storage before changing any installed files or scheduled jobs.
    subprocess.run([str(app / "Contents/MacOS" / info["CFBundleExecutable"]),
                    "--check-storage"], check=True, timeout=20)
    os.umask(0o077)
    for directory in (support, support / "logs", support / "bin"):
        private_directory(directory)
    # launchd would create its log with mode 0644; create it, or tighten it, first. Each
    # install also empties it, so it cannot grow without bound across reinstalls.
    log = os.open(support / "logs/launchd.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(log, 0o600)
    finally:
        os.close(log)
    agents.mkdir(parents=True, exist_ok=True)
    runtime = support / "runtime"
    refresh = support / "bin/refresh"
    config_path = support / "installation.json"
    plist_path = agents / f"{LABEL}.plist"
    executable = destination / "Contents/MacOS" / info["CFBundleExecutable"]
    devin = shutil.which("devin")
    # Pin only an absolute CLI directory. A relative PATH entry names whatever directory
    # the installer happened to run from, which would then lead the scheduled PATH.
    search_dirs = ([os.path.dirname(devin)] if devin and os.path.isabs(devin) else []) + [
        str(user_home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    config = {"python": str(python), "collector_path": str(runtime),
              "app_executable": str(executable), "path": ":".join(dict.fromkeys(search_dirs))}
    detected = detect_logins(python, config["path"])
    # The app also edits this list in place; refresh.py reads it on every run.
    config["disabled_providers"] = choose_disabled(detected, disable, enable_all,
                                                   previous=previous_disabled(config_path))
    launcher = f'#!/bin/sh\nexec {shlex.quote(str(python))} {shlex.quote(str(runtime / "refresh.py"))} "$@"\n'
    files = {
        config_path: (json.dumps(config, indent=2).encode(), 0o600),
        refresh: (launcher.encode(), 0o700),
        plist_path: (plistlib.dumps(launch_agent(refresh, support)), 0o600),
    }
    app_stage = Path(tempfile.mkdtemp(prefix=".bars-install-", dir=destination.parent))
    runtime_stage = Path(tempfile.mkdtemp(prefix=".install-", dir=support))
    may_remove_staging = True
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        shutil.copytree(app, app_stage / "new.app")
        new_runtime = runtime_stage / "new-runtime"
        shutil.copytree(package, new_runtime / "bars_collector",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(PROJECT / "integration/refresh.py", new_runtime / "refresh.py")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                        str(app_stage / "new.app")], check=True)
        with refresh_lock(support):
            if destination.exists():
                old_info = plistlib.loads((destination / "Contents/Info.plist").read_bytes())
                if old_info.get("CFBundleIdentifier") != info["CFBundleIdentifier"]:
                    raise RuntimeError("An unrelated application already occupies the destination.")
            previous_files = {path: ((path.read_bytes(), path.stat().st_mode & 0o777)
                                     if path.exists() else None) for path in files}
            was_loaded = subprocess.run(["/bin/launchctl", "print", service],
                                        check=False, **quiet).returncode == 0
            app_was_running = bool(running_processes(executable))
            if was_loaded:
                subprocess.run(["/bin/launchctl", "bootout", service], check=True, **quiet)
            switched = []
            may_remove_staging = False
            try:
                for new, live, old in (
                    (app_stage / "new.app", destination, app_stage / "old.app"),
                    (new_runtime, runtime, runtime_stage / "old-runtime"),
                ):
                    if live.exists():
                        live.rename(old)
                    switched.append((live, old))
                    new.rename(live)
                for path, (data, mode) in files.items():
                    atomic_write(path, data, mode)
                subprocess.run(["/usr/bin/plutil", "-lint", str(plist_path)], check=True)
                register_app(destination)
                if start:
                    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist_path)], check=True)
            except BaseException:
                subprocess.run(["/bin/launchctl", "bootout", service], check=False, **quiet)
                for live, old in reversed(switched):
                    if live.exists():
                        shutil.rmtree(live)
                    if old.exists():
                        old.rename(live)
                for path, previous in previous_files.items():
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, *previous)
                if destination.exists():
                    register_app(destination)
                if was_loaded:
                    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist_path)], check=True)
                may_remove_staging = True
                raise
            may_remove_staging = True
            # The files are in place. Processes still running the previous code are replaced
            # while the refresh lock is held, so no publication races the restart.
            warnings = restart_user_interfaces(destination, executable,
                                               relaunch_app=start or app_was_running)
        # RunAtLoad may have fired while installation held the refresh lock.
        if start:
            subprocess.run(["/bin/launchctl", "kickstart", service], check=True)
    finally:
        # If rollback itself fails, retain backup directories for recovery.
        if may_remove_staging:
            remove_staged_apps(app_stage)
            shutil.rmtree(runtime_stage)
    print(f"Installed {destination}")
    print(f"Collector launcher: {refresh}")
    print("Schedule: every quarter hour, with missed refreshes resumed after wake.")
    print("Widget extension restarted and timeline reload requested."
          + (" Menu bar app launched." if start or app_was_running else ""))
    print("Providers:")
    for line in provider_summary(detected, config["disabled_providers"]):
        print(line)
    print(CHANGE_HINT)
    if detected is None:
        warnings.append("Login detection did not run, so no provider was disabled automatically.")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)


def is_bars_bundle(app: Path) -> bool:
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    return isinstance(info, dict) and info.get("CFBundleIdentifier") == BUNDLE_IDENTIFIER


def remove_preferences(preferences: Path) -> bool:
    """Remove the app's AppKit preferences (window frames). True if anything was removed.

    `defaults` asks cfprefsd to drop the domain, so a cached copy cannot be written back;
    the file is then deleted if it remains. A link at that path is removed, not followed.
    """
    try:
        removed = subprocess.run(["/usr/bin/defaults", "delete", BUNDLE_IDENTIFIER], check=False,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        removed = False  # Absent domain, or no defaults tool: the file check below still runs.
    path = preferences / f"{BUNDLE_IDENTIFIER}.plist"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return removed
    if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        try:
            path.unlink()
            removed = True
        except OSError:
            print(f"Warning: {path} could not be removed.", file=sys.stderr)
    return removed


def uninstall(*, keep_data: bool = False, destination: Path | None = None,
              support: Path | None = None, agents: Path | None = None,
              preferences: Path | None = None) -> list[str]:
    """Remove the scheduled job, the app, its preferences and, unless `keep_data`, Bars' own data.

    Idempotent. Returns a description of each removal. Provider credentials (Keychain
    items, CLI login files) are never read or touched.
    """
    destination = destination or Path("/Applications/Bars.app")
    # Refuse before stopping jobs or processes, unregistering bundles or removing data.
    # Links remain subject to the existing leave-in-place behavior below.
    if not destination.is_symlink() and destination.exists() and os.lstat(destination).st_uid != os.getuid():
        raise RuntimeError(f"{destination} belongs to another user account on this Mac. "
                           "Uninstall Bars from the account that installed it.")
    support = support or user_home() / "Library/Application Support/Bars"
    agents = agents or user_home() / "Library/LaunchAgents"
    preferences = preferences or user_home() / "Library/Preferences"
    plist_path = agents / f"{LABEL}.plist"
    service = f"gui/{os.getuid()}/{LABEL}"
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    removed = []
    if subprocess.run(["/bin/launchctl", "print", service], check=False, **quiet).returncode == 0:
        subprocess.run(["/bin/launchctl", "bootout", service], check=False, **quiet)
        removed.append(f"Stopped the scheduled collector ({LABEL}).")
    # Never follow a link at the fixed app path: only a real Bars bundle is removed.
    bars_app = not destination.is_symlink() and destination.is_dir() and is_bars_bundle(destination)
    foreign = (destination.exists() or destination.is_symlink()) and not bars_app
    if foreign:
        print(f"Warning: {destination} is not a Bars build; it was left in place.", file=sys.stderr)
    executables = [destination / "Contents/PlugIns/BarsWidget.appex/Contents/MacOS/BarsWidget",
                   destination / "Contents/MacOS/Bars"]
    # Processes of another application at that path are not Bars' to end. With no bundle
    # left, a match is normally a Bars process that outlived an earlier removal.
    pids = [] if foreign else [pid for executable in executables for pid in running_processes(executable)]
    if pids:
        terminate(pids)
        removed.append("Ended the running Bars app and widget processes.")
    if plist_path.exists():
        plist_path.unlink()
        removed.append(f"Removed {plist_path}")
    if bars_app:
        # Unregister first so the widget gallery and Launch Services forget the bundle.
        unregister_app(destination)
        shutil.rmtree(destination)
        removed.append(f"Removed {destination}")
    # After the app's processes have ended, so none can write its preferences back.
    if remove_preferences(preferences):
        removed.append(f"Removed the Bars preferences ({BUNDLE_IDENTIFIER}).")
    if support.exists() or support.is_symlink():
        if keep_data:
            removed.append(f"Kept {support} (--keep-data).")
        elif not is_private_directory(support):
            print(f"Warning: {support} is a link or is not owned by you; it was left in place.", file=sys.stderr)
        else:
            # A manual refresh started by the app may still be finishing.
            with refresh_lock(support):
                shutil.rmtree(support)
            removed.append(f"Removed {support}")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=PROJECT / ".build/native/Release/Bars.app")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--disable", action="append", choices=list(PROVIDERS), metavar="ID",
                        help="Disable this provider (claude, codex, cursor or devin). Repeatable. "
                             "Without --disable or --enable-all, providers with no local login are disabled.")
    parser.add_argument("--enable-all", action="store_true", help="Enable every provider, whatever is detected.")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove the scheduled job, /Applications/Bars.app, its preferences and Bars' data. "
                             "Logins are untouched.")
    parser.add_argument("--keep-data", action="store_true", help="With --uninstall, keep ~/Library/Application Support/Bars.")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("Run the Bars installer as your own user, not as root or with sudo.", file=sys.stderr)
        return 1
    if args.disable and args.enable_all:
        parser.error("--disable and --enable-all cannot be combined")
    if args.keep_data and not args.uninstall:
        parser.error("--keep-data requires --uninstall")
    if args.uninstall:
        if args.disable or args.enable_all or args.no_start:
            parser.error("--uninstall accepts only --keep-data")
        try:
            removed = uninstall(keep_data=args.keep_data)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Uninstall failed: {error}", file=sys.stderr)
            return 1
        for line in removed or ["Bars is not installed; nothing to remove."]:
            print(line)
        return 0
    try:
        install(args.app, args.python, start=not args.no_start,
                disable=args.disable, enable_all=args.enable_all)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
