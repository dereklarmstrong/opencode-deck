# DATE-RANGE KNOB — implementation contract (issue #5)

## Ground rules (non-negotiable)
- Python 3.9 stdlib only. ZERO new dependencies. No frameworks, no pip installs.
- DO NOT change the three SQL reads in `scan()` (session / message / part). Same queries, same streaming cursors.
- The default `GET /api/all` with NO query params must return a payload BYTE-IDENTICAL to the current implementation (modulo generated_at/scan_ms which are inherently time-based). This is the regression floor — enforced by tests/test_golden.py (deterministic golden replay against the pre-refactor implementation).
- Run `python3 -m unittest discover -s tests` and keep it green.
- Frontend is a single `opencode_deck/static/index.html` (no build step).

## KEY STRATEGY
Current `scan(db)` (server.py ~L117-397): three SQL reads → build in-memory intermediates (`sessions` dict, `turns` list, `tool_parts` list, `compactions` list) → compute every payload section → return payload. Today it caches the *payload* (`_cache["data"] = scan(db)`).

Do NOT rewrite the aggregation math from scratch:
1. Refactor `scan()` so that after the three SQL reads it RETURNS THE RAW INTERMEDIATES (sessions, turns, tool_parts, compactions, tool_time-by-msgid, scan_ms, scan_rows, now_ms, db) rather than the final payload.
2. LIFT THE EXISTING aggregation code (KPIs, daily, hourly, calendar, agents, models, tools, api_errors_top, context, projects, top_sessions[:20], insights _insights(...) — server.py ~L190-397) into a new `aggregate(raw, start_ms=None, end_ms=None)`. Same payload from in-memory rows, with an optional inclusive time-window filter `[start_ms, end_ms)` applied to the row universe. All-time (start_ms=None) must reproduce the current output exactly.
3. Cache stores `raw` keyed by DB mtimes, as before. On request: range given → `aggregate(raw, start_ms, end_ms)`; else all-time. A pass over ~18k in-memory turn dicts is trivial — no per-range rescans, no per-range cache slots.

Turn membership: a turn is in-window iff its `created` timestamp is in `[start, end)`. `completed` NEVER participates in membership.

## API (new query-string support on `/api/all`)
- `from=YYYY-MM-DD&to=YYYY-MM-DD` — inclusive both ends, **server-local** dates (same `_local()` mktime/localtime convention as today). Convert to ms: local midnight of `from`; end = local midnight of (`to` + 1 day) EXCLUSIVE.
- `days=N` — last N days including today. Explicit `from`/`to` win over `days`.
- No params → all-time, exactly today's payload.
- Malformed dates, `from > to`, or `days <= 0` → HTTP 400 with JSON `{"error": "bad range: ..."}`.
- IMPORTANT: today `do_GET` STRIPS the query string on `/api/all` — change that to parse it.

## Per-surface window behavior
- `n_sessions`/`n_subagents`: sessions with >=1 turn in window. (all-time = all sessions, unchanged.)
- `n_turns`, `api_errors`, `median_turn_s`, `median_tps`: in-window turns only.
- `total_in/out/reason/cache_r`: in-window turns.
- `total_tokens`: ALL-TIME = session-lifetime (unchanged); WINDOWED = in-window turn tokens. The all-time number is deliberately NOT redefined.
- `streaks`: over in-window turn days, anchored to `min(range end, today)`; all-time unchanged.
- `daily`, `hourly`, `calendar`: in-window dates only (daily filtered to [from,to]; hourly/calendar naturally only in-window turns).
- `agents`, `models`(sessions, turns, tokens, errors, tok/s, err%): in-window turns/sessions; tokens from in-window turn tokens (not session-lifetime) when windowed, session-lifetime when all-time.
- `tools`(calls, errors, p50/p95, wall): parts whose attributed turn is in-window (membership INHERITED FROM THE TURN). all-time = all parts (unchanged, incl. orphan parts).
- `context`(histogram, bloat%): in-window turns; the 14d / prev-14d compaction windows ANCHOR TO min(range end, today). all-time anchors to now (unchanged).
- `compactions`, `api_errors_top`: in-window.
- `projects`: in-window sessions + in-window turn tokens.
- `top_sessions`: ranked by IN-WINDOW turn tokens (header win); title/agent/model/dir from session row; `duration_s` stays session-lifetime (UI documents this). all-time = all sessions, session-lifetime tokens (unchanged).
- `insights`: computed from in-window universe (`_insights` already gets row collections); existing min-sample rules guard small windows; may be empty for thin window — render nothing.
  - NOTE: _insights currently reads `data["n_sessions"]`, `data["context"]`, `data["models"]`, `data["tools"]`, `data["hourly"]`, `data["top_sessions"]`, and iterates `turns`/`tool_parts`. When windowed, pass the WINDOWED turns/tool_parts and the WINDOWED `data` so every insight is window-scoped. All-time passes full sets (unchanged).

## Edge cases
- Empty window (no activity in range): valid empty payload — zeros, empty arrays, `insights: []` (or the single "No assistant turns" line) — via existing empty guards; UI renders cleanly.
- Range end in the future: end (exclusive) capped at "now" for membership + anchoring; but daily date bounds stay [from,to].
- 1-2 day windows: valid; thin heatmaps, min-sample rules may empty insights — accepted.
- DST/local boundaries: convert the two local dates to ms once via mktime(local struct_time), identical to existing _local convention. No new IANA-TZ handling.
- Pre-existing server-TZ vs browser-TZ calendar skew: OUT OF SCOPE. Don't make it worse, don't fix.

## Frontend (`opencode_deck/static/index.html`)
- Add a global date-window knob in the sticky header (currently `h1 · #meta · .spacer · #refresh`, flex, 14px gap — slot between `.spacer` and `#refresh`).
- Presets: `all`(default) / `7d` / `30d` / `90d` + `custom` revealing two native `<input type="date">` (from / to).
- Module-scope `rangeState` (same cross-renders survival as existing module-scope `sortState`). Changing the knob re-fetches `/api/all` with params.
- The 120s `setInterval` auto-refresh and the manual refresh/`load()` path KEEP the active window (build the fetch URL from `rangeState` each time).
- Persist the window in the URL via `history.replaceState` (e.g. `?d=30` or `?from=...&to=...`); on load, read it to restore the window.
- `#meta` line shows the active window ("... · last 30d"); when windowed, section subtitles/captions reflect the window.
- Both `sortState` and `rangeState` survive re-renders.

## Acceptance criteria
1. No-param `/api/all` is BYTE-IDENTICAL to today (diff against GOLDEN_ALLTIME.json, ignoring generated_at/scan_ms). ALL 17 existing tests stay GREEN and UNCHANGED — they are the value-level regression floor.
2. New tests (new `tests/test_range.py` and/or extend `tests/test_api.py`), all passing under `python3 -m unittest discover -s tests`:
   - inclusive boundaries: turn/compaction exactly at from 00:00:00.000 and exactly at to 23:59:59.999 included; the ms after excluded;
   - `days=30` equivalent to matching from/to;
   - malformed / `from > to` / `days <= 0` → 400 JSON error;
   - empty window → valid empty payload;
   - re-ranking: a session heavy BEFORE the window and one heavy INSIDE → windowed `top_sessions` leads with the in-window one;
   - `total_tokens` dual semantics (session-lifetime unwindowed vs in-window windowed);
   - compaction 14d/prev-14d anchored to range end;
   - streaks and `models` (tok/s, err%) recompute correctly for the window.
3. Frontend: header shows preset control + custom range; selecting a preset updates all sections + `#meta`; URL restores the window on reload; auto-refresh and rescan keep the window; both states survive re-renders.
4. Browser verification per PLAN.md: playwright smoke — control visible, click-through presets, no console errors, plus a screenshot with a 30d window active.
5. No new deps, no SQL change, Python 3.9 stdlib only, existing tests green.

## Non-goals (do NOT build)
- Hourly/time-of-day range granularity (dates only); per-section independent date filters; IANA/per-user TZ; multi-range cache slots or row streaming/pagination; fixing the pre-existing server/browser TZ calendar skew.
