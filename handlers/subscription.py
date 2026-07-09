

import asyncio
import logging
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ..storage.state import settings, save_settings
from ..core.logging_setup import log_short, log_full

logger = logging.getLogger(__name__)
router = Router()

REQUIRED_CHANNEL_USERNAME = "AiDefender_125"
REQUIRED_CHANNEL_URL = "https://t.me/AiDefender_125"
REQUIRED_CHANNEL_LINK = f"@{REQUIRED_CHANNEL_USERNAME}"

SUBSCRIPTION_TIMEOUT = 5 * 60

SUBSCRIPTION_TEXT = (
    "📢 <b>Обязательная подписка на канал</b>\n\n"
    f"Для дальнейшей работы бота в этой группе владелец/админ, добавивший бота, "
    f"обязан быть подписан на канал {REQUIRED_CHANNEL_LINK}.\n\n"
    "1️⃣ Нажмите «📢 Открыть канал» и подпишитесь.\n"
    "2️⃣ Вернитесь сюда и нажмите «✅ Я подписался» — бот проверит подписку.\n\n"
    f"❗ <b>У вас 5 минут.</b> Если подписка не будет подтверждена — "
    f"бот автоматически покинет группу."
)


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")],
    ])


def is_subscription_confirmed(chat_id) -> bool:
    return bool(settings.get(str(chat_id), {}).get("subscription_ok"))


async def _is_user_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL_LINK, user_id)
        status = getattr(member, "status", None)
        status_str = str(status).lower() if status is not None else ""
        return any(s in status_str for s in ("member", "administrator", "creator", "owner"))
    except Exception as e:
        logger.warning(f"subscription: check failed for user {user_id}: {e}")
        return False


_sub_tasks: dict = {}


def cancel_subscription_watchdog(chat_id: int) -> None:
    task = _sub_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _subscription_watchdog(bot, chat_id: int):
    try:
        await asyncio.sleep(SUBSCRIPTION_TIMEOUT)
        if is_subscription_confirmed(chat_id):
            log_short(chat_id, "subscription: ok (подтверждена в срок)")
            return

        log_short(chat_id, "subscription timeout: не подтверждена за 5 минут — выходим")
        try:
            await bot.send_message(
                chat_id,
                "⏰ Прошло 5 минут, но подписка на "
                f"{REQUIRED_CHANNEL_LINK} не была подтверждена.\n"
                "Бот покидает группу. Добавьте его снова и подпишитесь на канал.",
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat_id)
        except Exception as e:
            log_full(chat_id, "error", f"subscription leave_chat failed: {e}")
    except asyncio.CancelledError:
        return
    except Exception as e:
        log_full(chat_id, "error", f"subscription watchdog error: {e}")
    finally:
        _sub_tasks.pop(chat_id, None)


def start_subscription_watchdog(bot, chat_id: int) -> None:
    cancel_subscription_watchdog(chat_id)
    task = asyncio.create_task(_subscription_watchdog(bot, chat_id))
    _sub_tasks[chat_id] = task


async def send_subscription_prompt(bot, chat_id: int, added_by_user_id: Optional[int] = None):
    cid = str(chat_id)
    settings.setdefault(cid, {})

    if settings[cid].get("subscription_ok"):
        return True

    if added_by_user_id is not None:
        settings[cid]["subscription_required_from"] = added_by_user_id
    save_settings(cid)

    try:
        sent = await bot.send_message(
            chat_id,
            SUBSCRIPTION_TEXT,
            reply_markup=_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        settings[cid]["subscription_message_id"] = sent.message_id
        save_settings(cid)
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=False)
        except Exception as e:
            log_full(chat_id, "warning", f"subscription pin failed: {e}")
        log_short(chat_id, f"subscription: запрошена подписка на {REQUIRED_CHANNEL_LINK}")
    except Exception as e:
        log_full(chat_id, "error", f"subscription send error: {e}")
        return False

    start_subscription_watchdog(bot, chat_id)
    return False


@router.callback_query(F.data == "sub:check")
async def subscription_check(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    cid = str(chat_id)

    try:
        member = await cb.bot.get_chat_member(chat_id, cb.from_user.id)
        status = str(getattr(member, "status", "")).lower()
        if not any(s in status for s in ("administrator", "creator", "owner")):
            return await cb.answer(
                "❗ Только владелец или админ чата может подтвердить подписку.",
                show_alert=True,
            )
    except Exception as e:
        log_full(chat_id, "error", f"subscription: get_chat_member failed: {e}")
        return await cb.answer("Ошибка проверки прав.", show_alert=True)

    subscribed = await _is_user_subscribed(cb.bot, cb.from_user.id)
    if not subscribed:
        return await cb.answer(
            f"❌ Вы не подписаны на {REQUIRED_CHANNEL_LINK}.\n"
            "Подпишитесь и нажмите кнопку снова.",
            show_alert=True,
        )

    settings.setdefault(cid, {})
    settings[cid]["subscription_ok"] = True
    settings[cid]["subscription_confirmed_by"] = cb.from_user.id
    save_settings(cid)
    cancel_subscription_watchdog(chat_id)

    try:
        await cb.message.edit_text(
            "✅ <b>Подписка подтверждена!</b>\n"
            f"Спасибо за подписку на {REQUIRED_CHANNEL_LINK}.\n\n"
            "Бот продолжает работу в обычном режиме.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    log_short(chat_id, f"subscription: подтверждена пользователем {cb.from_user.id}")
    await cb.answer("✅ Подписка подтверждена")

    # После подтверждения подписки — НЕ открываем меню настройки в группе.
    # Вместо этого приглашаем продолжить настройку в ЛС.
    try:
        from .dm_setup import send_dm_setup_invite
        added_by = settings.get(cid, {}).get("added_by_user_id") or cb.from_user.id
        await send_dm_setup_invite(chat_id, added_by)
    except Exception as e:
        log_full(chat_id, "error", f"dm setup invite (after sub) error: {e}")