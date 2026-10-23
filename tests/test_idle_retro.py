import json
from types import SimpleNamespace
from unittest.mock import patch

from factory.idle import decide
from support import Fixture


class IdleRetroTests(Fixture):
    def test_normalized_local_persisted_then_stuck_and_changed_runnable(self):
        self.write('queue.json', {'PROJ-123': {'waiting': 'env-human', 'stage': 'tobe',
                                            'needs': 'db', 'nextStep': None, 'note': 'keep'}})
        result, code = decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-123', result['reasons'])
        row = self.read('queue.json')['PROJ-123']
        self.assertEqual((row['nextStep'], row['needs'], row['waiting']), ('local', '', None))
        self.assertEqual(row['note'], 'keep')
        result, code = decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertTrue(result['idle'])
        self.assertIn('stuck-runnable:PROJ-123', result['stuck'])
        self.assertEqual(self.read('guard-last.json')['stuck'], result['stuck'])
        lesson = json.loads((self.state / 'lessons.jsonl').read_text())
        self.assertEqual(lesson['kind'], 'infra_fail')
        decide(self.cfg)
        self.assertEqual(len((self.state / 'lessons.jsonl').read_text().splitlines()), 1)
        row['note'] = 'changed'
        self.write('queue.json', {'PROJ-123': row})
        self.assertEqual(decide(self.cfg)[1], 1)

    def test_stuck_notification_once_per_content_hash(self):
        self.data['notify'] = {'cmd': ['notify', '{message}']}
        self.write('queue.json', {'PROJ-123': {'waiting': None}})
        with patch.object(self.cfg, 'run', side_effect=self.responses_for_notify) as run:
            decide(self.cfg)
            decide(self.cfg)
            decide(self.cfg)
        self.assertEqual(sum(call.args[0][0] == 'notify' for call in run.call_args_list), 1)

    def responses_for_notify(self, command, **values):
        return SimpleNamespace(returncode=0, stdout=json.dumps({'events': [], 'errors': []}), stderr='')

    def test_preflight_cached_per_needs_set_and_fail_fast(self):
        self.write('queue.json', {f'PROJ-{key}': {'waiting': 'env-human', 'needs': needs}
                                 for key, needs in ((1, 'db,web'), (2, 'web,db'), (3, ['db', 'web']))})
        with patch('factory.preflight.check', return_value=({'results': []}, 10)) as check:
            self.assertEqual(decide(self.cfg)[1], 0)
        check.assert_called_once_with(self.cfg, ['db', 'web'], fail_fast=True)

    def test_waiting_human_groups_notified_once_and_failure_nonfatal(self):
        self.data['notify'] = {'cmd': ['notify', '{message}']}
        self.write('queue.json', {f'PROJ-{key}': {'waiting': 'env-human', 'needs': 'db'} for key in (1, 2)})
        probe = {'results': [{'status': 'HUMAN', 'human_action': 'Connect network', 'reason': 'wrong-network'}]}
        with patch('factory.preflight.check', return_value=(probe, 10)), \
                patch.object(self.cfg, 'run', side_effect=self.responses_for_notify) as run:
            result, code = decide(self.cfg)
            self.assertEqual(code, 0)
            self.assertEqual(result['waitingHuman'], [{'action': 'Connect network', 'keys': ['PROJ-1', 'PROJ-2']}])
            decide(self.cfg)
            self.write('queue.json', {'PROJ-1': {'waiting': 'env-human', 'needs': 'db'}})
            decide(self.cfg)
        self.assertEqual(sum(call.args[0][0] == 'notify' for call in run.call_args_list), 2)
        self.write('guard-state.json', {})
        def fail_notify(command, **values):
            if command[0] == 'notify':
                raise OSError('private detail')
            return self.responses_for_notify(command)
        with patch('factory.preflight.check', return_value=(probe, 10)), \
                patch.object(self.cfg, 'run', side_effect=fail_notify):
            result, code = decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(result['notify'], 'failed')
        self.assertNotIn('private detail', str(result))

    def test_scan_retries_nonzero_once_and_uses_fresh_response(self):
        failed = SimpleNamespace(returncode=1, stdout='', stderr='temporary')
        good = self.responses_for_notify([])
        with patch.object(self.cfg, 'run', side_effect=[failed, good]) as run, patch('factory.idle.time.sleep') as sleep:
            result, code = decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(3)
        with patch.object(self.cfg, 'run', return_value=failed) as run, patch('factory.idle.time.sleep'):
            result, code = decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn('scan-failed', result['reasons'])
        self.assertEqual(run.call_count, 2)
        self.assertFalse((self.state / 'scan.json').exists())

    def test_recovered_environment_also_has_stuck_backstop(self):
        self.write('queue.json', {'PROJ-123': {'waiting': 'env-down', 'needs': 'db'}})
        with patch('factory.preflight.check', return_value=({'results': []}, 0)):
            self.assertIn('environment-ready:PROJ-123', decide(self.cfg)[0]['reasons'])
            result, code = decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn('stuck-runnable:PROJ-123', result['stuck'])

    def test_malformed_guard_state_fails_open(self):
        self.write('queue.json', {'PROJ-123': {'waiting': None}})
        for state in ({'runnable': {'PROJ-123': []}}, {'waitingHuman': []}):
            with self.subTest(state=state):
                self.write('guard-state.json', state)
                result, code = decide(self.cfg)
                self.assertEqual(code, 2)
                self.assertIn('internal-error', result['reasons'])
