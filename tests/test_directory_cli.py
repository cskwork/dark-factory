"""Unsafe configured state directories fail before any CLI state access."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io

from factory.config import StateDirectoryError, load_config
from support import Fixture


class StateDirectoryCliTests(Fixture):
    def check_directory(self, directory):
        self.data["state_dir"] = directory
        self.save_config()
        # Confirm rejection before invoking commands that could otherwise write at /.
        with self.assertRaises(StateDirectoryError):
            load_config(self.config)
        commands = [
            ("runtime", ["guard"], 2),
            ("runtime", ["guard", "--owns", "old"], 1),
            ("runtime", ["wake"], 2),
            ("runtime", ["worker", "builder", "title", "brief.md"], 2),
            ("idle", [], 2),
            ("state", ["patch", "queue.json", "--json", "{}"], 1),
            ("drain", ["validate-queue"], 2),
            ("decision_page", ["approval", "PROJ-123"], 2),
            ("check_base", ["--repo", str(self.root), "--branch", "ticket", "--key", "PROJ-123"], 2),
            ("check_deploy", ["--repo", str(self.root), "--job", "job", "--build", "1",
                              "--context", "dev", "--expect", "HEAD"], 2),
            ("check_migrations", [], 10),
            ("preflight", [], 2),
            ("ticket_refs", ["PROJ-123"], 2),
            ("comment_check", ["lint", "comment.md"], 2),
        ]
        for name, arguments, expected in commands:
            with self.subTest(module=name, arguments=arguments):
                module = importlib.import_module("factory." + name)
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    try:
                        code = module.main(["--config", str(self.config), *arguments])
                    except SystemExit as error:
                        code = error.code
                self.assertEqual(code, expected)
                self.assertIn("state_dir", output.getvalue() + errors.getvalue())
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.state.iterdir()), [])

    def test_empty_state_directory_is_clear_cli_error(self):
        self.check_directory("")

    def test_root_state_directory_is_clear_cli_error(self):
        self.check_directory("/")
