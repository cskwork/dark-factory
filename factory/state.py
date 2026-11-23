"""Atomic local state and serialized, key-level updates."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .locking import acquire, release


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
        acquire(stream)
        try:
            yield
        finally:
            release(stream)


def _target(directory, filename):
    directory = Path(directory).resolve()
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory) or path == directory:
        raise ValueError("state filename must stay within state directory")
    return path


# A queue note grows by appending " | " + entry. Every agent rereads queue.json, so a note over
# these limits moves its oldest entries to state_dir/notes/<KEY>.md (appended, never deleted).
NOTE_SEPARATOR = " | "
NOTE_MAX_LINES = 20
NOTE_MAX_CHARS = 4000
NOTE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def note_marker(key):
    return f"(earlier notes: notes/{key}.md){NOTE_SEPARATOR}"


def _cap_note(directory, key, row, previous):
    """Keep a changed note within the limits, newest entry last; call under the state lock.

    A writer that prepended its entry (new + " | " + old) is reordered to old + " | " + new
    first, so trimming the front never drops the newest entry. The overflow is appended to
    notes/<KEY>.md before the queue is written."""
    note = row.get("note")
    old = previous.get("note") if isinstance(previous, dict) else None
    if not isinstance(note, str) or note == old:
        return
    if isinstance(old, str) and old.strip() and note.endswith(old) and not note.startswith(old):
        added = note[:-len(old)].rstrip(" |\n")
        if added:
            note = old + NOTE_SEPARATOR + added
    row["note"] = note
    if (len(note) <= NOTE_MAX_CHARS and note.count("\n") < NOTE_MAX_LINES) or not NOTE_KEY.fullmatch(str(key)):
        return
    marker = note_marker(key)
    body = note
    while body.startswith(marker):
        body = body[len(marker):]
    lines = body.split("\n")
    start = len("\n".join(lines[:-NOTE_MAX_LINES])) + 1 if len(lines) > NOTE_MAX_LINES else 0
    budget = NOTE_MAX_CHARS - len(marker)
    if len(body) - start > budget:
        cut = len(body) - budget
        # Cut after the next entry boundary so no entry is split.
        boundaries = [found + len(mark) for mark in (NOTE_SEPARATOR, "\n")
                      for found in [body.find(mark, cut)] if found != -1]
        start = min(boundaries) if boundaries else cut
    overflow, kept = body[:start].rstrip(" |\n"), body[start:].lstrip(" |\n")
    if overflow:
        path = _target(directory, "notes/" + key + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(f"## {datetime.now(timezone.utc).isoformat(timespec='seconds')} moved from the queue note\n\n"
                      f"{overflow}\n\n")
            out.flush()
            os.fsync(out.fileno())
    row["note"] = marker + kept


def _stage(row):
    return row.get("stage") if isinstance(row, dict) else None


class _DeliveryConflict(ValueError):
    """The delivery log no longer matches the pending append."""


def _finish_delivery(log, pending, queue):
    try:
        journal = json.loads(pending.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    rows = journal["rows"]
    if all(_stage(queue.get(row["key"])) == row["to"] for row in rows):
        # The queue was replaced. Complete this transaction's tail if the append
        # was interrupted, including the case where it completed before unlink.
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
        with log.open("a+b") as stream:
            if stream.tell() < journal["offset"]:
                raise _DeliveryConflict("delivery log was shortened during a queue transaction")
            stream.seek(journal["offset"])
            existing = stream.read()
            if not payload.startswith(existing):
                raise _DeliveryConflict("delivery log changed during a queue transaction")
            stream.write(payload[len(existing):])
            stream.flush()
            os.fsync(stream.fileno())
    pending.unlink()


def transaction(directory, filename, callback):
    """Apply a mutation to the latest object and recover any interrupted queue journal append."""
    directory = Path(directory)
    target = _target(directory, filename)
    with locked(directory / ".state.lock"):
        data = read_object(target)
        queue = target == _target(directory, "queue.json")
        if queue:
            try:
                (directory / ".delivery-pending.json").lstat()
            except FileNotFoundError:
                pass
            else:
                log = _target(directory, "delivery.jsonl")
                pending = _target(directory, ".delivery-pending.json")
                try:
                    _finish_delivery(log, pending, data)
                except _DeliveryConflict as error:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    conflict = pending.with_name(f".delivery-pending.conflict-{stamp}.json")
                    pending.rename(conflict)
                    print(f"{error}; moved pending append to {conflict.name}", file=sys.stderr)
        before = json.loads(json.dumps(data))
        callback(data)
        if queue:
            rows = []
            for key, row in data.items():
                previous = before.pop(key, None)
                if row == previous:
                    continue
                old_stage, new_stage = _stage(previous), _stage(row)
                if old_stage != new_stage:
                    rows.append({"key": key, "from": old_stage, "to": new_stage})
                if isinstance(row, dict):
                    _cap_note(directory, key, row, previous)
                    if not row.get("createdAt"):
                        row["createdAt"] = datetime.now(timezone.utc).isoformat()
                    if (isinstance(previous, dict) and "deferCount" in row
                            and item_digest(row) != item_digest(previous)):
                        row["deferCount"] = 0
            rows.extend({"key": key, "from": _stage(row), "to": None}
                        for key, row in before.items() if _stage(row) is not None)
            if rows:
                at = datetime.now(timezone.utc).isoformat()
                for row in rows:
                    row["at"] = at
                log = _target(directory, "delivery.jsonl")
                pending = _target(directory, ".delivery-pending.json")
                offset = log.stat().st_size if log.exists() else 0
                write_json(pending, {"offset": offset, "rows": rows})
        write_json(target, data)
        if queue and rows:
            try:
                _finish_delivery(log, pending, data)
            except (OSError, _DeliveryConflict) as error:
                print(f"queue committed; delivery append pending: {error}", file=sys.stderr)
        return data


# A human-risk wait either asks (question, suggest: the judge may answer it) or names a step
# only a person can do (action, runSheet: shown as a to-do, never judged). One form replaces the other.
HUMAN_ASK = ("question", "suggest")
HUMAN_ACTION = ("action", "runSheet")


def patch(directory, filename, path, values, delete=()):
    """Merge named fields without replacing sibling items or nested objects."""
    if (not isinstance(path, list) or not all(isinstance(key, str) for key in path)
            or not isinstance(values, dict) or not isinstance(delete, (list, tuple))
            or not all(isinstance(key, str) for key in delete)):
        raise ValueError("path/delete must contain strings and values must be an object")
    queue = _target(directory, filename) == _target(directory, "queue.json")
    if queue and "action" in values and set(HUMAN_ASK) & set(values):
        raise ValueError("action (a step only a person can do) excludes question and suggest")

    def apply(data):
        node = data
        for key in path:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError("patch path must contain objects")
        node.update(values)
        if queue:
            for name in (HUMAN_ASK if "action" in values else HUMAN_ACTION if set(HUMAN_ASK) & set(values) else ()):
                node.pop(name, None)
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
    from .config import StateDirectoryError, load_config
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("patch", parents=[common])
    change.add_argument("filename")
    change.add_argument("--path", type=json.loads, default=[])
    change.add_argument("--json", type=json.loads, required=True, dest="values")
    change.add_argument("--delete", type=json.loads, default=[])
    change.add_argument("--action", help="human-risk: one line naming a step only a person can do; "
                                          "excludes question and suggest")
    change.add_argument("--run-sheet", dest="runSheet", help="with --action: the path of the steps the person follows")
    append = commands.add_parser("append-pending", parents=[common])
    content = append.add_mutually_exclusive_group(required=True)
    content.add_argument("--line")
    content.add_argument("--json", type=json.loads, dest="row")
    consume = commands.add_parser("consume-pending", parents=[common])
    consume.add_argument("--lines", type=json.loads, required=True)
    lesson = commands.add_parser("lesson", parents=[common])
    lesson.add_argument("--json", type=json.loads, required=True, dest="row")
    fixed = commands.add_parser("lesson-fixed", parents=[common],
                                help="record the commit that fixed a fix-now lesson and close its to-do item")
    fixed.add_argument("signature", help="lesson signature (at least 8 leading hex characters)")
    fixed.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(getattr(args, "config", None))
        directory = cfg.state_dir
        if args.command == "patch":
            if not isinstance(args.values, dict):
                raise ValueError("--json must be an object")
            if args.runSheet is not None and args.action is None:
                raise ValueError("--run-sheet needs --action")
            args.values.update({name: getattr(args, name) for name in HUMAN_ACTION
                                if getattr(args, name) is not None})
            print(json.dumps(patch(directory, args.filename, args.path, args.values, args.delete)))
        elif args.command == "append-pending":
            if args.row is not None and not isinstance(args.row, dict):
                raise ValueError("pending focus JSON must be an object")
            append_pending(directory, args.line if args.line is not None else json.dumps(args.row))
        elif args.command == "lesson":
            from .lessons import append_lesson
            if not isinstance(args.row, dict):
                raise ValueError("lesson JSON must be an object")
            from .lessons import validate_class
            # A class is required only where the operator configured [lessons] classes.
            validate_class(cfg, args.row, required=cfg.lesson_classes()[1])
            print(json.dumps(append_lesson(cfg, args.row)))
        elif args.command == "lesson-fixed":
            from .lessons import mark_fixed
            print(json.dumps(mark_fixed(cfg, args.signature, args.commit)))
        else:
            print(consume_pending(directory, args.lines))
        return 0
    except StateDirectoryError as error:
        print("state: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError) as error:
        detail = str(error) if str(error).startswith("lesson class") else "invalid state or arguments"
        print("state: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
