
import asyncio

from aiogram import Router, F
from aiogram.filters import BaseFilter, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardButton,
    InlineKeyboardMarkup, CallbackQuery,
)

from ..storage.state import settings, save_settings
from ..core.utils import is_admin
from ..core.logging_setup import log_short, log_full

router = Router()

PRIVACY_POLICY_URL = "https://telegra.ph/POLNYJ-GAJD-PO-PRAVILNOJ-NASTROJKE-II-05-31"
AI_DEFENDER_GUIDE = "https://teletype.in/@chelik01/jOToRQLsy8m"
ACTIVATION_TIMEOUT = 5 * 60

PRIVACY_TEXT = (
    "Прежде чем продолжить,ознакомьтесь с ботом,посмотрите гайды"
)


class PrefixCmd(BaseFilter):
    def __init__(self, *names: str):
        self.names = {n.lower() for n in names}

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return False
        t = message.text.strip()
        if not t or t[0] not in "!.":
            return False
        head = t[1:].split(maxsplit=1)[0].lower()
        return head in self.names


def _kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Открыть гайд по ии", url=PRIVACY_POLICY_URL)],
            [InlineKeyboardButton(text="📄 Открыть полный гайд по AI Defender", url=AI_DEFENDER_GUIDE)],
        [
            InlineKeyboardButton(text="✅ Продолжить", callback_data="privacy:accept"),
            InlineKeyboardButton(text="❌ Прервать настройку", callback_data="privacy:decline"),
        ],
    ])


def is_privacy_accepted(chat_id) -> bool:
    return bool(settings.get(str(chat_id), {}).get("privacy_accepted"))


def global_notifications_enabled(chat_id) -> bool:
    return not settings.get(str(chat_id), {}).get("global_notifications_disabled", False)


_activation_tasks: dict = {}


def _cancel_activation_task(chat_id: int) -> None:
    task = _activation_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _check_bot_is_admin(bot, chat_id: int) -> bool:
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log_full(chat_id, "warning", f"activation: get_chat_member failed: {e}")
        return False


async def _activation_watchdog(bot, chat_id: int):
    try:
        await asyncio.sleep(ACTIVATION_TIMEOUT)
        accepted = is_privacy_accepted(chat_id)
        is_admin_ok = await _check_bot_is_admin(bot, chat_id)
        if accepted and is_admin_ok:
            log_short(chat_id, "activation: ok (политика принята, бот админ)")
            return

        reasons = []
        if not accepted:
            reasons.append("политика не принята")
        if not is_admin_ok:
            reasons.append("боту не выданы права админа")
        reason_text = ", ".join(reasons)
        log_short(chat_id, f"activation timeout: {reason_text} — выходим из группы")
        try:
            await bot.send_message(
                chat_id,
                "⏰ Прошло 5 минут с момента добавления бота, "
                f"но {reason_text}.\n"
                "Бот покидает группу. Добавьте его снова,и настройте.Также проверьте что вы подписаны на @AiDefender_125",
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat_id)
        except Exception as e:
            log_full(chat_id, "error", f"activation leave_chat failed: {e}")
    except asyncio.CancelledError:
        return
    except Exception as e:
        log_full(chat_id, "error", f"activation watchdog error: {e}")
    finally:
        _activation_tasks.pop(chat_id, None)


def _start_activation_watchdog(bot, chat_id: int) -> None:
    _cancel_activation_task(chat_id)
    task = asyncio.create_task(_activation_watchdog(bot, chat_id))
    _activation_tasks[chat_id] = task


async def send_privacy_prompt(bot, chat_id: int):
    try:
        sent = await bot.send_message(
            chat_id, PRIVACY_TEXT,
            reply_markup=_kb(), parse_mode="HTML", disable_web_page_preview=True,
        )
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=False)
        except Exception as e:
            log_full(chat_id, "warning", f"privacy pin failed: {e}")
        cid = str(chat_id)
        settings.setdefault(cid, {})
        settings[cid]["privacy_message_id"] = sent.message_id
        save_settings(cid)
        log_short(chat_id, "privacy: запрошено согласие, сообщение закреплено")
    except Exception as e:
        log_full(chat_id, "error", f"privacy send error: {e}")


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_bot_added(event: ChatMemberUpdated):
    if event.chat.type not in ("group", "supergroup"):
        return
    cid = str(event.chat.id)
    settings.setdefault(cid, {})

    try:
        if event.from_user and not event.from_user.is_bot:
            settings[cid]["added_by_user_id"] = event.from_user.id
            settings[cid]["added_by_user_name"] = (
                event.from_user.full_name or event.from_user.username or ""
            )
            save_settings(cid)
    except Exception:
        pass

    if settings[cid].get("privacy_accepted"):
        _start_activation_watchdog(event.bot, event.chat.id)
        if not settings[cid].get("subscription_ok"):
            try:
                from .subscription import send_subscription_prompt
                await send_subscription_prompt(
                    event.bot, event.chat.id,
                    added_by_user_id=settings[cid].get("added_by_user_id"),
                )
            except Exception as e:
                log_full(event.chat.id, "error", f"subscription prompt on add error: {e}")
        return
    await send_privacy_prompt(event.bot, event.chat.id)
    _start_activation_watchdog(event.bot, event.chat.id)


@router.callback_query(F.data == "privacy:accept")
async def privacy_accept(cb: CallbackQuery):
    member = await cb.bot.get_chat_member(cb.message.chat.id, cb.from_user.id)
    if member.status not in ("administrator", "creator"):
        return await cb.answer("Только админ может выполнять это действие.", show_alert=True)

    cid = str(cb.message.chat.id)
    settings.setdefault(cid, {})
    settings[cid]["privacy_accepted"] = True
    settings[cid]["history_confirmed"] = True
    save_settings(cid)

    chat_id = cb.message.chat.id
    is_admin_ok = await _check_bot_is_admin(cb.bot, chat_id)
    if is_admin_ok:
        _cancel_activation_task(chat_id)
    else:
        _start_activation_watchdog(cb.bot, chat_id)

    try:
        await cb.message.edit_text(
            "✅ Готово. Бот активирован.\n"
            "Чтобы узнать команды и другие функции — ответьте реплаем на сообщение бота "
            "или напишите <code>!команды</code>.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    log_short(cb.message.chat.id, f"privacy: принята {cb.from_user.id}")
    await cb.answer("Принято")

    # После принятия политики — сначала требуем подписку на канал,
    # и только после её подтверждения отправится приглашение на настройку в ЛС.
    try:
        from .subscription import send_subscription_prompt, is_subscription_confirmed
        if is_subscription_confirmed(chat_id):
            from .dm_setup import send_dm_setup_invite
            cid_local = str(chat_id)
            added_by = settings.get(cid_local, {}).get("added_by_user_id") or cb.from_user.id
            await send_dm_setup_invite(chat_id, added_by)
        else:
            cid_local = str(chat_id)
            added_by = settings.get(cid_local, {}).get("added_by_user_id") or cb.from_user.id
            await send_subscription_prompt(cb.bot, chat_id, added_by_user_id=added_by)
    except Exception as e:
        log_full(cb.message.chat.id, "error", f"after-privacy flow error: {e}")


@router.callback_query(F.data == "privacy:decline")
async def privacy_decline(cb: CallbackQuery):
    member = await cb.bot.get_chat_member(cb.message.chat.id, cb.from_user.id)
    if member.status not in ("administrator", "creator"):
        return await cb.answer("Только админ.", show_alert=True)
    chat_id = cb.message.chat.id
    _cancel_activation_task(chat_id)
    try:
        await cb.message.edit_text("❌ Настройка прервана. Бот покидает группу.")
    except Exception:
        pass
    log_short(chat_id, f"privacy: отклонена {cb.from_user.id}, выходим")
    await cb.answer("Выхожу")
    try:
        await cb.bot.leave_chat(chat_id)
    except Exception as e:
        log_full(chat_id, "error", f"leave_chat: {e}")


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    PrefixCmd("политика"),
)
async def resend_privacy(message: Message):
    if not await is_admin(message):
        return
    if is_privacy_accepted(message.chat.id):
        return await message.reply(
            f"Политика отменена."
        )
    await send_privacy_prompt(message.bot, message.chat.id)
    _start_activation_watchdog(message.bot, message.chat.id)


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    PrefixCmd("глобал_соо", "глобалсоо"),
)
async def global_notifications_toggle(message: Message):
    if not is_privacy_accepted(message.chat.id):
        return
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")

    cid = str(message.chat.id)
    settings.setdefault(cid, {})
    parts = message.text.split()
    current = settings[cid].get("global_notifications_disabled", False)

    if len(parts) < 2:
        state = "ВЫКЛ" if current else "ВКЛ"
        return await message.reply(
            f"🌐 Глобальные уведомления: <b>{state}</b>\n"
            "Использование: <code>!глобал_соо вкл/выкл</code>",
            parse_mode="HTML",
        )

    mode = parts[1].lower()
    if mode not in ("вкл", "выкл"):
        return await message.reply(
            "❗ Используй: <code>!глобал_соо вкл</code> или <code>!глобал_соо выкл</code>",
            parse_mode="HTML",
        )

    settings[cid]["global_notifications_disabled"] = (mode == "выкл")
    save_settings(cid)

    if mode == "выкл":
        log_short(message.chat.id, "global_notifications: OFF")
        await message.reply("🔕 Глобальные уведомления отключены.")
    else:
        log_short(message.chat.id, "global_notifications: ON")
        await message.reply("🔔 Глобальные уведомления включены.")