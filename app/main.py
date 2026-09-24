"""Точка входа: загрузка конфигурации, запуск монитора и бота."""
from __future__ import annotations

import json
import logging
import os
import sys

from bot import Bot
from monitor import Monitor
from storage import Storage
from tg import Telegram

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
    datefmt="%d.%m %H:%M:%S",
)
log = logging.getLogger("main")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")
DB_PATH = os.environ.get("DB_PATH", "/data/svetobot.db")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        log.error("не найден файл конфигурации %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        config = json.load(handle)

    targets = config.get("targets") or []
    if not targets:
        log.error("в конфигурации не задан ни один объект (targets)")
        sys.exit(1)
    for target in targets:
        if not target.get("id") or not target.get("hosts"):
            log.error("у объекта должны быть заданы id и hosts: %s", target)
            sys.exit(1)
        target.setdefault("name", target["id"])
    return config


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    raw_chats = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
    if not token or not raw_chats:
        log.error("задайте TELEGRAM_TOKEN и TELEGRAM_CHAT_IDS в .env")
        sys.exit(1)

    try:
        chat_ids = [int(part) for part in raw_chats.replace(" ", "").split(",") if part]
    except ValueError:
        log.error("TELEGRAM_CHAT_IDS: ожидается список чисел через запятую")
        sys.exit(1)

    config = load_config()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    storage = Storage(DB_PATH)
    telegram = Telegram(token, chat_ids)
    monitor = Monitor(config, storage, telegram)

    log.info(
        "объектов: %d, опрос каждые %s с, «нет связи» через ~%s с, "
        "«света нет» через %s с",
        len(config["targets"]),
        monitor.poll,
        monitor.poll * monitor.fail_threshold,
        monitor.confirm_after,
    )
    monitor.start()
    Bot(telegram, storage, monitor).run()


if __name__ == "__main__":
    main()
