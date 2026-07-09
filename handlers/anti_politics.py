import logging
from aiogram import Router, F
from aiogram.types import Message

from ..storage.state import settings, save_settings
from ..core.utils import is_admin

logger = logging.getLogger(__name__)
router = Router()

# Ключевые слова для политики
POLITICS_KEYWORDS = [
    "политика", "политик", "выборы", "президент", "правительство",
    "депутат", "парламент", "госдума", "министр", "партия",
    "election", "politics", "government", "president",
    "война", "ukraine", "russia", "украина", "россия",
    "путин", "зеленский", "biden", "trump", "bidеn"
]


async def check_politics(message: Message) -> bool:
    """Проверяет сообщение на обсуждение политики."""
    if not message.text:
        return False
    
    text_lower = message.text.lower()
    for keyword in POLITICS_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


@router.message(F.text, ~F.text.startswith(("!", ".", "/")), F.chat.type.in_(("group", "supergroup")))
async def handle_politics(message: Message):
    """Обработка политики в группах."""
    chat_id_str = str(message.chat.id)
    
    # Проверяем включена ли защита
    if chat_id_str not in settings:
        return
    
    anti_pol = settings[chat_id_str].get("anti_politics", {})
    if not anti_pol.get("enabled"):
        return
    
    # Админы игнорируются
    if await is_admin(message):
        return
    
    # Проверяем на политику
    if await check_politics(message):
        punishment = anti_pol.get("punishment", "delete")
        
        if punishment == "delete":
            try:
                await message.delete()
                logger.info(f"[ANTI_POL] Удалено политическое сообщение от {message.from_user.id} в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_POL] Ошибка удаления сообщения: {e}")
        
        elif punishment == "mute":
            duration = anti_pol.get("duration", 30)
            unit = anti_pol.get("unit", "мин")
            
            try:
                from ..core.utils import get_duration_seconds
                mute_seconds = get_duration_seconds(duration, unit)
                from datetime import timedelta, datetime
                until_date = datetime.now() + timedelta(seconds=mute_seconds)
                
                await message.chat.restrict(
                    user_id=message.from_user.id,
                    until_date=until_date,
                    can_send_messages=False
                )
                await message.delete()
                logger.info(f"[ANTI_POL] Замучен {message.from_user.id} за политику в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_POL] Ошибка мута: {e}")
        
        elif punishment == "ban":
            try:
                await message.chat.ban(message.from_user.id)
                await message.delete()
                logger.info(f"[ANTI_POL] Забанен {message.from_user.id} за политику в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_POL] Ошибка бана: {e}")


@router.message(F.text.startswith(("!антиполитика", ".антиполитика")))
async def configure_anti_politics(message: Message):
    """Команда настройки защиты от политики."""
    if not await is_admin(message):
        return await message.reply("❗ Только администратор может управлять настройками.")
    
    chat_id_str = str(message.chat.id)
    settings.setdefault(chat_id_str, {})
    
    if "anti_politics" not in settings[chat_id_str]:
        settings[chat_id_str]["anti_politics"] = {
            "enabled": False,
            "punishment": "delete",
            "duration": 30,
            "unit": "мин"
        }
    
    parts = message.text.strip().split()
    
    if len(parts) == 1:
        # Показать статус
        anti_pol = settings[chat_id_str]["anti_politics"]
        status = "✅ Включена" if anti_pol.get("enabled") else "❌ Выключена"
        punishment = anti_pol.get("punishment", "delete")
        return await message.reply(
            f"🏛️ **Защита от политики:** {status}\n"
            f"🔨 Наказание: {punishment}\n\n"
            f"Использование:\n"
            f"!антиполитика вкл — включить\n"
            f"!антиполитика выкл — выключить\n"
            f"!антиполитика наказание [delete|mute|ban]",
            parse_mode="Markdown"
        )
    
    action = parts[1].lower()
    
    if action in ("вкл", "on", "enable"):
        settings[chat_id_str]["anti_politics"]["enabled"] = True
        save_settings(chat_id_str)
        return await message.reply("✅ Защита от политики включена")
    
    elif action in ("выкл", "off", "disable"):
        settings[chat_id_str]["anti_politics"]["enabled"] = False
        save_settings(chat_id_str)
        return await message.reply("❌ Защита от политики выключена")
    
    elif action == "наказание":
        if len(parts) >= 3:
            punishment = parts[2].lower()
            if punishment in ("delete", "удалить"):
                settings[chat_id_str]["anti_politics"]["punishment"] = "delete"
            elif punishment in ("mute", "мут"):
                settings[chat_id_str]["anti_politics"]["punishment"] = "mute"
            elif punishment in ("ban", "бан"):
                settings[chat_id_str]["anti_politics"]["punishment"] = "ban"
            else:
                return await message.reply("❌ Неверное наказание. Доступно: delete, mute, ban")
            save_settings(chat_id_str)
            return await message.reply(f"✅ Наказание установлено: {punishment}")
        else:
            return await message.reply("❌ Укажите наказание: delete, mute или ban")
    
    else:
        return await message.reply("❌ Неверная команда. Используйте: вкл, выкл, наказание")
