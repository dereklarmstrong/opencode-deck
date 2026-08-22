#!/usr/bin/env bash
#
# Gate: publish a released tag as a squashed snapshot to GitHub main.
#
#   git tag v0.1.0
#   ./publish.sh v0.1.0          # full run
#   ./publish.sh v0.1.0 --dry-run  # gate check only, nothing pushed
#
# What it does:
#   1. Secret-scans the exact tagged tree (not history, not working dir).
#   2. Rejects forbidden files (.env, *.pem, *.key, private key files).
#   3. Pushes the tag to the internal Forgejo origin (idempotent).
#   4. Builds a fresh single-commit repo from exactly the tagged tree and
#      force-pushes it to GitHub main via a write-only deploy key.
#      -> GitHub never sees dev history, branch names, or commit messages.
#
# Required: write-only GitHub deploy key at ~/.ssh/opencode-deck-gh-publish
set -euo pipefail

TAG=""
DRY_RUN=0
NOTE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --note=*) NOTE="${arg#--note=}" ;;
    -h|--help)
      echo "usage: $(basename "$0") <tag e.g. v0.1.0> [--note='gh#12 — @user'] [--dry-run]"; exit 0 ;;
    *) TAG="$arg" ;;
  esac
done
[ -n "$TAG" ] || { echo "usage: $(basename "$0") <tag e.g. v0.1.0> [--note=...] [--dry-run]" >&2; exit 2; }

ROOT="$(cd "$(dirname "$0")" && pwd)"
GH_DEST="ssh://git@github.com/dereklarmstrong/opencode-deck.git"
KEY="$HOME/.ssh/opencode-deck-gh-publish"

cd "$ROOT"

# --- preflight ---------------------------------------------------------------
git rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null \
  || { echo "no local tag '$TAG' — create one first: git tag $TAG" >&2; exit 2; }
SHA="$(git rev-parse "${TAG}^{commit}")"
echo "== gate: scanning tree of $TAG ($SHA) =="

# --- gate 1: forbidden files (structural — content is irrelevant) ------------
BAD_FILES="$(git ls-tree -r "$TAG" --name-only | grep -E '(^|/)(\.env(\..*)?|.*\.pem$|.*\.key$|.*\.p12$|id_ed25519$|id_rsa$)' || true)"

# --- gate 2: secret patterns in the tree -------------------------------------
PATTERNS=(
  '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
  'ghp_[A-Za-z0-9]{36,}'
  'github_pat_[A-Za-z0-9_]{30,}'
  '(^|[^a-zA-Z0-9])sk-[A-Za-z0-9_-]{32,}'
  'AKIA[0-9A-Z]{16}'
  'xox[abpos]-[A-Za-z0-9-]{10,}'
)
FAIL=0
[ -n "$BAD_FILES" ] && {
  echo "BLOCKED: forbidden file(s) in tree:"; echo "$BAD_FILES" >&2; FAIL=1
}
for P in "${PATTERNS[@]}"; do
  # fail-closed: rc 0 = match, 1 = no match, >1 = error (abort — never skip a scan)
  HITS=""
  rc=0
  HITS="$(git grep -IInE -e "$P" "$TAG" -- .)" || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "gate error: git grep failed (rc=$rc) for pattern: $P" >&2
    exit 2
  fi
  [ -n "$HITS" ] && {
    echo "BLOCKED by secret pattern: $P"; echo "$HITS" | head -10 >&2; FAIL=1
  }
done
[ "$FAIL" -eq 0 ] || {
  echo "Publish blocked. Fix the tree, re-cut the tag, rerun." >&2; exit 1
}
echo "gate passed ($(git -C . ls-tree -r "$TAG" --name-only | wc -l) files, no findings)"

# --- publish ------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] gate OK — would push tag to origin and snapshot $SHA to $GH_DEST (main)"
  exit 0
fi

# tag to the internal primary (idempotent if already pushed)
git push origin "$TAG"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SNAP="$WORK/snap"
mkdir -p "$SNAP"
git archive "$TAG" | tar -xf - -C "$SNAP"
git -C "$SNAP" init -q -b main
git -C "$SNAP" config user.name  "opencode-deck publish"
git -C "$SNAP" config user.email "publish@opencode-deck"
git -C "$SNAP" add -A
git -C "$SNAP" commit -qm "$TAG — published from $SHA${NOTE:+ | $NOTE}"

[ -f "$KEY" ] || { echo "missing deploy key: $KEY" >&2; exit 2; }
chmod 600 "$KEY"
# never prompt in a non-interactive context: file may be empty (egress blip);
# accept-new takes no prompt, ConnectTimeout bounds the rest.
timeout 15 ssh-keyscan github.com > "$WORK/known_hosts" 2>/dev/null || true
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$WORK/known_hosts -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
git -C "$SNAP" push -f "$GH_DEST" main

echo "✓ published $TAG → GitHub main (snapshot of $SHA)"
