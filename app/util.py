"""Вспомогательные функции: форматирование времени и длительности."""
from __future__ import annotations

import datetime as dt
import os

TZ_NAME = os.environ.get("TZ", "Europe/Moscow")

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(TZ_NAME)
except Exception:  # на случай отсутствия tzdata
    TZ = None


def now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def fmt_time(ts: int | None) -> str:
    """Время в локальной зоне: 21:05 24.09.2026"""
    if not ts:
        return "—"
    moment = dt.datetime.fromtimestamp(ts, TZ or dt.timezone.utc)
    return moment.strftime("%H:%M %d.%m.%Y")


def fmt_short_time(ts: int | None) -> str:
    if not ts:
        return "—"
    moment = dt.datetime.fromtimestamp(ts, TZ or dt.timezone.utc)
    return moment.strftime("%H:%M")


def fmt_duration(seconds: int | None) -> str:
    """4520 -> '1 ч 15 мин'"""
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds} сек"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {rest} сек" if rest else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
