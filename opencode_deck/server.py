#!/usr/bin/env python3
"""OpenCode Dashboard — read-only analytics over opencode.db.

Stdlib only. Opens the DB read-only, scans session/message/part into
in-memory aggregates (content-signature-gated, TTL'd, with a per-range
aggregate LRU), and serves them as JSON plus a single-page dashboard.
"""

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
TTL_S = 120
# Re-scan throttle: on a live DB the content signature flips with every
# opencode commit (about once per 1-2 s while a session is active) and a full
# scan costs ~2.4 s, so without a floor we'd be scanning per request. A usage
# dashboard tolerates a 15 s freshness cap; POST /api/refresh is the escape
# hatch for callers that need data right now.
RESCAN_MIN_S = 15
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/chart.umd.min.js": "chart.umd.min.js",
}

_ctx_lock = threading.Lock()
_cache = {"db_path": None, "key": None, "ts": 0.0, "data": None}
# LRU of aggregated payloads, (start_ms, end_ms) -> data. Entries are only
# valid for the current raw scan: get_cache() clears this whenever a new scan
# lands, so the range alone is a safe key. Bounded so a churning set of
# custom ranges can't grow memory without limit.
AGG_CACHE_MAX = 64
_agg = OrderedDict()


def _local(ms):
    """ms epoch -> (date 'YYYY-MM-DD', hour, weekday 0=Mon). None-safe."""
    if not ms:
        return None, None, None
    t = time.localtime(ms / 1000)
    return time.strftime("%Y-%m-%d", t), t.tm_hour, t.tm_wday


def _pick_model(msg_model_id, msg_provider, session_model):
    """Prefer per-message model, fall back to session-level model dict.

    Handles opencode's `providerID: "provider:model"` shorthand (model id
    unresolved -> the part after the colon is the model id) and normalizes
    every missing fragment to "unknown" (issue #12).
    """
    mid = (msg_model_id or "").strip()
    prov = (msg_provider or "").strip()
    if not mid and not prov and isinstance(session_model, dict):
        mid = (session_model.get("id") or "").strip()
        prov = (session_model.get("providerID") or "").strip()
    if not mid and prov and ":" in prov:
        prov, mid = prov.split(":", 1)
    return (mid or "unknown", prov or "unknown")


def _fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _pct(sorted_vals, p):
    """Nearest-rank percentile of an already-sorted non-empty list (issue #8)."""
    k = min(len(sorted_vals), max(1, math.ceil(p * len(sorted_vals))))
    return sorted_vals[k - 1]


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


def _streaks(day_set, anchor_date=None):
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
    # "Current" is measured from `anchor_date` (the window end, capped at today)
    # or, when all-time, from today.
    ref = anchor_date if anchor_date is not None else date.today()
    anchor = ref if ref.isoformat() in day_set else (
        ref - timedelta(days=1) if (ref - timedelta(days=1)).isoformat() in day_set else None
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

    # tool wall-time per turn, keyed by msg id (index-independent, so a windowed
    # subset of turns can look up its own tool time without position bookkeeping)
    tool_time = {t["msg_id"]: 0.0 for t in turns}
    for tp in tool_parts:
        i = turns_index.get(tp["msg"])
        if i is None or not tp["start"] or not tp["end"] or tp["end"] < tp["start"]:
            continue
        tool_time[turns[i]["msg_id"]] += (tp["end"] - tp["start"]) / 1000.0

    return {
        "db": os.path.abspath(db_path),
        "sessions": sessions,
        "turns": turns,
        "tool_parts": tool_parts,
        "compactions": compactions,
        "tool_time": tool_time,
        "now_ms": int(time.time() * 1000),
        "scan_ms": int((time.time() - t0) * 1000),
        "scan_rows": {"sessions": len(sessions), "turns": len(turns), "tool_parts": len(tool_parts)},
    }


def aggregate(raw, start_ms=None, end_ms=None):
    """Aggregate raw scan intermediates into the dashboard payload.

    start_ms / end_ms define the turn-creation window [start, end) in epoch ms
    (server-local dates, produced by the caller). Both None = all-time, which
    must reproduce the pre-refactor payload byte-for-byte (modulo the
    inherently time-based generated_at / scan_ms).

    Window semantics: a turn is in-window iff its `created` is in [start, end);
    tool parts inherit membership from their owning turn (by part message id),
    compactions use their own timestamp; session-level surfaces collapse to the
    window's sessions. Session-lifetime token sums become in-window turn sums
    when windowed.
    """
    now_ms = raw["now_ms"]
    all_sessions = raw["sessions"]
    tool_time = raw["tool_time"]

    if start_ms is None:
        windowed = False
        win_end = now_ms
        wturns, wparts, wcomps = raw["turns"], raw["tool_parts"], raw["compactions"]
        wsessions = all_sessions
    else:
        windowed = True
        # A future end (custom range reaching tomorrow) can't swallow "now" —
        # but the from/to date bounds used below stay the user's actual dates.
        win_end = min(end_ms, now_ms)
        wturns = [t for t in raw["turns"] if t["created"] is not None and start_ms <= t["created"] < win_end]
        win_msg = {t["msg_id"] for t in wturns}
        wparts = [p for p in raw["tool_parts"] if p["msg"] in win_msg]
        wcomps = [c for c in raw["compactions"] if c is not None and start_ms <= c < win_end]
        wsess = {t["session"] for t in wturns}
        wsessions = {sid: s for sid, s in all_sessions.items() if sid in wsess}

    sessions = wsessions
    turns, tool_parts, compactions = wturns, wparts, wcomps
    # per-turn tool wall-time, parallel to `turns` (same index trick as before)
    tool_time_by_turn = [tool_time.get(t["msg_id"], 0.0) for t in turns]

    data = {"generated_at": int(time.time()), "db": raw["db"]}

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
    data["n_sessions"] = len(wsessions)
    data["n_subagents"] = sum(1 for s in wsessions.values() if s["parent"])
    data["n_turns"] = len(turns)
    # Tokens KPI: all-time = session-lifetime totals (tracks the whole inbox);
    # windowed = in-window assistant-turn tokens (session totals would leak the
    # full lifetime of every windowed session).
    if windowed:
        data["total_tokens"] = sum(t["in"] + t["out"] + t["reason"] + t["cache_r"] for t in turns)
    else:
        data["total_tokens"] = sum(s["tokens"] for s in wsessions.values())
    data["total_in"] = sum(t["in"] for t in turns)
    data["total_out"] = sum(t["out"] for t in turns)
    data["total_reason"] = sum(t["reason"] for t in turns)
    data["total_cache_r"] = sum(t["cache_r"] for t in turns)
    data["api_errors"] = sum(1 for t in turns if t["error"])
    data["median_turn_s"] = round(statistics.median(turn_durs), 1) if turn_durs else None
    data["median_tps"] = None  # set after throughput pass
    # Streaks: all-time anchors to today; a window anchors to min(range end,
    # today) so a historical range shows the streak as it stood then.
    if windowed:
        dte_end, _, _ = _local(end_ms - 1)
        streak_anchor = min(date.fromisoformat(dte_end), date.today()) if dte_end else date.today()
    else:
        streak_anchor = None
    data["streaks"] = _streaks(day_set, streak_anchor)

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
    if windowed:
        # date bounds stay the user's actual [from, to] even when `to` is ahead
        # of now (membership was already clamped above)
        dmin, _, _ = _local(start_ms)
        dmax, _, _ = _local(end_ms - 1)
        keys = [k for k in sorted(daily) if dmin and dmax and dmin <= k <= dmax]
    else:
        keys = sorted(daily)
    data["daily"] = [{"date": k, **daily[k]} for k in keys]

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
    if windowed:
        for t in turns:
            agents[t["agent"]]["tokens"] += t["in"] + t["out"] + t["reason"] + t["cache_r"]
    else:
        for s in sessions.values():
            agents[s["agent"]]["tokens"] += s["tokens"]
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
        if m["label"] is None:
            m["label"] = f"{key[0] or '?'} @ {key[1] or '?'}"
    if windowed:
        for t in turns:
            m = models.setdefault(t["model"], {"label": f"{t['model'][0] or '?'} @ {t['model'][1] or '?'}", "sessions": 0, "tokens": 0, "turns": 0, "errors": 0, "tps": []})
            m["tokens"] += t["in"] + t["out"] + t["reason"] + t["cache_r"]
    else:
        for s in sessions.values():
            models[_pick_model(None, None, s["model"])]["tokens"] += s["tokens"]
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
        if m["turns"] == 0 and m["tokens"] == 0:
            continue  # phantom rows: no turns and no tokens (issue #12)
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

    # ---- recent throughput (HUD) --------------------------------------------
    # Per-turn tok/s for every completed turn, using the same pure-time filter
    # as the model-level throughput above (tool wait excluded, 1..3600 s, out > 0).
    recent = []
    tps_all = []  # unrounded values, for stats
    for i, t in enumerate(turns):
        if not (t["completed"] and t["created"]):
            continue
        pure = (t["completed"] - t["created"]) / 1000.0 - tool_time_by_turn[i]
        if not (1.0 <= pure <= 3600.0 and t["out"] > 0):
            continue
        v = t["out"] / pure
        tps_all.append(v)
        recent.append({
            "ts": t["completed"],
            "model": t["model"][0] or "?",
            "end": f"{t['model'][0] or '?'} @ {t['model'][1] or '?'}",
            "tps": round(v, 1),
            "_v": v,  # unrounded value, dropped before serving
            "out": t["out"],
            "dur": round(pure, 1),
        })
    recent.sort(key=lambda r: r["ts"])
    # "avg last 10" is TIME-sorted by completion (issue #8), unlike the old
    # storage-order mean; computed on unrounded values so the mean stays exact.
    data["recent_tps_avg10"] = round(statistics.mean(r["_v"] for r in recent[-10:]), 1) if recent else None
    for r in recent:
        r.pop("_v")
    data["recent_tps"] = recent[-30:]
    data["median_tps"] = round(statistics.median(tps_all), 1) if tps_all else None
    ip = None
    for t in turns:
        if t["created"] and not t["completed"] and (ip is None or t["created"] > ip["created"]):
            ip = t
    data["recent_in_progress"] = (
        {"model": ip["model"][0] or "?",
         "end": f"{ip['model'][0] or '?'} @ {ip['model'][1] or '?'}",
         "started": ip["created"]}
        if ip else None
    )

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
            "p50_s": round(_pct(lat, 0.50), 3) if lat else None,
            "p95_s": round(_pct(lat, 0.95), 3) if lat else None,
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
    # 14d/prev-14d compaction windows anchor to min(range end, today);
    # all-time anchors to now (unchanged).
    comp_anchor = win_end if windowed else now_ms
    comp_recent = sum(1 for c in compactions if c and (comp_anchor - c) <= 14 * 86400000)
    comp_prev = sum(1 for c in compactions if c and 14 * 86400000 < (comp_anchor - c) <= 28 * 86400000)
    data["context"] = {
        "histogram": {
            "bins": [
                f"{bin_edges[i] // 1000}k-{bin_edges[i + 1] // 1000}k"
                if bin_edges[i + 1] != float("inf")
                else f"{bin_edges[i] // 1000}k+"
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
    if windowed:
        for t in turns:
            d = sessions[t["session"]]["dir"]
            projects.setdefault(d, {"sessions": 0, "tokens": 0})["tokens"] += t["in"] + t["out"] + t["reason"] + t["cache_r"]
    else:
        for s in sessions.values():
            projects[s["dir"]]["tokens"] += s["tokens"]
    data["projects"] = [{"dir": k, **v} for k, v in sorted(projects.items(), key=lambda kv: -kv[1]["tokens"])]

    # ---- top sessions ---------------------------------------------------------------
    turns_per_session = Counter(t["session"] for t in turns)
    errs_per_session = Counter(t["session"] for t in turns if t["error"])
    if windowed:
        tok_by_sess = Counter()
        for t in turns:
            tok_by_sess[t["session"]] += t["in"] + t["out"] + t["reason"] + t["cache_r"]
    span = {}
    if windowed:
        # in-window activity span per session (first turn start -> last turn
        # end) — session-lifetime duration would leak outside the window
        for t in turns:
            end_t = t["completed"] if (t["completed"] and t["completed"] > t["created"]) else t["created"]
            lo, hi = span.get(t["session"], (t["created"], end_t))
            span[t["session"]] = (min(lo, t["created"]), max(hi, end_t))
    top = []
    for sid, s in sessions.items():
        if windowed and sid in span:
            dur = (span[sid][1] - span[sid][0]) / 1000.0
        elif s["updated"] and s["created"] and s["updated"] >= s["created"]:
            dur = (s["updated"] - s["created"]) / 1000.0
        else:
            dur = None
        top.append({
            "id": sid,
            "title": s["title"],
            "agent": s["agent"],
            "model": _pick_model(None, None, s["model"])[0],
            "dir": (s["dir"] or "").rstrip("/").split("/")[-1] or (s["dir"] or ""),
            # windowed: ranked by in-window turn tokens; all-time: session-lifetime
            "tokens": tok_by_sess[sid] if windowed else s["tokens"],
            "turns": turns_per_session.get(sid, 0),
            "errors": errs_per_session.get(sid, 0),
            "duration_s": round(dur) if dur is not None else None,
            "subagent": 1 if s["parent"] else 0,
        })
    top.sort(key=lambda r: -r["tokens"])
    data["top_sessions"] = top[:20]

    # ---- insights ---------------------------------------------------------------------
    data["insights"] = _insights(turns, sessions, data, tool_parts)
    data["scan_ms"] = raw["scan_ms"]
    data["scan_rows"] = raw["scan_rows"]
    return data


def aggregate_cached(raw, start_ms=None, end_ms=None):
    """aggregate() with a small per-range LRU (see _agg).

    Windowed re-requests — the browser's 120s refresh, users flipping
    7d/30d/custom — otherwise re-aggregate the full raw (~400ms at ~34k
    turns) on every hit. Returns the stored payload on a hit; aggregate() is
    pure over raw, so the stored dict is safe to serve repeatedly. (A cached
    payload's generated_at is its computation time; the frontend does not
    display it.)
    """
    key = (start_ms, end_ms)
    with _ctx_lock:
        data = _agg.get(key)
        if data is not None:
            _agg.move_to_end(key)
            return data
    data = aggregate(raw, start_ms, end_ms)
    with _ctx_lock:
        _agg[key] = data
        _agg.move_to_end(key)
        while len(_agg) > AGG_CACHE_MAX:
            _agg.popitem(last=False)
    return data


def _day_ms(day_str):
    """Local midnight epoch-ms for a 'YYYY-MM-DD' date string."""
    y, m, d = (int(x) for x in day_str.split("-"))
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)) * 1000)


def _parse_range(qs):
    """Parse a ?days=N or ?from=D&to=D query string into (start_ms, end_ms).

    (None, None) when unset (all-time). Raises ValueError with a short reason
    for bad input (callers map it to a 400). Semantics:
      - days=N: local [today-(N-1) .. today] inclusive
      - from/to: local [from .. to] inclusive, must be a paired, real, ordered
        YYYY-MM-DD pair
    """
    p = parse_qs(qs)
    days = p.get("days", [None])[0]
    frm = p.get("from", [None])[0]
    to = p.get("to", [None])[0]
    if frm is not None or to is not None:
        # explicit from/to win over days (issue #5 contract)
        if (frm is None) != (to is None):
            raise ValueError("from and to must be given together")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", frm) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", to):
            raise ValueError("from and to must be YYYY-MM-DD")
        try:
            d_f, d_t = date.fromisoformat(frm), date.fromisoformat(to)
        except ValueError:
            raise ValueError("invalid date")
        if d_f > d_t:
            raise ValueError("from is after to")
        try:
            start = _day_ms(frm)
            end = _day_ms((d_t + timedelta(days=1)).isoformat())
        except (OverflowError, ValueError):
            raise ValueError("range out of representable date range") from None
        return start, end
    if days is not None:
        if not re.fullmatch(r"\d+", days) or int(days) < 1:
            raise ValueError(f"days must be a positive integer, got {days!r}")
        today = date.today()
        try:
            start = _day_ms((today - timedelta(days=int(days) - 1)).isoformat())
            end = _day_ms((today + timedelta(days=1)).isoformat())
        except (OverflowError, ValueError):
            raise ValueError("range out of representable date range") from None
        return start, end
    return None, None


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
        (m for m in data["models"] if m["turns"] >= 20),
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

    if data["api_errors"]:
        top_e, n_e = Counter(t["error"] for t in turns if t["error"]).most_common(1)[0]
        out.append(
            f"API errors: {data['api_errors']} failed turn{'s' if data['api_errors'] != 1 else ''} "
            f"— most common: {top_e} ({100 * n_e / data['api_errors']:.0f}% of errors)."
        )

    tot_wall = sum(t["wall_s"] for t in data["tools"])
    if tot_wall > 0:
        top_tool = max(data["tools"], key=lambda t: t["wall_s"])
        out.append(
            f"{top_tool['tool']} ate {100.0 * top_tool['wall_s'] / tot_wall:.0f}% of all tool wall-time ({top_tool['wall_s'] / 3600:.1f} h)."
        )

    if any(any(row) for row in data["hourly"]):
        best_w = max(range(7), key=lambda w: sum(data["hourly"][w]))
        best_h = max(range(24), key=lambda h: data["hourly"][best_w][h])
        dow = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][best_w]
        out.append(f"Peak hours: on {dow} around {best_h}:00 local time.")

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


def _probe_sig(db_path):
    """Content staleness probe: per-table (count, max created, max updated).

    The mtime of a WAL database that opencode itself writes to churns on every
    commit (and on checkpoint noise), so an mtime gate rescans on every
    request. A content signature instead: both inserts and updates advance
    the timestamp maxima (opencode maintains time_updated), so the probe
    can tell real data changes from WAL churn. It is a few small scans —
    ~5% of a full scan's cost — and raises sqlite3.Error (missing/unreadable
    DB) exactly like a scan would.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return tuple(
            conn.execute(
                f"SELECT COUNT(*), MAX(time_created), MAX(time_updated) FROM {t}"
            ).fetchone()
            for t in ("session", "message", "part")
        )
    finally:
        conn.close()


def get_cache(force=False):
    """Raw-scan cache: rescan when force'd, when the content signature
    changed, or when the TTL expires — but at most once per RESCAN_MIN_S
    (debounce: on a live DB the signature flips with every opencode commit,
    and a ~2.4 s full scan per request is the cost of that; a usage
    dashboard tolerates a 15 s freshness cap, and POST /api/refresh stays
    the explicit fresh-data escape hatch).
    """
    now = time.time()
    db = _cache["db_path"]
    if force:
        stale = True
    else:
        sig = _probe_sig(db)  # may raise sqlite3.Error -> 503 at the caller
        stale = (
            _cache["data"] is None
            or sig != _cache["key"]
            or (now - _cache["ts"]) > TTL_S
        )
        if stale and (now - _cache["ts"]) < RESCAN_MIN_S:
            stale = False  # debounce: bounded staleness beats a full scan
    if stale:
        with _ctx_lock:
            # re-check inside the lock (a concurrent caller may have refreshed)
            now = time.time()
            if force and now - _cache["ts"] < 1.0:
                return _cache["data"]  # throttle hammering refreshes
            if _cache["data"] is not None:
                should_scan = True
                if force:
                    pass
                else:
                    sig = _probe_sig(db)
                    fresh = sig == _cache["key"] and (now - _cache["ts"]) <= TTL_S
                    should_scan = (not fresh) and (now - _cache["ts"]) >= RESCAN_MIN_S
                if not should_scan:
                    return _cache["data"]
            _cache["data"] = scan(db)
            _cache["key"] = _probe_sig(db)
            _cache["ts"] = time.time()
            _agg.clear()  # new raw generation -> every cached aggregate is stale
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
        path, _, qs = self.path.partition("?")
        if path == "/api/health":
            try:
                d = get_cache()
            except sqlite3.Error as e:
                self._json({"ok": False, "error": f"database unavailable: {e}"}, 503)
                return
            self._json({
                "ok": d is not None,
                "cache_age_s": round(time.time() - _cache["ts"], 1),
                "scan_ms": (d or {}).get("scan_ms"),
                "rows": (d or {}).get("scan_rows"),
                "db": _cache["db_path"],
            })
        elif path == "/api/all":
            try:
                start, end = _parse_range(qs)
            except ValueError as e:
                self._json({"error": f"bad range: {e}"}, 400)
                return
            try:
                d = aggregate_cached(get_cache(), start, end)
            except sqlite3.Error as e:
                # 503 path returns before the payload is built, so no
                # raw_scan_ts to attach (also guards a still-None raw cache).
                self._json({"error": f"database unavailable: {e}"}, 503)
                return
            # raw_scan_ts = generation time of the raw scan this payload was
            # aggregated from (float, seconds). Attached here — at the HTTP
            # layer — on a FRESH shallow copy: the aggregate LRU stores the
            # payload by reference, so it must never carry this key. Lets the
            # live-mode client skip re-renders when nothing was rescanned.
            self._json({"raw_scan_ts": _cache["ts"], **d})
        elif path in STATIC_FILES:
            fpath = os.path.join(APP_DIR, "static", STATIC_FILES[path])
            try:
                with open(fpath, "rb") as f:
                    body = f.read()
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
            try:
                d = get_cache(force=True)
            except sqlite3.Error as e:
                self._json({"error": f"database unavailable: {e}"}, 503)
                return
            self._json({"ok": d is not None, "scan_ms": (d or {}).get("scan_ms"), "rows": (d or {}).get("scan_rows")})
        else:
            self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="OpenCode usage dashboard")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to opencode.db")
    ap.add_argument("--demo", action="store_true",
                    help="serve a deterministic synthetic dataset "
                         "(no real data; for trying it out or screenshots)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    if args.demo:
        from opencode_deck.demo import build_demo_db
        db = os.path.expanduser("~/.cache/opencode-deck/demo.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        if not os.path.exists(db):
            build_demo_db(db)
        print(f"opencode-deck: demo mode — synthetic data at {db}",
              file=sys.stderr)
    elif not os.path.exists(args.db):
        raise SystemExit(f"DB not found: {args.db}")
    else:
        db = args.db
    _cache["db_path"] = os.path.abspath(db)
    get_cache(force=True)  # warm at startup
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"opencode-deck: http://{args.host}:{args.port} (db={_cache['db_path']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
