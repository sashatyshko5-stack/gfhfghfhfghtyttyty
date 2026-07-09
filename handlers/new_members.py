


import logging

from aiogram import Router, F
from aiogram.types import Message, ChatMemberUpdated
from aiogram.dispatcher.event.bases import SkipHandler

from ..storage.state import settings
from ..storage.ai_context_events import log_chat_event, format_user_tg
from ..handlers.antispam import antispam_check
from ..handlers.anti_raid import storage, get_anti_raid_settings

logger = logging.getLogger(__name__)

router = Router()


# Статусы, означающие "бот ВНЕ чата"
_OUT_STATUSES = {"left", "kicked"}
# Статусы, означающие "бот В чате"
_IN_STATUSES = {"member", "administrator", "creator", "restricted"}


def _is_join_transition(event: ChatMemberUpdated) -> bool:
    """Универсальный детект 'добавили в чат' для ВСЕХ клиентов TG (PC/Android/iOS/Web).

    Не полагаемся на встроенный JOIN_TRANSITION фильтр, т.к. он не всегда совпадает
    при добавлении бота сразу админом с разных платформ.
    """
    try:
        old = event.old_chat_member
        new = event.new_chat_member
        old_status = str(getattr(old, "status", "") or "")
        new_status = str(getattr(new, "status", "") or "")
    except Exception:
        return False

    # Случай 'restricted' учитывает is_member флаг
    old_is_member = getattr(old, "is_member", None)
    new_is_member = getattr(new, "is_member", None)

    old_out = (old_status in _OUT_STATUSES) or (old_status == "restricted" and old_is_member is False)
    new_in = (new_status in _IN_STATUSES and new_status != "restricted") or (
        new_status == "restricted" and new_is_member is True
    )
    return old_out and new_in


@router.my_chat_member()
async def handle_bot_added(event: ChatMemberUpdated):
    """Приветствие при добавлении бота в группу.

    Универсальный обработчик: работает с любого клиента (PC, Android, iOS, Web),
    т.к. вручную определяет переход 'вне чата -> в чате', а не полагается на
    встроенный JOIN_TRANSITION фильтр.

    В конце пробрасывает SkipHandler, чтобы следующие роутеры (privacy,
    welcome_setup, private.on_bot_status_change) тоже получили это событие.
    """
    try:
        if event.chat.type not in ("group", "supergroup"):
            return

        if not _is_join_transition(event):
            return

        text = (
            "👋 Привет! Я — бот-защитник групп.\n\n"
            "🔐 Я умею:\n"
            "• Блокировать спам и рейды\n"
            "• Автоматически мутить/банить нарушителей\n"
            "• Реагировать на команды !антиспам, !антирейд, !связь(лс), !апи и др.\n\n"
            "📌 Введите !команды для списка основных команд"
        )

        # Отправляем без parse_mode, чтобы не сломаться на спецсимволах
        # (!, (), и т.д.) при разных клиентах TG.
        try:
            await event.bot.send_message(event.chat.id, text, parse_mode=None)
        except Exception as e:
            logger.warning(
                f"[NEW_MEMBERS] send welcome to chat {event.chat.id} failed: {type(e).__name__}: {e}"
            )
    finally:
        # ВАЖНО: пробрасываем событие следующим роутерам (welcome_setup, privacy,
        # private.on_bot_status_change), иначе они никогда не отработают.
        raise SkipHandler


@router.message(F.new_chat_members)
async def handle_new_members(message: Message):
    chat_id = message.chat.id
    bot_user = await message.bot.me()

    rules = settings.get(str(chat_id), {}).get("rules") or settings.get(chat_id, {}).get("rules")
    custom_welcome = settings.get(str(chat_id), {}).get("welcome_message")

    anti_raid_settings = get_anti_raid_settings(chat_id)

    for member in message.new_chat_members:
        if member.id == bot_user.id:
            continue

        log_chat_event(chat_id, f"ВХОД В ЧАТ: {format_user_tg(member)}")

        # Трекаем join в anti-raid storage
        storage.add_join(chat_id, member.id)
        storage.add_join_message(chat_id, message.message_id)

        if custom_welcome:
            greeting = custom_welcome.replace("{name}", member.full_name)
        else:
            greeting = f"👋 Добро пожаловать, {member.full_name}!"

        if rules:
            greeting += f"\n\n📜 {rules}"

        try:
            await message.reply(greeting)
        except Exception as e:
            logger.warning(f"[NEW_MEMBERS] reply welcome failed: {type(e).__name__}: {e}")

    # Проверяем порог рейда
    if anti_raid_settings.get("enabled", False):
        storage.cleanup_old_joins(chat_id, anti_raid_settings["join_window"])
        if storage.get_join_count(chat_id) >= anti_raid_settings["join_threshold"]:
            from ..handlers.anti_raid import handle_raid
            await handle_raid(chat_id, anti_raid_settings)

    await antispam_check(message)


@router.message(F.pinned_message, F.chat.type.in_({"group", "supergroup"}))
async def log_pin_for_ai_context(message: Message):
    """Сервисное сообщение о закрепе — пишем в ленту для контекста ИИ."""
    try:
        pm = message.pinned_message
        if not pm:
            return
        prev = (pm.text or pm.caption or "").strip()[:120]
        who = format_user_tg(message.from_user)
        author = format_user_tg(pm.from_user)
        log_chat_event(
            message.chat.id,
            f"ЗАКРЕП (сервис): закрепил {who}; автор сообщения {author}; «{prev}»",
        )
    except Exception:
        pass