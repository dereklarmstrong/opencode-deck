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
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta
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
        self.assertIn("performance hud", body)
        self.assertIn("c-hud", body)

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

    def test_recent_tps_hud(self):
        d = self._all()
        rt = d["recent_tps"]
        # only T1 qualifies (T2 in-progress, T3/T6 under 1s pure, T4 zero output)
        self.assertEqual(len(rt), 1)
        self.assertAlmostEqual(rt[0]["tps"], 100 / 27, places=1)
        self.assertEqual(rt[0]["out"], 100)
        self.assertEqual(rt[0]["model"], "test-27b")
        self.assertEqual(rt[0]["end"], "test-27b @ vader")
        self.assertGreaterEqual(rt[0]["dur"], 26.9)
        self.assertLessEqual(rt[0]["dur"], 27.1)
        self.assertAlmostEqual(d["recent_tps_avg10"], 100 / 27, places=1)
        self.assertAlmostEqual(d["median_tps"], 100 / 27, places=1)
        ip = d["recent_in_progress"]
        self.assertIsNotNone(ip)
        self.assertEqual(ip["model"], "test-27b")
        self.assertEqual(ip["end"], "test-27b @ vader")
        self.assertGreater(ip["started"], 0)

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
        self.assertEqual(d["recent_tps"], [])
        self.assertIsNone(d["recent_tps_avg10"])
        self.assertIsNone(d["median_tps"])
        self.assertIsNone(d["recent_in_progress"])
        self.assertEqual(len(d["insights"]), 1)
        self.assertEqual(d["tools"], [])
        self.assertEqual(d["models"], [])
        self.assertEqual(d["context"]["bloat_pct"], 0.0)


class TestAggregateCache(unittest.TestCase):
    """/api/all serves repeat ranges from the aggregate LRU (cycle-2 perf).

    aggregate() is pure over the raw scan, so the handler's aggregate_cached
    memoizes per (start_ms, end_ms); a fresh raw scan (get_cache rescan)
    clears the LRU. We watch server.aggregate with a wrapping mock — a cache
    hit must not call it again.
    """

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

    def setUp(self):
        # module-level LRU is shared across tests: give every test a
        # deterministic empty baseline for aggregate-call-count assertions
        server._agg.clear()

    def test_repeat_range_served_from_cache(self):
        with mock.patch.object(server, "aggregate", wraps=server.aggregate) as m:
            _, b1 = self.client.get("/api/all?days=7")
            _, b2 = self.client.get("/api/all?days=7")
        self.assertEqual(b1, b2)
        self.assertEqual(m.call_count, 1, "second identical range should hit the LRU")

    def test_distinct_ranges_are_distinct_entries(self):
        with mock.patch.object(server, "aggregate", wraps=server.aggregate) as m:
            self.client.get("/api/all")          # (None, None)
            self.client.get("/api/all?days=7")   # (start7, end7)
            self.client.get("/api/all?days=30")  # (start30, end30)
        self.assertEqual(m.call_count, 3)

    def test_lru_eviction_bound(self):
        raw = server.get_cache()  # warms the raw cache; get_cache clears _agg on rescan
        old_max = server.AGG_CACHE_MAX
        try:
            server.AGG_CACHE_MAX = 2
            for i in range(4):
                server.aggregate_cached(raw, i, i + 1)
            self.assertLessEqual(len(server._agg), 2)
            # most recent two survive, oldest evicted
            self.assertIn((3, 4), server._agg)
            self.assertNotIn((0, 1), server._agg)
        finally:
            server.AGG_CACHE_MAX = old_max

    def test_rescan_invalidates_aggregate_cache(self):
        # Resize the debounce floor to 0 so the intra-test data change is seen
        # immediately (the production floor is RESCAN_MIN_S; exercised separately
        # in test_debounce_holds_scan_until_floor).
        with mock.patch.object(server, "RESCAN_MIN_S", 0):
            with mock.patch.object(server, "aggregate", wraps=server.aggregate) as m:
                _, b1 = self.client.get("/api/all?days=90")
            self.assertEqual(m.call_count, 1)
            n1 = json.loads(b1)["n_turns"]
            # add a turn: the content signature (message count) changes, so the
            # next get_cache must rescan — mtime is no longer consulted
            t = _ts((datetime.now() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0))
            conn = sqlite3.connect(self.db)
            conn.execute(
                "INSERT INTO message VALUES (?, 's1', ?, ?, ?)",
                ("mCacheExtra", t, t, json.dumps({
                    "role": "assistant", "agent": "build",
                    "modelID": "test-27b", "providerID": "vader",
                    "tokens": {"input": 0, "output": 0, "reasoning": 0,
                               "cache": {"read": 0, "write": 0}},
                    "time": {"created": t, "completed": t + 500},
                })),
            )
            conn.commit()
            conn.close()
            with mock.patch.object(server, "aggregate", wraps=server.aggregate) as m2:
                _, b2 = self.client.get("/api/all?days=90")
        # rescan cleared the LRU: aggregate ran again, and the new turn shows up
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(json.loads(b2)["n_turns"], n1 + 1)

    def test_mtime_churn_does_not_rescan(self):
        """Self-contained: prove the gate is content, not mtime.

        A cold client must scan once; then churning only the db/wal/shm mtimes
        (no data change) must NOT trigger a second scan. This is the core
        fix — opencode's live WAL churns mtime on every commit.

        Uses a throwaway DB + client; save/restore the module-global _cache
        so sibling tests in this class see the class fixture again.
        """
        saved_cache = dict(server._cache)
        tmp = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(tmp.name, "opencode.db")
            build_fixture(db)
            client = ServerClient(db)
            with mock.patch.object(server, "scan", wraps=server.scan) as m:
                status, _ = client.get("/api/all?days=7")
                self.assertEqual(status, 200)
                first = m.call_count
                self.assertGreaterEqual(first, 1, "cold client should scan once")
                # churn mtime only (no content change)
                st = os.stat(db)
                os.utime(db, (st.st_atime + 5, st.st_mtime + 5))
                for sfx in ("-wal", "-shm"):
                    p = db + sfx
                    if os.path.exists(p):
                        s2 = os.stat(p)
                        os.utime(p, (s2.st_atime + 5, s2.st_mtime + 5))
                status2, _ = client.get("/api/all?days=7")
                self.assertEqual(status2, 200)
            self.assertEqual(m.call_count, first, "mtime churn must not trigger a rescan")
            client.close()
        finally:
            tmp.cleanup()
            server._cache.update(saved_cache)

    def test_debounce_holds_scan_until_floor(self):
        """A signature change within RESCAN_MIN_S holds the scan (bounded
        staleness); once the floor elapses, the same pending change rescans.
        """
        saved_cache = dict(server._cache)
        tmp = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(tmp.name, "opencode.db")
            build_fixture(db)
            client = ServerClient(db)
            with mock.patch.object(server, "RESCAN_MIN_S", 3600):
                with mock.patch.object(server, "scan", wraps=server.scan) as m:
                    status, body = client.get("/api/all?days=7")
                    self.assertEqual(status, 200)
                    first = m.call_count
                    self.assertGreaterEqual(first, 1)
                    n1 = json.loads(body)["n_turns"]
                    t = _ts(datetime.now().replace(hour=10, minute=0, second=0, microsecond=0))
                    conn = sqlite3.connect(db)
                    conn.execute(
                        "INSERT INTO message VALUES (?, 's1', ?, ?, ?)",
                        ("mDeb", t, t, json.dumps({
                            "role": "assistant", "agent": "build",
                            "modelID": "test-27b", "providerID": "vader",
                            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                       "cache": {"read": 0, "write": 0}},
                            "time": {"created": t, "completed": t + 100},
                        })),
                    )
                    conn.commit()
                    conn.close()
                    status2, _ = client.get("/api/all?days=7")
                    self.assertEqual(status2, 200)
                self.assertEqual(m.call_count, first, "within the floor, hold the scan")
            # floor elapses (0): the still-pending change now rescans
            with mock.patch.object(server, "RESCAN_MIN_S", 0):
                with mock.patch.object(server, "scan", wraps=server.scan) as m2:
                    status3, body3 = client.get("/api/all?days=7")
                    self.assertEqual(status3, 200)
                self.assertEqual(m2.call_count, 1, "after the floor, the pending change rescans")
                self.assertEqual(json.loads(body3)["n_turns"], n1 + 1, "rescan picks up the debounced turn")
            client.close()
        finally:
            tmp.cleanup()
            server._cache.update(saved_cache)


class TestDbUnavailable(unittest.TestCase):
    """A missing DB must surface as 503 with a machine-readable error (not 500).

    ServerClient starts cold (key=None), so the first request rescans and hits
    the missing file deterministically. Deleting after a warm request would
    serve stale cached 200s until TTL — flaky.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "opencode.db")
        build_empty(self.db)
        for p in (self.db, self.db + "-wal", self.db + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        self.client = ServerClient(self.db)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_503_missing_db(self):
        for meth, path in (("get", "/api/health"), ("get", "/api/all"), ("post", "/api/refresh")):
            status, body = getattr(self.client, meth)(path)
            self.assertEqual(status, 503, f"{path} -> {body}")
            self.assertIn("database unavailable", json.loads(body)["error"])


class TestLiveMode(unittest.TestCase):
    """/api/all stamps the raw-scan generation time (issue #13, live mode).

    raw_scan_ts is attached at the HTTP layer only, on a fresh shallow copy:
    the aggregate LRU stores payloads by reference and must never carry the
    key. Two unchanged GETs are byte-identical; a real change (insert +
    POST /api/refresh) advances the stamp.
    """

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

    def setUp(self):
        # the module-level aggregate LRU is shared across tests
        server._agg.clear()

    def _get_all(self):
        status, body = self.client.get("/api/all")
        self.assertEqual(status, 200, body)
        return body

    def test_all_includes_raw_scan_ts(self):
        d = json.loads(self._get_all())
        self.assertIn("raw_scan_ts", d)
        self.assertIsInstance(d["raw_scan_ts"], float)
        self.assertGreater(d["raw_scan_ts"], 0)

    def test_consecutive_gets_byte_identical(self):
        b1 = self._get_all()
        b2 = self._get_all()
        self.assertEqual(b1, b2, "unchanged DB -> identical bodies")

    def test_raw_scan_ts_advances_after_refresh(self):
        t1 = json.loads(self._get_all())["raw_scan_ts"]
        t = _ts((datetime.now() - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0))
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO message VALUES (?, 's1', ?, ?, ?)",
            ("mLiveMode", t, t, json.dumps({
                "role": "assistant", "agent": "build",
                "modelID": "test-27b", "providerID": "vader",
                "tokens": {"input": 0, "output": 0, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}},
                "time": {"created": t, "completed": t + 500},
            })),
        )
        conn.commit()
        conn.close()
        # POST /api/refresh is a no-op within 1 s of the last scan
        time.sleep(1.1)
        status, body = self.client.post("/api/refresh")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        t2 = json.loads(self._get_all())["raw_scan_ts"]
        self.assertGreater(t2, t1)

    def test_cached_aggregate_has_no_raw_scan_ts(self):
        self._get_all()
        cached = server._agg.get((None, None))
        self.assertIsNotNone(cached, "all-time aggregate should be in the LRU")
        self.assertNotIn("raw_scan_ts", cached,
                         "raw_scan_ts must not be stored in the cached aggregate")

    def test_response_is_fresh_shallow_copy(self):
        # The body must be a NEW dict, not the cached aggregate object itself:
        # if it were the same reference, anything that mutates the payload
        # (e.g. a future raw_scan_ts attached in place) would poison the LRU.
        self._get_all()
        cached = server._agg[(None, None)]
        captured = {}
        real = server.Handler._json

        def spy(self_, obj, code=200):
            captured.setdefault("payload", obj)
            return real(self_, obj, code)

        with mock.patch.object(server.Handler, "_json", spy):
            self._get_all()
        payload = captured["payload"]
        self.assertIsNot(payload, cached, "response must be a fresh copy")
        self.assertEqual(payload["n_sessions"], cached["n_sessions"])  # same content


if __name__ == "__main__":
    unittest.main(verbosity=2)
