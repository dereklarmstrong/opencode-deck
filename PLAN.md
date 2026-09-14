# OpenCode Dashboard — Plan

## Review Log (honest process)

### v1 (first draft)
Read-only single-page dashboard over `~/.local/share/opencode/opencode.db`:
Python stdlib server + Chart.js, ~12 API endpoints (one per chart), metrics:
cost over time, tokens, agents, models, tools, latency, hours of day, projects,
sessions table, "sparklines".

**Review 1 — what was wrong:**
1. **Cost metrics are dead** — every model here is local (vader/ollama/vaulttec/...),
   `cost` is $0.00 across all 665 sessions. Replaced with *estimated throughput
   (tok/s)*, which is the actually-useful tuning signal for local endpoints.
2. **12 endpoints is over-engineering** for a single client page. One `/api/all`
   round trip, done in the SSE-less, no-framework spirit of the project.
3. **Cache invalidation was hand-waved.** The real DB is 771MB and mtime changes
   on every keystroke in opencode (WAL). Solution: cache key = mtime, TTL floor
   120s, plus manual refresh. A scan is a few seconds max; worst case we scan
   once per 2 minutes while typing.
4. **Testing plan was "test against the real DB"** — no. Tests build a synthetic
   fixture DB in a temp dir and assert exact aggregate values. Real DB gets a
   live smoke test with SQL cross-checks.
5. **The `event` table (258k rows) was going to be scanned.** It's not needed —
   messages and parts already carry timestamps. Dropped.
6. **Throughput formula was unspecified.** Set: per assistant message,
   `pure_gen = (completed−created) − Σ(tool window durations)`;
   `tok/s = tokens.output / pure_gen`; only samples with 1s ≤ pure_gen ≤ 3600s
   and tokens.output > 0 count; report per-model **median** (robust to spikes).
   Documented as an estimate (pre/queue time contaminates it).
7. **Edge cases ignored:** missing `time.updated`, `pending`/`running` tool parts,
   malformed JSON rows, empty DB, zero denominators. All get guards.
8. **Network exposure:** bind `127.0.0.1` by default; `--host` flag for LAN use.

### v2 (this plan)
Incorporates all 8 fixes. Second review pass:

**Review 2 — what else:**
- Scan reads **only needed columns** from message/part (not the full `data`
  blob when possible); part.time parsing tolerates missing start/end.
- Heatmap + calendar are hand-rolled CSS grids (fewer deps than a chart lib feature).
- Single page, no tabs, no framework: KPIs → Insights → Activity → Agents/Models
  → Tools → Context health → Top sessions. Dark, terminal-flavored.
- Timezone: serve local-time buckets (server-local), document it.
- Subagent insight: sessions with `parent_id` are subagent invocations —
  "delegation rate" is a fun one.
- `question` tool calls: how many times the agent poked me. Keep it.

## Final Spec

### Data source
SQLite at `~/.local/share/opencode/opencode.db` (opencode 1.18.18), opened
read-only. Tables used: `session` (665), `message` (17,811), `part` (74,672).
`event`, `todo`, `workspace`, `project*` not needed (session.directory covers
project buckets).

### Architecture
- `server.py` — stdlib `http.server.ThreadingHTTPServer` + `sqlite3`.
  On first request (or when cache stale): scan → build aggregates dict
  (pure Python, in-memory). Serve from memory.
- `static/index.html` — single page, vanilla JS, Chart.js 4.4.9 (vendored
  UMD), no build step. Fetches `/api/all`, renders.
- Endpoints: `/` (index), `/api/all`, `POST /api/refresh`, `/api/health`.
- Defaults: `127.0.0.1:8799`, `--host --port --db` flags.
- Cache: `(db_mtime, wal_mtime)` + 120s TTL; refresh forces rescan.

### Metrics (and why they're interesting)
| Metric | Source | Why |
|---|---|---|
| KPIs: sessions, turns, subagent calls, total tokens, current/longest streak, API errors | session/message | the headline numbers |
| Daily token flow (in/out/reasoning/cache-read stacked) | message tokens | watch context bloat grow across the day |
| Sessions & turns per day | times | rhythm, "are I burning out" |
| Hour × weekday heatmap | message times | night-owl score; shows the 8-5 job gap |
| Calendar heatmap (last ~9 weeks) | message times | GitHub-style streak glance |
| Agent breakdown (sessions, tokens, errors) | session.agent / message.agent | which agent eats the most |
| Model usage + **median tok/s** + error rate | message modelID, time, tokens, error | **the tuning signal** for local vLLM/ollama endpoints |
| Tool usage: count, error rate, p50/p95 latency, total wall-time | part (tool) | which tool breaks, which one hangs |
| Context health: input-token histogram per turn, compactions/week, % turns ≥64k | message.tokens.input, compaction parts | when starts hitting the context wall |
| Error breakdown: top API error messages w/ host, top failing tools | message.error, tool parts | endpoint health (e.g. the 404 on :4242) |
| Project breakdown (tokens/sessions by directory) | session.directory | where the activity actually is |
| Top-20 token-burning sessions (title, agent, model, duration, errors) | joined | the big spenders |
| Auto-insights (bullets) | computed heuristics below | the "wow, didn't know that" layer |

Heuristics (computed server-side, stable ordering):
1. Night-owl score — % of turns 21:00–02:00
2. Delegation rate — subagent sessions / total
3. Context rot — % of turns with input ≥ 64k tokens
4. Compaction trend — last 14d vs prior 14d
5. Fastest / flakiest model (min 5 samples each)
6. Most errant tool (min 10 calls)
7. Wall-time dominance — top tool's % of total tool wall-time
8. Productive hour — peak hour by turns
9. Longest turn + heaviest session (names)
10. "Agent asked N questions" (question tool)

### UI
Dark terminal aesthetic (bg #0b0f14, green accent #7ee787, mono). Sticky
header with data-age + refresh button. Sections scroll top-to-bottom; every
chart has a title + one-line "so what". Graceful degradation if chart.js is
missing (data tables still render).

### Testing
- `tests/test_api.py` (unittest): builds fixture DB (3 sessions, messages with
  2 errors + token sets, 10 parts incl. error/pending tools, compactions,
  subagent parent links), starts server on ephemeral port, asserts:
  KPIs, hours bucket, tool error count, p95, streaks, insights non-empty,
  model throughput min-samples rule, empty-DB safety (second fixture),
  zero-denominator guards, `/` returns HTML.
- Live smoke: real DB, `curl /api/all`, cross-check 4–5 numbers against raw
  SQL counts.
- Browser: playwright screenshot + console-error check.

### Out of scope (v1)
Auth, multi-user, live/SSE updates, cost (everything's local), event table,
Docker, auto-start. All candidate follow-ups.
