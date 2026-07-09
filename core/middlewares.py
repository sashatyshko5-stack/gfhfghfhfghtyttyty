import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

from ..storage.message_logs import (
    log_message,
    extract_entry_from_message,
    save_message_logs,
    is_dirty,
)
from ..storage.state import settings, group_users

logger = logging.getLogger(__name__)


def _remember_user(event: Message) -> None:
    """Запоминает участника чата для AI-подсказок (нужен для [ACTION:MUTE:<id>] и т.п.)."""
    try:
        u = event.from_user
        if not u or u.is_bot:
            return
        chat_id = event.chat.id
        bucket = group_users.setdefault(chat_id, {})
        bucket[u.id] = {
            "id": u.id,
            "username": u.username or "",
            "first_name": u.first_name or "",
            "last_name": u.last_name or "",
            "last_seen": int(__import__("time").time()),
        }
        # не разрастаемся без меры: храним до 200 последних по чату
        if len(bucket) > 200:
            oldest = sorted(bucket.items(), key=lambda kv: kv[1].get("last_seen", 0))[:len(bucket) - 200]
            for uid, _ in oldest:
                bucket.pop(uid, None)
    except Exception as e:
        logger.debug(f"[MSG-LOG] remember_user err: {e}")


class MessageLoggerMiddleware(BaseMiddleware):
    """Outer middleware: пишет в журнал ВСЕ сообщения из групп, ничего не блокирует."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        try:
            if isinstance(event, Message) and event.chat and event.chat.type in ("group", "supergroup"):
                if event.from_user:
                    entry = extract_entry_from_message(event)
                    log_message(event.chat.id, event.chat.title or "", entry)
                    if not event.from_user.is_bot:
                        _remember_user(event)
        except Exception as e:
            logger.error(f"[MSG-LOG] Ошибка журналирования: {e}")
        return await handler(event, data)


class PrivacyGateMiddleware(BaseMiddleware):
    """Outer middleware: пока админ не принял политику — блокируем ВСЕ команды/сообщения
    (кроме callback-кнопок политики и ChatMemberUpdated).

    Разрешено проходить:
      - CallbackQuery с data, начинающейся на `privacy:`
      - Команды `!политика` / `.политика`
      - Системные сообщения о входе/выходе участников
      - Приватные чаты
    """

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        try:
            if isinstance(event, CallbackQuery):
                if event.data and event.data.startswith("privacy:"):
                    return await handler(event, data)
                if event.data and event.data.startswith("sub:"):
                    return await handler(event, data)
                chat = event.message.chat if event.message else None
                if chat and chat.type in ("group", "supergroup"):
                    if not _is_accepted(chat.id):
                        try:
                            await event.answer("Бот ещё не активирован админом.", show_alert=False)
                        except Exception:
                            pass
                        return None
                return await handler(event, data)

            if isinstance(event, Message):
                if not event.chat or event.chat.type not in ("group", "supergroup"):
                    return await handler(event, data)
                if event.new_chat_members or event.left_chat_member:
                    return await handler(event, data)
                txt = (event.text or "").strip().lower()
                if txt in ("!политика", ".политика"):
                    return await handler(event, data)

                if not _is_accepted(event.chat.id):
                    logger.debug(
                        f"[PRIVACY-GATE] blocked msg in chat {event.chat.id} "
                        f"(privacy not accepted)"
                    )
                    return None
        except Exception as e:
            logger.error(f"[PRIVACY-GATE] error: {e}")

        return await handler(event, data)


def _is_accepted(chat_id) -> bool:
    return bool(settings.get(str(chat_id), {}).get("privacy_accepted"))


async def message_logs_autosave_task(interval_seconds: int = 15):
    """Фоновая задача: сохраняет JSON каждые `interval_seconds` секунд, если есть изменения."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if is_dirty():
                save_message_logs()
        except asyncio.CancelledError:
            try:
                save_message_logs()
            except Exception:
                pass
            raise
        except Exception as e:
            logger.error(f"[MSG-LOG] Ошибка автосейва: {e}")