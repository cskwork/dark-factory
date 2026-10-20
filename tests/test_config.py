from factory.config import Config, load_config
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

    def test_invalid_values_rejected(self):
        for value in (-1, True, "one"):
            with self.assertRaises(ValueError):
                Config({"lock_stale_seconds": value}, self.config)
        with self.assertRaises(ValueError):
            Config({"triage": {"mode": "invalid"}}, self.config)
