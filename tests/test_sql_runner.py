import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factory import sql_runner as runner


class Cursor:
    def __init__(self, host="test-db-01", guard=0, fail_on=None):
        self.host, self.guard, self.fail_on = host, guard, fail_on
        self.executed, self.rowcount = [], 1

    def execute(self, sql):
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("statement failure")

    def fetchone(self):
        return (self.host if self.executed[-1] == "SELECT @@hostname" else self.guard,)

    def close(self):
        pass


class Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.committed = self.rolled = self.closed = False
        self.autocommit = True

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled = True

    def close(self):
        self.closed = True


class SQLRunnerTests(unittest.TestCase):
    def run_sql(self, cur=None, **changes):
        connection = Connection(cur or Cursor())
        args = dict(expect_host="test-db-", sql_text="UPDATE items SET n=1; INSERT INTO items VALUES (2)",
                    allowed_host_prefixes=["test-"], forbidden_host_substrings=["protected"],
                    host_query="SELECT @@hostname", guard_sql="SELECT COUNT(*) FROM items", guard_expect=0)
        args.update(changes)
        result = runner.run_connection(connection, **args)
        return result, connection

    def test_quoted_semicolons_and_comments(self):
        self.assertEqual(runner.statements("-- heading\nINSERT INTO t VALUES ('a;b'';c'); /* ; */ SELECT \"x;y\";"),
                         ["INSERT INTO t VALUES ('a;b'';c')", 'SELECT "x;y"'])

    def test_malformed_and_executable_sql_rejected(self):
        for text in ("SELECT 'oops", "SELECT 1 /*", "/*! DELETE FROM t */", "SELECT $$x$$", "DELIMITER x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                runner._validate(text)

    def test_order_host_guard_sql_commit_same_connection(self):
        result, conn = self.run_sql()
        self.assertEqual(result["status"], "done")
        self.assertEqual(conn.cur.executed, ["BEGIN", "SELECT @@hostname", "SELECT COUNT(*) FROM items",
                                             "UPDATE items SET n=1", "INSERT INTO items VALUES (2)"])
        self.assertTrue(conn.committed)
        self.assertFalse(conn.autocommit)

    def test_wrong_and_forbidden_hosts_roll_back(self):
        for host in ("other-db-01", "test-db-protected-01"):
            result, conn = self.run_sql(Cursor(host))
            self.assertEqual(result["status"], "refused")
            self.assertEqual(conn.cur.executed, ["BEGIN", "SELECT @@hostname"])
            self.assertTrue(conn.rolled)
            self.assertFalse(conn.committed)

    def test_unapproved_expected_prefix_refused_before_sql(self):
        result, conn = self.run_sql(expect_host="other-")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(conn.cur.executed, [])

    def test_guard_mismatch_rolls_back(self):
        result, conn = self.run_sql(Cursor(guard=1))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(len(conn.cur.executed), 3)
        self.assertTrue(conn.rolled)

    def test_driver_failure_is_failed_and_redacted(self):
        cursor = Cursor()
        cursor.execute = lambda statement: (_ for _ in ()).throw(ValueError("password=secret"))
        result, conn = self.run_sql(cursor)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", str(result))
        self.assertTrue(conn.rolled)

    def test_cleanup_failure_does_not_hide_commit(self):
        cursor = Cursor()
        cursor.close = lambda: (_ for _ in ()).throw(RuntimeError("password=secret"))
        result, conn = self.run_sql(cursor)
        self.assertEqual(result["status"], "done")
        self.assertTrue(conn.committed)
        self.assertIn("cleanup_error", result)
        self.assertNotIn("secret", str(result))

    def test_first_error_stops_and_rolls_back(self):
        result, conn = self.run_sql(Cursor(fail_on="UPDATE"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(s.startswith("INSERT") for s in conn.cur.executed))
        self.assertTrue(conn.rolled)
        self.assertFalse(conn.committed)

    def test_ddl_transaction_and_multistatement_guard_refused(self):
        for change in ({"sql_text": "ALTER TABLE items ADD n INT"}, {"sql_text": "COMMIT"},
                       {"guard_sql": "SELECT 1; DELETE FROM items"},
                       {"sql_text": "SELECT 1 INTO OUTFILE '/tmp/output'"}):
            result, conn = self.run_sql(**change)
            self.assertEqual(result["status"], "refused")
            self.assertEqual(conn.cur.executed, [])

    def test_config_allowlist_checked_before_import(self):
        cfg = SimpleNamespace(get=lambda *args: {"sql": {"allowed_config_names": ["test"]}})
        with patch.object(runner.importlib, "import_module") as importer:
            result = runner.run(cfg, config_name="unknown", sql_text="SELECT 1", expect_host="test-",
                                guard_sql="SELECT 0", guard_expect=0)
        self.assertEqual(result["status"], "refused")
        importer.assert_not_called()

    def test_connector_lifecycle(self):
        conn = Connection(Cursor())
        settings = {"sql": {"allowed_config_names": ["test"], "connector_module": "example_connector",
                             "configs": {"test": {"database": "test"}},
                             "expected_host_prefixes": ["test-"]}}
        cfg = SimpleNamespace(get=lambda *args: settings)
        with patch.object(runner.importlib, "import_module", return_value=SimpleNamespace(connect=lambda **kw: conn)):
            result = runner.run(cfg, config_name="test", sql_text="UPDATE items SET n=1", expect_host="test-",
                                guard_sql="SELECT 0", guard_expect=0)
        self.assertEqual(result["status"], "done")
        self.assertTrue(conn.closed)

    def test_real_sqlite_rollback_preserves_data(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE items (n INTEGER)")
        conn.execute("INSERT INTO items VALUES (1)")
        conn.commit()
        result = runner.run_connection(conn, expect_host="test-", allowed_host_prefixes=["test-"],
                                       forbidden_host_substrings=[], host_query="SELECT 'test-db-01'",
                                       guard_sql="SELECT COUNT(*) FROM items", guard_expect=1,
                                       sql_text="UPDATE items SET n=2; UPDATE missing SET n=3")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(conn.execute("SELECT n FROM items").fetchone(), (1,))
        conn.close()


if __name__ == "__main__":
    unittest.main()
