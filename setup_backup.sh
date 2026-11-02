#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="/opt/Port-Tunnel-Bot"
PYTHON="$PROJECT/venv/bin/python"
BACKUP="$PROJECT/backup.py"
DATABASE="$PROJECT/bot.sqlite3"
CONFIG="/etc/port-tunnel-bot/backup.env"
CRON_FILE="/etc/cron.d/port-tunnel-backup"
LOG="/var/log/port-tunnel-backup.log"
STATE_DIR="/var/lib/port-tunnel-bot-backup"
BACKUP_DIR="/var/backups/port-tunnel-bot"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BLUE='\033[0;34m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
info()    { echo -e "${CYAN}[*]${NC} $1"; }
warning() { echo -e "${YELLOW}[!]${NC} $1"; }
line()    { printf '%b\n' "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

backup_banner() {
    echo -e "${CYAN}${BOLD}"
    echo '  ╔══════════════════════════════════════════════════════╗'
    echo '  ║             PORT TUNNEL  |  BACKUP MANAGER           ║'
    echo '  ╚══════════════════════════════════════════════════════╝'
    echo -e "${NC}${DIM}          Secure SQLite snapshots | Telegram delivery${NC}"
    line
}

require_root() {
    if [[ "$EUID" -ne 0 ]]; then
        error "Please run this operation as root."
        exit 1
    fi
}

status_backup() {
    echo "========== BACKUP STATUS =========="
    if [[ -f "$CRON_FILE" ]]; then
        success "Automatic backups are enabled."
        echo "Cron configuration:"
        grep -vE '^(SHELL|PATH)=' "$CRON_FILE" || true
    else
        echo "Automatic backups are disabled."
    fi
    if [[ -f "$CONFIG" ]]; then
        echo "Configuration file: present (credentials hidden)"
        if [[ -x "$PYTHON" ]]; then
            "$PYTHON" - "$CONFIG" <<'PY'
import sys
values = {}
for line in open(sys.argv[1], encoding="utf-8"):
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.rstrip().split("=", 1)
        values[k] = v
print("Backup interval:", values.get("BACKUP_INTERVAL_MINUTES", "unknown"), "minutes")
print("Database path:", values.get("DATABASE_PATH", "default"))
PY
        fi
    else
        echo "Configuration file not found."
    fi
    echo "Log file: $LOG"
}

disable_backup() {
    rm -f "$CRON_FILE"
    success "Automatic backups disabled. Settings and existing archives were preserved."
}

uninstall_backup() {
    rm -f "$CRON_FILE"
    rm -f "$CONFIG"
    rm -rf "$STATE_DIR"
    success "Backuper removed. Existing backup archives are preserved in $BACKUP_DIR."
}

install_backup() {
    backup_banner
    echo -e "${WHITE}${BOLD}  BACKUP SETUP WIZARD${NC}"
    line

    [[ -f "$BACKUP" ]] || { error "backup.py not found: $BACKUP"; return 1; }
    [[ -x "$PYTHON" ]] || { error "Python virtual environment not found. Install the bot first."; return 1; }
    if ! command -v cron >/dev/null 2>&1; then
        echo "Installing the Cron service..."
        apt-get update
        apt-get install -y cron
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now cron >/dev/null 2>&1 || systemctl start cron >/dev/null 2>&1 || true
    elif command -v service >/dev/null 2>&1; then
        service cron start >/dev/null 2>&1 || true
    fi
    [[ -f "$DATABASE" ]] || { error "Database not found. Install and start the bot first."; return 1; }
    "$PYTHON" -c 'import aiohttp' >/dev/null 2>&1 || {
        error "The aiohttp package is missing from the project environment."; return 1;
    }

    echo
    read -rsp "Dedicated Backup Bot Token: " BOT_TOKEN
    echo
    if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
        error "The token format does not look valid."
        unset BOT_TOKEN
        return 1
    fi

    read -rp "Destination Chat ID (numeric ID or @channel_username): " CHAT_ID
    if [[ ! "$CHAT_ID" =~ ^-?[0-9]+$ && ! "$CHAT_ID" =~ ^@[A-Za-z0-9_]{5,}$ ]]; then
        error "Chat ID must be numeric or formatted as @channel_username."
        unset BOT_TOKEN CHAT_ID
        return 1
    fi

    echo
    read -rp "Backup interval in minutes [30]: " INTERVAL
    INTERVAL="${INTERVAL:-30}"
    if [[ ! "$INTERVAL" =~ ^[0-9]+$ ]] || (( INTERVAL < 1 || INTERVAL > 525600 )); then
        error "Interval must be a number between 1 and 525600 minutes."
        unset BOT_TOKEN CHAT_ID INTERVAL
        return 1
    fi

    echo
    echo "Validating the bot token with Telegram..."
    export BACKUP_BOT_TOKEN="$BOT_TOKEN"
    if ! "$PYTHON" - <<'PY'
import asyncio, os, aiohttp, sys
async def main():
    url = f"https://api.telegram.org/bot{os.environ['BACKUP_BOT_TOKEN']}/getMe"
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                data = await response.json(content_type=None)
                if response.status != 200 or not data.get("ok"):
                    print("Telegram rejected the token:", data.get("description", response.status))
                    return 1
                bot = data["result"]
                print(f"Token verified: @{bot.get('username', 'unknown')}")
                return 0
    except Exception as exc:
        print("Could not connect to Telegram:", exc)
        return 1
sys.exit(asyncio.run(main()))
PY
    then
        unset BOT_TOKEN CHAT_ID INTERVAL BACKUP_BOT_TOKEN
        return 1
    fi

    unset BACKUP_BOT_TOKEN
    mkdir -p /etc/port-tunnel-bot "$STATE_DIR"
    chmod 700 /etc/port-tunnel-bot "$STATE_DIR"

    local OLD_CONFIG=""
    if [[ -f "$CONFIG" ]]; then
        OLD_CONFIG=$(mktemp)
        cp -p "$CONFIG" "$OLD_CONFIG"
    fi

    export BACKUP_BOT_TOKEN="$BOT_TOKEN" BACKUP_CHAT_ID="$CHAT_ID" \
        BACKUP_INTERVAL_MINUTES="$INTERVAL" BACKUP_DATABASE_PATH="$DATABASE"
    "$PYTHON" - <<'PY'
import os
from pathlib import Path

path = Path("/etc/port-tunnel-bot/backup.env")
content = (
    f"BACKUP_BOT_TOKEN={os.environ['BACKUP_BOT_TOKEN']}\n"
    f"BACKUP_CHAT_ID={os.environ['BACKUP_CHAT_ID']}\n"
    f"BACKUP_INTERVAL_MINUTES={os.environ['BACKUP_INTERVAL_MINUTES']}\n"
    f"DATABASE_PATH={os.environ['BACKUP_DATABASE_PATH']}\n"
)
tmp = path.with_suffix(".env.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(content)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
    local CONFIGURED_INTERVAL="$INTERVAL"
    unset BOT_TOKEN CHAT_ID INTERVAL BACKUP_BOT_TOKEN BACKUP_CHAT_ID BACKUP_INTERVAL_MINUTES BACKUP_DATABASE_PATH

    echo
    echo "Creating and sending a test backup..."
    if ! "$PYTHON" "$BACKUP" --scheduled --force; then
        error "Test backup delivery failed. Scheduling was not enabled."
        if [[ -n "$OLD_CONFIG" ]]; then
            cp -p "$OLD_CONFIG" "$CONFIG"
            rm -f "$OLD_CONFIG"
        else
            rm -f "$CONFIG"
        fi
        echo "Error log: $LOG"
        return 1
    fi
    [[ -n "$OLD_CONFIG" ]] && rm -f "$OLD_CONFIG"

    local CRON_TMP
    CRON_TMP=$(mktemp)
    cat > "$CRON_TMP" <<CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

* * * * * root $PYTHON $BACKUP --scheduled >> $LOG 2>&1
CRON
    chown root:root "$CRON_TMP"
    chmod 644 "$CRON_TMP"
    mv "$CRON_TMP" "$CRON_FILE"

    success "Backuper enabled successfully. Test backup delivered."
    echo "Configured interval: every $CONFIGURED_INTERVAL minutes"
    echo "Cron checks every minute; a backup runs only after the configured interval has elapsed."
    echo "Log file: $LOG"
    echo "Private configuration: $CONFIG (root access only)"
}

require_root
case "${1:-install}" in
    install|configure) install_backup ;;
    --status|status) status_backup ;;
    --disable|disable) disable_backup ;;
    --uninstall|uninstall) uninstall_backup ;;
    *) echo "Usage: $0 [install|status|disable|uninstall]"; exit 2 ;;
esac
