"""The weekly report must connect user proposals to the existing decision UI."""
from pathlib import Path
import unittest


class RetroDecisionTests(unittest.TestCase):
    def test_template_opens_recommended_choices_and_reuses_existing_requests(self):
        text = (Path(__file__).resolve().parents[1] / "templates/RETRO.template.md").read_text()
        for required in ("bin/decision-request", "--spec", "recommended", "recommendReason",
                         "existing open", "instead of creating a duplicate", "spec path",
                         "only after", "opens successfully", "never answers"):
            with self.subTest(required=required):
                self.assertIn(required, text)
