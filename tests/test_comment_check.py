"""Planner-readable comment gate before posting and after read-back, offline."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from factory import comment_check


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def para(text, marks=()):
    return {"type": "paragraph", "content": [{"type": "text", "text": text, "marks": [{"type": m} for m in marks]}]}


MEDIA = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "img-1", "alt": "after"}}]}
MENTION = {"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": "acct", "text": "@Planner"}}]}
PLAIN = doc(MENTION, para("The class report now shows every student. Please check it on the teacher screen."), MEDIA)


class CommentCheck(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_main(self, argv, stdin=""):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", out), \
                mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("FACTORY_CONFIG", None)
            code = comment_check.main(argv)
        return code, json.loads(out.getvalue())

    def write(self, name, value):
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return str(path)

    def rules(self, report):
        return {failure["rule"] for failure in report["failures"]}

    def test_planner_comment_passes(self):
        code, report = self.run_main(["lint", self.write("ok.json", PLAIN)])
        self.assertEqual((code, report["status"], report["failures"]), (0, "PASS", []))

    def test_developer_detail_is_rejected(self):
        body = doc(para("Merged fix/PROJ-12 at 3f9a2c1d, build #482 passed. listStudents in StudentMapper.xml "
                        "now joins USER_ID with SQL."), {"type": "codeBlock", "content": [para("SELECT 1")]},
                   para("see", marks=("code",)))
        code, report = self.run_main(["lint", self.write("dev.json", body)])
        self.assertEqual(code, 1)
        self.assertTrue({"commit hash", "build number", "branch name", "code name", "developer term",
                         "code block", "code format"} <= self.rules(report))

    def test_markdown_source_is_checked(self):
        code, report = self.run_main(["lint", self.write("c.md", "Fixed in `listStudents`.\n```sql\nSELECT 1\n```\n")])
        self.assertEqual(code, 1)
        self.assertTrue({"code format", "code block", "code name"} <= self.rules(report))
        code, report = self.run_main(["lint", self.write("ok.md", "@[Planner](acct) Fixed. ![after](a.png)")])
        self.assertEqual((code, report["media"], report["mentions"]), (0, 1, 1))

    def test_unconverted_markup_and_length(self):
        code, report = self.run_main(["lint", self.write("raw.json", doc(para("**Result** " + "word " * 300)))])
        self.assertEqual(code, 1)
        self.assertTrue({"unconverted markup", "length"} <= self.rules(report))

    def test_config_extends_terms_and_allow(self):
        config = self.write("factory.toml", '[comments]\nterms = ["쿼리"]\nallow = ["API"]\nmax_chars = 0\n')
        body = self.write("ko.json", doc(para("API 화면은 그대로이고 쿼리를 고쳤습니다.")))
        code, report = self.run_main(["--config", config, "lint", body])
        self.assertEqual((code, self.rules(report)), (1, {"developer term"}))
        self.assertEqual(report["failures"][0]["examples"], ["쿼리"])

    def test_readback_requires_media_and_mention(self):
        view = {"fields": {"comment": {"comments": [
            {"id": "101", "author": {"displayName": "Bot"}, "updated": "now", "body": doc(para("Done."))},
            {"id": "102", "author": {"displayName": "Bot"}, "updated": "now", "body": PLAIN}]}}}
        code, report = self.run_main(["readback", "--comment", "101", "--min-media", "1", "--min-mention", "1"],
                                     json.dumps(view))
        self.assertEqual((code, self.rules(report)), (1, {"media", "mention"}))
        code, report = self.run_main(["readback", "--comment", "102", "--min-media", "1", "--min-mention", "1"],
                                     json.dumps(view))
        self.assertEqual((code, report["author"], report["status"]), (0, "Bot", "PASS"))

    def test_file_card_is_not_an_image(self):
        card = {"type": "mediaGroup", "content": [{"type": "media", "attrs": {"type": "file", "id": "sql-1"}}]}
        view = {"fields": {"comment": {"comments": [{"id": "7", "body": doc(MENTION, para("Fixed."), card)}]}}}
        code, report = self.run_main(["readback", "--comment", "7", "--min-file", "1", "--min-media", "1"],
                                     json.dumps(view))
        self.assertEqual((code, self.rules(report), report["files"], report["media"]), (1, {"media"}, 1, 0))
        code, report = self.run_main(["lint", self.write("f.md", "Fixed.\n![for developers](101_check.sql)\n")])
        self.assertEqual((code, report["files"], report["media"]), (0, 1, 0))

    def test_readback_description_has_no_length_cap(self):
        view = {"fields": {"description": doc(para("word " * 400))}}
        code, report = self.run_main(["readback", "--description"], json.dumps(view))
        self.assertEqual(code, 0, report)

    def test_invalid_input_is_explicit(self):
        code, report = self.run_main(["readback", "--comment", "9"], json.dumps({"fields": {"comment": {}}}))
        self.assertEqual((code, report["status"]), (2, "FAIL"))
        code, report = self.run_main(["readback", "--comment", "9"], "not json")
        self.assertEqual(code, 2)
        code, report = self.run_main(["lint", self.write("bad.json", "{")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
