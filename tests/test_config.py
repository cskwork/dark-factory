from factory.config import Config, StateDirectoryError, load_config
from support import Fixture


class ConfigTests(Fixture):
    def test_paths_relative_to_config_and_project(self):
        cfg = Config({"project_root": "work", "state_dir": "state"}, self.config)
        self.assertEqual(cfg.project_root, self.root / "work")
        self.assertEqual(cfg.state_dir, self.root / "work/state")

    def test_template_substitution_is_single_argument(self):
        value = "space ; $(do-not-run) {literal}"
        self.assertEqual(self.cfg.command('tool --note "{note}"', note=value), ["tool", "--note", value])

    def test_load_toml(self):
        self.save_config()
        self.assertEqual(load_config(self.config).state_dir, self.state)

    def test_blank_state_directories_rejected(self):
        for value in ("", " ", "\t\n"):
            with self.subTest(value=value), self.assertRaisesRegex(StateDirectoryError, "state_dir"):
                Config({"state_dir": value}, self.config)

    def test_root_state_directories_and_aliases_rejected(self):
        root = self.root.parents[-1]
        root_link = self.root / "root-link"
        root_link.symlink_to(root, target_is_directory=True)
        relative_root = "../" * len(self.root.parts)
        for value in (str(root), "//", str(root) + "./", relative_root, str(root_link)):
            with self.subTest(value=value), self.assertRaisesRegex(StateDirectoryError, "state_dir.*root"):
                Config({"state_dir": value}, self.config)

    def test_valid_state_directories_preserve_resolution(self):
        for value, expected in (("state", self.state), (str(self.state), self.state),
                                (".", self.root), (" state ", self.root / " state ")):
            with self.subTest(value=value):
                self.assertEqual(Config({"state_dir": value}, self.config).state_dir, expected)
        self.assertEqual(Config({}, self.config).state_dir, self.root / ".factory")

    def test_cli_rejects_invalid_state_directories_without_side_effects(self):
        root_link = self.root / "root-link"
        root_link.symlink_to(self.root.parents[-1], target_is_directory=True)
        for value in ("", " \t", "/", str(root_link)):
            with self.subTest(value=value):
                self.data["state_dir"] = value
                # --owns is read-only even before validation, so the RED run is safe for /.
                result = self.cli("factory-guard", "--owns", "test-token")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(list(self.state.iterdir()), [])
                self.assertEqual(self.calls(), [])

    def test_invalid_values_rejected(self):
        for value in (-1, True, "one"):
            with self.assertRaises(ValueError):
                Config({"lock_stale_seconds": value}, self.config)
        with self.assertRaises(ValueError):
            Config({"triage": {"mode": "invalid"}}, self.config)
