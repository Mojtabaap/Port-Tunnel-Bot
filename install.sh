#!/usr/bin/env bash
# دریافت خودکار آخرین نسخه پروژه از GitHub
set -Eeuo pipefail

INSTALL_DIR="/opt/Port-Tunnel-Bot"
REPO_URL="https://github.com/Mojtabaap/Port-Tunnel-Bot/archive/refs/heads/main.tar.gz"

download_project() {
    echo "در حال دریافت آخرین نسخه پروژه از GitHub..."

    mkdir -p "$INSTALL_DIR"

    local TMP_DIR
    TMP_DIR=$(mktemp -d)

    trap 'rm -rf "$TMP_DIR"' RETURN

    if ! curl -fLsS "$REPO_URL" -o "$TMP_DIR/project.tar.gz"; then
        echo "خطا در دریافت پروژه از GitHub."
        return 1
    fi

    mkdir -p "$TMP_DIR/source"

    if ! tar -xzf "$TMP_DIR/project.tar.gz" \
        --strip-components=1 \
        -C "$TMP_DIR/source"; then
        echo "فایل دریافتی معتبر نیست."
        return 1
    fi

    # حفظ اطلاعات محرمانه و داده‌های قبلی
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

    echo "فایل‌های پروژه دریافت شدند."
}

if [[ $EUID -ne 0 ]]; then
    echo "نصب‌کننده باید با دسترسی root اجرا شود."
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
NC='\033[0m'

error() {
    echo -e "${RED}خطا: $1${NC}"
}

success() {
    echo -e "${GREEN}$1${NC}"
}

pause() {
    echo
    read -rp "برای ادامه Enter بزنید..."
}

install_bot() {
    echo
    echo "======================================"
    echo "          نصب ربات فروش"
    echo "======================================"

    if [[ $EUID -ne 0 ]]; then
        error "نصب‌کننده باید با دسترسی root اجرا شود."
        return 1
    fi

    if [[ ! -f "$INSTALL_DIR/bot.py" ]]; then
        error "فایل bot.py پیدا نشد."
        return 1
    fi

    if [[ ! -f "$INSTALL_DIR/requirements.txt" ]]; then
        error "فایل requirements.txt پیدا نشد."
        return 1
    fi

    if systemctl is-active --quiet sanaei-tunnel-bot; then
        error "سرویس قدیمی sanaei-tunnel-bot فعال است."
        echo "برای جلوگیری از اجرای هم‌زمان دو ربات، نصب متوقف شد."
        echo "سرویس فعلی بدون تغییر باقی ماند."
        return 1
    fi

    echo
    echo "در حال بررسی پیش‌نیازها..."

    apt-get update
    apt-get install -y python3 python3-venv python3-pip

    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        "$PYTHON" -m venv "$INSTALL_DIR/venv"
    fi

    echo
    echo "در حال نصب وابستگی‌ها..."

    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install \
        -r "$INSTALL_DIR/requirements.txt"

    if [[ ! -f "$INSTALL_DIR/config.py" ]]; then

        echo
        echo "اطلاعات ربات را وارد کنید."

        read -rsp "توکن ربات تلگرام: " BOT_TOKEN
        echo

        if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            error "فرمت توکن معتبر نیست."
            return 1
        fi

        read -rp "آیدی عددی مدیر: " ADMIN_ID

        if [[ ! "$ADMIN_ID" =~ ^[0-9]+$ ]] || [[ "$ADMIN_ID" == "0" ]]; then
            error "آیدی مدیر باید یک عدد معتبر باشد."
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
        success "فایل config.py از قبل وجود دارد."
        echo "تنظیمات قبلی حفظ می‌شوند."
    fi

    echo
    echo "در حال بررسی دیتابیس..."

    cd "$INSTALL_DIR"

    "$INSTALL_DIR/venv/bin/python" -c \
        'import asyncio; from bot import init_db; asyncio.run(init_db())'

    echo
    echo "در حال ساخت سرویس systemd..."

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
    success "مراحل نصب به پایان رسید."

    systemctl --no-pager --full status "$SERVICE_NAME" || true
}

show_menu() {
    clear
    echo "======================================"
    echo "       PORT TUNNEL BOT INSTALLER"
    echo "======================================"
    echo
    echo "  1) نصب ربات فروش"
    echo "  2) فعال‌سازی بکاپ دیتابیس تلگرام"
    echo "  0) خروج"
    echo
    echo "======================================"
}

while true; do
    show_menu
    read -rp "انتخاب شما: " choice

    case "$choice" in
        1)
            install_bot
            pause
            ;;
        2)
    echo
    echo "Starting Telegram backup setup..."
    bash /opt/Port-Tunnel-Bot/setup_backup.sh
    pause
    ;;
        0)
            echo "خروج از نصب‌کننده."
            exit 0
            ;;
        *)
            error "انتخاب نامعتبر است."
            sleep 2
            ;;
    esac
done
