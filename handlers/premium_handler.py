"""Обработчик !премиум — покупка подписки за Telegram Stars (XTR)."""
import logging

from aiogram import Router, F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from ..core.loader import bot
from ..storage.premium import (
    activate_chat_premium,
    activate_premium,
    get_chat_expires_str,
    get_chat_limit,
    get_chat_premium_info,
    get_expires_str,
    get_premium_chats,
    get_premium_info,
    has_chat_premium,
    has_premium,
    _MAX_CHATS_BY_PLAN,
)

logger = logging.getLogger(__name__)
router = Router(name="premium_handler")

_PLAN_LABELS = {
    "monthly": "Месяц",
    "yearly": "Год",
}
_STARS = {
    "monthly": 100,
    "yearly": 150,
}
_DAYS = {
    "monthly": 30,
    "yearly": 365,
}

_BOT_GUIDE = "https://teletype.in/@chelik01/jOToRQLsy8m"
_AI_GUIDE = "https://telegra.ph/POLNYJ-GAJD-PO-PRAVILNOJ-NASTROJKE-II-05-31"
_CHANNEL = "https://t.me/AiDefender_125"


# ─── Вспомогательные тексты и клавиатуры ─────────────────────────────────────

def _main_menu_text() -> str:
    return (
        "⭐ <b>ПРЕМИУМ-ПОДПИСКА</b>\n\n"
        "<b>🔐 Премиум-функции:</b>\n"
        "• <b>Антишлюхобот</b> — AI-фильтр шлюхоботов\n"
        "• <b>Антиссылки</b> — защита от слива инвайт-ссылок\n"
        "• <b>Рейд-база</b> — глобальный чёрный список рейдеров\n"
        "• <b>AI Анализ медиа</b> — ИИ анализирует фото, видео, GIF, аудио и стикеры\n\n"
        "💳 <b>Тарифы (оплата Telegram ⭐ Stars):</b>\n\n"
        "📅 <b>МЕСЯЦ — 100 ⭐</b>\n"
        "   • Все функции на 30 дней\n"
        "   • До 5 чатов\n\n"
        "📆 <b>ГОД — 150 ⭐</b>\n"
        "   • Все функции на 365 дней\n"
        "   • До 10 чатов\n\n"
        "👤 <b>Личный</b> — привязан к вам, работает в ваших чатах\n"
        "💬 <b>Чат</b> — для одного конкретного чата\n\n"
        "Выбери тип подписки 👇"
    )


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Личный премиум", callback_data="prem:type:user"),
            InlineKeyboardButton(text="💬 Чат-премиум", callback_data="prem:type:chat"),
        ],
    ])


def _user_plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Месяц — 100 ⭐ (до 5 чатов)", callback_data="prem:buy:user:monthly")],
        [InlineKeyboardButton(text="📆 Год — 150 ⭐ (до 10 чатов)", callback_data="prem:buy:user:yearly")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="prem:back:main")],
    ])


def _chat_plans_kb(user_id: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Месяц — 100 ⭐", callback_data="prem:chat_info:monthly")],
        [InlineKeyboardButton(text="📆 Год — 150 ⭐", callback_data="prem:chat_info:yearly")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="prem:back:main")],
    ])


async def _get_user_admin_chats(user_id: int) -> list[tuple[int, str]]:
    """Возвращает список (chat_id, title) где пользователь — администратор или владелец."""
    from ..storage.state import settings
    result: list[tuple[int, str]] = []
    for cid_str in list(settings.keys()):
        try:
            cid = int(cid_str)
            member = await bot.get_chat_member(cid, user_id)
            status = str(getattr(member, "status", "")).lower().split(".")[-1]
            if status in ("administrator", "creator", "owner"):
                try:
                    chat_obj = await bot.get_chat(cid)
                    title = chat_obj.title or f"Чат {cid}"
                except Exception:
                    title = f"Чат {cid}"
                result.append((cid, title))
        except Exception:
            pass
    return result


def _active_user_text(user_id: int) -> str:
    rec = get_premium_info(user_id)
    if not rec:
        return "❌ Нет активной подписки."
    plan = _PLAN_LABELS.get(rec.get("plan", ""), rec.get("plan", ""))
    expires = get_expires_str(user_id)
    chats_used = len(get_premium_chats(user_id))
    chat_limit = get_chat_limit(user_id)
    return (
        f"✅ <b>Личный премиум активен</b>\n\n"
        f"📋 Тариф: <b>{plan}</b>\n"
        f"📅 Действует до: <b>{expires}</b>\n"
        f"💬 Чатов: <b>{chats_used} / {chat_limit}</b>\n\n"
        f"<b>Доступные функции:</b>\n"
        f"• Антишлюхобот ✅\n"
        f"• Антиссылки ✅\n"
        f"• Рейд-база ✅\n\n"
        f"<i>Включить в группе: !антишлюхобот вкл, !антиссылки вкл, !список вкл</i>"
    )


# ─── /start cprem_CHATID — покупка чат-премиума через deeplink ───────────────

@router.message(CommandStart(deep_link=True), F.chat.type == ChatType.PRIVATE)
async def start_deeplink_handler(message: Message, command: CommandObject):
    args = (command.args or "").strip()

    if args == "buychat":
        # Перенаправление из группы — сразу показываем выбор тарифа чат-премиума
        await message.answer(
            "💬 <b>Чат-премиум</b>\n\n"
            "Выбери тариф, затем выбери чат для активации 👇",
            reply_markup=_chat_plans_kb(),
            parse_mode="HTML",
        )
        return

    if not args.startswith("cprem_"):
        return UNHANDLED  # отдаём другим обработчикам (dm_setup, referral)

    try:
        chat_id = int(args.split("_", 1)[1])
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат ссылки.")
        return

    if has_chat_premium(chat_id):
        info = get_chat_premium_info(chat_id)
        plan = _PLAN_LABELS.get(info.get("plan", ""), "")
        expires = get_chat_expires_str(chat_id)
        await message.answer(
            f"✅ У этого чата уже есть активный чат-премиум.\n"
            f"Тариф: <b>{plan}</b>, действует до <b>{expires}</b>.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"💬 <b>Чат-премиум</b>\n\n"
        f"Покупаете премиум для чата <code>{chat_id}</code>.\n"
        f"Все премиум-функции будут доступны всем администраторам этого чата.\n\n"
        f"📅 <b>Месяц — 50 ⭐</b> — 30 дней\n"
        f"📆 <b>Год — 100 ⭐</b> — 365 дней\n\n"
        f"Выбери тариф 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Месяц — 100 ⭐", callback_data=f"prem:buy:chat:{chat_id}:monthly")],
            [InlineKeyboardButton(text="📆 Год — 150 ⭐", callback_data=f"prem:buy:chat:{chat_id}:yearly")],
        ]),
        parse_mode="HTML",
    )


# ─── !премиум (в ЛС) ─────────────────────────────────────────────────────────

@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text.lower().in_({"!премиум", "!premium", ".премиум", ".premium"}),
)
async def cmd_premium(message: Message):
    uid = message.from_user.id
    if has_premium(uid):
        await message.answer(_active_user_text(uid), parse_mode="HTML")
    else:
        await message.answer(_main_menu_text(), reply_markup=_main_menu_kb(), parse_mode="HTML")


# ─── !чат_премиум (в группе) ─────────────────────────────────────────────────

@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: bool(t) and t.lower().strip() in {
        "!чат_премиум", ".чат_премиум", "!чат_premium", ".чат_premium"
    }),
)
async def cmd_chat_premium_group(message: Message):
    chat_id = message.chat.id

    if has_chat_premium(chat_id):
        info = get_chat_premium_info(chat_id)
        plan = _PLAN_LABELS.get(info.get("plan", ""), "")
        expires = get_chat_expires_str(chat_id)
        await message.reply(
            f"✅ <b>Чат-премиум активен</b>\n\n"
            f"📋 Тариф: <b>{plan}</b>\n"
            f"📅 Действует до: <b>{expires}</b>\n\n"
            f"• Антишлюхобот ✅\n"
            f"• Антиссылки ✅\n"
            f"• Рейд-база ✅",
            parse_mode="HTML",
        )
        return

    # Покупка строго в ЛС — перенаправляем туда
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "bot"
    await message.reply(
        "💬 <b>Чат-премиум</b>\n\n"
        "Покупка премиума для чата возможна только в <b>личных сообщениях</b> бота.\n\n"
        "Нажмите кнопку ниже, выберите тариф и нужный чат 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💳 Купить чат-премиум в ЛС",
                url=f"https://t.me/{bot_username}?start=buychat",
            )],
        ]),
        parse_mode="HTML",
    )


# ─── Callbacks: навигация ─────────────────────────────────────────────────────

@router.callback_query(F.data == "prem:back:main")
async def cb_back_main(cb: CallbackQuery):
    try:
        await cb.message.edit_text(_main_menu_text(), reply_markup=_main_menu_kb(), parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "prem:type:user")
async def cb_type_user(cb: CallbackQuery):
    uid = cb.from_user.id
    if has_premium(uid):
        try:
            await cb.message.edit_text(_active_user_text(uid), parse_mode="HTML")
        except Exception:
            pass
    else:
        text = (
            "👤 <b>Личный премиум</b>\n\n"
            "Привязан к вашему аккаунту.\n"
            "Активируйте в любых своих группах (до 5 или 10 чатов).\n\n"
            "Выбери тариф 👇"
        )
        try:
            await cb.message.edit_text(text, reply_markup=_user_plans_kb(), parse_mode="HTML")
        except Exception:
            pass
    await cb.answer()


@router.callback_query(F.data == "prem:type:chat")
async def cb_type_chat(cb: CallbackQuery):
    text = (
        "💬 <b>Чат-премиум</b>\n\n"
        "Привязывается к одному конкретному чату.\n"
        "Все функции доступны в этом чате для всех администраторов.\n\n"
        "Чтобы купить чат-премиум — зайди в нужную группу и введи команду "
        "<code>!чат_премиум</code>. Бот даст ссылку для оплаты.\n\n"
        "Или выбери тариф ниже 👇"
    )
    try:
        await cb.message.edit_text(text, reply_markup=_chat_plans_kb(cb.from_user.id), parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("prem:chat_info:"))
async def cb_chat_info(cb: CallbackQuery):
    """Выбран тариф → показываем список чатов пользователя (строго в ЛС)."""
    if cb.message.chat.type != ChatType.PRIVATE:
        return await cb.answer("Покупка только в личных сообщениях бота!", show_alert=True)

    plan = cb.data.split(":", 2)[2]
    uid = cb.from_user.id
    label = _PLAN_LABELS.get(plan, plan)
    stars = _STARS.get(plan, 0)
    star_str = f"{stars} ⭐"

    await cb.answer("🔍 Ищу ваши чаты…")
    try:
        await cb.message.edit_text(
            f"💬 <b>Чат-премиум — {label} ({star_str})</b>\n\n"
            "⏳ Загружаю список ваших чатов…",
            parse_mode="HTML",
        )
    except Exception:
        pass

    chats = await _get_user_admin_chats(uid)

    if not chats:
        try:
            await cb.message.edit_text(
                "😔 <b>Чаты не найдены.</b>\n\n"
                "Убедитесь, что вы являетесь администратором чата, в котором есть бот, "
                "и попробуйте снова.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="prem:type:chat")],
                ]),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # Строим кнопки со списком чатов
    rows = []
    for cid, title in chats:
        label_btn = title[:38] + ("…" if len(title) > 38 else "")
        mark = " ✅" if has_chat_premium(cid) else ""
        rows.append([InlineKeyboardButton(
            text=f"{label_btn}{mark}",
            callback_data=f"prem:chatsel:{plan}:{cid}",
        )])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="prem:type:chat")])

    try:
        await cb.message.edit_text(
            f"💬 <b>Чат-премиум — {label} ({star_str})</b>\n\n"
            "Выберите чат, для которого хотите активировать премиум:\n"
            "<i>(отображаются чаты, где вы администратор)</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─── Callback: инвойс личного премиума ───────────────────────────────────────

@router.callback_query(F.data.startswith("prem:buy:user:"))
async def cb_buy_user(cb: CallbackQuery):
    plan = cb.data.split(":", 3)[3]
    if plan not in _STARS:
        return await cb.answer("Неизвестный тариф", show_alert=True)

    stars = _STARS[plan]
    days = _DAYS[plan]
    chat_limit = _MAX_CHATS_BY_PLAN[plan]
    label = _PLAN_LABELS[plan]

    pay_link = await bot.create_invoice_link(
        title=f"Личный премиум — {label}",
        description=f"Все премиум-функции на {days} дней. До {chat_limit} чатов.",
        payload=f"uprem:{plan}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Премиум {label}", amount=stars)],
    )
    try:
        await cb.message.edit_text(
            f"⭐ <b>Личный премиум — {label}</b>\n\n"
            f"📅 Срок: <b>{days} дней</b>\n"
            f"💬 Чатов: <b>до {chat_limit}</b>\n"
            f"💳 Стоимость: <b>{stars} ⭐</b>\n\n"
            f"Нажми кнопку для оплаты через Telegram Stars 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {stars} ⭐", url=pay_link)],
                [InlineKeyboardButton(text="↩️ Назад", callback_data="prem:type:user")],
            ]),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await cb.answer()


# ─── Callback: выбран чат → экран подтверждения ──────────────────────────────

@router.callback_query(F.data.startswith("prem:chatsel:"))
async def cb_chatsel(cb: CallbackQuery):
    """prem:chatsel:<plan>:<chat_id> — показываем экран подтверждения."""
    if cb.message.chat.type != ChatType.PRIVATE:
        return await cb.answer("Покупка только в личных сообщениях!", show_alert=True)

    parts = cb.data.split(":")
    if len(parts) != 4:
        return await cb.answer("Ошибка данных", show_alert=True)

    plan, chat_id_str = parts[2], parts[3]
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        return await cb.answer("Неверный ID чата", show_alert=True)

    uid = cb.from_user.id
    label = _PLAN_LABELS.get(plan, plan)
    stars = _STARS.get(plan, 0)
    days = _DAYS.get(plan, 30)
    star_str = f"<b>{stars} ⭐ Telegram Stars</b>"

    try:
        chat_obj = await bot.get_chat(chat_id)
        chat_title = chat_obj.title or f"Чат {chat_id}"
    except Exception:
        chat_title = f"Чат {chat_id}"

    already = has_chat_premium(chat_id)
    already_note = "\n⚠️ <i>Премиум уже есть — срок будет продлён.</i>" if already else ""

    try:
        await cb.message.edit_text(
            f"🛒 <b>Подтверждение покупки</b>\n\n"
            f"💬 Чат: <b>{chat_title}</b>\n"
            f"📦 Тариф: <b>{label}</b>\n"
            f"💰 Стоимость: {star_str}\n"
            f"📅 Срок: <b>{days} дней</b>{already_note}\n\n"
            "Подтвердите покупку 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Подтвердить и оплатить" if stars > 0 else "✅ Активировать бесплатно",
                    callback_data=f"prem:chatconfirm:{plan}:{chat_id}",
                )],
                [InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=f"prem:chat_info:{plan}",
                )],
            ]),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await cb.answer()


# ─── Callback: подтверждено → оплата или мгновенная активация ────────────────

@router.callback_query(F.data.startswith("prem:chatconfirm:"))
async def cb_chatconfirm(cb: CallbackQuery):
    """prem:chatconfirm:<plan>:<chat_id> — выставляем счёт или сразу активируем (тест)."""
    if cb.message.chat.type != ChatType.PRIVATE:
        return await cb.answer("Покупка только в личных сообщениях!", show_alert=True)

    parts = cb.data.split(":")
    if len(parts) != 4:
        return await cb.answer("Ошибка данных", show_alert=True)

    plan, chat_id_str = parts[2], parts[3]
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        return await cb.answer("Неверный ID чата", show_alert=True)

    uid = cb.from_user.id
    label = _PLAN_LABELS.get(plan, plan)
    stars = _STARS.get(plan, 0)
    days = _DAYS.get(plan, 30)

    try:
        chat_obj = await bot.get_chat(chat_id)
        chat_title = chat_obj.title or f"Чат {chat_id}"
    except Exception:
        chat_title = f"Чат {chat_id}"

    # ── Выставляем Stars-инвойс ──
    try:
        pay_link = await bot.create_invoice_link(
            title=f"Чат-премиум — {label}",
            description=f"Все премиум-функции для чата «{chat_title}» на {days} дней.",
            payload=f"cprem:{plan}:{chat_id}",
            currency="XTR",
            prices=[LabeledPrice(label=f"Чат-премиум {label}", amount=stars)],
        )
        await cb.message.edit_text(
            f"💳 <b>Счёт выставлен!</b>\n\n"
            f"💬 Чат: <b>{chat_title}</b>\n"
            f"📦 Тариф: <b>{label}</b> · {days} дней\n"
            f"💰 К оплате: <b>{stars} ⭐ Telegram Stars</b>\n\n"
            "Нажмите кнопку для оплаты — после успешной оплаты\n"
            "премиум активируется автоматически 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {stars} ⭐", url=pay_link)],
                [InlineKeyboardButton(text="↩️ Назад", callback_data="prem:back:main")],
            ]),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"[PREMIUM] Ошибка create_invoice_link: {e}")
        try:
            await cb.message.edit_text(
                f"❌ Не удалось выставить счёт: <code>{e}</code>\n\n"
                "Попробуйте позже или напишите <code>!связь</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await cb.answer()


# ─── Callback: инвойс чат-премиума через deeplink (старый способ) ────────────

@router.callback_query(F.data.startswith("prem:buy:chat:"))
async def cb_buy_chat(cb: CallbackQuery):
    """Оставлен для обратной совместимости с deeplink-флоу."""
    parts = cb.data.split(":")
    if len(parts) != 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    try:
        chat_id = int(parts[3])
    except ValueError:
        return await cb.answer("Неверный ID чата", show_alert=True)
    plan = parts[4]
    if plan not in _STARS:
        return await cb.answer("Неизвестный тариф", show_alert=True)

    stars = _STARS[plan]
    days = _DAYS[plan]
    label = _PLAN_LABELS[plan]

    try:
        chat_obj = await bot.get_chat(chat_id)
        chat_title = chat_obj.title or f"Чат {chat_id}"
    except Exception:
        chat_title = f"Чат {chat_id}"

    pay_link = await bot.create_invoice_link(
        title=f"Чат-премиум — {label}",
        description=f"Все премиум-функции для чата «{chat_title}» на {days} дней.",
        payload=f"cprem:{plan}:{chat_id}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Чат-премиум {label}", amount=stars)],
    )
    try:
        await cb.message.edit_text(
            f"💬 <b>Чат-премиум — {label}</b>\n\n"
            f"💬 Чат: <b>{chat_title}</b>\n"
            f"📅 Срок: <b>{days} дней</b>\n"
            f"💳 Стоимость: <b>{stars} ⭐</b>\n\n"
            f"Нажми кнопку для оплаты через Telegram Stars 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {stars} ⭐", url=pay_link)],
                [InlineKeyboardButton(text="↩️ Назад", callback_data="prem:back:main")],
            ]),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await cb.answer()


# ─── Pre-checkout (обязательно одобрить) ─────────────────────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


# ─── Successful payment ───────────────────────────────────────────────────────

@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    uid = message.from_user.id
    parts = payload.split(":")

    if payload.startswith("uprem:") and len(parts) == 2:
        plan = parts[1]
        activate_premium(uid, plan)
        label = _PLAN_LABELS.get(plan, plan)
        expires = get_expires_str(uid)
        chat_limit = get_chat_limit(uid)
        await message.answer(
            f"🎉 <b>Личный премиум активирован!</b>\n\n"
            f"📋 Тариф: <b>{label}</b>\n"
            f"📅 Действует до: <b>{expires}</b>\n"
            f"💬 Доступно чатов: <b>до {chat_limit}</b>\n\n"
            f"<b>Теперь доступны:</b>\n"
            f"• Антишлюхобот ✅\n"
            f"• Антиссылки ✅\n"
            f"• Рейд-база ✅\n\n"
            f"Включи функции в группе:\n"
            f"<code>!антишлюхобот вкл</code>\n"
            f"<code>!антиссылки вкл</code>\n"
            f"<code>!список вкл</code>",
            parse_mode="HTML",
        )
        logger.info(f"[PREMIUM] Личный plan={plan} активирован user={uid} до {expires}")

    elif payload.startswith("cprem:") and len(parts) == 3:
        plan = parts[1]
        try:
            chat_id = int(parts[2])
        except ValueError:
            await message.answer("❌ Ошибка обработки платежа.")
            return
        activate_chat_premium(chat_id, plan, uid)
        label = _PLAN_LABELS.get(plan, plan)
        expires = get_chat_expires_str(chat_id)
        await message.answer(
            f"🎉 <b>Чат-премиум активирован!</b>\n\n"
            f"💬 Чат: <code>{chat_id}</code>\n"
            f"📋 Тариф: <b>{label}</b>\n"
            f"📅 Действует до: <b>{expires}</b>\n\n"
            f"<b>В этом чате теперь доступны:</b>\n"
            f"• Антишлюхобот ✅\n"
            f"• Антиссылки ✅\n"
            f"• Рейд-база ✅\n\n"
            f"Любой администратор может включить:\n"
            f"<code>!антишлюхобот вкл</code>\n"
            f"<code>!антиссылки вкл</code>\n"
            f"<code>!список вкл</code>",
            parse_mode="HTML",
        )
        logger.info(f"[PREMIUM] Чат plan={plan} активирован chat={chat_id} owner={uid} до {expires}")
    else:
        logger.warning(f"[PREMIUM] Неизвестный payload: {payload!r}")
