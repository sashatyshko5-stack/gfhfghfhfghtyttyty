import html
import logging
from aiogram import Router, F
from aiogram.types import Message

from ..core.config import OWNER_ID
from ..storage.moderators import (
    assign, set_level, demote, remove,
    is_moderator, list_all, LEVEL_NAMES,
)

logger = logging.getLogger(__name__)
router = Router()


def _parse_id(parts):
    if len(parts) < 2:
        return None
    raw = parts[1].strip().strip('"').strip("'")
    try:
        return int(raw)
    except ValueError:
        return None


@router.message(F.chat.type == "private", F.text.startswith("!назначить"))
async def cmd_assign(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    target = _parse_id(parts)
    if target is None:
        return await message.answer(
            "⚠️ Использование: <code>!назначить &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )
    if target == OWNER_ID:
        return await message.answer("❌ Нельзя назначить владельца — он и так выше всех.")
    rec = assign(target, by_user_id=OWNER_ID, level=1)
    await message.answer(
        f"✅ Пользователь <code>{target}</code> назначен модератором.\n"
        f"Уровень: <b>{rec['level']}</b> ({LEVEL_NAMES.get(rec['level'], '?')})\n"
        f"Теперь он видит обращения в поддержку и может отвечать через "
        f"<code>!ответ &lt;user_id&gt; &lt;текст&gt;</code>.",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", F.text.startswith("!повысить"))
async def cmd_promote(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        return await message.answer(
            "⚠️ Использование: <code>!повысить &lt;user_id&gt; &lt;уровень 1-4&gt;</code>\n"
            "Уровни: 1 — хелпер, 2 — модер, 3 — админ, 4 — владелец",
            parse_mode="HTML",
        )
    target = _parse_id(parts)
    if target is None:
        return await message.answer("❌ user_id должен быть числом.")
    try:
        level = int(parts[2].strip().strip('"').strip("'"))
    except ValueError:
        return await message.answer("❌ Уровень должен быть числом от 1 до 4.")
    if not (1 <= level <= 4):
        return await message.answer("❌ Уровень должен быть от 1 до 4.")
    if target == OWNER_ID:
        return await message.answer("❌ OWNER уже на максимуме.")
    rec = set_level(target, level, by_user_id=OWNER_ID)
    await message.answer(
        f"⬆️ Уровень <code>{target}</code> установлен: "
        f"<b>{rec['level']}</b> ({LEVEL_NAMES.get(rec['level'], '?')})",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", F.text.startswith("!понизить"))
async def cmd_demote(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    target = _parse_id(parts)
    if target is None:
        return await message.answer(
            "⚠️ Использование: <code>!понизить &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )
    ok, info = demote(target, by_user_id=OWNER_ID)
    if ok:
        await message.answer(f"⬇️ <code>{target}</code>: {html.escape(info)}", parse_mode="HTML")
    else:
        await message.answer(f"❌ {html.escape(info)}", parse_mode="HTML")


@router.message(F.chat.type == "private", F.text.startswith("!разжаловать"))
async def cmd_revoke(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    target = _parse_id(parts)
    if target is None:
        return await message.answer(
            "⚠️ Использование: <code>!разжаловать &lt;user_id&gt;</code>",
            parse_mode="HTML",
        )
    if remove(target):
        await message.answer(
            f"🗑 Пользователь <code>{target}</code> полностью разжалован.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"ℹ️ <code>{target}</code> не был в списке модераторов.",
            parse_mode="HTML",
        )


@router.message(F.chat.type == "private", F.text.startswith("!список_модераторов"))
async def cmd_list_mods(message: Message):
    uid = message.from_user.id
    if uid != OWNER_ID and not is_moderator(uid, min_level=1):
        return
    mods = list_all()
    if not mods:
        return await message.answer("ℹ️ Список модераторов пуст.")
    lines = ["👥 <b>Список модераторов:</b>\n"]
    for user_id, rec in mods:
        lvl = int(rec.get("level", 0))
        name = LEVEL_NAMES.get(lvl, "?")
        added_at = rec.get("added_at", "?")
        lines.append(
            f"• <code>{user_id}</code> — <b>{lvl}</b> ({name})  "
            f"<i>с {html.escape(str(added_at))}</i>"
        )
    lines.append(f"\nВсего: <b>{len(mods)}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")