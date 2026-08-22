# Contributing

opencode-deck is a single-module, stdlib-only dashboard. That constraint is
part of the design, so:

- **No new dependencies.** `sqlite3` + `http.server`, nothing else. No frameworks.
- **Tests are the regression floor:** `python3 -m unittest discover -s tests`
  (runs against a fixture DB in a temp dir — never a real `opencode.db`).
- **The DB is read-only.** All access goes through `mode=ro`; nothing here
  writes to, or assumes write access on, your opencode data.
- The frontend is one `index.html` with inline JS + vendored Chart.js. Keep it that way.

## Pull requests

- Open PRs against `main`. **Don't push directly** — `main` here is a
  publish-only snapshot and is never edited in place.
- PRs are reviewed with the test suite and shipped as tagged snapshots
  (`vX.Y.Z`). When your change ships, the PR is closed with a note pointing
  at the tag — that tag is your upstream merge.
- Keep PRs small. Doc fixes, bugfixes, and clearly-scoped feature slices are
  welcome; open an issue first for anything bigger.

## Not planned (won't merge)

- Auth, multi-user, session writing, or any write path to `opencode.db`
- New runtime dependencies
- Serving from anything other than `127.0.0.1` by default (bind wider at your
  own risk — that's your call at deploy time)
