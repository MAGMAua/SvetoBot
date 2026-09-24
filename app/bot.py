"""Telegram-бот: меню с кнопками внутри сообщения (long polling).

Главный экран — одно сообщение со статусом объектов и кнопками. Нажатие
кнопки не присылает новое сообщение, а перерисовывает это же: журнал,
сбои, статистика и тихий режим открываются на его месте, «◀ Меню»
возвращает к статусу. Команды тоже работают и присылают нужный экран
новым сообщением.
"""
from __future__ import annotations

import logging
import time

from checker import DOWN, NONET
from monitor import ICON, LABEL, target_title
from util import escape_html, fmt_day_time, fmt_duration, fmt_short_time, fmt_when, now_ts

log = logging.getLogger("bot")

COMMANDS = [
    ("menu", "🏠 Меню со статусом и кнопками"),
    ("status", "💡 Текущее состояние объектов"),
    ("check", "🔍 Проверить прямо сейчас"),
    ("log", "📜 Последние события (/log 20)"),
    ("flaps", "⚡ Кратковременные сбои связи"),
    ("stats", "📊 Статистика за период (/stats 7)"),
    ("mute", "🔕 Тихий режим (/mute 60)"),
    ("unmute", "🔔 Выключить тихий режим"),
    ("help", "❓ Справка"),
]

# Подписи кнопок старой клавиатуры под полем ввода: у кого она ещё
# осталась, по нажатию получит новое меню, а клавиатура исчезнет.
LEGACY_BUTTONS = {
    "💡 Статус", "🔄 Проверить", "📜 Журнал", "⚡ Сбои", "📊 Статистика", "🔕 Тихий режим",
}

STATS_PERIODS = [("Сутки", 1), ("Неделя", 7), ("Месяц", 30)]
MUTE_OPTIONS = [("30 мин", 30), ("1 ч", 60), ("3 ч", 180), ("8 ч", 480), ("24 ч", 1440)]

BACK = ("◀ Меню", "menu")

HELP = (
    "<b>Мониторинг электроснабжения</b>\n\n"
    "Всё управление — кнопками в сообщении-меню. Открыть его заново: "
    "/menu или кнопка «Меню» слева от поля ввода.\n\n"
    "🔄 Обновить — свежий статус\n"
    "🔍 Проверить — внеочередная проверка\n"
    "📜 Журнал — последние события\n"
    "⚡ Сбои — пропадания связи, не ставшие отключением\n"
    "📊 Статистика — за сутки, неделю или месяц\n"
    "🔕 Тихий режим — уведомления без звука\n\n"
    "Команды с числом: /log 30, /flaps 30, /stats 90, /mute 45."
)


def _inline(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


MENU_MARKUP = _inline([
    [("🔄 Обновить", "menu"), ("🔍 Проверить", "check")],
    [("📜 Журнал", "log"), ("⚡ Сбои", "flaps")],
    [("📊 Статистика", "stats:7"), ("🔕 Тихий режим", "mute")],
])
BACK_MARKUP = _inline([[BACK]])


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

        if text in LEGACY_BUTTONS:
            command, args = "menu", []
        elif text.startswith("/"):
            parts = text.split()
            command = parts[0].split("@")[0].lstrip("/").lower()
            args = parts[1:]
        else:
            return

        if chat_id not in self.telegram.chat_ids:
            log.warning("сообщение из постороннего чата %s", chat_id)
            self.telegram.send(chat_id, "Доступ запрещён.")
            return

        self._command(chat_id, command, args)

    def _command(self, chat_id: int, command: str, args: list[str]) -> None:
        """Команда присылает нужный экран новым сообщением."""
        if command in ("start", "menu"):
            self._remove_reply_keyboard(chat_id)
            screen = self._menu_screen()
        elif command == "status":
            screen = self._menu_screen()
        elif command == "help":
            screen = (HELP, MENU_MARKUP)
        elif command == "check":
            screen = self._check_screen()
        elif command in ("log", "history"):
            screen = self._log_screen(self._int_arg(args, 15, 1, 100))
        elif command == "flaps":
            screen = self._flaps_screen(self._int_arg(args, 15, 1, 100))
        elif command == "stats":
            screen = self._stats_screen(self._int_arg(args, 7, 1, 365))
        elif command == "mute":
            if args:
                self._set_mute(self._int_arg(args, 60, 1, 10080))
            screen = self._mute_screen()
        elif command == "unmute":
            self._set_mute(0)
            screen = self._mute_screen()
        else:
            screen = ("Неизвестная команда. Откройте меню: /menu", MENU_MARKUP)
        self.telegram.send(chat_id, screen[0], reply_markup=screen[1])

    def _handle_callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if chat_id not in self.telegram.chat_ids:
            log.warning("нажатие кнопки из постороннего чата %s", chat_id)
            self.telegram.answer_callback(query["id"], "Доступ запрещён.", alert=True)
            return

        message_id = message["message_id"]
        action, _, value = (query.get("data") or "").partition(":")

        if action == "check":
            # Проверка идёт несколько секунд — сразу показать, что работаем.
            self.telegram.answer_callback(query["id"], "Проверяю…")
            self.telegram.edit(chat_id, message_id, "🔍 Проверяю…")
            screen = self._check_screen()
        else:
            self.telegram.answer_callback(query["id"])
            if action == "log":
                screen = self._log_screen(15)
            elif action == "flaps":
                screen = self._flaps_screen(15)
            elif action == "stats" and value.isdigit():
                screen = self._stats_screen(max(1, min(365, int(value))))
            elif action == "mute":
                if value.isdigit():
                    self._set_mute(max(1, min(10080, int(value))))
                screen = self._mute_screen()
            elif action == "unmute":
                self._set_mute(0)
                screen = self._mute_screen()
            else:  # "menu" и всё неизвестное — главный экран
                screen = self._menu_screen()
        self.telegram.edit(chat_id, message_id, screen[0], screen[1])

    def _remove_reply_keyboard(self, chat_id: int) -> None:
        """Убрать клавиатуру под полем ввода, оставшуюся от прошлой версии."""
        sent = self.telegram.send(
            chat_id, "Меню обновлено", reply_markup={"remove_keyboard": True}
        )
        if sent:
            self.telegram.delete(chat_id, sent["message_id"])

    # ---------- экраны: (текст, кнопки) ----------

    def _menu_screen(self) -> tuple[str, dict]:
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
            blocks.append(self._target_block(target))

        if self._mute_left():
            blocks.append(f"🔕 Тихий режим ещё {fmt_duration(self._mute_left())}")

        blocks.append(f"<i>Обновлено в {fmt_short_time(now_ts())}</i>")
        return "\n\n".join(blocks), MENU_MARKUP

    def _target_block(self, target: dict) -> str:
        state = self.storage.get_state(target["id"])
        if not state:
            return f"{ICON[NONET]} <b>Нет данных</b> · {target_title(target)}"

        status = state["status"]
        since = state["since_ts"]
        suspect = self.monitor.suspect_since(target["id"])
        pending = self.monitor.pending_info(target["id"])
        if suspect:
            deadline = suspect + self.monitor.confirm_after
            lines = [
                f"🟡 <b>Нет связи</b> · {target_title(target)}",
                f"Не отвечает уже {fmt_duration(now_ts() - suspect)} — "
                "возможно, пропал свет",
                f"Если связь не появится до {fmt_short_time(deadline)}, "
                "это отключение",
            ]
            pending = None
        else:
            verb = "пропал" if status == DOWN else "появился"
            lines = [
                f"{ICON[status]} <b>{LABEL[status]}</b> · {target_title(target)}",
                f"Уже {fmt_duration(now_ts() - since)} — {verb} {fmt_when(since)}",
            ]
        if pending:
            if pending[0] == DOWN:
                hint = "нет ответа, проверяю связь"
                needed = self.monitor.fail_threshold
            else:
                hint = "появился ответ, проверяю, вернулся ли свет"
                needed = self.monitor.ok_threshold
            lines.append(f"⏳ {hint} ({pending[1]}/{needed})")
        return "\n".join(lines)

    def _check_screen(self) -> tuple[str, dict]:
        lines = ["<b>Разовая проверка</b>"]
        statuses = []
        for target in self.monitor.targets:
            status = self.monitor.checker.check(target["hosts"])
            statuses.append(status)
            line = f"{ICON[status]} {LABEL[status]} · {target_title(target)}"
            if status == NONET:
                line += " — у сервера нет интернета"
            lines.append(line)
        if NONET not in statuses:
            lines.append(
                "\n<i>Это одна проверка без подтверждения — статус меняется "
                "только после нескольких проверок подряд.</i>"
            )
        # Полноценная проверка со сменой статуса и уведомлениями.
        self.monitor.check_now()
        return "\n".join(lines), BACK_MARKUP

    def _log_screen(self, limit: int) -> tuple[str, dict]:
        events = self.storage.last_events(limit)
        if not events:
            return "📜 Журнал пуст.", BACK_MARKUP

        targets = {t["id"]: t for t in self.monitor.targets}
        lines = [f"📜 <b>Последние события ({len(events)})</b>"]
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
        return "\n".join(lines), BACK_MARKUP

    def _flaps_screen(self, limit: int) -> tuple[str, dict]:
        flaps = self.storage.last_flaps(limit)
        if not flaps:
            return "⚡ Кратковременных сбоев не зафиксировано.", BACK_MARKUP
        targets = {t["id"]: t for t in self.monitor.targets}
        lines = ["⚡ <b>Кратковременные сбои связи</b>"]
        for flap in flaps:
            target = targets.get(flap["target_id"], {"name": flap["target_id"]})
            lines.append(
                f"{fmt_day_time(flap['ts'])} · {target_title(target)} — "
                f"не отвечал ~{fmt_duration(flap['seconds'])}"
            )
        lines.append(
            "\n<i>Объект ненадолго перестал отвечать и снова появился раньше, "
            "чем подтвердилось отключение. Статус не менялся.</i>"
        )
        return "\n".join(lines), BACK_MARKUP

    def _stats_screen(self, days: int) -> tuple[str, dict]:
        since = now_ts() - days * 86400
        lines = [f"📊 <b>Статистика за {days} дн.</b>"]
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
        periods = [
            (f"• {label} •" if d == days else label, f"stats:{d}")
            for label, d in STATS_PERIODS
        ]
        return "\n".join(lines), _inline([periods, [BACK]])

    def _mute_screen(self) -> tuple[str, dict]:
        left = self._mute_left()
        if left:
            until = int(self.storage.get_setting("mute_until"))
            state = (
                f"🔕 Уведомления без звука до {fmt_short_time(until)} "
                f"(ещё {fmt_duration(left)})."
            )
        else:
            state = "🔔 Уведомления приходят со звуком."
        text = f"{state}\n\nНа сколько отключить звук?"

        options = [(label, f"mute:{minutes}") for label, minutes in MUTE_OPTIONS]
        rows = [options[:3], options[3:]]
        if left:
            rows.append([("🔔 Включить звук", "unmute")])
        rows.append([BACK])
        return text, _inline(rows)

    # ---------- утилиты ----------

    def _set_mute(self, minutes: int) -> None:
        until = now_ts() + minutes * 60 if minutes else 0
        self.storage.set_setting("mute_until", str(until))

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
