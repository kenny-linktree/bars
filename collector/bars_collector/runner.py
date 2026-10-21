"""Parallel, deadline-bounded workers with publication after every completion.

Each provider's plugin runs in its own spawned process and process group. The
coordinator never imports plugin code; it receives a bounded JSON message, validates
it and applies only the fields a result may carry. A plugin that hangs, crashes,
prints, exits early or reports nonsense therefore costs its own provider one attempt
and nothing else.
"""

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from multiprocessing.connection import wait
import os
import random
import signal
import threading
import time

from bars_plugin.access import child_environment
from bars_plugin.values import MAX_TEXT

from . import schedule
from .model import (MAX_SNAPSHOT_BYTES, CollectionError, apply_enabled, encoded_size, is_pausing, now, parse_instant,
                    rate_limit_pause, reconcile, validate_result)
from .storage import load_snapshot, write_snapshot, writer_lock

# A valid result is a few kilobytes; anything near this is a plugin fault.
MAX_RESULT_BYTES = 256 * 1024
FAILURE_MESSAGE = "Provider collection failed. Collection will retry later."
INVALID_MESSAGE = "Provider plugin returned invalid usage data."
OVERSIZE_MESSAGE = "Provider plugin returned more data than the snapshot can hold."
TIMEOUT_MESSAGE = "Provider collection timed out or stopped. Collection will retry later."
# Collection runs once a minute. A provider's worker, including every retry, must finish
# inside that minute so the next run starts fresh; this is the default hard deadline.
DEFAULT_TIMEOUT = 55
# Transient failures are retried inside the worker with exponential backoff: the first
# wait is this long, each later wait doubles, and every wait is jittered by up to half.
# Retries stop when the next wait would pass the deadline. Only `error` statuses retry;
# a missing or rejected login (`setup`, `login_required`) cannot fix itself in seconds.
RETRY_FIRST_DELAY = 1.0
RETRYABLE = frozenset({"error"})
# An explicit refresh names the time the person asked (`requested_at`); a provider whose
# latest attempt finished at or after it, in a run that held the lock meanwhile, already
# answers the request and is not asked again. Older request times are ignored.
MAX_REQUEST_AGE = 10 * 60


class CollectionCancelled(BaseException):
    """SIGTERM requests cleanup before the coordinator exits."""


def cancel_collection(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise CollectionCancelled()


def signal_group(pgid, signum):
    """Signal a worker's process group, tolerating a group with nothing left to signal.

    ProcessLookupError means the group no longer exists. macOS raises PermissionError
    when the group's only member is its exited but not yet reaped leader (a zombie).
    """
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def stop_worker(process):
    """Stop the complete worker group, including CLI children left after success."""
    # A worker's PID also names its session/process group after fetch_worker setsid.
    # Signal the group before join/is_alive can reap its leader.
    signal_group(process.pid, signal.SIGTERM)
    # Also covers cancellation before a newly spawned worker has called setsid.
    if process.is_alive():
        process.terminate()
    process.join(timeout=0.25)
    # The leader may have exited while a child ignored SIGTERM. Always signal the
    # group again, independent of the leader's current state.
    signal_group(process.pid, signal.SIGKILL)
    if process.is_alive():
        process.kill()
    process.join(timeout=0.25)


def safe_message(message):
    """A plugin's own failure text, if it is short and printable; otherwise the fixed text."""
    message = str(message)
    if not message or len(message) > MAX_TEXT or not message.isprintable():
        return FAILURE_MESSAGE
    return message


def silence_standard_streams():
    """Plugin output never reaches the coordinator's status line or the scheduler's log."""
    try:
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)
    except OSError:
        pass


def attempt(collector):
    """One call of the plugin, reduced to a status dictionary. Never raises."""
    try:
        return {"status": "ok", "message": None, **collector()}
    except CollectionError as error:
        value = {"status": error.status, "message": safe_message(error)}
        if error.retry_after is not None:
            value["retry_after"] = error.retry_after
        return value
    except BaseException:
        # Raw exception strings may include tokens, endpoints, IDs or response data.
        return {"status": "error", "message": FAILURE_MESSAGE}


def retry_delays(deadline, first=RETRY_FIRST_DELAY, jitter=random.random, clock=time.monotonic):
    """Yield successive backoff waits (1, 2, 4, ... seconds, each jittered up to +50%) while the
    next wait still ends before the monotonic `deadline`. The generator sleeps itself."""
    delay = first
    while True:
        wait_for = delay * (1 + 0.5 * jitter())
        if clock() + wait_for >= deadline:
            return
        time.sleep(wait_for)
        yield wait_for
        delay *= 2


def retryable(value):
    """Transient failures retry; a rate limit does not, since asking again is what it forbids."""
    return value["status"] in RETRYABLE and "retry_after" not in value


def collect_with_retries(collector, deadline, delays=None):
    """Call the plugin until it succeeds or reports a non-transient status, backing off
    exponentially between transient failures, until the monotonic `deadline`. The caller's
    process deadline still bounds a single slow attempt."""
    value = attempt(collector)
    if not retryable(value):
        return value
    for _ in (retry_delays(deadline) if delays is None else delays):
        value = attempt(collector)
        if not retryable(value):
            break
    return value


def restrict_environment():
    """Plugin code sees only the allowlisted environment, whoever started the collector.

    The scheduled runner already filters; this covers a collector started by hand from a
    shell holding unrelated API keys. `sys.path` is already set, so dropping PYTHONPATH
    here does not affect the plugin's `bars_plugin` import.
    """
    allowed = child_environment()
    os.environ.clear()
    os.environ.update(allowed)


def fetch_worker(connection, collector, budget=0):
    # The retry budget starts when the worker does, so every attempt and wait together
    # stay inside the coordinator's deadline for this worker.
    deadline = time.monotonic() + budget
    os.setsid()
    # The coordinator blocks these briefly while registering the new worker.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
    silence_standard_streams()
    restrict_environment()
    value = collect_with_retries(collector, deadline)
    value["last_attempt_at"] = now()
    if value["status"] == "ok":
        value["fetched_at"] = value["last_attempt_at"]
    try:
        try:
            payload = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            payload = json.dumps({"status": "error", "message": INVALID_MESSAGE,
                                  "last_attempt_at": value["last_attempt_at"]}).encode()
        connection.send_bytes(payload)
    finally:
        connection.close()


def receive(connection):
    """The worker's JSON message; None when the worker sent nothing (it exited or was
    stopped), or an invalid-result failure when it sent something oversized or malformed,
    so the two are told apart in the provider's message."""
    try:
        payload = connection.recv_bytes(MAX_RESULT_BYTES)
    except EOFError:
        return None
    except OSError:
        return dict(status="error", message=INVALID_MESSAGE, last_attempt_at=now())
    try:
        value = json.loads(payload)
    except (ValueError, RecursionError, UnicodeError):
        value = None
    if not isinstance(value, dict):
        return dict(status="error", message=INVALID_MESSAGE, last_attempt_at=now())
    return value


def collect_results(selected, timeout=DEFAULT_TIMEOUT, collectors=None, retry=True):
    """Yield `(pid, value)` as each worker finishes, with a hard deadline per worker.

    `collectors` maps each id to a picklable callable: a `plugins.Plugin`, which imports
    its module only inside the worker, or any module-level function in tests. With
    `retry`, a worker retries transient failures with backoff inside the same deadline.
    """
    context = multiprocessing.get_context("spawn")
    pending = {}
    owns_signal = threading.current_thread() is threading.main_thread()
    previous_signal = signal.getsignal(signal.SIGTERM) if owns_signal else None
    if owns_signal:
        signal.signal(signal.SIGTERM, cancel_collection)
    try:
        for pid in selected:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=fetch_worker, args=(sender, collectors[pid], timeout if retry else 0),
                                      daemon=True)
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
            try:
                process.start()
                pending[receiver] = (pid, process, time.monotonic() + timeout)
            except BaseException:
                receiver.close()
                raise
            finally:
                sender.close()
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        while pending:
            delay = max(0, min(deadline for _, _, deadline in pending.values()) - time.monotonic())
            ready = wait(list(pending), timeout=delay)
            expired = [connection for connection, (_, _, deadline) in pending.items() if deadline <= time.monotonic()]
            for connection in dict.fromkeys(ready + expired):
                pid, process, _ = pending[connection]
                value = receive(connection) if connection in ready else None
                if value is None:
                    value = dict(status="error", message=TIMEOUT_MESSAGE, last_attempt_at=now())
                stop_worker(process)
                connection.close()
                del pending[connection]
                yield pid, value
    finally:
        if owns_signal:
            # Do not let a new TERM interrupt cleanup entered for another reason.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            for connection, (_, process, _) in pending.items():
                stop_worker(process)
                connection.close()
        finally:
            if owns_signal:
                signal.signal(signal.SIGTERM, previous_signal)


def checked_result(value):
    """The worker's value if it is a valid result, otherwise the fixed invalid-result failure."""
    try:
        validate_result(value)
    except (ValueError, CollectionError, KeyError, TypeError):
        value = dict(status="error", message=INVALID_MESSAGE, last_attempt_at=now())
    return value


def apply_result(snapshot, pid, value, pause_until=None):
    """Apply one worker result to its own provider entry. Returns the value applied, which is
    a fixed failure when the result was invalid or would not fit the snapshot.

    A rate limit pauses the provider until `pause_until` when the caller's schedule gives
    one, otherwise for `rate_limit_pause`."""
    previous = next(p for p in snapshot["providers"] if p["id"] == pid)
    value = checked_result(value)
    retained = dict(previous)
    if value["status"] == "ok":
        previous.update(value)
        previous["next_attempt_at"] = None
        # Every reader refuses a snapshot over the size bound, so a result that would push
        # the whole file past it is refused instead, keeping the file readable.
        if encoded_size(snapshot) > MAX_SNAPSHOT_BYTES:
            previous.clear()
            previous.update(retained)
            value = dict(status="error", message=OVERSIZE_MESSAGE, last_attempt_at=value["last_attempt_at"])
    if value["status"] != "ok":
        # A failed attempt cannot replace the last successful period or timestamp.
        previous.update({k: value[k] for k in ("status", "message", "last_attempt_at")})
        next_attempt = None
        if "retry_after" in value:
            # A rate limit pauses this provider's scheduled attempts; see rate_limit_pause.
            if pause_until is None:
                attempted = parse_instant(value["last_attempt_at"])
                pause_until = attempted + timedelta(seconds=rate_limit_pause(retained, value["retry_after"], attempted))
            next_attempt = pause_until.isoformat(timespec="seconds").replace("+00:00", "Z")
        previous["next_attempt_at"] = next_attempt
        # Nor may its message be the bytes that push the file past the bound. The pause
        # outcome still stands: an attempt that answered must not leave a stale pause behind.
        if encoded_size(snapshot) > MAX_SNAPSHOT_BYTES:
            previous.clear()
            previous.update(retained)
            previous["next_attempt_at"] = next_attempt
            if encoded_size(snapshot) > MAX_SNAPSHOT_BYTES:
                previous["next_attempt_at"] = retained.get("next_attempt_at")
    snapshot["generated_at"] = now()
    return value


def outcome(value):
    """A fixed category for an applied result, for the schedule and its diagnostics."""
    if "retry_after" in value:
        return "rate_limited"
    if value["status"] == "error" and value["message"] == TIMEOUT_MESSAGE:
        return "timeout"
    if value["status"] == "error" and value["message"] in (INVALID_MESSAGE, OVERSIZE_MESSAGE):
        return "invalid"
    return value["status"]


def settle(snapshot, plan, pid, value, started, scheduled, report):
    """Apply a result and record it in the schedule. A rate limit's pause is the later of the
    snapshot's escalating pause and the provider's doubled interval, plus a positive jitter."""
    value = checked_result(value)
    if "retry_after" in value:
        entry = next(p for p in snapshot["providers"] if p["id"] == pid)
        attempted = parse_instant(value["last_attempt_at"])
        pause = rate_limit_pause(entry, value["retry_after"], attempted)
        change = plan.record(pid, "rate_limited", started, scheduled=scheduled,
                             attempted=int(attempted.timestamp()), pause=pause)
        value = apply_result(snapshot, pid, value, datetime.fromtimestamp(plan.due(pid), timezone.utc))
    else:
        value = apply_result(snapshot, pid, value)
        change = plan.record(pid, outcome(value), started, scheduled=scheduled)
    report(schedule.attempt_line(pid, outcome(value), plan.interval(pid), plan.due(pid), value.get("retry_after"), change))


def unloadable_result(plugin):
    return dict(status="error", message=plugin.error, last_attempt_at=now())


def skip_reason(provider, plan, at, scheduled, requested_at):
    """Why a run leaves an enabled provider alone, with the time it is next expected, or None.

    Every run, explicit or scheduled, respects a rate-limit pause. A scheduled run also waits
    for the provider's interval. An explicit run skips a provider whose latest attempt
    finished after the person asked, which a run holding the lock meanwhile made.
    """
    moment = datetime.fromtimestamp(at, timezone.utc)
    pause_until = plan.pause_until(provider["id"], at)
    if is_pausing(provider, moment):
        pause_until = max(pause_until or 0, int(parse_instant(provider["next_attempt_at"]).timestamp()))
    if pause_until is not None:
        return "rate_limited", pause_until
    if scheduled and not plan.is_due(provider["id"], at):
        return "not_due", plan.due(provider["id"])
    attempted = parse_instant(provider.get("last_attempt_at"))
    if requested_at is not None and attempted is not None and attempted.timestamp() >= requested_at:
        return "already_attempted", plan.due(provider["id"])
    return None


def run(output, selected, timeout=DEFAULT_TIMEOUT, results=None, disabled=(), plugins=(), scheduled=False,
        requested_at=None, report=None, clock=time.time, draw=random.random):
    """Collect `selected` providers from `plugins`, skipping any in `disabled`.

    Every run rebuilds the provider list from the discovered plugins and records each
    provider's enabled state. A disabled provider's worker never starts and its other
    fields stay exactly as retained. Unknown ids in `selected` are ignored. A provider
    pausing after a rate limit is skipped by every run; a `scheduled` run also skips a
    provider that is not yet due on its own interval (see `schedule`), and an explicit run
    with `requested_at` (epoch seconds) skips one attempted since that time. Skipped
    providers keep every field. `report` receives one diagnostic line per provider.
    """
    disabled = frozenset(disabled)
    by_id = {plugin.id: plugin for plugin in plugins}
    selected = [pid for pid in selected if pid not in disabled and pid in by_id]
    report = report or (lambda line: None)
    with writer_lock(output):
        # A first run publishes the provider list even when nothing collects, so the
        # readers can show which plugins exist (or that none do).
        changed = not os.path.lexists(output)
        snapshot = load_snapshot(output)
        changed = reconcile(snapshot, plugins) or changed
        if apply_enabled(snapshot, disabled) or changed:
            snapshot["generated_at"] = now()
            # Too many plugins for one file is the one change that cannot be trimmed to fit;
            # the previous readable snapshot stays, and the command reports why.
            write_snapshot(output, snapshot)
        state = schedule.schedule_path(output)
        plan, problem = schedule.load(state, draw=draw)
        if problem:
            report(schedule.state_line(problem))
        pruned = plan.prune(by_id)
        started = int(clock())
        if requested_at is not None and not started - MAX_REQUEST_AGE <= requested_at <= started + schedule.DUE_TOLERANCE:
            requested_at = None
        entries = {p["id"]: p for p in snapshot["providers"]}
        due = []
        for pid in selected:
            skipped = skip_reason(entries[pid], plan, started, scheduled, requested_at)
            if skipped:
                report(schedule.skip_line(pid, skipped[0], plan.interval(pid), skipped[1]))
            else:
                due.append(pid)
        selected = due
        saved = True

        def save():
            # Scheduling state is advisory: failing to save it must not lose a result, and
            # a rate-limit pause is still recorded in the snapshot itself.
            nonlocal saved
            if not plan.save(state) and saved:
                saved = False
                report(schedule.state_line("not_saved"))

        if not selected:
            if pruned or problem:
                save()
            return snapshot
        # Saved before any request, so a run that is stopped midway still counts its attempts.
        for pid in selected:
            plan.reserve(pid, started)
        save()
        for pid in [pid for pid in selected if not by_id[pid].loadable]:
            settle(snapshot, plan, pid, unloadable_result(by_id[pid]), started, scheduled, report)
            save()
            write_snapshot(output, snapshot)
        workers = [pid for pid in selected if by_id[pid].loadable]
        if results is None:
            if not workers:
                return snapshot
            results = collect_results(workers, timeout, {pid: by_id[pid] for pid in workers})
        try:
            for pid, value in results:
                if pid in by_id:
                    settle(snapshot, plan, pid, value, started, scheduled, report)
                    # The schedule goes first: a crash between the two writes then errs
                    # toward asking the provider later, never sooner.
                    save()
                    write_snapshot(output, snapshot)
        finally:
            close = getattr(results, "close", None)
            if close:
                close()
    return snapshot
