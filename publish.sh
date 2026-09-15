#!/usr/bin/env bash
#
# Gate + publisher for opencode-deck.
#
# Publish a released tag as a squashed snapshot to GitHub main:
#   git tag v0.1.0
#   ./publish.sh v0.1.0            # full run
#   ./publish.sh v0.1.0 --dry-run  # gate check only, nothing pushed
#
# Gate any ref (for CI + pre-push hooks — no tag, nothing pushed):
#   ./publish.sh --gate HEAD
#   ./publish.sh --gate refs/heads/main
#
# What the gate does (both modes):
#   1. Secret-scans the resolved tree (not history, not the working dir).
#   2. Rejects forbidden files (.env, *.pem, *.key, private key files).
#   3. Fails closed: an error inside git grep aborts instead of skipping a scan.
#
# What publish additionally does:
#   4. Pushes the tag to the internal Forgejo origin (idempotent).
#   5. Builds a fresh single-commit repo from exactly the tagged tree and
#      force-pushes it to GitHub main via a write-only deploy key.
#      -> GitHub never sees dev history, branch names, or commit messages.
#
# Required: write-only GitHub deploy key at ~/.ssh/opencode-deck-gh-publish
set -euo pipefail

TAG=""
DRY_RUN=0
NOTE=""
GATE_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --note=*)   NOTE="${1#--note=}"; shift ;;
    --gate)
      [ -n "${2:-}" ] || { echo "--gate requires a ref (e.g. HEAD, main, v0.1.0)" >&2; exit 2; }
      GATE_REF="$2"; shift 2 ;;
    -h|--help)
      echo "usage: $(basename "$0") <tag e.g. v0.1.0> [--note='gh#12 — @user'] [--dry-run]"
      echo "       $(basename "$0") --gate <ref>"
      exit 0 ;;
    -*) echo "unknown flag: $1 (run with --help)" >&2; exit 2 ;;
    *)
      [ -z "$TAG" ] || { echo "only one tag arg expected" >&2; exit 2; }
      TAG="$1"; shift ;;
  esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
GH_DEST="ssh://git@github.com/dereklarmstrong/opencode-deck.git"
KEY="$HOME/.ssh/opencode-deck-gh-publish"

cd "$ROOT"

# --- gate (shared core — used by both --gate and publish) -------------------
PATTERNS=(
  '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
  'ghp_[A-Za-z0-9]{36,}'
  'github_pat_[A-Za-z0-9_]{30,}'
  '(^|[^a-zA-Z0-9])sk-[A-Za-z0-9_-]{32,}'
  'AKIA[0-9A-Z]{16}'
  'xox[abpos]-[A-Za-z0-9-]{10,}'
)

gate_tree() { # $1 = any revspec that resolves to a commit
  local REF="$1" SHA BAD_FILES P HITS rc FAIL=0
  if ! SHA="$(git rev-parse -q --verify "${REF}^{commit}")"; then
    echo "gate error: cannot resolve '$REF' to a commit" >&2
    return 2
  fi
  echo "== gate: scanning $REF ($SHA) =="

  # gate 1: forbidden files (structural — content is irrelevant)
  BAD_FILES="$(git ls-tree -r "$SHA" --name-only | grep -E '(^|/)(\.env(\..*)?|.*\.pem$|.*\.key$|.*\.p12$|id_ed25519$|id_rsa$)' || true)"
  [ -n "$BAD_FILES" ] && {
    echo "gate BLOCKED: forbidden file(s) in tree:"; echo "$BAD_FILES" >&2; FAIL=1
  }

  # gate 2: secret patterns in the tree
  for P in "${PATTERNS[@]}"; do
    # fail-closed: rc 0 = match, 1 = no match, >1 = error (abort — never skip a scan)
    rc=0
    HITS="$(git grep -IInE -e "$P" "$SHA" -- .)" || rc=$?
    if [ "$rc" -gt 1 ]; then
      echo "gate error: git grep failed (rc=$rc) for pattern: $P" >&2
      return 2
    fi
    [ -n "$HITS" ] && {
      echo "gate BLOCKED by secret pattern: $P"; echo "$HITS" | head -10 >&2; FAIL=1
    }
  done

  if [ "$FAIL" -ne 0 ]; then
    echo "gate FAILED for $REF — fix the tree and rerun." >&2
    return 1
  fi
  echo "gate passed ($(git ls-tree -r "$SHA" --name-only | wc -l) files, no findings)"
}

gate() {
  [ -z "$TAG" ] || { echo "give a tag OR --gate <ref>, not both" >&2; exit 2; }
  gate_tree "$GATE_REF"
  [ "$DRY_RUN" -eq 1 ] && echo "[dry-run] gate mode (nothing to publish)" || true
  exit 0
}

# --- dispatch -----------------------------------------------------------------
if [ -n "$GATE_REF" ]; then
  gate
fi

[ -n "$TAG" ] || {
  echo "usage: $(basename "$0") <tag e.g. v0.1.0> [--note=...] [--dry-run] | --gate <ref>" >&2; exit 2;
}

# --- preflight: publish --------------------------------------------------------
git rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null \
  || { echo "no local tag '$TAG' — create one first: git tag $TAG" >&2; exit 2; }
SHA="$(git rev-parse "${TAG}^{commit}")"
gate_tree "$TAG"

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
ssh_push() {
  GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$WORK/known_hosts -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 $1" \
    git -C "$SNAP" push -f "$GH_DEST" main
}
if ! ssh_push ""; then
  echo "push over :22 failed — retrying via ssh.github.com:443" >&2
  ssh_push "-o Hostname=ssh.github.com -o Port=443"
fi

echo "✓ published $TAG → GitHub main (snapshot of $SHA)"
