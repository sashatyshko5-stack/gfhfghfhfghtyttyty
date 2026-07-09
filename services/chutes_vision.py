"""
Анализ медиа через Gemini 2.0 Flash на Chutes.ai.
Видео/GIF передаются нативно.
Аудио/голос — Whisper large-v3.
"""
import base64
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

from ..core.config import CHUTES_API_KEY  # ключ берётся из config.py
CHUTES_BASE    = "https://llm.chutes.ai/v1"
VISION_MODEL   = "google/gemini-2.0-flash"
AUDIO_MODEL    = "openai/whisper-large-v3"

MAX_VIDEO_BYTES = 19 * 1024 * 1024  # 19 MB


# ─── Базовый запрос с медиа ───────────────────────────────────────────────────

async def _media_request(
    media_bytes: bytes,
    mime: str,
    prompt: str,
    max_tokens: int = 1000,
) -> Optional[str]:
    size_kb = len(media_bytes) // 1024
    logger.info(f"[CHUTES] _media_request: mime={mime} size={size_kb}KB model={VISION_MODEL}")

    b64 = base64.b64encode(media_bytes).decode()
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{CHUTES_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {CHUTES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                body = await r.text()
                logger.info(f"[CHUTES] HTTP {r.status} | preview: {body[:200]}")
                if r.status != 200:
                    logger.error(f"[CHUTES] ошибка {r.status}: {body[:500]}")
                    return None
                import json as _json
                data = _json.loads(body)
                text = data["choices"][0]["message"]["content"]
                logger.info(f"[CHUTES] ответ получен, len={len(text)}")
                return text
    except Exception as e:
        logger.error(f"[CHUTES] исключение в _media_request: {e}", exc_info=True)
        return None


# ─── Текстовый запрос ────────────────────────────────────────────────────────

async def _text_request(prompt: str, max_tokens: int = 400) -> Optional[str]:
    logger.debug(f"[CHUTES] _text_request max_tokens={max_tokens}")
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{CHUTES_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {CHUTES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    logger.error(f"[CHUTES] _text_request HTTP {r.status}: {await r.text()[:200]}")
                    return None
                data = await r.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"[CHUTES] _text_request исключение: {e}", exc_info=True)
        return None


# ─── Аудио транскрипция ───────────────────────────────────────────────────────

async def _audio_transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
    mime_map = {
        "ogg": "audio/ogg", "opus": "audio/ogg",
        "mp3": "audio/mpeg", "m4a": "audio/mp4",
        "wav": "audio/wav", "flac": "audio/flac", "mp4": "video/mp4",
    }
    content_type = mime_map.get(ext, "audio/ogg")
    size_kb = len(audio_bytes) // 1024
    logger.info(f"[CHUTES-AUDIO] транскрипция: filename={filename} ext={ext} mime={content_type} size={size_kb}KB")

    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type=content_type)
    form.add_field("model", AUDIO_MODEL)
    form.add_field("response_format", "text")
    form.add_field("language", "ru")

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{CHUTES_BASE}/audio/transcriptions",
                headers={"Authorization": f"Bearer {CHUTES_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as r:
                body = await r.text()
                logger.info(f"[CHUTES-AUDIO] HTTP {r.status} | результат: {body[:300]}")
                if r.status != 200:
                    logger.error(f"[CHUTES-AUDIO] ошибка {r.status}: {body[:500]}")
                    return None
                result = body.strip()
                logger.info(f"[CHUTES-AUDIO] транскрипция готова len={len(result)}")
                return result
    except Exception as e:
        logger.error(f"[CHUTES-AUDIO] исключение: {e}", exc_info=True)
        return None


# ─── Публичные функции ────────────────────────────────────────────────────────

async def analyze_photo(image_bytes: bytes, mime: str = "image/jpeg") -> Optional[str]:
    logger.info(f"[CHUTES] analyze_photo mime={mime} size={len(image_bytes)//1024}KB")
    return await _media_request(
        image_bytes, mime,
        "Подробно опиши что изображено на этом фото. Отвечай по-русски.",
    )


async def analyze_gif(gif_bytes: bytes) -> Optional[str]:
    logger.info(f"[CHUTES] analyze_gif size={len(gif_bytes)//1024}KB")
    if len(gif_bytes) > MAX_VIDEO_BYTES:
        logger.warning(f"[CHUTES] GIF слишком большой: {len(gif_bytes)//1024//1024}MB > 19MB")
        return "❌ GIF слишком большой для анализа (лимит ~19 MB)."
    return await _media_request(
        gif_bytes, "video/mp4",
        "Это GIF-анимация. Подробно опиши что в ней происходит. Отвечай по-русски.",
        max_tokens=800,
    )


async def analyze_video(video_bytes: bytes, mime: str = "video/mp4") -> Optional[str]:
    logger.info(f"[CHUTES] analyze_video mime={mime} size={len(video_bytes)//1024}KB")
    if len(video_bytes) > MAX_VIDEO_BYTES:
        logger.warning(f"[CHUTES] видео слишком большое: {len(video_bytes)//1024//1024}MB > 19MB")
        return "❌ Видео слишком большое для анализа (лимит ~19 MB)."
    return await _media_request(
        video_bytes, mime,
        (
            "Подробно опиши это видео: что происходит, кто или что в нём, "
            "какой контекст, настроение. Если есть речь — передай суть. "
            "Отвечай по-русски."
        ),
        max_tokens=1000,
    )


async def analyze_sticker(sticker_bytes: bytes, mime: str = "image/webp", is_video: bool = False) -> Optional[str]:
    logger.info(f"[CHUTES] analyze_sticker mime={mime} is_video={is_video} size={len(sticker_bytes)//1024}KB")
    if is_video:
        if len(sticker_bytes) > MAX_VIDEO_BYTES:
            return "❌ Видео-стикер слишком большой."
        return await _media_request(
            sticker_bytes, "video/webm",
            "Это анимированный стикер Telegram. Опиши что на нём происходит. Отвечай по-русски.",
            max_tokens=400,
        )
    return await _media_request(
        sticker_bytes, mime,
        "Опиши этот стикер Telegram: что нарисовано, какая эмоция или сцена. Отвечай по-русски.",
        max_tokens=400,
    )


async def analyze_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
    logger.info(f"[CHUTES] analyze_audio filename={filename} size={len(audio_bytes)//1024}KB")
    transcript = await _audio_transcribe(audio_bytes, filename)

    if not transcript:
        logger.warning("[CHUTES] транскрипция вернула None")
        return None
    if len(transcript.strip()) < 5:
        logger.info("[CHUTES] транскрипция пустая/тихая")
        return "🎵 <i>(тишина или неразборчиво)</i>"

    summary = await _text_request(
        f"Транскрипция аудиосообщения:\n\n\"{transcript}\"\n\n"
        "Кратко (1-2 предложения) скажи о чём речь. Отвечай по-русски."
    )

    if summary:
        return (
            f"🎤 <b>Транскрипция:</b>\n<i>{transcript}</i>\n\n"
            f"💬 <b>Суть:</b> {summary}"
        )
    return f"🎤 <b>Транскрипция:</b>\n<i>{transcript}</i>"
