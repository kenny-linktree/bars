"""Parallel, deadline-bounded workers with publication after every completion.

Each provider's plugin runs in its own spawned process and process group. The
coordinator never imports plugin code; it receives a bounded JSON message, validates
it and applies only the fields a result may carry. A plugin that hangs, crashes,
prints, exits early or reports nonsense therefore costs its own provider one attempt
and nothing else.
"""

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

from .model import MAX_SNAPSHOT_BYTES, CollectionError, apply_enabled, encoded_size, now, reconcile, validate_result
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
        return {"status": error.status, "message": safe_message(error)}
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


def collect_with_retries(collector, deadline, delays=None):
    """Call the plugin until it succeeds or reports a non-transient status, backing off
    exponentially between transient failures, until the monotonic `deadline`. The caller's
    process deadline still bounds a single slow attempt."""
    value = attempt(collector)
    if value["status"] not in RETRYABLE:
        return value
    for _ in (retry_delays(deadline) if delays is None else delays):
        value = attempt(collector)
        if value["status"] not in RETRYABLE:
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


def apply_result(snapshot, pid, value):
    previous = next(p for p in snapshot["providers"] if p["id"] == pid)
    try:
        validate_result(value)
    except (ValueError, CollectionError, KeyError, TypeError):
        value = dict(status="error", message=INVALID_MESSAGE, last_attempt_at=now())
    if value["status"] == "ok":
        retained = dict(previous)
        previous.update(value)
        # Every reader refuses a snapshot over the size bound, so a result that would push
        # the whole file past it is refused instead, keeping the file readable.
        if encoded_size(snapshot) > MAX_SNAPSHOT_BYTES:
            previous.clear()
            previous.update(retained)
            value = dict(status="error", message=OVERSIZE_MESSAGE, last_attempt_at=value["last_attempt_at"])
    if value["status"] != "ok":
        # A failed attempt cannot replace the last successful period or timestamp.
        previous.update({k: value[k] for k in ("status", "message", "last_attempt_at")})
    snapshot["generated_at"] = now()


def unloadable_result(plugin):
    return dict(status="error", message=plugin.error, last_attempt_at=now())


def run(output, selected, timeout=DEFAULT_TIMEOUT, results=None, disabled=(), plugins=()):
    """Collect `selected` providers from `plugins`, skipping any in `disabled`.

    Every run rebuilds the provider list from the discovered plugins and records each
    provider's enabled state. A disabled provider's worker never starts and its other
    fields stay exactly as retained. Unknown ids in `selected` are ignored.
    """
    disabled = frozenset(disabled)
    by_id = {plugin.id: plugin for plugin in plugins}
    selected = [pid for pid in selected if pid not in disabled and pid in by_id]
    with writer_lock(output):
        # A first run publishes the provider list even when nothing collects, so the
        # readers can show which plugins exist (or that none do).
        changed = not os.path.lexists(output)
        snapshot = load_snapshot(output)
        changed = reconcile(snapshot, plugins) or changed
        if apply_enabled(snapshot, disabled) or changed:
            snapshot["generated_at"] = now()
            write_snapshot(output, snapshot)
        if not selected:
            return snapshot
        for pid in [pid for pid in selected if not by_id[pid].loadable]:
            apply_result(snapshot, pid, unloadable_result(by_id[pid]))
            write_snapshot(output, snapshot)
        workers = [pid for pid in selected if by_id[pid].loadable]
        if results is None:
            if not workers:
                return snapshot
            results = collect_results(workers, timeout, {pid: by_id[pid] for pid in workers})
        try:
            for pid, value in results:
                if pid in by_id:
                    apply_result(snapshot, pid, value)
                    write_snapshot(output, snapshot)
        finally:
            close = getattr(results, "close", None)
            if close:
                close()
    return snapshot
