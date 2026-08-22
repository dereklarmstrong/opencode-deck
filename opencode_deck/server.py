#!/usr/bin/env python3
"""OpenCode Dashboard — read-only analytics over opencode.db.

Stdlib only. Opens the DB read-only, scans session/message/part into
in-memory aggregates (mtime-gated, TTL'd), and serves them as JSON plus a
single-page dashboard.
"""

import argparse
import json
import os
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
TTL_S = 120
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/chart.umd.min.js": "chart.umd.min.js",
}

_ctx_lock = threading.Lock()
_cache = {"db_path": None, "key": None, "ts": 0.0, "data": None}


def _local(ms):
    """ms epoch -> (date 'YYYY-MM-DD', hour, weekday 0=Mon). None-safe."""
    if not ms:
        return None, None, None
    t = time.localtime(ms / 1000)
    return time.strftime("%Y-%m-%d", t), t.tm_hour, t.tm_wday


def _pick_model(msg_model_id, msg_provider, session_model):
    """Prefer per-message model, fall back to session-level model dict."""
    mid = (msg_model_id or "").strip()
    prov = (msg_provider or "").strip()
    if mid or prov:
        return (mid or "?", prov or "?")
    if isinstance(session_model, dict):
        return (session_model.get("id") or "?", session_model.get("providerID") or "?")
    return ("unknown", "unknown")


def _fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _parse_message(r, sessions):
    """Build a turn record from a message row, or None."""
    try:
        d = json.loads(r["data"])
    except (ValueError, TypeError):
        return None
    if d.get("role") != "assistant":
        return None
    mtime = d.get("time") or {}
    tokens = d.get("tokens") or {}
    cache = tokens.get("cache") or {}
    err = d.get("error") or {}
    if isinstance(err, dict) and err.get("name"):
        meta = err.get("data") or {}
        rec = {
            "error": err.get("name"),
            "err_status": meta.get("statusCode"),
            "err_url": (meta.get("metadata") or {}).get("url") if isinstance(meta.get("metadata"), dict) else None,
        }
    else:
        rec = {"error": None, "err_status": None, "err_url": None}
    sess = sessions.get(r["session_id"], {})
    return {
        "msg_id": r["id"],
        "session": r["session_id"],
        "created": mtime.get("created") or r["time_created"],
        "completed": mtime.get("completed"),
        "agent": d.get("agent") or "unknown",
        "model": _pick_model(d.get("modelID"), d.get("providerID"), sess.get("model")),
        "in": tokens.get("input") or 0,
        "out": tokens.get("output") or 0,
        "reason": tokens.get("reasoning") or 0,
        "cache_r": cache.get("read") or 0,
        **rec,
    }


def _streaks(day_set):
    if not day_set:
        return {"current": 0, "longest": 0}
    days = sorted(day_set)
    cur = longest = 1
    for prev, nxt in zip(days, days[1:]):
        if (date.fromisoformat(nxt) - date.fromisoformat(prev)).days == 1:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 1
    today = date.today()
    anchor = today if today.isoformat() in day_set else (
        today - timedelta(days=1) if (today - timedelta(days=1)).isoformat() in day_set else None
    )
    if anchor is not None:
        c, d = 0, anchor
        while d.isoformat() in day_set:
            c += 1
            d -= timedelta(days=1)
        return {"current": c, "longest": longest}
    return {"current": 0, "longest": longest}


def scan(db_path):
    t0 = time.time()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    sessions = {}
    for r in conn.execute(
        "SELECT id, parent_id, directory, title, agent, model, cost, "
        "tokens_input, tokens_output, tokens_reasoning, "
        "tokens_cache_read, tokens_cache_write, time_created, time_updated FROM session"
    ):
        try:
            mjson = json.loads(r["model"]) if r["model"] else {}
        except (ValueError, TypeError):
            mjson = {}
        if not isinstance(mjson, dict):
            mjson = {}
        sessions[r["id"]] = {
            "parent": r["parent_id"],
            "dir": r["directory"] or "unknown",
            "title": r["title"] or "(untitled)",
            "agent": r["agent"] or "unknown",
            "model": mjson,
            "tokens": sum(
                r[c] or 0
                for c in (
                    "tokens_input", "tokens_output", "tokens_reasoning",
                    "tokens_cache_read", "tokens_cache_write",
                )
            ),
            "created": r["time_created"],
            "updated": r["time_updated"],
        }

    turns = []
    for r in conn.execute("SELECT id, session_id, time_created, data FROM message"):
        turn = _parse_message(r, sessions)
        if turn:
            turns.append(turn)

    turns_index = {t["msg_id"]: i for i, t in enumerate(turns)}

    tool_parts = []
    compactions = []
    for r in conn.execute("SELECT id, message_id, session_id, time_created, data FROM part"):
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        ptype = d.get("type")
        if ptype == "tool":
            st = d.get("state") or {}
            tm = st.get("time") or {}
            tool_parts.append({
                "msg": r["message_id"],
                "tool": d.get("tool") or "unknown",
                "status": st.get("status") or "unknown",
                "start": tm.get("start"),
                "end": tm.get("end"),
            })
        elif ptype == "compaction":
            compactions.append(r["time_created"])

    conn.close()

    # tool wall-time per turn
    tool_time_by_turn = [0.0] * len(turns)
    for tp in tool_parts:
        i = turns_index.get(tp["msg"])
        if i is None or not tp["start"] or not tp["end"] or tp["end"] < tp["start"]:
            continue
        tool_time_by_turn[i] += (tp["end"] - tp["start"]) / 1000.0

    data = {"generated_at": int(time.time()), "db": os.path.abspath(db_path)}

    # ---- KPIs ---------------------------------------------------------------
    day_set = set()
    for t in turns:
        dte, _, _ = _local(t["created"])
        if dte:
            day_set.add(dte)
    turn_durs = [
        (t["completed"] - t["created"]) / 1000.0
        for t in turns
        if t["completed"] and t["created"] and t["completed"] > t["created"]
    ]
    data["n_sessions"] = len(sessions)
    data["n_subagents"] = sum(1 for s in sessions.values() if s["parent"])
    data["n_turns"] = len(turns)
    data["total_tokens"] = sum(s["tokens"] for s in sessions.values())
    data["total_in"] = sum(t["in"] for t in turns)
    data["total_out"] = sum(t["out"] for t in turns)
    data["total_reason"] = sum(t["reason"] for t in turns)
    data["total_cache_r"] = sum(t["cache_r"] for t in turns)
    data["api_errors"] = sum(1 for t in turns if t["error"])
    data["median_turn_s"] = round(statistics.median(turn_durs), 1) if turn_durs else None
    data["median_tps"] = None  # set after throughput pass
    data["streaks"] = _streaks(day_set)

    # ---- daily series -------------------------------------------------------
    daily = {}
    for t in turns:
        dte, _, _ = _local(t["created"])
        if not dte:
            continue
        row = daily.setdefault(
            dte, {"turns": 0, "sessions": 0, "in": 0, "out": 0, "reason": 0, "cache": 0, "compactions": 0}
        )
        for k, v in (("turns", 1), ("in", t["in"]), ("out", t["out"]), ("reason", t["reason"]), ("cache", t["cache_r"])):
            row[k] += v
    for c in compactions:
        dte, _, _ = _local(c)
        if dte:
            daily.setdefault(dte, {"turns": 0, "sessions": 0, "in": 0, "out": 0, "reason": 0, "cache": 0, "compactions": 0})["compactions"] += 1
    for s in sessions.values():
        dte, _, _ = _local(s["created"])
        if dte:
            daily.setdefault(dte, {"turns": 0, "sessions": 0, "in": 0, "out": 0, "reason": 0, "cache": 0, "compactions": 0})["sessions"] += 1
    data["daily"] = [{"date": k, **daily[k]} for k in sorted(daily)]

    # ---- heatmaps -----------------------------------------------------------
    hourly = [[0] * 24 for _ in range(7)]
    calendar = Counter()
    for t in turns:
        dte, h, w = _local(t["created"])
        if dte is None:
            continue
        hourly[w][h] += 1
        calendar[dte] += 1
    data["hourly"] = hourly
    data["calendar"] = dict(calendar)

    # ---- agents -------------------------------------------------------------
    agents = defaultdict(lambda: {"sessions": 0, "subagents": 0, "tokens": 0, "turns": 0, "errors": 0})
    for s in sessions.values():
        a = agents[s["agent"]]
        a["sessions"] += 1
        a["subagents"] += 1 if s["parent"] else 0
        a["tokens"] += s["tokens"]
    for t in turns:
        a = agents[t["agent"]]
        a["turns"] += 1
        a["errors"] += 1 if t["error"] else 0
    data["agents"] = [
        {"agent": k, **v} for k, v in sorted(agents.items(), key=lambda kv: -kv[1]["sessions"])
    ]

    # ---- models + throughput ------------------------------------------------
    models = {}
    for s in sessions.values():
        key = _pick_model(None, None, s["model"])
        m = models.setdefault(key, {"label": None, "sessions": 0, "tokens": 0, "turns": 0, "errors": 0, "tps": []})
        m["sessions"] += 1
        m["tokens"] += s["tokens"]
        if m["label"] is None:
            m["label"] = f"{key[0] or '?'} @ {key[1] or '?'}"
    for i, t in enumerate(turns):
        m = models.setdefault(t["model"], {"label": f"{t['model'][0] or '?'} @ {t['model'][1] or '?'}", "sessions": 0, "tokens": 0, "turns": 0, "errors": 0, "tps": []})
        m["turns"] += 1
        m["errors"] += 1 if t["error"] else 0
        if t["completed"] and t["created"]:
            pure = (t["completed"] - t["created"]) / 1000.0 - tool_time_by_turn[i]
            if 1.0 <= pure <= 3600.0 and t["out"] > 0:
                m["tps"].append(t["out"] / pure)
    data["models"] = []
    for (mid, prov), m in models.items():
        tps = sorted(m["tps"])
        data["models"].append({
            "model": mid,
            "provider": prov,
            "label": m["label"],
            "sessions": m["sessions"],
            "turns": m["turns"],
            "tokens": m["tokens"],
            "errors": m["errors"],
            "tps_median": round(statistics.median(tps), 1) if tps else None,
            "tps_samples": len(tps),
            "error_rate": round(m["errors"] / m["turns"], 3) if m["turns"] else 0.0,
        })
    data["models"].sort(key=lambda r: -r["tokens"])
    all_tps = [t["out"] / ((t["completed"] - t["created"]) / 1000.0 - tool_time_by_turn[i])
               for i, t in enumerate(turns)
               if t["completed"] and t["created"]
               and 1.0 <= (t["completed"] - t["created"]) / 1000.0 - tool_time_by_turn[i] <= 3600.0
               and t["out"] > 0]
    data["median_tps"] = round(statistics.median(all_tps), 1) if all_tps else None

    # ---- tools ----------------------------------------------------------------
    tools = {}
    for tp in tool_parts:
        v = tools.setdefault(tp["tool"], {"calls": 0, "errors": 0, "lat": [], "wall": 0.0})
        v["calls"] += 1
        v["errors"] += 1 if tp["status"] == "error" else 0
        if tp["start"] and tp["end"] and tp["end"] >= tp["start"]:
            v["lat"].append((tp["end"] - tp["start"]) / 1000.0)
            v["wall"] += (tp["end"] - tp["start"]) / 1000.0
    data["tools"] = []
    for name, v in tools.items():
        lat = sorted(v["lat"])
        data["tools"].append({
            "tool": name,
            "calls": v["calls"],
            "errors": v["errors"],
            "error_rate": round(v["errors"] / v["calls"], 3) if v["calls"] else 0.0,
            "p50_s": round(lat[len(lat) // 2], 3) if lat else None,
            "p95_s": round(lat[int(len(lat) * 0.95)] if lat else 0.0, 3) if lat else None,
            "wall_s": round(v["wall"], 1),
        })
    data["tools"].sort(key=lambda r: -r["calls"])

    # ---- errors ----------------------------------------------------------------
    api_err = Counter()
    for t in turns:
        if t["error"]:
            host = ""
            if t["err_url"]:
                host = t["err_url"].split("//", 1)[-1].split("/", 1)[0]
            api_err[" ".join(x for x in (t["error"], str(t["err_status"] or ""), host) if x)] += 1
    data["api_errors_top"] = [{"error": k, "count": v} for k, v in api_err.most_common(8)]

    # ---- context health -----------------------------------------------------------
    bin_edges = [0, 8000, 16000, 32000, 64000, 128000, float("inf")]
    hist = [0] * (len(bin_edges) - 1)
    big = 0
    for t in turns:
        for bi in range(len(bin_edges) - 1):
            if bin_edges[bi] <= t["in"] < bin_edges[bi + 1]:
                hist[bi] += 1
                break
        if t["in"] >= 64000:
            big += 1
    now = time.time() * 1000
    comp_recent = sum(1 for c in compactions if c and (now - c) <= 14 * 86400000)
    comp_prev = sum(1 for c in compactions if c and 14 * 86400000 < (now - c) <= 28 * 86400000)
    data["context"] = {
        "histogram": {
            "bins": [
                f"{bin_edges[i] // 1000}k-{(bin_edges[i + 1] // 1000) if bin_edges[i + 1] != float('inf') else 'inf'}k"
                for i in range(len(bin_edges) - 1)
            ],
            "values": hist,
        },
        "bloat_pct": round(100.0 * big / len(turns), 1) if turns else 0.0,
        "compactions_total": len(compactions),
        "compactions_14d": comp_recent,
        "compactions_prev_14d": comp_prev,
    }

    # ---- projects -----------------------------------------------------------------
    projects = {}
    for s in sessions.values():
        p = projects.setdefault(s["dir"], {"sessions": 0, "tokens": 0})
        p["sessions"] += 1
        p["tokens"] += s["tokens"]
    data["projects"] = [{"dir": k, **v} for k, v in sorted(projects.items(), key=lambda kv: -kv[1]["tokens"])]

    # ---- top sessions ---------------------------------------------------------------
    turns_per_session = Counter(t["session"] for t in turns)
    errs_per_session = Counter(t["session"] for t in turns if t["error"])
    top = []
    for sid, s in sessions.items():
        top.append({
            "id": sid,
            "title": s["title"],
            "agent": s["agent"],
            "model": s["model"].get("id") or s["model"].get("providerID") or "?",
            "dir": (s["dir"] or "").rstrip("/").split("/")[-1] or (s["dir"] or ""),
            "tokens": s["tokens"],
            "turns": turns_per_session.get(sid, 0),
            "errors": errs_per_session.get(sid, 0),
            "duration_s": round((s["updated"] - s["created"]) / 1000.0) if s["updated"] and s["created"] and s["updated"] >= s["created"] else None,
            "subagent": 1 if s["parent"] else 0,
        })
    top.sort(key=lambda r: -r["tokens"])
    data["top_sessions"] = top[:20]

    # ---- insights ---------------------------------------------------------------------
    data["insights"] = _insights(turns, sessions, data, tool_parts)
    data["scan_ms"] = int((time.time() - t0) * 1000)
    data["scan_rows"] = {"sessions": len(sessions), "turns": len(turns), "tool_parts": len(tool_parts)}
    return data


def _insights(turns, sessions, data, tool_parts):
    out = []
    if not turns:
        return ["No assistant turns found in the database."]

    late = sum(
        1 for t in turns
        if t["created"] and time.localtime(t["created"] / 1000).tm_hour in (21, 22, 23, 0, 1)
    )
    out.append(f"Night-owl score {100.0 * late / len(turns):.0f}% — share of turns between 9pm and 2am.")

    de = data["n_subagents"]
    out.append(f"Delegation rate {100.0 * de / data['n_sessions']:.0f}% — {de} of {data['n_sessions']} sessions are subagent spawns.")

    bloat = data["context"]["bloat_pct"]
    out.append(
        f"Context rot {bloat:.0f}% — turns running with 64k+ input tokens. "
        + ("That's a bloated context; tighter sessions or earlier compaction would help." if bloat > 25 else "Context usage looks manageable.")
    )

    c1, c2 = data["context"]["compactions_14d"], data["context"]["compactions_prev_14d"]
    if c1 or c2:
        word = "rising" if c1 > c2 else ("easing" if c1 < c2 else "flat")
        out.append(f"Compactions: {c1} in the last 14 days vs {c2} before — context pressure is {word}.")

    best_tps = max(
        (m for m in data["models"] if m["tps_samples"] >= 5),
        key=lambda m: m["tps_median"],
        default=None,
    )
    if best_tps:
        out.append(f"Fastest endpoint: {best_tps['label']} — {best_tps['tps_median']:.0f} tok/s median over {best_tps['tps_samples']} turns.")
    flaky = max(
        (m for m in data["models"] if m["turns"] >= 5),
        key=lambda m: m["error_rate"],
        default=None,
    )
    if flaky and flaky["error_rate"] > 0.05:
        out.append(f"Flakiest endpoint: {flaky['label']} — {100 * flaky['error_rate']:.0f}% of its turns errored.")

    errant = max(
        (t for t in data["tools"] if t["calls"] >= 10),
        key=lambda t: t["error_rate"],
        default=None,
    )
    if errant and errant["error_rate"] > 0.02:
        out.append(f"Most errant tool: {errant['tool']} — {100 * errant['error_rate']:.0f}% of its {errant['calls']} calls errored.")

    tot_wall = sum(t["wall_s"] for t in data["tools"])
    if tot_wall > 0:
        top_tool = max(data["tools"], key=lambda t: t["wall_s"])
        out.append(
            f"{top_tool['tool']} ate {100.0 * top_tool['wall_s'] / tot_wall:.0f}% of all tool wall-time ({top_tool['wall_s'] / 3600:.1f} h)."
        )

    if any(any(row) for row in data["hourly"]):
        best_w = max(range(7), key=lambda w: sum(data["hourly"][w]))
        best_h = max(range(24), key=lambda h: data["hourly"][best_w][h])
        dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][best_w]
        out.append(f"Peak hours: {dow}s around {best_h}:00 local time.")

    dur = [
        ((t["completed"] - t["created"]) / 1000.0, t)
        for t in turns
        if t["completed"] and t["created"] and t["completed"] > t["created"]
    ]
    if dur:
        ln = max(dur, key=lambda x: x[0])
        s = sessions.get(ln[1]["session"], {})
        out.append(f"Longest turn: {ln[0] / 3600:.1f} h in {s.get('title', '(untitled)')[:70]}.")
    if data["top_sessions"]:
        hs = data["top_sessions"][0]
        out.append(f"Heaviest session: {hs['title'][:70]} — {_fmt_tokens(hs['tokens'])} tokens over {hs['turns']} turns.")

    q = sum(1 for tp in tool_parts if tp["tool"] == "question")
    if q:
        out.append(f"The agent asked {q} question(s) and waited for your answer.")
    return out


def _mtimes(db_path):
    out = []
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        try:
            out.append(os.path.getmtime(p))
        except OSError:
            out.append(0)
    return tuple(out)


def get_cache(force=False):
    now = time.time()
    db = _cache["db_path"]
    mt = _mtimes(db)
    stale = _cache["key"] is None or mt != _cache["key"] or (now - _cache["ts"]) > TTL_S
    if force or stale:
        with _ctx_lock:
            # re-check inside the lock (a concurrent caller may have refreshed)
            now = time.time()
            mt = _mtimes(db)
            fresh = _cache["key"] == mt and (now - _cache["ts"]) <= TTL_S
            if force and now - _cache["ts"] < 1.0:
                return _cache["data"]  # throttle hammering refreshes
            if not force and fresh:
                return _cache["data"]
            if force or _cache["key"] != mt or (now - _cache["ts"]) > TTL_S:
                _cache["data"] = scan(db)
                _cache["key"] = _mtimes(db)
                _cache["ts"] = time.time()
    return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            d = get_cache()
            self._json({
                "ok": d is not None,
                "cache_age_s": round(time.time() - _cache["ts"], 1),
                "scan_ms": (d or {}).get("scan_ms"),
                "rows": (d or {}).get("scan_rows"),
                "db": _cache["db_path"],
            })
        elif path == "/api/all":
            self._json(get_cache())
        elif path in STATIC_FILES:
            fpath = os.path.join(APP_DIR, "static", STATIC_FILES[path])
            try:
                body = open(fpath, "rb").read()
            except OSError:
                self._json({"error": "not found"}, 404)
                return
            ct = "application/javascript" if path == "/chart.umd.min.js" else "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/refresh":
            d = get_cache(force=True)
            self._json({"ok": d is not None, "scan_ms": (d or {}).get("scan_ms"), "rows": (d or {}).get("scan_rows")})
        else:
            self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="OpenCode usage dashboard")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to opencode.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    if not os.path.exists(args.db):
        raise SystemExit(f"DB not found: {args.db}")
    _cache["db_path"] = os.path.abspath(args.db)
    get_cache(force=True)  # warm at startup
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"opencode-deck: http://{args.host}:{args.port} (db={_cache['db_path']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
