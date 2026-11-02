#!/usr/bin/env bash
# Download the latest project release from GitHub
set -Eeuo pipefail

INSTALL_DIR="/opt/Port-Tunnel-Bot"
REPO_URL="https://github.com/Mojtabaap/Port-Tunnel-Bot/archive/refs/heads/main.tar.gz"

download_project() {
    echo "[INFO] Downloading the latest project from GitHub..."

    mkdir -p "$INSTALL_DIR"

    local TMP_DIR
    TMP_DIR=$(mktemp -d)

    trap 'rm -rf "$TMP_DIR"' RETURN

    if ! curl -fLsS "$REPO_URL" -o "$TMP_DIR/project.tar.gz"; then
        echo "[ERROR] Failed to download the project from GitHub." >&2
        return 1
    fi

    mkdir -p "$TMP_DIR/source"

    if ! tar -xzf "$TMP_DIR/project.tar.gz" \
        --strip-components=1 \
        -C "$TMP_DIR/source"; then
        echo "[ERROR] The downloaded archive is invalid." >&2
        return 1
    fi

    # Preserve existing credentials and user data
    tar -czf "$TMP_DIR/project-files.tar.gz" \
        --exclude='./config.py' \
        --exclude='./bot.sqlite3' \
        --exclude='./bot.sqlite3-*' \
        --exclude='./*.sqlite3' \
        --exclude='./*.db' \
        --exclude='./venv' \
        --exclude='./backups' \
        -C "$TMP_DIR/source" .

    tar -xzf "$TMP_DIR/project-files.tar.gz" -C "$INSTALL_DIR"

    echo "[OK] Project files downloaded successfully."
}

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Please run the installer as root." >&2
    exit 1
fi

apt-get update
apt-get install -y curl tar

download_project

set -Eeuo pipefail

INSTALL_DIR="/opt/Port-Tunnel-Bot"
SERVICE_NAME="port-tunnel-bot"
PYTHON="/usr/bin/python3"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[*]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warning() { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }

line() { printf '%b\n' "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

banner() {
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}"
    echo '  +------------------------------------------------------+ '
    echo '  |              PORT TUNNEL BOT INSTALLER               |'
    echo '  +------------------------------------------------------+ '
    echo -e "${NC}${WHITE}${BOLD}                 SERVER MANAGEMENT MENU${NC}"
    echo -e "${DIM}                 Linux Deployment Utility${NC}"
    line
}

pause() {
    echo
    read -rp "$(echo -e "${DIM}Press ENTER to return to the menu...${NC}")"
}

install_bot() {
    echo
    line
    echo -e "${WHITE}${BOLD}  BOT INSTALLATION${NC}"
    line

    if [[ $EUID -ne 0 ]]; then
        error "Please run the installer as root."
        return 1
    fi

    if [[ ! -f "$INSTALL_DIR/bot.py" ]]; then
        error "bot.py was not found."
        return 1
    fi

    if [[ ! -f "$INSTALL_DIR/requirements.txt" ]]; then
        error "requirements.txt was not found."
        return 1
    fi

    if systemctl is-active --quiet sanaei-tunnel-bot; then
        error "Legacy service sanaei-tunnel-bot is active."
        warning "Installation stopped to prevent duplicate bot processes."
        info "The existing service has not been changed."
        return 1
    fi

    echo
    info "Checking system requirements..."

    apt-get update
    apt-get install -y python3 python3-venv python3-pip

    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        "$PYTHON" -m venv "$INSTALL_DIR/venv"
    fi

    echo
    info "Installing Python dependencies..."

    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install \
        -r "$INSTALL_DIR/requirements.txt"

    if [[ ! -f "$INSTALL_DIR/config.py" ]]; then

        echo
        info "Enter your Telegram bot configuration."

        read -rsp "Telegram Bot Token: " BOT_TOKEN
        echo

        if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            error "Invalid Telegram bot token format."
            return 1
        fi

        read -rp "Admin User ID: " ADMIN_ID

        if [[ ! "$ADMIN_ID" =~ ^[0-9]+$ ]] || [[ "$ADMIN_ID" == "0" ]]; then
            error "Admin ID must be a valid positive number."
            return 1
        fi

        export BOT_TOKEN ADMIN_ID

        "$PYTHON" - <<'PY'
import os
from pathlib import Path

token = os.environ["BOT_TOKEN"]
admin = int(os.environ["ADMIN_ID"])

config = Path("/opt/Port-Tunnel-Bot/config.py")

content = f'''BOT_TOKEN = {token!r}
ADMIN_ID = {admin}

DATABASE_PATH = "/opt/Port-Tunnel-Bot/bot.sqlite3"

VERIFY_PANEL_SSL = True
LOW_BALANCE_WARNING = 10000
USAGE_CHECK_MINUTES = 10
'''

fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(content)
PY

        unset BOT_TOKEN ADMIN_ID

        chmod 600 "$INSTALL_DIR/config.py"

    else
        echo
        success "Existing config.py found. Keeping current settings."
    fi

    echo
    info "Initializing the database..."

    cd "$INSTALL_DIR"

    "$INSTALL_DIR/venv/bin/python" -c \
        'import asyncio; from bot import init_db; asyncio.run(init_db())'

    echo
    info "Creating the systemd service..."

    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=Port Tunnel Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

    chmod 600 "$INSTALL_DIR/config.py"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"

    echo
    success "Bot installation completed successfully."

    systemctl --no-pager --full status "$SERVICE_NAME" || true
}

show_menu() {
    banner
    echo -e "  ${GREEN}1${NC}  Install / Repair Bot"
    echo -e "  ${GREEN}2${NC}  Install / Configure Telegram Backups"
    echo -e "  ${GREEN}3${NC}  View Backup Status"
    echo -e "  ${GREEN}4${NC}  Disable Automatic Backups"
    echo -e "  ${GREEN}5${NC}  Uninstall Backuper ${DIM}(keep backup archives)${NC}"
    echo -e "  ${RED}0${NC}  Exit"
    echo
    line
}

while true; do
    show_menu
    read -rp "Select an option [0-5]: " choice

    case "$choice" in
        1)
            install_bot
            pause
            ;;
        2)
            bash /opt/Port-Tunnel-Bot/setup_backup.sh install
            pause
            ;;
        3)
            bash /opt/Port-Tunnel-Bot/setup_backup.sh status
            pause
            ;;
        4)
            bash /opt/Port-Tunnel-Bot/setup_backup.sh disable
            pause
            ;;
        5)
            bash /opt/Port-Tunnel-Bot/setup_backup.sh uninstall
            pause
            ;;
        0)
            success "Exiting installer. Goodbye!"
            exit 0
            ;;
        *)
            error "Invalid selection. Please choose 0-5."
            sleep 2
            ;;
    esac
done
