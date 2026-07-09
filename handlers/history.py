from aiogram import Router, F
from aiogram.types import Message

from ..storage.state import chat_histories

router = Router()


@router.message(F.text == "!история")
async def show_history(message: Message):
    chat_id = message.chat.id
    history = chat_histories.get(chat_id, [])
    if not history:
        return await message.reply("История пуста.")
    lines = [f"{h['role']}: {h['content'][:100]}" for h in history[-10:]]
    await message.reply("\n".join(lines))
