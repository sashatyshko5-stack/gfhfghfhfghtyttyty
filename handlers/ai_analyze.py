
import asyncio
import logging
import tempfile
import time
from typing import Dict

import aiohttp
import cv2

from aiogram import Router, F
from aiogram.types import Message

from ..core.loader import bot
from ..services.huggingface_client import hf_client
from ..storage.state import settings, save_settings
from ..core.utils import is_admin

logger = logging.getLogger(__name__)
router = Router()

waiting_for_image: Dict[tuple, dict] = {}
WAIT_TIMEOUT = 30


async def cleanup_expired_waits():
    current_time = time.time()
    expired = [key for key, data in waiting_for_image.items() if current_time - data["timestamp"] > WAIT_TIMEOUT]
    for key in expired:
        del waiting_for_image[key]


@router.message(F.text.regexp(r"^[!\.]ии(\s.*)?$"), F.chat.type.in_(("group", "supergroup")))
async def toggle_ai_chat(message: Message):
    """Включение/выключение общения ИИ в чате (per-chat, сохраняется в settings.json)."""
    if not await is_admin(message):
        return await message.reply("❗ Только администратор может управлять настройками.")

    chat_id = message.chat.id
    chat_id_str = str(chat_id)
    settings.setdefault(chat_id_str, {})
    # На всякий случай — у некоторых хендлеров есть ветка с int-ключом
    if chat_id in settings and isinstance(settings[chat_id], dict):
        pass

    if "ai_enabled" not in settings[chat_id_str]:
        settings[chat_id_str]["ai_enabled"] = True

    parts = message.text.strip().split()

    if len(parts) == 1:
        status = "✅ Включено" if settings[chat_id_str]["ai_enabled"] else "❌ Выключено"
        return await message.reply(
            f"🤖 **Общение с ИИ:** {status}\n\n"
            f"Использование:\n"
            f"!ии вкл — включить\n"
            f"!ии выкл — выключить",
            parse_mode="Markdown"
        )

    action = parts[1].lower()

    if action in ("вкл", "on", "enable"):
        settings[chat_id_str]["ai_enabled"] = True
        # синхронизация на случай, если код где-то держит int-ключ
        if chat_id in settings and isinstance(settings[chat_id], dict):
            settings[chat_id]["ai_enabled"] = True
        save_settings(chat_id_str)
        return await message.reply("✅ Общение с ИИ включено")

    elif action in ("выкл", "off", "disable"):
        settings[chat_id_str]["ai_enabled"] = False
        if chat_id in settings and isinstance(settings[chat_id], dict):
            settings[chat_id]["ai_enabled"] = False
        save_settings(chat_id_str)
        return await message.reply("❌ Общение с ИИ выключено")

    else:
        return await message.reply("❌ Неверная команда. Используйте: вкл, выкл")


@router.message(F.text.in_({"!иианализ", ".иианализ"}))
async def ai_analyze_command(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    await cleanup_expired_waits()
    
    key = (chat_id, user_id)
    waiting_for_image[key] = {"timestamp": time.time(), "message_id": message.message_id}
    
    reply = await message.reply(
        "🖼 **Ожидаю изображение для анализа**\n\n"
        "Отправьте фото, гифку или стикер в течение 30 секунд.\n"
        "ИИ опишет что на изображении.",
        parse_mode="Markdown"
    )
    
    logger.info(f"[AI_ANALYZE] Пользователь {user_id} запросил анализ в чате {chat_id}")
    
    async def timeout_cleanup():
        await asyncio.sleep(WAIT_TIMEOUT)
        if key in waiting_for_image:
            del waiting_for_image[key]
            try:
                await reply.edit_text("⏱ Время ожидания изображения истекло.")
            except Exception:
                pass
    
    asyncio.create_task(timeout_cleanup())


# ИСПРАВЛЕНО: Проверяем ТОЛЬКО если пользователь в режиме ожидания
async def check_waiting_for_image(message: Message) -> bool:
    """Фильтр - проверяет есть ли активное ожидание для этого пользователя"""
    if not message.from_user:
        return False
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    key = (chat_id, user_id)
    
    if key not in waiting_for_image:
        return False
    
    data = waiting_for_image[key]
    if time.time() - data["timestamp"] > WAIT_TIMEOUT:
        del waiting_for_image[key]
        return False
    
    return True


@router.message(F.photo | F.animation | F.sticker | F.video, check_waiting_for_image)
async def handle_media_for_analyze(message: Message):
    """Обработка медиа для анализа если пользователь в режиме ожидания"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    key = (chat_id, user_id)
    
    # Удаляем из списка ожидания
    del waiting_for_image[key]
    
    logger.info(f"[AI_ANALYZE] Получено изображение от {user_id} в чате {chat_id}")
    processing_msg = await message.reply("🔍 Анализирую изображение через Hugging Face...")
    
    try:
        file_id = None
        media_type = "изображение"
        use_thumbnail = False
        
        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "фото"
        elif message.animation:
            file_id = message.animation.file_id
            media_type = "гифку"
        elif message.sticker:
            if getattr(message.sticker, "is_animated", False):
                thumb = getattr(message.sticker, "thumbnail", None)
                if thumb:
                    file_id = thumb.file_id
                    use_thumbnail = True
                else:
                    await processing_msg.edit_text("❌ Не удалось получить изображение.")
                    return
            else:
                file_id = message.sticker.file_id
            media_type = "стикер"
        elif message.video:
            file_id = message.video.file_id
            media_type = "видео"
        
        if not file_id:
            await processing_msg.edit_text("❌ Не удалось получить файл.")
            return
        
        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    await processing_msg.edit_text("❌ Не удалось скачать файл.")
                    return
                file_bytes = await resp.read()
        
        logger.info(f"[AI_ANALYZE] Скачан файл размером {len(file_bytes)} байт")
        
        # Извлекаем кадр для видео/анимаций
        if not use_thumbnail and (
            (message.sticker and getattr(message.sticker, "is_video", False))
            or message.animation or message.video
        ):
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                cap = cv2.VideoCapture(tmp_path)
                ret, frame = cap.read()
                cap.release()
                if not ret:
                    await processing_msg.edit_text("❌ Не удалось извлечь кадр.")
                    return
                resized = cv2.resize(frame, (640, 360))
                _, buffer = cv2.imencode(".jpg", resized)
                file_bytes = buffer.tobytes()
            except Exception as e:
                logger.error(f"[AI_ANALYZE] Ошибка кадра: {e}")
                await processing_msg.edit_text("❌ Ошибка обработки.")
                return
        
        # Анализ через Hugging Face
        description = await hf_client.analyze_image(file_bytes)
        
        if not description:
            await processing_msg.edit_text("❌ Не удалось проанализировать изображение.")
            return
        
        logger.info(f"[AI_ANALYZE] Ответ HF: {description}")
        
        await processing_msg.edit_text(
            f"🖼 **Анализ {media_type}**\n\n{description}",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.exception(f"[AI_ANALYZE] Ошибка: {e}")
        await processing_msg.edit_text(f"❌ Ошибка: {e}")