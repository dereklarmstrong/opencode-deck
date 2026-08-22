"""Fixture-based API tests for the opencode dashboard.

Builds a synthetic opencode.db in a temp dir (never touches the real one),
runs the server on an ephemeral port, and asserts aggregate values.
"""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opencode_deck import server  # noqa: E402


def _ts(dt):
    return int(dt.timestamp() * 1000)


def _now_local(*args):
    return datetime(*args, tzinfo=None)


def build_fixture(path):
    """3 real sessions + 1 today session; turns with tools, errors, compactions."""
    today = datetime.now()
    d1 = today - timedelta(days=3)
    d2 = today - timedelta(days=2)

    def at(day, h, m=0):
        return _ts(day.replace(hour=h, minute=m, second=0, microsecond=0))

    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE session (
      id TEXT, project_id TEXT, workspace_id TEXT, parent_id TEXT, slug TEXT,
      directory TEXT, path TEXT, title TEXT, version TEXT, share_url TEXT,
      summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
      summary_diffs TEXT, metadata TEXT, cost REAL,
      tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
      tokens_cache_read INTEGER, tokens_cache_write INTEGER,
      revert TEXT, permission TEXT, agent TEXT, model TEXT,
      time_created INTEGER, time_updated INTEGER, time_compacting INTEGER,
      time_archived INTEGER
    );
    CREATE TABLE message (
      id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
    );
    CREATE TABLE part (
      id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER,
      time_updated INTEGER, data TEXT
    );
    """)
    conn.execute("PRAGMA journal_mode=WAL")

    model = json.dumps({"id": "test-27b", "providerID": "vader", "variant": "default"})
    sessions = [
        ("s1", None, "/home/derek/blog", "Fixture blog work", "build", model, 500000, 12000, 3000, 5000, 50, at(d1, 21, 0), at(d1, 21, 30)),
        ("s2", "s1", "/home/derek/blog", "Fixture subagent", "general", model, 10000, 500, 100, 0, 0, at(d1, 21, 10), at(d1, 21, 20)),
        ("s3", None, "/home/derek/projects", "Fixture project", "Assistant", model, 20000, 900, 200, 0, 0, at(d2, 9, 0), at(d2, 9, 45)),
        ("s4", None, "/home/derek", "Fixture today", "build", model, 1000, 50, 0, 0, 0, at(today, 8, 0), at(today, 8, 5)),
    ]
    for sid, parent, d, title, agent, m, ti, to, tr, tc, tw, t0, t1 in sessions:
        conn.execute(
            "INSERT INTO session (id, parent_id, directory, title, agent, model, "
            "cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, "
            "tokens_cache_write, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, parent, d, title, agent, m, 0.0, ti, to, tr, tc, tw, t0, t1),
        )

    # ---- messages (turns) ---------------------------------------------------
    # T1: s1, 21:00, 30s turn, 100 out tokens. Tools: bash 2s + bash 1s => pure 27s => ~3.7 tps
    t1 = at(d1, 21, 0)
    conn.execute("INSERT INTO message VALUES ('m1', 's1', ?, ?, ?)",
                 (t1, t1, json.dumps({
                     "role": "assistant", "agent": "build",
                     "modelID": "test-27b", "providerID": "vader",
                     "tokens": {"input": 12000, "output": 100, "reasoning": 50,
                                "cache": {"read": 8000, "write": 0}},
                     "cost": 0, "time": {"created": t1, "completed": t1 + 30000},
                 })))
    # T2: s1, 21:10, completed missing
    t2 = at(d1, 21, 10)
    conn.execute("INSERT INTO message VALUES ('m2', 's1', ?, ?, ?)",
                 (t2, t2, json.dumps({
                     "role": "assistant", "agent": "build",
                     "modelID": "test-27b", "providerID": "vader",
                     "tokens": {"input": 13000, "output": 20, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}},
                     "time": {"created": t2},
                 })))
    # T3: s3, 9:00, 2s turn with 1.5s tool time => pure 0.5s => excluded from tps
    t3 = at(d2, 9, 0)
    conn.execute("INSERT INTO message VALUES ('m3', 's3', ?, ?, ?)",
                 (t3, t3, json.dumps({
                     "role": "assistant", "agent": "Assistant",
                     "modelID": "test-27b", "providerID": "vader",
                     "tokens": {"input": 70000, "output": 15, "reasoning": 0,
                                "cache": {"read": 100, "write": 0}},
                     "time": {"created": t3, "completed": t3 + 2000},
                 })))
    # T4: s3, 9:10, errored
    t4 = at(d2, 9, 10)
    conn.execute("INSERT INTO message VALUES ('m4', 's3', ?, ?, ?)",
                 (t4, t4, json.dumps({
                     "role": "assistant", "agent": "Assistant",
                     "modelID": "test-27b", "providerID": "vader",
                     "tokens": {"input": 1000, "output": 0, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}},
                     "error": {"name": "APIError", "data": {
                         "message": "not found", "statusCode": 404,
                         "metadata": {"url": "http://192.0.2.42:4242/chat/completions"},  # TEST-NET-1 (RFC 5737)
                     }},
                     "time": {"created": t4, "completed": t4 + 500},
                 })))
    # T5: user message — must not count
    conn.execute("INSERT INTO message VALUES ('m5', 's1', ?, ?, ?)",
                 (t1, t1, json.dumps({"role": "user", "agent": "build", "time": {"created": t1}})))
    # T6: today's turn (s4) — needed for current streak
    t6 = at(today, 8, 0)
    conn.execute("INSERT INTO message VALUES ('m7', 's4', ?, ?, ?)",
                 (t6, t6, json.dumps({
                     "role": "assistant", "agent": "build",
                     "modelID": "test-27b", "providerID": "vader",
                     "tokens": {"input": 400, "output": 50, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}},
                     "time": {"created": t6, "completed": t6 + 800},  # pure 0.8s -> tps excluded
                 })))
    # T7: malformed JSON — must be skipped
    conn.execute("INSERT INTO message VALUES ('m6', 's1', ?, ?, ?)",
                 (t1, t1, "{not json"))

    # ---- parts ---------------------------------------------------------------
    def _part(pid, mid, sid, t0, t1, data):
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, sid, t0, t1, json.dumps(data)))
    _part("p1", "m1", "s1", t1, t1 + 3000, {
        "type": "tool", "tool": "bash", "callID": "c1",
        "state": {"status": "completed", "time": {"start": t1 + 1000, "end": t1 + 3000}}})
    _part("p2", "m1", "s1", t1, t1 + 5000, {
        "type": "tool", "tool": "bash", "callID": "c2",
        "state": {"status": "completed", "time": {"start": t1 + 4000, "end": t1 + 5000}}})
    _part("p3", "m3", "s3", t3, t3 + 1700, {
        "type": "tool", "tool": "read", "callID": "c3",
        "state": {"status": "completed", "time": {"start": t3 + 200, "end": t3 + 1700}}})
    _part("p4", "m4", "s3", t4, t4, {
        "type": "tool", "tool": "webfetch", "callID": "c4",
        "state": {"status": "error", "time": {}}})
    _part("p5", "m2", "s1", t2, t2, {
        "type": "tool", "tool": "glob", "callID": "c5",
        "state": {"status": "pending", "time": {}}})
    _part("p6", "mX", "s1", t1, t1 + 60000, {
        "type": "tool", "tool": "bash", "callID": "c6",
        "state": {"status": "completed", "time": {"start": t1, "end": t1 + 60000}}})
    _part("p7", "m1", "s1", t1 + 60000, t1 + 60000, {"type": "compaction"})
    conn.commit()
    conn.close()
    return {"today": today, "d1": d1, "d2": d2, "s1": "s1"}


def build_empty(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE session (
      id TEXT, project_id TEXT, workspace_id TEXT, parent_id TEXT, slug TEXT,
      directory TEXT, path TEXT, title TEXT, version TEXT, share_url TEXT,
      summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
      summary_diffs TEXT, metadata TEXT, cost REAL,
      tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
      tokens_cache_read INTEGER, tokens_cache_write INTEGER,
      revert TEXT, permission TEXT, agent TEXT, model TEXT,
      time_created INTEGER, time_updated INTEGER, time_compacting INTEGER,
      time_archived INTEGER
    );
    CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
    CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    conn.commit()
    conn.close()


class ServerClient:
    def __init__(self, db_path):
        server._cache.update({"db_path": db_path, "key": None, "ts": 0.0, "data": None})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = srv.server_address[1]
        self.thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self.thread.start()
        self.srv = srv

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body

    def post(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "opencode.db")
        build_fixture(cls.db)
        cls.client = ServerClient(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def _all(self):
        status, body = self.client.get("/api/all")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_index_html(self):
        status, body = self.client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("opencode", body.lower())

    def test_chartjs(self):
        status, body = self.client.get("/chart.umd.min.js")
        self.assertEqual(status, 200)
        self.assertIn("Chart", body[:5000] or body)

    def test_kpis(self):
        d = self._all()
        self.assertEqual(d["n_sessions"], 4)
        self.assertEqual(d["n_subagents"], 1)
        self.assertEqual(d["n_turns"], 5)  # m6 malformed + m5 user excluded
        self.assertEqual(d["api_errors"], 1)
        # session tokens: s1 520050 (+5050 cache) + s2 10600 + s3 21100 + s4 1050
        self.assertEqual(d["total_tokens"], 552800)
        self.assertEqual(d["total_out"], 185)  # 100 + 20 + 15 + 0 + 50
        self.assertEqual(d["total_in"], 96400)  # 12000 + 13000 + 70000 + 1000 + 400

    def test_streaks(self):
        d = self._all()
        # days: d1 (3d ago), d2 (2d ago), today => longest >= 2
        self.assertGreaterEqual(d["streaks"]["longest"], 2)
        self.assertGreaterEqual(d["streaks"]["current"], 1)

    def test_hourly(self):
        d = self._all()
        today = datetime.now()
        w = (today.weekday())
        self.assertEqual(d["hourly"][w][8], 1)  # T of s4 at 08:00

    def test_daily(self):
        d = self._all()
        dates = [r["date"] for r in d["daily"]]
        self.assertEqual(dates, sorted(dates))
        tot = sum(r["turns"] for r in d["daily"])
        self.assertEqual(tot, d["n_turns"])

    def test_agents(self):
        d = self._all()
        by = {a["agent"]: a for a in d["agents"]}
        self.assertEqual(by["build"]["sessions"], 2)
        self.assertEqual(by["build"]["subagents"], 0)  # s2's parent is s1, but s2 agent=general
        self.assertEqual(by["general"]["sessions"], 1)
        self.assertEqual(by["general"]["subagents"], 1)
        self.assertEqual(by["Assistant"]["sessions"], 1)

    def test_models_and_tps(self):
        d = self._all()
        self.assertEqual(len(d["models"]), 1)
        m = d["models"][0]
        self.assertEqual(m["label"], "test-27b @ vader")
        self.assertEqual(m["turns"], 5)
        # only T1 qualifies for tps: pure = 30s - 3s = 27s, out=100 -> ~3.7
        self.assertEqual(m["tps_samples"], 1)
        self.assertAlmostEqual(m["tps_median"], 100 / 27, places=1)

    def test_tools(self):
        d = self._all()
        by = {t["tool"]: t for t in d["tools"]}
        # p1 (2s) + p2 (1s) + p6 (60s, orphaned part whose turn is gone) = 3 calls
        self.assertEqual(by["bash"]["calls"], 3)
        self.assertEqual(by["bash"]["errors"], 0)
        self.assertAlmostEqual(by["bash"]["wall_s"], 63.0, places=1)
        self.assertEqual(by["webfetch"]["errors"], 1)
        self.assertIsNone(by["webfetch"]["p50_s"])
        self.assertEqual(by["glob"]["calls"], 1)

    def test_context(self):
        d = self._all()
        cx = d["context"]
        self.assertEqual(cx["compactions_total"], 1)
        # bloat: only m3 (70k) >= 64k of 5 turns => 20%
        self.assertEqual(cx["bloat_pct"], 20.0)
        # 6 bins [0-8k, 8-16k, 16-32k, 32-64k, 64-128k, 128k+]:
        # T4(1000)+T7(400)->0, T1(12000)+T2(13000)->1, T3(70000)->4
        self.assertEqual(cx["histogram"]["values"], [2, 2, 0, 0, 1, 0])

    def test_api_errors(self):
        d = self._all()
        self.assertEqual(len(d["api_errors_top"]), 1)
        self.assertEqual(d["api_errors_top"][0]["count"], 1)
        self.assertIn("APIError", d["api_errors_top"][0]["error"])
        self.assertIn("404", d["api_errors_top"][0]["error"])
        self.assertIn("192.0.2.42:4242", d["api_errors_top"][0]["error"])

    def test_top_sessions(self):
        d = self._all()
        top = {s["id"]: s for s in d["top_sessions"]}
        self.assertEqual(d["top_sessions"][0]["id"], "s1")
        self.assertEqual(top["s1"]["turns"], 2)
        self.assertEqual(top["s3"]["errors"], 1)

    def test_insights(self):
        d = self._all()
        self.assertTrue(len(d["insights"]) >= 3)
        joined = " | ".join(d["insights"])
        self.assertIn("Night-owl", joined)
        self.assertIn("Delegation", joined)
        self.assertTrue(any(k in joined for k in ("APIError", "Flakiest")))

    def test_health(self):
        status, body = self.client.get("/api/health")
        h = json.loads(body)
        self.assertTrue(h["ok"])
        self.assertEqual(h["rows"]["turns"], 5)

    def test_refresh(self):
        status, body = self.client.post("/api/refresh")
        r = json.loads(body)
        self.assertTrue(r["ok"])
        self.assertIn("scan_ms", r)

    def test_404(self):
        status, _ = self.client.get("/nope")
        self.assertEqual(status, 404)
        status, _ = self.client.post("/nope")
        self.assertEqual(status, 404)


class TestEmptyDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "empty.db")
        build_empty(self.db)
        self.client = ServerClient(self.db)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_empty(self):
        status, body = self.client.get("/api/all")
        d = json.loads(body)
        self.assertEqual(d["n_sessions"], 0)
        self.assertEqual(d["n_turns"], 0)
        self.assertEqual(d["total_tokens"], 0)
        self.assertEqual(d["streaks"], {"current": 0, "longest": 0})
        self.assertIsNone(d["median_turn_s"])
        self.assertEqual(len(d["insights"]), 1)
        self.assertEqual(d["tools"], [])
        self.assertEqual(d["models"], [])
        self.assertEqual(d["context"]["bloat_pct"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
