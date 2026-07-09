"""Лог исходящих non-AI сообщений бота (по чатам).

Используется чтобы ИИ «помнил», какие сообщения бот слал сам (не AI-ответы и не
сообщения пользователей) — например ответы на команды (`!правила`, `!бан` и т.п.)
— и мог учитывать этот контекст при следующем обращении.

Хранится только в памяти (ring-buffer, до MAX_PER_CHAT на чат).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Iterable

MAX_PER_CHAT = 30
MAX_TEXT_LEN = 400

_store: dict[int, deque] = {}
_lock = threading.Lock()


def log_bot_message(chat_id: int, text: str) -> None:
    """Сохранить одно non-AI сообщение бота в буфер чата."""
    if not text:
        return
    text = text.strip()
    if not text:
        return
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "…"
    with _lock:
        buf = _store.get(chat_id)
        if buf is None:
            buf = deque(maxlen=MAX_PER_CHAT)
            _store[chat_id] = buf
        buf.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "text": text,
        })


def get_recent(chat_id: int, limit: int = 8) -> list[dict]:
    """Вернуть последние `limit` non-AI сообщений бота в чате."""
    with _lock:
        buf = _store.get(chat_id)
        if not buf:
            return []
        items = list(buf)
    return items[-limit:]


def format_recent_for_prompt(chat_id: int, limit: int = 8) -> str:
    """Готовая строка, которую можно вставить в системный промпт."""
    items = get_recent(chat_id, limit)
    if not items:
        return ""
    lines: list[str] = []
    for it in items:
        ts = it.get("ts", "")
        txt = (it.get("text") or "").replace("\n", " ")
        lines.append(f"- [{ts}] {txt}")
    return "\n".join(lines)
