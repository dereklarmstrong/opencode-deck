"""Tests for the --demo synthetic dataset (opencode_deck/demo.py).

Guards: schema matches the real opencode.db, the build is deterministic per
seed, the dataset is rich enough to fill every dashboard panel, and nothing
that looks like a private identifier leaks into the generated rows.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from opencode_deck.demo import build_demo_db  # noqa: E402
from test_api import ServerClient  # noqa: E402

SESSION_COLS = [
    "id", "project_id", "workspace_id", "parent_id", "slug", "directory",
    "path", "title", "version", "share_url", "summary_additions",
    "summary_deletions", "summary_files", "summary_diffs", "metadata", "cost",
    "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read",
    "tokens_cache_write", "revert", "permission", "agent", "model",
    "time_created", "time_updated", "time_compacting", "time_archived",
]
MESSAGE_COLS = ["id", "session_id", "time_created", "time_updated", "data"]
PART_COLS = ["id", "message_id", "session_id", "time_created",
             "time_updated", "data"]

# Strings that must never appear in a public screenshot of demo data.
FORBIDDEN = ["orcafam", "192.168", "10.42", "derek", "linkedin", "github.com"]


def _rows(path):
    """Deterministic structural fingerprint of a demo DB (seed-stable)."""
    conn = sqlite3.connect(path)
    sessions = conn.execute(
        "SELECT title, directory, agent, model FROM session").fetchall()
    turns = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    parts = conn.execute("SELECT COUNT(*) FROM part").fetchone()[0]
    subs = conn.execute(
        "SELECT COUNT(*) FROM session WHERE parent_id IS NOT NULL").fetchone()[0]
    conn.close()
    return len(sessions), turns, parts, subs, sessions


class TestDemoBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p1 = os.path.join(self.tmp.name, "a.db")
        self.p2 = os.path.join(self.tmp.name, "b.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_matches_real_db(self):
        build_demo_db(self.p1)
        conn = sqlite3.connect(self.p1)
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(session)")],
                         SESSION_COLS)
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(message)")],
                         MESSAGE_COLS)
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(part)")],
                         PART_COLS)
        conn.close()

    def test_size_is_in_the_right_ballpark(self):
        s = build_demo_db(self.p1)
        self.assertGreaterEqual(s["sessions"], 100)
        self.assertLessEqual(s["sessions"], 200)
        self.assertGreaterEqual(s["turns"], 20000)
        self.assertLessEqual(s["turns"], 45000)
        self.assertGreaterEqual(s["tool_parts"], 5000)

    def test_deterministic_for_seed(self):
        build_demo_db(self.p1, seed=42)
        build_demo_db(self.p2, seed=42)
        self.assertEqual(_rows(self.p1), _rows(self.p2))

    def test_different_seed_changes_data(self):
        build_demo_db(self.p1, seed=42)
        build_demo_db(self.p2, seed=7)
        self.assertNotEqual(_rows(self.p1), _rows(self.p2))

    def test_no_private_strings(self):
        build_demo_db(self.p1)
        blob = " ".join(str(r) for r in _rows(self.p1)[4]).lower()
        for s in FORBIDDEN:
            self.assertNotIn(s, blob)


class TestDemoAggregation(unittest.TestCase):
    """Full build -> scan -> aggregate -> HTTP round trip on demo data."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "demo.db")
        build_demo_db(cls.db)
        cls.client = ServerClient(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def _all(self, qs=""):
        status, body = self.client.get(f"/api/all{qs}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_aggregate_fills_every_panel(self):
        d = self._all()
        self.assertGreater(d["n_sessions"], 100)
        self.assertGreater(d["n_turns"], 20000)
        self.assertGreater(len(d["models"]), 2)
        self.assertGreater(len(d["tools"]), 4)
        self.assertGreater(d["api_errors"], 100)
        self.assertGreaterEqual(d["n_subagents"], 10)
        self.assertGreaterEqual(d["context"]["compactions_total"], 8)
        self.assertGreater(len(d["insights"]), 0)
        self.assertGreater(len(d["top_sessions"]), 0)
        self.assertTrue(d["streaks"]["current"] >= 0)
        self.assertGreater(len(d["recent_in_progress"]), 0)

    def test_realistic_error_classes(self):
        d = self._all()
        joined = " ".join(e["error"] for e in d["api_errors_top"])
        self.assertTrue(any(c in joined for c in ("429", "502", "503", "504")),
                        joined)

    def test_night_owl_hourly_curve(self):
        d = self._all()
        # 7 weekday rows x 24 hours
        self.assertEqual(len(d["hourly"]), 7)
        self.assertTrue(all(len(row) == 24 for row in d["hourly"]))
        # night owl: evening (20:00-23:00) carries substantial activity
        evening = sum(sum(row[20:24]) for row in d["hourly"])
        self.assertGreater(evening, 100)

    def test_generic_directories_only(self):
        d = self._all()
        for p in d["projects"]:
            for bad in FORBIDDEN:
                self.assertNotIn(bad, p["dir"].lower())

    def test_windowed_days7(self):
        full = self._all()
        w = self._all("?days=7")
        self.assertLess(w["n_turns"], full["n_turns"])
        lo = (date.today() - timedelta(days=6)).isoformat()
        hi = date.today().isoformat()
        for day in w["daily"]:
            self.assertGreaterEqual(day["date"], lo)
            self.assertLessEqual(day["date"], hi)


class TestDemoCli(unittest.TestCase):
    def test_help_lists_demo(self):
        r = subprocess.run(
            [sys.executable, "-m", "opencode_deck.server", "--help"],
            capture_output=True, text=True, cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--demo", r.stdout)
        self.assertIn("synthetic", r.stdout)


if __name__ == "__main__":
    unittest.main()
