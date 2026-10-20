from unittest.mock import patch
from factory.idle import decide
from support import Fixture, event


class IdleTests(Fixture):
    def test_empty_scan_is_idle(self):
        result, code = decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertTrue(result["idle"])

    def test_events_and_runnable_queue_resume(self):
        self.scan([event()])
        self.assertEqual(decide(self.cfg)[1], 1)
        self.scan()
        for item in ({}, {"waiting": None}):
            self.write("queue.json", {"PROJ-123": item})
            self.assertEqual(decide(self.cfg)[1], 1)

    def test_waiting_queue_and_used_approval_idle(self):
        self.write("queue.json", {"PROJ-123": {"waiting": "human-risk"}})
        self.write("approvals.json", {"PROJ-123": {"approved": True, "used_at": "today"}})
        self.assertEqual(decide(self.cfg)[1], 0)

    def test_unused_approval_wakes_without_queue(self):
        self.write("approvals.json", {"PROJ-123": {"approved": True, "used_at": None}})
        self.assertEqual(decide(self.cfg)[1], 1)

    def test_recovered_environment_resumes(self):
        self.write("queue.json", {"PROJ-123": {"waiting": "env-down", "needs": "db,service"}})
        for code in (0, 10, 20, 2):
            with self.subTest(code=code), patch("factory.preflight.check", return_value=({}, code)) as probe:
                self.assertEqual(decide(self.cfg)[1], {0: 1, 10: 0, 20: 0, 2: 2}[code])
                probe.assert_called_once_with(self.cfg, ["db", "service"])

    def test_failure_never_reuses_old_empty_scan(self):
        self.write("scan.json", {"events": [], "errors": []})
        for response in ({"stdout": "{}", "code": 1}, {"stdout": "invalid"},
                         {"stderr": "Unauthorized private-value"}):
            self.scan(**response)
            result, code = decide(self.cfg)
            self.assertNotEqual(code, 0)
            self.assertFalse(result["idle"])
            self.assertNotIn("private-value", str(result))
            self.assertFalse((self.state / "scan.json").exists())

    def test_errors_or_invalid_event_never_idle(self):
        self.scan(errors=["scan failed"])
        self.assertEqual(decide(self.cfg)[1], 1)
        self.scan([{"key": "PROJ-123", "id": "1"}])
        self.assertEqual(decide(self.cfg)[1], 2)

    def test_malformed_state_fails_open(self):
        for name, value in (("queue.json", []), ("approvals.json", {"PROJ-123": []}),
                            ("queue.json", {"PROJ-123": {"waiting": 1}})):
            path = self.write(name, value)
            self.assertEqual(decide(self.cfg)[1], 2)
            path.unlink()

    def test_answered_events_are_handled_and_idle(self):
        self.scan([event(answered_by_me_after=True)])
        self.assertEqual(decide(self.cfg)[1], 0)

    def test_pending_decision_wake_resumes(self):
        self.write("decisions/example.wake-pending.json", {"wake_pending": True})
        self.assertEqual(decide(self.cfg)[1], 1)
