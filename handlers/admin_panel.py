import logging
import os
import re
from typing import Dict, List, Tuple
from datetime import datetime
import json

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..storage.state import settings, save_settings
from ..storage.message_logs import get_chat_messages, get_known_chats
from ..core.loader import bot

logger = logging.getLogger(__name__)
router = Router()


# ─── Хранилище сессий для навигации ───────────────────────────────────────
# user_id -> {"chat_id": текущий_чат, "menu": текущее_меню}
_panel_sessions: Dict[int, Dict] = {}


async def get_user_owned_chats(user_id: int) -> List[Tuple[int, str]]:
    """Возвращает список чатов где пользователь является владельцем."""
    owned_chats = []
    for chat_id_str in settings.keys():
        chat_id = int(chat_id_str)
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status == "creator":
                chat = await bot.get_chat(chat_id)
                title = chat.title or f"Чат {chat_id}"
                owned_chats.append((chat_id, title))
        except Exception as e:
            logger.error(f"Ошибка проверки прав в чате {chat_id}: {e}")
    return owned_chats


async def get_chat_title(chat_id: int) -> str:
    """Получает название чата по chat_id."""
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or f"Чат {chat_id}"
    except Exception as e:
        logger.error(f"Ошибка получения названия чата {chat_id}: {e}")
        return f"Чат {chat_id}"


# ─── Главное меню ───────────────────────────────────────────────────────────
async def show_main_menu(user_id: int, message: Message = None, callback: CallbackQuery = None):
    """Показывает главное меню панели управления."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔧 Настройки", callback_data="panel:settings")
    builder.button(text="⚡ Быстрые действия", callback_data="panel:quick_actions")
    builder.button(text="📋 Логи", callback_data="panel:logs")
    builder.button(text="📊 Активность", callback_data="panel:activity")
    builder.button(text="📜 Сообщения", callback_data="panel:recent_messages")
    builder.button(text="🚫 Наказанные", callback_data="panel:punished")
    builder.button(text="🤖 Статус ИИ", callback_data="panel:ai_status")
    builder.button(text="ℹ️ О боте", callback_data="panel:bot_info")
    builder.button(text="🔄 Выбрать чат", callback_data="panel:select_chat")
    builder.adjust(2, 2, 2, 2, 1)

    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")
    if chat_id:
        chat_title = await get_chat_title(chat_id)
        text = (
            f"🛡️ **Панель управления ботом**\n\n"
            f"📱 Текущий чат: {chat_title}\n\n"
            f"Выберите действие:"
        )
    else:
        text = (
            "🛡️ **Панель управления бота**\n\n"
            "⚠️ Чат не выбран. Выберите чат для работы.\n\n"
            f"Выберите действие:"
        )

    if message:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    elif callback:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            await callback.answer()
        except Exception:
            pass


# ─── Выбор чата ─────────────────────────────────────────────────────────────
async def show_chat_selection(user_id: int, callback: CallbackQuery):
    """Показывает список чатов где пользователь владелец."""
    chats = await get_user_owned_chats(user_id)

    if not chats:
        await callback.answer("❌ Вы не являетесь владельцем ни одного чата с ботом", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for chat_id, title in chats[:10]:  # Максимум 10 чатов
        builder.button(text=title[:30], callback_data=f"panel:chat:{chat_id}")

    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    text = "📱 **Выберите чат:**\n\n(только чаты где вы владелец)"

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка показа выбора чата: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ─── Настройки чата ────────────────────────────────────────────────────────
async def show_settings(user_id: int, chat_id: int, callback: CallbackQuery):
    """Показывает настройки выбранного чата."""
    _panel_sessions[user_id] = {"chat_id": chat_id, "menu": "settings"}

    chat_settings = settings.get(str(chat_id), {})
    chat_title = await get_chat_title(chat_id)

    antispam = chat_settings.get("antispam", {})
    anti_raid = chat_settings.get("anti_raid", {})
   
    antinsfw = chat_settings.get("antinsfw", {})
    anti_ad = chat_settings.get("anti_advertising", {})
    anti_pol = chat_settings.get("anti_politics", {})
    anti_ins = chat_settings.get("anti_insults", {})
    ai_enabled = chat_settings.get("ai_enabled", False)

    text = (
        f"⚙️ **Настройки чата:** {chat_title}\n\n"
        f"📊 **Антиспам:** {'✅ Вкл' if antispam.get('enabled') else '❌ Выкл'}\n"
        f"🚨 **Антирейд:** {'✅ Вкл' if anti_raid.get('enabled') else '❌ Выкл'}\n"
        f"📢 **Антиреклама:** {'✅ Вкл' if anti_ad.get('enabled') else '❌ Выкл'}\n"
        f"🏛️ **Антиполитика:** {'✅ Вкл' if anti_pol.get('enabled') else '❌ Выкл'}\n"
        f"🚫 **Антиоскорбления:** {'✅ Вкл' if anti_ins.get('enabled') else '❌ Выкл'}\n"
     
        f"🔞 **NSFW:** {'✅ Вкл' if antinsfw.get('enabled') else '❌ Выкл'}\n"
        f"🤖 **ИИ чат:** {'✅ Вкл' if ai_enabled else '❌ Выкл'}\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка показа настроек: {e}")


# ─── Логи чата ─────────────────────────────────────────────────────────────
async def show_logs(user_id: int, callback: CallbackQuery):
    """Показывает логи выбранного чата."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")

    if not chat_id:
        await callback.answer("❌ Сначала выберите чат", show_alert=True)
        await show_chat_selection(user_id, callback)
        return

    chat_title = await get_chat_title(chat_id)
    logs_dir = "logs"
    
    # Ищем лог-файлы связанные с чатом
    log_files = []
    if os.path.exists(logs_dir):
        for f in os.listdir(logs_dir):
            if f.endswith('.log') and str(chat_id) in f:
                log_files.append(f)

    if not log_files:
        text = f"📋 **Логи чата:** {chat_title}\n\nНет логов для этого чата"
    else:
        text = f"📋 **Логи чата:** {chat_title}\n\nДоступные файлы:\n"
        for log_file in log_files[:10]:
            text += f"• {log_file}\n"

    builder = InlineKeyboardBuilder()
    for log_file in log_files[:10]:
        builder.button(text=f"📄 {log_file[:20]}", callback_data=f"panel:log:{log_file}")

    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text[:4000], reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка показа логов: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)


async def show_log_content(user_id: int, log_file: str, callback: CallbackQuery):
    """Показывает содержимое лог-файла."""
    logs_dir = "logs"
    log_path = os.path.join(logs_dir, log_file)

    if not os.path.exists(log_path):
        await callback.answer("❌ Файл не найден", show_alert=True)
        return

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Показываем последние 50 строк
        content_lines = lines[-50:]
        content = ''.join(content_lines)

        text = f"📄 **{log_file}**\n\n```\n{content[-1500:]}\n```"

        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к логам", callback_data="panel:logs")
        builder.adjust(1)

        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            await callback.answer()
        except Exception:
            text = f"📄 **{log_file}**\n\n```\n{content[-800:]}\n```"
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка чтения лога: {e}")
        await callback.answer("❌ Ошибка чтения", show_alert=True)


# ─── Наказанные ───────────────────────────────────────────────────────────
def parse_punished_log(chat_id: int) -> List[Dict]:
    """Парсит punished.log и возвращает записи для конкретного чата."""
    punish_log_path = os.path.join("logs", "punished.log")
    records = []

    if not os.path.exists(punish_log_path):
        return records

    try:
        with open(punish_log_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Формат: chat=123 user_id=456 username=@user punishment=мут duration=30 мин by=admin reason=spam
                if f"chat={chat_id}" in line:
                    record = {}
                    # Парсим ключ=значение
                    parts = line.split()
                    for part in parts:
                        if '=' in part:
                            key, value = part.split('=', 1)
                            record[key] = value
                    records.append(record)
    except Exception as e:
        logger.error(f"Ошибка чтения punished.log: {e}")

    return records


async def show_punished(user_id: int, callback: CallbackQuery):
    """Показывает список наказанных пользователей для выбранного чата."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")

    if not chat_id:
        await callback.answer("❌ Сначала выберите чат", show_alert=True)
        await show_chat_selection(user_id, callback)
        return

    chat_title = await get_chat_title(chat_id)
    records = parse_punished_log(chat_id)

    if not records:
        text = f"🚫 **Наказанные в чате:** {chat_title}\n\nНет записей"
    else:
        text = f"🚫 **Наказанные в чате:** {chat_title}\n\n"
        # Последние 20 записей
        for record in records[-20:]:
            user_id = record.get('user_id', '?')
            username = record.get('username', '')
            punishment = record.get('punishment', '?')
            duration = record.get('duration', '')
            unit = record.get('unit', '')
            reason = record.get('reason', '')
            by = record.get('by', '')

            user_info = f"@{username}" if username else f"ID: {user_id}"
            duration_str = f" ({duration} {unit})" if duration else ""
            reason_str = f"\n   Причина: {reason}" if reason else ""
            by_str = f"\n   Кем: {by}" if by else ""

            text += f"👤 {user_info} — {punishment}{duration_str}{reason_str}{by_str}\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text[:4000], reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception:
        await callback.message.edit_text(text[:3000], reply_markup=builder.as_markup())


# ─── Статус ИИ ───────────────────────────────────────────────────────────
async def show_ai_status(user_id: int, callback: CallbackQuery):
    """Показывает статус ИИ для выбранного чата."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")

    if not chat_id:
        await callback.answer("❌ Сначала выберите чат", show_alert=True)
        await show_chat_selection(user_id, callback)
        return

    chat_title = await get_chat_title(chat_id)
    chat_settings = settings.get(str(chat_id), {})

    personality = chat_settings.get("personality", "default")
    custom = chat_settings.get("custom_personality", "")
    provider = chat_settings.get("ai_provider", "pollinations")
    model = chat_settings.get("ai_model", "default")

    custom_status = "Да" if custom else "Нет"
    text = (
        f"🤖 **Статус ИИ для чата:** {chat_title}\n\n"
        f"👤 Персональность: {personality}\n"
        f"📝 Кастомная: {custom_status}\n"
        f"🔌 Провайдер: {provider}\n"
        f"🧠 Модель: {model}\n\n"
    )

    if custom:
        text += f"📄 Кастомная персональность:\n{custom[:200]}\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка показа статуса ИИ: {e}")


# ─── Активность пользователей ───────────────────────────────────────────────
async def show_activity(user_id: int, callback: CallbackQuery):
    """Показывает статистику активности пользователей в чате."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")

    if not chat_id:
        await callback.answer("❌ Сначала выберите чат", show_alert=True)
        await show_chat_selection(user_id, callback)
        return

    chat_title = await get_chat_title(chat_id)
    messages = get_chat_messages(chat_id, limit=200)

    if not messages:
        text = f"📊 **Активность в чате:** {chat_title}\n\nНет данных"
    else:
        # Считаем активность по пользователям
        user_stats = {}
        for msg in messages:
            uid = msg.get("user_id")
            if uid:
                if uid not in user_stats:
                    user_stats[uid] = {"count": 0, "name": msg.get("user_name", ""), "username": msg.get("username", "")}
                user_stats[uid]["count"] += 1

        # Сортируем по количеству сообщений
        sorted_users = sorted(user_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:20]

        text = f"📊 **Активность в чате:** {chat_title}\n\n"
        for uid, stats in sorted_users:
            user_info = f"@{stats['username']}" if stats['username'] else stats['name'] or f"ID: {uid}"
            text += f"👤 {user_info} — {stats['count']} сообщений\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text[:4000], reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception:
        await callback.message.edit_text(text[:3000], reply_markup=builder.as_markup())


# ─── Информация о боте ─────────────────────────────────────────────────────
async def show_bot_info(user_id: int, callback: CallbackQuery):
    """Показывает информацию о боте."""
    total_chats = len(settings)
    known_chats = get_known_chats()
    
    text = (
        f"🤖 **Информация о боте**\n\n"
        f"📱 Чатов с настройками: {total_chats}\n"
        f"📋 Чатов с логами: {len(known_chats)}\n\n"
        f"⚙️ Версия: 1.0\n"
        f"🔧 Статус: Активен\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка показа инфо о боте: {e}")


# ─── Быстрые действия ───────────────────────────────────────────────────────
async def show_quick_actions(user_id: int, callback: CallbackQuery):
    """Показывает быстрые действия для выбранного чата."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")

    if not chat_id:
        await callback.answer("❌ Сначала выберите чат", show_alert=True)
        await show_chat_selection(user_id, callback)
        return

    chat_title = await get_chat_title(chat_id)
    chat_settings = settings.get(str(chat_id), {})

    antispam_enabled = chat_settings.get("antispam", {}).get("enabled", False)
    anti_raid_enabled = chat_settings.get("anti_raid", {}).get("enabled", False)

    text = (
        f"⚡ **Быстрые действия для чата:** {chat_title}\n\n"
        f"📊 Антиспам: {'✅ Вкл' if antispam_enabled else '❌ Выкл'}\n"
        f"🚨 Антирейд: {'✅ Вкл' if anti_raid_enabled else '❌ Выкл'}\n\n"
        f"Выберите действие:"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Вкл/Выкл Антиспам", callback_data="panel:toggle_antispam")
    builder.button(text="🚨 Вкл/Выкл Антирейд", callback_data="panel:toggle_raid")
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(2, 1)

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка быстрых действий: {e}")


async def toggle_antispam(user_id: int, callback: CallbackQuery):
    """Переключает антиспам."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")
    if not chat_id:
        return

    chat_id_str = str(chat_id)
    if chat_id_str not in settings:
        settings[chat_id_str] = {}
    
    if "antispam" not in settings[chat_id_str]:
        settings[chat_id_str]["antispam"] = {"enabled": False, "punishment": "мут", "duration": 30, "unit": "мин"}
    
    current = settings[chat_id_str]["antispam"].get("enabled", False)
    settings[chat_id_str]["antispam"]["enabled"] = not current
    save_settings(chat_id_str)
    
    await show_quick_actions(user_id, callback)
    await callback.answer(f"Антиспам {'включён' if not current else 'выключен'}")


async def toggle_raid(user_id: int, callback: CallbackQuery):
    """Переключает антирейд."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")
    if not chat_id:
        return

    chat_id_str = str(chat_id)
    if chat_id_str not in settings:
        settings[chat_id_str] = {}
    
    if "anti_raid" not in settings[chat_id_str]:
        settings[chat_id_str]["anti_raid"] = {"enabled": False}
    
    current = settings[chat_id_str]["anti_raid"].get("enabled", False)
    settings[chat_id_str]["anti_raid"]["enabled"] = not current
    save_settings(chat_id_str)
    
    await show_quick_actions(user_id, callback)
    await callback.answer(f"Антирейд {'включён' if not current else 'выключен'}")


# ─── Последние сообщения ───────────────────────────────────────────────────
async def show_recent_messages(user_id: int, callback: CallbackQuery):
    """Показывает последние сообщения в чате."""
    session = _panel_sessions.get(user_id, {})
    chat_id = session.get("chat_id")

    if not chat_id:
        await callback.answer("❌ Сначала выберите чат", show_alert=True)
        await show_chat_selection(user_id, callback)
        return

    chat_title = await get_chat_title(chat_id)
    messages = get_chat_messages(chat_id, limit=20)

    if not messages:
        text = f"📜 **Последние сообщения:** {chat_title}\n\nНет сообщений"
    else:
        text = f"📜 **Последние сообщения:** {chat_title}\n\n"
        for msg in messages[-10:]:
            user = msg.get("user_name", "Unknown")
            msg_text = msg.get("text", "")[:50]
            msg_type = msg.get("type", "text")
            text += f"👤 {user}: [{msg_type}] {msg_text}...\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="panel:main")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text[:4000], reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()
    except Exception:
        await callback.message.edit_text(text[:3000], reply_markup=builder.as_markup())


# ─── Callback handler ───────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("panel:"))
async def handle_panel_callback(callback: CallbackQuery):
    """Обработка нажатий на кнопки панели."""
    user_id = callback.from_user.id
    data = callback.data.split(":")

    if len(data) < 2:
        await callback.answer("❌ Неверный формат", show_alert=True)
        return

    action = data[1]

    if action == "main":
        await show_main_menu(user_id, callback=callback)
    elif action == "select_chat":
        await show_chat_selection(user_id, callback)
    elif action == "settings":
        session = _panel_sessions.get(user_id, {})
        chat_id = session.get("chat_id")
        if not chat_id:
            await show_chat_selection(user_id, callback)
        else:
            await show_settings(user_id, chat_id, callback)
    elif action == "logs":
        await show_logs(user_id, callback)
    elif action == "log":
        if len(data) >= 3:
            log_file = data[2]
            await show_log_content(user_id, log_file, callback)
    elif action == "punished":
        await show_punished(user_id, callback)
    elif action == "ai_status":
        await show_ai_status(user_id, callback)
    elif action == "activity":
        await show_activity(user_id, callback)
    elif action == "bot_info":
        await show_bot_info(user_id, callback)
    elif action == "quick_actions":
        await show_quick_actions(user_id, callback)
    elif action == "recent_messages":
        await show_recent_messages(user_id, callback)
    elif action == "toggle_antispam":
        await toggle_antispam(user_id, callback)
    elif action == "toggle_raid":
        await toggle_raid(user_id, callback)
    elif action == "chat":
        if len(data) >= 3:
            chat_id = int(data[2])
            await show_settings(user_id, chat_id, callback)


# ─── Команда .панель ───────────────────────────────────────────────────────
@router.message(F.text.in_({".панель", "!панель"}))
async def open_panel(message: Message):
    """Открывает панель управления в ЛС."""
    if message.chat.type != "private":
        return await message.reply("❌ Эта команда работает только в личных сообщениях бота.")
    user_id = message.from_user.id
    await show_main_menu(user_id, message=message)
