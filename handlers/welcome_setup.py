
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated, Chat, User
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION

from ..core.loader import bot
from ..storage.state import settings, save_settings
from ..core.utils import is_admin

logger = logging.getLogger(__name__)

router = Router()


async def send_quick_setup_menu(chat: Chat, added_by: User):
    """Старое меню быстрой настройки в группе. Оставлено только как fallback —
    основная настройка теперь идёт в ЛС через dm_setup.py."""
    chat_id = chat.id
    chat_title = chat.title or "группа"
    added_by_name = added_by.first_name if added_by else "администратор"

    try:
        member = await bot.get_chat_member(chat_id, added_by.id)
        is_chat_owner = (member.status == "creator")
        is_chat_admin = (member.status in ["creator", "administrator"])
    except Exception as e:
        logger.error(f"Ошибка проверки прав: {e}")
        is_chat_owner = False
        is_chat_admin = False

    welcome_text = (
        f"👋 **Привет, {chat_title}!**\n\n"
        f"Меня добавил {'👑 Владелец' if is_chat_owner else '⭐ Администратор' if is_chat_admin else '👤 Участник'} "
        f"**{added_by_name}**\n\n"
        f"🛡️ Я — **AI Defender**, бот-защитник с искусственным интеллектом.\n\n"
        f"**Мои возможности:**\n"
        f"• ⛔ Антиспам\n"
        f"• 🚨 Антирейд (массовый вход)\n"
        f"• 🤖 AI-помощник (отвечаю на вопросы)\n"
        f"• 📝 Персонализация поведения\n\n"
    )

    if is_chat_owner or is_chat_admin:
        welcome_text += (
            f"👑 **Быстрая настройка для {added_by_name}:**\n"
            f"Нажми кнопки ниже для настройки защит или просто попроси меня:\n"
            f"_\"Включи все защиты\"_ - и я всё настрою сам!"
        )
    else:
        welcome_text += (
            f"⚠️ **Внимание!** {added_by_name} не является админом.\n"
            f"Настройка доступна только владельцу и админам группы.\n\n"
            f"Владелец может настроить меня кнопками ниже или просто написать:\n"
            f"_\"Включи все защиты\"_ (Через ответ на сообщения бота)"
        )

    builder = InlineKeyboardBuilder()
    if is_chat_owner or is_chat_admin:
        builder.button(text="⛔ Включить антиспам", callback_data=f"setup:spam:on:{chat_id}")
        builder.button(text="🚨 Включить антирейд", callback_data=f"setup:raid:on:{chat_id}")
        builder.button(text="✅ Включить ВСЁ", callback_data=f"setup:all:on:{chat_id}")
        builder.button(text="❌ Пока ничего", callback_data=f"setup:skip:{chat_id}")
        builder.adjust(2, 1, 1)
    else:
        builder.button(text="❌ Закрыть", callback_data=f"setup:skip:{chat_id}")

    try:
        await bot.send_message(
            chat_id,
            welcome_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки приветствия/меню быстрой настройки: {e}")


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added_to_chat(event: ChatMemberUpdated):
    """Когда бота добавляют в группу. По новой логике после политики и подписки
    настройка идёт в ЛС, поэтому здесь только отправляем приглашение в ЛС, если
    политика+подписка уже подтверждены ранее."""
    if event.chat.type not in ['group', 'supergroup']:
        return

    chat_id = event.chat.id
    chat_title = event.chat.title
    added_by = event.from_user

    logger.info(f"[WELCOME] Бот добавлен в группу '{chat_title}' ({chat_id}) пользователем {added_by.first_name}")

    cid = str(chat_id)
    if not settings.get(cid, {}).get("privacy_accepted"):
        return

    # Политика принята ранее. По новой логике, дальнейшая настройка идёт в ЛС.
    if settings.get(cid, {}).get("subscription_ok"):
        try:
            from .dm_setup import send_dm_setup_invite
            await send_dm_setup_invite(
                chat_id,
                settings.get(cid, {}).get("added_by_user_id") or (added_by.id if added_by else None),
            )
        except Exception as e:
            logger.error(f"[WELCOME] send_dm_setup_invite failed: {e}")


@router.callback_query(F.data.startswith("setup:"))
async def handle_setup_callback(callback: CallbackQuery):
    """Старый обработчик инлайн-кнопок настройки — оставлен для совместимости
    со старыми сообщениями. Новая настройка идёт через dm_setup.py."""

    data = callback.data.split(":")
    action_type = data[1]
    chat_id = int(data[3]) if len(data) > 3 else callback.message.chat.id

    try:
        member = await bot.get_chat_member(chat_id, callback.from_user.id)
        if member.status not in ["creator", "administrator"]:
            await callback.answer("❌ Только админы могут настраивать бота!", show_alert=True)
            return
    except Exception as e:
        logger.error(f"Ошибка проверки прав: {e}")
        await callback.answer("❌ Ошибка проверки прав", show_alert=True)
        return

    chat_id_str = str(chat_id)

    if action_type == "skip":
        await callback.message.edit_text(
            "✅ Настройка завершена.\n\n"
            "Ты можешь настроить меня позже:\n"
            "• **Командами:** `!антиспам вкл`, `!антирейд вкл`\n"
            "• **Через AI:** просто напиши мне _\"Включи антиспам\"_\n\n"
            "Я умный — понимаю обычный язык!✅ ",
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    if chat_id_str not in settings:
        settings[chat_id_str] = {}

    if action_type == "all":
        settings[chat_id_str]["antispam"] = {"enabled": True, "action": "mute", "mute_duration": 1800}
        settings[chat_id_str]["anti_raid"] = {
            "enabled": True, "analyze_photos": True, "caps_threshold": 0.0,
            "join_threshold": 5, "join_window": 10, "lockdown_duration": 300,
            "ban_new_joins": True, "restrict_new_users": True, "notify_admins": True,
            "ban_for_tags": True, "delete_links": True, "test_mode": False,
        }
        save_settings(chat_id_str)
        await callback.message.edit_text("✅ Все защиты включены.", parse_mode="Markdown")
        await callback.answer("✅ Все защиты активированы!")

    elif action_type == "spam":
        settings[chat_id_str]["antispam"] = {"enabled": True, "action": "mute", "mute_duration": 1800}
        save_settings(chat_id_str)
        await callback.answer("✅ Антиспам включен!")

    elif action_type == "raid":
        settings[chat_id_str]["anti_raid"] = {
            "enabled": True, "analyze_photos": True, "caps_threshold": 0.0,
            "join_threshold": 5, "join_window": 10, "lockdown_duration": 300,
            "ban_new_joins": True, "restrict_new_users": True, "notify_admins": True,
            "ban_for_tags": True, "delete_links": True, "test_mode": False,
        }
        save_settings(chat_id_str)
        await callback.answer("✅ Антирейд включен!")