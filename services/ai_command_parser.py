import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

from aiogram.types import Message, ChatPermissions

from ..core.loader import bot
from ..storage.state import settings, save_settings, chat_histories
from ..storage.ai_context_events import log_chat_event, format_user_tg

logger = logging.getLogger(__name__)


# ============================================================================
# РЕГЕКСПЫ И ВСПОМОГАТЕЛЬНОЕ
# ============================================================================

# Универсальный паттерн: [ACTION:TYPE] или [ACTION:TYPE:value...] — value может
# содержать любые символы кроме `]`, в т.ч. кириллицу/пробелы/двоеточия.
ACTION_RE = re.compile(r'\[ACTION:([A-Z_]+)(?::([^\]]*))?\]')

# Допустимые значения слоумода (Telegram API)
SLOWMODE_ALLOWED = {0, 10, 30, 60, 300, 900, 3600}

# Допустимые персональности
VALID_PERSONALITIES = {
    "нейтральный", "добрый", "злой",
    "саркастичный", "смешной", "токсичный", "кастомный", "фембой",
}

WARN_LIMIT = 3

# Типы контента для антиспама
SPAM_CONTENT_TYPES = {
    "TEXT": "text",
    "STICKER": "sticker",
    "GIF": "gif",
    "PHOTO": "photo",
    "VIDEO": "video",
    "VOICE": "voice",
    "DOCUMENT": "document",
}

# Конвертация единиц времени в секунды
TIME_UNIT_TO_SECONDS = {
    "сек": 1,
    "мин": 60,
    "час": 3600,
    "день": 86400,
}


def _split_value(value: str, max_parts: int) -> List[str]:
    """Разрезает value по `:` максимум на max_parts частей."""
    if not value:
        return []
    return value.split(":", max_parts - 1)


async def _resolve_target(message: Message, action_value: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Возвращает (target_user_id, display_name) для действий над пользователем.
    action_value:
      - "USER" → реплай если есть, иначе автор текущего сообщения
      - число  → готовый user_id
      - "@username" → пытаемся найти в участниках чата
    """
    if not action_value:
        return None, None

    av = action_value.strip()

    # числовой id
    if av.isdigit():
        uid = int(av)
        name = str(uid)
        try:
            member = await bot.get_chat_member(message.chat.id, uid)
            u = member.user
            name = u.first_name or (f"@{u.username}" if u.username else str(uid))
        except Exception:
            pass
        return uid, name

    # @username — пробуем достать из known users
    if av.startswith("@"):
        from ..storage.state import group_users
        uname = av.lstrip("@").lower()
        bucket = group_users.get(message.chat.id, {})
        for uid, info in bucket.items():
            if (info.get("username") or "").lower() == uname:
                name = info.get("first_name") or av
                return uid, name
        return None, None

    # USER — сначала реплай, если нет — автор текущего сообщения
    # (актуально для самообороны: бот отвечает тому, кто его оскорбил)
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        return target.id, (
            target.first_name or (f"@{target.username}" if target.username else str(target.id))
        )

    if message.from_user:
        u = message.from_user
        return u.id, (u.first_name or (f"@{u.username}" if u.username else str(u.id)))

    return None, None


def _ensure_chat_settings(chat_id_str: str) -> dict:
    if chat_id_str not in settings:
        settings[chat_id_str] = {}
    return settings[chat_id_str]


def _ensure_antispam_cfg(chat_st: dict) -> dict:
    """Гарантирует наличие всех ключей в настройках антиспама чата."""
    chat_st.setdefault("antispam", {})
    antispam = chat_st["antispam"]

    antispam.setdefault("enabled", False)
    antispam.setdefault("punishment", "мут")
    antispam.setdefault("duration", 30)
    antispam.setdefault("unit", "мин")
    antispam.setdefault("test_mode", False)
    antispam.setdefault("threshold_count", 5)
    antispam.setdefault("threshold_seconds", 10)
    antispam.setdefault("duplicate_limit", 3)

    # types — по дефолту все включены
    types_cfg = antispam.setdefault("types", {})
    for type_key in SPAM_CONTENT_TYPES.values():
        types_cfg.setdefault(type_key, True)

    return antispam


def _duration_to_seconds(duration: int, unit: str) -> int:
    """Конвертирует duration + unit в секунды."""
    multiplier = TIME_UNIT_TO_SECONDS.get(unit, 60)
    return duration * multiplier


# ============================================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================================

async def extract_and_execute_commands(message: Message, ai_response: str) -> Tuple[str, bool]:
    """
    Парсит [ACTION:...] теги из ответа AI и выполняет соответствующие команды.
    По правилу пользователя: успех — молча, в чат идут ТОЛЬКО ошибки и отказы.

    Возвращает (очищенный_текст, был_ли_выполнен_action).
    """
    chat_id = message.chat.id
    user_id = message.from_user.id
    chat_id_str = str(chat_id)

    # bot.id — для защиты от самобана
    try:
        me = await bot.get_me()
        bot_id = me.id
    except Exception:
        bot_id = 0

    actions = ACTION_RE.findall(ai_response)
    if not actions:
        return ai_response, False

    # Чистим теги из текста
    clean_text = ACTION_RE.sub("", ai_response).strip()

    # Права автора запроса (нужны только для некоторых действий)
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        is_admin = member.status in ("creator", "administrator")
        is_owner = member.status == "creator"
    except Exception as e:
        logger.error(f"Ошибка проверки прав: {e}")
        is_admin = False
        is_owner = False

    # ── Запоминаем только ошибки/отказы, успехи — молча ─────────────────────
    errors: List[str] = []
    executed = 0

    for action_type, raw_value in actions:
        value = raw_value or ""
        logger.info(f"[AI ACTION] {action_type}={value!r} chat={chat_id} user={user_id}")
        try:
            chat_st = _ensure_chat_settings(chat_id_str)

            # ============ ВКЛ/ВЫКЛ ЗАЩИТ (только для админов чата) ============
            if action_type in ("SPAM", "RAID", "ALL", "SPAM_PUNISH", "SPAM_THRESHOLD",
                               "SPAM_DUPLICATE", "SPAM_TYPE", "SPAM_TEST_MODE", "SPAM_STATUS",
                               "RAID_THRESHOLD", "RAID_LOCKDOWN", "RAID_CAPS",
                               "RAID_TAGS", "RAID_LINKS", "RAID_PHOTOS", "SPAM_ACTION",
                               "PERSONALITY", "SET_CUSTOM", "SET_RULES", "CLEAR_RULES",
                               "SET_WELCOME", "CLEAR_WELCOME", "SLOWMODE",
                               "CLEAR_LOGS", "SET_PROVIDER", "SET_MODEL"):
                if not is_admin:
                    errors.append(f"❌ {action_type}: требуются права администратора")
                    continue

            if action_type == "SPAM":
                antispam = _ensure_antispam_cfg(chat_st)
                enabled = (value.upper() == "ON")
                antispam["enabled"] = enabled
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "RAID":
                enabled = (value.upper() == "ON")
                cur = chat_st.get("anti_raid", {})
                chat_st["anti_raid"] = {
                    "enabled": enabled,
                    "analyze_photos": cur.get("analyze_photos", True),
                    "caps_threshold": cur.get("caps_threshold", 0.7),
                    "join_threshold": cur.get("join_threshold", 5),
                    "join_window": cur.get("join_window", 10),
                    "lockdown_duration": cur.get("lockdown_duration", 300),
                    "ban_new_joins": True,
                    "restrict_new_users": True,
                    "notify_admins": True,
                    "ban_for_tags": cur.get("ban_for_tags", True),
                    "delete_links": cur.get("delete_links", True),
                    "test_mode": False,
                }
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "ALL":
                enabled = (value.upper() == "ON")
                chat_st["antinsfw"] = {"enabled": enabled, "action": "mute", "mute_duration": 1800}
                antispam = _ensure_antispam_cfg(chat_st)
                antispam["enabled"] = enabled
                chat_st["anti_raid"] = {"enabled": enabled, "analyze_photos": True, "caps_threshold": 0.0}
                save_settings(chat_id_str)
                executed += 1

            # ============ НАСТРОЙКИ АНТИСПАМА ============

            elif action_type == "SPAM_PUNISH":
                parts = _split_value(value, 3)
                punishment = parts[0].upper() if parts else ""
                antispam = _ensure_antispam_cfg(chat_st)
                if punishment == "BAN":
                    antispam["punishment"] = "бан"
                    antispam["duration"] = None
                    antispam["unit"] = None
                    save_settings(chat_id_str)
                    executed += 1
                elif punishment == "MUTE":
                    duration = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
                    unit = parts[2] if len(parts) > 2 else "мин"
                    if unit not in TIME_UNIT_TO_SECONDS:
                        unit = "мин"
                    antispam["punishment"] = "мут"
                    antispam["duration"] = duration
                    antispam["unit"] = unit
                    save_settings(chat_id_str)
                    executed += 1
                else:
                    errors.append("❌ SPAM_PUNISH: ожидается BAN или MUTE[:duration:unit]")

            elif action_type == "SPAM_THRESHOLD":
                parts = _split_value(value, 2)
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    antispam = _ensure_antispam_cfg(chat_st)
                    antispam["threshold_count"] = int(parts[0])
                    antispam["threshold_seconds"] = int(parts[1])
                    save_settings(chat_id_str)
                    executed += 1
                else:
                    errors.append("❌ SPAM_THRESHOLD: ожидается формат 'количество:секунд' (например 5:10)")

            elif action_type == "SPAM_DUPLICATE":
                if value.isdigit():
                    antispam = _ensure_antispam_cfg(chat_st)
                    antispam["duplicate_limit"] = int(value)
                    save_settings(chat_id_str)
                    executed += 1
                else:
                    errors.append("❌ SPAM_DUPLICATE: ожидается число")

            elif action_type == "SPAM_TYPE":
                parts = _split_value(value, 2)
                if len(parts) >= 2:
                    content_type_raw = parts[0].upper()
                    action_val = parts[1].upper()
                    if content_type_raw in SPAM_CONTENT_TYPES:
                        type_key = SPAM_CONTENT_TYPES[content_type_raw]
                        antispam = _ensure_antispam_cfg(chat_st)
                        antispam["types"][type_key] = (action_val == "ON")
                        save_settings(chat_id_str)
                        executed += 1
                    else:
                        valid_types = ", ".join(SPAM_CONTENT_TYPES.keys())
                        errors.append(f"❌ SPAM_TYPE: неизвестный тип '{content_type_raw}'. Доступно: {valid_types}")
                else:
                    errors.append("❌ SPAM_TYPE: ожидается формат 'ТИП:ON/OFF' (например TEXT:ON)")

            elif action_type == "SPAM_TEST_MODE":
                antispam = _ensure_antispam_cfg(chat_st)
                antispam["test_mode"] = (value.upper() == "ON")
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "SPAM_STATUS":
                logger.info(f"[AI ACTION] SPAM_STATUS запрошен для чата {chat_id}")
                executed += 1

            # ============ НАСТРОЙКИ АНТИРЕЙДА ============

            elif action_type == "RAID_THRESHOLD":
                parts = _split_value(value, 2)
                threshold = int(parts[0]) if parts and parts[0].isdigit() else 5
                window = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                ar = chat_st.setdefault("anti_raid", {})
                ar["join_threshold"] = threshold
                ar["join_window"] = window
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "RAID_LOCKDOWN":
                if not value.isdigit():
                    errors.append("❌ RAID_LOCKDOWN: ожидается число секунд")
                    continue
                ar = chat_st.setdefault("anti_raid", {})
                ar["lockdown_duration"] = int(value)
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "RAID_CAPS":
                if not value.isdigit():
                    errors.append("❌ RAID_CAPS: ожидается процент")
                    continue
                ar = chat_st.setdefault("anti_raid", {})
                ar["caps_threshold"] = int(value) / 100
                save_settings(chat_id_str)
                executed += 1

            elif action_type in ("RAID_TAGS", "RAID_LINKS", "RAID_PHOTOS"):
                key = {
                    "RAID_TAGS": "ban_for_tags",
                    "RAID_LINKS": "delete_links",
                    "RAID_PHOTOS": "analyze_photos",
                }[action_type]
                ar = chat_st.setdefault("anti_raid", {})
                ar[key] = (value.upper() == "ON")
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "SPAM_ACTION":
                antispam = _ensure_antispam_cfg(chat_st)
                parts = _split_value(value, 2)
                act = (parts[0] or "").upper()
                if act == "BAN":
                    antispam["punishment"] = "бан"
                    antispam["duration"] = None
                    antispam["unit"] = None
                    save_settings(chat_id_str)
                    executed += 1
                elif act == "MUTE":
                    duration = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1800
                    antispam["punishment"] = "мут"
                    antispam["duration"] = duration
                    antispam["unit"] = "сек"
                    save_settings(chat_id_str)
                    executed += 1
                else:
                    errors.append(f"❌ {action_type}: ожидается BAN или MUTE")

            # ============ ПЕРСОНАЛЬНОСТЬ ============

            elif action_type == "PERSONALITY":
                p = value.strip().lower()
                if p in VALID_PERSONALITIES:
                    chat_st["personality"] = p
                    save_settings(chat_id_str)
                    executed += 1
                else:
                    errors.append(f"❌ Неизвестная персональность: {value}")

            elif action_type == "SET_CUSTOM":
                if not value.strip():
                    errors.append("❌ SET_CUSTOM: пустой текст")
                else:
                    chat_st["custom"] = value.strip()
                    chat_st["personality"] = "кастомный"
                    save_settings(chat_id_str)
                    executed += 1

            # ============ ПРАВИЛА И ПРИВЕТСТВИЕ ============

            elif action_type == "SET_RULES":
                if not value.strip():
                    errors.append("❌ SET_RULES: пустой текст правил")
                else:
                    chat_st["rules"] = value.strip()
                    save_settings(chat_id_str)
                    executed += 1

            elif action_type == "CLEAR_RULES":
                chat_st.pop("rules", None)
                save_settings(chat_id_str)
                executed += 1

            elif action_type == "SET_WELCOME":
                if not value.strip():
                    errors.append("❌ SET_WELCOME: пустой текст")
                else:
                    chat_st["welcome_message"] = value.strip()
                    save_settings(chat_id_str)
                    executed += 1

            elif action_type == "CLEAR_WELCOME":
                chat_st.pop("welcome_message", None)
                save_settings(chat_id_str)
                executed += 1

            # ============ СЛОУМОД ============

            elif action_type == "SLOWMODE":
                if not value.isdigit():
                    errors.append("❌ SLOWMODE: ожидается число секунд")
                    continue
                seconds = int(value)
                if seconds not in SLOWMODE_ALLOWED:
                    errors.append(f"❌ SLOWMODE: разрешены только значения {sorted(SLOWMODE_ALLOWED)}")
                    continue
                try:
                    await message.chat.set_slow_mode(slow_mode_delay=seconds)
                    executed += 1
                except Exception as e:
                    errors.append(f"❌ Слоумод: {e}")

            # ============ БАН ПОЛЬЗОВАТЕЛЯ ============
            # Формат: [ACTION:BAN:USER] / [ACTION:BAN:123456789] / [ACTION:BAN:@username]

            elif action_type == "BAN":
                target_id, target_name = await _resolve_target(message, value)
                if target_id is None:
                    errors.append("⚠️ BAN: укажи user_id, @username или сделай реплай")
                elif target_id == bot_id:
                    errors.append("❌ Не могу забанить себя")
                else:
                    try:
                        # Проверяем, не является ли цель администратором
                        target_member = await bot.get_chat_member(chat_id, target_id)
                        if target_member.status in ("creator", "administrator"):
                            errors.append(f"❌ Нельзя забанить администратора: {target_name}")
                        else:
                            await bot.ban_chat_member(chat_id, target_id)
                            log_chat_event(
                                chat_id,
                                f"БАН: цель {target_name} id={target_id} — инициатор {format_user_tg(message.from_user)}",
                            )
                            executed += 1
                    except Exception as e:
                        errors.append(f"❌ Бан {target_name}: {e}")

            # ============ МУТ ПОЛЬЗОВАТЕЛЯ ============
            # Формат: [ACTION:MUTE:USER:30:мин]
            #         [ACTION:MUTE:123456789:60:мин]
            #         [ACTION:MUTE:@username:1:час]

            elif action_type == "MUTE":
                # Парсим: первая часть — цель, вторая — длительность, третья — единица
                parts = _split_value(value, 3)
                target_raw = parts[0] if parts else "USER"
                duration_raw = parts[1] if len(parts) > 1 else "30"
                unit_raw = parts[2] if len(parts) > 2 else "мин"

                duration = int(duration_raw) if duration_raw.isdigit() else 30
                unit = unit_raw if unit_raw in TIME_UNIT_TO_SECONDS else "мин"
                total_seconds = _duration_to_seconds(duration, unit)

                target_id, target_name = await _resolve_target(message, target_raw)
                if target_id is None:
                    errors.append("⚠️ MUTE: укажи user_id, @username или сделай реплай")
                elif target_id == bot_id:
                    errors.append("❌ Не могу замутить себя")
                else:
                    try:
                        # Проверяем, не является ли цель администратором
                        target_member = await bot.get_chat_member(chat_id, target_id)
                        if target_member.status in ("creator", "administrator"):
                            errors.append(f"❌ Нельзя замутить администратора: {target_name}")
                        else:
                            until = datetime.now() + timedelta(seconds=total_seconds)
                            await bot.restrict_chat_member(
                                chat_id,
                                target_id,
                                ChatPermissions(
                                    can_send_messages=False,
                                    can_send_media_messages=False,
                                    can_send_other_messages=False,
                                    can_add_web_page_previews=False,
                                ),
                                until_date=until,
                            )
                            log_chat_event(
                                chat_id,
                                f"МУТ {duration}{unit}: цель {target_name} id={target_id} — инициатор {format_user_tg(message.from_user)}",
                            )
                            executed += 1
                    except Exception as e:
                        errors.append(f"❌ Мут {target_name}: {e}")

            # ============ КИК ============

            elif action_type == "KICK":
                target_id, target_name = await _resolve_target(message, value)
                if target_id is None:
                    errors.append("⚠️ KICK: укажи user_id, @username или сделай реплай")
                elif target_id == bot_id:
                    errors.append("❌ Не могу кикнуть себя")
                elif target_id == user_id:
                    errors.append("❌ Не буду кикать автора запроса")
                else:
                    try:
                        await bot.ban_chat_member(chat_id, target_id)
                        await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                        log_chat_event(
                            chat_id,
                            f"КИК: цель {target_name} id={target_id} — инициатор {format_user_tg(message.from_user)}",
                        )
                        executed += 1
                    except Exception as e:
                        errors.append(f"❌ Кик {target_name}: {e}")

            # ============ РАЗМУТ ============

            elif action_type == "UNMUTE":
                target_id, target_name = await _resolve_target(message, value)
                if target_id is None:
                    errors.append("⚠️ UNMUTE: укажи user_id, @username или сделай реплай")
                else:
                    try:
                        await bot.restrict_chat_member(
                            chat_id,
                            target_id,
                            ChatPermissions(
                                can_send_messages=True,
                                can_send_media_messages=True,
                                can_send_other_messages=True,
                                can_add_web_page_previews=True,
                            ),
                        )
                        log_chat_event(
                            chat_id,
                            f"РАЗМУТ: цель {target_name} id={target_id} — инициатор {format_user_tg(message.from_user)}",
                        )
                        executed += 1
                    except Exception as e:
                        errors.append(f"❌ Размут {target_name}: {e}")

            # ============ ВАРН ============

            elif action_type == "WARN":
                target_id, target_name = await _resolve_target(message, value)
                if target_id is None:
                    errors.append("⚠️ WARN: укажи user_id, @username или сделай реплай")
                elif target_id == bot_id:
                    errors.append("❌ Не могу варнить себя")
                elif target_id == user_id:
                    errors.append("❌ Нельзя варнить автора запроса")
                else:
                    chat_st.setdefault("warns", {})
                    warn_key = str(target_id)
                    cnt = chat_st["warns"].get(warn_key, 0) + 1
                    chat_st["warns"][warn_key] = cnt
                    save_settings(chat_id_str)
                    if cnt >= WARN_LIMIT:
                        try:
                            await bot.restrict_chat_member(
                                chat_id,
                                target_id,
                                ChatPermissions(can_send_messages=False),
                                until_date=datetime.now() + timedelta(hours=1),
                            )
                            chat_st["warns"][warn_key] = 0
                            save_settings(chat_id_str)
                            log_chat_event(
                                chat_id,
                                f"ВАРН→МУТ 1ч: цель {target_name} id={target_id} (было {WARN_LIMIT} варна) — выдал {format_user_tg(message.from_user)}",
                            )
                            executed += 1
                        except Exception as e:
                            errors.append(f"❌ Варн → мут {target_name}: {e}")
                    else:
                        log_chat_event(
                            chat_id,
                            f"ВАРН {cnt}/{WARN_LIMIT}: цель {target_name} id={target_id} — выдал {format_user_tg(message.from_user)}",
                        )
                        executed += 1

            # ============ УДАЛЕНИЕ / ПИН / АНПИН ============

            elif action_type == "DELETE":
                if not message.reply_to_message:
                    errors.append("⚠️ DELETE: сделай реплай на сообщение, которое нужно удалить")
                else:
                    try:
                        await bot.delete_message(chat_id, message.reply_to_message.message_id)
                        ru = message.reply_to_message.from_user
                        log_chat_event(
                            chat_id,
                            f"УДАЛЕНИЕ msg: автор сообщения {format_user_tg(ru)} — инициатор {format_user_tg(message.from_user)}",
                        )
                        executed += 1
                    except Exception as e:
                        errors.append(f"❌ Удаление: {e}")

            elif action_type == "PIN":
                if not message.reply_to_message:
                    errors.append("⚠️ PIN: сделай реплай на сообщение, которое нужно закрепить")
                else:
                    try:
                        await bot.pin_chat_message(
                            chat_id, message.reply_to_message.message_id, disable_notification=True
                        )
                        pm = message.reply_to_message
                        prev = (pm.text or pm.caption or "")[:100]
                        log_chat_event(
                            chat_id,
                            f"ЗАКРЕП: закрепил {format_user_tg(message.from_user)}; сообщение от {format_user_tg(pm.from_user)} «{prev}»",
                        )
                        executed += 1
                    except Exception as e:
                        errors.append(f"❌ Закреп: {e}")

            elif action_type == "UNPIN":
                try:
                    if message.reply_to_message:
                        await bot.unpin_chat_message(chat_id, message.reply_to_message.message_id)
                        log_chat_event(
                            chat_id,
                            f"ОТКРЕП: конкретное сообщение — инициатор {format_user_tg(message.from_user)}",
                        )
                    else:
                        await bot.unpin_all_chat_messages(chat_id)
                        log_chat_event(
                            chat_id,
                            f"ОТКРЕП ВСЕ — инициатор {format_user_tg(message.from_user)}",
                        )
                    executed += 1
                except Exception as e:
                    errors.append(f"❌ Открепление: {e}")

            # ============ ОЧИСТКИ ============

            elif action_type == "CLEAR_HISTORY":
                chat_histories.pop(chat_id, None)
                executed += 1

            elif action_type == "CLEAR_LOGS":
                try:
                    from ..core.logging_setup import SHORT_LOG_PATH
                    import os
                    if os.path.exists(SHORT_LOG_PATH):
                        open(SHORT_LOG_PATH, "w", encoding="utf-8").write("")
                    executed += 1
                except Exception as e:
                    errors.append(f"❌ Очистка логов: {e}")

            elif action_type == "CLEAR_WARNS":
                if not value.strip():
                    chat_st.pop("warns", None)
                    save_settings(chat_id_str)
                    executed += 1
                else:
                    target_id, _ = await _resolve_target(message, value)
                    if target_id is None:
                        errors.append("⚠️ CLEAR_WARNS: цель не найдена")
                    else:
                        chat_st.setdefault("warns", {}).pop(str(target_id), None)
                        save_settings(chat_id_str)
                        executed += 1

            # ============ AI-ПРОВАЙДЕР / МОДЕЛЬ ============

            elif action_type == "SET_PROVIDER":
                if not is_owner and user_id != _get_owner_id():
                    errors.append("❌ Сменить провайдера может только владелец чата или бота")
                    continue
                from .g4f_client import g4f_client
                parts = _split_value(value, 2)
                name = parts[0] if parts else ""
                model = parts[1] if len(parts) > 1 else None
                ok, msg = g4f_client.set_active_provider(name, model)
                if ok:
                    executed += 1
                else:
                    errors.append(f"❌ {msg}")

            elif action_type == "SET_MODEL":
                if not is_owner and user_id != _get_owner_id():
                    errors.append("❌ Сменить модель может только владелец чата или бота")
                    continue
                from .g4f_client import g4f_client
                ok, msg = g4f_client.set_active_model(value.strip())
                if ok:
                    executed += 1
                else:
                    errors.append(f"❌ {msg}")

            # ============ ПОИСК ФОТО ============

            elif action_type == "PHOTO":
                from .ai_module import fetch_image_pixabay
                from aiogram.types import BufferedInputFile
                q = value.strip()
                if not q:
                    errors.append("❌ PHOTO: пустой запрос")
                    continue
                img = await fetch_image_pixabay(q)
                if not img:
                    errors.append(f"❌ Фото по запросу '{q}' не найдено")
                else:
                    try:
                        await message.answer_photo(
                            photo=BufferedInputFile(img, filename="image.jpg")
                        )
                        executed += 1
                    except Exception as e:
                        errors.append(f"❌ Отправка фото: {e}")

            # ============ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ ============

            elif action_type == "GENIMAGE":
                from ..handlers.image_gen import generate_image_pollinations
                from aiogram.types import BufferedInputFile
                prompt = value.strip()
                if not prompt:
                    errors.append("❌ GENIMAGE: пустое описание")
                    continue
                try:
                    await bot.send_chat_action(chat_id, action="upload_photo")
                    img_bytes = await generate_image_pollinations(prompt)
                    if not img_bytes:
                        errors.append(f"❌ Не удалось сгенерировать изображение по описанию: '{prompt[:80]}'")
                    else:
                        await message.answer_photo(
                            photo=BufferedInputFile(img_bytes, filename="generated.jpg"),
                            caption=f"🖼 {prompt[:200]}",
                        )
                        executed += 1
                except Exception as e:
                    errors.append(f"❌ Генерация изображения: {e}")

            else:
                errors.append(f"❌ Неизвестное действие: {action_type}")

        except Exception as e:
            logger.exception(f"Ошибка при выполнении {action_type}: {e}")
            errors.append(f"❌ {action_type}: {e}")

    # По правилу: успехи — молча. В чат уходят только ошибки/отказы.
    if errors:
        if clean_text:
            clean_text += "\n\n" + "\n".join(errors)
        else:
            clean_text = "\n".join(errors)

    return clean_text, executed > 0


def _get_owner_id() -> int:
    try:
        from ..core.config import OWNER_ID
        return int(OWNER_ID or 0)
    except Exception:
        return 0
