#!/bin/sh
# Installs the token collector as a LaunchAgent that runs every 5 minutes.
#   sh scripts/install.sh            install or update
#   sh scripts/install.sh uninstall  remove the LaunchAgent (config and cache stay)
set -eu

LABEL="app.rillwerk.clstats.tokens"
# Label of older versions, removed on install
OLD_LABEL="de.svenrill.clstats.tokens"
APP_DIR="$HOME/Library/Application Support/claude-usage"
CONFIG_DIR="$HOME/.config/claude-usage"
CONFIG="$CONFIG_DIR/config"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-usage-tokens.log"
PY="${PYTHON:-/usr/bin/python3}"

if [ "${1:-}" = "uninstall" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "LaunchAgent removed."
    exit 0
fi

launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$(dirname "$0")/claude_tokens.py" "$APP_DIR/claude_tokens.py"

if [ ! -f "$CONFIG" ]; then
    printf "Server address (e.g. https://clstats.example.com): "
    read -r url
    printf "API_TOKEN: "
    stty -echo 2>/dev/null || true
    read -r token
    stty echo 2>/dev/null || true
    echo
    umask 077
    printf 'SERVER_URL=%s\nAPI_TOKEN=%s\n' "$url" "$token" > "$CONFIG"
    echo "Saved to $CONFIG"
fi
chmod 600 "$CONFIG"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$APP_DIR/claude_tokens.py</string>
    </array>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><true/>
    <key>LowPriorityIO</key><true/>
    <key>Nice</key><integer>10</integer>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. First run starts now, log: $LOG"
