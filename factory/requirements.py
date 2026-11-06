"""Caller-declared evidence gate; worker results cannot waive declared checks.

An optional event declares commentIds, stateArtifact and optional queueArtifact.
Listed state evidence must contain handled[id] for every declared id, with the
same key, a nonempty action and timestamp. Timestamps may be strings or numeric
epoch seconds, as written by triage. Done events may retain any waiting value
and report the declared stage or the same key's stage in the listed queue
artifact. Non-event completion and all declared verifier checks remain strict.
"""
from copy import deepcopy
import json
import math
from pathlib import Path


def snapshot(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or not {'key', 'stage', 'checks'} <= set(value)
            or set(value) - {'key', 'stage', 'checks', 'event'}
            or not all(isinstance(value.get(k), str) and value[k] for k in ('key', 'stage'))
            or not isinstance(value['checks'], list) or not value['checks']):
        raise ValueError('invalid requirements manifest')
    if 'event' in value:
        event = value['event']
        if (not isinstance(event, dict) or set(event) - {'commentIds', 'stateArtifact', 'queueArtifact'}
                or not isinstance(event.get('commentIds'), list) or not event['commentIds']
                or not all(isinstance(cid, str) and cid.strip() for cid in event['commentIds'])
                or len(set(event['commentIds'])) != len(event['commentIds'])
                or not isinstance(event.get('stateArtifact'), str) or not event['stateArtifact'].strip()
                or 'queueArtifact' in event and (not isinstance(event['queueArtifact'], str)
                                                or not event['queueArtifact'].strip())):
            raise ValueError('invalid event declaration')
    names = set()
    for row in value['checks']:
        if (not isinstance(row, dict) or set(row) != {'name', 'artifact', 'resultField', 'environment'}
                or not all(isinstance(row.get(k), str) and row[k] for k in ('name', 'artifact', 'resultField'))
                or row['name'] in names or not isinstance(row['environment'], dict) or not row['environment']):
            raise ValueError('invalid required check')
        names.add(row['name'])
        for field in [row['resultField'], *row['environment']]:
            if not isinstance(field, str) or not field or any(not part for part in field.split('.')):
                raise ValueError('invalid JSON field binding')
        # Null, nested values and nonfinite numbers cannot establish an environment identity.
        for expected in row['environment'].values():
            if (expected is None or not isinstance(expected, (str, bool, int, float))
                    or isinstance(expected, str) and not expected
                    or isinstance(expected, float) and not math.isfinite(expected)):
                raise ValueError('invalid environment binding')
    return {'manifest': deepcopy(value), 'base': str(path.parent)}


def field(value, selector):
    for part in selector.split('.'):
        if not isinstance(value, dict) or part not in value:
            raise ValueError('missing verifier field')
        value = value[part]
    return value


def evaluate(declaration, value, key=None):
    manifest, base = declaration['manifest'], Path(declaration['base'])
    passed, failures = [], []
    event = manifest.get('event')
    artifacts = value.get('artifacts', []) if isinstance(value, dict) else []
    listed = {(base / p).resolve() for p in artifacts if isinstance(p, str)} if isinstance(artifacts, list) else set()

    def event_artifact(name):
        artifact = (base / event[name]).resolve()
        if artifact not in listed:
            raise ValueError('event artifact not listed')
        return json.loads(artifact.read_text())

    stages = [manifest['stage']]
    if event and event.get('queueArtifact'):
        try:
            queue = event_artifact('queueArtifact')
            row = queue.get(manifest['key']) if isinstance(queue, dict) else None
            queue_stage = row.get('stage') if isinstance(row, dict) else None
            if isinstance(queue_stage, str) and queue_stage:
                stages.append(queue_stage)
        except (OSError, ValueError, TypeError):
            pass  # Unavailable queue evidence cannot grant another stage identity.
    if (not isinstance(value, dict) or value.get('key') != manifest['key']
            or key is not None and key != manifest['key'] or value.get('stage') not in stages):
        failures.append({'name': 'identity', 'reason': 'key-or-stage-mismatch'})
    if (not isinstance(value, dict) or value.get('status') != 'done'
            or not event and value.get('waiting') is not None):
        failures.append({'name': 'completion', 'reason': 'not-complete'})
    if event:
        try:
            handled = field(event_artifact('stateArtifact'), 'handled')
            for cid in event['commentIds']:
                row = handled.get(cid) if isinstance(handled, dict) else None
                at = row.get('at') if isinstance(row, dict) else None
                valid_at = (isinstance(at, str) and at.strip() or type(at) is int
                            or type(at) is float and math.isfinite(at))
                if (not isinstance(row, dict) or row.get('key') != manifest['key']
                        or not isinstance(row.get('action'), str) or not row['action'].strip() or not valid_at):
                    raise ValueError('event not handled')
        except (OSError, ValueError, TypeError):
            failures.append({'name': 'completion', 'reason': 'event-not-handled'})
    checks = value.get('checks', []) if isinstance(value, dict) else []
    if not isinstance(checks, list):
        checks = []
    for row in manifest['checks']:
        artifact = (base / row['artifact']).resolve()
        evidence = {'name': row['name'], 'artifact': str(artifact)}
        matches = [c for c in checks if isinstance(c, dict) and c.get('name') == row['name']]
        reason = None
        if len(matches) != 1:
            reason = 'missing-or-duplicate-check'
        elif matches[0].get('result') != 'PASS':
            reason = 'check-not-pass'
        elif artifact not in listed:
            reason = 'artifact-not-listed'
        else:
            try:
                saved = json.loads(artifact.read_text())
                if field(saved, row['resultField']) != 'PASS':
                    reason = 'verifier-not-pass'
                elif any(type(field(saved, name)) is not type(expected) or field(saved, name) != expected
                         for name, expected in row['environment'].items()):
                    reason = 'environment-mismatch'
            except (OSError, ValueError, TypeError):
                reason = 'unreadable-or-invalid-verifier'
        if reason:
            failures.append({**evidence, 'reason': reason})
        else:
            passed.append(evidence)
    return {'passed': not failures, 'retryAllowed': False if failures else True,
            'key': manifest['key'], 'stage': manifest['stage'], 'passedChecks': passed, 'failures': failures}
