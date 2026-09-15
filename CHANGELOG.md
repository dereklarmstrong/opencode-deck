# Changelog

User-facing changes per release. The private Forgejo repo is tagged at ship;
the public GitHub mirror only ever receives tagged-tree snapshots.

## v0.7.0 — 2026-09-15

- **Demo mode** (`--demo`): `opencode-deck --demo` serves a deterministic
  synthetic ~90-day dataset (`opencode_deck/demo.py`) — ~150 sessions, ~32k
  turns, ~47k tool parts, 4 models across anthropic/openai/google/ollama, 7
  tools, ~20% subagent children, context compactions, realistic API-error
  turns (429/502/503/504), a night-owl hourly curve, and in-progress work on
  the current day. Every identifier is fictional (no real projects, hosts,
  people, or sessions), so it is safe for trying the dashboard without an
  opencode install or for privacy-free screenshots. Built once into
  `~/.cache/opencode-deck/demo.db`; same seed (default 42) → identical data.
- Test suite is now green at any time of day: fixtures pin "today" activity
  to a recent-past instant (now − 5 min) instead of a fixed 08:00 wall time,
  which previously made the windowed range/debounce tests fail whenever the
  suite ran before 08:00 local (new turns must never be in the future for a
  `?days=N` window that is clamped to `now`).
- Test suite grown to 76 (11 new `tests/test_demo.py`: schema, size, seed
  determinism, seed divergence, no-private-strings, full aggregate coverage,
  realistic error classes, night-owl curve, windowing, CLI smoke).

## v0.6.0 — 2026-09-14

- **Live mode** (issue #13): optional auto-refresh. A `live` toggle in the
  header (default off, persisted) polls `/api/all` every 30 s. The server now
  stamps each response with `raw_scan_ts`, the generation time of the raw
  scan it was aggregated from; a poll with an unchanged stamp skips the
  re-render entirely, so idle polling costs nothing but a sub-second round
  trip. Poll failures keep the last data, flag a connection error, and
  auto-recover. Indicators respect `prefers-reduced-motion`.
- Test suite grown to 65.

## v0.5.2 — 2026-09-14

**Performance (cycle 2).**

- Per-range aggregate LRU: `/api/all` results are memoized per
  `(start_ms, end_ms)` (bounded, 64 entries) for the current raw generation —
  repeating a range is now near-free instead of re-aggregating the full raw
  scan every request.
- Content-signature staleness gate replaced the mtime gate. The DB this
  dashboard watches is the one opencode itself writes to while it runs, and a
  WAL database's mtime churns on every commit — so the old gate forced a full
  ~2.5 s rescan on *every* request. Staleness is now detected with a few
  small per-table probes (`COUNT(*)`, `MAX(time_created)`,
  `MAX(time_updated)`, ~5% of a scan's cost), and rescans are floored at one
  per 15 s (`RESCAN_MIN_S`); the 120 s TTL stays as the upper bound.
  `POST /api/refresh` remains the fresh-data escape hatch.
- Measured on the live ~34k-turn DB: repeated same-range request ~2.9 s →
  ~0.12 s; distinct ranges 0.1–0.7 s; real data changes still land within the
  debounce floor.
- Test suite grown to 60.

## v0.5.1 — 2026-09-14

**Defect cycle (issues #6–#12).**

- #6: `/api/all` robustness — range overflow (e.g. `days=99999`) → 400
  `bad range` instead of 500; missing/unreadable DB → 503 `database
  unavailable` JSON (not 500) on all endpoints incl. health.
- #7: Calendar windows match the selected range (n days = trailing n days
  incl. today).
- #8: p50/p95 use nearest-rank; `recent_tps_avg10` is over the last 10
  completed turns in completion order, unrounded.
- #9: Insights — full weekday names in "peak hours", `128k+` context bin,
  "flakiest" needs ≥20 samples, new "API errors: N failed turns — most
  common: X (P%)" insight.
- #10: No page overflow at 1440/1280/375 — in-card `.tscroll`, `min-width:0`,
  label ellipsis + title tooltips, `#range` flex-wrap.
- #11: Header meta and KPIs are scoped to the selected window ("lifetime" vs
  "in window" qualifiers); no whole-DB headlines.
- #12: Phantom model rows dropped; `provider:model` shorthand resolved
  per-message (model id no longer swallows the provider).
- Test suite grown to 54.

## v0.5.0 — 2026-09-13

- **Theme support** (issue #2): six palettes (dark, light, nord, dracula,
  everforest, everforest light), WCAG 4.5:1/3:1 contrast-gated, persisted in
  localStorage, defaults to system preference.
- **Date-range knob** (issue #5): all time / 7d / 30d / 90d / custom; the
  server now serves windowed payloads (`days=N` or `from=…&to=…`, inclusive
  local dates, from/to beat days, malformed → 400); URL-deep-linkable.
- `scan()` refactored to raw + `aggregate(raw, start_ms, end_ms)`; golden
  replay suite (`tests/golden_alltime.json`) pins the all-time payload to the
  pre-refactor implementation.
- Favicon 404 suppressed; range URL clears on "all".

## v0.4.0 — 2026-09-13

- **Performance HUD** (top of page): last completed turn's tok/s, last-30
  turn trend vs median, avg-last-10 delta, and in-progress turn heads-up.

## v0.3.0 — 2026-09-11

- **Sortable table headers** (issue #1): endpoint / tools / sessions tables.
- CONTRIBUTING: issue label set + triage convention.

## v0.2.1 — 2026-09-11

- QA: reusable gate mode in `publish.sh`, CI workflow, local pre-push hook —
  unit tests + secret scan now run on every push even when no runner is
  online.
- CI suite hardening (manual dispatch, runner label match, gate on HEAD).

## v0.2.0 — 2026-08-22

- Shipping pipeline: `publish.sh` (secret-scan gate + GitHub snapshot
  mirror), `ssh.github.com:443` fallback for flaky :22 egress; inbound PR
  lane docs.

## v0.1.0 — 2026-08-21

- First release: read-only single-page dashboard over `opencode.db` — KPIs,
  auto-generated insights, activity (daily flow, hour×weekday + calendar
  heatmaps), agent/model/tool breakdowns with p50/p95 latency,
  context-health, top sessions, error breakdowns. Stdlib Python server,
  vanilla JS + vendored Chart.js, fixture-based test suite.
