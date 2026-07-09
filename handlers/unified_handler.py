import asyncio
import logging
import re
from typing import Dict, List, Optional

from aiogram import Router, F, Bot
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import BaseFilter
from aiogram.types import Message, ContentType
from aiogram.exceptions import TelegramAPIError

from ..core.loader import bot
from ..storage.state import settings
from ..core.utils import is_admin

# Импортируем только нужные функции
from .anti_raid import get_anti_raid_settings, has_links, has_forbidden_tags, ban_user

from .anti_link_leak import check_invite_link_in_message
from .antispam import antispam_check
from .anti_nsfw import nsfw_scan

logger = logging.getLogger(__name__)

# КАСТОМНЫЙ ФИЛЬТР-ГАЙТКИПЕР ДЛЯ ЗАЩИТ
class ProtectionGateFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        # Пропускаем команды - они обрабатываются в commands_router
        if message.text and (message.text.startswith('/') or message.text.startswith('!')):
            return False
        
        # Пропускаем сообщения с новыми участниками - их обрабатывает new_members_router
        if message.new_chat_members:
            return False
        
        # Работаем только в группах
        if message.chat.type not in ['group', 'supergroup']:
            return False
        
        # Пропускаем реплаи боту - это для AI (только для текстовых сообщений)
        if (message.reply_to_message and 
            message.reply_to_message.from_user.id == (await bot.get_me()).id and
            message.content_type == 'text'):
            return False
        
        return True

# ЕДИНЫЙ РОУТЕР ДЛЯ ВСЕХ ФУНКЦИЙ ЗАЩИТЫ
unified_router = Router()

@unified_router.message(ProtectionGateFilter())
async def handle_protections_unified(message: Message):
    """Единый обработчик всех защит - ТОЛЬКО ЗАЩИТЫ, БЕЗ КОНФЛИКТОВ"""
    
    chat_id = message.chat.id
    chat_id_str = str(chat_id)
    user_id = message.from_user.id
    is_admin_user = await is_admin(message)
    
    logger.info(f"[PROTECTIONS] ===== НАЧАЛО ОБРАБОТКИ =====")
    logger.info(f"[PROTECTIONS] Сообщение: {message.content_type} от user={user_id}, chat={chat_id}, admin={is_admin_user}")
    logger.info(f"[PROTECTIONS] Текст: {message.text[:50] if message.text else 'нет текста'}...")
    
    # Получаем все настройки
    chat_settings = settings.get(chat_id_str, {})
    logger.info(f"[PROTECTIONS] Все настройки чата: {chat_settings}")
    
    anti_raid_settings = chat_settings.get('anti_raid', {})
    antispam_settings = chat_settings.get('antispam', {})
    
    antinsfw_settings = chat_settings.get('antinsfw', {})
    
    logger.info(f"[PROTECTIONS] anti_raid: enabled={anti_raid_settings.get('enabled')}, test_mode={anti_raid_settings.get('test_mode')}")
    logger.info(f"[PROTECTIONS] antispam: enabled={antispam_settings.get('enabled')}")
    logger.info(f"[PROTECTIONS] antinsfw: enabled={antinsfw_settings.get('enabled')}")
    
    # 1. АНТИ-РЕЙД
    logger.info(f"[PROTECTIONS] >>> Запуск анти-рейд проверки...")
    await handle_anti_raid(message, anti_raid_settings, is_admin_user)
    
    # 2. АНТИ-СПАМ
    logger.info(f"[PROTECTIONS] >>> Запуск анти-спам проверки...")
    await handle_antispam(message, antispam_settings, is_admin_user)
    
    # 3. АНТИ-ЛИВ
   
    
    # 4. АНТИ-18+
    logger.info(f"[PROTECTIONS] >>> Запуск анти-18+ проверки...")
    await handle_antinsfw(message, antinsfw_settings, is_admin_user)
    
    logger.info(f"[PROTECTIONS] ===== КОНЕЦ ОБРАБОТКИ =====")
    # Иначе aiogram считает апдейт «обработанным» и не доходит до media_react / остальных роутеров.
    return UNHANDLED

async def handle_anti_raid(message: Message, settings: Dict, is_admin: bool):
    """Обработка анти-рейда с AI анализом медиа"""
    try:
        if not settings.get("enabled", False):
            logger.info(f"[ANTI-RAID] Отключен, пропускаем")
            return
        
        test_mode = settings.get("test_mode", False)
        analyze_photos = settings.get("analyze_photos", True)
        
        logger.info(f"[ANTI-RAID] Проверка: admin={is_admin}, test_mode={test_mode}, analyze_photos={analyze_photos}")
        
        # Пропускаем админов если не тестовый режим
        if is_admin and not test_mode:
            logger.info(f"[ANTI-RAID] Админ вне тестового режима, пропускаем")
            return
        
        violations = []
        
        # Проверка ссылок (только для текста)
        if message.text and settings.get("delete_links", True):
            if has_links(message.text):
                violations.append("ССЫЛКИ")
                logger.warning(f"[ANTI-RAID] Нарушение: ссылки в сообщении")
        
        # Проверка тегов
        if settings.get("ban_for_tags", True):
            username = message.from_user.username or ""
            first_name = message.from_user.first_name or ""
            if has_forbidden_tags(username, first_name):
                violations.append("ЗАПРЕЩЕННЫЙ ТЕГ")
                logger.warning(f"[ANTI-RAID] Нарушение: запрещенный тег")
        
        # AI анализ медиа (фото, стикеры, GIF)
        if analyze_photos and message.content_type in ['photo', 'sticker', 'animation']:
            logger.info(f"[ANTI-RAID] Запуск AI анализа для {message.content_type}...")
            
            # Импортируем функцию анализа
            from .anti_raid import analyze_media_with_ai, storage
            
            # Проверяем режим "тест ии"
            chat_id = message.chat.id
            ai_test_mode = False
            if hasattr(storage, 'test_mode') and chat_id in storage.test_mode:
                ai_test_mode = storage.test_mode[chat_id].get("active", False)
                logger.info(f"[ANTI-RAID] Режим ТЕСТ ИИ активен для чата {chat_id}")
            
            try:
                ai_result = await analyze_media_with_ai(message, settings)
                
                if ai_result:
                    label = ai_result.get("label", "unknown")
                    score = ai_result.get("score", 0)
                    reason = ai_result.get("reason", "")
                    
                    logger.info(f"[ANTI-RAID] AI результат: label={label}, score={score}, reason={reason}")
                    
                    # Режим тест ИИ - всегда показываем результат
                    if ai_test_mode:
                        result_text = (
                            f"🤖 **ТЕСТ AI АНАЛИЗ**\n\n"
                            f"**Тип контента:** {message.content_type}\n"
                            f"**Результат:** {label.upper()}\n"
                            f"**Уверенность:** {score:.0%}\n"
                            f"**Причина:** {reason}\n\n"
                            f"_В обычном режиме: {'удаление/бан' if label in ['raid', 'spam', 'inappropriate'] else 'пропуск'}_"
                        )
                        try:
                            await message.reply(result_text, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"[ANTI-RAID] Ошибка отправки результата теста: {e}")
                        
                        # Очищаем режим теста после использования
                        if chat_id in storage.test_mode:
                            del storage.test_mode[chat_id]
                            logger.info(f"[ANTI-RAID] Режим ТЕСТ ИИ отключен после проверки")
                    
                    # Проверяем на нарушение
                    if label in ["raid", "spam", "inappropriate"]:
                        violations.append(f"AI: {label.upper()} ({score:.0%})")
                        logger.warning(f"[ANTI-RAID] AI обнаружил нарушение: {label}")
                else:
                    logger.warning(f"[ANTI-RAID] AI не вернул результат")
                    
                    # В режиме тест ИИ сообщаем об ошибке
                    if ai_test_mode:
                        await message.reply("❌ AI не смог проанализировать медиа. Проверьте логи.")
                        if chat_id in storage.test_mode:
                            del storage.test_mode[chat_id]
                            
            except Exception as e:
                logger.error(f"[ANTI-RAID] Ошибка AI анализа: {e}")
                if ai_test_mode:
                    await message.reply(f"❌ Ошибка AI анализа: {e}")
        
        # Обработка нарушений
        if violations:
            if test_mode and is_admin:
                # Тестовый режим для админов - только уведомления
                violations_text = "\n".join([f"• {v}" for v in violations])
                try:
                    await message.reply(f"⚠️ **ТЕСТ НАРУШЕНИЯ:**\n{violations_text}")
                except Exception as e:
                    logger.error(f"[ANTI-RAID] Ошибка отправки уведомления: {e}")
            else:
                # Обычный режим - применяем действия
                chat_id = message.chat.id
                user_id = message.from_user.id
                for violation in violations:
                    logger.warning(f"[ANTI-RAID] НАРУШЕНИЕ: {violation} от пользователя {user_id}")

                # Бан за запрещенные теги или AI raid
                should_ban = any("ЗАПРЕЩЕННЫЙ ТЕГ" in v or "AI: RAID" in v for v in violations)
                if should_ban:
                    if any("ЗАПРЕЩЕННЫЙ ТЕГ" in v for v in violations):
                        await ban_user(bot, chat_id, user_id, "Запрещенный тег в нике")
                    else:
                        await ban_user(bot, chat_id, user_id, "AI: Рейд-контент обнаружен")
                    try:
                        await message.delete()
                        logger.info(f"[ANTI-RAID] Сообщение удалено после бана")
                    except Exception as e:
                        logger.error(f"[ANTI-RAID] Не удалось удалить сообщение: {e}")
                else:
                    try:
                        await message.delete()
                        logger.info(f"[ANTI-RAID] Сообщение удалено")
                    except Exception as e:
                        logger.error(f"[ANTI-RAID] Не удалось удалить сообщение: {e}")
        else:
            logger.info(f"[ANTI-RAID] Нарушений нет")
        
    except Exception as e:
        logger.error(f"[ANTI-RAID] Ошибка: {e}")

async def handle_antispam(message: Message, settings: Dict, is_admin: bool):
    """Обработка анти-спама"""
    try:
        logger.debug(f"[ANTISPAM] enabled={settings.get('enabled')} test={settings.get('test_mode')} admin={is_admin}")
        
        if not settings.get("enabled", False):
            logger.info(f"[ANTISPAM] Отключен, пропускаем")
            return
        
        test_mode = settings.get("test_mode", False)
        
        # Пропускаем админов если не тестовый режим
        if is_admin and not test_mode:
            logger.info(f"[ANTISPAM] Админ вне тестового режима, пропускаем")
            return
        
        # Запускаем проверку анти-спама
        logger.info(f"[ANTISPAM] Запуск проверки для {message.from_user.id}, test_mode={test_mode}")
        result = await antispam_check(message, test_mode=test_mode, is_admin=is_admin)
        logger.info(f"[ANTISPAM] Результат проверки: {result}")
        
    except Exception as e:
        logger.error(f"[ANTISPAM] Ошибка: {e}")




async def handle_antinsfw(message: Message, settings: Dict, is_admin: bool):
    """Обработка анти-18+"""
    try:
        logger.info(f"[ANTINSFW] Проверка: enabled={settings.get('enabled')} admin={is_admin} type={message.content_type}")
        
        if not settings.get("enabled", False):
            logger.info(f"[ANTINSFW] Отключен, пропускаем")
            return
        
        # Пропускаем админов
        if is_admin:
            logger.info(f"[ANTINSFW] Админ, пропускаем")
            return
        
        # Для фото, стикеров, анимаций, видео, документов
        if message.content_type in ['photo', 'sticker', 'animation', 'video', 'document']:
            logger.info(f"[ANTINSFW] Запуск NSFW scan для {message.content_type} от {message.from_user.id}")
            
            # ИСПРАВЛЕНО: await вместо create_task чтобы видеть ошибки
            try:
                await nsfw_scan(message)
            except Exception as e:
                logger.error(f"[ANTINSFW] Ошибка в nsfw_scan: {e}")
        else:
            logger.info(f"[ANTINSFW] Пропуск - тип контента: {message.content_type}")
        
    except Exception as e:
        logger.error(f"[ANTINSFW] Ошибка: {e}")