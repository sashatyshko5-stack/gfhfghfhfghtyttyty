"""Доп команды: !стата, !топ, !приветствие, !варн, !слоумод."""
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import Message, ChatPermissions

from ..core.utils import is_admin
from ..storage.state import settings, save_settings
from ..storage.message_logs import get_chat_messages  # убираем message_logs отсюда
from ..core.logging_setup import log_short, log_full

router = Router()
WARN_LIMIT = 3


@router.message(F.text.in_({"!стата", ".стата"}))
async def stats_cmd(message: Message):
    chat_id = message.chat.id
    logs = get_chat_messages(chat_id, limit=500)
    total = len(logs)
    users = {}
    today = datetime.now().date()
    today_count = 0
    
    for item in logs:
        if not isinstance(item, dict):
            continue
        uid = item.get("user_id")
        if uid:
            users[uid] = users.get(uid, 0) + 1
        ts = item.get("date")
        if ts:
            try:
                if datetime.fromisoformat(str(ts)[:19]).date() == today:
                    today_count += 1
            except Exception:
                pass
    
    await message.reply(
        f"📊 <b>Статистика группы</b>\n• Всего: <b>{total}</b>\n"
        f"• Сегодня: <b>{today_count}</b>\n• Авторов: <b>{len(users)}</b>",
        parse_mode="HTML")
    log_short(chat_id, f"!стата total={total}")

import time
from aiogram.filters import Command

import time
from aiogram.filters import Command

import time
from aiogram.filters import Command

@router.message(Command("ping"))
async def ping_command(message: Message):
    start = time.perf_counter()

    await message.answer("🚀 Понг!")

    ping = (time.perf_counter() - start) * 1000

    await message.answer(f"⚡ Задержка: {ping:.0f} мс")
    
@router.message(F.text.in_({"!топ", ".топ"}))
async def top_cmd(message: Message):
    chat_id = message.chat.id
    # Исправлено: используем ту же функцию get_chat_messages
    logs = get_chat_messages(chat_id, limit=1000)  # берем больше записей для топа
    counter = {}
    names = {}
    
    for item in logs:
        if not isinstance(item, dict):
            continue
        uid = item.get("user_id")
        if not uid:
            continue
        counter[uid] = counter.get(uid, 0) + 1
        if uid not in names:
            # Сохраняем имя пользователя
            names[uid] = item.get("user_name") or item.get("username") or f"User_{uid}"
    
    if not counter:
        return await message.reply("📉 Нет данных для статистики.")
    
    top = sorted(counter.items(), key=lambda x: -x[1])[:10]
    medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 7
    lines = ["🏆 <b>Топ активных пользователей:</b>"]
    
    for i, (uid, cnt) in enumerate(top):
        name = names.get(uid, str(uid))
        # Обрезаем длинные имена
        if len(name) > 20:
            name = name[:17] + "..."
        lines.append(f"{medals[i]} {name} — <b>{cnt}</b> 💬")
    
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.startswith(("!приветствие", ".приветствие")))
async def welcome_cmd(message: Message):
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")
    
    prefix = "!приветствие" if message.text.startswith("!приветствие") else ".приветствие"
    body = message.text[len(prefix):].strip()
    cid = str(message.chat.id)
    settings.setdefault(cid, {})
    
    if not body:
        settings[cid].pop("welcome_message", None)
        save_settings(cid)
        return await message.reply("🧹 Приветствие сброшено.")

    settings[cid]["welcome_message"] = body
    save_settings(cid)
    await message.reply("✅ Приветствие сохранено. ")


@router.message(F.text.startswith(("!варн", ".варн")))
async def warn_cmd(message: Message):
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")
    
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    elif message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                target = ent.user
                break
    
    if not target:
        return await message.reply("❗ Ответь на сообщение или упомяни пользователя.")
    
    if target.id == message.from_user.id:
        return await message.reply("🤔 Нельзя выдать варн самому себе.")
    
    cid = str(message.chat.id)
    settings.setdefault(cid, {})
    settings[cid].setdefault("warns", {})
    key = str(target.id)
    count = settings[cid]["warns"].get(key, 0) + 1
    settings[cid]["warns"][key] = count
    save_settings(cid)

    if count >= WARN_LIMIT:
        try:
            await message.bot.restrict_chat_member(
                message.chat.id,
                target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1)
            )
            settings[cid]["warns"][key] = 0
            save_settings(cid)
            await message.reply(f"🚫 {target.full_name} замьючен на 1 час ({WARN_LIMIT}/{WARN_LIMIT}).")
        except Exception as e:
            log_full(message.chat.id, "error", f"warn mute: {e}")
            await message.reply(f"⚠️ Ошибка при мьюте: {e}")
    else:
        await message.reply(
            f"⚠️ {target.full_name} получил варн <b>{count}/{WARN_LIMIT}</b>.",
            parse_mode="HTML"
        )


@router.message(F.text.in_({"!размут", ".размут"}))
async def unmute_cmd(message: Message):
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")
    if not message.reply_to_message:
        return await message.reply("❗ Используй реплай на сообщение того, кого хочешь размутить.")
    user_id = message.reply_to_message.from_user.id
    try:
        await message.chat.restrict(
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await message.reply("✅ Пользователь размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")
@router.message(
    F.chat.type.in_({"group", "supergroup"}) &
    F.text.startswith(("!разбан", ".разбан"))
)
async def unban_cmd(message: Message):
    
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")

    target = None

    # Через реплай
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user

    # Через упоминание
    elif message.entities:
        for ent in message.entities:
            if ent.type == "mention":
                username = message.text[ent.offset: ent.offset + ent.length]
                try:
                    chat = await message.bot.get_chat(username)
                    target = chat
                    break
                except Exception:
                    pass
            elif ent.type == "text_mention" and ent.user:
                target = ent.user
                break

    if not target:
        return await message.reply(
            "❗ Ответь на сообщение пользователя или укажи @username.\n"
             "",
            parse_mode="HTML",
        )

    try:
        await message.bot.unban_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            only_if_banned=True,
        )
        # снимаем из защиты от авто-ребана
        try:
            cid = str(message.chat.id)
            if cid in settings and "bot_banned" in settings[cid]:
                if str(target.id) in settings[cid]["bot_banned"]:
                    del settings[cid]["bot_banned"][str(target.id)]
                    save_settings(cid)
        except Exception:
            pass
        await message.reply(f"✅ {target.full_name} разбанен.")
    except Exception as e:
        log_full(message.chat.id, "error", f"unban error: {e}")
        await message.reply(f"❌ Ошибка: {e}")

@router.message(F.text.startswith(("!слоумод", ".слоумод")))
async def slowmode_cmd(message: Message):
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")
    
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply(
            "📝 <b>Использование:</b> <code>!слоумод 10</code>\n"
            "📊 <b>Доступные значения:</b> 0, 10, 30, 60, 300, 900, 3600\n"
            "💡 <i>0 - выключить слоумод</i>",
            parse_mode="HTML"
        )
    
    seconds = int(parts[1])
    allowed = {0, 10, 30, 60, 300, 900, 3600}
    
    if seconds not in allowed:
        return await message.reply(
            f"❌ Недопустимое значение. Разрешены: {sorted(allowed)}",
            parse_mode="HTML"
        )
    
    try:
        # Правильный метод для aiogram 3.x
        await message.chat.set_slow_mode(slow_mode_delay=seconds)
        
        if seconds > 0:
            await message.reply(
                f"🐢 <b>Слоумод активирован!</b>\n"
                f"⏱ Пользователи смогут писать раз в <b>{seconds}</b> секунд.",
                parse_mode="HTML"
            )
        else:
            await message.reply(
                "🐢 <b>Слоумод выключен.</b>\n"
                "✅ Пользователи снова могут писать без ограничений.",
                parse_mode="HTML"
            )
    except Exception as e:
        log_full(message.chat.id, "error", f"slowmode error: {e}")
        await message.reply(
            f"⚠️ <b>Ошибка:</b> {e}\n"
            f"💡 Убедитесь, что бот имеет права администратора.",
            parse_mode="HTML"
        )