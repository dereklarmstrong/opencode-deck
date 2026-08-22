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
```

No dependencies: Python 3 stdlib only (sqlite3, http.server, json).
Chart.js is vendored in `static/`, no build step, no internet needed.

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
```

Or straight from the repo:

```bash
pipx install "opencode-deck @ git+https://github.com/dereklarmstrong/opencode-deck.git"
```

## What it shows

- KPIs: sessions, turns, subagent calls, total tokens, streaks, API errors
- Auto-generated insights (night-owl score, fastest/flakiest model, context rot, compaction trend, ...)
- Daily token flow, sessions/turns over time
- Hour × weekday heatmap + calendar heatmap
- Agent usage, model usage with estimated tok/s throughput + error rates
- Tool usage with error rates and p50/p95 latency
- Context-health histogram, compactions per week
- Top token-burning sessions

## API

- `GET /api/all` — full aggregate payload (one round trip)
- `POST /api/refresh` — force re-scan of the DB
- `GET /api/health` — cache age, scan duration, row counts

The DB is opened read-only (`mode=ro`); the dashboard never writes to it.
Aggregates are computed on each scan (mtime-gated, TTL 120s) and served from memory.

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
the exact tagged tree — dev history never crosses the wire.

Inbound: issues and PRs are welcome here (see `CONTRIBUTING.md`). PRs are
pulled down, reviewed with the test suite, and shipped as tags — PRs are
never merged directly into `main`.
