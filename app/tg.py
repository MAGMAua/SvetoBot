"""Минимальный клиент Telegram Bot API (без внешних SDK)."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("tg")


class Telegram:
    def __init__(self, token: str, chat_ids: list[int]):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_ids = chat_ids
        self.session = requests.Session()

    def _call(self, method: str, http_timeout: int = 20, **params):
        try:
            response = self.session.post(
                f"{self.base}/{method}", data=params, timeout=http_timeout
            )
            payload = response.json()
            if not payload.get("ok"):
                log.error("%s: %s", method, payload.get("description"))
                return None
            return payload["result"]
        except requests.RequestException as exc:
            log.warning("%s: сеть недоступна (%s)", method, exc)
            return None
        except ValueError:
            log.error("%s: некорректный ответ сервера", method)
            return None

    def send(self, chat_id: int, text: str, disable_notification: bool = False):
        return self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=disable_notification,
        )

    def broadcast(self, text: str, disable_notification: bool = False):
        for chat_id in self.chat_ids:
            self.send(chat_id, text, disable_notification)

    def get_updates(self, offset: int | None, timeout: int = 30):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", http_timeout=timeout + 15, **params)
        if result is None:
            # Сеть или API недоступны — не долбить сервер в цикле.
            time.sleep(5)
            return []
        return result

    def set_commands(self, commands: list[tuple[str, str]]):
        import json

        return self._call(
            "setMyCommands",
            commands=json.dumps(
                [{"command": c, "description": d} for c, d in commands],
                ensure_ascii=False,
            ),
        )
