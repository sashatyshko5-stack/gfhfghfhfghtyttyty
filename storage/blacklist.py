"""Глобальный чёрный список рейдеров.

Команды:

В ЛС у владельца бота (OWNER_ID):
    !собрать_список <chat>     — добавить ВСЕХ участников указанной группы
                                  в ГЛОБАЛЬНЫЙ ЧС.
                                  <chat> — числовой id, @username
                                  или t.me/+invite-ссылка.
    !чс_очистить               — полностью очистить глобальный ЧС.
    !чс_удалить <user_id>      — убрать одного юзера из ЧС.
    !чс_статус                 — статистика глобального ЧС.

В группах (любым админом):
    !список                    — статус ЧС в этом чате.
    !список вкл                — включить применение глобального ЧС в этом чате
                                  (сразу банит ВСЕХ из ЧС, в т.ч. уже сидящих).
    !список выкл               — выключить применение в этом чате.

При вступлении нового участника в чат, где ЧС включён, он сверяется
с глобальным списком и банится, если совпал.
"""
import asyncio
import logging
import re

from aiogram import Router, F
from aiogram.types import (
    Message, ChatMemberUpdated,
    ChatMemberAdministrator, ChatMemberOwner,
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.enums import ChatType

from ..core.loader import bot
from ..core.config import OWNER_ID
from ..storage import blacklist_storage as bl

logger = logging.getLogger(__name__)
router = Router()

_BAN_SLEEP = 0.05  # пауза между банами при массовой обработке

# Результат попытки бана
_BAN_OK = "ok"        # успешно забанен
_BAN_SKIP = "skip"    # пользователя нет в чате / невалидный участник —
                      # превентивно забанить нельзя, но JOIN-страховка поймает при входе
_BAN_FAIL = "fail"    # реальная ошибка


def _is_participant_invalid(err: Exception) -> bool:
    """True, если Telegram говорит «такого участника нет в чате».

    Для глобального ЧС это нормальный случай: юзер никогда не был в данной
    группе, значит превентивно забанить его нельзя. Но хендлер JOIN всё
    равно забанит его в момент попытки войти.
    """
    msg = str(err).upper()
    markers = (
        "PARTICIPANT_ID_INVALID",
        "USER_NOT_PARTICIPANT",
        "PEER_ID_INVALID",
        "USER_ID_INVALID",
        "MEMBER_NOT_FOUND",
        "USER_NOT_FOUND",
    )
    return any(m in msg for m in markers)


# ─── Утилиты ──────────────────────────────────────────────────
async def _is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
    except Exception as e:
        logger.error(f"[BLACKLIST] Ошибка проверки прав: {e}")
        return False


async def _ban_one(chat_id: int, user_id: int) -> str:
    """Забанить одного пользователя. Возвращает _BAN_OK / _BAN_SKIP / _BAN_FAIL."""
    try:
        await bot.ban_chat_member(chat_id, user_id)
        return _BAN_OK
    except TelegramRetryAfter as e:
        try:
            await asyncio.sleep(int(e.retry_after) + 1)
            await bot.ban_chat_member(chat_id, user_id)
            return _BAN_OK
        except TelegramAPIError as e2:
            if _is_participant_invalid(e2):
                logger.debug(f"[BLACKLIST] {user_id} not in chat — skip preemptive ban")
                return _BAN_SKIP
            logger.warning(f"[BLACKLIST] {user_id} ban failed after flood: {e2}")
            return _BAN_FAIL
        except Exception as e2:
            logger.warning(f"[BLACKLIST] {user_id} ban failed after flood: {e2}")
            return _BAN_FAIL
    except TelegramAPIError as e:
        if _is_participant_invalid(e):
            # Пользователя просто нет в чате — это нормально, поймает JOIN-хендлер.
            logger.debug(f"[BLACKLIST] {user_id} not in chat — skip preemptive ban")
            return _BAN_SKIP
        logger.warning(f"[BLACKLIST] {user_id} ban failed: {e}")
        return _BAN_FAIL
    except Exception as e:
        logger.warning(f"[BLACKLIST] {user_id} ban unexpected: {e}")
        return _BAN_FAIL


async def _ban_all_globals_in_chat(chat_id: int) -> tuple[int, int, int]:
    """Банит всех из глобального ЧС в указанном чате.

    Возвращает (banned, skipped, failed), где
      banned  — реально забанено сейчас,
      skipped — юзеров нет в чате (будут пойманы JOIN-хендлером),
      failed  — реальные ошибки Telegram API.
    """
    raiders = bl.get_raiders()
    banned = 0
    skipped = 0
    failed = 0
    for uid in raiders:
        result = await _ban_one(chat_id, uid)
        if result == _BAN_OK:
            banned += 1
        elif result == _BAN_SKIP:
            skipped += 1
        else:
            failed += 1
        await asyncio.sleep(_BAN_SLEEP)
    return banned, skipped, failed


def _parse_target(arg: str):
    if not arg:
        return None, None
    arg = arg.strip().strip('"').strip("'")
    m = re.search(r"(https?://)?(www\.)?t(?:elegram)?\.me/\+([A-Za-z0-9_-]+)", arg)
    if m:
        return f"https://t.me/+{m.group(3)}", None
    if re.fullmatch(r"-?\d+", arg):
        try:
            return None, int(arg)
        except ValueError:
            return None, None
    if arg.startswith("@"):
        return None, arg
    return None, "@" + arg


def _is_owner_dm(message: Message) -> bool:
    return (
        message.from_user
        and message.from_user.id == OWNER_ID
        and message.chat.type == ChatType.PRIVATE
    )


# ─── ЛС ВЛАДЕЛЬЦА: !собрать_список ────────────────────────────
@router.message(F.text.regexp(r"^[!\.]собрать_список(\s|$)"))
async def collect_blacklist_cmd(message: Message):
    # Только в ЛС с владельцем
    if not _is_owner_dm(message):
        # В группах/чужих ЛС — молча игнорим (или можно ответить, но лучше тихо)
        if message.from_user and message.from_user.id == OWNER_ID:
            await message.reply("❗ Эту команду используй только в ЛС с ботом.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await message.reply(
            "<b>Использование:</b>\n"
            "<code>!собрать_список -1001234567890</code>\n"
            "<code>!собрать_список @username</code>\n"
            "<code>!собрать_список https://t.me/+abcDEF</code>\n\n"
            "Соберёт ВСЕХ участников группы в <b>глобальный</b> чёрный список "
            "(работает на все чаты, где ЧС включён).",
            parse_mode="HTML",
        )

    invite_link, chat_ref = _parse_target(parts[1])
    if not invite_link and chat_ref is None:
        return await message.reply("❌ Не смог разобрать аргумент. Дай ID, @username или t.me-ссылку.")

    info = await message.reply("🔄 Подключаю юзербота и собираю участников (deep-scan)…")

    # Прогресс-колбек: апдейтим то же сообщение раз в 5+ сек
    import time as _t
    _last = {"t": 0.0}
    async def _progress(scanned: int, total_ids: int, from_hist: int):
        now = _t.time()
        if now - _last["t"] < 5.0:
            return
        _last["t"] = now
        try:
            await info.edit_text(
                f"🔄 Сканирую историю чата…\n"
                f"📜 Просмотрено сообщений: <b>{scanned}</b>\n"
                f"👤 Найдено уникальных: <b>{total_ids}</b> "
                f"(<i>+{from_hist} из истории</i>)",
                parse_mode="HTML",
            )
        except Exception:
            pass

    from bottt import collect_members_via_userbot
    try:
        ids, debug_msg = await collect_members_via_userbot(
            invite_link=invite_link,
            chat_id=chat_ref,
            max_members=50000,
            progress_cb=_progress,
        )
    except Exception as e:
        logger.error(f"[BLACKLIST] collect error: {e}")
        return await info.edit_text(f"❌ Ошибка сбора: {e}")

    if not ids:
        return await info.edit_text(
            f"❌ Не удалось собрать участников.\n<code>{debug_msg}</code>",
            parse_mode="HTML",
        )

    added = bl.add_raiders(ids)
    total = bl.total_raiders()
    enabled_chats = bl.enabled_chats()

    try:
        await info.edit_text(
            f"✅ Собрано: <b>{len(ids)}</b>\n"
            f"➕ Новых в ЧС: <b>{added}</b>\n"
            f"📛 Всего в глобальном ЧС: <b>{total}</b>\n"
            f"🔒 Чатов с активным ЧС: <b>{len(enabled_chats)}</b>\n"
            f"<i>{debug_msg}</i>",
            parse_mode="HTML",
        )
    except Exception:
        await message.reply(f"Готово. Добавлено {added}, всего {total}.")

    # Прокидываем бан по всем чатам, где ЧС включён
    if enabled_chats and added > 0:
        prog = await message.reply(
            f"🔨 Применяю ЧС в {len(enabled_chats)} чатах…"
        )
        total_banned = 0
        total_skipped = 0
        total_failed = 0
        for cid in enabled_chats:
            try:
                # Баним ТОЛЬКО новых добавленных, чтобы не делать N*M операций.
                # Но раз мы не помним «новых» поимённо — бьём по всему ЧС:
                # это корректно, повторный бан безопасен.
                b, s, f = await _ban_all_globals_in_chat(cid)
                total_banned += b
                total_skipped += s
                total_failed += f
            except Exception as e:
                logger.warning(f"[BLACKLIST] ban-loop chat={cid}: {e}")
        try:
            await prog.edit_text(
                f"🔨 Забанено сейчас: <b>{total_banned}</b>\n"
                f"⏳ Ждут входа: <b>{total_skipped}</b> (их нет в чатах — забаним при попытке войти)\n"
                f"⚠️ Ошибок: <b>{total_failed}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─── ЛС ВЛАДЕЛЬЦА: управление ЧС ──────────────────────────────
@router.message(F.text.regexp(r"^[!\.]чс_очистить(\s|$)"))
async def clear_blacklist_cmd(message: Message):
    if not _is_owner_dm(message):
        return
    n = bl.clear_all()
    await message.reply(f"🧹 Глобальный ЧС очищен. Удалено: <b>{n}</b>", parse_mode="HTML")


@router.message(F.text.regexp(r"^[!\.]чс_удалить(\s|$)"))
async def remove_raider_cmd(message: Message):
    if not _is_owner_dm(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply(
            "Использование: <code>!чс_удалить 123456789</code>",
            parse_mode="HTML",
        )
    uid = int(parts[1])
    ok = bl.remove_raider(uid)
    if ok:
        await message.reply(f"✅ Юзер <code>{uid}</code> удалён из ЧС.", parse_mode="HTML")
    else:
        await message.reply(f"ℹ️ Юзера <code>{uid}</code> в ЧС не было.", parse_mode="HTML")


@router.message(F.text.regexp(r"^[!\.]чс_статус(\s|$)"))
async def blacklist_status_cmd(message: Message):
    if not _is_owner_dm(message):
        return
    chats = bl.enabled_chats()
    await message.reply(
        f"<b>📛 Глобальный ЧС</b>\n"
        f"Рейдеров в списке: <b>{bl.total_raiders()}</b>\n"
        f"Чатов с активным ЧС: <b>{len(chats)}</b>\n\n"
        f"<b>Команды (только в ЛС, только владелец):</b>\n"
        f"<code>!собрать_список &lt;id|@|t.me/+&gt;</code>\n"
        f"<code>!чс_удалить &lt;user_id&gt;</code>\n"
        f"<code>!чс_очистить</code>",
        parse_mode="HTML",
    )


# ─── В ГРУППАХ: !список вкл/выкл ──────────────────────────────
@router.message(F.text.regexp(r"^[!\.]список(\s|$)"))
async def blacklist_toggle_cmd(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        # В ЛС эта команда не имеет смысла — управляющие там другие.
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    args = (message.text or "").split()[1:]

    if not args:
        enabled = bl.is_enabled(chat_id)
        return await message.reply(
            f"<b>📛 Чёрный список рейдеров</b>\n"
            f"В этом чате: {'✅ ВКЛ' if enabled else '❌ ВЫКЛ'}\n"
            f"Рейдеров в глобальном ЧС: <b>{bl.total_raiders()}</b>\n\n"
            f"<b>Команды:</b>\n"
            f"<code>!список вкл</code> — включить и забанить всех из ЧС\n"
            f"<code>!список выкл</code> — выключить\n\n"
            f"<i>Управление самим списком — только у владельца бота в ЛС.</i>",
            parse_mode="HTML",
        )

    sub = args[0].lower()

    if sub in ("вкл", "on"):
        if not await _is_chat_admin(chat_id, user_id):
            return await message.reply("❗ Только админ может управлять ЧС в этом чате.")
        from ..storage.premium import has_premium, register_premium_chat, _MAX_PREMIUM_CHATS
        if not has_premium(user_id):
            return await message.reply(
                "🔒 <b>Рейд-база</b> — премиум-функция.\n\n"
                "Для использования необходима подписка.\n"
                "Напишите боту в личные сообщения: <code>!премиум</code>",
                parse_mode="HTML",
            )
        if not register_premium_chat(user_id, chat_id):
            return await message.reply(
                f"🔒 Достигнут лимит премиум-чатов (<b>{_MAX_PREMIUM_CHATS}</b>).\n"
                "Отключите рейд-базу в другом чате, чтобы освободить место.",
                parse_mode="HTML",
            )
        bl.set_enabled(chat_id, True)
        total = bl.total_raiders()
        if total == 0:
            return await message.reply(
                "✅ ЧС <b>включён</b>, но глобальный список пуст. "
                "Владелец бота должен наполнить его через <code>!собрать_список</code> в ЛС.",
                parse_mode="HTML",
            )
        info = await message.reply(
            f"✅ ЧС <b>включён</b>.\n🔨 Баню {total} рейдеров…",
            parse_mode="HTML",
        )
        banned, skipped, failed = await _ban_all_globals_in_chat(chat_id)
        try:
            await info.edit_text(
                f"✅ ЧС <b>включён</b>.\n"
                f"🔨 Забанено сейчас: <b>{banned}</b>\n"
                f"⏳ Ждут входа: <b>{skipped}</b> (их нет в чате — забаним при попытке зайти)\n"
                f"⚠️ Ошибок: <b>{failed}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    if sub in ("выкл", "off"):
        if not await _is_chat_admin(chat_id, user_id):
            return await message.reply("❗ Только админ может управлять ЧС в этом чате.")
        bl.set_enabled(chat_id, False)
        return await message.reply("❌ ЧС <b>выключен</b> в этом чате.", parse_mode="HTML")

    await message.reply(
        "❓ Неизвестная подкоманда. Используй <code>!список</code> для справки.",
        parse_mode="HTML",
    )


# ─── JOIN-страховка ───────────────────────────────────────────
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def blacklist_on_join(event: ChatMemberUpdated):
    chat_id = event.chat.id
    if not bl.is_enabled(chat_id):
        return
    user_id = event.new_chat_member.user.id
    if not bl.is_raider(user_id):
        return
    result = await _ban_one(chat_id, user_id)
    if result == _BAN_OK:
        logger.info(f"[BLACKLIST] auto-banned raider {user_id} in {chat_id}")
        try:
            await bot.send_message(
                chat_id,
                f"🚫 Рейдер <code>{user_id}</code> забанен (чёрный список).",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(F.new_chat_members)
async def blacklist_on_new_members(message: Message):
    chat_id = message.chat.id
    if not bl.is_enabled(chat_id):
        return
    for u in message.new_chat_members or []:
        if u.is_bot:
            continue
        if bl.is_raider(u.id):
            result = await _ban_one(chat_id, u.id)
            if result == _BAN_OK:
                try:
                    await message.reply(
                        f"🚫 Рейдер <b>{u.full_name}</b> (<code>{u.id}</code>) забанен (чёрный список).",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
