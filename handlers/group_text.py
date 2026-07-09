import re
from aiogram import Router, F
from aiogram.types import Message

from ..services.ai_module import get_ai_reply
from ..services.ai_command_parser import extract_and_execute_commands
from ..services import chat_ai_router as _car
from ..core.loader import bot
from ..core.outgoing_logger import ai_response_ctx

router = Router()

_bot_username_cache: dict = {"value": None, "id": None}


async def _get_bot_identity():
    if _bot_username_cache["value"] is None:
        try:
            me = await bot.get_me()
            _bot_username_cache["value"] = (me.username or "").lower()
            _bot_username_cache["id"] = me.id
        except Exception:
            _bot_username_cache["value"] = ""
            _bot_username_cache["id"] = 0
    return _bot_username_cache["value"], _bot_username_cache["id"]


def _is_bot_mentioned(msg: Message, bot_username: str, bot_id: int) -> bool:
    if not msg.text:
        return False
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_id:
        return True
    text_lower = msg.text.lower()
    if bot_username and f"@{bot_username}" in text_lower:
        return True
    if msg.entities:
        for ent in msg.entities:
            if ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
                return True
            if ent.type == "mention":
                mention_txt = msg.text[ent.offset: ent.offset + ent.length].lower().lstrip("@")
                if bot_username and mention_txt == bot_username:
                    return True
    return False


def _strip_bot_mention(text: str, bot_username: str) -> str:
    if not text:
        return text
    if bot_username:
        text = re.sub(rf"(?i)@{re.escape(bot_username)}\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


@router.message(
    F.text,
    ~F.text.startswith(("!", ".", "/")),
    F.chat.type.in_({"group", "supergroup"}),
)
async def handle_text(msg: Message):
    text = msg.text.strip()
    chat_id = msg.chat.id

    bot_username, bot_id = await _get_bot_identity()

    if not _is_bot_mentioned(msg, bot_username, bot_id):
        return

    if not _car.is_ai_enabled(chat_id):
        return

    cleaned_text = _strip_bot_mention(text, bot_username)
    if not cleaned_text:
        return

    await bot.send_chat_action(chat_id, action="typing")

    ai_reply, source = await get_ai_reply(cleaned_text, chat_id, msg)

    final_reply, had_commands = await extract_and_execute_commands(msg, ai_reply)

    if not (final_reply and final_reply.strip()):
        return

    token = ai_response_ctx.set(True)
    try:
        try:
            await msg.reply(final_reply, parse_mode="Markdown")
        except Exception:
            await msg.reply(final_reply)
    finally:
        ai_response_ctx.reset(token)
