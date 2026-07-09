"""Реакция на медиа-реплай к боту: стикер, фото, гифка.
Поток: скачиваем → анализ через laozhang.ai → переписываем в живом стиле основного ИИ → отвечаем.
"""
import logging
import os
import tempfile
import base64
import aiohttp

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message

from ..core.loader import bot
from ..services.ai_module import lively_rewrite, lively_postprocess
from ..services.gemini_pool import get_current_key, rotate

log = logging.getLogger(__name__)
router = Router()


def _is_reply_to_bot(message: Message, bot_id: int) -> bool:
    r = message.reply_to_message
    return bool(r and r.from_user and r.from_user.id == bot_id)


async def _is_mention_or_reply(message: Message) -> bool:
    """В группе срабатываем если: реплай к боту ИЛИ caption с упоминанием бота."""
    try:
        me = await bot.get_me()
        bot_id = me.id
        bot_username = (me.username or "").lower()
    except Exception:
        return False

    if _is_reply_to_bot(message, bot_id):
        return True

    cap = (message.caption or "").lower()
    if bot_username and f"@{bot_username}" in cap:
        return True

    if message.caption_entities:
        for ent in message.caption_entities:
            if ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
                return True
    return False


class MediaReactEligibleFilter(BaseFilter):
    """Только ответ боту / @mention в подписи — иначе хендлер не матчится и защиты (антиспам) получат апдейт."""

    async def __call__(self, message: Message) -> bool:
        return await _is_mention_or_reply(message)


async def _gemini_describe_file(path: str, prompt: str, mime_hint: str | None = None) -> str:
    """Анализ файла через laozhang.ai API с base64."""
    try:
        # Конвертируем файл в base64
        with open(path, "rb") as f:
            file_base64 = base64.b64encode(f.read()).decode("utf-8")
        
        # Используем mime_hint или определяем по расширению
        if not mime_hint:
            import mimetypes
            mime_hint, _ = mimetypes.guess_type(path)
            if not mime_hint:
                mime_hint = "image/jpeg"
        
        API_BASE_URL = "https://api.laozhang.ai/v1"
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {get_current_key()}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "gemini-2.5-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_hint};base64,{file_base64}"
                                }
                            }
                        ]
                    }
                ]
            }
            
            async with session.post(
                f"{API_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60
            ) as resp:
                if resp.status == 429:
                    rotate()
                    return ""
                
                resp.raise_for_status()
                data = await resp.json()
                
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    return content.strip()
                else:
                    return ""
                    
    except Exception as e:
        log.error(f"[gemini_describe] {e}")
        return ""


async def _download_to_tmp(file_id: str, ext: str) -> str | None:
    try:
        tg = await bot.get_file(file_id)
        tmp = tempfile.mkdtemp(prefix="react_")
        path = os.path.join(tmp, f"in.{ext}")
        await bot.download_file(tg.file_path, path)
        if os.path.getsize(path) < 100:
            return None
        return path
    except Exception as e:
        log.error(f"[download] {e}")
        return None


# ─────────── СТИКЕР ───────────
@router.message(
    F.sticker,
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    MediaReactEligibleFilter(),
)
async def on_sticker_reply(message: Message):
    st = message.sticker
    emoji = (st.emoji or "").strip()
    set_name = (st.set_name or "").strip()

    # Скачиваем картинку: для статичных webp / для видео-стикеров берём thumbnail
    path = None
    mime = None
    if getattr(st, "is_video", False) or getattr(st, "is_animated", False):
        thumb = getattr(st, "thumbnail", None)
        if thumb:
            path = await _download_to_tmp(thumb.file_id, "jpg")
            mime = "image/jpeg"
    else:
        path = await _download_to_tmp(st.file_id, "webp")
        mime = "image/webp"

    raw = ""
    if path:
        prompt = (
            f"Опиши КОРОТКО (1-2 фразы) что изображено на этом стикере. "
            f"Эмодзи стикера: {emoji or '—'}. Пак: {set_name or '—'}. "
            "Фокус: эмоция/мем/персонаж. Без воды."
        )
        raw = await _gemini_describe_file(path, prompt, mime_hint=mime)
        try:
            os.remove(path)
        except Exception:
            pass

    if not raw:
        # хотя бы по эмодзи
        raw = f"стикер с эмодзи {emoji}" if emoji else "какой-то стикер без описания"

    out = await lively_rewrite(raw, message.chat.id, kind="стикер")
    if out:
        try:
            await message.reply(out)
        except Exception as e:
            log.error(f"[sticker reply] {e}")


# ─────────── ФОТО ───────────
@router.message(
    F.photo,
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    MediaReactEligibleFilter(),
)
async def on_photo_reply(message: Message):
    photo = message.photo[-1]  # лучший размер
    path = await _download_to_tmp(photo.file_id, "jpg")
    if not path:
        return
    user_caption = (message.caption or "").strip()
    extra = f"\nПодпись от юзера: {user_caption}" if user_caption else ""
    prompt = (
        "Опиши КОРОТКО (2-3 фразы) что на этом фото: люди/предметы/обстановка/настроение. "
        "Без воды, без 'на изображении'." + extra
    )
    raw = await _gemini_describe_file(path, prompt, mime_hint="image/jpeg")
    try:
        os.remove(path)
    except Exception:
        pass
    if not raw:
        raw = "какое-то фото, толком не разобрал"
    out = await lively_rewrite(raw, message.chat.id, kind="фото")
    if out:
        try:
            await message.reply(out)
        except Exception as e:
            log.error(f"[photo reply] {e}")


# ─────────── ГИФКА (animation) ───────────
@router.message(
    F.animation,
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    MediaReactEligibleFilter(),
)
async def on_animation_reply(message: Message):
    anim = message.animation
    path = await _download_to_tmp(anim.file_id, "mp4")
    if not path:
        return
    user_caption = (message.caption or "").strip()
    extra = f"\nПодпись от юзера: {user_caption}" if user_caption else ""
    prompt = (
        "Это короткая GIF-анимация. Опиши КОРОТКО (2-3 фразы) что в ней происходит, "
        "какая эмоция/мем/действие. Без воды." + extra
    )
    raw = await _gemini_describe_file(path, prompt, mime_hint="video/mp4")
    try:
        os.remove(path)
    except Exception:
        pass
    if not raw:
        raw = "какая-то гифка, не понял что там"
    out = await lively_rewrite(raw, message.chat.id, kind="гифка")
    if out:
        try:
            await message.reply(out)
        except Exception as e:
            log.error(f"[anim reply] {e}")
