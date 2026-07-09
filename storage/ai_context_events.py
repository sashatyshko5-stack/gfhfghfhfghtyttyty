"""Краткая лента событий чата для системного промпта ИИ (баны/муты, закрепы, входы).

Хранится в памяти (ring-buffer), как bot_outgoing — перезапуск бота очищает.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
MAX_PER_CHAT = 40
MAX_LINE_LEN = 280

_store: dict[int, deque] = {}
_lock = threading.Lock()


def _trim(line: str) -> str:
    line = (line or "").replace("\n", " ").strip()
    if len(line) > MAX_LINE_LEN:
        return line[: MAX_LINE_LEN - 1] + "…"
    return line


def format_user_tg(user) -> str:
    """Одна строка: id, @ник, имя."""
    if not user:
        return "?"
    parts = [str(user.id)]
    if getattr(user, "username", None):
        parts.append(f"@{user.username}")
    fn = (getattr(user, "full_name", None) or getattr(user, "first_name", None) or "").strip()
    if fn:
        parts.append(fn)
    return " ".join(parts)


def log_chat_event(chat_id: int, line: str) -> None:
    if not line:
        return
    line = _trim(line)
    if not line:
        return
    with _lock:
        buf = _store.get(chat_id)
        if buf is None:
            buf = deque(maxlen=MAX_PER_CHAT)
            _store[chat_id] = buf
        buf.append({"ts": datetime.now().isoformat(timespec="seconds"), "text": line})


def get_recent_events(chat_id: int, limit: int = 18) -> list[dict]:
    with _lock:
        buf = _store.get(chat_id)
        if not buf:
            return []
        items = list(buf)
    return items[-limit:]


def format_chat_events_for_prompt(chat_id: int, limit: int = 15) -> str:
    items = get_recent_events(chat_id, limit)
    if not items:
        return ""
    lines = []
    for it in items:
        ts = it.get("ts", "")
        txt = (it.get("text") or "").replace("\n", " ")
        lines.append(f"- [{ts}] {txt}")
    return "\n".join(lines)
