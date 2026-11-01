#!/bin/bash

set -e

PROJECT="/opt/Port-Tunnel-Bot"
PYTHON="$PROJECT/venv/bin/python"
BACKUP="$PROJECT/backup.py"
DATABASE="$PROJECT/bot.sqlite3"
CONFIG="/etc/port-tunnel-bot/backup.env"
CRON_FILE="/etc/cron.d/port-tunnel-backup"
LOG="/var/log/port-tunnel-backup.log"

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root."
    exit 1
fi

echo "=================================="
echo "     Telegram Database Backup"
echo "=================================="

if [ ! -f "$CONFIG" ]; then
    echo "Backup configuration not found."
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    echo "Python virtual environment not found."
    exit 1
fi

if [ ! -f "$DATABASE" ]; then
    echo "Database does not exist yet."
    echo "Install the sales bot first, then configure backup."
    exit 1
fi

if ! "$PYTHON" -c "import aiohttp" >/dev/null 2>&1; then
    echo "aiohttp is not installed in project environment."
    exit 1
fi

echo
echo "Select backup interval:"
echo "1) Every 10 minutes"
echo "2) Every 30 minutes"
echo "3) Every 60 minutes"
echo "4) Every 360 minutes"
echo "5) Every 720 minutes"
echo "6) Every 1440 minutes"
echo

read -rp "Your choice [1-6]: " choice

case "$choice" in
    1) SCHEDULE="*/10 * * * *" ;;
    2) SCHEDULE="*/30 * * * *" ;;
    3) SCHEDULE="0 * * * *" ;;
    4) SCHEDULE="0 */6 * * *" ;;
    5) SCHEDULE="0 */12 * * *" ;;
    6) SCHEDULE="0 0 * * *" ;;
    *)
        echo "Invalid choice."
        exit 1
        ;;
esac

echo
echo "Running immediate backup test..."
echo "The schedule will be installed only if this test succeeds."
echo

if ! "$PYTHON" "$BACKUP"; then
    echo
    echo "Backup test failed."
    echo "No cron schedule was installed."
    echo "Check: $LOG"
    exit 1
fi

CRON_TMP=$(mktemp)

cat > "$CRON_TMP" <<CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

$SCHEDULE root /usr/bin/flock -n /run/lock/port-tunnel-backup.lock $PYTHON $BACKUP >> $LOG 2>&1
CRON

chmod 600 "$CRON_TMP"
chown root:root "$CRON_TMP"
mv "$CRON_TMP" "$CRON_FILE"
chmod 600 "$CRON_FILE"

echo
echo "Backup schedule installed successfully."
echo "Schedule: $SCHEDULE"
echo "Log: $LOG"
