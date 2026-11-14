#!/bin/sh
# Token collector for Linux machines (e.g. a Claude Code workspace container on Unraid).
# Runs every 5 minutes via cron, or, without cron, as a small background loop.
#   sh scripts/install-linux.sh            install or update
#   sh scripts/install-linux.sh uninstall  remove the cron entry / stop the loop
# Reports under SOURCE_NAME (default: the host name) so machines do not mix their rows.
set -eu

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/claude-usage"
CONFIG_DIR="$HOME/.config/claude-usage"
CONFIG="$CONFIG_DIR/config"
LOG="$APP_DIR/tokens.log"
PY="${PYTHON:-$(command -v python3 || true)}"
MARK="# claude-usage-tokens"

if [ -z "$PY" ]; then
    echo "python3 not found." >&2
    exit 1
fi

stop_loop() {
    if [ -f "$APP_DIR/loop.pid" ]; then
        kill "$(cat "$APP_DIR/loop.pid")" 2>/dev/null || true
        rm -f "$APP_DIR/loop.pid"
    fi
}

if [ "${1:-}" = "uninstall" ]; then
    if command -v crontab >/dev/null 2>&1; then
        (crontab -l 2>/dev/null | grep -v "$MARK") | crontab - || true
    fi
    stop_loop
    echo "Removed."
    exit 0
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR"
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
    printf 'SERVER_URL=%s\nAPI_TOKEN=%s\nSOURCE_NAME=%s\n' "$url" "$token" "$(hostname -s 2>/dev/null || hostname)" > "$CONFIG"
fi
grep -q '^SOURCE_NAME=' "$CONFIG" || echo "SOURCE_NAME=$(hostname -s 2>/dev/null || hostname)" >> "$CONFIG"
chmod 600 "$CONFIG"

CMD="$PY $APP_DIR/claude_tokens.py >> $LOG 2>&1"
if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v "$MARK"; echo "*/5 * * * * $CMD $MARK") | crontab -
    echo "Cron entry added (every 5 minutes). Log: $LOG"
else
    stop_loop
    nohup sh -c "while true; do $CMD; sleep 300; done" >/dev/null 2>&1 &
    echo $! > "$APP_DIR/loop.pid"
    echo "No cron found: started a background loop (PID $(cat "$APP_DIR/loop.pid"))."
    echo "It runs until the container restarts; run this command again afterwards"
    echo "or add it to the container's start command."
fi
$PY "$APP_DIR/claude_tokens.py" >> "$LOG" 2>&1 || true
tail -1 "$LOG"
