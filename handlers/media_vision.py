"""
Анализ медиа через Google Gemini API — премиум-функция.

Доступ:
  • OWNER_ID       → всегда (для тестирования)
  • Личный премиум → работает в любом чате
  • Чат-премиум    → все участники этого чата

Триггер:
  • Группа: отправить медиа ОТВЕТОМ на сообщение бота
  • ЛС: просто отправить медиа

Подключение в main.py — ДО media_react и media_ai:
    dp.include_router(media_vision_router)
"""
import logging

import aiohttp
from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message

from ..core.loader import bot
from ..core.config import OWNER_ID
from ..storage.premium import has_premium, has_chat_premium

logger = logging.getLogger(__name__)
router = Router(name="media_vision")


# ─── Определение медиа ────────────────────────────────────────────────────────

def _get_media(message: Message):
    """Возвращает (media_object, kind, mime) или (None, None, None)."""
    if message.photo:
        return message.photo[-1], "photo", "image/jpeg"
    if message.animation:
        return message.animation, "gif", "video/mp4"
    if message.video:
        return message.video, "video", "video/mp4"
    if message.video_note:
        return message.video_note, "video_note", "video/mp4"
    if message.voice:
        return message.voice, "voice", "audio/ogg"
    if message.audio:
        return message.audio, "audio", "audio/mpeg"
    if message.sticker:
        st = message.sticker
        if getattr(st, "is_video", False):
            return st, "sticker", "video/webm"
        return st, "sticker", "image/webp"
    if message.document:
        mt = (message.document.mime_type or "").lower()
        if mt.startswith("image/") or mt.startswith("video/") \
                or mt.startswith("audio/") or mt == "application/ogg":
            return message.document, "document", mt
    return None, None, None


# ─── Фильтр доступа ───────────────────────────────────────────────────────────

class MediaVisionFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        media, kind, _ = _get_media(message)
        if not media:
            return False
        if not message.from_user:
            return False

        uid       = message.from_user.id
        chat_id   = message.chat.id
        chat_type = message.chat.type

        # ── ЛС ───────────────────────────────────────────────────────────────
        if chat_type == ChatType.PRIVATE:
            if uid == OWNER_ID:
                logger.info(f"[MEDIA-VISION] filter: ЛС owner {uid} → PASS (kind={kind})")
                return True
            prem = has_premium(uid)
            logger.info(f"[MEDIA-VISION] filter: ЛС uid={uid} has_premium={prem} kind={kind}")
            return prem

        # ── Группа: нужен реплай на бота ─────────────────────────────────────
        if chat_type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False

        r = message.reply_to_message
        if not r or not r.from_user:
            logger.debug(f"[MEDIA-VISION] filter: нет reply_to → пропуск uid={uid}")
            return False

        me = await message.bot.get_me()
        if r.from_user.id != me.id:
            logger.debug(f"[MEDIA-VISION] filter: reply не на бота → пропуск")
            return False

        if uid == OWNER_ID:
            logger.info(f"[MEDIA-VISION] filter: группа owner {uid} chat={chat_id} → PASS (kind={kind})")
            return True

        prem_user = has_premium(uid)
        prem_chat = has_chat_premium(chat_id)
        result    = prem_user or prem_chat
        logger.info(
            f"[MEDIA-VISION] filter: группа uid={uid} chat={chat_id} "
            f"has_premium={prem_user} has_chat_premium={prem_chat} kind={kind} "
            f"→ {'PASS' if result else 'SKIP'}"
        )
        return result


# ─── Скачивание файла ─────────────────────────────────────────────────────────

async def _download(file_id: str) -> bytes | None:
    try:
        logger.debug(f"[MEDIA-VISION] скачиваю file_id={file_id}")
        tg_file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{bot.token}/{tg_file.file_path}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=90)) as r:
                if r.status == 200:
                    data = await r.read()
                    logger.info(f"[MEDIA-VISION] скачано {len(data):,} байт")
                    return data
                logger.error(f"[MEDIA-VISION] download HTTP {r.status}")
                return None
    except Exception as e:
        logger.error(f"[MEDIA-VISION] download error: {e}", exc_info=True)
        return None


# ─── Обработчик ───────────────────────────────────────────────────────────────

_LABELS = {
    "photo":      ("🖼 Смотрю фото…",      "фото"),
    "gif":        ("🎞 Смотрю GIF…",        "GIF"),
    "video":      ("🎬 Смотрю видео…",      "видео"),
    "video_note": ("🎬 Смотрю кружочек…",   "видеосообщение"),
    "voice":      ("🎤 Слушаю голосовое…",  "голосовое"),
    "audio":      ("🎵 Слушаю аудио…",      "аудио"),
    "sticker":    ("🤔 Смотрю стикер…",     "стикер"),
    "document":   ("📁 Анализирую файл…",   "файл"),
}


@router.message(MediaVisionFilter())
async def on_media_vision(message: Message):
    from ..services.freetheai_vision import (
        analyze_photo, analyze_gif, analyze_video,
        analyze_sticker, analyze_audio,
    )

    media, kind, mime = _get_media(message)
    if not media:
        return

    uid = message.from_user.id if message.from_user else "?"
    logger.info(f"[MEDIA-VISION] → обработка uid={uid} kind={kind} mime={mime} chat={message.chat.id}")

    wait_text, kind_name = _LABELS.get(kind, ("⏳ Анализирую…", kind))
    st = await message.reply(wait_text)

    try:
        file_id = getattr(media, "file_id", None)
        if not file_id:
            logger.error(f"[MEDIA-VISION] нет file_id у media kind={kind}")
            await st.edit_text("❌ Не удалось получить файл.")
            return

        data = await _download(file_id)
        if not data:
            await st.edit_text("❌ Не удалось скачать. Попробуй ещё раз.")
            return

        logger.info(f"[MEDIA-VISION] отправляю в Gemini: kind={kind} size={len(data):,} bytes")
        result: str | None = None

        if kind == "photo":
            result = await analyze_photo(data, mime)

        elif kind == "gif":
            result = await analyze_gif(data)

        elif kind in ("video", "video_note"):
            result = await analyze_video(data, mime)

        elif kind in ("voice", "audio"):
            fn = "audio.ogg" if kind == "voice" else (
                getattr(media, "file_name", None) or "audio.mp3"
            )
            logger.info(f"[MEDIA-VISION] аудио filename={fn}")
            result = await analyze_audio(data, filename=fn)

        elif kind == "sticker":
            is_video    = getattr(message.sticker, "is_video", False)
            is_animated = getattr(message.sticker, "is_animated", False)
            logger.info(f"[MEDIA-VISION] стикер is_video={is_video} is_animated={is_animated}")
            if is_animated:
                thumb = getattr(message.sticker, "thumbnail", None)
                if thumb:
                    thumb_data = await _download(thumb.file_id)
                    if thumb_data:
                        result = await analyze_photo(thumb_data, "image/jpeg")
                if not result:
                    await st.edit_text("❌ Анимированные TGS-стикеры не поддерживаются.")
                    return
            else:
                result = await analyze_sticker(data, mime=mime, is_video=is_video)

        elif kind == "document":
            if mime.startswith("image/"):
                result = await analyze_photo(data, mime)
            elif mime.startswith("video/"):
                result = await analyze_video(data, mime)
            elif mime.startswith("audio/") or mime == "application/ogg":
                fn = getattr(message.document, "file_name", None) or "audio.mp3"
                result = await analyze_audio(data, filename=fn)
            else:
                logger.warning(f"[MEDIA-VISION] неподдерживаемый mime: {mime}")
                await st.edit_text("❌ Формат не поддерживается.")
                return

        logger.info(f"[MEDIA-VISION] Gemini ответил: {'OK len=' + str(len(result)) if result else 'None (ошибка)'}")

        if not result:
            await st.edit_text(
                "❌ Не удалось проанализировать. "
                "Возможно файл слишком большой или Gemini временно недоступен."
            )
            return

        header = f"<b>🤖 Анализ — {kind_name}</b>\n\n"
        full   = header + result
        if len(full) > 4096:
            full = full[:4090] + "…"

        await st.edit_text(full, parse_mode="HTML", disable_web_page_preview=True)
        logger.info(f"[MEDIA-VISION] ✅ ответ отправлен uid={uid} kind={kind}")

    except Exception as e:
        logger.exception(f"[MEDIA-VISION] необработанная ошибка uid={uid} kind={kind}: {e}")
        try:
            await st.edit_text(f"❌ Внутренняя ошибка: {e}")
        except Exception:
            pass
