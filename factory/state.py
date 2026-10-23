"""Atomic local state and serialized, key-level updates."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


BOOKKEEPING = {"deferredAt", "deferReason", "deferCount", "updatedAt", "createdAt",
               "backoffUntil", "backoffLevel"}


def item_digest(item):
    content = {key: value for key, value in item.items() if key not in BOOKKEEPING}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_object(path):
    path = Path(path)
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False) + "\n")


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", suffix=".tmp", delete=False) as stream:
            scratch = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch, path)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _target(directory, filename):
    directory = Path(directory).resolve()
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory) or path == directory:
        raise ValueError("state filename must stay within state directory")
    return path


def transaction(directory, filename, callback):
    """Apply a mutation to the latest object; failures leave the original intact."""
    directory = Path(directory)
    target = _target(directory, filename)
    with locked(directory / ".state.lock"):
        data = read_object(target)
        before = json.loads(json.dumps(data))
        callback(data)
        if target == _target(directory, "queue.json"):
            for key, row in data.items():
                previous = before.get(key)
                if isinstance(row, dict) and row != previous:
                    if not row.get("createdAt"):
                        row["createdAt"] = datetime.now(timezone.utc).isoformat()
                    if (isinstance(previous, dict) and "deferCount" in row
                            and item_digest(row) != item_digest(previous)):
                        row["deferCount"] = 0
        write_json(target, data)
        return data


def patch(directory, filename, path, values, delete=()):
    """Merge named fields without replacing sibling items or nested objects."""
    if (not isinstance(path, list) or not all(isinstance(key, str) for key in path)
            or not isinstance(values, dict) or not isinstance(delete, (list, tuple))
            or not all(isinstance(key, str) for key in delete)):
        raise ValueError("path/delete must contain strings and values must be an object")

    def apply(data):
        node = data
        for key in path:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError("patch path must contain objects")
        node.update(values)
        for key in delete:
            node.pop(key, None)

    return transaction(directory, filename, apply)


def append_pending(directory, line):
    if not isinstance(line, str) or not line.strip() or "\n" in line or "\r" in line:
        raise ValueError("pending focus must be one nonempty line")
    with locked(Path(directory) / ".state.lock"):
        with (Path(directory) / "pending-focus.jsonl").open("a", encoding="utf-8") as out:
            out.write(line + "\n")
            out.flush()
            os.fsync(out.fileno())


def consume_pending(directory, lines):
    """Consume exact occurrences, preserving requests appended after the snapshot."""
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ValueError("consumed lines must be an array of strings")
    requested = Counter(line.rstrip("\r\n") for line in lines)
    with locked(Path(directory) / ".state.lock"):
        target = Path(directory) / "pending-focus.jsonl"
        current = target.read_text(encoding="utf-8").splitlines(keepends=True) if target.exists() else []
        kept = []
        for line in current:
            key = line.rstrip("\r\n")
            if requested[key]:
                requested[key] -= 1
            else:
                kept.append(line)
        write_text(target, "".join(kept))
        return len(current) - len(kept)


def append_json(path, value):
    path = Path(path)
    with locked(path.with_suffix(path.suffix + ".lock")):
        with path.open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def main(argv=None):
    from .config import load_config
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("patch", parents=[common])
    change.add_argument("filename")
    change.add_argument("--path", type=json.loads, default=[])
    change.add_argument("--json", type=json.loads, required=True, dest="values")
    change.add_argument("--delete", type=json.loads, default=[])
    append = commands.add_parser("append-pending", parents=[common])
    content = append.add_mutually_exclusive_group(required=True)
    content.add_argument("--line")
    content.add_argument("--json", type=json.loads, dest="row")
    consume = commands.add_parser("consume-pending", parents=[common])
    consume.add_argument("--lines", type=json.loads, required=True)
    args = parser.parse_args(argv)
    try:
        directory = load_config(getattr(args, "config", None)).state_dir
        if args.command == "patch":
            print(json.dumps(patch(directory, args.filename, args.path, args.values, args.delete)))
        elif args.command == "append-pending":
            if args.row is not None and not isinstance(args.row, dict):
                raise ValueError("pending focus JSON must be an object")
            append_pending(directory, args.line if args.line is not None else json.dumps(args.row))
        else:
            print(consume_pending(directory, args.lines))
        return 0
    except (OSError, ValueError, TypeError):
        print("state: invalid state or arguments", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
