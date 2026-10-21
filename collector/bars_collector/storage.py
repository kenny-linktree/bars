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


def read_private(path, limit):
    """Up to `limit` bytes of a regular file, never through a symlink planted at `path`.

    Raises FileNotFoundError when it is missing and ValueError when it is not a regular
    file or holds more than `limit` bytes.
    """
    # Like the native publisher, never read through a symlink planted at the path.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("Not a regular file.")
        content = file.read(limit + 1)
    if len(content) > limit:
        raise ValueError("File too large.")
    return content


def load_snapshot(output):
    try:
        content = read_private(output, 1024 * 1024)
    except FileNotFoundError:
        return empty_snapshot()
    # A version 1 cache keeps its retained values; the runner then reconciles it with
    # the discovered plugins before anything is written.
    return validate_snapshot(upgrade_snapshot(json.loads(content)))


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
    write_private(output, json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")


def write_private(path, text):
    """Atomically replace `path` with `text` in a mode-0600 file, flushed to disk."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
