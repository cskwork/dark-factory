"""Human replies are deterministic; uncertain model classifications fail open."""
import json
import math
import subprocess
import time
from .state import append_json, locked, read_object, write_json


CLASSES = ("question", "action_request", "change_request", "decision_request", "information")


def validate_predictions(value, events):
    expected = {(event["key"], event["id"]) for event in events}
    if len(expected) != len(events) or not isinstance(value, dict) or set(value) != {"predictions"}:
        raise ValueError("invalid prediction batch")
    rows = value["predictions"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("incomplete prediction batch")
    predictions = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"key", "id", "class", "needs_response", "confidence", "reason"}:
            raise ValueError("invalid prediction")
        identity = (row["key"], row["id"])
        confidence = row["confidence"]
        if (identity not in expected or identity in predictions or row["class"] not in CLASSES
                or type(row["needs_response"]) is not bool or type(confidence) not in (int, float)
                or not math.isfinite(confidence) or not 0 <= confidence <= 1
                or not isinstance(row["reason"], str) or not 0 < len(row["reason"].strip()) <= 300):
            raise ValueError("invalid prediction fields")
        predictions[identity] = row
    return predictions


def record(cfg, decisions):
    with locked(cfg.state_dir / ".state.lock"):
        state = read_object(cfg.state_dir / "state.json")
        handled = state.setdefault("handled", {})
        if not isinstance(handled, dict):
            raise ValueError("invalid handled state")
        for event, action in decisions:
            identity = event["key"] + ":" + event["id"]
            if identity not in handled:
                row = {"key": event["key"], "id": event["id"], "action": action, "at": time.time()}
                # Evidence first: a failed log write must not hide runnable work.
                append_json(cfg.state_dir / "triage.jsonl", row)
                handled[identity] = row
        write_json(cfg.state_dir / "state.json", state)


def triage(cfg, events, classifier=None):
    remaining = list(events)
    try:
        humans = [event for event in events if event.get("answered_by_me_after") is True]
        if humans:
            record(cfg, [(event, "human") for event in humans])
            remaining = [event for event in remaining if event not in humans]
        handled = read_object(cfg.state_dir / "state.json").get("handled", {})
        if not isinstance(handled, dict):
            raise ValueError("invalid handled state")
        remaining = [event for event in remaining if event["key"] + ":" + event["id"] not in handled]
        settings = cfg.get("triage", {})
        command = settings.get("classifier_cmd")
        if not remaining or (classifier is None and not command):
            return remaining
        if classifier is None:
            def classifier(rows):
                response = subprocess.run(cfg.command(command), input=json.dumps({"events": rows}),
                                          capture_output=True, text=True, cwd=cfg.project_root,
                                          timeout=settings.get("timeout_seconds", 45))
                if response.returncode:
                    raise ValueError("classifier failed")
                return json.loads(response.stdout)
        predictions = validate_predictions(classifier(remaining), remaining)
        mode = settings.get("mode", "shadow")
        if mode not in ("shadow", "enforce"):
            raise ValueError("invalid triage mode")
        for row in predictions.values():
            append_json(cfg.state_dir / "triage-shadow.jsonl", {"at": time.time(), "mode": mode, **row})
        if mode == "shadow":
            return remaining
        skip = [event for event in remaining
                if (row := predictions[(event["key"], event["id"])])["class"] == "information"
                and row["needs_response"] is False
                and row["confidence"] >= settings.get("confidence_threshold", 0.9)]
        record(cfg, [(event, "classified-information") for event in skip])
        return [event for event in remaining if event not in skip]
    except Exception:
        # Captured output and exception strings may contain credentials. Never echo them.
        return remaining
