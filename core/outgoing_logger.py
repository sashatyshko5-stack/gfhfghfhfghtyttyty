"""Перехват исходящих сообщений бота → пишем non-AI-сообщения в bot_outgoing.

Через aiogram3 Request Middleware:
любой вызов SendMessage/SendPhoto/SendDocument/... анализируем, берём text/caption
и chat_id и, если это НЕ AI-ответ (см. contextvar `ai_response_ctx`), сохраняем.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware

from ..storage.bot_outgoing import log_bot_message

logger = logging.getLogger(__name__)

# Флаг «сейчас отправляется AI-ответ» — ставится в хендлере перед reply/answer.
ai_response_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ai_response_ctx", default=False
)

# Методы, у которых может быть текст/подпись для лога.
_TEXTY_METHODS = {
    "SendMessage",
    "SendPhoto",
    "SendVideo",
    "SendDocument",
    "SendAnimation",
    "SendAudio",
    "SendVoice",
    "SendVideoNote",
    "SendMediaGroup",
    "CopyMessage",
    "EditMessageText",
    "EditMessageCaption",
}


class OutgoingLoggerMiddleware(BaseRequestMiddleware):
    """Записывает все non-AI исходящие сообщения бота в buffer по чатам."""

    async def __call__(self, make_request, bot, method):  # type: ignore[override]
        result = await make_request(bot, method)
        try:
            cls_name = method.__class__.__name__
            if cls_name in _TEXTY_METHODS and not ai_response_ctx.get():
                chat_id = getattr(method, "chat_id", None)
                if isinstance(chat_id, int) and chat_id < 0:
                    # Только группы/каналы (у групп chat_id < 0 в Telegram).
                    text = (
                        getattr(method, "text", None)
                        or getattr(method, "caption", None)
                        or ""
                    )
                    if isinstance(text, str) and text.strip():
                        log_bot_message(chat_id, text)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[OUT-LOG] ошибка логирования: {e}")
        return result
