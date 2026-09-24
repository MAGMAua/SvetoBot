"""Обработка команд и кнопок Telegram-бота (long polling)."""
from __future__ import annotations

import logging
import time

from checker import DOWN, NONET, UP
from monitor import ICON, LABEL, target_title
from util import escape_html, fmt_day_time, fmt_duration, fmt_short_time, fmt_when, now_ts

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

# Постоянная клавиатура внизу чата.
BTN_STATUS = "💡 Статус"
BTN_CHECK = "🔄 Проверить"
BTN_LOG = "📜 Журнал"
BTN_FLAPS = "⚡ Сбои"
BTN_STATS = "📊 Статистика"
BTN_MUTE = "🔕 Тихий режим"

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_STATUS}, {"text": BTN_CHECK}],
        [{"text": BTN_LOG}, {"text": BTN_FLAPS}],
        [{"text": BTN_STATS}, {"text": BTN_MUTE}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Кнопки под сообщениями: (подпись, callback_data).
STATS_PERIODS = [("Сутки", 1), ("Неделя", 7), ("Месяц", 30)]
MUTE_OPTIONS = [("30 мин", 30), ("1 ч", 60), ("3 ч", 180), ("8 ч", 480), ("24 ч", 1440)]

HELP = (
    "<b>Мониторинг электроснабжения</b>\n\n"
    "Пользуйтесь кнопками внизу чата:\n"
    f"{BTN_STATUS} — текущее состояние\n"
    f"{BTN_CHECK} — проверка прямо сейчас\n"
    f"{BTN_LOG} — последние события\n"
    f"{BTN_FLAPS} — кратковременные пропадания, не ставшие отключением\n"
    f"{BTN_STATS} — отключения за сутки, неделю или месяц\n"
    f"{BTN_MUTE} — уведомления без звука на выбранное время\n\n"
    "Команды тоже работают: /status, /check, /log [N], /flaps [N], "
    "/stats [дней], /mute [минут], /unmute."
)


def _inline(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


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
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        buttons = {
            BTN_STATUS: self.cmd_status,
            BTN_CHECK: self.cmd_check,
            BTN_LOG: self.cmd_log,
            BTN_FLAPS: self.cmd_flaps,
            BTN_STATS: self.cmd_stats,
            BTN_MUTE: self.cmd_mute_menu,
        }
        if text in buttons:
            handler, args = buttons[text], []
        elif text.startswith("/"):
            parts = text.split()
            command = parts[0].split("@")[0].lstrip("/").lower()
            args = parts[1:]
            handler = {
                "start": self.cmd_start,
                "status": self.cmd_status,
                "check": self.cmd_check,
                "log": self.cmd_log,
                "history": self.cmd_log,
                "flaps": self.cmd_flaps,
                "stats": self.cmd_stats,
                "mute": self.cmd_mute,
                "unmute": self.cmd_unmute,
                "help": self.cmd_help,
            }.get(command, self.cmd_unknown)
        else:
            return

        if chat_id not in self.telegram.chat_ids:
            log.warning("сообщение из постороннего чата %s", chat_id)
            self.telegram.send(chat_id, "Доступ запрещён.")
            return

        handler(chat_id, args)

    def _handle_callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if chat_id not in self.telegram.chat_ids:
            log.warning("нажатие кнопки из постороннего чата %s", chat_id)
            self.telegram.answer_callback(query["id"], "Доступ запрещён.")
            return

        self.telegram.answer_callback(query["id"])
        message_id = message["message_id"]
        action, _, value = (query.get("data") or "").partition(":")

        if action == "stats" and value.isdigit():
            days = max(1, min(365, int(value)))
            self.telegram.edit(
                chat_id, message_id, self._stats_text(days), self._stats_markup()
            )
        elif action == "mute" and value.isdigit():
            minutes = max(1, min(10080, int(value)))
            self.storage.set_setting("mute_until", str(now_ts() + minutes * 60))
            self.telegram.edit(
                chat_id, message_id, self._mute_text(), self._mute_markup()
            )
        elif action == "unmute":
            self.storage.set_setting("mute_until", "0")
            self.telegram.edit(
                chat_id, message_id, self._mute_text(), self._mute_markup()
            )

    def _reply(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        """Ответ с постоянной клавиатурой, если не задана другая разметка."""
        self.telegram.send(chat_id, text, reply_markup=markup or MAIN_KEYBOARD)

    # ---------- команды ----------

    def cmd_start(self, chat_id: int, args: list[str]) -> None:
        self._reply(chat_id, HELP)
        self.cmd_status(chat_id, args)

    def cmd_help(self, chat_id: int, args: list[str]) -> None:
        self._reply(chat_id, HELP)

    def cmd_unknown(self, chat_id: int, args: list[str]) -> None:
        self._reply(chat_id, "Неизвестная команда. Пользуйтесь кнопками внизу чата.")

    def cmd_status(self, chat_id: int, args: list[str]) -> None:
        blocks = []
        offline_since = self.monitor.server_offline_since()
        if offline_since:
            blocks.append(
                f"⚠️ <b>Сервер без интернета</b> уже "
                f"{fmt_duration(now_ts() - offline_since)}\n"
                f"Связь пропала {fmt_when(offline_since)} — проверка приостановлена, "
                "ниже статусы на тот момент."
            )

        for target in self.monitor.targets:
            state = self.storage.get_state(target["id"])
            if not state:
                blocks.append(f"{ICON[NONET]} <b>Нет данных</b> · {target_title(target)}")
                continue

            status = state["status"]
            since = state["since_ts"]
            verb = "пропал" if status == DOWN else "появился"
            lines = [
                f"{ICON[status]} <b>{LABEL[status]}</b> · {target_title(target)}",
                f"Уже {fmt_duration(now_ts() - since)} — {verb} {fmt_when(since)}",
            ]
            pending = self.monitor.pending_info(target["id"])
            if pending:
                if pending[0] == DOWN:
                    hint = "нет ответа, проверяю, не пропал ли свет"
                    needed = self.monitor.fail_threshold
                else:
                    hint = "появился ответ, проверяю, вернулся ли свет"
                    needed = self.monitor.ok_threshold
                lines.append(f"⏳ {hint} ({pending[1]}/{needed})")
            last = self.monitor.last_check.get(target["id"]) or state["last_check_ts"]
            if last:
                lines.append(f"<i>проверено {fmt_duration(now_ts() - last)} назад</i>")
            blocks.append("\n".join(lines))

        if self._mute_left():
            blocks.append(f"🔕 Тихий режим ещё {fmt_duration(self._mute_left())}")

        self._reply(chat_id, "\n\n".join(blocks))

    def cmd_check(self, chat_id: int, args: list[str]) -> None:
        self._reply(chat_id, "🔍 Проверяю…")
        lines = ["<b>Разовая проверка</b>"]
        statuses = []
        for target in self.monitor.targets:
            status = self.monitor.checker.check(target["hosts"])
            statuses.append(status)
            line = f"{ICON[status]} {LABEL[status]} · {target_title(target)}"
            if status == NONET:
                line += " — у сервера нет интернета"
            lines.append(line)
        confirm = self.monitor.poll * self.monitor.fail_threshold
        if NONET not in statuses:
            lines.append(
                "\n<i>Это одна проверка без подтверждения. Если она расходится "
                f"со статусом, смена подтвердится в течение ~{confirm} сек.</i>"
            )
        self._reply(chat_id, "\n".join(lines))
        # Полноценная проверка со сменой статуса и уведомлениями.
        self.monitor.check_now()

    def cmd_log(self, chat_id: int, args: list[str]) -> None:
        limit = self._int_arg(args, default=15, low=1, high=100)
        events = self.storage.last_events(limit)
        if not events:
            self._reply(chat_id, "Журнал пуст.")
            return

        targets = {t["id"]: t for t in self.monitor.targets}
        lines = [f"<b>Последние события ({len(events)})</b>"]
        for event in events:
            target = targets.get(event["target_id"], {"name": event["target_id"]})
            status = event["status"]
            line = (
                f"{ICON[status]} {fmt_day_time(event['ts'])} · "
                f"{target_title(target)} — {LABEL[status].lower()}"
            )
            if event["duration"]:
                before = "свет был" if status == DOWN else "не было"
                line += f" ({before} {fmt_duration(event['duration'])})"
            lines.append(line)
        self._reply(chat_id, "\n".join(lines))

    def cmd_flaps(self, chat_id: int, args: list[str]) -> None:
        limit = self._int_arg(args, default=15, low=1, high=100)
        flaps = self.storage.last_flaps(limit)
        if not flaps:
            self._reply(chat_id, "Кратковременных сбоев не зафиксировано.")
            return
        targets = {t["id"]: t for t in self.monitor.targets}
        lines = ["<b>Кратковременные сбои связи</b>"]
        for flap in flaps:
            target = targets.get(flap["target_id"], {"name": flap["target_id"]})
            lines.append(
                f"⚡ {fmt_day_time(flap['ts'])} · {target_title(target)} — "
                f"не отвечал ~{fmt_duration(flap['seconds'])}"
            )
        lines.append(
            "\n<i>Объект ненадолго перестал отвечать и снова появился раньше, "
            "чем подтвердилось отключение. Статус не менялся, уведомлений не было.</i>"
        )
        self._reply(chat_id, "\n".join(lines))

    def cmd_stats(self, chat_id: int, args: list[str]) -> None:
        days = self._int_arg(args, default=7, low=1, high=365)
        self._reply(chat_id, self._stats_text(days), self._stats_markup())

    def cmd_mute_menu(self, chat_id: int, args: list[str]) -> None:
        self._reply(chat_id, self._mute_text(), self._mute_markup())

    def cmd_mute(self, chat_id: int, args: list[str]) -> None:
        if not args:
            self.cmd_mute_menu(chat_id, args)
            return
        minutes = self._int_arg(args, default=60, low=1, high=10080)
        self.storage.set_setting("mute_until", str(now_ts() + minutes * 60))
        self._reply(chat_id, self._mute_text(), self._mute_markup())

    def cmd_unmute(self, chat_id: int, args: list[str]) -> None:
        self.storage.set_setting("mute_until", "0")
        self._reply(chat_id, "🔔 Звук уведомлений включён.")

    # ---------- тексты и кнопки под сообщениями ----------

    def _stats_text(self, days: int) -> str:
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
        return "\n".join(lines)

    @staticmethod
    def _stats_markup() -> dict:
        return _inline([[(label, f"stats:{days}") for label, days in STATS_PERIODS]])

    def _mute_text(self) -> str:
        left = self._mute_left()
        if left:
            until = int(self.storage.get_setting("mute_until"))
            state = (
                f"🔕 Уведомления без звука до {fmt_short_time(until)} "
                f"(ещё {fmt_duration(left)})."
            )
        else:
            state = "🔔 Уведомления приходят со звуком."
        return f"{state}\n\nНа сколько отключить звук?"

    def _mute_markup(self) -> dict:
        options = [(label, f"mute:{minutes}") for label, minutes in MUTE_OPTIONS]
        rows = [options[:3], options[3:]]
        if self._mute_left():
            rows.append([("🔔 Включить звук", "unmute")])
        return _inline(rows)

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
