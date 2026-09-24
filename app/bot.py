"""Обработка команд Telegram-бота (long polling)."""
from __future__ import annotations

import logging
import time

from checker import DOWN, NONET, UP
from monitor import ICON, LABEL
from util import escape_html, fmt_duration, fmt_time, now_ts

log = logging.getLogger("bot")

COMMANDS = [
    ("status", "Текущее состояние всех объектов"),
    ("check", "Проверить прямо сейчас"),
    ("log", "Последние события (/log 20)"),
    ("flaps", "Кратковременные сбои связи"),
    ("stats", "Статистика за период (/stats 7)"),
    ("mute", "Тихий режим на N минут (/mute 60)"),
    ("unmute", "Выключить тихий режим"),
    ("help", "Справка"),
]

HELP = (
    "<b>Мониторинг электроснабжения</b>\n\n"
    "/status — текущее состояние\n"
    "/check — проверка прямо сейчас\n"
    "/log [N] — последние события, по умолчанию 15\n"
    "/flaps [N] — кратковременные пропадания, не ставшие отключением\n"
    "/stats [дней] — отключения за период, по умолчанию 7\n"
    "/mute [минут] — уведомления без звука, по умолчанию 60\n"
    "/unmute — вернуть звук\n"
)


class Bot:
    def __init__(self, telegram, storage, monitor):
        self.telegram = telegram
        self.storage = storage
        self.monitor = monitor
        self.offset: int | None = None

    def run(self) -> None:
        self.telegram.set_commands(COMMANDS)
        log.info("бот запущен, разрешённые чаты: %s", self.telegram.chat_ids)
        while True:
            try:
                for update in self.telegram.get_updates(self.offset):
                    self.offset = update["update_id"] + 1
                    self._handle(update)
            except Exception:  # noqa: BLE001
                log.exception("ошибка обработки обновлений")
                time.sleep(5)

    # ---------- маршрутизация ----------

    def _handle(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        if chat_id not in self.telegram.chat_ids:
            log.warning("сообщение из постороннего чата %s", chat_id)
            self.telegram.send(chat_id, "Доступ запрещён.")
            return

        parts = text.split()
        command = parts[0].split("@")[0].lstrip("/").lower()
        args = parts[1:]

        handler = {
            "start": self.cmd_status,
            "status": self.cmd_status,
            "check": self.cmd_check,
            "log": self.cmd_log,
            "history": self.cmd_log,
            "flaps": self.cmd_flaps,
            "stats": self.cmd_stats,
            "mute": self.cmd_mute,
            "unmute": self.cmd_unmute,
            "help": self.cmd_help,
        }.get(command)

        if handler:
            handler(chat_id, args)
        else:
            self.telegram.send(chat_id, "Неизвестная команда. /help — список.")

    # ---------- команды ----------

    def cmd_help(self, chat_id: int, args: list[str]) -> None:
        self.telegram.send(chat_id, HELP)

    def cmd_status(self, chat_id: int, args: list[str]) -> None:
        blocks = []
        for target in self.monitor.targets:
            state = self.storage.get_state(target["id"])
            if not state:
                blocks.append(
                    f"{target.get('emoji', '')} <b>{escape_html(target['name'])}</b>\n"
                    "Данных пока нет"
                )
                continue

            status = state["status"]
            elapsed = now_ts() - state["since_ts"]
            last = self.monitor.last_check.get(target["id"]) or state["last_check_ts"]
            lines = [
                f"{ICON[status]} <b>{escape_html(target['name'])}: {LABEL[status]}</b>",
                f"Уже {fmt_duration(elapsed)}, с {fmt_time(state['since_ts'])}",
                f"Опрошен {fmt_duration(now_ts() - last)} назад" if last else "",
            ]
            pending = self.monitor.pending_info(target["id"])
            if pending:
                needed = (
                    self.monitor.fail_threshold
                    if pending[0] == DOWN
                    else self.monitor.ok_threshold
                )
                lines.append(
                    f"⏳ Проверяется смена на «{LABEL[pending[0]]}» "
                    f"({pending[1]}/{needed})"
                )
            blocks.append("\n".join(line for line in lines if line))

        if self.monitor.server_offline():
            blocks.append("⚠️ У сервера нет связи с интернетом, опрос приостановлен")

        if self._mute_left():
            blocks.append(f"🔕 Тихий режим ещё {fmt_duration(self._mute_left())}")

        self.telegram.send(chat_id, "\n\n".join(blocks))

    def cmd_check(self, chat_id: int, args: list[str]) -> None:
        self.telegram.send(chat_id, "Проверяю…")
        results = []
        for target in self.monitor.targets:
            status = self.monitor.checker.check(target["hosts"])
            results.append(
                f"{ICON[status]} {escape_html(target['name'])}: {LABEL[status]}"
            )
        self.telegram.send(chat_id, "\n".join(results))
        # Полноценная проверка со сменой статуса и уведомлениями.
        self.monitor.check_now()

    def cmd_log(self, chat_id: int, args: list[str]) -> None:
        limit = self._int_arg(args, default=15, low=1, high=100)
        events = self.storage.last_events(limit)
        if not events:
            self.telegram.send(chat_id, "Журнал пуст.")
            return

        names = {t["id"]: t["name"] for t in self.monitor.targets}
        lines = [f"<b>Последние события ({len(events)})</b>"]
        for event in events:
            name = names.get(event["target_id"], event["target_id"])
            line = (
                f"{ICON[event['status']]} {fmt_time(event['ts'])} — "
                f"{escape_html(name)}: {LABEL[event['status']]}"
            )
            if event["duration"]:
                line += f" (пред. {fmt_duration(event['duration'])})"
            lines.append(line)
        self.telegram.send(chat_id, "\n".join(lines))

    def cmd_flaps(self, chat_id: int, args: list[str]) -> None:
        limit = self._int_arg(args, default=15, low=1, high=100)
        flaps = self.storage.last_flaps(limit)
        if not flaps:
            self.telegram.send(
                chat_id, "Кратковременных сбоев не зафиксировано."
            )
            return
        names = {t["id"]: t["name"] for t in self.monitor.targets}
        lines = ["<b>Кратковременные пропадания</b>"]
        for flap in flaps:
            name = names.get(flap["target_id"], flap["target_id"])
            lines.append(
                f"⚡ {fmt_time(flap['ts'])} — {escape_html(name)}, "
                f"около {fmt_duration(flap['seconds'])}"
            )
        lines.append("\nСтатус при таких сбоях не менялся.")
        self.telegram.send(chat_id, "\n".join(lines))

    def cmd_stats(self, chat_id: int, args: list[str]) -> None:
        days = self._int_arg(args, default=7, low=1, high=365)
        since = now_ts() - days * 86400
        lines = [f"<b>Статистика за {days} дн.</b>"]
        for target in self.monitor.targets:
            data = self.storage.stats(target["id"], since)
            total = days * 86400
            uptime = 100.0 * (total - data["downtime"]) / total
            lines.append(
                f"\n{target.get('emoji', '')} <b>{escape_html(target['name'])}</b>\n"
                f"Отключений: {data['outages']}\n"
                f"Без света: {fmt_duration(data['downtime'])}\n"
                f"Со светом: {uptime:.1f}% времени\n"
                f"Кратковременных сбоев: {data['flaps']}"
            )
        self.telegram.send(chat_id, "\n".join(lines))

    def cmd_mute(self, chat_id: int, args: list[str]) -> None:
        minutes = self._int_arg(args, default=60, low=1, high=10080)
        self.storage.set_setting("mute_until", str(now_ts() + minutes * 60))
        self.telegram.send(
            chat_id, f"🔕 Уведомления без звука на {fmt_duration(minutes * 60)}."
        )

    def cmd_unmute(self, chat_id: int, args: list[str]) -> None:
        self.storage.set_setting("mute_until", "0")
        self.telegram.send(chat_id, "🔔 Звук уведомлений включён.")

    # ---------- утилиты ----------

    def _mute_left(self) -> int:
        until = self.storage.get_setting("mute_until")
        return max(0, int(until) - now_ts()) if until else 0

    @staticmethod
    def _int_arg(args: list[str], default: int, low: int, high: int) -> int:
        if not args:
            return default
        try:
            return max(low, min(high, int(args[0])))
        except ValueError:
            return default
