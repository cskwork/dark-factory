"""Atomic local state and advisory locks shared by command entry points."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile


def read_object(path):
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            scratch = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        scratch.replace(path)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def append_json(path, value):
    with locked(path.with_suffix(path.suffix + ".lock")):
        with path.open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
