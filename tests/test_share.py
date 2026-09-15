"""Tests for share_result.py: the anonymous result, the form link and --configure.

Run from the repository root:
    python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_deliverables import make_review  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "vc-review", "scripts", "share_result.py")
KEYS = ["version", "month", "stage", "depth", "models", "score", "corrected", "recommendation",
        "ai_usage", "hidden_text", "pilot", "paying", "arr", "funding", "round", "sector"]
LINK = ("https://docs.google.com/forms/d/e/1FAIpQLSexample/viewform?usp=pp_url&"
        + "&".join(f"entry.{1000 + i}={key}" for i, key in enumerate(KEYS)))


def run(*args):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    result = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                            encoding="utf-8", env=env)
    return result.returncode, result.stdout + result.stderr


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.review = os.path.join(self.tmp.name, "example-2026-01-01")
        os.makedirs(self.review)
        self.config = os.path.join(self.tmp.name, "share-config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_configure_then_share_builds_an_anonymous_prefilled_link(self):
        code, out = run("--configure", LINK, "--config", self.config)
        self.assertEqual(code, 0, out)
        make_review(self.review)
        code, out = run(self.review, "--config", self.config, "--pilot", "yes", "--paying", "no",
                        "--arr", "under EUR 100k", "--funding", "eur 500k-2m",
                        "--round", "EUR 1m-3m", "--sector", "deep-tech-and-hardware")
        self.assertEqual(code, 0, out)
        link = next(line for line in out.splitlines() if line.startswith("https://"))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
        self.assertEqual(query["usp"], "pp_url")
        answers = {key: query[f"entry.{1000 + i}"] for i, key in enumerate(KEYS)}
        self.assertEqual(answers["score"], "35")          # 36 to the nearest 5
        self.assertEqual(answers["corrected"], "50")      # 52 to the nearest 5
        self.assertEqual(answers["recommendation"], "pass")
        self.assertEqual(answers["ai_usage"], "8")
        self.assertEqual(answers["hidden_text"], "yes")
        self.assertEqual(answers["stage"], "seed")
        self.assertEqual(answers["models"], "judicious")
        self.assertEqual(answers["funding"], "EUR 500k-2m")
        self.assertEqual(answers["round"], "EUR 1m-3m")
        self.assertEqual(answers["sector"], "deep tech and hardware")
        self.assertRegex(answers["month"], r"^\d{4}-\d{2}$")
        self.assertNotIn("Example Robotics", out)
        with open(os.path.join(self.review, "anonymous-result.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(set(saved), set(KEYS))
        self.assertNotIn("Example", json.dumps(saved))

    def test_defaults_when_the_materials_do_not_say(self):
        make_review(self.review)
        code, out = run(self.review, "--config", self.config)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.review, "anonymous-result.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["pilot"], saved["arr"], saved["round"], saved["sector"]),
                         ("not stated", "not stated", "not stated", "other"))

    def test_unknown_label_is_refused_with_the_allowed_ones(self):
        make_review(self.review)
        code, out = run(self.review, "--config", self.config, "--round", "about a million")
        self.assertEqual(code, 2)
        self.assertIn("EUR 3m-10m", out)

    def test_without_a_form_it_prints_the_result_but_no_link(self):
        make_review(self.review)
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump({"form_url": "", "fields": {}}, fh)
        code, out = run(self.review, "--config", self.config)
        self.assertEqual(code, 0, out)
        self.assertIn("isn't set up", out)
        self.assertNotIn("https://", out)

    def test_configure_rejects_a_link_missing_answers(self):
        code, out = run("--configure", LINK.split("&entry.1015")[0], "--config", self.config)
        self.assertEqual(code, 2)
        self.assertIn("sector", out)
        self.assertFalse(os.path.exists(self.config))

    def test_an_unfinished_review_is_not_shared(self):
        make_review(self.review)
        os.remove(os.path.join(self.review, "memo.md"))
        code, out = run(self.review, "--config", self.config)
        self.assertEqual(code, 1)
        self.assertIn("Finish it before sharing", out)


if __name__ == "__main__":
    unittest.main()
