import logging
from aiogram import Router, F
from aiogram.types import Message

from ..storage.state import settings, save_settings
from ..core.utils import is_admin

logger = logging.getLogger(__name__)
router = Router()

# Ключевые слова для рекламы
AD_KEYWORDS = [
    "купи", "продай", "скидка", "акция", "промокод", "бесплатно",
    "заработок", "заработай", "инвестиции", "крипта", "биткоин",
    "casino", "казино", "ставки", "букмекер", "прогноз", "сигналы",
    "подписывайся", "подпишись", "канал", "группа", "ссылка",
    "t.me/", "joinchat", "boosty", "donationalerts", "donatello"
]


async def check_advertising(message: Message) -> bool:
    """Проверяет сообщение на наличие рекламы."""
    if not message.text:
        return False
    
    text_lower = message.text.lower()
    for keyword in AD_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


@router.message(F.text, ~F.text.startswith(("!", ".", "/")), F.chat.type.in_(("group", "supergroup")))
async def handle_advertising(message: Message):
    """Обработка рекламы в группах."""
    chat_id_str = str(message.chat.id)
    
    # Проверяем включена ли защита
    if chat_id_str not in settings:
        return
    
    anti_ad = settings[chat_id_str].get("anti_advertising", {})
    if not anti_ad.get("enabled"):
        return
    
    # Админы игнорируются
    if await is_admin(message):
        return
    
    # Проверяем на рекламу
    if await check_advertising(message):
        punishment = anti_ad.get("punishment", "delete")
        
        if punishment == "delete":
            try:
                await message.delete()
                logger.info(f"[ANTI_AD] Удалено рекламное сообщение от {message.from_user.id} в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_AD] Ошибка удаления сообщения: {e}")
        
        elif punishment == "mute":
            duration = anti_ad.get("duration", 30)
            unit = anti_ad.get("unit", "мин")
            
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
                logger.info(f"[ANTI_AD] Замучен {message.from_user.id} за рекламу в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_AD] Ошибка мута: {e}")
        
        elif punishment == "ban":
            try:
                await message.chat.ban(message.from_user.id)
                await message.delete()
                logger.info(f"[ANTI_AD] Забанен {message.from_user.id} за рекламу в чате {chat_id_str}")
            except Exception as e:
                logger.error(f"[ANTI_AD] Ошибка бана: {e}")


@router.message(F.text.startswith(("!антиреклама", ".антиреклама")))
async def configure_anti_advertising(message: Message):
    """Команда настройки защиты от рекламы."""
    if not await is_admin(message):
        return await message.reply("❗ Только администратор может управлять настройками.")
    
    chat_id_str = str(message.chat.id)
    settings.setdefault(chat_id_str, {})
    
    if "anti_advertising" not in settings[chat_id_str]:
        settings[chat_id_str]["anti_advertising"] = {
            "enabled": False,
            "punishment": "delete",
            "duration": 30,
            "unit": "мин"
        }
    
    parts = message.text.strip().split()
    
    if len(parts) == 1:
        # Показать статус
        anti_ad = settings[chat_id_str]["anti_advertising"]
        status = "✅ Включена" if anti_ad.get("enabled") else "❌ Выключена"
        punishment = anti_ad.get("punishment", "delete")
        return await message.reply(
            f"📢 **Защита от рекламы:** {status}\n"
            f"🔨 Наказание: {punishment}\n\n"
            f"Использование:\n"
            f"!антиреклама вкл — включить\n"
            f"!антиреклама выкл — выключить\n"
            f"!антиреклама наказание [delete|mute|ban]",
            parse_mode="Markdown"
        )
    
    action = parts[1].lower()
    
    if action in ("вкл", "on", "enable"):
        settings[chat_id_str]["anti_advertising"]["enabled"] = True
        save_settings(chat_id_str)
        return await message.reply("✅ Защита от рекламы включена")
    
    elif action in ("выкл", "off", "disable"):
        settings[chat_id_str]["anti_advertising"]["enabled"] = False
        save_settings(chat_id_str)
        return await message.reply("❌ Защита от рекламы выключена")
    
    elif action == "наказание":
        if len(parts) >= 3:
            punishment = parts[2].lower()
            if punishment in ("delete", "удалить"):
                settings[chat_id_str]["anti_advertising"]["punishment"] = "delete"
            elif punishment in ("mute", "мут"):
                settings[chat_id_str]["anti_advertising"]["punishment"] = "mute"
            elif punishment in ("ban", "бан"):
                settings[chat_id_str]["anti_advertising"]["punishment"] = "ban"
            else:
                return await message.reply("❌ Неверное наказание. Доступно: delete, mute, ban")
            save_settings(chat_id_str)
            return await message.reply(f"✅ Наказание установлено: {punishment}")
        else:
            return await message.reply("❌ Укажите наказание: delete, mute или ban")
    
    else:
        return await message.reply("❌ Неверная команда. Используйте: вкл, выкл, наказание")
