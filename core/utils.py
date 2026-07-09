import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Tuple
from datetime import datetime, timedelta

from aiogram.types import Message

from ..storage.state import settings, punished_users
from ..core.loader import bot

# ─── Отдельный лог для наказанных пользователей ──────────────────────────
_PUNISH_LOG_DIR = "logs"
_PUNISH_LOG_PATH = os.path.join(_PUNISH_LOG_DIR, "punished.log")
os.makedirs(_PUNISH_LOG_DIR, exist_ok=True)

_punish_logger = logging.getLogger("bot.punished")
_punish_logger.setLevel(logging.INFO)
_punish_logger.propagate = False
if not _punish_logger.handlers:
    _fh = RotatingFileHandler(_PUNISH_LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _punish_logger.addHandler(_fh)


def escape_markdown_v2_keep_bold(text: str) -> str:
    """Экранирование для MarkdownV2, сохраняя жирный текст"""
    escape_chars = r"\_[]()~`>#+-=|{}.!"

    def esc(t):
        return "".join("\\" + c if c in escape_chars else c for c in t)

    parts = re.split(r"(\*\*.+?\*\*)", text)
    res = []
    for p in parts:
        if p.startswith("**") and p.endswith("**"):
            inner = esc(p[2:-2])
            res.append(f"**{inner}**")
        else:
            res.append(esc(p))
    return "".join(res)


async def is_admin(message: Message) -> bool:
    """Проверка, является ли пользователь админом"""
    try:
        member = await message.bot.get_chat_member(
            message.chat.id, message.from_user.id
        )
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _punish_key(chat_id, user_id: int, punishment_type: str):
    """Нормализуем ключ наказания (chat_id всегда как строка, чтобы не было рассинхрона int/str)."""
    return (str(chat_id), int(user_id), str(punishment_type))


async def is_already_punished(chat_id, user_id: int, punishment_type: str) -> bool:
    """Проверяет фактический статус ограничений пользователя в Telegram."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        
        if punishment_type == "мут":
            # Проверяем, может ли пользователь отправлять сообщения
            if hasattr(member, 'can_send_messages') and not member.can_send_messages:
                return True
            return False
            
        elif punishment_type == "бан":
            # Проверяем, забанен ли пользователь
            return member.status in ("kicked", "left")
            
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки статуса пользователя {user_id}: {e}")
        return False


async def mark_punished(
    chat_id,
    user_id: int,
    punishment_type: str,
    *,
    reason: str = "",
    duration: str = "",
    by: str = "",
    username: str = "",
) -> None:
    """Помечает пользователя как наказанного и пишет запись в logs/punished.log."""
    punished_users.add(_punish_key(chat_id, user_id, punishment_type))
    try:
        line = (
            f"chat={chat_id} user_id={user_id}"
            + (f" username=@{username}" if username else "")
            + f" punishment={punishment_type}"
            + (f" duration={duration}" if duration else "")
            + (f" by={by}" if by else "")
            + (f" reason={reason}" if reason else "")
        )
        _punish_logger.info(line)
    except Exception:
        pass


async def clear_punished(chat_id, user_id: int, punishment_type: str) -> None:
    """Снимает отметку наказания (например, если оно сняли/истёк срок)."""
    punished_users.discard(_punish_key(chat_id, user_id, punishment_type))
    try:
        _punish_logger.info(
            f"chat={chat_id} user_id={user_id} punishment={punishment_type} action=cleared"
        )
    except Exception:
        pass


async def already_punished(chat_id, user_id: int, punishment_type: str) -> bool:
    """⚠️ LEGACY совместимость."""
    return await is_already_punished(chat_id, user_id, punishment_type)


def get_duration_seconds(duration: int, unit: str) -> int:
    """Конвертация времени в секунды"""
    multipliers = {
        "сек": 1,
        "мин": 60,
        "час": 3600,
        "день": 86400,
        "дней": 86400,
        "дня": 86400,
    }
    return duration * multipliers.get(unit, 60)


async def can_bot_restrict_members(message: Message) -> Tuple[bool, str]:
    """Проверка, может ли бот ограничивать участников"""
    try:
        bot_member = await message.bot.get_chat_member(
            message.chat.id, (await bot.get_me()).id
        )
        if bot_member.status not in ("administrator", "creator"):
            return False, "Бот не является админом"
        if hasattr(bot_member, "can_restrict_members") and not bot_member.can_restrict_members:
            return False, "У бота нет прав на ограничение участников"
        return True, ""
    except Exception as e:
        return False, str(e)
