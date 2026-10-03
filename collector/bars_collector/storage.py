"""Single-writer locking and private atomic publication."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from .model import PROVIDERS, empty_provider, now, validate_snapshot


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
        return validate_snapshot(json.loads(content))
    except FileNotFoundError:
        return dict(schema_version=1, generated_at=now(), providers=[empty_provider(p) for p in PROVIDERS])


def write_snapshot(output, snapshot):
    validate_snapshot(snapshot)
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
