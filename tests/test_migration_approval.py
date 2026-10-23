from unittest.mock import patch

from factory.decision_page import DecisionRefused, create_request
from support import Fixture


class MigrationApprovalTests(Fixture):
    def test_failed_migrations_refuse_approval_before_persistence(self):
        self.data['migrations'] = {'repo': 'service', 'base': 'origin/stg', 'head': 'HEAD'}
        for code in (1, 3, 10):
            with self.subTest(code=code), patch('factory.check_migrations.check', return_value=({'status': 'FAIL'}, code)) as check:
                with self.assertRaises(DecisionRefused):
                    create_request(self.cfg, {'kind': 'approval', 'key': 'PROJ-123'})
                check.assert_called_once_with(self.cfg, repo=None, base=None, head=None)
                self.assertFalse((self.state / 'decisions').exists())

    def test_passing_migrations_bound_to_request_and_scope_overrides(self):
        self.data['migrations'] = {'repo': 'service'}
        report = {'status': 'PASS', 'base_head': 'aaa', 'head_commit': 'bbb'}
        with patch('factory.check_migrations.check', return_value=(report, 0)) as check:
            spec = create_request(self.cfg, {'kind': 'approval', 'key': 'PROJ-123',
                                            'migration_repo': 'other', 'migration_base': 'stg',
                                            'migration_head': 'reviewed'})
        check.assert_called_once_with(self.cfg, repo='other', base='stg', head='reviewed')
        self.assertEqual(spec['migration_check'], report)
        self.assertTrue((self.state / 'decisions' / (spec['decision_id'] + '.request.json')).exists())

    def test_choice_does_not_probe_migrations(self):
        self.data['migrations'] = {'repo': 'service'}
        with patch('factory.check_migrations.check') as check:
            create_request(self.cfg, {'kind': 'choice', 'key': 'PROJ-123',
                                      'options': [{'id': 'hold', 'label': 'Hold'}]})
        check.assert_not_called()
