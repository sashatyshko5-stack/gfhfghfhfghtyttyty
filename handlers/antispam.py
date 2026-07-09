import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..core.utils import (
    can_bot_restrict_members,
    clear_punished,
    is_admin,
    is_already_punished,
    mark_punished,
)
from ..storage.ai_context_events import format_user_tg, log_chat_event
from ..storage.state import save_settings, settings, user_messages

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Дефолты и хелперы по типам контента
# ---------------------------------------------------------------------------

# Все типы, по которым можно включать/выключать реакцию антиспама.
# Ключ — внутреннее имя, значение — список алиасов команды.
TYPE_ALIASES = {
    "text": ["текст"],
    "sticker": ["стикер", "стикеры"],
    "gif": ["гиф", "гифки", "анимация", "анимации"],
    "photo": ["фото", "фотки", "картинки"],
    "video": ["видео"],
    "document": ["документы", "документ", "доки", "файлы", "файл"],
    "voice": ["гс", "голосовые", "кружки", "кружок"],
}

# Дефолтные значения, если их нет в настройках чата
DEFAULT_THRESHOLD_COUNT = 5  # сколько сообщений
DEFAULT_THRESHOLD_SECONDS = 10  # за сколько секунд
DEFAULT_DUPLICATE_LIMIT = 3  # сколько одинаковых считается спамом

# Локи нужны, чтобы несколько сообщений одного спамера, обработанные параллельно,
# не проходили проверку is_already_punished до mark_punished одновременно.
# Без этого бот может несколько раз применить наказание и отправить несколько
# одинаковых уведомлений о муте/бане.
_punishment_locks: dict[tuple[str, int, str], asyncio.Lock] = {}

# Отдельный cooldown для уведомлений, где нельзя или не нужно ставить
# полноценную отметку наказания: тестовый режим и ошибки Telegram API.
_notice_cooldowns: dict[tuple[str, int, str], datetime] = {}
NOTICE_COOLDOWN_SECONDS = 30


def _get_punishment_lock(chat_id: str, user_id: int, punishment: str) -> asyncio.Lock:
    """Возвращает общий lock для наказания пользователя в конкретном чате."""
    key = (chat_id, user_id, punishment)
    lock = _punishment_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _punishment_locks[key] = lock
    return lock


def _drop_punishment_lock(chat_id: str, user_id: int, punishment: str) -> None:
    """Удаляет свободный lock, чтобы словарь не рос бесконечно."""
    key = (chat_id, user_id, punishment)
    lock = _punishment_locks.get(key)
    waiters = getattr(lock, "_waiters", None) if lock is not None else None
    has_waiters = bool(waiters)
    if lock is not None and not lock.locked() and not has_waiters:
        _punishment_locks.pop(key, None)


def _can_send_notice(chat_id: str, user_id: int, notice_type: str, now: datetime) -> bool:
    """Разрешает не чаще одного однотипного уведомления за cooldown."""
    key = (chat_id, user_id, notice_type)
    last_sent = _notice_cooldowns.get(key)
    if last_sent and (now - last_sent).total_seconds() < NOTICE_COOLDOWN_SECONDS:
        return False

    _notice_cooldowns[key] = now
    stale_after = NOTICE_COOLDOWN_SECONDS * 2
    for stale_key, sent_at in list(_notice_cooldowns.items()):
        if (now - sent_at).total_seconds() >= stale_after:
            _notice_cooldowns.pop(stale_key, None)
    return True


def _unmute_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Кнопка для ручного снятия мута, выданного антиспамом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Размутить",
                    callback_data=f"antispam_unmute:{user_id}",
                )
            ]
        ]
    )


def _full_chat_permissions() -> ChatPermissions:
    """Возвращает пользователю обычные права отправки сообщений после мута."""
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=True,
        can_invite_users=True,
        can_pin_messages=True,
        can_manage_topics=True,
    )


async def _is_callback_admin(callback: CallbackQuery) -> bool:
    """Проверяет, что кнопку нажал админ текущего чата."""
    if not callback.message:
        return False

    from ..core.loader import bot

    member = await bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
    return member.status in ("creator", "administrator")


def _resolve_type_alias(word: str):
    """Возвращает внутренний ключ типа по русскому слову, иначе None."""
    word = word.lower()
    for key, aliases in TYPE_ALIASES.items():
        if word in aliases:
            return key
    return None


def _detect_message_type(message: Message):
    """Определяет тип сообщения для антиспама."""
    if message.sticker:
        return "sticker"
    if message.animation:
        return "gif"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.voice or message.video_note:
        return "voice"
    if message.document:
        return "document"
    if message.text:
        return "text"
    return None


def _ensure_antispam_cfg(chat_id: str) -> dict:
    """Гарантирует наличие всех ключей в настройках антиспама чата."""
    settings.setdefault(chat_id, {})
    chat_settings = settings[chat_id]
    chat_settings.setdefault("antispam", {})
    antispam = chat_settings["antispam"]

    antispam.setdefault("enabled", False)
    antispam.setdefault("punishment", "мут")
    antispam.setdefault("duration", 30)
    antispam.setdefault("unit", "мин")
    antispam.setdefault("test_mode", False)
    antispam.setdefault("threshold_count", DEFAULT_THRESHOLD_COUNT)
    antispam.setdefault("threshold_seconds", DEFAULT_THRESHOLD_SECONDS)
    antispam.setdefault("duplicate_limit", DEFAULT_DUPLICATE_LIMIT)

    # types — по дефолту все включены
    types_cfg = antispam.setdefault("types", {})
    for key in TYPE_ALIASES.keys():
        types_cfg.setdefault(key, True)

    return antispam


def _format_status(antispam: dict) -> str:
    status = "✅ Включён" if antispam.get("enabled") else "❌ Выключен"
    test_status = "🧪 ВКЛ" if antispam.get("test_mode") else "🛡️ ВЫКЛ"

    t = antispam.get("types", {})
    types_lines = "\n".join(
        f"  • {ru[0]}: {'✅' if t.get(key, True) else '❌'}"
        for key, ru in TYPE_ALIASES.items()
    )

    return (
        f"**Антиспам:** {status}\n"
        f"**Режим теста:** {test_status}\n"
        f"**Наказание:** {antispam.get('punishment', 'мут')}\n"
        f"**Время:** {antispam.get('duration', 30)} {antispam.get('unit', 'мин')}\n"
        f"**Порог:** {antispam.get('threshold_count')} сообщений за "
        f"{antispam.get('threshold_seconds')} сек\n"
        f"**Лимит дубликатов:** {antispam.get('duplicate_limit')}\n"
        f"**Типы:**\n{types_lines}\n\n"
        "**Использование:**\n"
        "`!антиспам вкл/выкл`\n"
        "`!антиспам мут 30 мин`\n"
        "`!антиспам бан`\n"
        "`!антиспам порог 5 10` -- 5 сообщений за 10 сек\n"
        "`!антиспам дубли 3` -- лимит одинаковых сообщений\n"
        "`!антиспам текст вкл/выкл`\n"
        "`!антиспам стикеры вкл/выкл`\n"
        "`!антиспам гиф вкл/выкл`\n"
        "`!антиспам фото вкл/выкл`\n\n"
        "`!антиспам режим теста`"
    )


# ---------------------------------------------------------------------------
# Команда !антиспам
# ---------------------------------------------------------------------------

@router.message(F.text.startswith(("!антиспам", ".антиспам")))
async def handle_antispam(msg: Message):
    if not await is_admin(msg):
        return await msg.reply("❗ Только администратор может управлять настройками антиспама.")

    chat_id = str(msg.chat.id)
    parts = msg.text.strip().split()

    logger.info(f"ANTISPAM DEBUG: chat_id={chat_id}, parts={parts}")
    antispam = _ensure_antispam_cfg(chat_id)

    # --- !антиспам режим теста ---
    if len(parts) >= 3 and parts[1].lower() == "режим" and parts[2].lower() == "теста":
        current_mode = antispam.get("test_mode", False)
        new_mode = not current_mode
        antispam["test_mode"] = new_mode
        save_settings(chat_id)

        if new_mode:
            return await msg.reply(
                "🧪 **Режим теста антиспама ВКЛЮЧЕН**\n\n"
                "Теперь антиспам будет проверять сообщения админов и показывать уведомления без наказания.",
                parse_mode="Markdown",
            )
        return await msg.reply(
            "🛡️ **Режим теста антиспама ВЫКЛЮЧЕН**\n\nАнтиспам снова игнорирует админов.",
            parse_mode="Markdown",
        )

    # --- !антиспам порог N M ---
    if len(parts) >= 2 and parts[1].lower() == "порог":
        if len(parts) < 4:
            return await msg.reply(
                "❗ Использование: `!антиспам порог <сообщений> <секунд>`\n"
                f"Сейчас: {antispam.get('threshold_count')} сообщений / "
                f"{antispam.get('threshold_seconds')} сек",
                parse_mode="Markdown",
            )
        try:
            count = int(parts[2])
            seconds = int(parts[3])
        except ValueError:
            return await msg.reply("❗ Значения порога должны быть числами.")
        if count < 2 or seconds < 1:
            return await msg.reply("❗ Введи чуть больше")
        if seconds > 600:
            return await msg.reply("❗ Максимальное окно — 600 секунд.")

        antispam["threshold_count"] = count
        antispam["threshold_seconds"] = seconds
        save_settings(chat_id)
        return await msg.reply(
            f"✅ Порог антиспама: **{count}** сообщений за **{seconds}** секунд.",
            parse_mode="Markdown",
        )

    # --- !антиспам дубли N (лимит одинаковых сообщений) ---
    if len(parts) >= 2 and parts[1].lower() in ("дубли", "дубликаты", "одинаковых"):
        if len(parts) < 3:
            return await msg.reply(
                f"❗ Использование: `!антиспам дубли <N>`\nСейчас: {antispam.get('duplicate_limit')}",
                parse_mode="Markdown",
            )
        try:
            n = int(parts[2])
        except ValueError:
            return await msg.reply("❗ Значение должно быть числом.")
        if n < 2:
            return await msg.reply("❗ Введите чуть больше")
        antispam["duplicate_limit"] = n
        save_settings(chat_id)
        return await msg.reply(f"✅ Лимит одинаковых сообщений: **{n}**", parse_mode="Markdown")

    # --- !антиспам <тип> вкл/выкл ---
    if len(parts) >= 3:
        type_key = _resolve_type_alias(parts[1])
        action = parts[2].lower()
        if type_key and action in ("вкл", "выкл"):
            new_val = action == "вкл"
            antispam["types"][type_key] = new_val
            save_settings(chat_id)
            ru_name = TYPE_ALIASES[type_key][0]
            return await msg.reply(
                f"✅ Реакция антиспама на **{ru_name}**: "
                f"{'включена' if new_val else 'выключена'}.",
                parse_mode="Markdown",
            )

    # --- !антиспам вкл/выкл ---
    if (len(parts) == 2 and parts[1].lower() in ("вкл", "выкл")) or (
        len(parts) == 1 and parts[0].lower().endswith(("вкл", "выкл"))
    ):
        current_state = antispam.get("enabled", False)

        if len(parts) == 2:
            user_wants_enabled = parts[1].lower() == "вкл"
        else:
            cmd = parts[0].lower()
            user_wants_enabled = cmd.endswith("вкл")

        if current_state == user_wants_enabled:
            return await msg.reply(
                f"⚠️ Антиспам уже {'включён' if current_state else 'выключен'} по настройкам."
            )

        antispam["enabled"] = user_wants_enabled
        save_settings(chat_id)
        return await msg.reply(f"🛡️ Антиспам {'включён' if user_wants_enabled else 'выключен'}.")

    # --- !антиспам мут/бан ---
    if len(parts) >= 2:
        punishment = parts[1].lower()
        if punishment not in ("мут", "бан"):
            return await msg.reply(_format_status(antispam), parse_mode="Markdown")

        current_punishment = antispam.get("punishment", "мут")
        current_duration = antispam.get("duration", 30)
        current_unit = antispam.get("unit", "мин")

        if punishment == "бан":
            if current_punishment == "бан":
                return await msg.reply("⚠️ Наказание уже установлено: бан навсегда")
            antispam.update({"punishment": "бан", "duration": None, "unit": None})
            save_settings(chat_id)
            return await msg.reply("✅ Защита от спама: бан навсегда")

        # punishment == "мут"
        if len(parts) >= 3:
            try:
                duration = int(parts[2])
            except ValueError:
                return await msg.reply("❗ Время должно быть числом.")

            unit = parts[3].lower() if len(parts) > 3 else "мин"

            if current_punishment == "мут" and current_duration == duration and current_unit == unit:
                return await msg.reply(f"⚠️ Время мута уже установлено: {duration} {unit}")

            antispam.update({"punishment": "мут", "duration": duration, "unit": unit})
            save_settings(chat_id)
            return await msg.reply(f"✅ Защита от спама: мут на {duration} {unit}")

        if current_punishment == "мут" and current_duration == 30 and current_unit == "мин":
            return await msg.reply("⚠️ Время мута уже установлено: 30 мин")
        antispam.update({"punishment": "мут", "duration": 30, "unit": "мин"})
        save_settings(chat_id)
        return await msg.reply("✅ Защита от спама: мут на 30 мин")

    # --- !антиспам (без аргументов) ---
    return await msg.reply(_format_status(antispam), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Кнопка ручного размута
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("antispam_unmute:"))
async def handle_antispam_unmute(callback: CallbackQuery):
    if not callback.message:
        return await callback.answer("Сообщение уже недоступно.", show_alert=True)

    if not await _is_callback_admin(callback):
        return await callback.answer("❗ Размутить может только администратор.", show_alert=True)

    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (AttributeError, IndexError, ValueError):
        return await callback.answer("Некорректная кнопка размута.", show_alert=True)

    chat_id = str(callback.message.chat.id)

    from ..core.loader import bot

    try:
        await bot.restrict_chat_member(
            int(chat_id),
            user_id,
            permissions=_full_chat_permissions(),
        )
        await clear_punished(chat_id, user_id, "мут")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("✅ Пользователь размучен.", show_alert=True)
        log_chat_event(
            int(chat_id),
            f"АНТИСПАМ РАЗМУТ: user_id={user_id}, админ={format_user_tg(callback.from_user)}",
        )
    except Exception as e:
        await callback.answer(f"❌ Не удалось размутить: {e}", show_alert=True)


# ---------------------------------------------------------------------------
# Сама проверка на спам
# ---------------------------------------------------------------------------

async def antispam_check(message: Message, test_mode: bool = False, is_admin: bool = False) -> bool:
    """Проверка на спам — вызывается из unified_handler."""
    chat_id = str(message.chat.id)
    user_id = message.from_user.id
    now = datetime.now()
    key = (chat_id, user_id)

    logger.info(f"[ANTISPAM_CHECK] Начало проверки для user={user_id} в chat={chat_id}, test_mode={test_mode}")

    antispam_cfg = _ensure_antispam_cfg(chat_id)
    logger.info(f"[ANTISPAM_CHECK] Настройки: {antispam_cfg}")

    # Определяем тип сообщения и проверяем, включена ли реакция на него
    msg_type = _detect_message_type(message)
    if msg_type is None:
        logger.info("[ANTISPAM_CHECK] Неизвестный тип сообщения, пропускаем")
        return False

    types_cfg = antispam_cfg.get("types", {})
    if not types_cfg.get(msg_type, True):
        logger.info(f"[ANTISPAM_CHECK] Реакция на тип '{msg_type}' выключена, пропускаем")
        return False

    if key not in user_messages:
        user_messages[key] = []

    content_id = (
        f"text:{message.text[:100]}" if message.text else
        f"sticker:{message.sticker.file_unique_id}" if message.sticker else
        f"gif:{message.animation.file_unique_id}" if message.animation else
        f"photo:{message.photo[-1].file_unique_id}" if message.photo else
        f"video:{message.video.file_unique_id}" if message.video else
        f"voice:{message.voice.file_unique_id}" if message.voice else
        f"vnote:{message.video_note.file_unique_id}" if message.video_note else
        f"doc:{message.document.file_unique_id}" if message.document else
        ""
    )

    logger.info(f"[ANTISPAM_CHECK] content_id={content_id[:50] if content_id else 'пусто'}...")
    if not content_id:
        logger.info("[ANTISPAM_CHECK] Нет content_id, пропускаем")
        return False

    # Текущие пороги из настроек
    threshold_count = int(antispam_cfg.get("threshold_count", DEFAULT_THRESHOLD_COUNT))
    threshold_seconds = int(antispam_cfg.get("threshold_seconds", DEFAULT_THRESHOLD_SECONDS))
    duplicate_limit = int(antispam_cfg.get("duplicate_limit", DEFAULT_DUPLICATE_LIMIT))

    # Чистим историю (держим максимум на window*6, но не меньше 60 сек — чтобы было что удалять)
    keep_seconds = max(threshold_seconds * 6, 60)
    user_messages[key] = [
        (t, cid, mid) for t, cid, mid in user_messages[key]
        if (now - t).total_seconds() < keep_seconds
    ]
    user_messages[key].append((now, content_id, message.message_id))

    window_msgs = [cid for t, cid, _ in user_messages[key] if (now - t).total_seconds() < threshold_seconds]
    is_spam = len(window_msgs) >= threshold_count or window_msgs.count(content_id) >= duplicate_limit

    logger.info(
        f"[ANTISPAM_CHECK] Сообщений за {threshold_seconds} сек: {len(window_msgs)}, "
        f"одинаковых: {window_msgs.count(content_id)}, порог={threshold_count}/{duplicate_limit}, "
        f"is_spam={is_spam}"
    )

    if not is_spam:
        return False

    logger.warning(f"[ANTISPAM_CHECK] СПАМ ОБНАРУЖЕН от user={user_id}!")

    # Тестовый режим для админов
    if test_mode and is_admin:
        if not _can_send_notice(chat_id, user_id, "test", now):
            logger.info("[ANTISPAM_CHECK] ТЕСТ: уведомление пропущено по cooldown")
            return True

        try:
            await message.reply(
                f"⚠️ ТЕСТ АНТИСПАМ: Обнаружен спам!\n"
                f"Сообщений за {threshold_seconds} сек: {len(window_msgs)}\n"
                f"Одинаковых: {window_msgs.count(content_id)}\n"
                f"Порог: {threshold_count}/{duplicate_limit}"
            )
            logger.info("[ANTISPAM_CHECK] ТЕСТ: Отправлено уведомление админу")
        except Exception as e:
            logger.error(f"[ANTISPAM_CHECK] Ошибка отправки тестового уведомления: {e}")
        return True

    punishment = antispam_cfg.get("punishment", "мут")
    duration = antispam_cfg.get("duration", 30)
    unit = antispam_cfg.get("unit", "мин")

    spam_msg_ids = [
        mid for t, cid, mid in user_messages[key]
        if (now - t).total_seconds() < threshold_seconds
    ]

    from ..core.loader import bot

    for mid in spam_msg_ids:
        try:
            await bot.delete_message(int(chat_id), mid)
        except Exception:
            pass
    logger.info(f"[ANTISPAM_CHECK] Удалено {len(spam_msg_ids)} спам-сообщений")

    punishment_lock = _get_punishment_lock(chat_id, user_id, punishment)
    try:
        async with punishment_lock:
            already_punished = await is_already_punished(int(chat_id), user_id, punishment)
            if already_punished:
                logger.info(f"[ANTISPAM_CHECK] Пользователь уже наказан ({punishment}), только удаление")
                return True

            # Метку ставим внутри lock до Telegram API-вызова: остальные параллельные
            # проверки увидят наказание и не отправят второе сообщение о наказании.
            await mark_punished(chat_id, user_id, punishment)

            if punishment == "мут":
                ok, reason = await can_bot_restrict_members(message)
                if not ok:
                    await clear_punished(chat_id, user_id, punishment)
                    if _can_send_notice(chat_id, user_id, "mute_failed", now):
                        await message.answer(f"❌ Не удалось замутить: {reason}")
                    return False

                seconds = duration * {"сек": 1, "мин": 60, "час": 3600, "день": 86400}.get(unit, 60)
                until = now + timedelta(seconds=seconds)
                try:
                    await bot.restrict_chat_member(
                        int(chat_id),
                        user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=until.timestamp(),
                    )
                    await message.answer(
                        f"⚠️ {message.from_user.full_name} замучен за спам (на {duration} {unit})",
                        reply_markup=_unmute_keyboard(user_id),
                    )
                    log_chat_event(
                        int(chat_id),
                        f"АНТИСПАМ МУТ на {duration} {unit}: {format_user_tg(message.from_user)}",
                    )
                    logger.info("[ANTISPAM_CHECK] Пользователь замучен")
                    return True
                except Exception as e:
                    await clear_punished(chat_id, user_id, punishment)
                    if not _can_send_notice(chat_id, user_id, "mute_failed", now):
                        return False
                    if "method is available only for supergroups" in str(e).lower():
                        await message.answer(
                            "❌ Не удалось замутить: Telegram позволяет мутить только в супергруппах.\n"
                            "Сделай группу супергруппой или используй наказание `бан`."
                        )
                    else:
                        await message.answer(f"❌ Не удалось замутить: {e}")
                    return False

            if punishment == "бан":
                try:
                    await bot.ban_chat_member(int(chat_id), user_id)
                    await message.answer(f"🚫 {message.from_user.full_name} забанен за спам")
                    log_chat_event(
                        int(chat_id),
                        f"АНТИСПАМ БАН: {format_user_tg(message.from_user)}",
                    )
                    logger.info("[ANTISPAM_CHECK] Пользователь забанен")
                    return True
                except Exception as e:
                    await clear_punished(chat_id, user_id, punishment)
                    if _can_send_notice(chat_id, user_id, "ban_failed", now):
                        await message.answer(f"❌ Не удалось забанить: {e}")
                    return False
    finally:
        _drop_punishment_lock(chat_id, user_id, punishment)

    return False
