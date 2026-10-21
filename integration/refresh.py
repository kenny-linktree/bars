#!/usr/bin/env python3
"""Run one collection and publish completed snapshots to the native widget."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

MAX_RUN_SECONDS = 120
# Allow an explicit request to follow a bounded run and its publication/cleanup.
MAX_LOCK_WAIT_SECONDS = 150
MAX_CONFIG_BYTES = 65536
# The collector ignores a request time older than this or further ahead than the tolerance
# (bars_collector.runner.MAX_REQUEST_AGE and schedule.DUE_TOLERANCE). A supplied time
# outside that window is treated as a request made now.
MAX_REQUEST_AGE = 10 * 60
REQUEST_TOLERANCE = 20
# The collector's exit status when the installed plugins no longer fit one snapshot file
# (bars_collector.__main__.SNAPSHOT_TOO_LARGE_STATUS); refresh.py runs it as a subprocess.
SNAPSHOT_TOO_LARGE_STATUS = 78
# The dropdown reads the published file directly, so it is always current. The desktop
# widget only redraws when WidgetKit reloads its timeline, and WidgetKit budgets those
# reloads per day. With a collection every minute, a reload is requested only when the
# displayed values changed and at least this long has passed since the last reload, or
# when this long has passed regardless (so the widget's "Updated" time keeps moving), or
# for an explicit refresh from the app. The widget's own 15-minute fallback still applies.
WIDGET_RELOAD_MIN_SECONDS = 5 * 60
WIDGET_RELOAD_MAX_SECONDS = 15 * 60
WIDGET_RELOAD_STATE = "widget-reload.json"
# Fields that change on every run without changing what the widget shows.
_TIMESTAMP_FIELDS = ("generated_at", "fetched_at", "last_attempt_at")
# A provider plugin id, the same grammar the collector enforces for plugin directories.
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_LOGGABLE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
# The collector's standard output is one status-only line, such as
# "claude: ok; codex: setup; cursor: disabled; devin: error". Only output in exactly
# that form is logged; anything else is reduced to a fixed line.
MAX_STATUS_BYTES = 4096
_STATUS_ENTRY = re.compile(r"([a-z][a-z0-9_]{0,31}): (ok|error|login_required|setup|disabled)")
# The collector's standard error carries scheduling diagnostics, one line per provider in a
# fixed grammar (bars_collector/schedule.py): provider ids, fixed words, whole numbers and
# canonical times. Only lines in exactly that form are logged, joined into one log line;
# everything else on standard error (fixed failure messages, a traceback) is never logged.
MAX_DIAGNOSTIC_BYTES = 16384
_SCHEDULE_ENTRY = re.compile(
    r"schedule: (?:[a-z][a-z0-9_]{0,31} "
    r"(?:attempted outcome=(?:ok|error|timeout|invalid|login_required|setup|rate_limited)"
    r"|skipped reason=(?:not_due|rate_limited|already_attempted))"
    r" interval=\d{2,4}s(?: retry_after=\d{1,5}s)?(?: next=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)?"
    r"(?: change=(?:slower|faster))?"
    r"|state (?:not_saved|discarded reason=(?:unreadable|invalid|unsupported)))")
# Enough for several days of history at one run a minute.
LOG_MAX_BYTES = 1024 * 1024
# Keep in step with the credential tools' environment in bars_plugin/access.py.
CHILD_ENVIRONMENT = (
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "__CF_USER_TEXT_ENCODING", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def child_environment() -> dict:
    return {key: os.environ[key] for key in CHILD_ENVIRONMENT if key in os.environ}


def private_directory(path: Path) -> Path:
    """Create `path` with mode 0700, or accept it only as a real directory this user owns."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("Bars storage is not a private directory.")
    os.chmod(path, 0o700, follow_symlinks=False)
    return path


def open_private(path: Path, flags: int) -> int:
    """Open without following a symlink planted at `path`; created files have mode 0600."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PermissionError("Bars storage is not a private regular file.")
        if flags & (os.O_WRONLY | os.O_RDWR):
            os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Validate the log on every open, including opens after rotation."""

    def _open(self):
        descriptor = open_private(Path(self.baseFilename), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        return os.fdopen(descriptor, self.mode, encoding="utf-8")


def read_config(path: Path) -> dict:
    descriptor = open_private(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("installation.json is not a regular file.")
        data = file.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("installation.json is too large.")
    config = json.loads(data)
    if not isinstance(config, dict):
        raise ValueError("installation.json is not an object.")
    return config


def revision(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def jitter_delay(seconds: float, *, sleep=time.sleep, draw=random.random) -> float:
    """Wait a uniformly random time up to `seconds` so Macs on the same minute schedule do
    not all ask the providers at once. Zero means no wait. Returns the time waited."""
    if seconds <= 0:
        return 0.0
    delay = draw() * seconds
    sleep(delay)
    return delay


def display_digest(snapshot: Path) -> str | None:
    """A digest of everything in the snapshot that affects what the widget shows, or None
    when the file cannot be read. Timestamps are excluded, so a run that only refreshed
    `fetched_at` has the same digest as the run before it."""
    try:
        with os.fdopen(open_private(snapshot, os.O_RDONLY | os.O_NONBLOCK), "rb") as file:
            data = json.loads(file.read(1024 * 1024 + 1))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    view = {key: value for key, value in data.items() if key not in _TIMESTAMP_FIELDS}
    if isinstance(view.get("providers"), list):
        view["providers"] = [{k: v for k, v in p.items() if k not in _TIMESTAMP_FIELDS} if isinstance(p, dict) else p
                             for p in view["providers"]]
    return hashlib.sha256(json.dumps(view, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_reload_state(directory: Path) -> dict:
    try:
        with os.fdopen(open_private(directory / WIDGET_RELOAD_STATE, os.O_RDONLY | os.O_NONBLOCK), "rb") as file:
            state = json.loads(file.read(4096))
    except (OSError, ValueError, UnicodeError):
        return {}
    return state if isinstance(state, dict) else {}


def write_reload_state(directory: Path, digest: str | None, at: float) -> None:
    data = json.dumps({"digest": digest, "at": at}).encode()
    descriptor, temporary = tempfile.mkstemp(prefix="." + WIDGET_RELOAD_STATE + ".", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        os.replace(temporary, directory / WIDGET_RELOAD_STATE)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def should_reload_widget(directory: Path, snapshot: Path, *, explicit: bool, now: float = None) -> tuple[bool, str | None]:
    """Whether the final publication should ask WidgetKit for a timeline reload.

    Returns the decision and the current display digest to record if a reload is made.
    """
    now = time.time() if now is None else now
    digest = display_digest(snapshot)
    if explicit or digest is None:
        return True, digest
    state = read_reload_state(directory)
    last = state.get("at")
    since = (now - last) if isinstance(last, (int, float)) and not isinstance(last, bool) else float("inf")
    # A recorded time in the future (the clock was set back) or a corrupt one must not
    # suppress reloads until the clock catches up; treat it as never reloaded.
    if since != since or since < 0 or since >= WIDGET_RELOAD_MAX_SECONDS:
        return True, digest
    return digest != state.get("digest") and since >= WIDGET_RELOAD_MIN_SECONDS, digest


def publish(executable: Path, snapshot: Path, *, reload: bool) -> bool:
    args = [str(executable), "--publish-snapshot", str(snapshot)]
    if not reload:
        args.append("--no-reload")
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env=child_environment(), timeout=15, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def signal_group(pgid: int, signum: int) -> bool:
    """Signal the collector's process group; False when nothing in it can be signalled.

    ProcessLookupError means the group no longer exists. macOS raises PermissionError
    when the group's only member is its exited but not yet reaped leader (a zombie).
    """
    try:
        os.killpg(pgid, signum)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_process(process: subprocess.Popen) -> None:
    try:
        if signal_group(process.pid, signal.SIGTERM):
            # The coordinator unwinds its independent provider process groups.
            process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # A group can still contain descendants after its leader has exited.
        signal_group(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def log_statuses(output, logger: logging.Logger) -> None:
    """Log the collector's provider statuses if its output is exactly the status line."""
    output.seek(0)
    data = output.read(MAX_STATUS_BYTES + 1)
    if not data.strip():
        return
    try:
        entries = data.decode("ascii").strip().split("; ") if len(data) <= MAX_STATUS_BYTES else None
    except UnicodeError:
        entries = None
    matches = [_STATUS_ENTRY.fullmatch(entry) for entry in entries or ()]
    ids = [match.group(1) for match in matches if match]
    if not entries or len(ids) != len(entries) or len(set(ids)) != len(ids):
        logger.warning("Collector status output was not recognized.")
        return
    logger.info("Provider statuses: %s.", "; ".join(match.group(0) for match in matches))


def log_schedule(errors, logger: logging.Logger) -> None:
    """Log the collector's scheduling diagnostics, only lines in exactly their grammar."""
    errors.seek(0)
    data = errors.read(MAX_DIAGNOSTIC_BYTES + 1)
    truncated = len(data) > MAX_DIAGNOSTIC_BYTES
    lines = data[:MAX_DIAGNOSTIC_BYTES].split(b"\n")
    if truncated:
        lines.pop()  # a line cut by the bound is not a whole diagnostic
    entries = []
    unrecognized = False
    for line in lines:
        if not line.startswith(b"schedule:"):
            continue
        try:
            text = line.decode("ascii")
        except UnicodeError:
            text = None
        if text is not None and _SCHEDULE_ENTRY.fullmatch(text):
            entries.append(text[len("schedule: "):])
        else:
            unrecognized = True
    if entries:
        logger.info("Schedule: %s.", "; ".join(entries))
    if unrecognized:
        logger.warning("Collector schedule diagnostics were not recognized.")
    if truncated:
        logger.warning("Collector schedule diagnostics exceeded %d bytes; the rest were not logged.",
                       MAX_DIAGNOSTIC_BYTES)


def disabled_providers(config: dict, logger: logging.Logger) -> list[str]:
    """Provider IDs that are disabled, each once, in the file's order. Missing means none.

    Only well-formed plugin ids are passed on; the collector ignores ids with no plugin.
    """
    value = config.get("disabled_providers", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        logger.error("Ignoring invalid disabled_providers in installation.json; collecting all providers.")
        return []
    invalid = sorted(set(pid for pid in value if not _PROVIDER_ID.fullmatch(pid)))
    if invalid:
        # The file is user-editable: never let its text forge or split log lines.
        names = [pid if _LOGGABLE_ID.fullmatch(pid) else "(invalid)" for pid in invalid]
        logger.warning("Ignoring invalid disabled provider ids: %s.", ", ".join(names))
    return list(dict.fromkeys(pid for pid in value if _PROVIDER_ID.fullmatch(pid)))


def acquire_refresh_lock(lock, *, wait: bool, logger: logging.Logger) -> bool:
    deadline = time.monotonic() + MAX_LOCK_WAIT_SECONDS
    waiting = False
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if not wait:
                logger.info("A refresh is already running.")
                return False
            if not waiting:
                logger.info("Waiting for the active refresh to finish.")
                waiting = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("Timed out waiting for the active refresh to finish.")
                return False
            time.sleep(min(0.25, remaining))


def collect_and_publish(config: dict, directory: Path, provider: str | None,
                        logger: logging.Logger, *, explicit: bool = True, requested_at: int | None = None) -> int:
    """Run the collector once and publish. `explicit` marks a refresh the person asked for,
    which always requests a widget reload; scheduled runs reload on the throttled policy.
    `requested_at` is when the person asked, before any wait for the lock."""
    snapshot = directory / "snapshot.json"
    executable = Path(config["app_executable"])
    args = [config["python"], "-m", "bars_collector", "--output", str(snapshot),
            "--plugins", str(config.get("plugins_path") or directory / "plugins")]
    if provider:
        args += ["--provider", provider]
    # Every run leaves a rate-limited provider alone until its pause ends. A scheduled run
    # also asks each provider only when it is due on its own interval. A refresh the person
    # asked for skips only providers a run that held the lock meanwhile has just attempted.
    if not explicit:
        args.append("--scheduled")
    elif requested_at is not None:
        args += ["--requested-at", str(int(requested_at))]
    # The collector skips disabled workers and records every provider's enabled state.
    for pid in disabled_providers(config, logger):
        args += ["--disable", pid]
    env = child_environment()
    env["PATH"] = config.get("path", os.defpath)
    env["PYTHONPATH"] = config["collector_path"]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    previous = revision(snapshot)
    started = time.monotonic()
    published = False
    publication_failed = False
    cancelled = False

    def request_cancellation(signum, frame):
        # Do not raise from a signal handler: Popen may have created its child but
        # not returned its handle yet. The loop unwinds only after it owns that handle.
        nonlocal cancelled
        cancelled = True

    # Standard output holds only provider statuses and standard error the scheduling
    # diagnostics; both go to unlinked private files so a stalled reader can never block
    # the collector, and only lines in their exact grammar reach the log.
    output = tempfile.TemporaryFile(dir=directory)
    errors = tempfile.TemporaryFile(dir=directory)
    previous_handler = signal.signal(signal.SIGTERM, request_cancellation)
    process = None
    try:
        if cancelled:
            return 128 + signal.SIGTERM
        # `python -m` puts the working directory first on sys.path. launchd starts this
        # runner in the support directory, so run the collector from its own package root.
        process = subprocess.Popen(args, env=env, cwd=config["collector_path"], stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=errors,
                                   start_new_session=True)
        while process.poll() is None:
            if cancelled:
                return 128 + signal.SIGTERM
            if time.monotonic() - started > MAX_RUN_SECONDS:
                logger.error("Collection exceeded its time limit; retaining saved results.")
                stop_process(process)
                return 1
            current = revision(snapshot)
            if current is not None and current != previous:
                if publish(executable, snapshot, reload=False):
                    published = True
                else:
                    publication_failed = True
                previous = current
            # Keep the partial-publication cadence, but wake as soon as the
            # collector exits instead of adding a full polling interval.
            remaining = MAX_RUN_SECONDS - (time.monotonic() - started)
            try:
                process.wait(timeout=min(0.25, max(0, remaining)))
            except subprocess.TimeoutExpired:
                pass
        if cancelled:
            return 128 + signal.SIGTERM
        current = revision(snapshot)
        changed = current != previous
        # A successful no-op still retries saved data after an earlier publication
        # failure. Provider fetch timestamps stay unchanged in that saved snapshot.
        if current is not None and (changed or published or publication_failed or process.returncode == 0):
            reload, digest = should_reload_widget(directory, snapshot, explicit=explicit)
            if not publish(executable, snapshot, reload=reload):
                logger.error("Saved results could not be published to widget storage.")
                return 1
            if reload:
                write_reload_state(directory, digest, time.time())
        if cancelled:
            return 128 + signal.SIGTERM
        log_schedule(errors, logger)
        log_statuses(output, logger)
        if process.returncode == SNAPSHOT_TOO_LARGE_STATUS:
            logger.error("Collector exited with status %s: the installed plugins exceed the snapshot size bound. "
                         "Remove some plugins. The previous snapshot was kept.", process.returncode)
            return 1
        if process.returncode:
            logger.error("Collector exited with status %s.", process.returncode)
            return 1
        logger.info("Refresh completed for %s in %.1f seconds.", provider or "all providers",
                    time.monotonic() - started)
        return 0
    finally:
        try:
            if process is not None:
                stop_process(process)
        finally:
            output.close()
            errors.close()
            signal.signal(signal.SIGTERM, previous_handler)


def provider_id(value: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("provider ids are lowercase letters, digits and underscores")
    return value


def epoch_seconds(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise argparse.ArgumentTypeError("request times are whole UTC epoch seconds")
    return int(value)


def request_time(supplied: int | None, now: int) -> int:
    """The time the person asked: the app's queued request time when the collector would
    accept it, otherwise now."""
    if supplied is not None and now - MAX_REQUEST_AGE <= supplied <= now + REQUEST_TOLERANCE:
        return supplied
    return now


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", type=provider_id, metavar="ID")
    parser.add_argument("--wait", action="store_true",
                        help="Wait up to 150 seconds for an active refresh before collecting.")
    parser.add_argument("--scheduled", action="store_true",
                        help="A launchd run: wait a random time up to --jitter seconds first, skip if another "
                             "refresh is running, and request widget reloads on the throttled schedule.")
    parser.add_argument("--jitter", type=float, default=5.0, metavar="SECONDS",
                        help="Longest random wait before a --scheduled run (default 5).")
    parser.add_argument("--requested-at", type=epoch_seconds, metavar="EPOCH",
                        help="When the person asked for this refresh, if earlier than now (default now).")
    parser.add_argument("--support-dir", type=Path,
                        default=Path.home() / "Library/Application Support/Bars")
    args = parser.parse_args()
    if args.scheduled and args.wait:
        parser.error("--scheduled and --wait cannot be combined")
    if args.scheduled and args.requested_at is not None:
        parser.error("--scheduled and --requested-at cannot be combined")
    if not 0 <= args.jitter <= 30:
        parser.error("--jitter must be between 0 and 30 seconds")
    directory = args.support_dir.expanduser()
    # Taken before the jitter and any wait for the lock: a run that collects a provider
    # meanwhile answers this request for it. The app passes the time of a request it queued
    # behind an active refresh, which that refresh may already have answered.
    requested_at = request_time(args.requested_at, int(time.time()))
    os.umask(0o077)
    if args.scheduled:
        jitter_delay(args.jitter)
    # A failed log write must never print a traceback, which launchd copies into
    # logs/launchd.log; the refresh itself continues.
    logging.raiseExceptions = False
    try:
        private_directory(directory)
        logs = private_directory(directory / "logs")
        logger = logging.getLogger("bars.refresh")
        logger.setLevel(logging.INFO)
        handler = PrivateRotatingFileHandler(logs / "refresh.log", maxBytes=LOG_MAX_BYTES, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        with os.fdopen(open_private(directory / "refresh.lock", os.O_RDWR | os.O_CREAT), "r+b") as lock:
            if not acquire_refresh_lock(lock, wait=args.wait, logger=logger):
                return 1 if args.wait else 0
            # Read after acquiring the lock: the app can edit settings while we wait.
            config = read_config(directory / "installation.json")
            return collect_and_publish(config, directory, args.provider, logger, explicit=not args.scheduled,
                                       requested_at=None if args.scheduled else requested_at)
    except Exception as error:
        # Exception bodies may contain local credential paths or subprocess output,
        # and launchd copies stderr into logs/launchd.log, so report only the type.
        print(f"Bars refresh failed ({type(error).__name__}). See the local refresh log.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
