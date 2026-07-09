import os
from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from ..core.utils import is_admin
from ..core.logging_setup import SHORT_LOG_PATH, FULL_LOG_PATH, log_short

router = Router()

def _filter_chat_lines(path, chat_id, limit=40):
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0,2); size = f.tell(); f.seek(max(0,size-200_000))
        f.readline(); tail = f.readlines()
    needle = f"chat={chat_id}"
    return [ln.rstrip() for ln in tail if needle in ln][-limit:]

@router.message(F.text.startswith(("!логи",".логи")))
async def show_logs(message: Message):
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")
    if "полн" in message.text.lower() or "full" in message.text.lower():
        if not os.path.exists(FULL_LOG_PATH):
            return await message.reply("📄 Пусто.")
        return await message.reply_document(FSInputFile(FULL_LOG_PATH),
            caption="📎 Полный лог (все события + AI)")
    lines = _filter_chat_lines(SHORT_LOG_PATH, message.chat.id, 40)
    if not lines: return await message.reply("📭 Логов нет.")
    body = "<pre>"+"\n".join(lines)+"</pre>"
    msg = f"📋 <b>Логи группы</b> ({len(lines)})\n\n{body}"
    if len(msg) > 4000:
        msg = f"📋 <b>Логи</b>\n\n<pre>"+"\n".join(lines[-25:])+"</pre>"
    try: await message.reply(msg, parse_mode="HTML")
    except Exception: await message.reply("\n".join(lines[-25:]))

@router.message(F.text.startswith(("!логи_очистить",".логи_очистить")))
async def clear_logs(message: Message):
    if not await is_admin(message):
        return await message.reply("❗ Только админ.")
    try:
        if os.path.exists(SHORT_LOG_PATH):
            open(SHORT_LOG_PATH,"w",encoding="utf-8").write("")
        log_short(message.chat.id, f"!логи_очистить {datetime.utcnow().isoformat()}")
        await message.reply("🗑 Короткие логи очищены. Детальный сохранён.")
    except Exception as e: await message.reply(f"❌ {e}")