#!/bin/bash
# Watch a shared folder, identify anything new, take in steward verifications,
# publish the result.
#
#   ./scripts/autopilot.sh ~/Dropbox/plant-photos
#
# Safe to run on a timer. Photos already in the library are matched by content
# hash and skipped, so the same folder can be pointed at repeatedly.
#
# Note this does NOT exit early when no photos arrive. Steward verifications land
# in the sheet independently of new photos, and someone who spends a quiet Tuesday
# confirming records should not have that work sit unpublished until the next
# photo happens to show up.

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

# Credentials up front: the sheet sync needs GOOGLE_*, not just ANTHROPIC_API_KEY.
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

say "checking $INBOX"

# 1. Take in whatever the stewards verified since last time. Optional — a project
#    without a sheet configured just skips it.
if [ -n "${GOOGLE_SHEET_ID:-}" ] && [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
  if $PY scripts/plantdb.py sheet-pull --yes >>"$LOG" 2>&1; then
    say "pulled steward verifications"
  else
    say "WARNING: sheet-pull failed (see log) — continuing with local data"
  fi
fi

# 2. Pull in any new photos.
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

added=$((after - before))
[ "$added" -gt 0 ] && say "ingested $added new photo(s)"

# 3. Identify anything new.
if [ "$added" -gt 0 ]; then
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    say "WARNING: no ANTHROPIC_API_KEY — photos added but not identified"
  else
    $PY scripts/identify.py >>"$LOG" 2>&1 || say "WARNING: identification had errors (see log)"
  fi
fi

# Nothing to do unless something actually changed. Both new photos and pulled
# verifications write to tracked files, so the working tree is the honest test —
# and it avoids re-hashing every thumbnail on an idle tick.
if [ -z "$(git status --porcelain)" ]; then
  say "nothing changed"
  exit 0
fi

# 4. Publish — but never push anything carrying precise location data.
# verify covers EXIF, coordinate precision, and whether records fall inside the
# survey area. The area check turns binding the first time a real Sears Island
# record is published, so a run can start failing here purely because stand-in
# data is still in the published set — the log says which.
if ! $PY scripts/plantdb.py verify >>"$LOG" 2>&1; then
  say "ABORTED: verify failed — nothing pushed. Last lines of the log:"
  tail -n 12 "$LOG" | sed 's/^/    /'
  exit 1
fi

# Must not fall through on failure: ingest and identify have already written to
# data/, so committing after a failed publish would push records whose images
# never reached R2 — a site full of broken photos.
if ! $PY scripts/plantdb.py publish >>"$LOG" 2>&1; then
  say "ABORTED: publish failed — nothing pushed. Last lines of the log:"
  tail -n 12 "$LOG" | sed 's/^/    /'
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  git add -A
  if [ "$added" -gt 0 ]; then
    msg="Add $added photo(s) from $(date '+%Y-%m-%d')"
  else
    msg="Steward verifications, $(date '+%Y-%m-%d')"
  fi
  git commit -q -m "$msg"
  if git push -q origin main 2>>"$LOG"; then
    say "pushed — site rebuilds in about a minute"
  else
    say "WARNING: push failed (see log). Committed locally."
  fi
else
  say "nothing to commit"
fi

# 5. Put new and changed records in front of the stewards. Last, so the sheet
#    reflects what is actually published rather than what we hoped to publish.
if [ -n "${GOOGLE_SHEET_ID:-}" ] && [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
  if $PY scripts/plantdb.py sheet-push >>"$LOG" 2>&1; then
    say "sheet updated for review"
  else
    say "WARNING: sheet-push failed (see log)"
  fi
fi
