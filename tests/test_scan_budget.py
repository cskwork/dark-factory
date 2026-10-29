"""A scan must not outlive its tick, and a tick killed inside a scan must still be counted."""
from datetime import date
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from factory.idle import decide
from factory.runtime import guard
from support import Fixture


def failed_scan():
    return SimpleNamespace(returncode=1, stdout='', stderr='temporary')


def healthy_scan():
    return SimpleNamespace(returncode=0, stdout=json.dumps({'events': [], 'errors': []}), stderr='')


class ScanBudgetTests(Fixture):
    def test_killed_scan_is_counted_and_cools_down_without_rescanning(self):
        self.write('scan-state.json', {'attemptStartedAt': 100.0, 'failureCount': 0})
        with patch('factory.idle.time.time', return_value=700.0), patch.object(self.cfg, 'run') as run:
            result, code = decide(self.cfg)
        run.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn('scan-interrupted', result['reasons'])
        self.assertFalse(result['shouldWake'])
        health = self.read('scan-state.json')
        self.assertEqual((health['failureCount'], health['lastError']), (1, 'scan-interrupted'))
        self.assertEqual((health['retryAt'], health['durationSeconds']), (2500.0, 600.0))
        self.assertIsNone(health['attemptStartedAt'])
        with patch('factory.idle.time.time', return_value=701.0), patch.object(self.cfg, 'run') as run:
            result, _ = decide(self.cfg)
        run.assert_not_called()
        self.assertEqual(result['reasons'], ['scan-cooldown'])

    def test_unreadable_attempt_marker_is_still_counted(self):
        self.write('scan-state.json', {'attemptStartedAt': 'not-a-timestamp', 'failureCount': 0})
        with patch.object(self.cfg, 'run') as run:
            result, _ = decide(self.cfg)
        run.assert_not_called()
        self.assertIn('scan-interrupted', result['reasons'])
        self.assertIsNone(self.read('scan-state.json')['durationSeconds'])

    def test_observed_outcomes_resolve_the_attempt_marker(self):
        self.assertEqual(decide(self.cfg)[1], 0)
        self.assertIsNone(self.read('scan-state.json')['attemptStartedAt'])
        self.scan(code=1)
        with patch('factory.idle.time.sleep'):
            decide(self.cfg)
        self.assertIsNone(self.read('scan-state.json')['attemptStartedAt'])
        self.responses['scan'] = {'stdout': 'not json'}
        self.save_responses()
        result, code = decide(self.cfg, force_refresh=True)
        self.assertEqual(code, 2)
        self.assertIn('internal-error', result['reasons'])
        self.assertIsNone(self.read('scan-state.json')['attemptStartedAt'])

    def test_hanging_scan_fails_once_within_the_budget(self):
        self.data['intake']['scan_budget_seconds'] = 30
        hang = subprocess.TimeoutExpired(cmd=['scan'], timeout=30)
        with patch.object(self.cfg, 'run', side_effect=hang) as run, patch('factory.idle.time.sleep') as sleep:
            result, code = decide(self.cfg)
        self.assertEqual((run.call_count, code), (1, 1))
        sleep.assert_not_called()
        self.assertEqual(run.call_args.kwargs['timeout'], 30.0)
        self.assertIn('scan-timeout', result['reasons'])
        self.assertFalse(result['shouldWake'])
        self.assertEqual(self.read('scan-state.json')['lastError'], 'scan-timeout')

    def test_fast_failure_retries_once_inside_the_remaining_budget(self):
        self.data['intake']['scan_budget_seconds'] = 30
        with patch.object(self.cfg, 'run', side_effect=[failed_scan(), healthy_scan()]) as run, \
                patch('factory.idle.time.sleep') as sleep:
            result, code = decide(self.cfg)
        self.assertEqual((run.call_count, code), (2, 0))
        sleep.assert_called_once_with(3)
        first, second = (call.kwargs['timeout'] for call in run.call_args_list)
        self.assertEqual(first, 30.0)
        self.assertTrue(0 < second <= 27.0)

    def test_budget_shorter_than_the_retry_pause_blocks_the_second_attempt(self):
        self.data['intake']['scan_budget_seconds'] = 2
        with patch.object(self.cfg, 'run', return_value=failed_scan()) as run, \
                patch('factory.idle.time.sleep') as sleep:
            result, _ = decide(self.cfg)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn('scan-failed', result['reasons'])

    def test_invalid_scan_budget_is_an_internal_failure(self):
        self.data['intake']['scan_budget_seconds'] = 0
        result, code = decide(self.cfg)
        self.assertEqual(code, 2)
        self.assertIn('internal-error', result['reasons'])
        self.assertFalse((self.state / 'scan-state.json').exists())

    def test_command_timeout_caps_a_caller_supplied_budget(self):
        self.data['command_timeout_seconds'] = 5
        with patch('factory.config.subprocess.run') as run:
            for requested, expected in ((60, 5), (2, 2)):
                self.cfg.run(['true'], timeout=requested)
                self.assertEqual(run.call_args.kwargs['timeout'], expected)
            self.cfg.run(['true'])
            self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_guard_skips_a_killed_scan_but_queue_work_still_wakes(self):
        (self.state / 'done-today').write_text(date.today().isoformat())
        self.write('scan-state.json', {'attemptStartedAt': 1.0, 'failureCount': 0})
        message, run = guard(self.cfg)
        self.assertIsNone(run)
        self.assertIn('monitoring: unavailable', message)
        self.write('queue.json', {'PROJ-123': {'waiting': None}})
        message, run = guard(self.cfg)
        self.assertIsNotNone(run)
        self.assertIn('queue-runnable:PROJ-123', run['reasons'])
