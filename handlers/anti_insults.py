import logging
from aiogram import Router, F
from aiogram.types import Message

from ..storage.state import settings, save_settings
from ..core.utils import is_admin

logger = logging.getLogger(__name__)
router = Router()

# Ключевые слова для оскорблений
INSULTS_KEYWORDS = [
    "идиот", "дурак", "тупой", "дебил", " imbecile", "moron",
    "кретин", "урод", "чмо", "гандон", "пидор", "пидар",
    "лох", "чушпан", "шлюха", "блядь", "бля", "сука",
    "сучка", "ебать", "ёб", "пизда", "хуй", "член",
    "жопа", "жопе", "жопу", "мразь", "сволочь",
    "fuck", "shit", "bitch", "whore", "idiot", "stupid"
]


async def check_insults(message: Message) -> bool:
    """Проверяет сообщение на оскорбления."""
    if not message.text:
        return False
    
    text_lower = message.text.lower()
    for keyword in INSULTS_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


@router.message(F.text, ~F.text.startswith(("!", ".", "/")), F.chat.type.in_(("group", "supergroup")))
async def handle_insults(message: Message):
    """Обработка оскорблений в группах."""
    chat_id_str = str(message.chat.id)
    
    # Проверяем включена ли защита
    if chat_id_str not in settings:
        return
    
    anti_ins = settings[chat_id_str].get("anti_insults", {})
    if not anti_ins.get("enabled"):
        return
    
    # Админы игнорируются
    if await is_admin(message):
        return
    
    # Проверяем на оскорбления
    if await check_insults(message):
        punishment = anti_ins.get("punishment", "delete")
        
        if punishment == "delete":
            try:
                await message.delete()
                logger.info(f"[ANTI_INS] Удалено оскорбление от {message.from_user.id} в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_INS] Ошибка удаления сообщения: {e}")
        
        elif punishment == "mute":
            duration = anti_ins.get("duration", 30)
            unit = anti_ins.get("unit", "мин")
            
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
                logger.info(f"[ANTI_INS] Замучен {message.from_user.id} за оскорбления в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_INS] Ошибка мута: {e}")
        
        elif punishment == "ban":
            try:
                await message.chat.ban(message.from_user.id)
                await message.delete()
                logger.info(f"[ANTI_INS] Забанен {message.from_user.id} за оскорбления в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_INS] Ошибка бана: {e}")


@router.message(F.text.startswith(("!антиоск", ".антиоск")))
async def configure_anti_insults(message: Message):
    """Команда настройки защиты от оскорблений."""
    if not await is_admin(message):
        return await message.reply("❗ Только администратор может управлять настройками.")
    
    chat_id_str = str(message.chat.id)
    settings.setdefault(chat_id_str, {})
    
    if "anti_insults" not in settings[chat_id_str]:
        settings[chat_id_str]["anti_insults"] = {
            "enabled": False,
            "punishment": "delete",
            "duration": 30,
            "unit": "мин"
        }
    
    parts = message.text.strip().split()
    
    if len(parts) == 1:
        # Показать статус
        anti_ins = settings[chat_id_str]["anti_insults"]
        status = "✅ Включена" if anti_ins.get("enabled") else "❌ Выключена"
        punishment = anti_ins.get("punishment", "delete")
        return await message.reply(
            f"🚫 **Защита от оскорблений:** {status}\n"
            f"🔨 Наказание: {punishment}\n\n"
            f"Использование:\n"
            f"!антиоск вкл — включить\n"
            f"!антиоск выкл — выключить\n"
            f"!антиоск наказание [delete|mute|ban]",
            parse_mode="Markdown"
        )
    
    action = parts[1].lower()
    
    if action in ("вкл", "on", "enable"):
        settings[chat_id_str]["anti_insults"]["enabled"] = True
        save_settings(chat_id_str)
        return await message.reply("✅ Защита от оскорблений включена")
    
    elif action in ("выкл", "off", "disable"):
        settings[chat_id_str]["anti_insults"]["enabled"] = False
        save_settings(chat_id_str)
        return await message.reply("❌ Защита от оскорблений выключена")
    
    elif action == "наказание":
        if len(parts) >= 3:
            punishment = parts[2].lower()
            if punishment in ("delete", "удалить"):
                settings[chat_id_str]["anti_insults"]["punishment"] = "delete"
            elif punishment in ("mute", "мут"):
                settings[chat_id_str]["anti_insults"]["punishment"] = "mute"
            elif punishment in ("ban", "бан"):
                settings[chat_id_str]["anti_insults"]["punishment"] = "ban"
            else:
                return await message.reply("❌ Неверное наказание. Доступно: delete, mute, ban")
            save_settings(chat_id_str)
            return await message.reply(f"✅ Наказание установлено: {punishment}")
        else:
            return await message.reply("❌ Укажите наказание: delete, mute или ban")
    
    else:
        return await message.reply("❌ Неверная команда. Используйте: вкл, выкл, наказание")
