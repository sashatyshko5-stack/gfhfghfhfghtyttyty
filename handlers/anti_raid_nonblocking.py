import asyncio
import logging
import json
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple
from collections import defaultdict, deque

import aiohttp
from aiogram import Router, F, Bot
from aiogram.types import Message, ChatMemberUpdated, ChatMemberAdministrator, ChatMemberOwner, ContentType
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.exceptions import TelegramAPIError

from ..core.loader import bot
from ..storage.state import settings, save_settings
from ..core.utils import can_bot_restrict_members, is_admin

logger = logging.getLogger(__name__)

# Логируем загрузку модуля
logger.info("[ANTI-RAID] Модуль anti_raid_nonblocking.py загружен")

# Импортируем функции из основного файла
try:
    from .anti_raid import (
        AntiRaidStorage, storage, DEFAULT_SETTINGS, FORBIDDEN_TAGS, LINK_PATTERNS,
        get_anti_raid_settings, save_anti_raid_settings, normalize_username,
        has_forbidden_tags, has_links, log_test_violation,
        handle_test_violation, analyze_media_with_ai, notify_admins, handle_raid, ban_user
    )
    logger.info("[ANTI-RAID] Импорт функций из anti_raid.py успешен")
except ImportError as e:
    logger.error(f"[ANTI-RAID] Ошибка импорта из anti_raid.py: {e}")
    # Определяем заглушки если импорт не удался
    def get_anti_raid_settings(chat_id):
        return {}
    def has_forbidden_tags(username, first_name):
        return False
    def has_links(text):
        return False
    async def analyze_media_with_ai(message, settings):
        return None
    async def handle_test_violation(message, violation_type, details, would_ban=False):
        pass
    async def ban_user(bot, chat_id, user_id, reason):
        pass
    async def is_admin(message):
        return False

# Создаем неконфликтующий роутер
nonblocking_router = Router()
logger.info("[ANTI-RAID] Роутер nonblocking_router создан")

@nonblocking_router.message(F.content_types.in_({'text', 'photo', 'sticker', 'animation'}))
async def handle_message_nonblocking(message: Message):
    """Неконфликтующий обработчик - только логирует, не блокирует"""
    # Работаем только в группах
    if message.chat.type not in ['group', 'supergroup']:
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Логируем входящее сообщение
    logger.info(f"[ANTI-RAID] ПРОВЕРКА: {message.content_type} от {user_id} (админ: {await is_admin(message)})")
    
    # Получаем настройки
    anti_raid_settings = get_anti_raid_settings(chat_id)
    
    # Если анти-рейд выключен - просто выходим
    if not anti_raid_settings.get("enabled", False):
        logger.info(f"[ANTI-RAID] выключен, пропускаем")
        return
    
    # Проверка админов и режима теста
    is_admin_user = await is_admin(message)
    test_mode_enabled = anti_raid_settings.get("test_mode", False)
    
    logger.info(f"[ANTI-RAID] настройки: admin={is_admin_user}, test_mode={test_mode_enabled}")
    
    # Пропускаем админов если режим теста выключен
    if is_admin_user and not test_mode_enabled:
        logger.info(f"[ANTI-RAID] админ пропущен")
        return
    
    # Логируем если админ в режиме теста
    if is_admin_user and test_mode_enabled:
        logger.info(f"[ANTI-RAID] ТЕСТ: админ {user_id} - проверка включена")
    
    # === ПРОВЕРКИ НАРУШЕНИЙ (только логирование) ===
    
    violation_found = False
    violation_type = ""
    violation_details = ""
    
    # Проверка тегов в нике
    if anti_raid_settings.get("ban_for_tags", True):
        username = message.from_user.username or ""
        first_name = message.from_user.first_name or ""
        
        if has_forbidden_tags(username, first_name):
            violation_found = True
            violation_type = "ЗАПРЕЩЕННЫЙ ТЕГ"
            violation_details = "Обнаружен запрещенный тег в нике"
    
    # Проверка ссылок
    if not violation_found and anti_raid_settings.get("delete_links", True) and message.text:
        if has_links(message.text):
            violation_found = True
            violation_type = "ССЫЛКИ"
            violation_details = "Обнаружены ссылки в сообщении"
    
    # AI анализ медиафайлов
    if not violation_found and anti_raid_settings.get("analyze_photos", False):
        if message.photo or message.sticker or message.animation:
            try:
                result = await analyze_media_with_ai(message, anti_raid_settings)
                
                if result:
                    label = result.get("label", "")
                    score = result.get("score", 0)
                    reason = result.get("reason", "AI analysis")
                    media_type = "фото" if message.photo else ("стикер" if message.sticker else "GIF")
                    
                    # Проверяем на запрещенный контент
                    if label in ["raid", "spam", "inappropriate"] and score > 0.7:
                        violation_found = True
                        violation_type = "AI АНАЛИЗ"
                        violation_details = f"{label.upper()} контент на {media_type}: {reason}"
                    else:
                        logger.info(f"[ANTI-RAID] {media_type.capitalize()} безопасен: {label} (score: {score:.2f})")
                        
            except Exception as e:
                logger.error(f"[ANTI-RAID] Ошибка анализа медиафайла: {e}")
    
    # Обрабатываем нарушение
    if violation_found:
        if is_admin_user and test_mode_enabled:
            # Тестовый режим для админов
            await handle_test_violation(message, violation_type, violation_details, would_ban=True)
        else:
            # Обычный режим - бан за теги и AI raid, лог для остальных
            if violation_type == "ЗАПРЕЩЕННЫЙ ТЕГ":
                await ban_user(bot, message.chat.id, user_id, "Запрещенный тег в нике")
            elif violation_type == "AI АНАЛИЗ" and "рейд" in violation_details.lower():
                await ban_user(bot, message.chat.id, user_id, "AI: Рейд-контент")
            else:
                logger.warning(f"[ANTI-RAID] НАРУШЕНИЕ: Пользователь {user_id} - {violation_type}: {violation_details}")
    else:
        logger.info(f"[ANTI-RAID] Сообщение от {user_id} проверено, нарушений нет")
