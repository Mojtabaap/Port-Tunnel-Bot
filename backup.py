#!/usr/bin/env python3
"""Create and deliver a consistent SQLite backup of Port-Tunnel-Bot."""

import argparse
import asyncio
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import aiohttp

CONFIG_FILE = Path("/etc/port-tunnel-bot/backup.env")
BACKUP_DIR = Path("/var/backups/port-tunnel-bot")
STATE_DIR = Path("/var/lib/port-tunnel-bot-backup")
STATE_FILE = STATE_DIR / "last_attempt"
LOCK_FILE = Path("/run/lock/port-tunnel-backup.lock")
LOG_FILE = Path("/var/log/port-tunnel-backup.log")
DEFAULT_DATABASE = "/opt/Port-Tunnel-Bot/bot.sqlite3"
TELEGRAM_MAX_DOCUMENT_BYTES = 49 * 1024 * 1024


def load_config():
    if not CONFIG_FILE.is_file():
        raise RuntimeError(f"Backup configuration not found: {CONFIG_FILE}")

    config = {}
    for raw_line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip('"').strip("'")

    for key in ("BACKUP_BOT_TOKEN", "BACKUP_CHAT_ID"):
        if not config.get(key):
            raise RuntimeError(f"Missing configuration: {key}")

    try:
        interval = int(config.get("BACKUP_INTERVAL_MINUTES", "30"))
    except ValueError as exc:
        raise RuntimeError("BACKUP_INTERVAL_MINUTES must be an integer") from exc
    if interval < 1:
        raise RuntimeError("Backup interval must be at least 1 minute")

    config["BACKUP_INTERVAL_MINUTES"] = interval
    config["DATABASE_PATH"] = config.get("DATABASE_PATH") or DEFAULT_DATABASE
    return config


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[handler],
        force=True,
    )


def create_backup(database_path: Path) -> Path:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = BACKUP_DIR / f"port_tunnel_backup_{timestamp}.sqlite3"
    source = destination = None

    try:
        # SQLite's online backup API safely includes committed data from a live DB.
        source_uri = f"file:{quote(str(database_path.resolve()))}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        destination = sqlite3.connect(str(backup_file), timeout=30)
        source.backup(destination, pages=256, sleep=0.1)
        destination.commit()

        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")

        if backup_file.stat().st_size == 0:
            raise RuntimeError("Backup file is empty")
        return backup_file
    except Exception:
        if backup_file.exists():
            backup_file.unlink()
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


async def send_backup(token: str, chat_id: str, backup_file: Path):
    size = backup_file.stat().st_size
    if size > TELEGRAM_MAX_DOCUMENT_BYTES:
        raise RuntimeError(
            f"Backup is {size} bytes; it exceeds the configured Telegram upload safety limit "
            f"({TELEGRAM_MAX_DOCUMENT_BYTES} bytes). The local backup was retained."
        )

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=300)
    caption = (
        "🗄 <b>Port Tunnel Bot Backup</b>\n"
        f"🕒 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"📦 {size:,} bytes"
    )

    with backup_file.open("rb") as document:
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        form.add_field(
            "document", document, filename=backup_file.name,
            content_type="application/octet-stream"
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form) as response:
                try:
                    result = await response.json(content_type=None)
                except Exception as exc:
                    raise RuntimeError(
                        f"Telegram returned an invalid response (HTTP {response.status})"
                    ) from exc
                if response.status != 200 or not result.get("ok"):
                    raise RuntimeError(
                        result.get("description", f"Telegram HTTP error {response.status}")
                    )


def scheduled_is_due(interval_minutes: int) -> bool:
    if not STATE_FILE.exists():
        return True
    try:
        last_attempt = float(STATE_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return True
    return (datetime.now().timestamp() - last_attempt) >= interval_minutes * 60


def mark_attempt():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(str(datetime.now().timestamp()), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


async def run_backup(config):
    database_path = Path(config["DATABASE_PATH"])
    logging.info("Starting database backup.")
    backup_file = create_backup(database_path)
    logging.info("Backup created: %s (%s bytes)", backup_file.name, backup_file.stat().st_size)
    await send_backup(config["BACKUP_BOT_TOKEN"], config["BACKUP_CHAT_ID"], backup_file)
    logging.info("Backup sent successfully.")
    backup_file.unlink(missing_ok=True)
    logging.info("Temporary backup file deleted.")


def main():
    parser = argparse.ArgumentParser(description="Port Tunnel Bot database backuper")
    parser.add_argument(
        "--scheduled", action="store_true",
        help="Run only when the configured interval has elapsed"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Run immediately, even when called with --scheduled"
    )
    args = parser.parse_args()

    setup_logging()
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("Another backup process is running; this run is skipped.")
            return 0

        try:
            config = load_config()
            if args.scheduled and not args.force and not scheduled_is_due(
                config["BACKUP_INTERVAL_MINUTES"]
            ):
                return 0

            # Record attempts (not just successes) to avoid retrying every minute
            # and flooding Telegram during a persistent failure.
            if args.scheduled:
                mark_attempt()
            asyncio.run(run_backup(config))
            if not args.scheduled:
                mark_attempt()
            return 0
        except Exception as error:
            logging.exception("Backup failed: %s", error)
            print(f"Backup failed: {error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
