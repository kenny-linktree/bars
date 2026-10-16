"""Single-writer locking and private atomic publication."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from .model import MAX_SNAPSHOT_BYTES, SCHEMA_VERSION, encoded_size, now, upgrade_snapshot, validate_snapshot


class AlreadyRunning(Exception):
    pass


@contextmanager
def writer_lock(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = output.parent / (output.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning() from None
        yield
    finally:
        os.close(descriptor)


def load_snapshot(output):
    try:
        # Like the native publisher, never read through a symlink planted at the output.
        descriptor = os.open(output, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("Snapshot is not a regular file.")
            content = file.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Snapshot too large.")
        # A version 1 cache keeps its retained values; the runner then reconciles it with
        # the discovered plugins before anything is written.
        return validate_snapshot(upgrade_snapshot(json.loads(content)))
    except FileNotFoundError:
        return empty_snapshot()


def empty_snapshot():
    return dict(schema_version=SCHEMA_VERSION, generated_at=now(), providers=[])


class SnapshotTooLarge(ValueError):
    """The snapshot would exceed the bound every reader enforces; nothing was written."""


def write_snapshot(output, snapshot):
    validate_snapshot(snapshot)
    # The last line of defense: whatever produced it, a file over the bound is unreadable
    # to every reader, so it is never the one that replaces a readable file.
    if encoded_size(snapshot) > MAX_SNAPSHOT_BYTES:
        raise SnapshotTooLarge("Snapshot too large.")
    output = Path(output)
    descriptor, temporary = tempfile.mkstemp(prefix="." + output.name + ".", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(snapshot, file, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
