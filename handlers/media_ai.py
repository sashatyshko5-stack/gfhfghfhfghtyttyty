"""Анализ аудио/видео в реплай боту (без поиска и выдачи треков)."""
import logging
import os
import tempfile

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message

from ..core.loader import bot
from ..services.media_tools import analyze_video_via_gemini, describe_audio_via_gemini

log = logging.getLogger(__name__)
router = Router()


def _has_media_ai_payload(message: Message) -> bool:
    if message.audio or message.voice or message.video or message.video_note:
        return True
    d = message.document
    if d and d.mime_type:
        mt = d.mime_type.lower()
        if mt.startswith("audio/") or mt == "application/ogg":
            return True
    return False


class MediaAiMessageFilter(BaseFilter):
    """Аудио/видео/документ-аудио: в ЛС — всегда; в группе — только реплай на сообщение бота."""

    async def __call__(self, message: Message) -> bool:
        if not _has_media_ai_payload(message):
            return False
        if message.chat.type == ChatType.PRIVATE:
            return True
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
        me = await message.bot.get_me()
        r = message.reply_to_message
        return bool(r and r.from_user and r.from_user.id == me.id)


@router.message(MediaAiMessageFilter())
async def on_media(message: Message):
    st = await message.reply("слушаю…")
    m = message.audio or message.voice or message.video or message.video_note
    ext = "ogg" if message.voice else ("mp4" if (message.video or message.video_note) else "m4a")
    if not m and message.document:
        m = message.document
        fn = (message.document.file_name or "").lower()
        if fn.endswith(".mp3"):
            ext = "mp3"
        elif fn.endswith(".ogg") or fn.endswith(".opus"):
            ext = "ogg"
        else:
            ext = "m4a"
    if not m:
        return await st.edit_text("не вижу файла")

    tmp = tempfile.mkdtemp(prefix="media_")
    path = os.path.join(tmp, f"in.{ext}")
    try:
        tg = await bot.get_file(m.file_id)
        await bot.download_file(tg.file_path, path)
    except Exception as e:
        return await st.edit_text(f"не скачал: {e}")

    raw_desc = ""
    kind = "медиа"

    if message.video or message.video_note:
        kind = "видео"
        await st.edit_text("смотрю…")
        raw_desc = await analyze_video_via_gemini(path)
    else:
        kind = "голосовое" if message.voice else "трек"
        await st.edit_text("распознаю…")
        raw_desc = await describe_audio_via_gemini(path)

    from ..services.ai_module import lively_rewrite

    lively = await lively_rewrite(raw_desc, message.chat.id, kind=kind) if raw_desc else ""

    final = lively or raw_desc or "хз чё это"
    try:
        await st.edit_text(final, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        try:
            await st.edit_text(final, disable_web_page_preview=True)
        except Exception as e:
            log.error(f"[media reply] {e}")
