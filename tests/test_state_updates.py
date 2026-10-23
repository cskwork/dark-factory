"""Shared-state concurrency and failure cases use temporary files only."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from factory import state
from support import Fixture, ROOT


class StateUpdateTests(Fixture):
    def state_cli(self, *args):
        self.save_config()
        return subprocess.run([sys.executable, "-m", "factory.state", "--config", str(self.config), *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)

    def test_nested_patch_retains_siblings_and_deletes_only_named_keys(self):
        self.write("state.json", {"morning": {"at": "keep"}, "handled": {"old": {}, "remove": {}}})
        state.patch(self.state, "state.json", ["handled"], {"new": {"action": "posted"}}, delete=["remove"])
        self.assertEqual(self.read("state.json"), {"morning": {"at": "keep"},
                                                  "handled": {"old": {}, "new": {"action": "posted"}}})

    def test_queue_created_at_backfills_only_changed_items(self):
        self.write("queue.json", {"untouched": {"waiting": None}, "changed": {"stage": "intent"},
                                  "dated": {"createdAt": "first", "stage": "intent"}})
        state.patch(self.state, "queue.json", ["changed"], {"stage": "plan"})
        state.patch(self.state, "queue.json", ["dated"], {"stage": "build"})
        queue = self.read("queue.json")
        self.assertNotIn("createdAt", queue["untouched"])
        self.assertTrue(queue["changed"]["createdAt"])
        self.assertEqual(queue["dated"]["createdAt"], "first")

    def test_three_processes_fifty_updates_each_across_shared_files(self):
        program = '''import sys
from pathlib import Path
from factory.state import patch
directory, worker = Path(sys.argv[1]), sys.argv[2]
for index in range(50):
    key = worker + '-' + str(index)
    patch(directory, 'queue.json', [], {key: {'waiting': None}})
    patch(directory, 'state.json', ['handled'], {key: {'action': 'human'}})
    patch(directory, 'approvals.json', [], {key: {'approved': True}})
'''
        children = [subprocess.Popen([sys.executable, "-c", program, str(self.state), str(worker)],
                                     cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for worker in range(3)]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stdout + stderr)
        expected = {f"{worker}-{index}" for worker in range(3) for index in range(50)}
        self.assertEqual(set(self.read("queue.json")), expected)
        self.assertEqual(set(self.read("state.json")["handled"]), expected)
        self.assertEqual(set(self.read("approvals.json")), expected)

    def test_semantic_change_resets_deferral_without_touching_siblings(self):
        sibling = {"waiting": "reporter", "stage": "intent", "deferCount": 2}
        self.write("queue.json", {"changed": {"waiting": "reporter", "stage": "intent", "deferCount": 3},
                                  "sibling": sibling, "legacy": {"waiting": "reporter", "stage": "intent"}})
        state.patch(self.state, "queue.json", ["changed"], {"stage": "plan"})
        state.patch(self.state, "queue.json", ["legacy"], {"stage": "plan"})
        queue = self.read("queue.json")
        self.assertEqual(queue["changed"]["deferCount"], 0)
        self.assertEqual(queue["sibling"], sibling)
        self.assertNotIn("deferCount", queue["legacy"])

    def test_bookkeeping_only_change_preserves_count_and_digest(self):
        row = {"waiting": "reporter", "stage": "intent", "deferCount": 3}
        self.write("queue.json", {"item": row})
        state.patch(self.state, "queue.json", ["item"], {
            "deferredAt": "new", "deferReason": "budget", "deferCount": 4,
            "updatedAt": "new", "createdAt": "first", "backoffUntil": "later", "backoffLevel": 1})
        current = self.read("queue.json")["item"]
        self.assertEqual(current["deferCount"], 4)
        self.assertEqual(state.item_digest(current), state.item_digest(row))

    def test_replace_failure_preserves_original_cleans_scratch_and_unlocks(self):
        target = self.write("state.json", {"original": True})
        original = target.read_bytes()
        with patch.object(state.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaises(OSError):
                state.patch(self.state, "state.json", [], {"new": True})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.state.glob("*.tmp")) + list(self.state.glob(".*.tmp")), [])
        self.assertEqual(self.state_cli("patch", "state.json", "--json", '{"after": true}').returncode, 0)

    def test_callback_or_serialization_failure_preserves_original(self):
        target = self.write("state.json", {"original": True})
        original = target.read_bytes()

        def fail(data):
            data["new"] = True
            raise ValueError("fixture callback failure")

        with self.assertRaises(ValueError):
            state.transaction(self.state, "state.json", fail)
        with self.assertRaises(TypeError):
            state.patch(self.state, "state.json", [], {"invalid": object()})
        self.assertEqual(target.read_bytes(), original)

    def test_corrupt_or_non_object_state_is_not_replaced(self):
        for payload in ("{broken", "[]"):
            target = self.state / "state.json"
            target.write_text(payload)
            result = self.state_cli("patch", "state.json", "--json", '{"new": true}')
            self.assertEqual(result.returncode, 1)
            self.assertEqual(target.read_text(), payload)

    def test_invalid_nested_path_or_filename_is_not_overwritten(self):
        self.write("state.json", {"handled": "invalid"})
        with self.assertRaises(ValueError):
            state.patch(self.state, "state.json", ["handled", "new"], {"new": {}})
        with self.assertRaises(ValueError):
            state.patch(self.state, "../outside.json", [], {})
        self.assertEqual(self.read("state.json"), {"handled": "invalid"})
        self.assertFalse((self.root / "outside.json").exists())

    def test_cli_patch_preserves_unrelated_approval(self):
        self.write("approvals.json", {"PROJ-1": {"approved": True}})
        result = self.state_cli("patch", "approvals.json", "--path", '["PROJ-2"]',
                                "--json", '{"approved": true,"used_at":null}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("approvals.json")["PROJ-1"], {"approved": True})
        self.assertIsNone(self.read("approvals.json")["PROJ-2"]["used_at"])

    def test_pending_consumes_exact_occurrences_preserving_new_requests(self):
        old = '{"key":"PROJ-1"}'
        state.append_pending(self.state, old)
        captured = (self.state / "pending-focus.jsonl").read_text().splitlines()
        state.append_pending(self.state, old)
        state.append_pending(self.state, '{"key":"PROJ-2"}')
        self.assertEqual(state.consume_pending(self.state, captured), 1)
        self.assertEqual((self.state / "pending-focus.jsonl").read_text().splitlines(),
                         [old, '{"key":"PROJ-2"}'])

    def test_pending_append_and_consume_share_lock(self):
        state.append_pending(self.state, "processed")
        program = '''import sys
from pathlib import Path
from factory.state import append_pending
for index in range(50):
    append_pending(Path(sys.argv[1]), 'new-' + str(index))
'''
        child = subprocess.Popen([sys.executable, "-c", program, str(self.state)], cwd=ROOT)
        try:
            self.assertEqual(state.consume_pending(self.state, ["processed"]), 1)
        finally:
            self.assertEqual(child.wait(timeout=15), 0)
        self.assertEqual(set((self.state / "pending-focus.jsonl").read_text().splitlines()),
                         {f"new-{index}" for index in range(50)})

    def test_pending_cli_and_multiline_rejection(self):
        result = self.state_cli("append-pending", "--json", '{"key":"PROJ-1"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        line = (self.state / "pending-focus.jsonl").read_text().splitlines()[0]
        result = self.state_cli("consume-pending", "--lines", json.dumps([line]))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "1"))
        self.assertEqual((self.state / "pending-focus.jsonl").read_text(), "")
        with self.assertRaises(ValueError):
            state.append_pending(self.state, "one\ntwo")
