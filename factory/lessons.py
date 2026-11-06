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


def _spec(cfg, rows, sig, window):
    sentence = " ".join(_what(rows[-1]).split())[:300]
    dates = ", ".join(sorted({_time(row["at"]).date().isoformat() for row in rows}))
    evidence = []
    for row in reversed(rows):
        paths = row.get("evidence", [])
        for path in ([paths] if isinstance(paths, str) else paths if isinstance(paths, list) else []):
            if isinstance(path, str) and path.strip() and path not in evidence:
                evidence.append(path)
    evidence = (evidence or [str(cfg.state_dir / "lessons.jsonl")])[:3]
    return {"kind": "choice", "key": _decision_key(cfg, rows), "lessonSignature": sig,
            "title": "How should this recurring factory failure be handled?",
            "question": "Fix this now, accept the current behavior, or ignore this recurrence?",
            "summary": f"{sentence}. {len(rows)} occurrences within {window} days. Dates: {dates}. Evidence: " + ", ".join(evidence),
            "evidence": evidence, "recommended": "fix-now",
            "recommendReason": "The same failure keeps recurring; address its cause.",
            "options": [{"id": "fix-now", "label": "Fix now", "detail": "Prioritize work to fix the cause."},
                        {"id": "accept", "label": "Accept", "detail": "Record acceptance of the current behavior."},
                        {"id": "ignore", "label": "Ignore", "detail": "Take no action for this recurrence."}]}


def append_lesson(cfg, row, *, now=None, opener=None):
    """Append once and escalate at configured recurrence thresholds (default 3/14/7).

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
        if len(recent) < count:
            return result
        status = _decision_status(cfg, sig, now, cooldown)
        if status:
            return dict(result, decision=status)
        spec_path = directory / "lesson-decisions" / ("lesson-" + sig + ".json")
        error_path = spec_path.with_suffix(".error.json")
        try:
            write_json(spec_path, _spec(cfg, recent, sig, window))
            (opener or open_request)(cfg, spec_path)
        except Exception as error:
            write_json(error_path, {"at": now.isoformat(), "signature": sig,
                                    "error": type(error).__name__ + ": " + str(error)[:300]})
            raise
        error_path.unlink(missing_ok=True)
        return dict(result, decision="opened")
