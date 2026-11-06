"""Event completion follows handled records while preserving declared evidence."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from factory import requirements
from factory.triage import record


class EventRequirementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.manifest = {'key': 'PROJ-123', 'stage': 'intake', 'checks': [
            {'name': 'judgement', 'artifact': 'judge.json', 'resultField': 'status',
             'environment': {'key': 'PROJ-123'}}],
            'event': {'commentIds': ['PROJ-123:1', 'PROJ-123:2'],
                      'stateArtifact': 'state.json', 'queueArtifact': 'queue.json'}}
        self.handled = {identity: {'key': 'PROJ-123', 'action': 'queued',
                                  'at': '2026-11-06T09:25:00Z'}
                        for identity in self.manifest['event']['commentIds']}
        self.save('judge.json', {'status': 'PASS', 'key': 'PROJ-123'})
        self.save('state.json', {'handled': self.handled})
        self.save('queue.json', {'PROJ-123': {'stage': 'reproduce', 'waiting': 'reporter'}})
        self.result = {'key': 'PROJ-123', 'stage': 'intake', 'status': 'done', 'waiting': 'reporter',
                       'checks': [{'name': 'judgement', 'result': 'PASS'}],
                       'artifacts': ['judge.json', 'state.json', 'queue.json']}

    def save(self, name, value):
        (self.base / name).write_text(json.dumps(value))

    def gate(self):
        return requirements.evaluate({'manifest': self.manifest, 'base': str(self.base)}, self.result)

    def test_manifest_accepts_explicit_event_and_keeps_snapshot(self):
        self.save('requirements.json', self.manifest)
        declaration = requirements.snapshot(self.base / 'requirements.json')
        self.assertEqual(declaration['manifest'], self.manifest)
        self.manifest['event']['commentIds'].append('PROJ-123:3')
        self.assertEqual(len(declaration['manifest']['event']['commentIds']), 2)

    def test_handled_events_complete_with_any_remaining_wait(self):
        for waiting in ('reporter', 'human-risk', 'env-human', 'stg-approval', None):
            with self.subTest(waiting=waiting):
                self.result['waiting'] = waiting
                self.assertTrue(self.gate()['passed'], self.gate())

    def test_event_accepts_records_written_by_triage(self):
        (self.base / 'state.json').unlink()
        record(SimpleNamespace(state_dir=self.base), [
            ({'key': 'PROJ-123', 'id': '1'}, 'human'),
            ({'key': 'PROJ-123', 'id': '2'}, 'classified-information')])
        self.assertTrue(self.gate()['passed'], self.gate())

    def test_result_can_report_declared_or_listed_queue_stage(self):
        for stage in ('intake', 'reproduce'):
            with self.subTest(stage=stage):
                self.result['stage'] = stage
                self.assertTrue(self.gate()['passed'], self.gate())
        self.result['stage'] = 'build'
        self.assertIn({'name': 'identity', 'reason': 'key-or-stage-mismatch'}, self.gate()['failures'])

    def test_unlisted_invalid_or_wrong_key_queue_cannot_grant_stage(self):
        self.result['stage'] = 'reproduce'
        for value in ('invalid', '[]', '{"PROJ-999":{"stage":"reproduce"}}',
                      '{"PROJ-123":{"stage":null}}'):
            with self.subTest(value=value):
                (self.base / 'queue.json').write_text(value)
                self.assertIn({'name': 'identity', 'reason': 'key-or-stage-mismatch'}, self.gate()['failures'])
        self.save('queue.json', {'PROJ-123': {'stage': 'reproduce'}})
        self.result['artifacts'].remove('queue.json')
        self.assertFalse(self.gate()['passed'])

    def test_queue_artifact_is_optional_for_declared_stage(self):
        del self.manifest['event']['queueArtifact']
        self.result['artifacts'].remove('queue.json')
        self.assertTrue(self.gate()['passed'], self.gate())

    def test_every_declared_event_requires_same_key_action_and_timestamp(self):
        valid = self.handled['PROJ-123:2']
        invalid = [None, {}, {**valid, 'key': 'PROJ-999'}]
        invalid += [{**valid, 'action': action} for action in ('', ' ', None, 1)]
        invalid += [{**valid, 'at': at} for at in ('', ' ', None, [], {}, True, float('nan'), float('inf'))]
        for row in invalid:
            with self.subTest(row=row):
                self.save('state.json', {'handled': {**self.handled, 'PROJ-123:2': row}})
                self.assertIn({'name': 'completion', 'reason': 'event-not-handled'}, self.gate()['failures'])
        self.save('state.json', {'handled': {'PROJ-123:1': self.handled['PROJ-123:1']}})
        self.assertIn({'name': 'completion', 'reason': 'event-not-handled'}, self.gate()['failures'])

    def test_unlisted_unreadable_or_wrong_layout_state_fails_explicitly(self):
        for value in ('invalid', '[]', '{}', '{"handled":[]}',
                      json.dumps({'followup': {'handled': self.handled}})):
            with self.subTest(value=value):
                (self.base / 'state.json').write_text(value)
                self.assertIn({'name': 'completion', 'reason': 'event-not-handled'}, self.gate()['failures'])
        self.save('state.json', {'handled': self.handled})
        self.result['artifacts'].remove('state.json')
        self.assertIn({'name': 'completion', 'reason': 'event-not-handled'}, self.gate()['failures'])
        self.result['artifacts'].append('state.json')
        (self.base / 'state.json').unlink()
        self.assertIn({'name': 'completion', 'reason': 'event-not-handled'}, self.gate()['failures'])

    def test_non_events_keep_strict_completion_and_stage(self):
        del self.manifest['event']
        self.assertIn({'name': 'completion', 'reason': 'not-complete'}, self.gate()['failures'])
        self.result['waiting'] = None
        self.assertTrue(self.gate()['passed'], self.gate())
        self.result['stage'] = 'reproduce'
        self.assertIn({'name': 'identity', 'reason': 'key-or-stage-mismatch'}, self.gate()['failures'])

    def test_event_still_requires_done_and_matching_result_and_caller_keys(self):
        self.result['status'] = 'waiting'
        self.assertIn({'name': 'completion', 'reason': 'not-complete'}, self.gate()['failures'])
        self.result['status'] = 'done'
        self.result['key'] = 'PROJ-999'
        self.assertIn({'name': 'identity', 'reason': 'key-or-stage-mismatch'}, self.gate()['failures'])
        self.result['key'] = 'PROJ-123'
        gate = requirements.evaluate({'manifest': self.manifest, 'base': str(self.base)},
                                     self.result, key='PROJ-999')
        self.assertFalse(gate['passed'])

    def test_event_preserves_all_required_verifier_checks(self):
        for failure in ('missing', 'duplicate', 'check', 'unlisted', 'verifier', 'environment', 'unreadable'):
            with self.subTest(failure=failure):
                result = copy.deepcopy(self.result)
                self.save('judge.json', {'status': 'PASS', 'key': 'PROJ-123'})
                if failure == 'missing': result['checks'] = []
                if failure == 'duplicate': result['checks'] *= 2
                if failure == 'check': result['checks'][0]['result'] = 'FAIL'
                if failure == 'unlisted': result['artifacts'].remove('judge.json')
                if failure == 'verifier': self.save('judge.json', {'status': 'FAIL', 'key': 'PROJ-123'})
                if failure == 'environment': self.save('judge.json', {'status': 'PASS', 'key': 'PROJ-999'})
                if failure == 'unreadable': (self.base / 'judge.json').write_text('invalid')
                gate = requirements.evaluate({'manifest': self.manifest, 'base': str(self.base)}, result)
                self.assertFalse(gate['passed'])
                self.assertFalse(gate['retryAllowed'])
                self.assertEqual([row['name'] for row in gate['failures']], ['judgement'])

    def test_event_declaration_rejects_invalid_fields(self):
        valid = self.manifest['event']
        invalid = [None, {}, {**valid, 'extra': True}]
        invalid += [{**valid, 'commentIds': ids} for ids in ([], ['x', 'x'], [''], [' '], [1], 'x')]
        invalid += [{**valid, field: value} for field in ('stateArtifact', 'queueArtifact')
                    for value in ('', ' ', None, 1)]
        for event in invalid:
            with self.subTest(event=event):
                manifest = {**self.manifest, 'event': event}
                self.save('requirements.json', manifest)
                with self.assertRaises(ValueError):
                    requirements.snapshot(self.base / 'requirements.json')
