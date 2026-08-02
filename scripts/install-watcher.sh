#!/bin/bash
# Install the every-15-minutes watcher.   ./scripts/install-watcher.sh ~/Dropbox/plant-photos
set -euo pipefail
cd "$(dirname "$0")/.."; ROOT="$PWD"
INBOX="${1:?usage: $0 /path/to/shared/folder}"
[ -d "$INBOX" ] || { echo "No such folder: $INBOX"; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.mueller.searsisland.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__INBOX__|$INBOX|g" scripts/com.mueller.searsisland.plist > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Watcher installed. Checking $INBOX every 15 minutes."
echo "Log:      tail -f $ROOT/logs/autopilot.log"
echo "Stop it:  launchctl unload $PLIST"
