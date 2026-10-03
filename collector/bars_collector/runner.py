"""Parallel, deadline-bounded workers with publication after every completion."""

import multiprocessing
from multiprocessing.connection import wait
import os
import signal
import threading
import time

from .adapters import COLLECTORS
from .model import CollectionError, apply_enabled, now
from .storage import load_snapshot, write_snapshot, writer_lock


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


def fetch_worker(connection, collector):
    os.setsid()
    # The coordinator blocks these briefly while registering the new worker.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        value = {"status": "ok", "message": None, **collector()}
    except CollectionError as error:
        value = {"status": error.status, "message": str(error)}
    except Exception:
        # Raw exception strings may include tokens, endpoints, IDs or response data.
        value = {"status": "error", "message": "Provider collection failed. Collection will retry later."}
    value["last_attempt_at"] = now()
    if value["status"] == "ok":
        value["fetched_at"] = value["last_attempt_at"]
    try:
        connection.send(value)
    finally:
        connection.close()


def collect_results(selected, timeout=40, collectors=None):
    collectors = COLLECTORS if collectors is None else collectors
    context = multiprocessing.get_context("spawn")
    pending = {}
    owns_signal = threading.current_thread() is threading.main_thread()
    previous_signal = signal.getsignal(signal.SIGTERM) if owns_signal else None
    if owns_signal:
        signal.signal(signal.SIGTERM, cancel_collection)
    try:
        for pid in selected:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=fetch_worker, args=(sender, collectors[pid]), daemon=True)
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
                value = None
                if connection in ready:
                    try:
                        value = connection.recv()
                    except EOFError:
                        pass
                if value is None:
                    value = dict(status="error", message="Provider collection timed out or stopped. Collection will retry later.",
                                 last_attempt_at=now())
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
    if value["status"] == "ok":
        previous.update(value)
    else:
        # A failed attempt cannot replace the last successful period or timestamp.
        previous.update({k: value[k] for k in ("status", "message", "last_attempt_at")})
    snapshot["generated_at"] = now()


def run(output, selected, timeout=40, results=None, disabled=()):
    """Collect `selected` providers, skipping any in `disabled`.

    Every run records all four providers' enabled state from `disabled`. A disabled
    provider's worker never starts and its other fields stay exactly as retained.
    """
    disabled = frozenset(disabled)
    selected = [pid for pid in selected if pid not in disabled]
    with writer_lock(output):
        snapshot = load_snapshot(output)
        if apply_enabled(snapshot, disabled):
            snapshot["generated_at"] = now()
            write_snapshot(output, snapshot)
        if not selected:
            return snapshot
        stream = collect_results(selected, timeout) if results is None else results
        try:
            for pid, value in stream:
                apply_result(snapshot, pid, value)
                write_snapshot(output, snapshot)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
    return snapshot
