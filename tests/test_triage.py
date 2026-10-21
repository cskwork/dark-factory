import json
from unittest.mock import Mock
from factory.triage import triage
from support import Fixture, event


def prediction(identity="1", **changes):
    return {"key": "PROJ-123", "id": identity, "class": "information", "needs_response": False,
            "confidence": 0.95, "reason": "Reference only", **changes}


class TriageTests(Fixture):
    def test_human_reply_without_model_preserves_state(self):
        self.write("state.json", {"unrelated": [1], "handled": {"old": {"action": "posted"}}})
        classifier = Mock(side_effect=AssertionError("must not call"))
        self.assertEqual(triage(self.cfg, [event(answered_by_me_after=True)], classifier), [])
        classifier.assert_not_called()
        self.assertEqual(self.read("state.json")["unrelated"], [1])
        self.assertEqual(self.read("state.json")["handled"]["PROJ-123:1"]["action"], "human")

    def test_shadow_logs_without_hiding_event(self):
        self.assertEqual(triage(self.cfg, [event()], lambda _: {"predictions": [prediction()]}), [event()])
        self.assertFalse((self.state / "state.json").exists())
        self.assertEqual(len((self.state / "triage-shadow.jsonl").read_text().splitlines()), 1)

    def test_enforce_only_confident_information(self):
        self.data["triage"]["mode"] = "enforce"
        for changes, skip in [({}, True), ({"confidence": 0.89}, False), ({"needs_response": True}, False),
                              ({"class": "action_request"}, False), ({"confidence": 0.9}, True)]:
            with self.subTest(changes=changes):
                (self.state / "state.json").unlink(missing_ok=True)
                result = triage(self.cfg, [event()], lambda _: {"predictions": [prediction(**changes)]})
                self.assertEqual(result, [] if skip else [event()])

    def test_classifier_failures_fail_open(self):
        self.data["triage"]["mode"] = "enforce"
        for output in ({}, {"predictions": []}, {"predictions": [prediction(confidence=True)]},
                       {"predictions": [prediction(confidence=float("nan"))]},
                       {"predictions": [prediction(id="other")]},
                       {"predictions": [prediction(reason="")]},
                       {"predictions": [prediction(), prediction()]}):
            with self.subTest(output=output):
                self.assertEqual(triage(self.cfg, [event()], lambda _: output), [event()])
        self.assertEqual(triage(self.cfg, [event()], Mock(side_effect=TimeoutError())), [event()])

    def test_ticket_intake_bypasses_classifier_and_survives_its_failure(self):
        self.data["triage"]["mode"] = "enforce"
        intake = event("intake", key="PROJ-9", intake=True)
        seen = []
        result = triage(self.cfg, [intake, event()], lambda rows: seen.extend(rows) or {"predictions": [prediction()]})
        self.assertEqual(result, [intake])
        self.assertEqual(seen, [event()])
        only = Mock(side_effect=AssertionError("must not call"))
        self.assertEqual(triage(self.cfg, [intake], only), [intake])
        only.assert_not_called()
        self.assertEqual(triage(self.cfg, [intake, event("2")], Mock(side_effect=TimeoutError())), [intake, event("2")])

    def test_whole_batch_validated_before_any_skip(self):
        self.data["triage"]["mode"] = "enforce"
        events = [event(), event("2")]
        self.assertEqual(triage(self.cfg, events, lambda _: {"predictions": [prediction(), prediction("2", confidence=9)]}), events)

    def test_persisted_handled_avoids_reclassification(self):
        triage(self.cfg, [event(answered_by_me_after=True)])
        classifier = Mock(side_effect=AssertionError())
        self.assertEqual(triage(self.cfg, [event()], classifier), [])
        classifier.assert_not_called()

    def test_classifier_command_receives_json_on_stdin(self):
        path = self.root / "classifier.py"
        path.write_text('import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"predictions":[dict(key=e["key"],id=e["id"],**{"class":"information","needs_response":False,"confidence":1,"reason":"Reference"}) for e in x["events"]]}))\n')
        import sys
        self.data["triage"].update(mode="enforce", classifier_cmd=[sys.executable, str(path)])
        self.assertEqual(triage(self.cfg, [event()]), [])
