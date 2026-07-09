import asyncio
import html
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from aiogram import Router, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from ..core.loader import bot
from ..core.config import OWNER_ID, SUPPORT_ID
from ..storage.state import chat_histories, group_users, user_last_seen, settings, save_settings
from ..storage.message_logs import get_chat_messages, get_chat_title, get_known_chats
from ..storage.moderators import (
    is_moderator,
    all_ids as mod_all_ids,
    reply_title_for,
    get_level,
    LEVEL_NAMES,
)
from ..services import chat_ai_router as _car

logger = logging.getLogger(__name__)
router = Router()

# Статусы, при которых пользователь фактически "не в бане"
_NOT_KICKED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.RESTRICTED,
    ChatMemberStatus.LEFT,
}

# Все булевы права, которые принимает promote_chat_member (для групп и супергрупп)
_ADMIN_RIGHT_FIELDS = (
    "can_manage_chat",
    "can_change_info",
    "can_post_messages",
    "can_edit_messages",
    "can_delete_messages",
    "can_invite_users",
    "can_restrict_members",
    "can_pin_messages",
    "can_promote_members",
    "can_manage_video_chats",
    "can_manage_topics",
)


# ============================================================
#  ГЛОБАЛЬНЫЙ AI (!глобал_ии)
#  Реализовано полностью здесь, без правок других файлов.
#  Через monkey-patch модуля chat_ai_router — поэтому ВСЕ функции,
#  работающие на ИИ (антиспам, модер, медиа-ии и т.д.), автоматически
#  переключаются на глобальный ключ, ничего не ломая.
# ============================================================
_GLOBAL_AI_FILE = Path(__file__).resolve().parent.parent.parent / "global_ai.json"

_global_ai_state = {
    "enabled":        False,
    "endpoint":       "",
    "api_key":        "",
    "model":          "",
    "blocked_chats":  [],   # чаты без доступа к ИИ
    "blocked_users":  [],   # пользователи без доступа к ИИ
}


def _load_global_ai():
    try:
        if _GLOBAL_AI_FILE.exists():
            with open(_GLOBAL_AI_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _global_ai_state["enabled"]   = bool(data.get("enabled", False))
                _global_ai_state["endpoint"]  = str(data.get("endpoint", "") or "")
                _global_ai_state["api_key"]   = str(data.get("api_key", "") or "")
                _global_ai_state["model"]     = str(data.get("model", "") or "")
                
                # Поддержка старого формата (backward compatibility)
                blocked = data.get("blocked_ids") or []
                blocked_chats = data.get("blocked_chats") or []
                blocked_users = data.get("blocked_users") or []
                
                def _clean_ids(lst):
                    clean = []
                    for x in lst:
                        try:
                            clean.append(int(x))
                        except Exception:
                            pass
                    return list(set(clean))
                
                _global_ai_state["blocked_chats"] = _clean_ids(blocked_chats or blocked)
                _global_ai_state["blocked_users"] = _clean_ids(blocked_users)
    except Exception as e:
        logger.error(f"[GLOBAL-AI] load fail: {e}")


def _save_global_ai():
    try:
        _GLOBAL_AI_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GLOBAL_AI_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_global_ai_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _GLOBAL_AI_FILE)
    except Exception as e:
        logger.error(f"[GLOBAL-AI] save fail: {e}")


_load_global_ai()


def _global_ai_configured() -> bool:
    return bool(
        (_global_ai_state.get("endpoint") or "").strip()
        and (_global_ai_state.get("model") or "").strip()
    )


def _is_chat_blocked_for_ai(chat_id) -> bool:
    """Проверка, заблокирован ли чат"""
    if chat_id is None:
        return False
    blocked = _global_ai_state.get("blocked_chats") or []
    try:
        return int(chat_id) in blocked
    except (TypeError, ValueError):
        return False


def _is_user_blocked_for_ai(user_id) -> bool:
    """Проверка, заблокирован ли пользователь"""
    if user_id is None:
        return False
    blocked = _global_ai_state.get("blocked_users") or []
    try:
        return int(user_id) in blocked
    except (TypeError, ValueError):
        return False


def _is_blocked_for_ai(chat_id=None, user_id=None) -> tuple[bool, str]:
    """
    Проверка блокировки. Возвращает (заблокирован, тип_блокировки).
    Тип: 'none', 'chat', 'user'
    """
    if _is_chat_blocked_for_ai(chat_id):
        return True, "chat"
    if _is_user_blocked_for_ai(user_id):
        return True, "user"
    return False, "none"


# --- сохраняем оригиналы один раз (защита от повторной загрузки модуля) ---
if not getattr(_car, "_global_ai_patched", False):
    _car._orig_generate_for_chat = _car.generate_for_chat
    _car._orig_get_chat_provider = _car.get_chat_provider
    _car._orig_get_chat_model    = _car.get_chat_model
    _car._orig_is_ai_enabled     = _car.is_ai_enabled
    _car._global_ai_patched      = True


async def _patched_generate_for_chat(chat_id, messages,
                                     max_tokens: int = 800,
                                     temperature: float = 0.7,
                                     user_id: int = None):
    if not _global_ai_state["enabled"]:
        return await _car._orig_generate_for_chat(
            chat_id, messages, max_tokens=max_tokens, temperature=temperature
        )
    
    is_blocked, block_type = _is_blocked_for_ai(chat_id=chat_id, user_id=user_id)
    if is_blocked:
        return None
    
    if _global_ai_configured():
        endpoint = _global_ai_state["endpoint"].strip()
        api_key  = _global_ai_state["api_key"].strip()
        model    = _global_ai_state["model"].strip()
        try:
            return await _car._post_openai_compat(
                endpoint, api_key, model, messages,
                max_tokens=max_tokens, temperature=temperature,
            )
        except Exception as e:
            logger.warning(f"[GLOBAL-AI] call fail: {e}")
            return None
    
    # Глобальный включён, но ключ не задан → fallback на оригинал
    return await _car._orig_generate_for_chat(
        chat_id, messages, max_tokens=max_tokens, temperature=temperature
    )


def _patched_get_chat_provider(chat_id):
    if _global_ai_state["enabled"] and _global_ai_configured():
        return "custom"
    return _car._orig_get_chat_provider(chat_id)


def _patched_get_chat_model(chat_id):
    if _global_ai_state["enabled"] and _global_ai_configured():
        return _global_ai_state["model"].strip()
    return _car._orig_get_chat_model(chat_id)


def _patched_is_ai_enabled(chat_id, user_id: int = None):
    # Per-chat !ии выкл всегда имеет приоритет над глобальным ИИ
    if not _car._orig_is_ai_enabled(chat_id):
        return False
    if _global_ai_state["enabled"]:
        is_blocked, _ = _is_blocked_for_ai(chat_id=chat_id, user_id=user_id)
        return not is_blocked
    return True


# Патчим модуль
_car.generate_for_chat = _patched_generate_for_chat
_car.get_chat_provider = _patched_get_chat_provider
_car.get_chat_model    = _patched_get_chat_model
_car.is_ai_enabled     = _patched_is_ai_enabled


# Догоняем уже импортированные ссылки в других модулях
def _repatch_imported_refs():
    targets = {
        "generate_for_chat": _patched_generate_for_chat,
        "get_chat_provider": _patched_get_chat_provider,
        "get_chat_model":    _patched_get_chat_model,
        "is_ai_enabled":     _patched_is_ai_enabled,
    }
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod is _car:
            continue
        if not mod_name.startswith("bot."):
            continue
        for attr, new_fn in targets.items():
            if hasattr(mod, attr):
                try:
                    if getattr(mod, attr) is not new_fn:
                        setattr(mod, attr, new_fn)
                except Exception:
                    pass


_repatch_imported_refs()


def _parse_quoted_args(s: str):
    """Парсит строку с кавычками:  "a b" "c"  d  → ['a b', 'c', 'd']"""
    out, buf, in_q = [], [], False
    for ch in s:
        if ch == '"':
            if in_q:
                out.append("".join(buf)); buf = []; in_q = False
            else:
                if buf:
                    out.extend("".join(buf).split())
                    buf = []
                in_q = True
        else:
            buf.append(ch)
    if buf:
        if in_q:
            out.append("".join(buf))
        else:
            out.extend("".join(buf).split())
    return [x for x in out if x != ""]


# ============================================================
#  ПРАВА НА DM-КОМАНДЫ (по уровням модераторов)
#  1 — хелпер, 2 — модер, 3 — админ, 4 — владелец
# ============================================================
DM_COMMAND_MIN_LEVEL: dict[str, int] = {
    # читать / смотреть — модер и выше
    "!список_соо":     2,
    "!чаты":           2,
    "!участники":      2,
    "!кто_в_сети":     2,
    "!сообщения":      2,
    "!получить_айди":  2,

    # банить / писать в чаты — админ и выше
    "!бан":            3,
    "!разбан":         3,
    "!массбан":        3,
    "!ссылка":         3,
    "!реплай":         3,

    # тяжёлая артиллерия — только владелец
    "!снос_чата":      4,
    "!глобалсоо":      4,
    "!админ":          4,
    "!снять_админ":    4,
    "!глобал_ии":      4,
    "!глобал_грог":    4,
}
# Команды, о применении которых не-владельцем стоит уведомлять владельца
_SENSITIVE_DM_CMDS = {
    "!бан", "!разбан", "!массбан", "!ссылка", "!реплай",
    "!снос_чата", "!глобалсоо", "!глобал_ии", "!глобал_грог",
}


def _can_use_dm(user_id: int, canonical_cmd: str) -> bool:
    """OWNER — всё. Остальным — по DM_COMMAND_MIN_LEVEL. Неизвестные — только OWNER."""
    if user_id == OWNER_ID:
        return True
    min_lvl = DM_COMMAND_MIN_LEVEL.get(canonical_cmd)
    if min_lvl is None:
        return False
    return get_level(user_id) >= min_lvl


def _actor_role(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "владелец"
    return LEVEL_NAMES.get(get_level(user_id), "пользователь")


def _is_protected_target(target_user_id: int) -> bool:
    """Защита от бана: владелец и любой модератор (lvl >= 1)."""
    if target_user_id == OWNER_ID:
        return True
    return get_level(target_user_id) >= 1


# ============================================================
#  АНТИ-РАЗБАН
# ============================================================
@router.chat_member()
async def track_unban_by_admin(update: ChatMemberUpdated):
    """Если админ вручную разбанил того, кого банил бот — баним обратно."""
    try:
        old_status = update.old_chat_member.status
        new_status = update.new_chat_member.status
    except Exception:
        return

    if old_status != ChatMemberStatus.KICKED:
        return
    if new_status not in _NOT_KICKED_STATUSES:
        return

    user_id = update.new_chat_member.user.id
    chat_id = update.chat.id
    cid = str(chat_id)

    bot_banned = settings.get(cid, {}).get("bot_banned", {})
    if str(user_id) not in bot_banned:
        return

    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        logger.warning(f"[AUTO-REBAN] {user_id} снова забанен в {chat_id} (был разбанен админом)")
        try:
            actor = update.from_user
            actor_label = f"@{actor.username}" if actor and actor.username else (actor.full_name if actor else "?")
            await bot.send_message(
                OWNER_ID,
                f"🛡 <b>AUTO-REBAN</b>\n"
                f"Чат: <code>{chat_id}</code>\n"
                f"Юзер: <code>{user_id}</code>\n"
                f"Пытался разбанить: {html.escape(actor_label)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[AUTO-REBAN] не удалось вернуть бан {user_id} в {chat_id}: {e}")


# ============================================================
#  РЕАЛЬНЫЙ ХЕНДЛЕР ИЗМЕНЕНИЯ СТАТУСА САМОГО БОТА
# ============================================================
@router.my_chat_member()
async def on_bot_status_change(update: ChatMemberUpdated):
    """Логируем и уведомляем владельца об изменениях статуса самого бота."""
    try:
        old_status = update.old_chat_member.status
        new_status = update.new_chat_member.status
    except Exception:
        return

    chat = update.chat
    actor = update.from_user
    actor_label = "?"
    if actor:
        actor_label = f"@{actor.username}" if actor.username else actor.full_name
        actor_label += f" [{actor.id}]"

    chat_title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or "(private)"
    chat_line = f"{html.escape(chat_title)} [<code>{chat.id}</code>]"

    # Человеко-читаемая причина
    reason = None
    if old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        reason = "➕ Бота добавили в чат"
    elif new_status == ChatMemberStatus.KICKED:
        reason = "⛔ Бота кикнули/забанили"
    elif new_status == ChatMemberStatus.LEFT:
        reason = "🚪 Бот покинул чат (или был удалён)"
    elif old_status == ChatMemberStatus.MEMBER and new_status == ChatMemberStatus.ADMINISTRATOR:
        reason = "⬆️ Боту выдали админку"
    elif old_status == ChatMemberStatus.ADMINISTRATOR and new_status == ChatMemberStatus.MEMBER:
        reason = "⬇️ У бота сняли админку"
    else:
        reason = f"ℹ️ Статус: {old_status} → {new_status}"

    logger.info(f"[MY_CHAT_MEMBER] chat={chat.id} ({chat_title}) {old_status} -> {new_status} by {actor_label}")

    try:
        await bot.send_message(
            OWNER_ID,
            f"🤖 <b>Изменение статуса бота</b>\n"
            f"Чат: {chat_line}\n"
            f"Кто: {html.escape(actor_label)}\n"
            f"{reason}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug(f"[MY_CHAT_MEMBER] notify owner failed: {e}")


# ============================================================
#  ПЕРИОДИЧЕСКАЯ ПРОВЕРКА (safety net)
# ============================================================
_reban_loop_started = False


async def _periodic_reban_check(interval_sec: int = 300):
    while True:
        try:
            snapshot = {cid: dict(v.get("bot_banned", {})) for cid, v in settings.items()}
            for cid, banned in snapshot.items():
                if not banned:
                    continue
                try:
                    chat_id = int(cid)
                except ValueError:
                    continue
                for uid_str in banned.keys():
                    try:
                        uid = int(uid_str)
                    except ValueError:
                        continue
                    try:
                        member = await bot.get_chat_member(chat_id, uid)
                        if member.status != ChatMemberStatus.KICKED:
                            await bot.ban_chat_member(chat_id=chat_id, user_id=uid)
                            logger.warning(
                                f"[PERIODIC-REBAN] {uid} вернулся в {chat_id} (status={member.status}) — бан восстановлен"
                            )
                    except TelegramBadRequest as e:
                        logger.debug(f"[PERIODIC-REBAN] skip {cid}/{uid_str}: {e}")
                    except Exception as e:
                        logger.debug(f"[PERIODIC-REBAN] error {cid}/{uid_str}: {e}")
                    await asyncio.sleep(0.05)
        except Exception as e:
            logger.exception(f"[PERIODIC-REBAN] loop error: {e}")
        await asyncio.sleep(interval_sec)


def ensure_reban_loop_started():
    global _reban_loop_started
    if _reban_loop_started:
        return
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_periodic_reban_check())
        _reban_loop_started = True
        logger.info("[PERIODIC-REBAN] loop started")
    except RuntimeError:
        pass


# ============================================================
#  PRIVATE HANDLER
# ============================================================


@router.message(F.chat.type == "private")
async def private_handler(message: Message):
    ensure_reban_loop_started()

    # Поддерживаем !связь как в text, так и в caption (фото/видео/документ)
    text = (message.text or message.caption or "").strip()
    uid = message.from_user.id

    # Определяем, есть ли в сообщении медиа-контент
    has_media = bool(
        message.photo or message.video or message.animation or message.document
        or message.audio or message.voice or message.video_note or message.sticker
    )

    # ----- утилита: лог любой команды от не-владельца OWNER'у -----
    async def _notify_owner_cmd(canonical: str, *, status: str = "ok", extra: str = ""):
        """
        Отправляет OWNER_ID уведомление о любой команде от не-владельца.
        status: 'ok' — команда выполнена, 'denied' — нет доступа, 'unknown' — неизвестная команда.
        """
        if uid == OWNER_ID:
            return
        try:
            who = f"@{message.from_user.username}" if message.from_user.username \
                  else (message.from_user.full_name or str(uid))
            role = _actor_role(uid)
            lvl = get_level(uid)
            preview = html.escape((text or "")[:500])
            status_tag = {
                "ok":      "✅ выполнена",
                "denied":  "⛔ отказ (нет доступа)",
                "unknown": "❓ неизвестная команда",
            }.get(status, status)
            tail = f"\nℹ️ {html.escape(extra)}" if extra else ""
            await bot.send_message(
                OWNER_ID,
                f"🛡 <b>MOD-LOG</b> — {status_tag}\n"
                f"Кто: {html.escape(who)} [<code>{uid}</code>] — "
                f"<b>{html.escape(role)}</b> (lvl <b>{lvl}</b>)\n"
                f"Команда: <code>{html.escape(canonical)}</code>\n"
                f"Текст: <code>{preview}</code>"
                f"{tail}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.debug(f"[MOD-LOG] notify owner fail: {e}")

    # ========================================================
    #  !связь — СТРОГАЯ проверка команды
    # ========================================================
    low = text.lower()
    is_svyaz_cmd = (
        low == "!связь"
        or low.startswith("!связь ")
        or low.startswith("!связь\n")
        or low.startswith("!связь\t")
    )

    if is_svyaz_cmd:
        parts = text.split(maxsplit=1)
        if len(parts) < 2 and not has_media:
            return await message.reply("❗ Использование: !связь и текст вопроса (можно с фото/видео/стикером)")
        msg_text = parts[1] if len(parts) > 1 else "(без текста)"
        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        media_tag = ""
        if has_media:
            if message.photo:
                media_tag = " 📷 [фото прикреплено]"
            elif message.video:
                media_tag = " 🎥 [видео прикреплено]"
            elif message.animation:
                media_tag = " 🎞 [GIF прикреплён]"
            elif message.document:
                media_tag = " 📎 [документ прикреплён]"
            elif message.audio:
                media_tag = " 🎵 [аудио прикреплено]"
            elif message.voice:
                media_tag = " 🎤 [голосовое прикреплено]"
            elif message.video_note:
                media_tag = " 📹 [кружок прикреплён]"
            elif message.sticker:
                media_tag = " 🎨 [стикер прикреплён]"
        forward_text = f"📩 Сообщение от {username} ({uid}):{media_tag}\n\n{msg_text}"
        try:
            await bot.send_message(SUPPORT_ID, forward_text)
            if has_media:
                try:
                    await bot.forward_message(
                        chat_id=SUPPORT_ID,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                    )
                except Exception:
                    try:
                        await bot.copy_message(
                            chat_id=SUPPORT_ID,
                            from_chat_id=message.chat.id,
                            message_id=message.message_id,
                        )
                    except Exception as e_copy:
                        logger.error(f"[SUPPORT-RELAY] copy/forward media fail: {e_copy}")
        except Exception as e:
            return await message.reply(f"❌ Ошибка: {e}")

        # Если !связь нажал модератор — тоже уведомим OWNER
        if uid != OWNER_ID and (is_moderator(uid, 1) or uid == SUPPORT_ID):
            await _notify_owner_cmd("!связь", status="ok")

        return await message.answer("✅ Сообщение отправлено в поддержку.")

    # ========================================================
    #  !ответ — для саппорта/модов
    # ========================================================
    if text.startswith("!ответ") and (uid == SUPPORT_ID or uid == OWNER_ID or is_moderator(uid, 1)):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            return await message.reply("❗ Формат: !ответ <user_id> <сообщение>")
        try:
            target_user_id = int(parts[1])
            reply_text = parts[2]
            title = reply_title_for(uid, OWNER_ID, SUPPORT_ID)
            await bot.send_message(target_user_id, f"{title}\n\n{reply_text}")
            # лог OWNER'у
            if uid != OWNER_ID:
                await _notify_owner_cmd(
                    "!ответ",
                    status="ok",
                    extra=f"→ user_id {target_user_id}",
                )
            return await message.reply("✅ Ответ отправлен.")
        except Exception as e:
            if uid != OWNER_ID:
                await _notify_owner_cmd("!ответ", status="denied", extra=f"ошибка: {e}")
            return await message.reply(f"❌ Ошибка: {e}")

    # ========================================================
    #  DM-команды с уровневым доступом
    # ========================================================
    if text.startswith(("!", ".")):
        low = text.lower()

        DM_HANDLERS = [
            (("!список_соо", ".список_соо"),                           "!список_соо",    list_chat_messages),
            (("!реплай", ".реплай"),                                   "!реплай",        reply_to_group_message),
            (("!глобалсоо", ".глобалсоо",
              "!глобал_соо", ".глобал_соо"),                           "!глобалсоо",     broadcast_global),
            (("!глобал_ии", ".глобал_ии",
              "!глобал_ai", ".глобал_ai"),                             "!глобал_ии",     global_ai_cmd),
            (("!глобал_грог", ".глобал_грог"),                        "!глобал_грог",   global_groq_cmd),
            (("!чаты", ".чаты"),                                       "!чаты",          list_logged_chats),
            (("!получить_айди", ".получить_айди"),                     "!получить_айди", get_seen_chat_ids),
            (("!сообщения", ".сообщения"),                             "!сообщения",     send_group_message_private),
            (("!снос_чата", ".снос_чата"),                             "!снос_чата",     nuke_chat_private),
            (("!ссылка", ".ссылка"),                                   "!ссылка",        gen_invite_link_private),
            (("!массбан", ".массбан"),                                 "!массбан",       mass_ban_private),
            (("!снять_админ", ".снять_админ"),                         "!снять_админ",   demote_admin_private),
            (("!админ", ".админ"),                                     "!админ",         promote_admin_private),
            (("!бан", ".бан"),                                         "!бан",           ban_user_private),
            (("!разбан", ".разбан"),                                   "!разбан",        unban_user_private),
            (("!кто_в_сети", ".кто_в_сети"),                           "!кто_в_сети",    who_is_online),
            (("!участники", ".участники"),                             "!участники",     list_seen_group_members),
            (("!выход", ".выход"),                                     "!выход",         leave_chat_private),
        ]

        matched = None
        for prefixes, canonical, handler in DM_HANDLERS:
            if low.startswith(prefixes):
                matched = (canonical, handler)
                break

        if matched is not None:
            canonical, handler = matched

            if not _can_use_dm(uid, canonical):
                lvl = get_level(uid)
                # лог попытки без доступа
                await _notify_owner_cmd(canonical, status="denied",
                                        extra=f"уровень пользователя: {lvl}")
                try:
                    await message.answer(
                        f"⛔ У тебя нет доступа к <code>{html.escape(canonical)}</code>.\n"
                        f"Твой уровень: <b>{lvl}</b> ({html.escape(LEVEL_NAMES.get(lvl, '—'))}).",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

            # ⭐ Лог OWNER'у о ЛЮБОЙ команде от не-владельца
            await _notify_owner_cmd(canonical, status="ok")

            logger.info(f"[DM-CMD] {canonical} by {uid} (lvl={get_level(uid)})")
            return await handler(message)

        # Неизвестная команда — логируем, если её ввёл модератор/саппорт
        if uid == OWNER_ID:
            return
        if is_moderator(uid, 1) or uid == SUPPORT_ID:
            await _notify_owner_cmd(
                (text.split() or ["?"])[0],
                status="unknown",
            )
        return

    # ========================================================
    #  Обычные сообщения — в поддержку НЕ релеим.
    #  В поддержку уходит СТРОГО только команда !связь (обработано выше).
    # ========================================================
    if uid == OWNER_ID:
        return

    try:
        return await message.answer(
            "ℹ️ Чтобы написать в поддержку, используй команду:\n"
            "<code>!связь &lt;текст вопроса&gt;</code>\n\n"
            "Можно прикрепить фото/видео/документ/голосовое к сообщению с этой командой.",
            parse_mode="HTML",
        )
    except Exception:
        return


# ============================================================
#  КОМАНДА !глобал_ии
# ============================================================
async def global_ai_cmd(message: Message):
    """!глобал_ии  вкл | выкл
       !глобал_ии  ключ "endpoint" "api_key" "model"
       !глобал_ии  ограничить чат <chat_id>
       !глобал_ии  ограничить юзер <user_id>
       !глобал_ии  разрешить чат <chat_id>
       !глобал_ии  разрешить юзер <user_id>
       !глобал_ии  статус
    """
    text = (message.text or "").strip()
    tail = ""
    for prefix in ("!глобал_ии", ".глобал_ии", "!глобал_ai", ".глобал_ai"):
        if text.lower().startswith(prefix):
            tail = text[len(prefix):].strip()
            break

    if not tail:
        return await message.answer(
            "🌐 <b>Глобальный ИИ</b>\n\n"
            "<code>!глобал_ии вкл</code> — включить (все чаты идут через глобальный ключ)\n"
            "<code>!глобал_ии выкл</code> — выключить (вернутся настройки чатов)\n"
            "<code>!глобал_ии ключ \"endpoint\" \"api_key\" \"model\"</code> — задать ключ\n"
            "<code>!глобал_ии ограничить чат &lt;chat_id&gt;</code> — лишить ИИ чат\n"
            "<code>!глобал_ии ограничить юзер &lt;user_id&gt;</code> — лишить ИИ пользователя\n"
            "<code>!глобал_ии разрешить чат &lt;chat_id&gt;</code> — разрешить чату\n"
            "<code>!глобал_ии разрешить юзер &lt;user_id&gt;</code> — разрешить пользователю\n"
            "<code>!глобал_ии статус</code> — текущее состояние",
            parse_mode="HTML",
        )

    parts = tail.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    # ── вкл / выкл ────────────────────────────────────────────────
    if sub in ("вкл", "on", "enable"):
        _global_ai_state["enabled"] = True
        _save_global_ai()
        cfg = "✅ настроен" if _global_ai_configured() else "⚠️ ключ не задан (используй !глобал_ии ключ ...)"
        return await message.answer(
            f"🌐 Глобальный ИИ <b>ВКЛЮЧЁН</b>.\n"
            f"Состояние ключа: {cfg}\n"
            f"Заблокировано чатов: <b>{len(_global_ai_state['blocked_chats'])}</b>\n"
            f"Заблокировано юзеров: <b>{len(_global_ai_state['blocked_users'])}</b>",
            parse_mode="HTML",
        )

    if sub in ("выкл", "off", "disable"):
        _global_ai_state["enabled"] = False
        _save_global_ai()
        return await message.answer(
            "🌐 Глобальный ИИ <b>ВЫКЛЮЧЕН</b>.\n"
            "Настроенные чаты вернулись к своим ключам, "
            "у ненастроенных ИИ снова недоступен.",
            parse_mode="HTML",
        )

    # ── ключ ──────────────────────────────────────────────────────
    if sub in ("ключ", "key"):
        args = _parse_quoted_args(rest)
        if len(args) < 3:
            return await message.answer(
                "⚠️ Использование:\n"
                "<code>!глобал_ии ключ \"endpoint\" \"api_key\" \"model\"</code>\n\n"
                "Пример:\n"
                "<code>!глобал_ии ключ \"https://api.openai.com/v1/chat/completions\" "
                "\"sk-xxxxx\" \"gpt-4o-mini\"</code>",
                parse_mode="HTML",
            )
        endpoint, api_key, model = args[0], args[1], args[2]
        if not endpoint.startswith(("http://", "https://")):
            return await message.answer("❌ endpoint должен начинаться с http:// или https://")
        _global_ai_state["endpoint"] = endpoint.strip()
        _global_ai_state["api_key"]  = api_key.strip()
        _global_ai_state["model"]    = model.strip()
        _save_global_ai()
        masked = api_key[:6] + "…" + api_key[-4:] if len(api_key) > 12 else "***"
        status = "ВКЛЮЧЁН" if _global_ai_state["enabled"] else "выключен"
        return await message.answer(
            f"✅ Глобальный ключ сохранён.\n"
            f"• endpoint: <code>{html.escape(endpoint)}</code>\n"
            f"• model: <code>{html.escape(model)}</code>\n"
            f"• key: <code>{html.escape(masked)}</code>\n"
            f"Состояние: <b>{status}</b>",
            parse_mode="HTML",
        )

    # ── ограничить ────────────────────────────────────────────────
    if sub in ("ограничить", "block", "deny", "ban"):
        args = _parse_quoted_args(rest)
        if len(args) < 2:
            return await message.answer(
                "⚠️ Использование: <code>!глобал_ии ограничить чат &lt;chat_id&gt;</code>\n"
                "или <code>!глобал_ии ограничить юзер &lt;user_id&gt;</code>",
                parse_mode="HTML",
            )
        target_type = args[0].lower()
        if target_type not in ("чат", "chat", "юзер", "user", "user_id", "chat_id"):
            return await message.answer(
                "❌ Первый аргумент должен быть 'чат' или 'юзер'",
                parse_mode="HTML",
            )
        try:
            tid = int(args[1])
        except ValueError:
            return await message.answer("❌ ID должен быть числом.")
        
        if target_type in ("чат", "chat", "chat_id"):
            if tid not in _global_ai_state["blocked_chats"]:
                _global_ai_state["blocked_chats"].append(tid)
                _save_global_ai()
            return await message.answer(
                f"🚫 Чат <code>{tid}</code> лишён доступа к ИИ.",
                parse_mode="HTML",
            )
        else:  # юзер
            if tid not in _global_ai_state["blocked_users"]:
                _global_ai_state["blocked_users"].append(tid)
                _save_global_ai()
            return await message.answer(
                f"🚫 Пользователь <code>{tid}</code> лишён доступа к ИИ.",
                parse_mode="HTML",
            )

    # ── разрешить ─────────────────────────────────────────────────
    if sub in ("разрешить", "allow", "unblock", "unban"):
        args = _parse_quoted_args(rest)
        if len(args) < 2:
            return await message.answer(
                "⚠️ Использование: <code>!глобал_ии разрешить чат &lt;chat_id&gt;</code>\n"
                "или <code>!глобал_ии разрешить юзер &lt;user_id&gt;</code>",
                parse_mode="HTML",
            )
        target_type = args[0].lower()
        if target_type not in ("чат", "chat", "юзер", "user", "user_id", "chat_id"):
            return await message.answer(
                "❌ Первый аргумент должен быть 'чат' или 'юзер'",
                parse_mode="HTML",
            )
        try:
            tid = int(args[1])
        except ValueError:
            return await message.answer("❌ ID должен быть числом.")
        
        if target_type in ("чат", "chat", "chat_id"):
            if tid in _global_ai_state["blocked_chats"]:
                _global_ai_state["blocked_chats"].remove(tid)
                _save_global_ai()
                return await message.answer(
                    f"✅ Чат <code>{tid}</code> снова имеет доступ к ИИ.",
                    parse_mode="HTML",
                )
            return await message.answer(
                f"ℹ️ Чат <code>{tid}</code> и так не был в блок-листе.",
                parse_mode="HTML",
            )
        else:  # юзер
            if tid in _global_ai_state["blocked_users"]:
                _global_ai_state["blocked_users"].remove(tid)
                _save_global_ai()
                return await message.answer(
                    f"✅ Пользователь <code>{tid}</code> снова имеет доступ к ИИ.",
                    parse_mode="HTML",
                )
            return await message.answer(
                f"ℹ️ Пользователь <code>{tid}</code> и так не был в блок-листе.",
                parse_mode="HTML",
            )

    # ── статус ────────────────────────────────────────────────────
    if sub in ("статус", "status", "инфо", "info"):
        st = _global_ai_state
        masked = "—"
        if st["api_key"]:
            masked = st["api_key"][:6] + "…" + st["api_key"][-4:] if len(st["api_key"]) > 12 else "***"
        
        blocked_chats_part = ""
        if st["blocked_chats"]:
            blocked_chats_part = "\n  Чаты: " + ", ".join(f"<code>{i}</code>" for i in st["blocked_chats"][:10])
            if len(st["blocked_chats"]) > 10:
                blocked_chats_part += f" ... и ещё {len(st['blocked_chats']) - 10}"
        
        blocked_users_part = ""
        if st["blocked_users"]:
            blocked_users_part = "\n  Юзеры: " + ", ".join(f"<code>{i}</code>" for i in st["blocked_users"][:10])
            if len(st["blocked_users"]) > 10:
                blocked_users_part += f" ... и ещё {len(st['blocked_users']) - 10}"
        
        return await message.answer(
            f"🌐 <b>Глобальный ИИ</b>\n"
            f"• Состояние: <b>{'ВКЛЮЧЁН' if st['enabled'] else 'выключен'}</b>\n"
            f"• endpoint: <code>{html.escape(st['endpoint'] or '—')}</code>\n"
            f"• model: <code>{html.escape(st['model'] or '—')}</code>\n"
            f"• key: <code>{html.escape(masked)}</code>\n"
            f"• Заблокировано чатов: <b>{len(st['blocked_chats'])}</b>{blocked_chats_part}\n"
            f"• Заблокировано юзеров: <b>{len(st['blocked_users'])}</b>{blocked_users_part}",
            parse_mode="HTML",
        )

    return await message.answer(
        f"❓ Неизвестная под-команда: <code>{html.escape(sub)}</code>.\n"
        f"Используй: вкл / выкл / ключ / ограничить / разрешить / статус",
        parse_mode="HTML",
    )


# ============================================================
#  КОМАНДА !глобал_грог
# ============================================================
async def global_groq_cmd(message: Message):
    """!глобал_грог "api_key"   — задать ключ
       !глобал_грог статус      — показать текущий ключ
       !глобал_грог сброс       — удалить ключ
    """
    from ..core.global_groq import get_global_groq_key, set_global_groq_key

    text = (message.text or "").strip()
    tail = ""
    for prefix in ("!глобал_грог", ".глобал_грог"):
        if text.lower().startswith(prefix):
            tail = text[len(prefix):].strip()
            break

    if not tail:
        current = get_global_groq_key()
        masked = (current[:6] + "…" + current[-4:] if len(current) > 12 else "***") if current else "—"
        return await message.answer(
            "🟣 <b>Глобальный Groq (vision для антишлюхобота)</b>\n\n"
            f"• Текущий ключ: <code>{masked}</code>\n\n"
            "<code>!глобал_грог \"gsk_xxxx...\"</code> — задать ключ\n"
            "<code>!глобал_грог статус</code> — показать ключ\n"
            "<code>!глобал_грог сброс</code> — удалить ключ",
            parse_mode="HTML",
        )

    parts = _parse_quoted_args(tail)
    sub = tail.split()[0].lower() if tail.split() else ""

    if sub in ("статус", "status", "инфо", "info"):
        current = get_global_groq_key()
        masked = (current[:6] + "…" + current[-4:] if len(current) > 12 else "***") if current else "—"
        return await message.answer(
            f"🟣 <b>Groq ключ:</b> <code>{masked}</code>\n"
            f"{'✅ задан' if current else '❌ не задан'}",
            parse_mode="HTML",
        )

    if sub in ("сброс", "reset", "удалить", "clear", "delete"):
        set_global_groq_key("")
        return await message.answer("🗑 Глобальный Groq ключ удалён.", parse_mode="HTML")

    # Иначе — первый аргумент это сам ключ
    api_key = parts[0] if parts else ""
    if not api_key:
        return await message.answer(
            "⚠️ Укажи ключ:\n<code>!глобал_грог \"gsk_xxxx...\"</code>",
            parse_mode="HTML",
        )
    if not api_key.startswith("gsk_"):
        return await message.answer(
            "❌ Groq ключ должен начинаться с <code>gsk_</code>",
            parse_mode="HTML",
        )
    set_global_groq_key(api_key)
    masked = api_key[:6] + "…" + api_key[-4:] if len(api_key) > 12 else "***"
    return await message.answer(
        f"✅ Глобальный Groq ключ сохранён: <code>{html.escape(masked)}</code>\n"
        f"Антишлюхобот будет использовать его для анализа аватарок.",
        parse_mode="HTML",
    )


# ============================================================
#  КОМАНДЫ БАНА/РАЗБАНА
# ============================================================

async def ban_user_private(message: Message):
    try:
        parts = message.text.split()
        if len(parts) < 3:
            return await message.answer(
                "⚠️ Использование: <code>!бан &lt;group_id&gt; &lt;user_id&gt; [причина]</code>",
                parse_mode="HTML",
            )

        try:
            group_id = int(parts[1])
            user_id = int(parts[2])
        except ValueError:
            return await message.answer("❌ group_id и user_id должны быть числами.")

        reason = " ".join(parts[3:]).strip() or None
        actor_id = message.from_user.id
        actor_lvl = 99 if actor_id == OWNER_ID else get_level(actor_id)

        # нельзя банить владельца / любого модератора
        if _is_protected_target(user_id):
            return await message.answer("⛔ Этот пользователь защищён (владелец/модератор).")

        # admin не может банить равного или вышестоящего
        target_lvl = get_level(user_id)
        if actor_id != OWNER_ID and target_lvl >= actor_lvl:
            return await message.answer("⛔ Нельзя банить пользователя того же или более высокого уровня.")

        await bot.ban_chat_member(chat_id=group_id, user_id=user_id)

        cid = str(group_id)
        settings.setdefault(cid, {})
        settings[cid].setdefault("bot_banned", {})
        settings[cid]["bot_banned"][str(user_id)] = {
            "banned_at": datetime.now().isoformat(timespec="seconds"),
            "banned_by": actor_id,
            "banned_by_level": actor_lvl,
            "reason": reason,
        }
        save_settings(cid)

        logger.warning(f"[BAN-DM] {actor_id} (lvl={actor_lvl}) -> ban {user_id} in {group_id} reason={reason!r}")
        tail = f"\n📝 Причина: {html.escape(reason)}" if reason else ""
        await message.answer(
            f"✅ Пользователь <code>{user_id}</code> забанен в группе <code>{group_id}</code>.\n"
            f"📌 Бан запомнен (защита от ручного разбана).{tail}",
            parse_mode="HTML",
        )
    except TelegramForbiddenError as e:
        await message.answer(f"❌ Нет прав: {html.escape(str(e))}", parse_mode="HTML")
    except TelegramBadRequest as e:
        await message.answer(f"❌ Telegram: {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        logger.exception(f"[BAN-DM] fail: {e}")
        await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")


async def unban_user_private(message: Message):
    try:
        parts = message.text.split()
        if len(parts) < 3:
            return await message.answer(
                "⚠️ Использование: <code>!разбан &lt;group_id&gt; &lt;user_id&gt;</code>",
                parse_mode="HTML",
            )

        try:
            group_id = int(parts[1])
            user_id = int(parts[2])
        except ValueError:
            return await message.answer("❌ group_id и user_id должны быть числами.")

        cid = str(group_id)
        removed = False
        if cid in settings and "bot_banned" in settings[cid]:
            if str(user_id) in settings[cid]["bot_banned"]:
                del settings[cid]["bot_banned"][str(user_id)]
                save_settings(cid)
                removed = True

        await bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        logger.warning(f"[UNBAN-DM] {message.from_user.id} -> unban {user_id} in {group_id}")

        tail = "🗑 Удалён из списка отслеживания." if removed else "ℹ️ В списке отслеживания не найден."
        await message.answer(
            f"✅ Пользователь <code>{user_id}</code> разбанен в группе <code>{group_id}</code>.\n{tail}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        await message.answer(f"❌ Telegram: {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        logger.exception(f"[UNBAN-DM] fail: {e}")
        await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")


async def mass_ban_private(message: Message):
    try:
        parts = message.text.split()
        if len(parts) < 3:
            return await message.answer(
                "⚠️ Использование: <code>!массбан &lt;group_id&gt; &lt;uid1&gt; &lt;uid2&gt; ...</code>",
                parse_mode="HTML",
            )

        try:
            group_id = int(parts[1])
            user_ids = list(map(int, parts[2:]))
        except ValueError:
            return await message.answer("❌ Все ID должны быть числами.")

        actor_id = message.from_user.id
        actor_lvl = 99 if actor_id == OWNER_ID else get_level(actor_id)

        cid = str(group_id)
        settings.setdefault(cid, {})
        settings[cid].setdefault("bot_banned", {})

        success, failed, skipped = [], [], []
        for u in user_ids:
            if _is_protected_target(u):
                skipped.append(f"{u} (защищён)")
                continue
            if actor_id != OWNER_ID and get_level(u) >= actor_lvl:
                skipped.append(f"{u} (≥ твой уровень)")
                continue
            try:
                await bot.ban_chat_member(chat_id=group_id, user_id=u)
                settings[cid]["bot_banned"][str(u)] = {
                    "banned_at": datetime.now().isoformat(timespec="seconds"),
                    "banned_by": actor_id,
                    "banned_by_level": actor_lvl,
                }
                success.append(u)
            except Exception as e:
                failed.append(f"{u} ({e})")
            await asyncio.sleep(0.04)

        save_settings(cid)
        logger.warning(
            f"[MASSBAN-DM] {actor_id} (lvl={actor_lvl}) group={group_id} "
            f"ok={success} fail={failed} skip={skipped}"
        )

        result = (
            f"✅ Забанены: {', '.join(map(str, success)) or 'никого'}\n"
            f"⛔ Пропущены: {', '.join(skipped) or 'нет'}\n"
            f"❌ Ошибки: {', '.join(failed) or 'нет'}"
        )
        await message.answer(result)
    except Exception as e:
        logger.exception(f"[MASSBAN-DM] fail: {e}")
        await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")


# ============================================================
#  УТИЛИТЫ ДЛЯ !снос_чата
# ============================================================

def _bot_owned_rights(bot_member) -> dict:
    """Вернёт dict вида {'can_xxx': True/False} — какими правами ОБЛАДАЕТ сам бот.
    Используется, чтобы понять, что именно мы ВПРАВЕ снимать у чужих админов."""
    rights = {}
    for f in _ADMIN_RIGHT_FIELDS:
        rights[f] = bool(getattr(bot_member, f, False))
    return rights


async def _demote_admin_safely(chat_id: int, user_id: int, bot_rights: dict) -> tuple[bool, str]:
    """
    Снять с админа ВСЕ права, которые бот вправе снять (т.е. которыми обладает сам).
    Права, которых у бота нет, мы НЕ трогаем (передаём None) — иначе Telegram вернёт 400.
    Возвращает (ok, info).
    """
    kwargs = {"chat_id": chat_id, "user_id": user_id}
    passed_any = False
    for f, has in bot_rights.items():
        if has:
            kwargs[f] = False
            passed_any = True

    if not passed_any:
        return False, "у бота нет ни одного админ-права для снятия"

    try:
        await bot.promote_chat_member(**kwargs)
        return True, "ok"
    except TelegramBadRequest as e:
        err1 = str(e)
        minimal = {
            "chat_id": chat_id,
            "user_id": user_id,
            "can_change_info": False,
            "can_invite_users": False,
            "can_pin_messages": False,
        }
        try:
            await bot.promote_chat_member(**minimal)
            return True, f"ok (fallback minimal; первая ошибка: {err1})"
        except Exception as e2:
            return False, f"{err1} | fallback: {e2}"
    except TelegramForbiddenError as e:
        return False, f"forbidden: {e}"
    except Exception as e:
        return False, f"{e}"


def _collect_all_known_users(group_id: int) -> set[int]:
    """
    Агрегируем ВСЕ известные нам user_id для данного чата из всех источников.
    Ключи в in-memory структурах могут быть и int, и str — нормализуем.
    """
    ids: set[int] = set()

    def _add(v):
        try:
            ids.add(int(v))
        except (TypeError, ValueError):
            pass

    for key in (group_id, str(group_id)):
        bucket = group_users.get(key) if hasattr(group_users, "get") else None
        if isinstance(bucket, dict):
            for uid, user_obj in bucket.items():
                _add(uid)
                if user_obj is not None and hasattr(user_obj, "id"):
                    _add(user_obj.id)

    for key in (group_id, str(group_id)):
        bucket = user_last_seen.get(key) if hasattr(user_last_seen, "get") else None
        if isinstance(bucket, dict):
            for uid in bucket.keys():
                _add(uid)

    for key in (group_id, str(group_id)):
        hist = chat_histories.get(key) if hasattr(chat_histories, "get") else None
        if hist:
            try:
                for item in hist:
                    fu = getattr(item, "from_user", None) or (item.get("from_user") if isinstance(item, dict) else None)
                    if fu is not None:
                        if hasattr(fu, "id"):
                            _add(fu.id)
                        elif isinstance(fu, dict) and "id" in fu:
                            _add(fu["id"])
                    if isinstance(item, dict):
                        _add(item.get("user_id"))
            except Exception:
                pass

    try:
        logs = get_chat_messages(group_id, limit=5000) or []
        for entry in logs:
            if isinstance(entry, dict):
                _add(entry.get("user_id"))
    except Exception as e:
        logger.debug(f"[NUKE] get_chat_messages fail: {e}")

    return ids


# ============================================================
#  !ссылка <group_id> — сгенерировать invite-ссылку
# ============================================================

async def gen_invite_link_private(message: Message):
    """!ссылка <group_id>  — создаёт одноразовую пригласительную ссылку."""
    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer(
            "⚠️ Использование: <code>!ссылка &lt;group_id&gt;</code>",
            parse_mode="HTML",
        )
    try:
        group_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ group_id должен быть числом.")

    try:
        me = await bot.me()
        bot_member = await bot.get_chat_member(group_id, me.id)
    except TelegramForbiddenError:
        return await message.answer(
            f"❌ Бота нет в чате <code>{group_id}</code>.",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        return await message.answer(f"❌ Telegram: {e}")
    except Exception as e:
        return await message.answer(f"❌ Ошибка: {e}")

    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return await message.answer(
            f"❌ Бот в <code>{group_id}</code> не админ.",
            parse_mode="HTML",
        )
    if not getattr(bot_member, "can_invite_users", False) and bot_member.status != ChatMemberStatus.CREATOR:
        return await message.answer(
            "❌ У бота нет права <code>can_invite_users</code>.",
            parse_mode="HTML",
        )

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=group_id,
            name="owner-invite",
            creates_join_request=False,
        )
    except Exception as e:
        return await message.answer(f"❌ Не удалось создать ссылку: {e}")

    chat_title = ""
    try:
        ch = await bot.get_chat(group_id)
        chat_title = getattr(ch, "title", "") or ""
    except Exception:
        pass

    title_html = f" — <b>{html.escape(chat_title)}</b>" if chat_title else ""
    await message.answer(
        f"🔗 <b>Приглашение</b>{title_html}\n"
        f"Чат: <code>{group_id}</code>\n"
        f"{invite.invite_link}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
#  !снос_чата <group_id>
# ============================================================


async def nuke_chat_private(message: Message):
    """
    !снос_чата <group_id>
    """
    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: !снос_чата <group_id>")
    try:
        group_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ group_id должен быть числом.")

    status = await message.answer(
        f"🚨 Начинаю <b>СНОС</b> чата <code>{group_id}</code>…",
        parse_mode="HTML",
    )

    try:
        me = await bot.me()
    except Exception as e:
        return await status.edit_text(f"❌ Не удалось получить инфо о боте: {e}")

    try:
        bot_member = await bot.get_chat_member(group_id, me.id)
    except TelegramForbiddenError:
        return await status.edit_text(
            "❌ <b>Бота нет в этом чате</b>.\n\n"
            f"Чат: <code>{group_id}</code>\n"
            "Через Bot API нельзя банить/переименовывать чат, в котором бота нет.\n\n"
            "🔧 Что делать:\n"
            "1) Добавьте бота в чат.\n"
            "2) Выдайте ему права администратора: "
            "<code>can_restrict_members</code>, <code>can_promote_members</code>, "
            "<code>can_change_info</code>, <code>can_delete_messages</code>.\n"
            "3) Запустите <code>!снос_чата</code> ещё раз.",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "chat not found" in msg:
            return await status.edit_text(
                f"❌ Чат <code>{group_id}</code> не найден.\n"
                "Возможно, бот никогда в нём не был, ID неверный, либо чат удалён.",
                parse_mode="HTML",
            )
        return await status.edit_text(f"❌ Telegram BadRequest: {e}")
    except Exception as e:
        return await status.edit_text(f"❌ Не удалось получить статус бота в {group_id}: {e}")

    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return await status.edit_text(
            f"❌ Бот есть в чате <code>{group_id}</code>, но он <b>не админ</b> "
            f"(status=<code>{bot_member.status}</code>).\n"
            "Выдайте ему админку с правами <code>can_restrict_members</code> и "
            "<code>can_promote_members</code> и повторите.",
            parse_mode="HTML",
        )

    bot_rights = _bot_owned_rights(bot_member)
    bot_can_restrict = bot_rights.get("can_restrict_members", False)
    bot_can_promote = bot_rights.get("can_promote_members", False)
    bot_can_invite = bot_rights.get("can_invite_users", False) or bot_member.status == ChatMemberStatus.CREATOR

    if not bot_can_restrict:
        return await status.edit_text(
            "❌ У бота нет права <code>can_restrict_members</code> — снос невозможен.",
            parse_mode="HTML",
        )

    userbot_ids: list[int] = []
    userbot_info = "не использовался"
    if bot_can_invite:
        try:
            await status.edit_text(
                "🔗 Создаю invite-ссылку для юзербота…",
                parse_mode="HTML",
            )
            invite = await bot.create_chat_invite_link(
                chat_id=group_id,
                name="nuke-userbot",
                creates_join_request=False,
            )
            invite_link = invite.invite_link
            logger.info(f"[NUKE] invite создан: {invite_link}")

            await status.edit_text(
                "🛰 Юзербот заходит в чат и собирает участников…",
                parse_mode="HTML",
            )

            try:
                from bottt import collect_members_via_userbot  # noqa: WPS433
                userbot_ids, userbot_info = await collect_members_via_userbot(
                    invite_link=invite_link,
                    chat_id=group_id,
                )
                logger.info(f"[NUKE] userbot собрал: {len(userbot_ids)} id, info={userbot_info}")
            except Exception as e:
                logger.error(f"[NUKE] userbot ошибка: {e}")
                userbot_info = f"ошибка: {e}"

            try:
                await bot.revoke_chat_invite_link(group_id, invite_link)
            except Exception as e:
                logger.debug(f"[NUKE] revoke_invite warn: {e}")
        except Exception as e:
            logger.error(f"[NUKE] invite/user-bot stage fail: {e}")
            userbot_info = f"stage fail: {e}"
    else:
        userbot_info = "у бота нет can_invite_users — юзербот не приглашён"

    try:
        admins = await bot.get_chat_administrators(group_id)
    except Exception as e:
        admins = []
        logger.error(f"[NUKE] не смог получить админов {group_id}: {e}")

    demoted: list[int] = []
    demote_failed: list[str] = []
    targets_admins: set[int] = set()
    creator_id: int | None = None

    for adm in admins:
        user = adm.user
        if user.is_bot:
            continue
        if adm.status == ChatMemberStatus.CREATOR:
            creator_id = user.id
            continue

        targets_admins.add(user.id)

        if bot_can_promote:
            ok, info = await _demote_admin_safely(group_id, user.id, bot_rights)
            if ok:
                demoted.append(user.id)
            else:
                demote_failed.append(f"{user.id} ({info})")
                logger.error(f"[NUKE] demote fail {user.id}: {info}")
        else:
            demote_failed.append(f"{user.id} (у бота нет can_promote_members)")

    to_ban: set[int] = set()
    to_ban |= targets_admins
    to_ban |= _collect_all_known_users(group_id)
    if userbot_ids:
        to_ban |= set(userbot_ids)

    to_ban.discard(OWNER_ID)
    to_ban.discard(me.id)
    if creator_id is not None:
        to_ban.discard(creator_id)

    # ⭐ улучшение модерации: не банить модераторов бота через снос
    for mid in list(to_ban):
        if get_level(mid) >= 1:
            to_ban.discard(mid)

    total_targets = len(to_ban)
    logger.info(f"[NUKE] {group_id}: targets_total={total_targets} admins={len(targets_admins)}")

    cid = str(group_id)
    settings.setdefault(cid, {})
    settings[cid].setdefault("bot_banned", {})

    banned_ok, banned_fail = 0, 0
    fail_examples: list[str] = []
    now_iso = datetime.now().isoformat()

    for u in to_ban:
        try:
            await bot.ban_chat_member(chat_id=group_id, user_id=u)
            settings[cid]["bot_banned"][str(u)] = {
                "banned_at": now_iso,
                "banned_by": message.from_user.id,
                "reason": "nuke_chat",
            }
            banned_ok += 1
        except TelegramForbiddenError as e:
            banned_fail += 1
            fail_examples.append(f"{u}: {e}")
            logger.error(f"[NUKE] ban fail (forbidden) {u}: {e}")
            break
        except TelegramBadRequest as e:
            banned_fail += 1
            if len(fail_examples) < 5:
                fail_examples.append(f"{u}: {e}")
            logger.error(f"[NUKE] ban fail {u}: {e}")
        except Exception as e:
            banned_fail += 1
            if len(fail_examples) < 5:
                fail_examples.append(f"{u}: {e}")
            logger.error(f"[NUKE] ban fail {u}: {e}")
        await asyncio.sleep(0.04)

    save_settings(cid)

    photo_deleted = False
    try:
        await bot.delete_chat_photo(group_id)
        photo_deleted = True
    except Exception as e:
        logger.error(f"[NUKE] delete_chat_photo fail: {e}")

    title_changed = False
    try:
        await bot.set_chat_title(group_id, "ВЫЕБАНО AI DEFENDER")
        title_changed = True
    except Exception as e:
        logger.error(f"[NUKE] set_chat_title fail: {e}")

    left = False
    try:
        await bot.leave_chat(group_id)
        left = True
    except Exception as e:
        logger.error(f"[NUKE] leave_chat fail: {e}")

    report = (
        "🚨 <b>СНОС ЧАТА ЗАВЕРШЁН</b>\n"
        f"Чат: <code>{group_id}</code>\n\n"
        f"🛰 Юзербот: <b>{len(userbot_ids)}</b> id <i>({html.escape(str(userbot_info))})</i>\n"
        f"👥 Целей всего: <b>{total_targets}</b>\n"
        f"⬇️ Снято админов: <b>{len(demoted)}</b>"
        + (f" (ошибок: {len(demote_failed)})" if demote_failed else "")
        + f"\n🔨 Забанено: <b>{banned_ok}</b>"
        + (f" / не удалось: {banned_fail}" if banned_fail else "")
        + f"\n🖼 Аватар удалён: {'✅' if photo_deleted else '❌'}"
        f"\n✏️ Название сменено: {'✅' if title_changed else '❌'}"
        f"\n🚪 Бот вышел: {'✅' if left else '❌'}"
    )
    if creator_id:
        report += f"\n👑 Creator (<code>{creator_id}</code>) не тронут — ограничение Bot API."
    if demote_failed:
        report += "\n\n<b>Demote errors (первые 5):</b>\n" + "\n".join(
            html.escape(x) for x in demote_failed[:5]
        )
    if fail_examples:
        report += "\n\n<b>Ban errors (первые 5):</b>\n" + "\n".join(
            html.escape(x) for x in fail_examples
        )

    await status.edit_text(report, parse_mode="HTML")


# ============================================================
#  ОСТАЛЬНЫЕ КОМАНДЫ
# ============================================================
async def leave_chat_private(message: Message):
    """
    !выход <chat_id>
    Пример:
    !выход -1001234567890
    """

    try:
        args = message.text.split(maxsplit=1)

        if len(args) < 2:
            return await message.answer(
                "⚠️ Использование:\n<code>!выход CHAT_ID</code>",
                parse_mode="HTML"
            )

        chat_id = int(args[1].strip())

        chat = await bot.get_chat(chat_id)
        title = getattr(chat, "title", str(chat_id))

        await bot.leave_chat(chat_id)

        # Очистка локальных данных (если есть)
        chat_histories.pop(chat_id, None)
        group_users.pop(chat_id, None)
        group_names.pop(chat_id, None)
        pending_messages.pop(chat_id, None)

        await message.answer(
            f"✅ Успешно вышел из чата:\n"
            f"<b>{title}</b>\n"
            f"<code>{chat_id}</code>",
            parse_mode="HTML"
        )

    except ValueError:
        await message.answer(
            "❌ CHAT_ID должен быть числом."
        )

    except Exception as e:
        await message.answer(
            f"❌ Ошибка выхода из чата:\n<code>{e}</code>",
            parse_mode="HTML"
        )
def _format_msg_entry(entry: dict) -> str:
    mid = entry.get("message_id")
    when = entry.get("date", "")
    try:
        dt = datetime.fromisoformat(when)
        when_short = dt.strftime("%d.%m %H:%M:%S")
    except Exception:
        when_short = when[:19] if when else ""

    name = html.escape(entry.get("user_name") or "")
    username = entry.get("username")
    user_id = entry.get("user_id")
    user_label = f"{name}"
    if username:
        user_label += f" @{html.escape(username)}"
    if user_id:
        user_label += f" [<code>{user_id}</code>]"

    ctype = entry.get("type", "text")
    text = entry.get("text") or ""
    reply_to = entry.get("reply_to")

    shown = html.escape(text).replace("\n", " ")
    if len(shown) > 180:
        shown = shown[:177] + "…"

    prefix = ""
    if ctype != "text":
        prefix = f"[{ctype}] "
    reply_part = f" ↩️{reply_to}" if reply_to else ""

    return (
        f"• <code>{mid}</code>{reply_part} <i>{when_short}</i> — {user_label}\n"
        f"    {prefix}{shown}"
    )


async def list_chat_messages(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer(
            "⚠️ Использование: <code>!список_соо &lt;group_id&gt; [лимит]</code>",
            parse_mode="HTML",
        )
    try:
        group_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ group_id должен быть числом.")

    limit = 100
    if len(parts) >= 3:
        try:
            limit = max(1, min(int(parts[2]), 500))
        except ValueError:
            pass

    msgs = get_chat_messages(group_id, limit=limit)
    if not msgs:
        return await message.answer(f"ℹ️ Для чата <code>{group_id}</code> пока нет логов.", parse_mode="HTML")

    title = get_chat_title(group_id) or "(без названия)"
    header = f"📋 <b>Последние {len(msgs)} сообщений</b> из чата <b>{html.escape(title)}</b> [<code>{group_id}</code>]\n\n"

    lines = [_format_msg_entry(e) for e in msgs]

    MAX_LEN = 3800
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


def _collect_broadcast_chats() -> list:
    ids = set()
    for key in list(settings.keys()):
        try:
            cid = int(key)
            if cid < 0:
                ids.add(cid)
        except (TypeError, ValueError):
            continue
    try:
        for cid_str, _title in get_known_chats():
            try:
                cid = int(cid_str)
                if cid < 0:
                    ids.add(cid)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return sorted(ids)


async def broadcast_global(message: Message):
    import asyncio as _asyncio

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)

    has_reply_content = bool(message.reply_to_message)
    payload_text = parts[1].strip() if len(parts) > 1 else ""

    if not has_reply_content and not payload_text:
        return await message.answer("⚠️ Использование: !глобалсоо текст или reply с командой")

    chats = _collect_broadcast_chats()
    if not chats:
        return await message.answer("ℹ️ Не найдено ни одной группы для рассылки.")

    status_msg = await message.answer(f"📡 Рассылка в <b>{len(chats)}</b> чатов...", parse_mode="HTML")

    sent = 0
    failed = 0
    skipped = 0
    errors = []

    broadcast_text = None
    if not has_reply_content:
        broadcast_text = f"📢 <b>Глобальное уведомление</b>\n\n{payload_text}"

    from .privacy import global_notifications_enabled

    for cid in chats:
        if not global_notifications_enabled(cid):
            skipped += 1
            continue
        try:
            if has_reply_content:
                src = message.reply_to_message
                await bot.copy_message(chat_id=cid, from_chat_id=message.chat.id, message_id=src.message_id)
            else:
                await bot.send_message(cid, broadcast_text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            if len(errors) < 10:
                errors.append(f"• <code>{cid}</code>: {html.escape(str(e)[:120])}")
        await _asyncio.sleep(0.05)

    result = f"✅ Рассылка завершена.\nУспешно: {sent}\nПропущено: {skipped}\nОшибок: {failed}"
    if errors:
        result += "\n\n" + "\n".join(errors)
    await status_msg.edit_text(result, parse_mode="HTML")


CHATS_PER_PAGE = 10


async def send_chats_page(message_or_call, page: int = 0):
    chats = get_known_chats()

    if not chats:
        text = "ℹ️ Ещё ни одного чата не залогировано."

        if isinstance(message_or_call, CallbackQuery):
            return await message_or_call.message.edit_text(text)

        return await message_or_call.answer(text)

    total_pages = (len(chats) - 1) // CHATS_PER_PAGE + 1

    start = page * CHATS_PER_PAGE
    end = start + CHATS_PER_PAGE

    current_chats = chats[start:end]

    lines = [
        f"📚 <b>Залогированные чаты</b> ({page + 1}/{total_pages}):"
    ]

    for cid, title in current_chats:
        safe_title = html.escape(title) if title else "(без названия)"
        lines.append(f"• <code>{cid}</code> — {safe_title}")

    keyboard = []

    nav_buttons = []

    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"chats_page:{page - 1}"
            )
        )

    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="➡️ Далее",
                callback_data=f"chats_page:{page + 1}"
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    text = "\n".join(lines)

    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=markup
        )
        await message_or_call.answer()
    else:
        await message_or_call.answer(
            text,
            parse_mode="HTML",
            reply_markup=markup
        )


# Команда
async def list_logged_chats(message: Message):
    await send_chats_page(message, 0)


# Callback handler
@router.callback_query(F.data.startswith("chats_page:"))
async def chats_pagination(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    await send_chats_page(call, page)



async def promote_admin_private(message: Message):
    """!админ <group_id> <user_id> [титул] — выдать админку."""
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        return await message.answer(
            "⚠️ Использование: <code>!админ &lt;group_id&gt; &lt;user_id&gt; [титул]</code>",
            parse_mode="HTML",
        )
    try:
        group_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        return await message.answer("❌ group_id и user_id должны быть числами.")

    custom_title = parts[3].strip() if len(parts) >= 4 else None
    if custom_title and len(custom_title) > 16:
        custom_title = custom_title[:16]

    try:
        me = await bot.me()
        bot_member = await bot.get_chat_member(group_id, me.id)
    except TelegramForbiddenError:
        return await message.answer(
            f"❌ Бота нет в чате <code>{group_id}</code>.",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        return await message.answer(f"❌ Telegram: {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        return await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")

    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return await message.answer(
            f"❌ Бот в <code>{group_id}</code> не админ — не может выдавать админку.",
            parse_mode="HTML",
        )
    if not getattr(bot_member, "can_promote_members", False) and bot_member.status != ChatMemberStatus.CREATOR:
        return await message.answer(
            "❌ У бота нет права <code>can_promote_members</code>.",
            parse_mode="HTML",
        )

    # Передаём ТОЛЬКО те права, которыми обладает сам бот.
    bot_rights = _bot_owned_rights(bot_member)
    kwargs = {"chat_id": group_id, "user_id": user_id}
    granted: list[str] = []
    for f, has in bot_rights.items():
        if has:
            kwargs[f] = True
            granted.append(f)

    if len(granted) == 0:
        return await message.answer("❌ У бота нет ни одного админ-права для делегирования.")

    try:
        await bot.promote_chat_member(**kwargs)
    except TelegramBadRequest as e:
        return await message.answer(f"❌ Telegram: {html.escape(str(e))}", parse_mode="HTML")
    except TelegramForbiddenError as e:
        return await message.answer(f"❌ Нет прав: {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        logger.exception(f"[PROMOTE-DM] fail: {e}")
        return await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")

    title_info = ""
    if custom_title:
        try:
            await bot.set_chat_administrator_custom_title(group_id, user_id, custom_title)
            title_info = f"\n🏷 Титул: <b>{html.escape(custom_title)}</b>"
        except Exception as e:
            title_info = f"\n⚠️ Титул не поставлен: <code>{html.escape(str(e))}</code>"

    logger.warning(
        f"[PROMOTE-DM] {message.from_user.id} -> promote {user_id} in {group_id}; "
        f"rights={granted}; title={custom_title!r}"
    )
    await message.answer(
        f"✅ Пользователь <code>{user_id}</code> теперь админ в группе <code>{group_id}</code>.\n"
        f"🔑 Выдано прав: <b>{len(granted)}</b>"
        f"{title_info}",
        parse_mode="HTML",
    )


async def demote_admin_private(message: Message):
    """!снять_админ <group_id> <user_id> — снять админку."""
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer(
            "⚠️ Использование: <code>!снять_админ &lt;group_id&gt; &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )
    try:
        group_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        return await message.answer("❌ group_id и user_id должны быть числами.")

    try:
        me = await bot.me()
        bot_member = await bot.get_chat_member(group_id, me.id)
    except TelegramForbiddenError:
        return await message.answer(
            f"❌ Бота нет в чате <code>{group_id}</code>.",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        return await message.answer(f"❌ Telegram: {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        return await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")

    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return await message.answer(
            f"❌ Бот в <code>{group_id}</code> не админ.",
            parse_mode="HTML",
        )
    if not getattr(bot_member, "can_promote_members", False) and bot_member.status != ChatMemberStatus.CREATOR:
        return await message.answer(
            "❌ У бота нет права <code>can_promote_members</code>.",
            parse_mode="HTML",
        )

    # Проверим, что цель — не создатель чата
    try:
        target_member = await bot.get_chat_member(group_id, user_id)
        if target_member.status == ChatMemberStatus.CREATOR:
            return await message.answer(
                "⛔ Это владелец группы (creator) — Bot API не позволяет снимать с него админку."
            )
    except Exception:
        pass

    bot_rights = _bot_owned_rights(bot_member)
    ok, info = await _demote_admin_safely(group_id, user_id, bot_rights)

    if ok:
        logger.warning(
            f"[DEMOTE-DM] {message.from_user.id} -> demote {user_id} in {group_id}; info={info}"
        )
        await message.answer(
            f"✅ С пользователя <code>{user_id}</code> снята админка в группе <code>{group_id}</code>.\n"
            f"ℹ️ {html.escape(info)}",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"❌ Не удалось снять админку у <code>{user_id}</code> в <code>{group_id}</code>:\n"
            f"<code>{html.escape(info)}</code>",
            parse_mode="HTML",
        )


async def reply_to_group_message(message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("⚠️ Использование: !реплай <group_id> <message_id> (в ответ на своё сообщение)")
    try:
        group_id = int(parts[1])
        target_msg_id = int(parts[2])
    except ValueError:
        return await message.answer("❌ group_id и message_id должны быть числами.")

    if not message.reply_to_message:
        return await message.answer("⚠️ Отправьте команду в ответ на своё сообщение с текстом.")

    src = message.reply_to_message
    try:
        await bot.copy_message(
            chat_id=group_id,
            from_chat_id=message.chat.id,
            message_id=src.message_id,
            reply_to_message_id=target_msg_id,
        )
        await message.answer(f"✅ Отправлено как реплай на сообщение {target_msg_id} в чат {group_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def get_seen_chat_ids(message: Message):
    group_ids = set()
    for chat_id in chat_histories.keys():
        try:
            cid = int(chat_id)
            if cid < 0:
                group_ids.add(cid)
        except Exception:
            continue
    if group_ids:
        text = "Группы:\n" + "\n".join([f"<code>{gid}</code>" for gid in group_ids])
    else:
        text = "Бот пока не видел сообщений ни в одной группе."
    await message.answer(text, parse_mode="HTML")


async def send_group_message_private(message: Message):
    try:
        _, group_id, *msg_parts = message.text.split()
        text_to_send = " ".join(msg_parts)
        await bot.send_message(chat_id=int(group_id), text=text_to_send)
        await message.answer("✅ Сообщение отправлено")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def who_is_online(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: !кто_в_сети <group_id>")
    try:
        group_id = int(parts[1])
        members = group_users.get(group_id, {}) or group_users.get(str(group_id), {})
        seen = user_last_seen.get(group_id, {}) or user_last_seen.get(str(group_id), {})
    except Exception as e:
        return await message.answer(f"❌ Ошибка: {e}")
    if not members:
        return await message.answer("Нет данных об участниках.")
    lines = []
    for user_id, user in members.items():
        last = seen.get(user_id)
        last_str = last.strftime("%d.%m.%Y %H:%M:%S") if last else "нет данных"
        username = f"@{user.username}" if user.username else ""
        lines.append(f"• {user.full_name} {username} — <code>{user.id}</code>\n  Последний раз: {last_str}")
    await message.answer("🟢 Активность участников:\n" + "\n".join(lines), parse_mode="HTML")


async def list_seen_group_members(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: !участники <group_id>")
    try:
        group_id = int(parts[1])
        members = list((group_users.get(group_id) or group_users.get(str(group_id)) or {}).values())
    except Exception as e:
        return await message.answer(f"❌ Ошибка: {e}")
    if not members:
        return await message.answer("Нет данных об участниках.")
    lines = []
    for user in members:
        username = f"@{user.username}" if user.username else ""
        lines.append(f"• {user.full_name} {username} — <code>{user.id}</code>")
    await message.answer("👥 Участники:\n" + "\n".join(lines), parse_mode="HTML")