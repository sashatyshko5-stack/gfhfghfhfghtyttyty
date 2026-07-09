import asyncio
import logging

import aiohttp
from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..core.loader import bot
from ..storage.state import settings, save_settings

logger = logging.getLogger(__name__)
router = Router(name="dm_setup")


LAOZHANG_API_URL = "https://api.laozhang.ai/v1/chat/completions"
LAOZHANG_TEST_MODEL = "gpt-4o-mini"


# ============================================================
#  IN-MEMORY STATE (user_id -> { step, chat_id, data })
# ============================================================
_dm_state: dict[int, dict] = {}


def _state(uid: int) -> dict | None:
    return _dm_state.get(uid)


def _set_state(uid: int, st: dict) -> None:
    _dm_state[uid] = st


def _clear_state(uid: int) -> None:
    _dm_state.pop(uid, None)


# ============================================================
#  ПРОВЕРКА КЛЮЧА LAOZHANG (быстрый тестовый запрос)
# ============================================================
async def validate_laozhang_key(api_key: str, timeout: int = 20) -> tuple[bool, str]:
    """Проверяет ключ Laozhang.ai простым chat completions запросом."""
    if not api_key or not api_key.strip().startswith("sk-"):
        return False, "Ключ должен начинаться с `sk-`."
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LAOZHANG_TEST_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "temperature": 0,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                LAOZHANG_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return True, "ok"
                text = await resp.text()
                snippet = text[:200].replace("\n", " ")
                if resp.status in (401, 403):
                    return False, "Ключ отклонён сервером (401/403). Проверьте, что это ключ от laozhang.ai."
                if resp.status in (402, 429):
                    return False, f"Ключ не прошёл проверку (статус {resp.status}): возможно, исчерпан лимит/баланс."
                return False, f"Ошибка проверки ключа (HTTP {resp.status}): {snippet}"
    except asyncio.TimeoutError:
        return False, "Таймаут при проверке ключа. Попробуйте ещё раз."
    except Exception as e:
        return False, f"Ошибка сети: {e}"


# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
async def _is_admin_of_chat(chat_id: int, user_id: int) -> bool:
    """
    Проверяет, является ли user_id админом chat_id.
    Делает 2 попытки: get_chat_member, затем get_chat_administrators (fallback).
    """
    # Попытка 1: get_chat_member
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status_raw = getattr(member, "status", "")
        status_str = str(status_raw).lower().split(".")[-1]
        logger.info(f"[DM-SETUP] get_chat_member chat={chat_id} user={user_id} status={status_str}")
        if status_str in ("administrator", "creator", "owner"):
            return True
    except Exception as e:
        logger.warning(f"[DM-SETUP] get_chat_member failed chat={chat_id} user={user_id}: {e}")

    # Попытка 2: get_chat_administrators
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for adm in admins:
            try:
                if adm.user and adm.user.id == user_id:
                    logger.info(f"[DM-SETUP] user {user_id} найден в admins {chat_id}")
                    return True
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[DM-SETUP] get_chat_administrators failed chat={chat_id}: {e}")

    # Финальный fallback: тот кто запускал политику/подписку
    try:
        cid = str(chat_id)
        chat_settings = settings.get(cid, {})
        added_by = chat_settings.get("added_by_user_id")
        sub_by = chat_settings.get("subscription_confirmed_by")
        if user_id in (added_by, sub_by):
            logger.info(f"[DM-SETUP] user {user_id} распознан как админ через settings (added_by/sub_by)")
            return True
    except Exception as e:
        logger.debug(f"[DM-SETUP] settings fallback err: {e}")

    return False


async def send_dm_setup_invite(chat_id: int, admin_user_id: int | None) -> None:
    """Отправляет в группу сообщение со ссылкой на продолжение настройки в ЛС."""
    try:
        me = await bot.get_me()
        bot_username = me.username
        # Telegram допускает [A-Za-z0-9_-] в start-параметре, минус разрешён.
        deeplink = f"https://t.me/{bot_username}?start=setup_{chat_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🛠 Продолжить настройку в ЛС", url=deeplink),
            ],
            [
                InlineKeyboardButton(text="⏭ Пропустить настройку", callback_data=f"skip_setup:{chat_id}"),
            ],
        ])
        text = (
            "✅ <b>Подписка подтверждена.</b>\n\n"
            "🛠 <b>Дальнейшая настройка проходит в личных сообщениях с ботом.</b>\n"
            "Это нужно, чтобы безопасно передать API-ключи и сконфигурировать защиты "
            "только для вас как админа.\n\n"
            "Нажмите кнопку ниже — откроется чат с ботом, и настройка продолжится там.\n"
            "Если бот в ЛС не ответил — нажмите там кнопку <b>«Запустить» / «Start»</b> "
            "или отправьте команду: <code>/start setup_" + str(chat_id) + "</code>\n\n"
            "Или нажмите «⏭ Пропустить» — бот сразу активируется без настройки API-ключей."
        )
        await bot.send_message(
            chat_id, text, reply_markup=kb,
            parse_mode="HTML", disable_web_page_preview=True,
        )
        logger.info(f"[DM-SETUP] invite отправлен в группу {chat_id}, deeplink={deeplink}")
    except Exception as e:
        logger.error(f"[DM-SETUP] send_dm_setup_invite error: {e}")


async def _send_ai_greeting(chat_id: int) -> None:
    """Отправляет AI-приветствие бота в группу после добавления/настройки."""
    try:
        from ..services.ai_module import get_ai_reply
        cid = str(chat_id)
        settings.setdefault(cid, {})
        settings[cid].setdefault("ai_enabled", True)
        prompt = (
            "Тебя только что добавили в эту группу (или завершили настройку). "
            "Поприветствуй участников группы, расскажи кратко что ты умеешь: "
            "отвечаешь на вопросы, защищаешь от спама/рейдов, реагируешь на обращения по имени. "
            "Будь дружелюбным, в стиле текущей персональности. 3–5 предложений."
        )
        reply, _ = await get_ai_reply(prompt, chat_id, message=None)
        if reply and not reply.startswith("❌") and reply != "None":
            await bot.send_message(chat_id, reply)
        else:
            await bot.send_message(
                chat_id,
                "👋 Привет! Я готов к работе в этой группе.\n"
                "Обращайтесь ко мне по имени или через @упоминание, и я отвечу. "
                "Напишите <code>!команды</code> для списка доступных команд.",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning(f"[DM-SETUP] ai greeting failed: {e}")
        try:
            await bot.send_message(
                chat_id,
                "👋 Привет! Я готов к работе. Напишите !команды для списка команд.",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("skip_setup:"))
async def cb_skip_setup(cb: CallbackQuery):
    """Пропустить настройку — активировать ИИ сразу без конфигурации."""
    parts = cb.data.split(":", 1)
    if len(parts) < 2:
        return await cb.answer()
    try:
        chat_id = int(parts[1])
    except ValueError:
        return await cb.answer("Ошибка: неверный chat_id")

    if not await _is_admin_of_chat(chat_id, cb.from_user.id):
        return await cb.answer("❌ Только администратор может пропустить настройку", show_alert=True)

    cid = str(chat_id)
    settings.setdefault(cid, {})
    settings[cid]["ai_enabled"] = True
    settings[cid]["setup_skipped"] = True
    save_settings(cid)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("✅ Настройка пропущена — ИИ активирован!")

    await _send_ai_greeting(chat_id)
    logger.info(f"[DM-SETUP] setup skipped for chat={chat_id} by user={cb.from_user.id}")


# ============================================================
#  ОБЩАЯ ЛОГИКА ШАГА 0 (/start setup_<chat_id>)
# ============================================================
async def _begin_dm_setup(message: Message, raw_payload: str) -> bool:
    """
    Общая логика старта DM-setup.
    Возвращает True, если апдейт обработан (даже если с ошибкой пользователю),
    False — если payload не наш и нужно пропустить дальше.
    """
    payload = (raw_payload or "").strip()
    if not payload.startswith("setup_"):
        return False

    uid = message.from_user.id
    logger.info(f"[DM-SETUP] /start payload='{payload}' от user={uid}")

    raw_id = payload[len("setup_"):]
    try:
        chat_id = int(raw_id)
    except ValueError:
        await message.answer(
            f"❌ Неверная ссылка для настройки (chat_id=<code>{raw_id}</code>).",
            parse_mode="HTML",
        )
        return True

    if not await _is_admin_of_chat(chat_id, uid):
        await message.answer(
            "❌ Вы не являетесь админом этой группы. Настройка доступна "
            "только владельцу/админам той группы, куда добавлен бот.\n\n"
            f"chat_id: <code>{chat_id}</code>\n"
            f"user_id: <code>{uid}</code>",
            parse_mode="HTML",
        )
        return True

    cid = str(chat_id)
    chat_settings = settings.get(cid, {})
    if not chat_settings.get("privacy_accepted") or not chat_settings.get("subscription_ok"):
        await message.answer(
            "❌ В группе ещё не приняты политика или подписка.\n"
            f"privacy_accepted: <b>{bool(chat_settings.get('privacy_accepted'))}</b>\n"
            f"subscription_ok: <b>{bool(chat_settings.get('subscription_ok'))}</b>\n\n"
            "Завершите эти шаги в группе, затем вернитесь сюда.",
            parse_mode="HTML",
        )
        return True

    _set_state(uid, {"step": "await_key_text", "chat_id": chat_id, "data": {}})
    logger.info(f"[DM-SETUP] стейт установлен для user={uid}, chat={chat_id}, step=await_key_text")

    await message.answer(
       
        "🛠 <b>Настройка бота для вашей группы</b>\n\n"
        "<b>Шаг 1 из 5 — API-ключ для текстового ИИ</b>\n\n"
        "Отправьте сюда <b>API-ключ от сервиса laozhang.ai</b>.\n\n"
        "Этот ключ будет использоваться для текстовых запросов ИИ "
        "(ответы в чате, анализ сообщений и т.п.).\n\n"
        "⚠️ Ключ должен быть <b>строго от laozhang.ai</b> и начинаться с <code>sk-</code>.\n\n"
        "Получить ключ можно на сайте: https://api.laozhang.ai\n\n"
        "Отправьте ключ одной строкой следующим сообщением.\n\n"
        "Чтобы прервать настройку — отправьте /cancel.\n\n"
        "ГАЙД ПО НАСТРОЙКЕ:https://telegra.ph/POLNYJ-GAJD-PO-PRAVILNOJ-NASTROJKE-II-05-31\n\n",
        
        parse_mode="HTML", disable_web_page_preview=True,
    )
    return True


# ============================================================
#  ШАГ 0a: /start setup_<chat_id> через CommandStart(deep_link=True)
# ============================================================
@router.message(CommandStart(deep_link=True), F.chat.type == ChatType.PRIVATE)
async def dm_setup_start_deeplink(message: Message, command: CommandObject):
    logger.info(f"[DM-SETUP] HIT deeplink handler args='{command.args}' user={message.from_user.id}")
    handled = await _begin_dm_setup(message, command.args or "")
    if not handled:
        # Не наш payload — отдаём дальше (referral и т.п.)
        return


# ============================================================
#  ШАГ 0b: запасной хендлер для /start setup_... (без deep_link flag)
#  Срабатывает, если payload не подхватился через CommandStart(deep_link=True).
# ============================================================
@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text.regexp(r"^/start\s+setup_-?\d+\s*$"),
)
async def dm_setup_start_fallback(message: Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    logger.info(f"[DM-SETUP] HIT fallback handler text='{text}' user={message.from_user.id}")
    await _begin_dm_setup(message, payload)


# ============================================================
#  /cancel
# ============================================================
@router.message(F.chat.type == ChatType.PRIVATE, F.text == "/cancel")
async def dm_setup_cancel(message: Message):
    uid = message.from_user.id
    if uid in _dm_state:
        _clear_state(uid)
        await message.answer(
            "❌ Настройка прервана. Чтобы начать заново, "
            "вернитесь в группу и нажмите кнопку «Продолжить настройку в ЛС»."
        )


# ============================================================
#  Текстовый роутер шагов настройки
#  ВАЖНО: фильтр ~F.text.startswith("/") — не хватаем команды.
# ============================================================
def _has_dm_setup_state(message: Message) -> bool:
    try:
        return _state(message.from_user.id) is not None
    except Exception:
        return False


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.startswith("!"),
    ~F.text.startswith("."),
    _has_dm_setup_state,
)
async def dm_setup_text_router(message: Message):
    uid = message.from_user.id
    st = _state(uid)
    if not st:
        return
    step = st.get("step")
    if step == "await_key_text":
        await _handle_key_text(message, st)
    elif step == "await_key_vision":
        await _handle_key_vision(message, st)
    elif step == "await_personality":
        await _handle_personality(message, st)


async def _handle_key_text(message: Message, st: dict):
    key = (message.text or "").strip()
    waiting = await message.answer("⏳ Проверяю ключ Laozhang.ai...")
    ok, info = await validate_laozhang_key(key)
    try:
        await waiting.delete()
    except Exception:
        pass
    if not ok:
        await message.answer(
            f"❌ Ключ не принят: {info}\n\n"
            "Отправьте корректный ключ Laozhang.ai (формат <code>sk-...</code>) или /cancel.",
            parse_mode="HTML",
        )
        return
    st["data"]["laozhang_text_key"] = key
    st["step"] = "await_key_vision"
    await message.answer(
        "✅ Ключ для текстового ИИ принят.\n\n"
        "<b>Шаг 2 из 5 — второй API-ключ laozhang.ai</b>\n\n"
        "Теперь отправьте <b>ещё один API-ключ от laozhang.ai</b>.\n"
        "Второй ключ нужен для распределения нагрузки и работы зрения/антирейда "
        "(анализ фото, NSFW, рейд-фильтр).\n\n"
        "Если у вас только один ключ — можно отправить тот же самый.\n"
        "Чтобы прервать — /cancel.",
        parse_mode="HTML",
    )


async def _handle_key_vision(message: Message, st: dict):
    key = (message.text or "").strip()
    waiting = await message.answer("⏳ Проверяю второй ключ Laozhang.ai...")
    ok, info = await validate_laozhang_key(key)
    try:
        await waiting.delete()
    except Exception:
        pass
    if not ok:
        await message.answer(
            f"❌ Ключ не принят: {info}\n\n"
            "Отправьте корректный ключ Laozhang.ai (формат <code>sk-...</code>) или /cancel.",
            parse_mode="HTML",
        )
        return
    st["data"]["laozhang_vision_key"] = key
    st["step"] = "choose_protections"
    st["data"]["protections"] = {
        "antinsfw": False, "antispam": False, "anti_raid": False, 
    }
    await message.answer(
        "✅ Второй ключ принят.\n\n"
        "<b>Шаг 3 из 5 — выбор защит</b>\n\n"
        "Отметьте, какие защиты включить в группе. "
        "Нажмите на пункт, чтобы переключить его (✅/⬜), затем нажмите «Далее».",
        parse_mode="HTML",
        reply_markup=_protections_kb(st["data"]["protections"]),
    )


def _protections_kb(p: dict) -> InlineKeyboardMarkup:
    def mark(v: bool) -> str:
        return "✅" if v else "⬜"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mark(p['antinsfw'])} Защита 18+ (NSFW)",
                              callback_data="dmsetup:p:antinsfw")],
        [InlineKeyboardButton(text=f"{mark(p['antispam'])} Антиспам",
                              callback_data="dmsetup:p:antispam")],
        [InlineKeyboardButton(text=f"{mark(p['anti_raid'])} Антирейд",
                              callback_data="dmsetup:p:anti_raid")],
       
        [InlineKeyboardButton(text="✅ Включить ВСЁ", callback_data="dmsetup:p:all"),
         InlineKeyboardButton(text="⬜ Снять ВСЁ", callback_data="dmsetup:p:none")],
        [InlineKeyboardButton(text="➡️ Далее", callback_data="dmsetup:next")],
    ])


@router.callback_query(F.data.startswith("dmsetup:p:"))
async def dm_protections_toggle(cb: CallbackQuery):
    uid = cb.from_user.id
    st = _state(uid)
    if not st or st.get("step") != "choose_protections":
        return await cb.answer()
    key = cb.data.split(":", 2)[2]
    p = st["data"].setdefault("protections", {
        "antinsfw": False, "antispam": False, "anti_raid": False, 
    })
    if key == "all":
        for k in p:
            p[k] = True
    elif key == "none":
        for k in p:
            p[k] = False
    elif key in p:
        p[key] = not p[key]
    try:
        await cb.message.edit_reply_markup(reply_markup=_protections_kb(p))
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "dmsetup:next")
async def dm_protections_next(cb: CallbackQuery):
    uid = cb.from_user.id
    st = _state(uid)
    if not st or st.get("step") != "choose_protections":
        return await cb.answer()
    st["step"] = "await_personality"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        "<b>Шаг 4 из 5 — персональность бота</b>\n\n"
        "Опишите одной фразой стиль/характер бота "
        "(например: «дружелюбный модератор, общается на ты, любит шутки»).\n\n"
        "Это будет учитываться при ответах ИИ в группе.\n"
        "Отправьте текст или нажмите «Пропустить».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="dmsetup:skip_personality"),
        ]]),
    )
    await cb.answer()


async def _handle_personality(message: Message, st: dict):
    text = (message.text or "").strip()
    if len(text) > 500:
        await message.answer("❗ Слишком длинно (>500 символов). Сократите, пожалуйста, или /cancel.")
        return
    st["data"]["personality"] = text
    await _go_to_confirm(message.chat.id, message.from_user.id)


@router.callback_query(F.data == "dmsetup:skip_personality")
async def dm_skip_personality(cb: CallbackQuery):
    uid = cb.from_user.id
    st = _state(uid)
    if not st or st.get("step") != "await_personality":
        return await cb.answer()
    st["data"]["personality"] = ""
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _go_to_confirm(cb.message.chat.id, uid)
    await cb.answer("Пропущено")


async def _go_to_confirm(dm_chat_id: int, uid: int):
    st = _state(uid)
    if not st:
        return
    st["step"] = "confirm"
    p = st["data"].get("protections", {})
    chat_id = st["chat_id"]
    try:
        info = await bot.get_chat(chat_id)
        chat_title = info.title or str(chat_id)
    except Exception:
        chat_title = str(chat_id)

    enabled_list = [
        ("🔞 Защита 18+", p.get("antinsfw")),
        ("⛔ Антиспам", p.get("antispam")),
        ("🚨 Антирейд", p.get("anti_raid")),
      
    ]
    rows = "\n".join(
        f"• {name}: {'✅ вкл' if val else '⬜ выкл'}" for name, val in enabled_list
    )
    personality = st["data"].get("personality") or "—"

    text = (
        "<b>Шаг 5 из 5 — подтверждение</b>\n\n"
        f"Группа: <b>{chat_title}</b>\n"
        f"Текстовый ключ Laozhang: <code>сохранён</code>\n"
        f"Vision-ключ Laozhang: <code>сохранён</code>\n\n"
        f"Защиты:\n{rows}\n\n"
        f"Персональность: <i>{personality}</i>\n\n"
        "Применить настройки?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data="dmsetup:apply"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="dmsetup:abort")],
    ])
    await bot.send_message(dm_chat_id, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "dmsetup:abort")
async def dm_abort(cb: CallbackQuery):
    uid = cb.from_user.id
    _clear_state(uid)
    try:
        await cb.message.edit_text("❌ Настройка отменена. Чтобы начать заново — "
                                   "вернитесь в группу и снова нажмите кнопку настройки.")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "dmsetup:apply")
async def dm_apply(cb: CallbackQuery):
    uid = cb.from_user.id
    st = _state(uid)
    if not st or st.get("step") != "confirm":
        return await cb.answer()

    chat_id = st["chat_id"]
    cid = str(chat_id)
    data = st["data"]
    settings.setdefault(cid, {})

    settings[cid]["laozhang_text_key"] = data.get("laozhang_text_key", "")
    settings[cid]["laozhang_vision_key"] = data.get("laozhang_vision_key", "")

    p = data.get("protections", {})
    if p.get("antinsfw"):
        settings[cid]["antinsfw"] = {"enabled": True, "action": "mute", "mute_duration": 1800}
    if p.get("antispam"):
        settings[cid]["antispam"] = {"enabled": True, "action": "mute", "mute_duration": 1800}
    if p.get("anti_raid"):
        settings[cid]["anti_raid"] = {
            "enabled": True, "analyze_photos": True, "caps_threshold": 0.0,
            "join_threshold": 5, "join_window": 10, "lockdown_duration": 300,
            "ban_new_joins": True, "restrict_new_users": True, "notify_admins": True,
            "ban_for_tags": True, "delete_links": True, "test_mode": False,
        }
  

    pers = data.get("personality", "")
    if pers:
        settings[cid]["personality"] = pers

    settings[cid]["dm_setup_completed"] = True
    settings[cid]["dm_setup_by_user_id"] = uid
    save_settings(cid)

    _clear_state(uid)

    try:
        await cb.message.edit_text(
            "✅ <b>Настройки применены.</b>\n\n"
            "Бот готов к работе в вашей группе.\n"
            "Команды доступны прямо в группе — напишите там <code>!команды</code>.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await cb.answer("Готово")

    try:
        enabled_names = []
        if p.get("antinsfw"):
            enabled_names.append("🔞 18+")
        if p.get("antispam"):
            enabled_names.append("⛔ Антиспам")
        if p.get("anti_raid"):
            enabled_names.append("🚨 Антирейд")

        en_text = ", ".join(enabled_names) if enabled_names else "—"
        await bot.send_message(
            chat_id,
            "✅ <b>Настройка завершена администратором в ЛС.</b>\n"
            f"Включённые защиты: {en_text}\n"
            "Бот готов к работе.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"[DM-SETUP] notify group failed: {e}")

    await _send_ai_greeting(chat_id)