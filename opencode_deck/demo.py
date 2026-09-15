"""Deterministic synthetic dataset for `opencode-deck --demo`.

Builds a realistic ~90-day opencode.db (session/message/part tables, same
schema the real DB uses) so the dashboard can be served — and screenshotted —
with zero real data. Every identifier is generic on purpose: no real project,
company, or host names appear anywhere in the generated rows.

All randomness flows through a single `random.Random(seed)`, so a given seed
rebuilds byte-stable data (row counts, token sums, titles identical).
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
from datetime import datetime, timedelta

_VERBS = ["Refactor", "Fix", "Design", "Migrate", "Debug", "Prototype",
          "Review", "Optimize", "Document"]
_OBJECTS = ["retry handler", "schema v3", "webhook pipeline", "context loader",
            "token estimator", "cache layer", "heatmap renderer", "CI runner",
            "settings page", "metrics endpoint", "prompt router", "blob store"]
_TITLES = [f"{v} {o}" for v in _VERBS for o in _OBJECTS]
DIRS = ["~/code/payments-api", "~/code/infra", "~/code/deck",
        "~/code/blog", "~/code/homelab"]
# (providerID, modelID, weight, min turn secs, max turn secs, error prob)
MODELS = [
    ("anthropic", "claude-sonnet-4-5", 0.40, 3.0, 20.0, 0.015),
    ("openai", "gpt-5", 0.25, 5.0, 30.0, 0.020),
    ("google", "gemini-2.5-pro", 0.15, 6.0, 35.0, 0.020),
    ("ollama", "llama-3.3-70b", 0.20, 20.0, 90.0, 0.050),
]
AGENTS = ["general", "build", "explore", "planner"]
# (tool, min secs, max secs)
_TOOLS = [("bash", 2.0, 30.0), ("webfetch", 3.0, 30.0), ("read", 0.2, 4.0),
          ("edit", 0.2, 6.0), ("grep", 0.2, 5.0), ("task", 5.0, 40.0),
          ("question", 0.2, 2.0)]
_ERRORS = [("rate limited", 429), ("upstream 5xx", 503),
           ("request timeout", 504), ("connection reset", 502)]

_SCHEMA = """
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
"""


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _pick_hour(rng: random.Random) -> int:
    """Night-owl distribution: evening + late-night peaks, small workday bump."""
    bucket = rng.choices(["evening", "late", "workday", "other"],
                         weights=[40, 20, 15, 15])[0]
    if bucket == "evening":
        return rng.randrange(20, 24)
    if bucket == "late":
        return rng.randrange(0, 2)
    if bucket == "workday":
        return rng.randrange(9, 11)
    return rng.choice([12, 13, 14, 15, 16, 17, 18, 19])


def build_demo_db(path: str, seed: int = 42, days: int = 90) -> dict:
    """Create a deterministic demo opencode.db at `path`. Returns a summary."""
    rng = random.Random(seed)
    now = datetime.now().replace(microsecond=0)
    today = now.replace(hour=0, minute=0, second=0)

    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            os.remove(p)

    srows, mrows, prows = [], [], []
    state = {"msg": 0, "part": 0}

    def pick_model():
        return rng.choices(MODELS, weights=[m[2] for m in MODELS])[0]

    def add_turn(sid, agent, pid, mid, created, dur_s, error=None,
                 inprog=False):
        """Insert an assistant message (+token accounting). Returns (ms, end_dt, tokens)."""
        state["msg"] += 1
        c_ms = _ms(created)
        end = created + timedelta(seconds=dur_s)
        tin = int(min(165000, max(9000, state["ctx"])))
        tout = 0 if error else rng.randint(30, 2500)
        tr = 0 if rng.random() < 0.8 else rng.randint(10, 800)
        cr = int(tin * rng.uniform(0.6, 0.9))
        cw = int(tin * 0.005) if rng.random() < 0.3 else 0
        data = {
            "role": "assistant", "agent": agent, "modelID": mid, "providerID": pid,
            "tokens": {"input": tin, "output": tout, "reasoning": tr,
                       "cache": {"read": cr, "write": cw}},
            "cost": 0, "time": {"created": c_ms},
        }
        if not inprog:
            data["time"]["completed"] = _ms(end)
        if error:
            label, code = error
            data["error"] = {"name": "APIError",
                             "data": {"message": label, "statusCode": code}}
        msg_id = f"msg{state['msg']:06d}"
        mrows.append((msg_id, sid, c_ms, _ms(end), json.dumps(data)))
        return msg_id, c_ms, end, (tin, tout, tr, cr, cw)

    def add_tools(msg_id, sid, c_ms, end_ms, k, tool_hi):
        """Insert up to `k` tool parts inside the turn; keep them in range."""
        cursor = c_ms + rng.randint(100, 1500)
        room = max(0, end_ms - 200 - cursor)
        for _ in range(k):
            name, tlo, thi = rng.choice(_TOOLS)
            t = min(rng.uniform(tlo, min(thi, tool_hi)), 15.0)
            if room < t * 1000:
                break
            state["part"] += 1
            status = "error" if rng.random() < 0.03 else "completed"
            data = {"type": "tool", "tool": name,
                    "state": {"status": status,
                              "time": {"start": cursor, "end": int(cursor + t * 1000)}}}
            prows.append((f"part{state['part']:07d}", msg_id, sid, cursor,
                          int(cursor + t * 1000), json.dumps(data)))
            cursor += int(t * 1000) + rng.randint(50, 800)
            room = max(0, end_ms - 200 - cursor)

    # ---- schedule top-level sessions over `days` days ending today --------
    plan = []
    for off in range(days):
        day = today + timedelta(days=off - (days - 1))
        is_today = off == days - 1
        if day.weekday() < 5:
            n = rng.choices([1, 2, 3], weights=[4, 4, 2])[0]
        else:
            n = rng.choices([0, 1], weights=[3, 2])[0]
        if is_today:
            n = max(1, min(3, n))
        for _ in range(n):
            start = day.replace(hour=_pick_hour(rng), minute=rng.randrange(60),
                                second=0)
            if is_today and start >= now - timedelta(minutes=10):
                start = now - timedelta(hours=2)
            plan.append((start, is_today))

    compaction_sessions = []
    for idx, (start, is_today) in enumerate(plan):
        pid, mid, _w, lo, hi, err_p = pick_model()
        agent = rng.choice(AGENTS)
        duration_h = rng.uniform(0.4, 5.0)
        if is_today:
            span = (now - start).total_seconds()
            if span <= 60:
                continue
            duration_h = min(duration_h, span / 3600.0 * 0.6)
            if duration_h < 0.15:
                continue
        sid = f"ses{idx + 1:04d}"
        directory = rng.choice(DIRS)
        title = rng.choice(_TITLES)

        n_turns = rng.randint(40, 400)
        step = 135000.0 / max(1, n_turns - 1)
        compactions = []
        if n_turns >= 200 and len(compaction_sessions) < 12:
            compactions = sorted(rng.sample(range(40, n_turns - 40),
                                            rng.randint(1, 2)))

        state["ctx"] = 15000
        tin_sum = tout_sum = tr_sum = cr_sum = cw_sum = 0
        last_end = start
        for i in range(n_turns):
            frac = i / max(1, n_turns - 1)
            created = start + timedelta(hours=duration_h * frac
                                        + rng.uniform(-0.02, 0.02))
            if is_today and created >= now - timedelta(minutes=3):
                created = now - timedelta(minutes=3)
            if i > 0:
                created = max(created, last_end + timedelta(seconds=1))
            dur_s = rng.uniform(lo, hi)
            inprog = is_today and i >= n_turns - 2 and \
                created + timedelta(seconds=dur_s) > now
            if inprog:
                dur_s = min(dur_s, max(1.0, (now - created).total_seconds() / 2))
            state["ctx"] = 15000 + step * i + rng.uniform(-2000, 2000)
            if i in compactions:
                state["ctx"] = 15000 + rng.uniform(0, 8000)
            error = rng.choice(_ERRORS) if rng.random() < err_p else None
            if error:
                dur_s = min(dur_s, 1.0)
            msg_id, c_ms, end, toks = add_turn(sid, agent, pid, mid, created,
                                               dur_s, error=error, inprog=inprog)
            tin_sum += toks[0]
            tout_sum += toks[1]
            tr_sum += toks[2]
            cr_sum += toks[3]
            cw_sum += toks[4]
            last_end = end
            if not inprog and not error:
                k = rng.choices([0, 1, 2, 3, 4, 5],
                                weights=[10, 30, 28, 20, 8, 4])[0]
                tool_hi = 15.0 if agent != "explore" else 5.0
                add_tools(msg_id, sid, c_ms, _ms(end), k, tool_hi)

        model_json = json.dumps({"id": mid, "providerID": pid, "variant": "default"})
        srows.append((sid, None, None, None, None, directory, None, title,
                      None, None, None, 0, 0, 0, None, 0.0,
                      tin_sum, tout_sum, tr_sum, cr_sum, cw_sum,
                      None, None, agent, model_json, _ms(start), _ms(last_end),
                      None, None))
        if compactions:
            compaction_sessions.append((sid, len(srows) - 1))

    # ---- compaction parts for the longest sessions -------------------------
    for sid, row_idx in compaction_sessions:
        row = srows[row_idx]
        start_ms, end_ms = row[25], row[26]
        comp = int(start_ms + (end_ms - start_ms) * 0.45)
        state["part"] += 1
        prows.append((f"part{state['part']:07d}", None, sid, comp, comp,
                      json.dumps({"type": "compaction"})))

    # ---- subagent children (~20% of top-level sessions) --------------------
    n_children = 0
    for srow in list(srows):
        if rng.random() >= 0.20:
            continue
        n_children += 1
        sid = f"ses{1000 + n_children:04d}"
        parent_start = datetime.fromtimestamp(srow[25] / 1000)
        start = parent_start + timedelta(minutes=rng.randint(5, 40))
        if start >= now:
            start = now - timedelta(minutes=20)
        agent = rng.choice(AGENTS)
        mid = json.loads(srow[24])["id"]
        pid = json.loads(srow[24])["providerID"]
        model_json = srow[24]
        n_turns = rng.randint(10, 60)
        dur_total = rng.uniform(10, 90)
        tin_sum = tout_sum = tr_sum = cr_sum = cw_sum = 0
        for i2 in range(n_turns):
            created = start + timedelta(
                seconds=dur_total * i2 / max(1, n_turns - 1) + rng.uniform(-1, 1))
            if created >= now - timedelta(minutes=1):
                break
            dur_s = rng.uniform(2, 25)
            state["ctx"] = 8000 + rng.uniform(0, 45000)
            msg_id, c_ms, end, toks = add_turn(sid, agent, pid, mid, created, dur_s)
            tin_sum += toks[0]
            tout_sum += toks[1]
            tr_sum += toks[2]
            cr_sum += toks[3]
            cw_sum += toks[4]
            k = rng.choices([0, 1, 2, 3], weights=[15, 45, 30, 10])[0]
            add_tools(msg_id, sid, c_ms, _ms(end), k, 10.0)
        srows.append((sid, None, None, srow[0], None, srow[5], None,
                      "Task: " + rng.choice(_OBJECTS), None, None, None,
                      0, 0, 0, None, 0.0,
                      tin_sum, tout_sum, tr_sum, cr_sum, cw_sum,
                      None, None, agent, model_json,
                      _ms(start), _ms(min(now, start + timedelta(seconds=dur_total + 60))),
                      None, None))

    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executemany(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        srows)
    conn.executemany("INSERT INTO message VALUES (?,?,?,?,?)", mrows)
    conn.executemany("INSERT INTO part VALUES (?,?,?,?,?,?)", prows)
    conn.commit()
    conn.close()
    return {"sessions": len(srows), "turns": len(mrows), "tool_parts": len(prows)}


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
    print(build_demo_db(out))
