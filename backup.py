#!/usr/bin/env python3

import os
import sys
import sqlite3
import asyncio
import logging
from pathlib import Path
from datetime import datetime

import aiohttp


CONFIG_FILE = Path("/etc/port-tunnel-bot/backup.env")
BACKUP_DIR = Path("/var/backups/port-tunnel-bot")
LOG_FILE = Path("/var/log/port-tunnel-backup.log")

DATABASE_PATH = "/opt/Port-Tunnel-Bot/bot.sqlite3"


def load_config():
    if not CONFIG_FILE.exists():
        raise RuntimeError("Backup configuration file not found.")

    config = {}

    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip('"').strip("'")

    required = ["BACKUP_BOT_TOKEN", "BACKUP_CHAT_ID"]

    for key in required:
        if not config.get(key):
            raise RuntimeError(f"Missing configuration: {key}")

    return config


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )


def create_backup(database_path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = BACKUP_DIR / f"backup_{timestamp}.sqlite3"

    source = None
    destination = None

    try:
        source = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=30
        )

        destination = sqlite3.connect(backup_file)
        source.backup(destination)
        destination.commit()

        return backup_file

    except Exception:
        if backup_file.exists():
            backup_file.unlink()
        raise

    finally:
        if destination:
            destination.close()

        if source:
            source.close()


async def send_backup(token, chat_id, backup_file):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    timeout = aiohttp.ClientTimeout(total=300)

    try:
        with backup_file.open("rb") as document:
            form = aiohttp.FormData()
            form.add_field("chat_id", chat_id)
            form.add_field(
                "document",
                document,
                filename=backup_file.name,
                content_type="application/octet-stream"
            )

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form) as response:
                    try:
                        result = await response.json()
                    except Exception:
                        raise RuntimeError(
                            f"Telegram returned invalid response: HTTP {response.status}"
                        )

                    if response.status != 200 or not result.get("ok"):
                        description = result.get(
                            "description",
                            "Unknown Telegram API error"
                        )
                        raise RuntimeError(
                            f"Telegram upload failed: {description}"
                        )

        return True

    except Exception:
        raise


async def main():
    setup_logging()
    backup_file = None

    try:
        config = load_config()

        database_path = Path(
            config.get("DATABASE_PATH", DATABASE_PATH)
        )

        if not database_path.is_file():
            raise RuntimeError("Database file does not exist.")

        logging.info("Starting database backup.")

        backup_file = create_backup(database_path)

        logging.info(
            "Backup created: %s (%s bytes)",
            backup_file.name,
            backup_file.stat().st_size
        )

        await send_backup(
            config["BACKUP_BOT_TOKEN"],
            config["BACKUP_CHAT_ID"],
            backup_file
        )

        logging.info("Backup sent successfully.")

        backup_file.unlink()
        logging.info("Temporary backup file deleted.")

    except Exception as error:
        logging.exception("Backup failed: %s", error)

        if backup_file and backup_file.exists():
            logging.error(
                "Backup retained on server: %s",
                backup_file
            )

        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
