"""One append-only lesson writer and recurrence escalation through human decisions."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .state import locked, read_object, write_json


def _what(row):
    return row.get("what", row.get("observation"))


def signature(row):
    """Only kind and normalized what identify recurrence; retain evidence separately."""
    what = _what(row)
    what = re.sub(r"\b\d{4}-\d\d-\d\d(?:[T ][\d:.]+(?:Z|[+-]\d\d:\d\d)?)?", " ", what)
    what = re.sub(r"\b[A-Z][A-Z0-9]*-\d+\b", " ", what, flags=re.I)
    what = re.sub(r"\b(?:comment[_-]?ids?|teams|todo)\s*[:=]\s*[\w:-]+", " ", what, flags=re.I)
    what = re.sub(r"\b[0-9a-f]{7,64}\b", " ", what, flags=re.I)
    what = re.sub(r"\d+", " ", what)
    what = " ".join(re.sub(r"[^\w]+", " ", what).lower().split())
    return hashlib.sha256((row["kind"].strip().lower() + ":" + what).encode()).hexdigest()


def _time(value):
    if isinstance(value, datetime):
        parsed = value
    elif type(value) in (int, float):
        parsed = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("lesson timestamp must be an ISO string or epoch seconds")
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def open_request(cfg, path):
    """Use the existing CLI so surface selection and answer authority remain shared."""
    spec = json.loads(path.read_text())
    subprocess.run([str(Path(__file__).resolve().parents[1] / "bin/decision-request"),
                    "--config", str(cfg.path), "choice", spec["key"], "--spec", str(path), "--open"],
                   cwd=cfg.project_root, capture_output=True, text=True, check=True, timeout=120)


def _decision_status(cfg, sig, now, cooldown):
    """Request snapshots and audited human answers are the existing decision registry."""
    answers = {row["decision_id"]: _time(row["at"]) for row in _rows(cfg.state_dir / "decisions.jsonl")
               if isinstance(row, dict) and row.get("event") == "decision"}
    todo = read_object(cfg.state_dir / "decision-pages/todo.json")
    for path in sorted((cfg.state_dir / "decisions").glob("*.request.json")):
        request = read_object(path)
        if request.get("lessonSignature") != sig:
            continue
        identity = request["decision_id"]
        if identity in answers:
            if now - answers[identity] < cooldown:
                return "answer-cooldown"
        elif todo.get(identity, {}).get("state") not in {"failed", "closed", "held"}:
            # A claim without an audit row can be a decision currently being recorded.
            return "already-open"
    return None


def _decision_key(cfg, rows):
    for row in reversed(rows):
        if cfg.valid_key(row.get("key")):
            return row["key"]
        for match in re.finditer(cfg.get("key_pattern", r"[A-Z][A-Z0-9]+-\d+"), _what(row)):
            if cfg.valid_key(match[0]):
                return match[0]
    if cfg.valid_key("FACTORY-0"):
        return "FACTORY-0"
    for key in read_object(cfg.state_dir / "queue.json"):
        if cfg.valid_key(key):
            return key
    raise ValueError("recurring lesson needs a decision key matching key_pattern")


def _facts(cfg, rows):
    """The latest failure sentence, the dates and up to three evidence paths."""
    sentence = " ".join(_what(rows[-1]).split())[:300]
    dates = ", ".join(sorted({_time(row["at"]).date().isoformat() for row in rows}))
    evidence = []
    for row in reversed(rows):
        paths = row.get("evidence", [])
        for path in ([paths] if isinstance(paths, str) else paths if isinstance(paths, list) else []):
            if isinstance(path, str) and path.strip() and path not in evidence:
                evidence.append(path)
    return sentence, dates, (evidence or [str(cfg.state_dir / "lessons.jsonl")])[:3]


def _spec(cfg, rows, sig, window, fixed=None):
    sentence, dates, evidence = _facts(cfg, rows)
    if fixed is not None:
        # A failure closed as fixed came back: ask at once instead of waiting for the threshold.
        how = f"commit {fixed['commit']}" if fixed.get("commit") else "to-do item closed by a person"
        return dict(_spec(cfg, rows, sig, window),
                    title="A recurring factory failure that was fixed happened again. How should it be handled?",
                    summary=f"The fix ({how}, {fixed['closedAt'][:10]}) did not hold. {sentence}. "
                            f"Dates: {dates}. Evidence: " + ", ".join(evidence),
                    recommendReason="A failure closed as fixed recurred; its cause needs another fix.")
    return {"kind": "choice", "key": _decision_key(cfg, rows), "lessonSignature": sig,
            "title": "How should this recurring factory failure be handled?",
            "question": "Fix this now, accept the current behavior, or ignore this recurrence?",
            "summary": f"{sentence}. {len(rows)} occurrences within {window} days. Dates: {dates}. Evidence: " + ", ".join(evidence),
            "evidence": evidence, "recommended": "fix-now",
            "recommendReason": "The same failure keeps recurring; address its cause.",
            "options": [{"id": "fix-now", "label": "Fix now", "detail": "Prioritize work to fix the cause."},
                        {"id": "accept", "label": "Accept", "detail": "Record acceptance of the current behavior."},
                        {"id": "ignore", "label": "Ignore", "detail": "Take no action for this recurrence."}]}


def append_lesson(cfg, row, *, now=None, opener=None, todo=None):
    """Append once and escalate at configured recurrence thresholds (default 3/14/7).

    A `fix-now` answer becomes one tracked `[factory fix]` to-do item per signature
    (see _track). While it is open, a recurrence adds at most one note per day to it
    instead of a decision; once closed (by a person or `lesson-fixed`), the next
    recurrence opens a "the fix did not hold" decision without waiting for the
    threshold or the cooldown.

    One lock serializes append, threshold checks and opening across processes and
    threads. Legacy observation/epoch rows count without rewriting history. Opening
    failures propagate after persisting both the lesson and a diagnostic artifact.
    """
    now = _time(datetime.now(timezone.utc) if now is None else now)
    if (not isinstance(row, dict) or not isinstance(row.get("kind"), str) or not row["kind"].strip()
            or not isinstance(_what(row), str) or not _what(row).strip()):
        raise ValueError("lesson needs nonempty kind and what (or observation)")
    row = dict(row)
    row.setdefault("what", _what(row))
    row.setdefault("at", now.isoformat())
    _time(row["at"])
    sig = row["signature"] = signature(row)
    settings = cfg.get("lessons", {})
    count = settings.get("escalate_count", 3)
    window = settings.get("window_days", 14)
    cooldown = timedelta(days=settings.get("cooldown_days", 7))
    directory = cfg.state_dir
    with locked(directory / ".lessons.lock"):
        path = directory / "lessons.jsonl"
        historical = _rows(path)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        recent = []
        for previous in [*historical, row]:
            if (not isinstance(previous, dict) or not isinstance(previous.get("kind"), str)
                    or not isinstance(_what(previous), str)):
                continue
            try:
                stamp = _time(previous.get("at"))
            except (ValueError, TypeError, OverflowError):
                continue
            if now - timedelta(days=window) <= stamp <= now and signature(previous) == sig:
                recent.append(previous)
        result = {"signature": sig, "count": len(recent), "decision": "not-needed"}
        todo = _todo(cfg) if todo is None else todo
        try:
            # A to-do failure must not lose the lesson or stop its caller (the guard reclaims locks
            # here): it is recorded in the result and fixes.error.json and retried next time.
            fixes = _track(cfg, todo)
            fix = fixes.get(sig)
            if fix and fix["state"] == "open" and fix.get("todoId") and todo is not None:
                if not any(item.get("id") == fix["todoId"] and item.get("status") != "done"
                           for item in todo(["list", "--json"]) or []):
                    fix.update(state="closed", closedAt=now.isoformat())  # A person closed the item.
                    write_json(directory / FIXES, fixes)
            if fix and fix["state"] == "open" and fix.get("notedDay") != now.date().isoformat():
                after = [r for r in recent if _time(r["at"]) > _time(fix["decidedAt"])] or [row]
                if fix.get("todoId") and todo is not None:
                    latest = max(_time(r["at"]) for r in after).date().isoformat()
                    todo(["note", fix["todoId"], f"Recurred {len(after)} times after fix-now, latest {latest}"])
                fix["notedDay"] = now.date().isoformat()
                write_json(directory / FIXES, fixes)
            (directory / FIXES).with_suffix(".error.json").unlink(missing_ok=True)
        except Exception as error:
            result["fixTrackError"] = type(error).__name__ + ": " + str(error)[:300]
            write_json((directory / FIXES).with_suffix(".error.json"),
                       {"at": now.isoformat(), "signature": sig, "error": result["fixTrackError"]})
            fix = read_object(directory / FIXES).get(sig)
        if fix and fix["state"] == "open":
            return dict(result, decision="fix-tracked")
        fixed = fix if fix and _time(row["at"]) >= _time(fix["closedAt"]) and not _asked_since(
            cfg, sig, _time(fix["closedAt"])) else None
        if fixed is None:
            if len(recent) < count:
                return result
            status = _decision_status(cfg, sig, now, cooldown)
            if status:
                return dict(result, decision=status)
        spec_path = directory / "lesson-decisions" / ("lesson-" + sig + ".json")
        error_path = spec_path.with_suffix(".error.json")
        try:
            write_json(spec_path, _spec(cfg, recent or [row], sig, window, fixed=fixed))
            (opener or open_request)(cfg, spec_path)
        except Exception as error:
            write_json(error_path, {"at": now.isoformat(), "signature": sig,
                                    "error": type(error).__name__ + ": " + str(error)[:300]})
            raise
        error_path.unlink(missing_ok=True)
        return dict(result, decision="opened-fix-failed" if fixed else "opened")


FIXES = "lesson-decisions/fixes.json"
FIX_TITLE = "[factory fix] "


def _todo(cfg):
    """The to-do CLI of a to-do decision surface; html keeps no item (None)."""
    from . import decision_inbox
    return None if decision_inbox.surface(cfg) == "html" else decision_inbox.todo_cli


def _requests(cfg):
    for path in sorted((cfg.state_dir / "decisions").glob("*.request.json")):
        request = read_object(path)
        if isinstance(request.get("lessonSignature"), str):
            yield request


def _asked_since(cfg, sig, since):
    return any(request["lessonSignature"] == sig and _time(request["created_at"]) > since
               for request in _requests(cfg))


def _chosen(cfg):
    """signature -> (decision id, answered at, option) of its latest answered lesson decision."""
    signatures = {request["decision_id"]: request["lessonSignature"] for request in _requests(cfg)}
    latest = {}
    path = cfg.state_dir / "decisions.jsonl"
    for text in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:  # A broken audit line is skipped.
            row = json.loads(text)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("event") != "decision" or row.get("decision_id") not in signatures:
            continue
        sig = signatures[row["decision_id"]]
        if sig not in latest or _time(row["at"]) >= _time(latest[sig][1]):
            latest[sig] = (row["decision_id"], row["at"], row.get("option"))
    return latest


def _untracked(cfg, fixes):
    """fix-now answers without a tracked fix yet: [(signature, decision id, answered at)]."""
    found = []
    for sig, (identity, at, option) in _chosen(cfg).items():
        fix = fixes.get(sig)
        if option == "fix-now" and not (fix and (fix["decision"] == identity or fix["state"] == "open"
                                                 or _time(at) <= _time(fix["closedAt"]))):
            found.append((sig, identity, at))
    return found


def _same(row, sig):
    try:
        _time(row["at"])
        return signature(row) == sig
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def _track(cfg, todo):
    """Record each fix-now answer once and open its to-do item; the caller holds the lesson lock."""
    fixes = read_object(cfg.state_dir / FIXES)
    changed = False
    for sig, identity, at in _untracked(cfg, fixes):
        rows = [row for row in _rows(cfg.state_dir / "lessons.jsonl") if _same(row, sig)]
        if not rows:
            raise ValueError(f"lesson decision {identity} has no lessons for signature {sig}")
        sentence, dates, evidence = _facts(cfg, rows)
        item = None
        if todo is not None:
            item = todo(["add", FIX_TITLE + sentence[:150], "--project", str(cfg.project_root), "--by", "factory"])
            todo(["wait", item, f"Factory fix needed: decision {identity[:8]} chose fix-now on {at[:10]}. Report the "
                                f"fixing commit with `python3 -m factory.state lesson-fixed {sig[:12]} --commit <sha>` "
                                "or close this item."])
            todo(["note", item, f"Signature {sig} · {len(rows)} occurrences · dates: {dates} · evidence: "
                                + ", ".join(evidence)])
        fixes[sig] = {"decision": identity, "decidedAt": at, "todoId": item, "state": "open",
                      "what": sentence, "count": len(rows)}
        changed = True
    if changed:
        write_json(cfg.state_dir / FIXES, fixes)
    return fixes


def track_fixes(cfg, *, todo=None):
    """Called after a decision is recorded. Returns open fixes: signature -> to-do item id (None on html)."""
    with locked(cfg.state_dir / ".lessons.lock"):
        fixes = _track(cfg, _todo(cfg) if todo is None else todo)
    return {sig: fix.get("todoId") for sig, fix in fixes.items() if fix["state"] == "open"}


def mark_fixed(cfg, prefix, commit, *, now=None, todo=None):
    """A factory commit fixed a fix-now lesson: record it and close its item. A later recurrence asks again."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise ValueError("--commit must be a 7-40 character hex commit sha")
    if not re.fullmatch(r"[0-9a-f]{8,64}", prefix or ""):
        raise ValueError("signature must be at least 8 hex characters")
    now = _time(datetime.now(timezone.utc) if now is None else now)
    todo = _todo(cfg) if todo is None else todo
    with locked(cfg.state_dir / ".lessons.lock"):
        fixes = read_object(cfg.state_dir / FIXES)
        found = [sig for sig, fix in fixes.items() if sig.startswith(prefix) and fix["state"] == "open"]
        if len(found) != 1:
            raise ValueError(f"expected one open fix-now lesson for {prefix}, found {len(found)}")
        fix = fixes[found[0]]
        if fix.get("todoId") and todo is not None:
            todo(["done", fix["todoId"], f"Closed by fix commit {commit}"])
        fix.update(state="fixed", commit=commit, closedAt=now.isoformat())
        write_json(cfg.state_dir / FIXES, fixes)
        return {"signature": found[0], "state": "fixed", "commit": commit}
