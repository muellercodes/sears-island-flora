#!/bin/bash
# Watch a shared folder, identify anything new, publish it.
#
#   ./scripts/autopilot.sh ~/Dropbox/plant-photos
#
# Safe to run on a timer: if there are no new photos it does nothing and exits.
# Photos already in the library are matched by content hash and skipped, so the
# same folder can be pointed at repeatedly.

set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

INBOX="${1:-${PLANT_INBOX:-}}"
[ -z "$INBOX" ] && { echo "usage: $0 /path/to/shared/folder   (or set PLANT_INBOX)"; exit 1; }
[ -d "$INBOX" ] || { echo "Shared folder not found: $INBOX"; exit 1; }

mkdir -p logs
LOG="$ROOT/logs/autopilot.log"
say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3

say "checking $INBOX"

# 1. Pull in anything new. Bail out early if there wasn't anything.
before=$($PY - <<'EOF'
import json,pathlib
p = pathlib.Path("data/observations.json")
print(len(json.load(open(p))) if p.exists() else 0)
EOF
)
$PY scripts/plantdb.py ingest "$INBOX" --batch "$(date '+%Y-%m-%d')" >>"$LOG" 2>&1
after=$($PY - <<'EOF'
import json,pathlib
p = pathlib.Path("data/observations.json")
print(len(json.load(open(p))) if p.exists() else 0)
EOF
)

if [ "$before" = "$after" ]; then
  say "no new photos"
  exit 0
fi
say "ingested $((after - before)) new photo(s)"

# 2. Identify them.
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  say "WARNING: no ANTHROPIC_API_KEY — photos added but not identified"
else
  $PY scripts/identify.py >>"$LOG" 2>&1 || say "WARNING: identification had errors (see log)"
fi

# 3. Publish — but never push anything carrying precise location data.
if ! $PY scripts/plantdb.py verify >>"$LOG" 2>&1; then
  say "ABORTED: privacy check failed — nothing pushed. Run: python3 scripts/plantdb.py scrub"
  exit 1
fi
$PY scripts/plantdb.py publish >>"$LOG" 2>&1

if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -q -m "Add $((after - before)) photo(s) from $(date '+%Y-%m-%d')"
  if git push -q origin main 2>>"$LOG"; then
    say "pushed — site rebuilds in about a minute"
  else
    say "WARNING: push failed (see log). Committed locally."
  fi
else
  say "nothing to commit"
fi
