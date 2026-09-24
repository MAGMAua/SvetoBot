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


def fmt_when(ts: int | None) -> str:
    """Относительно сегодняшнего дня: 'сегодня в 15:22', 'вчера в 23:10', '21.09 в 08:05'."""
    if not ts:
        return "—"
    tz = TZ or dt.timezone.utc
    moment = dt.datetime.fromtimestamp(ts, tz)
    days_ago = (dt.datetime.now(tz).date() - moment.date()).days
    clock = moment.strftime("%H:%M")
    if days_ago == 0:
        return f"сегодня в {clock}"
    if days_ago == 1:
        return f"вчера в {clock}"
    if moment.year == dt.datetime.now(tz).year:
        return f"{moment.strftime('%d.%m')} в {clock}"
    return f"{moment.strftime('%d.%m.%Y')} в {clock}"


def fmt_day_time(ts: int | None) -> str:
    """Для журнала: 24.09 15:22"""
    if not ts:
        return "—"
    moment = dt.datetime.fromtimestamp(ts, TZ or dt.timezone.utc)
    return moment.strftime("%d.%m %H:%M")


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
