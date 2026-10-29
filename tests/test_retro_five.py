"""Approved retrospective scenarios, offline commands and saved verifier evidence."""
from contextlib import redirect_stdout
from datetime import date
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from support import Fixture, event, ROOT
import sys
import time
from factory.idle import decide
from factory.runtime import guard, worker
from factory import drain


class MonitoringTests(Fixture):
    def partial(self, events=()):
        self.scan(stdout=json.dumps({'events': list(events), 'errors': ['chat-down'],
                  'sources': {'jira': {'status': 'available'}, 'chat': {'status': 'unavailable', 'reason': 'auth-required'}}}))

    def test_partial_health_preserves_events_and_does_not_cool_adapter(self):
        self.partial([event()])
        result, code = decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertEqual(result['events'], 1)
        self.assertEqual(result['sourceHealth']['status'], 'degraded')
        self.partial()
        result, code = decide(self.cfg)
        self.assertFalse(result['idle'])
        self.assertFalse(result['shouldWake'])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn('triageSeconds', result['timings'])

    def test_partial_unauthorized_reason_keeps_healthy_events(self):
        self.scan(stdout=json.dumps({'events': [event()], 'errors': ['chat unauthorized'],
                  'sources': {'jira': {'status': 'available'}, 'chat': {'status': 'unavailable', 'reason': 'unauthorized'}}}))
        result, code = decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertEqual(result['events'], 1)
        self.assertEqual(result['sourceHealth']['failureCount'], 0)

    def test_whole_failure_cooldown_queue_force_and_recovery(self):
        self.scan(code=1)
        with patch('factory.idle.time.sleep'):
            first, code = decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertEqual(first['sourceHealth']['failureCount'], 1)
        self.assertFalse(first['shouldWake'])
        result, _ = decide(self.cfg)
        self.assertEqual(len(self.calls()), 2)
        self.assertTrue(result['sourceHealth']['cooldown'])
        self.write('queue.json', {'PROJ-123': {'waiting': None}})
        result, _ = decide(self.cfg)
        self.assertTrue(result['shouldWake'])
        self.assertIn('queue-runnable:PROJ-123', result['reasons'])
        self.scan()
        result, _ = decide(self.cfg, force_refresh=True)
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(result['sourceHealth']['failureCount'], 0)
        self.assertIsNotNone(result['sourceHealth']['lastSuccessAt'])
        self.assertIsNotNone(result['sourceHealth']['lastErrorAt'])

    def test_guard_monitoring_failure_skips_but_invalid_queue_wakes(self):
        (self.state / 'done-today').write_text(date.today().isoformat())
        self.partial()
        message, run = guard(self.cfg)
        self.assertIsNone(run)
        self.assertIn('degraded', message)
        self.write('queue.json', {'bad': {}})
        result, code = decide(self.cfg)
        self.assertEqual(code, 2)
        self.assertTrue(result['shouldWake'])


    def test_command_cooldown_exponential_cap_and_sanitized_errors(self):
        self.scan(code=1, stderr='secret fixture token')
        for now, delay in ((100, 1800), (1901, 3600), (5502, 3600)):
            with patch('factory.idle.time.time', return_value=now), patch('factory.idle.time.sleep'):
                result, _ = decide(self.cfg)
            self.assertEqual(result['sourceHealth']['retryAt'] - now, delay)
            self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(len(self.calls()), 6)

    def test_failed_monitoring_retains_approval_focus_and_environment_recovery(self):
        self.partial()
        self.write('approvals.json', {'PROJ-123': {'approved': True}})
        self.assertTrue(decide(self.cfg)[0]['shouldWake'])
        self.write('approvals.json', {})
        (self.state / 'pending-focus.jsonl').write_text('{"key":"PROJ-123"}\n')
        self.assertTrue(decide(self.cfg)[0]['shouldWake'])
        (self.state / 'pending-focus.jsonl').unlink()
        self.write('queue.json', {'PROJ-123': {'waiting': 'env-down', 'needs': 'db'}})
        with patch('factory.preflight.check', return_value=({'results': []}, 0)):
            result, _ = decide(self.cfg)
        self.assertTrue(result['shouldWake'])
        self.assertIn('environment-ready:PROJ-123', result['reasons'])

    def test_force_refresh_real_cli_and_guard_bypass_window(self):
        self.scan(code=1)
        with patch('factory.idle.time.sleep'):
            decide(self.cfg)
        self.scan()
        self.save_config()
        response = subprocess.run([sys.executable, '-m', 'factory.idle', '--config', str(self.config), '--force-refresh'],
                                  cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        (self.state / 'done-today').write_text(date.today().isoformat())
        self.write('last-wake.json', {'at': time.time()})
        self.partial([event()])
        self.assertIsNotNone(guard(self.cfg, force_refresh=True)[1])

    def test_malformed_source_metadata_is_internal_failure(self):
        for sources in ({'chat': {'status': 'bogus'}}, {'chat': {'status': 'unavailable', 'reason': 'secret value'}},
                        {'chat': {'status': 'available', 'durationSeconds': -1}}):
            self.scan(stdout=json.dumps({'events': [], 'errors': [], 'sources': sources}))
            result, code = decide(self.cfg)
            self.assertEqual(code, 2)
            self.assertIn('internal-error', result['reasons'])


class EvidenceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.brief = self.root / 'brief.md'
        self.brief.write_text('Approved unit')
        self.result = {'status': 'done', 'summary': 'verified', 'key': 'PROJ-123', 'stage': 'built',
                       'waiting': None, 'artifacts': [str(self.root / 'verify.json')],
                       'checks': [{'name': 'runtime', 'result': 'PASS'}]}
        self.manifest = self.root / 'requirements.json'
        self.manifest.write_text(json.dumps({'key': 'PROJ-123', 'stage': 'built', 'checks': [
            {'name': 'runtime', 'artifact': 'verify.json', 'resultField': 'status', 'environment': {'env': 'dev'}}]}))
        (self.root / 'verify.json').write_text(json.dumps({'status': 'PASS', 'env': 'dev'}))
        self.responses['launch'] = {'stdout': json.dumps(self.result)}
        self.save_responses()

    def test_worker_gate_failures_preserve_artifact_and_passed_checks(self):
        for failure in ('missing', 'FAIL', 'SKIP', 'duplicate', 'environment', 'artifact'):
            with self.subTest(failure=failure):
                result = json.loads(json.dumps(self.result))
                (self.root / 'verify.json').write_text(json.dumps({'status': 'PASS', 'env': 'dev'}))
                if failure == 'missing': result['checks'] = []
                if failure in ('FAIL', 'SKIP'): result['checks'][0]['result'] = failure
                if failure == 'duplicate': result['checks'] *= 2
                if failure == 'environment': (self.root / 'verify.json').write_text('{"status":"PASS","env":"prd"}')
                if failure == 'artifact': (self.root / 'verify.json').unlink()
                self.responses['launch'] = {'stdout': json.dumps(result)}
                self.save_responses()
                response = self.cli('factory-worker', '--key', 'PROJ-123', '--requirements', str(self.manifest),
                                    'builder', 'title', str(self.brief))
                self.assertNotEqual(response.returncode, 0)
                self.assertFalse(json.loads(response.stdout)['retryAllowed'])

    def test_worker_gate_real_cli_passes_and_legacy_shape_stays(self):
        response = self.cli('factory-worker', '--key', 'PROJ-123', '--requirements', str(self.manifest),
                            'builder', 'title', str(self.brief))
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertEqual(json.loads(response.stdout), self.result)

    def test_checkpoint_survives_nonzero_and_timeout_with_process_failure(self):
        for timeout in (False, True):
            def launch(command, **kwargs):
                checkpoint = Path(json.JSONDecoder().raw_decode(command[-1].split('checkpoint_path=', 1)[1])[0])
                checkpoint.write_text(json.dumps(self.result))
                if timeout:
                    raise subprocess.TimeoutExpired(command, 1, output='partial', stderr='timeout')
                return SimpleNamespace(returncode=7, stdout='cleanup failed', stderr='denied')
            with patch('factory.runtime.subprocess.run', side_effect=launch), redirect_stdout(io.StringIO()) as out:
                code = worker(self.cfg, 'builder', 'title', self.brief, key='PROJ-123')
            self.assertEqual(code, 124 if timeout else 7)
            payload = json.loads(out.getvalue())
            self.assertTrue(Path(payload['checkpoint']).is_file())
            self.assertEqual(payload['result'], self.result)
            meta = json.loads(Path(payload['invocation']).read_text())
            self.assertGreaterEqual(meta['durationSeconds'], 0)
            self.assertNotEqual(meta['outcome'], 'success')

    def test_checkpoint_wrong_key_and_stale_rejected(self):
        import os
        for wrong_key in (True, False):
            def launch(command, **kwargs):
                checkpoint = Path(json.JSONDecoder().raw_decode(command[-1].split('checkpoint_path=', 1)[1])[0])
                value = {**self.result, 'key': 'PROJ-999'} if wrong_key else self.result
                checkpoint.write_text(json.dumps(value))
                if not wrong_key: os.utime(checkpoint, (1, 1))
                return SimpleNamespace(returncode=7, stdout='', stderr='')
            with patch('factory.runtime.subprocess.run', side_effect=launch), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(worker(self.cfg, 'builder', 'title', self.brief, key='PROJ-123'), 7)
            self.assertNotIn('result', json.loads(out.getvalue()))


    def test_manifest_snapshot_cannot_be_waived_during_worker(self):
        def launch(command, **kwargs):
            self.manifest.unlink()
            return SimpleNamespace(returncode=0, stdout=json.dumps({**self.result, 'checks': []}), stderr='')
        with patch('factory.runtime.subprocess.run', side_effect=launch), redirect_stdout(io.StringIO()) as out:
            code = worker(self.cfg, 'builder', 'title', self.brief, key='PROJ-123', requirements=self.manifest)
        self.assertEqual(code, 4)
        self.assertFalse(json.loads(out.getvalue())['retryAllowed'])

    def test_gate_retains_passing_evidence_and_checks_stage(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['checks'].append({**manifest['checks'][0], 'name': 'another'})
        self.manifest.write_text(json.dumps(manifest))
        response = self.cli('factory-worker', '--requirements', str(self.manifest), 'builder', 'title', str(self.brief))
        gate = json.loads(response.stdout)['gate']
        self.assertEqual(response.returncode, 4)
        self.assertEqual([row['name'] for row in gate['passedChecks']], ['runtime'])
        self.assertEqual(gate['failures'][0]['name'], 'another')
        self.result['stage'] = 'wrong-stage'
        self.responses['launch'] = {'stdout': json.dumps(self.result)}
        self.save_responses()
        response = self.cli('factory-worker', '--requirements', str(self.manifest), 'builder', 'title', str(self.brief))
        self.assertIn('key-or-stage-mismatch', response.stdout)

    def test_drain_cli_enforces_gate_without_destroying_result(self):
        self.write('running.json', {'token': 'test', 'started_at': time.time()})
        result_path = self.root / 'result.json'
        self.save_config()
        command = [sys.executable, '-m', 'factory.drain', '--config', str(self.config), '--token', 'test',
                   'check-result', '--key', 'PROJ-123', '--result', str(result_path), '--requirements', str(self.manifest)]
        for expected, value in ((0, self.result), (4, {**self.result, 'checks': []})):
            result_path.write_text(json.dumps(value))
            before = result_path.read_bytes()
            response = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(response.returncode, expected, response.stdout + response.stderr)
            payload = json.loads(response.stdout)
            self.assertEqual(payload['gate']['passed'], expected == 0)
            if expected:
                self.assertFalse(payload['retryAllowed'])
            self.assertEqual(result_path.read_bytes(), before)
            self.assertTrue(result_path.with_suffix('.json.gate.json').exists())

    def test_retry_preserves_first_raw_result_and_unique_attempts(self):
        self.write('running.json', {'token': 'test', 'started_at': time.time()})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, 'builder', 'title', self.brief, token='test', key='PROJ-123'), 0)
        first = (self.state / 'runs/test/PROJ-123-unit.json').read_bytes()
        self.responses['launch'] = {'code': 7, 'stdout': 'cleanup failed'}
        self.save_responses()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, 'builder', 'title', self.brief, token='test', key='PROJ-123'), 7)
        self.assertEqual((self.state / 'runs/test/PROJ-123-unit.json').read_bytes(), first)
        attempts = list((self.state / 'runs/test/attempts').glob('*/invocation.json'))
        self.assertEqual(len(attempts), 2)
        self.assertTrue((self.state / 'running.json').exists())


class MetricsTests(Fixture):
    def test_observed_metrics_unknown_mixed_idempotent_and_invalid(self):
        self.write('running.json', {'token': 'test', 'started_at': 100})
        drain.summary_init(self.cfg, 'test', 1, 0, now=100)
        observed = {'tokens': {'value': 40, 'source': 'provider-export'},
                    'costUsd': {'value': 0.02, 'source': 'billing-export'},
                    'waitSeconds': {'value': 5, 'source': 'operator-clock'},
                    'humanInterventions': {'value': 1, 'source': 'approval-log'},
                    'reworkCount': {'value': 0, 'source': 'unit-log'}}
        for _ in range(2):
            drain.summary_record(self.cfg, 'test', 'PROJ-123', 'one', 100, 110, 1, observations=observed)
        result = drain.summary_record(self.cfg, 'test', 'PROJ-123', 'two', 105, 120, 1)
        self.assertEqual(result['durationSeconds'], 25)
        self.assertEqual(result['wallElapsedSeconds'], 20)
        self.assertEqual(result['metrics']['tokens'], {'value': None, 'knownSubtotal': 40, 'knownUnits': 1, 'unknownUnits': 1})
        before = self.read('runs/test/summary.json')
        for bad in (-1, float('nan'), True):
            with self.assertRaises(ValueError):
                drain.summary_record(self.cfg, 'test', None, 'bad', 120, 121, 1,
                                     observations={'tokens': {'value': bad, 'source': 'provider'}})
        self.assertEqual(self.read('runs/test/summary.json'), before)

    def test_real_worker_invocation_summary_cli_with_measurements(self):
        started = time.time()
        self.write('running.json', {'token': 'test', 'started_at': started})
        brief = self.root / 'brief.md'
        brief.write_text('Approved fixture')
        self.responses['launch'] = {'code': 7, 'stdout': 'fixture failure'}
        self.save_responses()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, 'builder', 'title', brief, token='test', key='PROJ-123'), 7)
        invocation = next((self.state / 'runs/test/attempts').glob('*/invocation.json'))
        drain.summary_init(self.cfg, 'test', 1, 0)
        observations = self.root / 'observations.json'
        observations.write_text(json.dumps({'humanInterventions': {'value': 0, 'source': 'fixture-action-log'}}))
        self.save_config()
        command = [sys.executable, '-m', 'factory.drain', '--config', str(self.config), '--token', 'test',
                   'summary-record', '--invocation', str(invocation), '--runtime', '--observations', str(observations)]
        for _ in range(2):
            response = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        summary = json.loads(response.stdout)
        self.assertEqual(len(summary['units']), 1)
        self.assertEqual(summary['units'][0]['invocation']['exitCode'], 7)
        self.assertEqual(summary['metrics']['humanInterventions']['value'], 0)
        self.assertIsNone(summary['metrics']['costUsd']['value'])
        self.assertEqual(summary['metrics']['costUsd']['unknownUnits'], 1)

    def test_invocation_summary_preserves_monotonic_duration_across_clock_changes(self):
        self.write('running.json', {'token': 'test', 'started_at': 100})
        drain.summary_init(self.cfg, 'test', 1, 0, now=100)
        brief = self.root / 'brief.md'
        brief.write_text('Approved fixture')
        result = {'status': 'done', 'summary': 'verified', 'key': 'PROJ-123', 'stage': 'built',
                  'waiting': None, 'artifacts': [], 'checks': []}
        for index, wall in enumerate(((100, 200), (200, 100))):
            with patch('factory.drain.next_budget', return_value={'timeoutSeconds': 500, 'timeout': '500s'}), \
                 patch('factory.runtime.time.time', side_effect=wall), \
                 patch('factory.runtime.time.monotonic', side_effect=(1000, 1010)), \
                 patch('factory.runtime.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(worker(self.cfg, 'builder', 'title', brief, token='test', key='PROJ-123', unit=str(index)), 0)
            invocation_path = next(path for path in (self.state / 'runs/test/attempts').glob('*/invocation.json')
                                   if json.loads(path.read_text())['unit'] == str(index))
            invocation = json.loads(invocation_path.read_text())
            self.assertEqual(invocation['durationSeconds'], 10)
            self.save_config()
            response = subprocess.run([sys.executable, '-m', 'factory.drain', '--config', str(self.config), '--token', 'test',
                                       'summary-record', '--invocation', str(invocation_path)],
                                      cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
            summary = json.loads(response.stdout)
            self.assertEqual(summary['units'][-1]['durationSeconds'], 10)
            self.assertEqual(summary['durationSeconds'], 10 * (index + 1))
            self.assertEqual(summary['stages']['built']['durationSeconds'], 10 * (index + 1))
            self.assertEqual(summary['wallElapsedSeconds'], 100 if index == 0 else None)
            original_summary = self.read('runs/test/summary.json')
            for invalid in (-1, float('nan'), True, None):
                invocation_path.write_text(json.dumps({**invocation, 'durationSeconds': invalid}))
                rejected = subprocess.run([sys.executable, '-m', 'factory.drain', '--config', str(self.config), '--token', 'test',
                                           'summary-record', '--invocation', str(invocation_path)],
                                          cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(self.read('runs/test/summary.json'), original_summary)
            invocation_path.write_text(json.dumps(invocation))
