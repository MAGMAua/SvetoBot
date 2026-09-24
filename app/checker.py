"""Проверка доступности объектов через ICMP-ping.

При непрерывном опросе повторные попытки внутри одной проверки не нужны:
защиту от кратковременных помех обеспечивает монитор, который меняет статус
только после нескольких одинаковых результатов подряд.
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("checker")

UP = "up"
DOWN = "down"
NONET = "nonet"

_ping_missing_reported = False


def ping(host: str, count: int = 1, timeout: int = 2, deadline: int = 3) -> bool:
    """True, если хост ответил хотя бы на один пакет."""
    global _ping_missing_reported

    cmd = [
        "ping",
        "-n",  # не резолвить имена
        "-q",  # тихий вывод
        "-c", str(count),
        "-W", str(timeout),
        "-w", str(deadline),
        host,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=deadline + 3,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("ping %s: превышено время ожидания", host)
        return False
    except FileNotFoundError:
        if not _ping_missing_reported:
            log.error("нет утилиты ping (нужен пакет iputils-ping)")
            _ping_missing_reported = True
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("ping %s: %s", host, exc)
        return False


class Checker:
    def __init__(self, config: dict):
        net = config.get("network", {})
        self.internet_hosts: list[str] = net.get(
            "internet_hosts", ["77.88.8.8", "8.8.8.8"]
        )
        self.count: int = int(net.get("ping_count", 1))
        self.timeout: int = int(net.get("ping_timeout", 2))
        self.deadline: int = int(net.get("ping_deadline", 3))

    def internet_alive(self) -> bool:
        """Есть ли связь у самого сервера. Достаточно ответа одного адреса."""
        return any(
            ping(host, 1, self.timeout, self.deadline) for host in self.internet_hosts
        )

    def any_host_alive(self, hosts: list[str]) -> bool:
        return any(
            ping(host, self.count, self.timeout, self.deadline) for host in hosts
        )

    def check(self, hosts: list[str]) -> str:
        """Разовая проверка объекта: UP / DOWN / NONET."""
        if self.any_host_alive(hosts):
            return UP
        return DOWN if self.internet_alive() else NONET
