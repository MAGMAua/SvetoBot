"""Непрерывный опрос объектов и рассылка уведомлений.

Опрос идёт раз в poll_interval секунд. Отключение подтверждается в два этапа:
после fail_threshold неудач подряд приходит тихое «нет связи», а «света нет» —
только если объект молчит outage_confirm_after секунд. Так сбой связи у
провайдера на объекте не выдаётся за отключение света. Возврат подтверждается
после ok_threshold удачных проверок. Пропадания, не ставшие отключением,
записываются отдельно как кратковременные сбои.
"""
from __future__ import annotations

import logging
import threading
import time

from checker import DOWN, NONET, UP, Checker
from util import escape_html, fmt_duration, fmt_short_time, fmt_when, now_ts

log = logging.getLogger("monitor")

LABEL = {UP: "Свет есть", DOWN: "Света нет", NONET: "Неизвестно"}
ICON = {UP: "🟢", DOWN: "🔴", NONET: "⚪"}

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
        self.confirm_after = max(0, int(config.get("outage_confirm_after", 300)))

        self.last_check: dict[str, int] = {}      # id -> ts последнего опроса
        self.last_result: dict[str, str] = {}     # id -> последний сырой результат
        self._pending: dict[str, list] = {}       # id -> [status, count, first_ts]
        self._suspect: dict[str, int] = {}        # id -> с какого момента нет связи
        self._touched: dict[str, int] = {}

        self._nonet_since: int | None = None   # с какого момента у сервера нет сети
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

    def suspect_since(self, target_id: str) -> int | None:
        """Объект не отвечает, но отключение ещё не подтверждено: с какого момента."""
        return self._suspect.get(target_id)

    def server_offline_since(self) -> int | None:
        """Момент потери связи сервером, если она пропала надолго, иначе None."""
        return self._nonet_since if self._nonet_notified else None

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
            if self._nonet_since is None:
                self._nonet_since = now_ts()
            offline_for = now_ts() - self._nonet_since
            if not self._nonet_notified and offline_for >= self.internet_alert_after:
                self._nonet_notified = True
                # Дойдёт, только если пропал ICMP, а не весь интернет.
                self.telegram.broadcast(
                    "⚠️ <b>Сервер без интернета</b>\n"
                    f"Связь пропала {fmt_when(self._nonet_since)} — "
                    "проверка света приостановлена.\n"
                    "Статусы объектов сохранены. Сообщу, когда связь вернётся."
                )
            log.warning("нет связи у сервера (%s с)", offline_for)
            return

        if self._nonet_notified:
            self.telegram.broadcast(
                "✅ <b>Сервер снова на связи</b>\n"
                f"Связь пропала {fmt_when(self._nonet_since)}, "
                f"не было {fmt_duration(now_ts() - self._nonet_since)}.\n"
                "Всё это время свет не проверялся — проверка продолжена."
            )
        if self._nonet_since is not None:
            # Пока сервер был без сети, объекты не проверялись — это время
            # нельзя засчитывать в отключение, копим подтверждения заново.
            self._pending.clear()
        self._nonet_since = None
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
            self._settle(target, status, ts)
            self._touch(target_id, status, ts)
            return

        pending = self._pending.get(target_id)
        if pending and pending[0] == status:
            pending[1] += 1
        else:
            pending = [status, 1, ts]
            self._pending[target_id] = pending
        count, first_ts = pending[1], pending[2]
        self._touch(target_id, status, ts)

        if status == UP:
            log.info("%s: up (%d/%d подтверждений)", target_id, count, self.ok_threshold)
            if count >= self.ok_threshold:
                self._confirm(target, UP, first_ts)
            return

        silent_for = ts - first_ts
        log.info(
            "%s: down (%d/%d, нет ответа %d с из %d)",
            target_id, count, self.fail_threshold, silent_for, self.confirm_after,
        )
        if count < self.fail_threshold:
            return
        if silent_for >= self.confirm_after:
            self._confirm(target, DOWN, self._suspect.get(target_id, first_ts))
        elif target_id not in self._suspect:
            self._suspect[target_id] = first_ts
            self._notify_suspect(target, first_ts)

    def _confirm(self, target: dict, status: str, changed_at: int) -> None:
        """Подтвердить смену статуса. Датируется первым несовпадением."""
        self._pending.pop(target["id"], None)
        self._suspect.pop(target["id"], None)
        duration = self.storage.set_status(target["id"], status, changed_at)
        self._notify_change(target, status, duration, changed_at)

    def _settle(self, target: dict, status: str, ts: int) -> None:
        """Результат совпал с текущим статусом — сбросить накопленное."""
        pending = self._pending.pop(target["id"], None)
        suspect = self._suspect.pop(target["id"], None)
        if status != UP or not (pending or suspect):
            return
        if pending and pending[0] != DOWN and not suspect:
            return
        started = suspect or pending[2]
        seconds = max(self.poll, ts - started)
        self.storage.record_flap(target["id"], seconds)
        log.info("%s: сбой связи ~%d с, свет не пропадал", target["id"], seconds)
        if suspect:
            self._notify_recovered(target, seconds)

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
            f"{ICON[status]} <b>{LABEL[status]}</b> · {target_title(target)}\n"
            "Мониторинг запущен.",
            disable_notification=True,
        )

    def _notify_suspect(self, target: dict, since: int) -> None:
        deadline = fmt_short_time(since + self.confirm_after)
        self.telegram.broadcast(
            f"🟡 <b>Нет связи</b> · {target_title(target)}\n"
            f"Не отвечает с {fmt_short_time(since)} — возможно, пропал свет.\n"
            f"Если связь не появится до {deadline}, сообщу об отключении.",
            disable_notification=True,
        )

    def _notify_recovered(self, target: dict, seconds: int) -> None:
        self.telegram.broadcast(
            f"{ICON[UP]} <b>Связь восстановлена</b> · {target_title(target)}\n"
            f"Свет не пропадал — это был сбой связи на {fmt_duration(seconds)}.",
            disable_notification=True,
        )

    def _notify_change(
        self, target: dict, status: str, duration: int | None, changed_at: int
    ) -> None:
        if status == DOWN:
            lines = [
                f"{ICON[DOWN]} <b>Света нет</b> · {target_title(target)}",
                f"Пропал {fmt_when(changed_at)}",
            ]
            if duration:
                lines.append(f"До этого свет был {fmt_duration(duration)}")
        else:
            lines = [
                f"{ICON[UP]} <b>Свет есть</b> · {target_title(target)}",
                f"Появился {fmt_when(changed_at)}",
            ]
            if duration:
                lines.append(f"Света не было {fmt_duration(duration)}")
        self.telegram.broadcast("\n".join(lines), disable_notification=self._muted())


def target_title(target: dict) -> str:
    """'🏢 Работа' — эмодзи и название объекта."""
    emoji = target.get("emoji", "")
    name = escape_html(target["name"])
    return f"{emoji} {name}" if emoji else name
