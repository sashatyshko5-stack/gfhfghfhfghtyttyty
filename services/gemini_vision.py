"""
Анализ медиа через Google Gemini API (ai.google.dev).
Ключ бесплатный: https://aistudio.google.com/apikey

Поддерживает нативно: фото, GIF, видео, аудио, стикеры.
Для файлов > 15 MB используется File API (загрузка на сервер Google).
"""
import asyncio
import base64
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Ключ берётся из config.py
from ..core.config import GEMINI_API_KEY

GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta"
MODEL        = "gemini-2.0-flash"
INLINE_LIMIT = 15 * 1024 * 1024   # 15 MB — выше используем File API


# ─── Вспомогалки ─────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _inline_part(data: bytes, mime: str) -> dict:
    return {"inline_data": {"mime_type": mime, "data": _b64(data)}}


async def _upload_file(data: bytes, mime: str, display_name: str = "media") -> Optional[str]:
    """Загружает файл через Gemini File API и возвращает URI."""
    url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
    headers = {
        "X-Goog-Upload-Protocol": "multipart",
        "Content-Type": f'multipart/related; boundary="boundary"',
    }
    body = (
        b'--boundary\r\n'
        b'Content-Type: application/json; charset=UTF-8\r\n\r\n'
        + f'{{"file": {{"display_name": "{display_name}"}}}}'.encode()
        + b'\r\n--boundary\r\n'
        + f'Content-Type: {mime}\r\n\r\n'.encode()
        + data
        + b'\r\n--boundary--'
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, data=body,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                text = await r.text()
                logger.info(f"[GEMINI-UPLOAD] HTTP {r.status} | {text[:200]}")
                if r.status not in (200, 201):
                    logger.error(f"[GEMINI-UPLOAD] ошибка: {text[:500]}")
                    return None
                import json as _j
                resp = _j.loads(text)
                uri = resp.get("file", {}).get("uri")
                name = resp.get("file", {}).get("name", "")
                logger.info(f"[GEMINI-UPLOAD] загружен: {name} uri={uri}")
                return uri
    except Exception as e:
        logger.error(f"[GEMINI-UPLOAD] исключение: {e}", exc_info=True)
        return None


async def _wait_file_active(uri: str, retries: int = 10) -> bool:
    """Ждёт пока файл перейдёт в состояние ACTIVE."""
    # Извлекаем name из URI: .../files/abc123
    name = uri.split("/files/")[-1] if "/files/" in uri else ""
    if not name:
        return True  # предполагаем активен

    check_url = f"{GEMINI_BASE}/files/{name}?key={GEMINI_API_KEY}"
    for i in range(retries):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(check_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        import json as _j
                        d = _j.loads(await r.text())
                        state = d.get("state", "")
                        logger.debug(f"[GEMINI-UPLOAD] file state={state} attempt={i+1}")
                        if state == "ACTIVE":
                            return True
        except Exception:
            pass
        await asyncio.sleep(2)
    logger.warning(f"[GEMINI-UPLOAD] файл не стал ACTIVE за {retries} попыток")
    return False


async def _generate(parts: list, prompt: str, max_tokens: int = 1000) -> Optional[str]:
    """Отправляет запрос к Gemini generateContent."""
    url = f"{GEMINI_BASE}/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}] + parts}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                body = await r.text()
                logger.info(f"[GEMINI] HTTP {r.status} | preview: {body[:200]}")
                if r.status != 200:
                    logger.error(f"[GEMINI] ошибка {r.status}: {body[:500]}")
                    return None
                import json as _j
                data = _j.loads(body)
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                logger.info(f"[GEMINI] ответ получен len={len(text)}")
                return text
    except Exception as e:
        logger.error(f"[GEMINI] исключение: {e}", exc_info=True)
        return None


async def _media_part(data: bytes, mime: str, name: str = "media") -> Optional[list]:
    """
    Возвращает список из одного part — inline или file_data в зависимости от размера.
    """
    size = len(data)
    logger.info(f"[GEMINI] _media_part mime={mime} size={size//1024}KB")
    if size <= INLINE_LIMIT:
        return [_inline_part(data, mime)]
    # Большой файл — загружаем через File API
    logger.info(f"[GEMINI] файл большой ({size//1024//1024}MB), загружаю через File API…")
    uri = await _upload_file(data, mime, display_name=name)
    if not uri:
        return None
    await _wait_file_active(uri)
    return [{"file_data": {"mime_type": mime, "file_uri": uri}}]


# ─── Публичные функции ────────────────────────────────────────────────────────

async def analyze_photo(image_bytes: bytes, mime: str = "image/jpeg") -> Optional[str]:
    logger.info(f"[GEMINI] analyze_photo size={len(image_bytes)//1024}KB")
    parts = await _media_part(image_bytes, mime, "photo")
    if not parts:
        return None
    return await _generate(parts, "Подробно опиши что изображено на этом фото. Отвечай по-русски.")


async def analyze_gif(gif_bytes: bytes) -> Optional[str]:
    logger.info(f"[GEMINI] analyze_gif size={len(gif_bytes)//1024}KB")
    # Telegram шлёт GIF как MP4
    parts = await _media_part(gif_bytes, "video/mp4", "animation")
    if not parts:
        return None
    return await _generate(
        parts,
        "Это GIF-анимация. Подробно опиши что в ней происходит. Отвечай по-русски.",
        max_tokens=800,
    )


async def analyze_video(video_bytes: bytes, mime: str = "video/mp4") -> Optional[str]:
    logger.info(f"[GEMINI] analyze_video mime={mime} size={len(video_bytes)//1024}KB")
    parts = await _media_part(video_bytes, mime, "video")
    if not parts:
        return None
    return await _generate(
        parts,
        "Подробно опиши это видео: что происходит, кто или что в нём, "
        "контекст, настроение. Если есть речь — передай суть. Отвечай по-русски.",
        max_tokens=1000,
    )


async def analyze_sticker(sticker_bytes: bytes, mime: str = "image/webp", is_video: bool = False) -> Optional[str]:
    logger.info(f"[GEMINI] analyze_sticker mime={mime} is_video={is_video}")
    actual_mime = "video/webm" if is_video else mime
    parts = await _media_part(sticker_bytes, actual_mime, "sticker")
    if not parts:
        return None
    prompt = (
        "Это анимированный стикер Telegram. Опиши что на нём происходит. Отвечай по-русски."
        if is_video else
        "Опиши этот стикер Telegram: что нарисовано, эмоция или сцена. Отвечай по-русски."
    )
    return await _generate(parts, prompt, max_tokens=400)


async def analyze_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
    """Gemini слышит аудио нативно — транскрибирует и резюмирует за один запрос."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
    mime_map = {
        "ogg": "audio/ogg", "opus": "audio/ogg",
        "mp3": "audio/mpeg", "m4a": "audio/mp4",
        "wav": "audio/wav", "flac": "audio/flac", "mp4": "video/mp4",
    }
    mime = mime_map.get(ext, "audio/ogg")
    logger.info(f"[GEMINI] analyze_audio filename={filename} mime={mime} size={len(audio_bytes)//1024}KB")

    parts = await _media_part(audio_bytes, mime, filename)
    if not parts:
        return None

    return await _generate(
        parts,
        "Это аудиосообщение. Сначала точно транскрибируй речь, потом кратко (1-2 предложения) "
        "скажи о чём говорится. Формат ответа:\n"
        "🎤 <b>Транскрипция:</b>\n<i>[текст]</i>\n\n💬 <b>Суть:</b> [краткое резюме]\n"
        "Отвечай по-русски.",
        max_tokens=600,
    )
