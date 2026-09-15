# OpenCode Dashboard

| Field | Value |
|-------|-------|
| Created | 2026-08-21 |

Read-only analytics dashboard for my OpenCode usage. Point it at
`~/.local/share/opencode/opencode.db` and get a single-page web app with
usage, activity patterns, agent/model/tool breakdowns, latency, context-health,
and auto-generated insights.

## Run

```bash
./run.sh                 # http://localhost:8799
# or:
python3 -m opencode_deck.server --port 8799 --db ~/.local/share/opencode/opencode.db
# demo data — no real opencode DB required:
python3 -m opencode_deck.server --demo
```

No dependencies: Python 3 stdlib only (sqlite3, http.server, json).
Chart.js is vendored in `static/`, no build step, no internet needed.

`--demo` builds a deterministic synthetic ~90-day dataset (~150 sessions,
~32k turns, 4 models, subagents, compactions, error turns, night-owl curve)
into `~/.cache/opencode-deck/demo.db`. Every identifier is fictional — no real
projects, hosts, or sessions — so it's safe for trying the dashboard out or
for privacy-free screenshots.

## Install (optional)

If you want `opencode-deck` on your PATH in an isolated env:

```bash
pipx install .          # -> ~/.local/bin/opencode-deck
# or: uv tool install .
```

Then:

```bash
opencode-deck                    # http://127.0.0.1:8799
opencode-deck --port 9000 --db /path/to/opencode.db
opencode-deck --demo             # synthetic data, no real DB
```

Or straight from the repo:

```bash
pipx install "opencode-deck @ git+https://github.com/dereklarmstrong/opencode-deck.git"
```

## What it shows

- Performance HUD (top of the page): last completed turn's tok/s, last-30-turn
  trend vs median, avg-last-10 delta, and in-progress turn heads-up
- Date-range knob (top bar): all time / 7d / 30d / 90d / custom — windows every
  metric; deep-linkable via `?days=N` or `?from=…&to=…`
- Live mode (top bar): optional 30 s auto-refresh, persisted; re-renders only
  when the DB was actually rescanned (`raw_scan_ts` unchanged → skip)
- Six themes (dark, light, nord, dracula, everforest, everforest light) —
  WCAG 4.5:1 / 3:1 contrast-tested, persisted, defaults to system preference
- KPIs: sessions, turns, subagent calls, total tokens, streaks, API errors
- Auto-generated insights (night-owl score, fastest/flakiest model, context rot, compaction trend, ...)
- Daily token flow, sessions/turns over time
- Hour × weekday heatmap + calendar heatmap
- Agent usage, model usage with estimated tok/s throughput + error rates
- Tool usage with error rates and p50/p95 latency
- Context-health histogram, compactions per week
- Top token-burning sessions

## API

- `GET /api/all` — full aggregate payload (one round trip), including
  `raw_scan_ts`, the generation time (float seconds) of the raw scan the
  payload was aggregated from. Optional range:
  `days=N` (last N days incl. today) or `from=YYYY-MM-DD&to=YYYY-MM-DD`
  (inclusive, server-local dates); explicit from/to win over days; no params =
  all-time; malformed ranges → 400 `{"error": "bad range: …"}`
- `POST /api/refresh` — force re-scan of the DB
- `GET /api/health` — cache age, scan duration, row counts

The DB is opened read-only (`mode=ro`); the dashboard never writes to it.
Aggregates are computed on each scan and served from memory.

**Caching.** The DB this dashboard watches is the one OpenCode itself writes
to while sessions run, and a WAL database's mtime churns on every commit — so
an mtime gate would force a full ~2.5 s rescan on every request. Instead the
cache is gated on a content signature: per-table `COUNT(*)`,
`MAX(time_created)`, `MAX(time_updated)` probes (~5% of a scan's cost) detect
real data changes, rescans are floored at one per 15 s (`RESCAN_MIN_S`), and
per-range aggregates are LRU-cached for the current raw generation, so
repeating a range is near-free. `POST /api/refresh` stays as the fresh-data
escape hatch.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests run against a synthetic fixture DB in a temp dir — never the real one.

## Ops (optional)

`systemd/opencode-dashboard.service` is a unit file you *can* install if this
survives the fun phase:

```bash
sudo cp systemd/opencode-dashboard.service /etc/systemd/system/
sudo systemctl enable --now opencode-dashboard
```

## Publishing (maintainers)

Development happens on the private Forgejo mirror; this GitHub repo is a
publish-only snapshot. To ship a release:

```bash
git tag v0.1.0
./publish.sh v0.1.0    # gate: secret-scan the tagged tree, then mirror it to GitHub main
```

`publish.sh` refuses to run if the tagged tree contains private keys, provider
tokens, or forbidden files (`.env`, `*.pem`, `*.key`). GitHub only ever sees
the exact tagged tree — dev history never crosses the wire. The gate also
runs standalone against any ref (`./publish.sh --gate HEAD`) — that's the
entry point CI and the local pre-push hook invoke.

**Checks.** `git config core.hooksPath .githooks` enables a local pre-push
gate: every `git push` runs the unit tests + the scanner against HEAD first,
so nothing bad reaches `main` even on days when no runner is online. A `CI`
workflow (`.gitea/workflows/ci.yml`) does the same on a runner — unit tests,
wheel build + install-into-clean-venv smoke, gate — on pushes, PRs, and
publish tags.

Inbound: issues and PRs are welcome here (see `CONTRIBUTING.md`). PRs are
pulled down, reviewed with the test suite, and shipped as tags — PRs are
never merged directly into `main`.
