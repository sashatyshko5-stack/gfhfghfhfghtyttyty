import asyncio
import json
import logging
import os
import time
from typing import Dict

from aiogram import Router, F, BaseMiddleware
from aiogram.types import Message

from ..core.loader import bot
from ..storage.state import settings, save_settings

logger = logging.getLogger(__name__)
router = Router()

DEFAULT_AUTO_CLEAN_SETTINGS = {
    "enabled": False,
    "inactive_days": 7,
    "min_messages": 100,
}

_ACTIVITY_FILE = os.path.join("bot", "data", "auto_clean_activity.json")
_activity_data: Dict[str, Dict[str, Dict]] = {}


def _load_activity():
    global _activity_data
    try:
        if os.path.exists(_ACTIVITY_FILE):
            with open(_ACTIVITY_FILE, "r", encoding="utf-8") as f:
                _activity_data = json.load(f)
            logger.info(f"[AUTO-CLEAN] Загружена активность для {len(_activity_data)} чатов")
    except Exception as e:
        logger.error(f"[AUTO-CLEAN] Ошибка загрузки активности: {e}")
        _activity_data = {}


def _save_activity():
    try:
        os.makedirs(os.path.dirname(_ACTIVITY_FILE), exist_ok=True)
        with open(_ACTIVITY_FILE, "w", encoding="utf-8") as f:
            json.dump(_activity_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[AUTO-CLEAN] Ошибка сохранения активности: {e}")


def _ensure_settings(cid: str):
    if cid not in settings:
        settings[cid] = {}
    if "auto_clean" not in settings[cid]:
        settings[cid]["auto_clean"] = DEFAULT_AUTO_CLEAN_SETTINGS.copy()
        save_settings(cid)


def update_user_activity(chat_id: int, user_id: int):
    """Обновляет счётчик сообщений и время последней активности пользователя."""
    cid = str(chat_id)
    uid = str(user_id)
    now = int(time.time())
    chat_activity = _activity_data.setdefault(cid, {})
    user_activity = chat_activity.setdefault(uid, {"count": 0, "last": 0})
    user_activity["count"] += 1
    user_activity["last"] = now


class AutoCleanMiddleware(BaseMiddleware):
    """Middleware: трекает активность пользователей для авточистки."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat and event.chat.type in ("group", "supergroup"):
            if event.from_user and not event.from_user.is_bot:
                update_user_activity(event.chat.id, event.from_user.id)
        return await handler(event, data)


# ─── Команда !авточистка ────────────────────────────────────────────────────

@router.message(F.text.startswith(("!авточистка", ".авточистка")))
async def handle_auto_clean_command(message: Message):
    logger.info(f"[AUTO-CLEAN-CMD] Получена команда: {message.text!r} от user={message.from_user.id} в chat={message.chat.id}")
    if message.chat.type not in ("group", "supergroup"):
        return await message.reply("Команда работает только в группах.")

    chat_id = message.chat.id
    cid = str(chat_id)
    user_id = message.from_user.id

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status not in ("administrator", "creator"):
            return await message.reply("Только админ может настраивать авточистку.")
    except Exception:
        return await message.reply("Ошибка проверки прав.")

    _ensure_settings(cid)
    cfg = settings[cid]["auto_clean"]

    args = message.text.strip().split()[1:]
    if not args:
        status = "ВКЛ" if cfg["enabled"] else "ВЫКЛ"
        text = (
            f"Авточистка неактивных: {status}\n"
            f"Норма: {cfg['min_messages']} сообщений за {cfg['inactive_days']} дней\n\n"
            f"Команды:\n"
            f"!авточистка вкл/выкл\n"
            f"!авточистка норма <дней> <сообщений>\n"
            f"Пример: !авточистка норма 7 100"
        )
        return await message.reply(text)

    cmd = args[0].lower()

    if cmd in ("вкл", "выкл"):
        want = cmd == "вкл"
        cfg["enabled"] = want
        save_settings(cid)
        return await message.reply(f"Авточистка {'включена' if want else 'выключена'}.")

    if cmd == "норма" and len(args) >= 3:
        try:
            days = int(args[1])
            msgs = int(args[2])
            if days < 1 or msgs < 1:
                return await message.reply("Оба числа должны быть >= 1.")
            cfg["inactive_days"] = days
            cfg["min_messages"] = msgs
            save_settings(cid)
            return await message.reply(f"Норма установлена: {msgs} сообщений за {days} дней.")
        except ValueError:
            return await message.reply("Используй: !авточистка норма <дней> <сообщений>")

    return await message.reply("Неизвестная команда. Используй: !авточистка")


# ─── Фоновая задача ─────────────────────────────────────────────────────────

async def _run_auto_clean():
    """Каждый час проверяет и банит неактивных пользователей."""
    while True:
        try:
            await asyncio.sleep(3600)
            now = int(time.time())
            for cid, chat_activity in list(_activity_data.items()):
                if cid not in settings:
                    continue
                cfg = settings[cid].get("auto_clean", {})
                if not cfg.get("enabled", False):
                    continue

                inactive_seconds = cfg.get("inactive_days", 7) * 86400
                min_messages = cfg.get("min_messages", 100)
                chat_id = int(cid)

                for uid_str, user_activity in list(chat_activity.items()):
                    user_id = int(uid_str)
                    last_active = user_activity.get("last", 0)
                    msg_count = user_activity.get("count", 0)

                    # Баним если неактивен дольше порога И написал меньше сообщений
                    if now - last_active > inactive_seconds and msg_count < min_messages:
                        try:
                            await bot.ban_chat_member(chat_id, user_id)
                            logger.warning(
                                f"[AUTO-CLEAN] Забанен user={user_id} в chat={chat_id} "
                                f"(сообщений: {msg_count}, неактивен: {(now - last_active) // 86400}д)"
                            )
                        except Exception as e:
                            logger.error(f"[AUTO-CLEAN] Ошибка бана {user_id} в {chat_id}: {e}")
                        finally:
                            chat_activity.pop(uid_str, None)

                _save_activity()
        except Exception as e:
            logger.error(f"[AUTO-CLEAN] Ошибка в фоновой задаче: {e}")
            await asyncio.sleep(300)


async def start_auto_clean_task():
    _load_activity()
    asyncio.create_task(_run_auto_clean())
    logger.info("[AUTO-CLEAN] Фоновая задача запущена")
