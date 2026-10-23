"""Sequential drain boundaries use temporary state and injected commands."""
import json
import subprocess
import sys
from factory import drain
from factory.state import item_digest, patch
from support import Fixture, ROOT


class DrainTests(Fixture):
    def setUp(self):
        super().setUp()
        self.started = 1000000
        self.data["drain"] = {"enabled": True}
        self.write("running.json", {"token": "test", "started_at": self.started})

    def drain_cli(self, *args, token="test"):
        self.save_config()
        return subprocess.run([sys.executable, "-m", "factory.drain", "--config", str(self.config),
                               "--token", token, *args], cwd=ROOT, capture_output=True, text=True, timeout=20)

    def item(self, **fields):
        return {"stage": "intent", "waiting": None, "commentIds": [], "note": "", **fields}

    def test_budget_uses_json_start_and_caps_default(self):
        (self.state / "running").write_text("9")
        value = drain.next_budget(self.cfg, "test", "90m", self.started)
        self.assertEqual(value["deadline"], self.started + 9900)
        self.assertEqual(value["timeoutSeconds"], 5400)
        self.assertTrue(value["canStart"])
        self.assertTrue(value["drainEnabled"])

    def test_budget_boundary_and_shutdown_buffer(self):
        boundary = self.started + 9900 - 1800
        value = drain.next_budget(self.cfg, "test", 5400, boundary)
        self.assertTrue(value["canStart"])
        self.assertEqual(value["timeoutSeconds"], 1200)
        self.assertFalse(drain.next_budget(self.cfg, "test", 5400, boundary + 1)["canStart"])
        self.assertEqual(drain.next_budget(self.cfg, "test", 5400, self.started + 10000)["timeoutSeconds"], 0)

    def test_switch_zero_only_allows_first_unit_and_one_restores_drain(self):
        (self.state / "drain").write_text("0\n")
        self.assertTrue(drain.next_budget(self.cfg, "test", 5400, self.started, units=0)["canStart"])
        self.assertFalse(drain.next_budget(self.cfg, "test", 5400, self.started, units=1)["canStart"])
        self.write("runs/test/summary.json", {"runtimeUnitCount": 1})
        self.assertFalse(drain.next_budget(self.cfg, "test", 5400, self.started)["canStart"])
        (self.state / "drain").write_text("1\n")
        self.assertTrue(drain.next_budget(self.cfg, "test", 5400, self.started, units=3)["canStart"])

    def test_missing_or_disabled_config_restores_one_unit_behavior(self):
        for settings in (None, {"enabled": False}):
            if settings is None:
                self.data.pop("drain", None)
            else:
                self.data["drain"] = settings
            with self.subTest(settings=settings):
                self.assertTrue(drain.next_budget(self.cfg, "test", now=self.started, units=0)["canStart"])
                self.assertFalse(drain.next_budget(self.cfg, "test", now=self.started, units=1)["canStart"])
        (self.state / "drain").write_text("1")
        self.assertFalse(drain.enabled(self.cfg))

    def test_cli_accepts_common_options_after_command_and_rejects_other_owner(self):
        result = self.drain_cli("next-budget", "--default-timeout", "60m", "--now", str(self.started),
                                "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["timeout"], "3600s")
        self.assertEqual(self.drain_cli("next-budget", token="other").returncode, 1)

    def test_order_approval_pending_focus_created_handled_number(self):
        queue = {f"PROJ-{index}": self.item() for index in range(1, 9)}
        queue["PROJ-8"]["createdAt"] = "2020-01-01T00:00:00Z"
        queue["PROJ-7"]["createdAt"] = "2021-01-01T00:00:00Z"
        queue["PROJ-6"]["commentIds"] = ["old"]
        ranked = drain.rank(queue, approvals={"PROJ-5": {"approved": True}}, pending=["PROJ-4"],
                            focus="PROJ-3", handled={"PROJ-6:old": {"at": "2019-01-01T00:00:00Z"}})
        self.assertEqual(ranked, ["PROJ-5", "PROJ-4", "PROJ-3", "PROJ-8", "PROJ-7", "PROJ-6", "PROJ-1", "PROJ-2"])

    def test_rank_skips_only_backoff_item_and_blocked_key(self):
        queue = {"PROJ-1": self.item(backoffUntil=200), "PROJ-2": self.item(), "PROJ-3": self.item()}
        self.assertEqual(drain.rank(queue, now=100, blocked=["PROJ-3"]), ["PROJ-2"])
        self.assertEqual(drain.rank(queue, now=200, blocked=["PROJ-3"]), ["PROJ-1", "PROJ-2"])
        self.assertEqual(drain.rank(queue, now=200, blocked=["*"]), [])

    def test_wait_requires_explicit_runtime_eligibility(self):
        queue = {"PROJ-1": self.item(waiting="env-human")}
        self.assertEqual(drain.rank(queue), [])
        self.assertEqual(drain.rank(queue, eligible=["PROJ-1"]), ["PROJ-1"])
        self.assertEqual(drain.rank(queue, eligible=[], pending=["PROJ-1"], focus="PROJ-1",
                                    approvals={"PROJ-1": {"approved": True}}), [])

    def test_rank_cli_uses_configured_key_contract_and_handled_layout(self):
        self.data["key_pattern"] = r"ticket_\d+"
        self.write("queue.json", {"ticket_2": self.item(commentIds=["old"]), "ticket_1": self.item()})
        self.write("state.json", {"handled": {"ticket_2:old": {"at": 10}}})
        result = self.drain_cli("rank")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["ticket_2", "ticket_1"])

    def test_hash_ignores_bookkeeping_but_tracks_real_changes(self):
        item = self.item()
        changed = {**item, **dict.fromkeys(drain.BOOKKEEPING, 42)}
        self.assertEqual(item_digest(item), item_digest(changed))
        changed["note"] = "changed"
        self.assertNotEqual(item_digest(item), item_digest(changed))

    def test_summary_records_actual_workers_duration_and_idempotent_unit(self):
        result = self.drain_cli("summary-init", "--runnable", "4", "--lane-eligible", "0", "--now", str(self.started))
        self.assertEqual(result.returncode, 0, result.stderr)
        args = ("summary-record", "--key", "PROJ-1", "--unit", "runtime", "--started-at", str(self.started + 1),
                "--ended-at", str(self.started + 31), "--workers", "2", "--runtime")
        for _ in range(2):
            result = self.drain_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["workers"], 2)
        self.assertEqual(value["runtimeUnitCount"], 1)
        self.assertEqual(value["durationSeconds"], 30)
        self.assertEqual(value["startedAt"], self.started)
        result = self.drain_cli("summary-init", "--runnable", "0", "--lane-eligible", "1")
        self.assertEqual(json.loads(result.stdout)["workers"], 2)
        self.assertEqual(self.drain_cli(*args, token="other").returncode, 1)

    def test_invalid_summary_record_preserves_original(self):
        self.assertEqual(self.drain_cli("summary-init", "--runnable", "4", "--lane-eligible", "0").returncode, 0)
        path = self.state / "runs/test/summary.json"
        original = path.read_bytes()
        result = self.drain_cli("summary-record", "--key", "PROJ-1", "--unit", "runtime", "--started-at", "10",
                                "--ended-at", "9", "--workers", "1", "--runtime")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(path.read_bytes(), original)

    def test_defer_once_per_run_and_processed_reset(self):
        self.write("queue.json", {"PROJ-1": self.item()})
        for offset in (1, 2):
            result = self.drain_cli("defer", "--key", "PROJ-1", "--reason", "budget", "--now", str(self.started + offset))
            self.assertEqual(result.returncode, 0, result.stderr)
        row = self.read("queue.json")["PROJ-1"]
        self.assertEqual(row["deferCount"], 1)
        self.assertIn("createdAt", row)
        result = self.drain_cli("processed", "--key", "PROJ-1", "--now", str(self.started + 3))
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.read("queue.json")["PROJ-1"]
        self.assertEqual(row["deferCount"], 0)
        self.assertNotIn("deferredAt", row)

    def test_changed_hash_restarts_count_even_with_same_run_deferral(self):
        row = self.item(deferCount=3, deferredAt=drain.iso(self.started + 1))
        original = item_digest(row)
        row["note"] = "actual stage change"
        self.write("queue.json", {"PROJ-1": row})
        self.write("guard-state.json", {"runnable": {"PROJ-1": {"hash": original}}})
        result = self.drain_cli("defer", "--key", "PROJ-1", "--reason", "budget", "--now", str(self.started + 2))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["PROJ-1"]["deferCount"], 1)

    def test_rate_limit_backoff_progression_and_success_clear(self):
        self.write("queue.json", {"PROJ-1": self.item(), "PROJ-2": self.item()})
        for index, expected in enumerate((1800, 3600, 7200, 7200)):
            now = self.started + index * 10000
            self.write("running.json", {"token": "test", "started_at": now})
            result = self.drain_cli("rate-limit", "--key", "PROJ-1", "--now", str(now + 1))
            self.assertEqual(result.returncode, 0, result.stderr)
            queue = self.read("queue.json")
            row = queue["PROJ-1"]
            self.assertEqual(drain.epoch(row["backoffUntil"]) - now - 1, expected)
            self.assertEqual(row["deferReason"], "rate-limit")
            self.assertEqual(drain.rank(queue, now=now + 1), ["PROJ-2"])
        self.assertEqual(self.drain_cli("processed", "--key", "PROJ-1").returncode, 0)
        row = self.read("queue.json")["PROJ-1"]
        self.assertNotIn("backoffUntil", row)
        self.assertNotIn("backoffLevel", row)

    def test_result_or_stderr_rate_limit_is_infra_without_retry_or_secret_output(self):
        self.write("queue.json", {"PROJ-1": self.item()})
        result_path, stderr = self.state / "result.json", self.state / "stderr.txt"
        result_path.write_text('{"status":"done"}')
        stderr.write_text("private fixture: HTTP 429 overloaded")
        args = ("check-result", "--key", "PROJ-1", "--result", str(result_path), "--stderr", str(stderr))
        result = self.drain_cli(*args, "--now", str(self.started + 1))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"rateLimited": True, "retryAllowed": False,
                         "failureType": "infra", "deferReason": "rate-limit"})
        self.assertNotIn("private", result.stdout + result.stderr)
        row = self.read("queue.json")["PROJ-1"]
        self.assertTrue(drain.backoff_active(row, self.started + 1))
        result = self.drain_cli(*args, "--now", str(self.started + 2))
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = self.read("queue.json")["PROJ-1"]
        self.assertEqual(updated["backoffUntil"], row["backoffUntil"])
        self.assertEqual(updated["backoffLevel"], row["backoffLevel"])
        for text in ("rate limit", "RATE-LIMIT", "overloaded", "HTTP 429"):
            result_path.write_text(text)
            self.assertTrue(drain.rate_limited(result_path))
        result_path.write_text("PROJ-1429 passed")
        stderr.write_text("")
        self.assertFalse(drain.rate_limited(result_path))
        self.assertEqual(json.loads(self.drain_cli(*args).stdout), {"rateLimited": False, "retryAllowed": True})

    def test_same_run_expired_backoff_advances_at_boundary_and_after_expiry(self):
        self.write("running.json", {"token": "test", "started_at": 1000})
        for now in (2801, 2802):
            with self.subTest(now=now):
                self.write("queue.json", {"PROJ-1": self.item()})
                initial = self.drain_cli("rate-limit", "--key", "PROJ-1", "--now", "1001")
                self.assertEqual(initial.returncode, 0, initial.stderr)
                first = self.read("queue.json")["PROJ-1"]
                self.assertEqual(drain.epoch(first["backoffUntil"]), 2801)
                repeated = self.drain_cli("rate-limit", "--key", "PROJ-1", "--now", str(now))
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                row = self.read("queue.json")["PROJ-1"]
                self.assertEqual(row["backoffLevel"], 1)
                self.assertEqual(drain.epoch(row["backoffUntil"]), now + 3600)
                self.assertTrue(drain.backoff_active(row, now))

    def test_changed_content_does_not_expand_same_run_duplicate_rate_limit(self):
        row = self.item()
        self.write("queue.json", {"PROJ-1": row})
        self.write("guard-state.json", {"runnable": {"PROJ-1": {"hash": item_digest(row)}}})
        patch(self.state, "queue.json", ["PROJ-1"], {"note": "worker update"})
        for offset in (1, 2):
            result = self.drain_cli("rate-limit", "--key", "PROJ-1", "--now", str(self.started + offset))
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(result.stdout)["PROJ-1"]
            self.assertEqual(row["backoffLevel"], 0)
            self.assertEqual(drain.epoch(row["backoffUntil"]), self.started + 1 + 1800)

    def test_validation_rejects_corrupt_or_incomplete_queue(self):
        self.write("queue.json", {"PROJ-1": self.item()})
        self.assertEqual(self.drain_cli("validate-queue").returncode, 0)
        for queue in ({"PROJ-1": {"waiting": None}}, {"PROJ-1": self.item(waiting=12)},
                      {"PROJ-1": self.item(commentIds=[12])}, {"invalid": self.item()}):
            self.write("queue.json", queue)
            self.assertEqual(self.drain_cli("validate-queue").returncode, 2)
        (self.state / "queue.json").write_text("{broken")
        self.assertEqual(self.drain_cli("validate-queue").returncode, 2)

    def test_pending_focus_rank_and_invalid_json(self):
        self.write("queue.json", {"PROJ-1": self.item(), "PROJ-2": self.item()})
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-2"}\n')
        result = self.drain_cli("rank")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["PROJ-2", "PROJ-1"])
        (self.state / "pending-focus.jsonl").write_text("broken\n")
        self.assertEqual(self.drain_cli("rank").returncode, 2)

    def test_lost_owner_cannot_mutate_summary_or_queue(self):
        self.write("queue.json", {"PROJ-1": self.item()})
        original = (self.state / "queue.json").read_bytes()
        self.assertEqual(self.drain_cli("defer", "--key", "PROJ-1", "--reason", "budget", token="other").returncode, 1)
        self.assertEqual(self.drain_cli("summary-init", "--runnable", "1", "--lane-eligible", "0", token="other").returncode, 1)
        self.assertEqual((self.state / "queue.json").read_bytes(), original)
        self.assertFalse((self.state / "runs/other/summary.json").exists())
