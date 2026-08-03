#!/bin/bash
# Install the every-15-minutes watcher.
#
#   ./scripts/install-watcher.sh                      # read the shared Drive folder
#   ./scripts/install-watcher.sh ~/Dropbox/photos     # ...or a local folder
#
# Only install this if the pipeline is NOT running in GitHub Actions. Both commit
# to data/, and identifications.db is binary — git cannot merge a conflict there.
set -euo pipefail
cd "$(dirname "$0")/.."; ROOT="$PWD"
# Optional: with no argument the watcher reads the shared Drive folder, which is
# where contributors actually put photos. Pass a local folder only for the
# pre-Drive path.
INBOX="${1:-}"
[ -z "$INBOX" ] || [ -d "$INBOX" ] || { echo "No such folder: $INBOX"; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.mueller.searsisland.plist"
mkdir -p "$HOME/Library/LaunchAgents"
if [ -n "$INBOX" ]; then
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__INBOX__|$INBOX|g" \
      scripts/com.mueller.searsisland.plist > "$PLIST"
else
  # Drop the argument entirely rather than passing an empty string — launchd would
  # hand autopilot.sh an empty $1, which is harmless but reads as a mistake.
  sed -e "s|__ROOT__|$ROOT|g" -e "/__INBOX__/d" \
      scripts/com.mueller.searsisland.plist > "$PLIST"
fi
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Watcher installed. Checking ${INBOX:-the shared Drive folder} every 15 minutes."
echo "Log:      tail -f $ROOT/logs/autopilot.log"
echo "Stop it:  launchctl unload $PLIST"
