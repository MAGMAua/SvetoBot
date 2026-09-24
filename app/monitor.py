"""Непрерывный опрос объектов и рассылка уведомлений.

Опрос идёт раз в poll_interval секунд. Статус объекта меняется только после
нескольких одинаковых результатов подряд (fail_threshold / ok_threshold),
поэтому одиночные потери пакетов не дают ложных срабатываний. Пропадания,
не дошедшие до порога, записываются отдельно как кратковременные сбои.
"""
from __future__ import annotations

import logging
import threading
import time

from checker import DOWN, NONET, UP, Checker
from util import fmt_duration, fmt_time, now_ts

log = logging.getLogger("monitor")

LABEL = {UP: "Свет есть", DOWN: "Света нет", NONET: "Нет связи"}
ICON = {UP: "💡", DOWN: "🕯", NONET: "❓"}

TOUCH_EVERY = 60  # как часто записывать факт проверки в БД, секунд


class Monitor:
    def __init__(self, config: dict, storage, telegram):
        self.config = config
        self.storage = storage
        self.telegram = telegram
        self.checker = Checker(config)
        self.targets = config["targets"]

        self.poll = max(2, int(config.get("poll_interval", 10)))
        self.fail_threshold = max(1, int(config.get("fail_threshold", 3)))
        self.ok_threshold = max(1, int(config.get("ok_threshold", 2)))
        self.internet_alert_after = int(config.get("internet_alert_after", 120))

        self.last_check: dict[str, int] = {}      # id -> ts последнего опроса
        self.last_result: dict[str, str] = {}     # id -> последний сырой результат
        self._pending: dict[str, list] = {}       # id -> [status, count]
        self._touched: dict[str, int] = {}

        self._nonet_streak = 0
        self._nonet_notified = False

        self._wake = threading.Event()
        self._stop = threading.Event()

    # ---------- публичное ----------

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def check_now(self) -> None:
        self._wake.set()

    def pending_info(self, target_id: str) -> tuple[str, int] | None:
        """Что сейчас 'копится' по объекту: (статус, сколько подтверждений)."""
        pending = self._pending.get(target_id)
        return (pending[0], pending[1]) if pending else None

    def server_offline(self) -> bool:
        return self._nonet_notified

    # ---------- цикл ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("ошибка в цикле опроса")
            delay = max(1.0, self.poll - (time.monotonic() - started))
            self._wake.wait(delay)
            self._wake.clear()

    def _tick(self) -> None:
        if not self.checker.internet_alive():
            self._nonet_streak += 1
            offline_for = self._nonet_streak * self.poll
            if not self._nonet_notified and offline_for >= self.internet_alert_after:
                self._nonet_notified = True
                self.telegram.broadcast(
                    "⚠️ <b>Сервер потерял связь с интернетом</b>\n"
                    "Проверка объектов приостановлена, статусы сохранены."
                )
            log.warning("нет связи у сервера (%s с)", offline_for)
            return

        if self._nonet_notified:
            self.telegram.broadcast(
                "✅ <b>Связь восстановлена</b>\nПроверка объектов продолжена."
            )
        self._nonet_streak = 0
        self._nonet_notified = False

        for target in self.targets:
            if self._stop.is_set():
                return
            self._poll_target(target)

    def _poll_target(self, target: dict) -> None:
        target_id = target["id"]
        status = UP if self.checker.any_host_alive(target["hosts"]) else DOWN

        ts = now_ts()
        self.last_check[target_id] = ts
        self.last_result[target_id] = status

        state = self.storage.get_state(target_id)
        if state is None:
            self.storage.set_status(target_id, status)
            self._notify_start(target, status)
            return

        if status == state["status"]:
            self._settle(target, status)
            self._touch(target_id, status, ts)
            return

        pending = self._pending.get(target_id)
        if pending and pending[0] == status:
            pending[1] += 1
        else:
            pending = [status, 1]
            self._pending[target_id] = pending

        needed = self.fail_threshold if status == DOWN else self.ok_threshold
        log.info(
            "%s: %s (%d/%d подтверждений)", target_id, status, pending[1], needed
        )
        self._touch(target_id, status, ts)

        if pending[1] >= needed:
            self._pending.pop(target_id, None)
            # Смена началась с первого несовпадения, а не с момента подтверждения.
            changed_at = ts - (needed - 1) * self.poll
            duration = self.storage.set_status(target_id, status, changed_at)
            self._notify_change(target, status, duration, changed_at)

    def _settle(self, target: dict, status: str) -> None:
        """Результат совпал с текущим статусом — сбросить накопленное."""
        pending = self._pending.pop(target["id"], None)
        if not pending:
            return
        if pending[0] == DOWN and status == UP:
            seconds = pending[1] * self.poll
            self.storage.record_flap(target["id"], seconds)
            log.info("%s: кратковременный сбой ~%d с", target["id"], seconds)

    def _touch(self, target_id: str, status: str, ts: int) -> None:
        """Писать факт проверки в БД не чаще раза в минуту."""
        if ts - self._touched.get(target_id, 0) < TOUCH_EVERY:
            return
        self._touched[target_id] = ts
        self.storage.touch(target_id, status)

    # ---------- уведомления ----------

    def _muted(self) -> bool:
        until = self.storage.get_setting("mute_until")
        return bool(until and int(until) > now_ts())

    def _notify_start(self, target: dict, status: str) -> None:
        self.telegram.broadcast(
            f"{target.get('emoji', '')} <b>{target['name']}</b>\n"
            f"Мониторинг запущен. Сейчас: {ICON[status]} {LABEL[status]}",
            disable_notification=True,
        )

    def _notify_change(
        self, target: dict, status: str, duration: int | None, changed_at: int
    ) -> None:
        lines = [
            f"{ICON[status]} <b>{target['name']}: {LABEL[status]}</b>",
            f"Время: {fmt_time(changed_at)}",
        ]
        if duration:
            previous = "со светом" if status == DOWN else "без света"
            lines.append(
                f"Предыдущее состояние ({previous}) длилось {fmt_duration(duration)}"
            )
        self.telegram.broadcast("\n".join(lines), disable_notification=self._muted())
