#!/usr/bin/env bash
# Run the HTTP server (for ChatGPT / claude.ai) at login, restarted if it dies.
#   scripts/install-macos-service.sh https://cronometer.example.com [port]
# Logs: ~/Library/Logs/cronometer-mcp.log
set -euo pipefail
PUBLIC_URL="${1:?usage: $0 https://your-public-host [port]}"
PORT="${2:-8430}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
LABEL="com.cronometer-mcp.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/cronometer-mcp.log"

grep -q '^MCP_AUTH_PASSWORD=.\+' "$REPO/.env" 2>/dev/null || {
  echo "Set MCP_AUTH_PASSWORD in $REPO/.env first (see .env.example)." >&2; exit 1; }

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>--frozen</string><string>cronometer-mcp</string>
    <string>--transport</string><string>http</string>
    <string>--port</string><string>$PORT</string>
    <string>--public-url</string><string>$PUBLIC_URL</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL -> http://127.0.0.1:$PORT/mcp (public $PUBLIC_URL/mcp). Log: $LOG"
