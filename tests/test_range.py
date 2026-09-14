"""Range-window tests for GET /api/all?days=N / ?from=D&to=D.

Same fixture as test_api: 5 assistant turns on d1 = today-3 (s1), d2 = today-2
(s3) and today (s4); session s2 (subagent of s1) has no turns of its own.
Plus synthetic DBs for inclusive-start/exclusive-end boundaries and for the
compaction 14d/prev-14d anchor.

Window semantics under test:
  - turn in-window iff start <= created < end (ms, server-local days)
  - tool parts / compactions inherit membership from their owning turn
    (compactions by own timestamp; parts by part msg id)
  - sessions collapse to those with >=1 in-window turn
  - total_tokens: all-time = session-lifetime, windowed = in-window turns
  - streaks anchor to min(range end, today); 14d compaction anchor to min(end, now)
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_api import ServerClient, build_fixture  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opencode_deck import server  # noqa: E402

SCHEMA = """
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
"""


def _ts(dt):
    return int(dt.timestamp() * 1000)


def _at(dt, h, m=0):
    return _ts(dt.replace(hour=h, minute=m, second=0, microsecond=0))


class TestRange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "opencode.db")
        fx = build_fixture(cls.db)
        cls.today, cls.d1, cls.d2 = fx["today"], fx["d1"], fx["d2"]
        cls.client = ServerClient(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def _all(self, qs):
        status, body = self.client.get(f"/api/all?{qs}" if qs else "/api/all")
        self.assertEqual(status, 200)
        return json.loads(body)

    # ---- quick ranges -----------------------------------------------------------

    def test_days7(self):
        d = self._all("days=7")
        self.assertEqual(d["n_turns"], 5)
        self.assertEqual(d["n_sessions"], 3)  # s2 has no turns of its own
        self.assertEqual(d["n_subagents"], 0)
        # windowed total = turn tokens: 20150 + 13020 + 70115 + 1000 + 450
        self.assertEqual(d["total_tokens"], 104735)
        self.assertEqual(d["total_in"], 96400)
        self.assertEqual(d["total_out"], 185)
        self.assertEqual(d["total_cache_r"], 8100)
        self.assertEqual(d["api_errors"], 1)
        # s3 (71115) outranks s1 (33170) when ranked by window tokens
        self.assertEqual(d["top_sessions"][0]["id"], "s3")
        tokens = {s["id"]: s["tokens"] for s in d["top_sessions"]}
        self.assertEqual(tokens["s3"], 71115)
        self.assertEqual(tokens["s1"], 33170)
        self.assertEqual(tokens["s4"], 450)
        # turn-only surfaces are identical to all-time here (all turns in window)
        full = self._all("")
        self.assertEqual(d["hourly"], full["hourly"])
        self.assertEqual(d["calendar"], full["calendar"])
        # session-level daily count collapses to windowed sessions:
        # d1 had s1+s2 created all-time (2) but only s1 has window turns (1)
        win_d1 = [r for r in d["daily"] if r["date"] == self.d1.date().isoformat()][0]
        full_d1 = [r for r in full["daily"] if r["date"] == self.d1.date().isoformat()][0]
        self.assertEqual(win_d1["sessions"], 1)
        self.assertEqual(full_d1["sessions"], 2)

    def test_days30_90_match_days7_here(self):
        # fixture data is only 3 days old: wider quick ranges cover everything
        for n in (30, 90):
            d = self._all(f"days={n}")
            self.assertEqual(d["n_turns"], 5)
            self.assertEqual(d["n_sessions"], 3)

    def test_days7_equivalent_to_from_to(self):
        a = self._all("days=7")
        b = self._all(f"from={(self.today - timedelta(days=6)).date().isoformat()}"
                      f"&to={self.today.date().isoformat()}")
        a.pop("generated_at")
        b.pop("generated_at")
        self.assertEqual(a, b)

    # ---- narrow from/to ----------------------------------------------------------

    def test_from_to_single_day(self):
        d1 = self.d1.date().isoformat()
        d = self._all(f"from={d1}&to={d1}")
        self.assertEqual(d["n_turns"], 2)
        self.assertEqual(d["n_sessions"], 1)
        self.assertEqual(d["n_subagents"], 0)
        self.assertEqual(d["total_tokens"], 33170)
        self.assertEqual(d["top_sessions"][0]["id"], "s1")
        # only s1's own parts survive: p1+p2 bash (3s wall), glob on m2
        tools = {t["tool"]: t for t in d["tools"]}
        self.assertEqual(tools["bash"]["calls"], 2)
        self.assertAlmostEqual(tools["bash"]["wall_s"], 3.0, places=1)
        self.assertEqual(tools["glob"]["calls"], 1)
        self.assertNotIn("read", tools)
        self.assertNotIn("webfetch", tools)
        # context: both turns < 16k (bins 0,2,0,0,0,0), compaction m1 in window
        cx = d["context"]
        self.assertEqual(cx["histogram"]["values"], [0, 2, 0, 0, 0, 0])
        self.assertEqual(cx["bloat_pct"], 0.0)
        self.assertEqual(cx["compactions_total"], 1)
        self.assertEqual(cx["compactions_14d"], 1)
        # streak as it stood on d1: single day
        self.assertEqual(d["streaks"], {"current": 1, "longest": 1})
        # insights re-derived on the window: no errors, no subagents
        joined = " | ".join(d["insights"])
        self.assertIn("Night-owl score 100%", joined)
        self.assertNotIn("APIError", joined)

    def test_from_to_other_day(self):
        d2 = self.d2.date().isoformat()
        d = self._all(f"from={d2}&to={d2}")
        self.assertEqual(d["n_turns"], 2)
        m = d["models"][0]
        self.assertEqual(m["turns"], 2)
        self.assertEqual(m["tokens"], 71115)
        self.assertEqual(m["tps_samples"], 0)
        self.assertEqual(m["error_rate"], 0.5)
        agents = {a["agent"]: a for a in d["agents"]}
        self.assertNotIn("general", agents)  # s2 (general) has no turns here
        self.assertEqual(agents["Assistant"]["turns"], 2)
        self.assertEqual(d["api_errors"], 1)

    def test_range_wider_than_data_is_not_alltime(self):
        # dual-token-semantics: even a window that covers everything is not
        # the all-time payload (session-lifetime vs window-turn totals)
        full = self._all("")
        wide = self._all("days=3650")
        self.assertEqual(wide["n_turns"], full["n_turns"])
        self.assertEqual(wide["n_sessions"], 3)  # s2 excluded (no turns)
        self.assertEqual(full["n_sessions"], 4)
        self.assertNotEqual(wide["total_tokens"], full["total_tokens"])

    # ---- validation ------------------------------------------------------------

    def _expect_400(self, qs):
        status, body = self.client.get(f"/api/all?{qs}")
        self.assertEqual(status, 400, body)
        self.assertIn("bad range", json.loads(body)["error"])

    def test_invalid_ranges(self):
        self._expect_400("from=2026-09-01")
        self._expect_400("to=2026-09-01")
        self._expect_400("days=abc")
        self._expect_400("days=0")
        self._expect_400("days=-3")
        self._expect_400("from=2020-02-30&to=2020-03-01")  # regex ok, not a date
        self._expect_400("from=2020/01/01&to=2020-01-02")
        self._expect_400("from=2026-09-02&to=2026-09-01")  # reversed
        # cache unaffected: a good request still works afterwards
        d = self._all("days=7")
        self.assertEqual(d["n_turns"], 5)

    def test_unknown_params_ignored(self):
        d = self._all("days=7&bogus=1")
        self.assertEqual(d["n_turns"], 5)

    def test_empty_window(self):
        # no activity in 2099: every surface must degrade gracefully
        d = self._all("from=2099-01-01&to=2099-12-31")
        self.assertEqual(d["n_turns"], 0)
        self.assertEqual(d["n_sessions"], 0)
        self.assertEqual(d["total_tokens"], 0)
        self.assertEqual(d["streaks"], {"current": 0, "longest": 0})
        self.assertIsNone(d["median_turn_s"])
        self.assertEqual(d["models"], [])
        self.assertEqual(d["tools"], [])
        self.assertEqual(d["daily"], [])
        self.assertEqual(d["context"]["bloat_pct"], 0.0)
        self.assertEqual(d["insights"], ["No assistant turns found in the database."])

    def test_overflow_ranges_rejected(self):
        # out-of-representable-date-range windows are 400s, not 500s
        self._expect_400("days=99999999")
        self._expect_400("from=9999-12-30&to=9999-12-31")
        # ~1000 years back IS still representable: served normally
        d = self._all("days=365200")
        self.assertEqual(d["n_turns"], 5)


class TestRangeBoundary(unittest.TestCase):
    """Turn A exactly at local midnight of day X (past), turn B exactly at
    midnight of day X+1 (past): from=X&to=X must include A and exclude B.
    (Both in the past: the window end is clamped to now, so future-dated
    turns can never be in a window.)"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "boundary.db")
        cls.client = ServerClient(cls.db)

        x = datetime.now().date() - timedelta(days=2)  # day X, in the past
        conn = sqlite3.connect(cls.db)
        conn.executescript(SCHEMA)
        model = json.dumps({"id": "test-27b", "providerID": "vader"})
        t0 = _at(datetime.combine(x, datetime.min.time()), 0)
        conn.execute(
            "INSERT INTO session (id, directory, title, agent, model, tokens_input, "
            "tokens_output, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?)",
            ("sA", "/tmp/edge", "Edge session", "build", model, 11, 1, t0, t0 + 86400000))
        a = {"role": "assistant", "agent": "build", "modelID": "test-27b", "providerID": "vader",
             "tokens": {"input": 10, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
             "time": {"created": t0, "completed": t0 + 3600000}}
        conn.execute("INSERT INTO message VALUES ('mA','sA',?,?,?)", (t0, t0, json.dumps(a)))
        b = {"role": "assistant", "agent": "build", "modelID": "test-27b", "providerID": "vader",
             "tokens": {"input": 999, "output": 99, "reasoning": 0, "cache": {"read": 0, "write": 0}},
             "time": {"created": t0 + 86400000, "completed": t0 + 86400000 + 3600000}}
        conn.execute("INSERT INTO message VALUES ('mB','sA',?,?,?)", (t0 + 86400000, t0 + 86400000, json.dumps(b)))
        conn.commit()
        conn.close()
        cls.day_x = x.isoformat()
        cls.day_y = (x + timedelta(days=1)).isoformat()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def _all(self, qs):
        status, body = self.client.get(f"/api/all?{qs}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_inclusive_start_exclusive_end(self):
        d_x = self._all(f"from={self.day_x}&to={self.day_x}")
        self.assertEqual(d_x["n_turns"], 1)
        self.assertEqual(d_x["total_tokens"], 11)  # turn A only

        d_y = self._all(f"from={self.day_y}&to={self.day_y}")
        self.assertEqual(d_y["n_turns"], 1)
        self.assertEqual(d_y["total_tokens"], 1098)  # turn B only

    def test_two_day_span_covers_both(self):
        d = self._all(f"from={self.day_x}&to={self.day_y}")
        self.assertEqual(d["n_turns"], 2)
        self.assertEqual(d["total_tokens"], 1109)


class TestRangeCompactions(unittest.TestCase):
    """Compaction 20 days ago, no turns: exercises the 14d/prev-14d anchor
    (all-time anchors to now; a window anchors to min(range end, now))."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "comp.db")
        cls.client = ServerClient(cls.db)

        c_day = date.today() - timedelta(days=20)  # compaction day
        conn = sqlite3.connect(cls.db)
        conn.executescript(SCHEMA)
        model = json.dumps({"id": "test-27b", "providerID": "vader"})
        created = _at(datetime.combine(date.today() - timedelta(days=30), datetime.min.time()), 0)
        comp_ms = _at(datetime.combine(c_day, datetime.min.time()), 12)
        conn.execute(
            "INSERT INTO session (id, directory, title, agent, model, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?)",
            ("sC", "/tmp/comp", "Comp session", "build", model, created, created + 86400000))
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            ("pc", "mZ", "sC", comp_ms, comp_ms, json.dumps({"type": "compaction"})))
        conn.commit()
        conn.close()
        cls.c_iso = c_day.isoformat()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def _all(self, qs):
        status, body = self.client.get(f"/api/all?{qs}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_alltime_anchor_now(self):
        cx = self._all("")["context"]
        self.assertEqual(cx["compactions_total"], 1)
        self.assertEqual(cx["compactions_14d"], 0)
        self.assertEqual(cx["compactions_prev_14d"], 1)  # ~20d ago

    def test_window_end_inside_14d_of_comp(self):
        # window [C, C+1]: anchor = midnight(C+2), 1.5d after the compaction
        to = (date.fromisoformat(self.c_iso) + timedelta(days=1)).isoformat()
        cx = self._all(f"from={self.c_iso}&to={to}")["context"]
        self.assertEqual(cx["compactions_total"], 1)
        self.assertEqual(cx["compactions_14d"], 1)
        self.assertEqual(cx["compactions_prev_14d"], 0)

    def test_window_end_outside_14d_of_comp(self):
        # window [C, C+14]: anchor = midnight(C+15), 14.5d after the compaction
        to = (date.fromisoformat(self.c_iso) + timedelta(days=14)).isoformat()
        cx = self._all(f"from={self.c_iso}&to={to}")["context"]
        self.assertEqual(cx["compactions_total"], 1)
        self.assertEqual(cx["compactions_14d"], 0)
        self.assertEqual(cx["compactions_prev_14d"], 1)

    def test_window_before_comp(self):
        to = (date.fromisoformat(self.c_iso) - timedelta(days=1)).isoformat()
        cx = self._all(f"from={to}&to={to}")["context"]
        self.assertEqual(cx["compactions_total"], 0)
        self.assertEqual(cx["compactions_14d"], 0)
        self.assertEqual(cx["compactions_prev_14d"], 0)


class TestParseRange(unittest.TestCase):
    def setUp(self):
        self.today = date.today()

    def test_none(self):
        self.assertEqual(server._parse_range(""), (None, None))
        self.assertEqual(server._parse_range("bogus=1"), (None, None))

    def test_days(self):
        start, end = server._parse_range("days=7")
        self.assertEqual(start, server._day_ms((self.today - timedelta(days=6)).isoformat()))
        self.assertEqual(end, server._day_ms((self.today + timedelta(days=1)).isoformat()))

    def test_days_single(self):
        start, end = server._parse_range("days=1")
        self.assertEqual(start, server._day_ms(self.today.isoformat()))
        self.assertEqual(end, server._day_ms((self.today + timedelta(days=1)).isoformat()))

    def test_from_to(self):
        start, end = server._parse_range("from=2026-01-01&to=2026-01-03")
        self.assertEqual(start, server._day_ms("2026-01-01"))
        # end is exclusive: midnight of the day AFTER `to`
        self.assertEqual(end, server._day_ms("2026-01-04"))

    def test_errors(self):
        for qs in ("days=0", "days=abc", "days=-1", "from=2026-01-01",
                   "to=2026-01-01", "from=2020-13-01&to=2020-13-02",
                   "from=2026-01-05&to=2026-01-01", "from=2026/01/01&to=2026-01-02"):
            with self.assertRaises(ValueError, msg=qs):
                server._parse_range(qs)

    def test_overflow(self):
        for qs in ("days=99999999", "from=9999-12-30&to=9999-12-31"):
            with self.assertRaises(ValueError, msg=qs):
                server._parse_range(qs)
        # ~1000 years back is representable
        start, end = server._parse_range("days=365200")
        self.assertIsNotNone(start)
        self.assertGreater(end, start)


class TestLatencyPercentiles(unittest.TestCase):
    """Tool p50/p95 are nearest-rank percentiles over sorted latencies (issue #8):
    20 latencies of 100..2000 ms give p50 = 10th-smallest = 1.0 s and
    p95 = 19th-smallest = 1.9 s — NOT the max (2.0 s)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "pct.db")
        cls.client = ServerClient(cls.db)
        base = _ts((datetime.now() - timedelta(hours=2)).replace(second=0, microsecond=0))
        conn = sqlite3.connect(cls.db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO session (id, directory, title, agent, model, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?)",
            ("sP", "/tmp/pct", "Pct session", "build",
             json.dumps({"id": "test-27b", "providerID": "vader"}), base, base + 100000))
        for i in range(20):  # latencies 0.1 .. 2.0 s, one orphan part each
            start = base + i * 1000
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"p{i}", "mX", "sP", start, start + (i + 1) * 100,
                 json.dumps({"type": "tool", "tool": "bash", "callID": f"c{i}",
                             "state": {"status": "completed",
                                       "time": {"start": start, "end": start + (i + 1) * 100}}})))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_nearest_rank_percentiles(self):
        status, body = self.client.get("/api/all")
        self.assertEqual(status, 200)
        bash = {t["tool"]: t for t in json.loads(body)["tools"]}["bash"]
        self.assertEqual(bash["calls"], 20)
        self.assertEqual(bash["p50_s"], 1.0)
        self.assertEqual(bash["p95_s"], 1.9)  # 19th-smallest, not the max (2.0)


class TestHudTimeOrder(unittest.TestCase):
    """recent_tps_avg10 means the last 10 turns by COMPLETION time over
    unrounded values (issue #8) — not storage order. Fast turn inserted first
    (rowid 1) but completing last: time-order avg10 = 13.0, storage-order = 10.0."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "hud.db")
        cls.client = ServerClient(cls.db)
        base = _ts((datetime.now() - timedelta(hours=2)).replace(second=0, microsecond=0))
        conn = sqlite3.connect(cls.db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO session (id, directory, title, agent, model, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?)",
            ("sH", "/tmp/hud", "Hud session", "build",
             json.dumps({"id": "test-27b", "providerID": "vader"}), base, base + 60000))

        def turn(mid, created, completed, out):
            conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                         (mid, "sH", created, created, json.dumps({
                             "role": "assistant", "agent": "build",
                             "modelID": "test-27b", "providerID": "vader",
                             "tokens": {"input": 50, "output": out, "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}},
                             "time": {"created": created, "completed": completed}})))

        # 600 out / 15 s = 40 tps, completes LAST (t0 + 15 s)
        turn("mFast", base, base + 15000, 600)
        # 12 slow turns: 10 out / 1 s = 10 tps, completing t0+2 s .. t0+13 s
        for i in range(1, 13):
            turn(f"mSlow{i}", base + i * 1000, base + i * 1000 + 1000, 10)
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_avg10_time_sorted(self):
        status, body = self.client.get("/api/all")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(len(d["recent_tps"]), 13)
        # completion order: [10 x12, 40] -> last 10 = nine 10s + the 40 -> 13.0
        self.assertEqual(d["recent_tps_avg10"], 13.0)
        # median of [10 x12, 40] across all 13 turns
        self.assertEqual(d["median_tps"], 10.0)


class TestInsightGuards(unittest.TestCase):
    """flakiest requires >= 20 model turns (issue #12); the API-error insight
    reports the most common failed-turn class with its share of errors."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "ins.db")
        cls.client = ServerClient(cls.db)
        base = _ts((datetime.now() - timedelta(hours=2)).replace(second=0, microsecond=0))
        conn = sqlite3.connect(cls.db)
        conn.executescript(SCHEMA)
        for sid, mid, pid, n, errs in (("sA", "flaky-model", "vader", 30, 12),
                                       ("sB", "rare-model", "other", 12, 5)):
            conn.execute(
                "INSERT INTO session (id, directory, title, agent, model, time_created, time_updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, f"/tmp/{sid}", f"{sid} session", "build",
                 json.dumps({"id": mid, "providerID": pid}), base, base + 100000))
            for i in range(n):
                d = {
                    "role": "assistant", "agent": "build",
                    "modelID": mid, "providerID": pid,
                    "tokens": {"input": 10, "output": 0, "reasoning": 0,
                               "cache": {"read": 0, "write": 0}},
                    "time": {"created": base + i * 1000, "completed": base + i * 1000 + 500},
                }
                if i < errs:
                    d["error"] = {"name": "APIError", "data": {"message": "boom",
                                                               "statusCode": 404}}
                conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                             (f"m{sid}{i}", sid, base + i * 1000, base + i * 1000,
                              json.dumps(d)))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_insight_guards(self):
        status, body = self.client.get("/api/all")
        self.assertEqual(status, 200)
        joined = " | ".join(json.loads(body)["insights"])
        # flaky-model: 12/30 = 40% with >= 20 turns -> fires
        self.assertIn("Flakiest endpoint: flaky-model @ vader — 40% of its turns errored.", joined)
        # rare-model: 5/12 = 41.7% but only 12 turns -> must not fire anywhere
        self.assertNotIn("rare-model", joined)
        # 12 + 5 = 17 failed turns, all APIError
        self.assertIn("API errors: 17 failed turns — most common: APIError (100% of errors).", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
