import asyncio
import logging
import re
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, ChatPermissions, ChatMemberUpdated
from aiogram.enums import ChatType
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.exceptions import TelegramAPIError

try:
    from pyrogram import Client as UserClient, filters as pyro_filters, errors as pyro_errors
except Exception:
    UserClient = None
    pyro_filters = None
    pyro_errors = None

from ..core.loader import bot
from ..storage.state import settings, save_settings
from ..core.utils import get_duration_seconds
from ..storage.anti_leak_storage import (
    get_leak_registry,
    save_leak_registry,
    register_invite_link,
    get_chat_invite_links,
    get_active_leaks_for_chat,
    record_join_event,
    get_recent_joins,
    calculate_risk,
    get_user_risk,
    update_user_profile,
    mark_user_banned,
    increment_user_join_count,
    cleanup_old_join_events,
    revoke_invite_link_record,
    register_leaked_link,
)

logger = logging.getLogger(__name__)
router = Router()

_INVITE_LINK_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?t(?:elegram)?\.me/\+([A-Za-z0-9_-]+)")

DEFAULT_LINK_LEAK_SETTINGS = {
    "enabled": False,
    "action_on_leak": "revoke_and_ban",
    "auto_revoke": True,
    "join_request_mode": False,
    "notify_admins": True,
    "test_mode": False,
    "scan_channels": [],
    "userbot_connected": False,
    "userbot_session": None,
    "userbot_api_id": None,
    "userbot_api_hash": None,
    "userbot_channels_ok": False,
}


def _ensure_anti_link_leak_settings(cid: str):
    settings.setdefault(cid, {})
    settings[cid].setdefault("anti_link_leak", {})
    current = settings[cid]["anti_link_leak"]
    for key, value in DEFAULT_LINK_LEAK_SETTINGS.items():
        if key not in current:
            current[key] = value


# ─── Команда !антиссылки ──────────────────────────────────────────────────

@router.message(F.text.startswith(("!антиссылки", ".антиссылки")))
async def handle_anti_links_command(message: Message):
    logger.info(f"[ANTI-LINK-CMD] Получена команда: {message.text!r} от user={message.from_user.id} в chat={message.chat.id} type={message.chat.type}")
    if message.chat.type not in ("group", "supergroup"):
        return await message.reply("❌ Команда работает только в группах.")

    chat_id = message.chat.id
    cid = str(chat_id)
    user_id = message.from_user.id

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status not in ("administrator", "creator"):
            return await message.reply("❗ Только админ может настраивать анти-ссылки.")
    except Exception as e:
        logger.error(f"[ANTI-LINK] Ошибка проверки прав: {e}")
        return await message.reply("❌ Ошибка проверки прав.")

    _ensure_anti_link_leak_settings(cid)
    cfg = settings[cid]["anti_link_leak"]
    parts = message.text.strip().split()
    args = parts[1:] if len(parts) > 1 else []

    if not args:
        enabled = cfg["enabled"]
        status_icon = "🟢" if enabled else "🔴"
        status_text = "Включено" if enabled else "Выключено"
        mode_text = "🧪 Тестовый (без бана)" if cfg.get("test_mode") else "🛡️ Активный"
        ub_ok = cfg.get("userbot_connected") and cfg.get("userbot_channels_ok")
        ub_icon = "✅" if cfg.get("userbot_connected") else "❌"
        ch_icon = "✅" if cfg.get("userbot_channels_ok") else "❌"

        ub_warning = ""
        if not cfg.get("userbot_connected"):
            ub_warning = (
                "\n\n⚠️ <b>Userbot не подключён</b> — без него антиссылки не работают.\n"
                "Напишите боту в ЛС:\n"
                "<code>!юзербот CHAT_ID SESSION API_ID API_HASH</code>\n"
                "<i>SESSION получить через @StringSessionBot</i>"
            )

        text = (
            f"🔗 <b>Антиссылки</b>  {status_icon} {status_text}\n\n"

            f"<b>Как это работает:</b>\n"
            f"Бот отслеживает инвайт-ссылки вашего чата через подключённый userbot-аккаунт. "
            f"Если ссылку на ваш чат сольют в рк — бот мгновенно банит всех, кто войдёт по ней. "
            f"Дополнительно банит при резком спайке входов (5+ человек за 5 минут).\n\n"

            f"<b>Статус:</b>\n"
            f"• Userbot: {ub_icon}  |  Рейд-каналы: {ch_icon}\n"
            f"• Режим: {mode_text}\n"
            f"• Авто-отзыв ссылки: {'✅' if cfg.get('auto_revoke') else '❌'}\n"
            f"• Join Request: {'✅' if cfg.get('join_request_mode') else '❌'}\n"
            f"• Уведомлять админов: {'✅' if cfg.get('notify_admins', True) else '❌'}\n\n"

            f"<b>Команды:</b>\n"
            f"<code>!антиссылки вкл/выкл</code> — включить/выключить\n"
            f"<code>!антиссылки тест вкл/выкл</code> — режим без бана (только логирование)\n"
            f"<code>!антиссылки ревок вкл/выкл</code> — авто-отзыв слитой ссылки\n"
            f"<code>!антиссылки запрос вкл/выкл</code> — режим заявок на вступление\n"
            f"<code>!антиссылки лог</code> — входы за последний час\n"
            f"<code>!антиссылки проверка</code> — активные утечки\n"
            f"<code>!антиссылки сброс</code> — сбросить все настройки"
            f"{ub_warning}"
        )
        return await message.reply(text, parse_mode="HTML")

    cmd = args[0].lower()

    if cmd in ("вкл", "выкл"):
        want = cmd == "вкл"
        if want:
            from ..storage.premium import has_premium, has_chat_premium, register_premium_chat, get_chat_limit
            user_prem = has_premium(user_id)
            chat_prem = has_chat_premium(chat_id)
            if not user_prem and not chat_prem:
                return await message.reply(
                    "🔒 <b>Антиссылки</b> — премиум-функция.\n\n"
                    "Для использования необходима подписка.\n"
                    "• Личный премиум: напишите <code>!премиум</code> боту в ЛС\n"
                    "• Чат-премиум: введите <code>!чат_премиум</code> здесь",
                    parse_mode="HTML",
                )
            if user_prem and not chat_prem and not register_premium_chat(user_id, chat_id):
                limit = get_chat_limit(user_id)
                return await message.reply(
                    f"🔒 Достигнут лимит премиум-чатов (<b>{limit}</b>).\n"
                    "Отключите антиссылки в другом чате, чтобы освободить место.",
                    parse_mode="HTML",
                )
            if not cfg.get("userbot_connected"):
                return await message.reply(
                    "⚠️ <b>Userbot не подключён</b>\n\n"
                    "Без userbot-аккаунта антиссылки не могут мониторить рейд-каналы.\n\n"
                    "Подключите его, написав боту в ЛС:\n"
                    "<code>!юзербот CHAT_ID SESSION API_ID API_HASH</code>\n\n"
                    "<i>SESSION — получайте как угодно</i>",
                    parse_mode="HTML",
                )
            if not cfg.get("userbot_channels_ok"):
                return await message.reply(
                    "⚠️ <b>Userbot подключён, но не добавлен в рейд-каналы</b>\n\n"
                    "Добавьте аккаунт в нужные каналы и переподключите userbot.",
                    parse_mode="HTML",
                )
        if cfg["enabled"] == want:
            return await message.reply(
                f"{'🟢 Антиссылки уже включены.' if want else '🔴 Антиссылки уже выключены.'}",
                parse_mode="HTML",
            )
        cfg["enabled"] = want
        save_settings(cid)
        if want:
            asyncio.create_task(_ensure_userbot_task(cid))
        return await message.reply(
            f"{'🟢 <b>Антиссылки включены.</b> Мониторинг активирован.' if want else '🔴 <b>Антиссылки выключены.</b>'}",
            parse_mode="HTML",
        )

    if cmd == "ревок":
        if len(args) < 2:
            return await message.reply(
                "❌ Укажи значение: <code>!антиссылки ревок вкл</code> или <code>выкл</code>",
                parse_mode="HTML",
            )
        val = args[1].lower() in ("вкл", "on", "true")
        cfg["auto_revoke"] = val
        save_settings(cid)
        return await message.reply(
            f"{'✅ <b>Авто-отзыв ссылки включён.</b> При утечке — ссылка отзывается автоматически.' if val else '❌ <b>Авто-отзыв выключен.</b> Слитая ссылка остаётся активной.'}",
            parse_mode="HTML",
        )

    if cmd == "запрос":
        if len(args) < 2:
            return await message.reply(
                "❌ Укажи значение: <code>!антиссылки запрос вкл</code> или <code>выкл</code>",
                parse_mode="HTML",
            )
        val = args[1].lower() in ("вкл", "on", "true")
        cfg["join_request_mode"] = val
        save_settings(cid)
        return await message.reply(
            f"{'✅ <b>Режим заявок включён.</b> Новые участники должны подавать заявку на вступление.' if val else '❌ <b>Режим заявок выключен.</b> Вступление без подтверждения.'}",
            parse_mode="HTML",
        )

    if cmd == "тест":
        if len(args) < 2:
            return await message.reply(
                "❌ Укажи значение: <code>!антиссылки тест вкл</code> или <code>выкл</code>",
                parse_mode="HTML",
            )
        val = args[1].lower() in ("вкл", "on", "true")
        cfg["test_mode"] = val
        save_settings(cid)
        return await message.reply(
            f"{'🧪 <b>Тестовый режим включён.</b> Бот будет логировать нарушения, но никого не банить.' if val else '🛡️ <b>Тестовый режим выключен.</b> Нарушители получают реальный бан.'}",
            parse_mode="HTML",
        )

    if cmd == "проверка":
        leaks = get_active_leaks_for_chat(chat_id)
        links = get_chat_invite_links(chat_id)
        text = f"📊 <b>Проверка антиссылок</b>\n\n"
        text += f"🔗 Мониторимых ссылок чата: <b>{len(links)}</b>\n"
        if leaks:
            text += f"🚨 <b>Активных утечек: {len(leaks)}</b>\n\n"
            for ll in leaks[:5]:
                text += f"• <code>+{ll['hash']}</code> — {ll.get('source_chat_name', 'неизвестный канал')} ({ll['found_at'][:16]})\n"
            if len(leaks) > 5:
                text += f"<i>...и ещё {len(leaks) - 5}</i>\n"
        else:
            text += "\n✅ Активных утечек не обнаружено."
        return await message.reply(text, parse_mode="HTML")

    if cmd == "лог":
        recent = get_recent_joins(chat_id, minutes=60)
        leaks = get_active_leaks_for_chat(chat_id)
        leak_hashes = {ll.get("hash") for ll in leaks}

        text = f"📋 <b>Лог антиссылок</b> (последний час)\n\n"

        if not recent:
            text += "📭 Входов за последний час не зафиксировано.\n"
        else:
            text += f"👥 <b>Входов: {len(recent)}</b>\n\n"
            for ev in recent[-15:]:
                uid = ev.get("user_id", "?")
                h = ev.get("invite_hash")
                ts = ev.get("timestamp", "")[:16] if ev.get("timestamp") else "—"
                if h and h in leak_hashes:
                    row = f"🚨 <code>{uid}</code> — <code>+{h}</code> <b>[СЛИТАЯ]</b> {ts}"
                elif h:
                    row = f"✅ <code>{uid}</code> — <code>+{h}</code> {ts}"
                else:
                    row = f"➡️ <code>{uid}</code> — без ссылки {ts}"
                text += row + "\n"
            if len(recent) > 15:
                text += f"<i>...показаны последние 15 из {len(recent)}</i>\n"

        text += f"\n🚨 <b>Активных утечек: {len(leaks)}</b>"
        if leaks:
            for ll in leaks[:3]:
                text += f"\n• <code>+{ll['hash']}</code> из {ll.get('source_chat_name', '???')}"

        return await message.reply(text, parse_mode="HTML")

    if cmd == "сброс":
        if cid in _userbot_tasks:
            _userbot_tasks[cid].cancel()
            del _userbot_tasks[cid]
        if cid in _userbot_clients:
            try:
                await _userbot_clients[cid].stop()
            except Exception:
                pass
            del _userbot_clients[cid]
        settings[cid]["anti_link_leak"] = DEFAULT_LINK_LEAK_SETTINGS.copy()
        save_settings(cid)
        return await message.reply(
            "♻️ <b>Настройки антиссылок сброшены.</b>\n"
            "Userbot отключён. Используйте <code>!антиссылки</code> для повторной настройки.",
            parse_mode="HTML",
        )

    if cmd in ("бан", "мут", "сломо", "риск"):
        return await message.reply(
            "ℹ️ Эта опция больше недоступна.\n"
            "Антиссылки работают только в одном режиме: <b>мгновенный бан</b> при входе через слитую ссылку.",
            parse_mode="HTML",
        )

    await message.reply(
        "❓ Неизвестная команда.\n"
        "Напиши <code>!антиссылки</code> без аргументов, чтобы увидеть список команд.",
        parse_mode="HTML",
    )


# ─── Отслеживание входов ──────────────────────────────────────────────────

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_join(event: ChatMemberUpdated):
    chat_id = event.chat.id
    cid = str(chat_id)
    user = event.new_chat_member.user
    user_id = user.id

    _ensure_anti_link_leak_settings(cid)
    cfg = settings[cid]["anti_link_leak"]
    if not cfg.get("enabled", False):
        return

    update_user_profile(user_id, username=user.username, first_name=user.first_name)
    increment_user_join_count(user_id)

    invite_hash = None
    if event.invite_link:
        invite_url = getattr(event.invite_link, "invite_link", "")
        m = _INVITE_LINK_PATTERN.search(invite_url)
        if m:
            invite_hash = m.group(1)
            if not get_chat_invite_links(chat_id).get(invite_hash):
                register_invite_link(chat_id, invite_hash, is_primary=False)

    record_join_event(user_id=user_id, chat_id=chat_id, invite_hash=invite_hash)

    name = user.first_name or "?"
    uname = f"@{user.username}" if user.username else f"id:{user_id}"
    hash_info = f"по ссылке <code>+{invite_hash}</code>" if invite_hash else "без ссылки"
    recent_all = get_recent_joins(chat_id, minutes=5)

    # Если зашел через слитую ссылку — мгновенный бан
    # Проверяем все статусы кроме "revoked": даже после первой реакции новые входы надо банить
    if invite_hash and any(
        ll.get("hash") == invite_hash and ll.get("status") not in ("revoked", None)
        for ll in get_leak_registry().get("leaked_links", [])
    ):
        logger.warning(f"[ANTI-LINK] Рейдер через leaked link user={user_id} hash={invite_hash}")
        if cfg.get("test_mode"):
            try:
                await bot.send_message(
                    chat_id,
                    f"🧪 <b>[ТЕСТ] Слитая ссылка!</b>\n"
                    f"👤 {name} ({uname})\n"
                    f"🔗 Ссылка: <code>+{invite_hash}</code> — <b>в базе утечек</b>\n"
                    f"🚫 В боевом режиме: <b>мгновенный бан</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return
        await _apply_link_leak_reaction(chat_id, user_id, cfg, "бан", "вход через слитую ссылку")
        return

    # Спайк входов (5+ за 5 мин) — тоже бан
    if len(recent_all) >= 5:
        logger.warning(f"[ANTI-LINK] Спайк входов user={user_id} chat={chat_id}")
        if cfg.get("test_mode"):
            try:
                await bot.send_message(
                    chat_id,
                    f"🧪 <b>[ТЕСТ] Спайк входов!</b>\n"
                    f"👤 {name} ({uname}) — {hash_info}\n"
                    f"📈 Вошло за 5 мин: <b>{len(recent_all)}</b> чел.\n"
                    f"🚫 В боевом режиме: <b>бан всех</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return
        await _apply_link_leak_reaction(chat_id, user_id, cfg, "бан", "спайк входов")
        return

    # Обычный вход — логируем только в тест-режиме
    if cfg.get("test_mode"):
        try:
            await bot.send_message(
                chat_id,
                f"🧪 <b>[ТЕСТ] Вход:</b> {name} ({uname})\n"
                f"🔗 {hash_info} — ссылка чистая ✅\n"
                f"📊 Входов за 5 мин: {len(recent_all)}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    logger.info(f"[ANTI-LINK] JOIN user={user_id} chat={chat_id} hash={invite_hash} — пропуск")


# ─── Проверка сообщений на invite links ───────────────────────────────────

async def check_invite_link_in_message(message: Message) -> bool:
    chat_id = message.chat.id
    cid = str(chat_id)
    _ensure_anti_link_leak_settings(cid)
    cfg = settings[cid]["anti_link_leak"]
    if not cfg.get("enabled", False):
        return False
    if message.content_type != "text" or not message.text:
        return False

    text = message.text
    hashes = _INVITE_LINK_PATTERN.findall(text)
    if not hashes:
        return False

    chat_links = get_chat_invite_links(chat_id)
    for h in hashes:
        if h in chat_links:
            logger.warning(f"[ANTI-LINK] Пользователь {message.from_user.id} опубликовал invite link чата!")
            try:
                await message.delete()
            except Exception as e:
                logger.error(f"[ANTI-LINK] Не удалось удалить: {e}")

            await _apply_link_leak_reaction(
                chat_id, message.from_user.id, cfg,
                cfg.get("punishment", "бан"), "публикация invite link чата"
            )
            return True
    return False


# ─── Применение реакции ───────────────────────────────────────────────────

async def _apply_link_leak_reaction(chat_id: int, user_id: int, cfg: dict, action: str, reason: str):
    test_mode = cfg.get("test_mode", False)
    if test_mode:
        logger.info(f"[ANTI-LINK] ТЕСТ: would {action} user={user_id} reason={reason}")
        try:
            await bot.send_message(chat_id, f"🧪 [ТЕСТ] Было бы: {action} {user_id} — {reason}")
        except Exception:
            pass
        return

    if action == "бан":
        try:
            await bot.ban_chat_member(chat_id, user_id)
            mark_user_banned(user_id, chat_id)
            logger.warning(f"[ANTI-LINK] БАН user={user_id} chat={chat_id} reason={reason}")
        except TelegramAPIError as e:
            logger.error(f"[ANTI-LINK] Ошибка бана: {e}")

    if cfg.get("notify_admins", True):
        await _notify_admins_about_leak(bot, chat_id, f"Пользователь {user_id} получил {action}. Причина: {reason}")


async def _notify_admins_about_leak(bot_obj, chat_id: int, text: str):
    try:
        admins = await bot_obj.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                await bot_obj.send_message(admin.user.id, f"🚨 [Anti-Leak] {text}")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[ANTI-LINK] Ошибка уведомления админов: {e}")


# ─── Реакция на утечку (revoke + slowmode) ────────────────────────────────

async def _react_to_leak_discovery(chat_id: int, leaked_hash: str):
    cid = str(chat_id)
    _ensure_anti_link_leak_settings(cid)
    cfg = settings[cid]["anti_link_leak"]
    if not cfg.get("enabled", False):
        return

    actions = []

    if cfg.get("auto_revoke", True):
        try:
            revoke_invite_link_record(leaked_hash)
            actions.append(f"🔗 Ссылка +{leaked_hash} отозвана (registry)")
        except Exception as e:
            logger.error(f"[ANTI-LINK] Ошибка ревока: {e}")

    if cfg.get("join_request_mode", False):
        actions.append("📋 Включите Join Request вручную в настройках чата")

    recent = get_recent_joins(chat_id, minutes=10)
    banned_count = 0
    for ev in recent:
        if ev.get("invite_hash") == leaked_hash:
            uid = ev["user_id"]
            await _apply_link_leak_reaction(chat_id, uid, cfg, "бан", "вход через сливнутую ссылку")
            banned_count += 1
    if banned_count:
        actions.append(f"🚫 Наказано недавних входов: {banned_count}")

    if cfg.get("notify_admins", True):
        actions_text = "\n".join(actions) if actions else "Действия не требуются"
        await _notify_admins_about_leak(
            bot, chat_id,
            f"Обнаружена утечка +{leaked_hash}!\n{actions_text}"
        )

    logger.warning(f"[ANTI-LINK] Реакция на утечку +{leaked_hash} chat={chat_id}: {actions}")


# ─── Фоновая задача: проверка registry ────────────────────────────────────

async def periodic_leak_check():
    while True:
        try:
            await asyncio.sleep(30)
            registry = get_leak_registry()
            for ll in registry.get("leaked_links", []):
                if ll.get("status") != "active":
                    continue
                h = ll.get("hash")
                for hash_val, info in registry.get("invite_links", {}).items():
                    if hash_val == h and not info.get("is_revoked", False):
                        chat_id = info.get("chat_id")
                        if chat_id:
                            await _react_to_leak_discovery(chat_id, h)
                            ll["status"] = "reacted"
                            save_leak_registry()
            cleanup_old_join_events(hours=48)
        except Exception as e:
            logger.error(f"[ANTI-LINK] Ошибка periodic check: {e}")


# ─── Публичный API для userbot ────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════
#  USERBOT: per-chat Pyrogram клиент для мониторинга raid-каналов
# ═══════════════════════════════════════════════════════════════════════════

_userbot_clients: dict = {}   # cid -> pyrogram.Client
_userbot_tasks: dict = {}     # cid -> asyncio.Task

# Raid-каналы по умолчанию (как в bottt.py)
_DEFAULT_RAID_CHANNELS = [
    "pdvltgk", "haussllii", "K1SSM1T3",
]


async def _connect_and_verify_userbot(cid: str) -> tuple:
    """Подключает userbot, проверяет raid-каналы. Возвращает (ok, message)."""
    if UserClient is None:
        return False, "pyrogram не установлен"

    cfg = settings[cid]["anti_link_leak"]
    session = cfg.get("userbot_session")
    api_id = cfg.get("userbot_api_id")
    api_hash = cfg.get("userbot_api_hash")

    if not all([session, api_id, api_hash]):
        return False, "неполные данные userbot"

    try:
        api_id_int = int(api_id)
    except ValueError:
        return False, "API_ID должен быть числом"

    client = UserClient(
        f"ub_{cid}",
        api_id=api_id_int,
        api_hash=api_hash,
        session_string=session,
        in_memory=True,
    )

    try:
        await client.start()
        me = await client.get_me()
    except Exception as e:
        return False, f"не удалось авторизоваться: {e}"

    # Проверяем raid-каналы
    missing = []
    for ch in _DEFAULT_RAID_CHANNELS:
        try:
            await client.get_chat(ch)
        except Exception:
            missing.append(ch)

    if missing:
        await client.stop()
        return False, (
            f"аккаунт не состоит в каналах: {', '.join(missing)}. "
            f"Добавь аккаунт в эти каналы и переподключи."
        )

    # Каналы ок
    cfg["userbot_connected"] = True
    cfg["userbot_channels_ok"] = True
    save_settings(cid)

    _userbot_clients[cid] = client
    # Запускаем мониторинг
    asyncio.create_task(_run_userbot_monitor(cid, client))

    return True, f"@{me.username or me.first_name}"


async def _run_userbot_monitor(cid: str, client):
    """Фоновая задача: сканит raid-каналы через userbot."""
    logger.info(f"[USERBOT] Мониторинг запущен для чата {cid}")

    try:
        # Резолвим каналы
        resolved = {}
        for ch in _DEFAULT_RAID_CHANNELS:
            try:
                chat = await client.get_chat(ch)
                resolved[chat.id] = chat.title
            except Exception as e:
                logger.warning(f"[USERBOT] Не удалось получить канал {ch}: {e}")

        if not resolved:
            logger.error(f"[USERBOT] Ни один канал не доступен для {cid}")
            return

        chat_ids = list(resolved.keys())

        @client.on_message(pyro_filters.chat(chat_ids))
        async def _handler(_, message):
            await _process_userbot_message(_, message, cid)

        # idle — держим клиент живым
        from pyrogram import idle
        await idle()

    except Exception as e:
        logger.error(f"[USERBOT] Ошибка мониторинга {cid}: {e}")
    finally:
        try:
            await client.stop()
        except Exception:
            pass
        if cid in _userbot_clients:
            del _userbot_clients[cid]


async def _process_userbot_message(client, message, cid: str):
    """Обрабатывает сообщение из raid-канала: ищет invite links,
    проверяет что userbot админ в чате, только тогда репортит."""
    from pyrogram.types import Message as PyroMessage
    msg: PyroMessage = message

    # Извлекаем хеши (как в bottt.py)
    hashes = set()
    for source in (msg.text, msg.caption):
        if source:
            for m in _INVITE_LINK_PATTERN.finditer(source):
                hashes.add(m.group(1))

    # entities
    for entities, source_text in [
        (msg.entities, msg.text),
        (msg.caption_entities, msg.caption),
    ]:
        if not entities:
            continue
        for ent in entities:
            url = None
            if ent.type.name == "TEXT_LINK":
                url = ent.url
            elif ent.type.name == "URL" and source_text:
                url = source_text[ent.offset:ent.offset + ent.length]
            if url:
                m = _INVITE_LINK_PATTERN.search(url)
                if m:
                    hashes.add(m.group(1))

    if not hashes:
        return

    source_chat_id = msg.chat.id if msg.chat else None
    source_chat_name = msg.chat.title if msg.chat else None

    for h in hashes:
        full_link = f"https://t.me/+{h}"
        target_chat = None

        # Пробуем получить чат по ссылке (без вступления если возможно)
        try:
            target_chat = await client.get_chat(full_link)
        except pyro_errors.UserAlreadyParticipant:
            # Уже в чате — резолвим по invite link
            try:
                target_chat = await client.get_chat(full_link)
            except Exception:
                pass
        except pyro_errors.InviteHashExpired:
            logger.info(f"[USERBOT] Ссылка +{h} уже истекла, пропускаем")
            continue
        except pyro_errors.InviteHashInvalid:
            logger.info(f"[USERBOT] Ссылка +{h} невалидна, пропускаем")
            continue
        except pyro_errors.ChannelInvalid:
            logger.info(f"[USERBOT] Не удалось резолвить +{h}")
            continue
        except pyro_errors.FloodWait as e:
            logger.warning(f"[USERBOT] FloodWait {e.value}с на get_chat +{h}")
            await asyncio.sleep(e.value)
            continue
        except Exception as e:
            # Fallback: пробуем вступить чтобы получить chat_id
            logger.info(f"[USERBOT] get_chat +{h} не сработал ({type(e).__name__}), пробуем join_chat")
            try:
                target_chat = await client.join_chat(full_link)
            except pyro_errors.UserAlreadyParticipant:
                try:
                    target_chat = await client.get_chat(full_link)
                except Exception:
                    pass
            except pyro_errors.InviteHashExpired:
                logger.info(f"[USERBOT] Ссылка +{h} истекла")
                continue
            except pyro_errors.InviteHashInvalid:
                logger.info(f"[USERBOT] Ссылка +{h} невалидна")
                continue
            except Exception as e2:
                logger.info(f"[USERBOT] join_chat +{h} тоже не сработал: {type(e2).__name__}: {e2}")
                continue

        if target_chat is None:
            continue

        target_chat_id = target_chat.id

        # Проверяем: userbot участник этого чата?
        try:
            my_member = await client.get_chat_member(target_chat_id, "me")
        except pyro_errors.PeerIdInvalid:
            logger.info(f"[USERBOT] Не участник чата {target_chat_id}, пропускаем +{h}")
            continue
        except pyro_errors.ChannelPrivate:
            logger.info(f"[USERBOT] Чат {target_chat_id} приватный, не участник")
            continue
        except Exception as e:
            logger.info(f"[USERBOT] get_chat_member ошибка: {type(e).__name__}: {e}")
            continue

        if my_member is None:
            continue

        # Проверяем что userbot — участник этого чата (владелец, админ или рядовой)
        # Pyrogram: ChatMemberStatus.OWNER, ADMINISTRATOR, MEMBER, RESTRICTED, LEFT, BANNED
        try:
            from pyrogram.enums import ChatMemberStatus as PyroStatus
            _admin_statuses = (PyroStatus.OWNER, PyroStatus.ADMINISTRATOR)
        except Exception:
            # Fallback: сравниваем строки (поддержка разных версий pyrogram)
            _status_str = str(my_member.status).lower()
            _admin_statuses = None

        if _admin_statuses is not None:
            is_admin = my_member.status in _admin_statuses
        else:
            # fallback path
            is_admin = any(s in _status_str for s in ("owner", "creator", "administrator"))

        if not is_admin:
            logger.info(
                f"[USERBOT] Ссылка +{h} ведёт в {target_chat_id}, "
                f"но userbot не админ (status={my_member.status}), пропускаем"
            )
            continue

        logger.warning(
            f"[USERBOT] УТЕЧКА: +{h} ведёт в {target_chat_id} "
            f"(userbot участник/админ, status={my_member.status})"
        )

        # Регистрируем invite link для этого чата
        if not get_chat_invite_links(target_chat_id).get(h):
            register_invite_link(target_chat_id, h, is_primary=False)

        # Проверяем: ссылка уже в реестре утечек (повторное появление в рейд-канале)?
        already_leaked = any(
            ll.get("hash") == h and ll.get("status") not in ("revoked", None)
            for ll in get_leak_registry().get("leaked_links", [])
        )

        # Регистрируем/обновляем утечку
        register_leaked_link(
            invite_hash=h,
            source_chat_id=source_chat_id,
            source_chat_name=source_chat_name,
            source_message_id=msg.id,
            context_text=(msg.text or msg.caption or "")[:500],
            leak_confidence=0.9,
        )

        if already_leaked:
            # Ссылка уже известна — молча реагируем без повторного уведомления
            logger.info(f"[USERBOT] Повторное появление +{h} в рейд-канале, реагируем снова")
            await _react_to_leak_discovery(target_chat_id, h)
            continue

        # Уведомляем чат об утечке (в тест-режиме — подробно, иначе — кратко)
        target_cfg = settings.get(str(target_chat_id), {}).get("anti_link_leak", {})
        try:
            if target_cfg.get("test_mode"):
                await bot.send_message(
                    target_chat_id,
                    f"🧪 <b>[ТЕСТ] Userbot обнаружил утечку!</b>\n\n"
                    f"🔗 Ссылка: <code>t.me/+{h}</code>\n"
                    f"📢 Источник: <b>{source_chat_name or '???'}</b>\n"
                    f"🆔 ID источника: <code>{source_chat_id}</code>\n\n"
                    f"В боевом режиме: ссылка будет отозвана, все вошедшие — забанены.",
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    target_chat_id,
                    f"🚨 <b>Обнаружена утечка ссылки!</b>\n"
                    f"Ссылка <code>+{h}</code> слита в <b>{source_chat_name or '???'}</b>.\n"
                    f"Принимаю меры…",
                    parse_mode="HTML",
                )
        except Exception:
            pass

        # Реагируем в чате
        await _react_to_leak_discovery(target_chat_id, h)


async def _ensure_userbot_task(cid: str):
    """Убеждаемся что userbot задача запущена для чата."""
    if cid in _userbot_tasks:
        return
    ok, msg = await _connect_and_verify_userbot(cid)
    if not ok:
        logger.error(f"[USERBOT] Не удалось запустить для {cid}: {msg}")


def report_leaked_link(invite_hash: str, source_chat_id: int = None,
                       source_chat_name: str = None,
                       source_message_id: int = None,
                       context_text: str = None):
    register_leaked_link(
        invite_hash=invite_hash,
        source_chat_id=source_chat_id,
        source_chat_name=source_chat_name,
        source_message_id=source_message_id,
        context_text=context_text,
        leak_confidence=0.9,
    )
    logger.warning(f"[ANTI-LINK] Userbot зарепортил утечку: +{invite_hash} из {source_chat_name}")


# ─── ЛС-команда: !юзербот (подключение userbot'а админа) ──────────────────

@router.message(
    F.text.startswith(("!юзербот", ".юзербот")),
    F.chat.type == ChatType.PRIVATE,
)
async def handle_userbot_connect_private(message: Message):
    """Подключение userbot в ЛС: !юзербот <CHAT_ID> <SESSION> <API_ID> <API_HASH>"""
    user_id = message.from_user.id
    parts = message.text.strip().split()

    if len(parts) < 5:
        return await message.answer(
            "<b>Подключение userbot для антиссылок</b>\n\n"
            "Использование:\n"
            "<code>!юзербот CHAT_ID SESSION_STRING API_ID API_HASH</code>\n\n"
            "Пример:\n"
            "<code>!юзербот -1001234567890 AgA0xABCDE... 38876656 99ce1cc5...</code>\n\n"
            "<b>Шаги:</b>\n"
            "1. Получи SESSION_STRING каким угодно образом\n"
            "2. Получи API_ID/API_HASH на https://my.telegram.org\n"
            "3. Узнай CHAT_ID группы\n"
            "4. Введи команду выше\n\n"
            "⚠️ Не показывай SESSION_STRING никому!",
            parse_mode="HTML",
        )

    try:
        target_chat_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ CHAT_ID должен быть числом (например -1001234567890).")

    cid = str(target_chat_id)
    session_str = parts[2]
    api_id = parts[3]
    api_hash = parts[4]

    # Проверяем что пользователь админ в указанном чате
    try:
        member = await bot.get_chat_member(target_chat_id, user_id)
        if member.status not in ("administrator", "creator"):
            return await message.answer("❌ Ты не админ в этой группе.")
    except Exception as e:
        return await message.answer(f"❌ Не удалось проверить права. Убедись что бот в группе и ты админ.\n({e})")

    _ensure_anti_link_leak_settings(cid)
    cfg = settings[cid]["anti_link_leak"]

    # Сохраняем данные
    cfg["userbot_session"] = session_str
    cfg["userbot_api_id"] = api_id
    cfg["userbot_api_hash"] = api_hash
    cfg["userbot_connected"] = False
    cfg["userbot_channels_ok"] = False
    save_settings(cid)

    await message.answer("⏳ Подключаю userbot и проверяю каналы...")

    ok, msg = await _connect_and_verify_userbot(cid)
    if ok:
        import html as _html
        await message.answer(
            f"✅ <b>Userbot подключён!</b>\n"
            f"Аккаунт: <b>{_html.escape(str(msg))}</b>\n"
            f"Рейд-каналы: ✅\n\n"
            f"Теперь в группе напиши:\n"
            f"<code>!антиссылки вкл</code>",
            parse_mode="HTML",
        )
    else:
        import html as _html
        await message.answer(
            f"❌ <b>Ошибка подключения:</b>\n"
            f"<code>{_html.escape(str(msg))}</code>\n\n"
            f"Проверь:\n"
            f"• SESSION_STRING валидный\n"
            f"• API_ID и API_HASH верные\n"
            f"• Аккаунт добавлен в рейд-каналы\n\n"
            f"Потом повтори команду.",
            parse_mode="HTML",
        )
