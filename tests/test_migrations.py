import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from factory import check_migrations as migrations
from factory.config import Config


class Connection:
    def __init__(self, host="test-db-01", present=True, fail=False):
        self.host, self.present, self.fail = host, present, fail
        self.executed = []
        self.closed = self.cursor_closed = self.committed = False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.fail:
            raise RuntimeError("secret-password")

    def fetchone(self):
        if self.executed[-1][0] == "SELECT @@hostname":
            return (self.host,)
        return (1,) if self.present else None

    def commit(self):
        self.committed = True
        raise AssertionError("read-only verifier must never commit")

    def close(self):
        # The fake doubles as a cursor and connection.
        if self.cursor_closed:
            self.closed = True
        self.cursor_closed = True


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config({"migrations": {"sql_config": "dev", "expect_host": "test-db-",
                                         "default_schema": "app"},
                           "decisions": {"sql": {"connector_module": "fake_driver",
                             "allowed_config_names": ["dev"], "configs": {"dev": {}},
                             "expected_host_prefixes": ["test-"],
                             "forbidden_host_substrings": ["protected"]}}}, "/tmp/factory.toml")
        self.files = {"db/001.sql": "CREATE TABLE things (id BIGINT PRIMARY KEY, note VARCHAR(20));"}
        self.git_calls = []

    def git(self, repo, stage, *args):
        self.git_calls.append((repo, stage, args))
        if stage.startswith("resolve-"):
            return ("a" if stage == "resolve-base" else "b") * 40 + "\n"
        if stage == "migration-files":
            return "\0".join(self.files) + ("\0" if self.files else "")
        if stage == "migration-content":
            return self.files[args[-1].split(":", 1)[1]]
        raise AssertionError(stage)

    def check(self, conn=None, **kwargs):
        conn = conn or Connection()
        calls = []

        def connect(module, settings):
            calls.append((module, settings))
            return conn

        report, code = migrations.check(self.cfg, git=self.git, connect=connect, **kwargs)
        return report, code, conn, calls

    def test_applied_is_read_only_and_uses_same_connection(self):
        report, code, conn, calls = self.check()
        self.assertEqual((code, report["status"]), (0, "PASS"))
        self.assertEqual(calls, [("fake_driver", {})])
        self.assertEqual(conn.executed[0], ("SELECT @@hostname", None))
        self.assertTrue(all(sql.startswith("SELECT ") for sql, _ in conn.executed))
        self.assertEqual(len(conn.executed), 4)  # host, table, two columns
        self.assertTrue(conn.closed)
        self.assertFalse(conn.committed)

    def test_missing_is_failure_with_object_identity(self):
        report, code, _, _ = self.check(Connection(present=False))
        self.assertEqual((code, report["status"]), (1, "FAIL"))
        self.assertIn({"kind": "table", "schema": "app", "table": "things"}, report["missing"])

    def test_no_migrations_needs_no_db_configuration(self):
        self.files = {}
        self.cfg.data.pop("decisions")
        report, code, conn, calls = self.check()
        self.assertEqual((code, report["reason"], calls, conn.executed),
                         (0, "no-relevant-migrations", [], []))

    def test_defaults_and_overrides_are_used_by_git(self):
        self.check()
        self.assertEqual(self.git_calls[0][2][-1], "origin/main^{commit}")
        self.assertEqual(self.git_calls[1][2][-1], "HEAD^{commit}")
        self.assertEqual(self.git_calls[2][2][-1], ":(glob)**/*.sql")
        self.cfg.data["migrations"].update(repo="service", base="stable", head="ticket", glob="db/*.sql")
        self.git_calls.clear()
        self.check()
        self.assertEqual(self.git_calls[0][0], self.cfg.project_root / "service")
        self.assertEqual(self.git_calls[0][2][-1], "stable^{commit}")
        self.assertEqual(self.git_calls[1][2][-1], "ticket^{commit}")
        self.assertEqual(self.git_calls[2][2][-1], ":(glob)db/*.sql")
        self.git_calls.clear()
        self.check(repo="other", base="override-base", head="override-head")
        self.assertEqual(self.git_calls[0][0], self.cfg.project_root / "other")
        self.assertEqual(self.git_calls[0][2][-1], "override-base^{commit}")

    def test_use_prefixes_and_alter_add_columns(self):
        self.files = {"db/001.sql": """USE `school`;
          CREATE TABLE IF NOT EXISTS `items` (`id` BIGINT, PRIMARY KEY (`id`)) ENGINE=InnoDB;
          ALTER TABLE `audit`.`events` ADD COLUMN `code` VARCHAR(30) AFTER `id`,
             ADD `kind` INT DEFAULT 0;
          ALTER TABLE items ADD (name VARCHAR(20), age INT);
        """}
        report, code, _, _ = self.check()
        self.assertEqual(code, 0, report)
        self.assertIn({"kind": "column", "schema": "audit", "table": "events", "column": "kind"},
                      report["requirements"])
        self.assertIn({"kind": "column", "schema": "school", "table": "items", "column": "name"},
                      report["requirements"])

    def test_unknown_or_malformed_sql_never_connects(self):
        examples = ["DROP TABLE things", "ALTER TABLE things DROP COLUMN x",
                    "CREATE TABLE things AS SELECT 1", "CREATE TABLE things (id WHATTYPE)",
                    "CREATE TABLE things (id INT", "CREATE TABLE things (id INT); UPDATE things SET id=2",
                    "ALTER TABLE things ADD INDEX ix (id)", "/*! CREATE TABLE things (id INT) */",
                    "CREATE TABLE things (id INT) PARTITION BY HASH (id) PARTITIONS 2",
                    "ALTER TABLE things ADD x INT, RENAME TO other", "USE app",
                    "CREATE TABLE things (id INT, id INT)", "CREATE TABLE things (id INT('bad'))",
                    "CREATE TABLE things (id ENUM(2))", "CREATE TABLE things (id VARCHAR)"]
        for sql in examples:
            with self.subTest(sql=sql):
                self.files = {"db/001.sql": sql}
                report, code, conn, calls = self.check()
                self.assertEqual((code, report["status"], calls, conn.executed), (3, "HUMAN", [], []))

    def test_missing_schema_needs_human_review(self):
        self.cfg.data["migrations"].pop("default_schema")
        report, code, _, calls = self.check()
        self.assertEqual((code, calls), (3, []))

    def test_database_config_provides_default_schema_and_explicit_value_wins(self):
        self.cfg.data["migrations"].pop("default_schema")
        options = self.cfg.data["decisions"]["sql"]["configs"]["dev"]
        for key in ("database", "dbname"):
            with self.subTest(key=key):
                options.clear()
                options[key] = "service_db"
                report, code, _, _ = self.check()
                self.assertEqual(code, 0, report)
                self.assertEqual({item["schema"] for item in report["requirements"]}, {"service_db"})
        self.cfg.data["migrations"]["default_schema"] = "explicit_db"
        report, code, _, _ = self.check()
        self.assertEqual(code, 0, report)
        self.assertEqual({item["schema"] for item in report["requirements"]}, {"explicit_db"})

    def test_unsafe_host_query_never_connects(self):
        for query in ("SELECT 1 INTO OUTFILE '/tmp/write'", "SELECT do_write()", "SELECT @@hostname FOR SHARE",
                      "SELECT @@hostname; DELETE FROM items", "SELECT `do_write`()", 'SELECT "do_write"()'):
            with self.subTest(query=query):
                self.cfg.data["decisions"]["sql"]["host_query"] = query
                report, code, _, calls = self.check()
                self.assertEqual((code, calls), (10, []))

    def test_host_identity_query_can_return_database_and_version(self):
        self.cfg.data["decisions"]["sql"]["host_query"] = "SELECT @@hostname, DATABASE(), VERSION()"
        conn = Connection()
        original = conn.fetchone
        conn.fetchone = lambda: ("test-db-01", "app", "8.0") if len(conn.executed) == 1 else original()
        report, code, _, _ = self.check(conn)
        self.assertEqual(code, 0, report)

    def test_custom_driver_module_receives_allowlisted_options(self):
        conn = Connection()
        with patch.object(migrations.importlib, "import_module") as importer:
            importer.return_value.connect.return_value = conn
            report, code = migrations.check(self.cfg, git=self.git)
        self.assertEqual(code, 0, report)
        importer.assert_called_once_with("fake_driver")
        importer.return_value.connect.assert_called_once_with()
        self.assertTrue(conn.closed)

    def test_wrong_or_forbidden_host_runs_no_schema_queries(self):
        for host in ("prod-db-01", "test-db-protected-01"):
            report, code, conn, _ = self.check(Connection(host=host))
            self.assertEqual((code, report["status"]), (10, "ERROR"))
            self.assertEqual(conn.executed, [("SELECT @@hostname", None)])
            self.assertTrue(conn.closed)

    def test_disallowed_config_and_expect_host_fail_before_connect(self):
        for name, value in (("sql_config", "prod"), ("expect_host", "prod-")):
            with self.subTest(name=name):
                old = self.cfg.data["migrations"][name]
                self.cfg.data["migrations"][name] = value
                report, code, _, calls = self.check()
                self.assertEqual((code, calls), (10, []))
                self.cfg.data["migrations"][name] = old

    def test_database_errors_hide_driver_text(self):
        report, code, conn, _ = self.check(Connection(fail=True))
        self.assertEqual(code, 10)
        self.assertNotIn("secret-password", json.dumps(report))
        self.assertTrue(conn.closed)

    def test_git_errors_return_environment_error(self):
        with patch.object(migrations, "git_command", side_effect=RuntimeError("secret")):
            report, code = migrations.check(self.cfg)
        self.assertEqual(code, 10)
        self.assertNotIn("secret", json.dumps(report))

    def test_cli_json_and_exit_code(self):
        with patch.object(migrations, "load_config", return_value=self.cfg), \
             patch.object(migrations, "check", return_value=({"status": "HUMAN"}, 3)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(migrations.main(["--config", "/tmp/config.toml"]), 3)
        self.assertEqual(json.loads(output.getvalue()), {"status": "HUMAN"})

    def test_real_local_git_uses_committed_blobs_and_added_modified_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.run(["git", "-C", tmp, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()

            git("init")
            git("config", "user.email", "offline@example.invalid")
            git("config", "user.name", "Offline Test")
            (root / "old.sql").write_text("CREATE TABLE old (id INT);")
            (root / "deleted.sql").write_text("DROP DATABASE dangerous;")
            git("add", ".")
            git("commit", "-m", "baseline")
            base = git("rev-parse", "HEAD")
            (root / "old.sql").write_text("CREATE TABLE old (id INT, value INT);")
            (root / "deleted.sql").unlink()
            (root / "added.sql").write_text("ALTER TABLE old ADD extra INT;")
            git("add", ".")
            git("commit", "-m", "migrations")
            (root / "old.sql").write_text("DROP DATABASE must_not_read_worktree;")
            conn = Connection()
            report, code = migrations.check(self.cfg, repo=tmp, base=base, head="HEAD",
                                             connect=lambda module, settings: conn)
            self.assertEqual(code, 0, report)
            self.assertEqual(set(report["migrations"]), {"old.sql", "added.sql"})


if __name__ == "__main__":
    unittest.main()
