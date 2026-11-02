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
NC='\033[0m'

error() { echo -e "${RED}$1${NC}" >&2; }
success() { echo -e "${GREEN}$1${NC}"; }

require_root() {
    if [[ "$EUID" -ne 0 ]]; then
        error "این عملیات باید با دسترسی root اجرا شود."
        exit 1
    fi
}

status_backup() {
    echo "========== وضعیت بکاپر =========="
    if [[ -f "$CRON_FILE" ]]; then
        success "زمان‌بندی بکاپ فعال است."
        echo "تنظیم Cron:"
        grep -vE '^(SHELL|PATH)=' "$CRON_FILE" || true
    else
        echo "زمان‌بندی بکاپ غیرفعال است."
    fi
    if [[ -f "$CONFIG" ]]; then
        echo "فایل تنظیمات: موجود (محرمانه)"
        if [[ -x "$PYTHON" ]]; then
            "$PYTHON" - "$CONFIG" <<'PY'
import sys
values = {}
for line in open(sys.argv[1], encoding="utf-8"):
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.rstrip().split("=", 1)
        values[k] = v
print("فاصله بکاپ:", values.get("BACKUP_INTERVAL_MINUTES", "نامشخص"), "دقیقه")
print("مسیر دیتابیس:", values.get("DATABASE_PATH", "پیش‌فرض"))
PY
        fi
    else
        echo "فایل تنظیمات وجود ندارد."
    fi
    echo "گزارش: $LOG"
}

disable_backup() {
    rm -f "$CRON_FILE"
    success "زمان‌بندی بکاپ غیرفعال شد. تنظیمات و فایل‌های بکاپ حفظ شدند."
}

uninstall_backup() {
    rm -f "$CRON_FILE"
    rm -f "$CONFIG"
    rm -rf "$STATE_DIR"
    success "بکاپر حذف شد. فایل‌های بکاپ قبلی در $BACKUP_DIR حفظ شدند."
}

install_backup() {
    echo "=========================================="
    echo "       PORT TUNNEL - TELEGRAM BACKUP      "
    echo "=========================================="

    [[ -f "$BACKUP" ]] || { error "backup.py پیدا نشد: $BACKUP"; return 1; }
    [[ -x "$PYTHON" ]] || { error "محیط مجازی پایتون پیدا نشد. ابتدا ربات را نصب کنید."; return 1; }
    if ! command -v cron >/dev/null 2>&1; then
        echo "در حال نصب سرویس Cron..."
        apt-get update
        apt-get install -y cron
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now cron >/dev/null 2>&1 || systemctl start cron >/dev/null 2>&1 || true
    elif command -v service >/dev/null 2>&1; then
        service cron start >/dev/null 2>&1 || true
    fi
    [[ -f "$DATABASE" ]] || { error "دیتابیس پیدا نشد. ابتدا ربات را نصب و اجرا کنید."; return 1; }
    "$PYTHON" -c 'import aiohttp' >/dev/null 2>&1 || {
        error "کتابخانه aiohttp در محیط پروژه نصب نیست."; return 1;
    }

    echo
    read -rsp "توکن ربات اختصاصی بکاپر: " BOT_TOKEN
    echo
    if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
        error "فرمت توکن معتبر به نظر نمی‌رسد."
        unset BOT_TOKEN
        return 1
    fi

    read -rp "Chat ID مقصد (عدد یا @username کانال): " CHAT_ID
    if [[ ! "$CHAT_ID" =~ ^-?[0-9]+$ && ! "$CHAT_ID" =~ ^@[A-Za-z0-9_]{5,}$ ]]; then
        error "Chat ID باید عددی یا به شکل @username باشد."
        unset BOT_TOKEN CHAT_ID
        return 1
    fi

    echo
    read -rp "هر چند دقیقه یک بار بکاپ ارسال شود؟ [30]: " INTERVAL
    INTERVAL="${INTERVAL:-30}"
    if [[ ! "$INTERVAL" =~ ^[0-9]+$ ]] || (( INTERVAL < 1 || INTERVAL > 525600 )); then
        error "فاصله زمانی باید عددی بین 1 تا 525600 دقیقه باشد."
        unset BOT_TOKEN CHAT_ID INTERVAL
        return 1
    fi

    echo
    echo "در حال اعتبارسنجی توکن ربات در تلگرام..."
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
                    print("توکن توسط تلگرام تأیید نشد:", data.get("description", response.status))
                    return 1
                bot = data["result"]
                print(f"توکن معتبر است: @{bot.get('username', 'unknown')}")
                return 0
    except Exception as exc:
        print("ارتباط با تلگرام ناموفق بود:", exc)
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
    echo "در حال گرفتن و ارسال بکاپ آزمایشی..."
    if ! "$PYTHON" "$BACKUP" --scheduled --force; then
        error "ارسال بکاپ آزمایشی ناموفق بود؛ زمان‌بندی فعال نشد."
        if [[ -n "$OLD_CONFIG" ]]; then
            cp -p "$OLD_CONFIG" "$CONFIG"
            rm -f "$OLD_CONFIG"
        else
            rm -f "$CONFIG"
        fi
        echo "گزارش خطا: $LOG"
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

    success "بکاپر با موفقیت فعال شد و بکاپ آزمایشی ارسال شد."
    echo "فاصله تنظیم‌شده: هر $CONFIGURED_INTERVAL دقیقه"
    echo "Cron هر دقیقه بررسی می‌کند و بکاپ فقط پس از گذشت فاصله تعیین‌شده اجرا می‌شود."
    echo "گزارش: $LOG"
    echo "تنظیمات محرمانه: $CONFIG (دسترسی فقط root)"
}

require_root
case "${1:-install}" in
    install|configure) install_backup ;;
    --status|status) status_backup ;;
    --disable|disable) disable_backup ;;
    --uninstall|uninstall) uninstall_backup ;;
    *) echo "Usage: $0 [install|status|disable|uninstall]"; exit 2 ;;
esac
