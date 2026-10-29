"""Caller-declared evidence gate; worker results cannot waive declared checks."""
from copy import deepcopy
import json
import math
from pathlib import Path


def snapshot(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) != {'key', 'stage', 'checks'}
            or not all(isinstance(value.get(k), str) and value[k] for k in ('key', 'stage'))
            or not isinstance(value['checks'], list) or not value['checks']):
        raise ValueError('invalid requirements manifest')
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
    if (not isinstance(value, dict) or value.get('key') != manifest['key']
            or key is not None and key != manifest['key'] or value.get('stage') != manifest['stage']):
        failures.append({'name': 'identity', 'reason': 'key-or-stage-mismatch'})
    if not isinstance(value, dict) or value.get('status') != 'done' or value.get('waiting') is not None:
        failures.append({'name': 'completion', 'reason': 'not-complete'})
    checks = value.get('checks', []) if isinstance(value, dict) else []
    artifacts = value.get('artifacts', []) if isinstance(value, dict) else []
    if not isinstance(checks, list):
        checks = []
    if not isinstance(artifacts, list):
        artifacts = []
    listed = {(base / p).resolve() for p in artifacts if isinstance(p, str)}
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
