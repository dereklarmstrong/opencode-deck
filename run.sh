#!/usr/bin/env bash
# Convenience launcher. Point at a different DB with: ./run.sh --db /path/to/db
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m opencode_deck.server --host 127.0.0.1 --port 8799 "$@"
