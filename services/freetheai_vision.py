"""
Анализ медиа через FreeTheAI (api.freetheai.xyz/v1) — бесплатно.
OpenAI-совместимый API. Ключ с Discord: https://freetheai.xyz

Поддержка: фото, GIF, видео (через ffmpeg), аудио/голос (Whisper).

При 429 (rate limit) читаем retry-after и ждём автоматически.
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import shutil
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

from ..core.config import FREETHEAI_API_KEY, GROQ_API_KEY

BASE_URL      = "https://api.freetheai.xyz/v1"
GROQ_BASE     = "https://api.groq.com/openai/v1"
VISION_MODEL  = "bbl/gemini-3.5-flash"    # Gemini 3.5 Flash с vision
GROQ_WHISPER  = "whisper-large-v3"        # Groq Whisper для аудио
MAX_WAIT      = 65
MAX_RETRIES   = 4


# ─── Вспомогалки ─────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _parse_retry_after(headers) -> float:
    for hdr in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        val = headers.get(hdr)
        if val:
            try:
                return min(float(val), MAX_WAIT)
            except ValueError:
                pass
    return 30.0


# ─── Vision (chat/completions) ────────────────────────────────────────────────

async def _vision_request(
    images: list[tuple[bytes, str]],   # [(bytes, mime), ...]
    prompt: str,
    max_tokens: int = 900,
) -> Optional[str]:
    """
    Запрос к OpenAI-совместимому chat/completions с vision.
    images — список (data, mime_type). Пустой список = только текст.
    Автоматический retry при 429.
    """
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {FREETHEAI_API_KEY}",
        "Content-Type": "application/json",
    }

    content: list[dict] = [{"type": "text", "text": prompt}]
    for data, mime in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{_b64(data)}"},
        })

    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    body = await r.text()
                    logger.info(f"[FREETHEAI] HTTP {r.status} attempt={attempt} | {body[:200]}")

                    if r.status == 429:
                        wait = _parse_retry_after(r.headers)
                        logger.warning(f"[FREETHEAI] rate limit, жду {wait:.1f}s (attempt {attempt}/{MAX_RETRIES})")
                        await asyncio.sleep(wait)
                        continue

                    if r.status != 200:
                        logger.error(f"[FREETHEAI] ошибка {r.status}: {body[:500]}")
                        return None

                    resp = json.loads(body)
                    text = resp["choices"][0]["message"]["content"]
                    logger.info(f"[FREETHEAI] ответ len={len(text)}")
                    return text

        except asyncio.TimeoutError:
            logger.warning(f"[FREETHEAI] timeout attempt={attempt}")
        except Exception as e:
            logger.error(f"[FREETHEAI] исключение: {e}", exc_info=True)
            return None

    logger.error("[FREETHEAI] все попытки исчерпаны")
    return None


# ─── Audio (transcription → Whisper или Gemini через текст) ──────────────────

async def _transcribe_via_gemini(audio_data: bytes, mime: str = "audio/ogg") -> Optional[str]:
    """
    Передаём аудио напрямую в Gemini через image_url поле (data URL).
    Gemini нативно понимает аудио — прокси может пропустить это поле.
    """
    logger.info(f"[FREETHEAI-AUDIO] пробую аудио напрямую в Gemini size={len(audio_data)//1024}KB")
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {FREETHEAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text":
                    "Это аудиосообщение. Сначала точно транскрибируй всю речь, "
                    "потом кратко (1-2 предложения) скажи о чём говорится. "
                    "Формат:\n🎤 Транскрипция: [текст]\n💬 Суть: [резюме]\nОтвечай по-русски."},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{_b64(audio_data)}"}},
            ],
        }],
        "max_tokens": 600,
        "temperature": 0.2,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    body = await r.text()
                    logger.info(f"[FREETHEAI-AUDIO] HTTP {r.status} attempt={attempt} | {body[:200]}")

                    if r.status == 429:
                        wait = _parse_retry_after(r.headers)
                        logger.warning(f"[FREETHEAI-AUDIO] rate limit, жду {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue

                    if r.status != 200:
                        logger.error(f"[FREETHEAI-AUDIO] ошибка {r.status}: {body[:400]}")
                        return None

                    resp = json.loads(body)
                    text = resp["choices"][0]["message"]["content"]
                    logger.info(f"[FREETHEAI-AUDIO] ответ len={len(text)}")
                    return text

        except Exception as e:
            logger.error(f"[FREETHEAI-AUDIO] {e}", exc_info=True)
            return None
    return None


# ─── Извлечение кадров через ffmpeg ──────────────────────────────────────────

def _extract_frames(video_bytes: bytes, n_frames: int = 3) -> list[bytes]:
    """Извлекает кадры через ffmpeg. Возвращает [] если ffmpeg недоступен."""
    frames: list[bytes] = []
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp()
        tmp_in  = os.path.join(tmp_dir, "input.mp4")
        with open(tmp_in, "wb") as f:
            f.write(video_bytes)

        out_pattern = os.path.join(tmp_dir, "frame_%03d.jpg")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in,
             "-vf", "fps=0.5,scale=1280:-2",
             "-frames:v", str(n_frames),
             "-q:v", "3", out_pattern],
            capture_output=True, timeout=60,
        )
        logger.info(f"[FFMPEG] rc={result.returncode}")

        for i in range(1, n_frames + 1):
            path = os.path.join(tmp_dir, f"frame_{i:03d}.jpg")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    frames.append(f.read())

    except FileNotFoundError:
        logger.warning("[FFMPEG] не найден")
    except subprocess.TimeoutExpired:
        logger.error("[FFMPEG] timeout")
    except Exception as e:
        logger.error(f"[FFMPEG] {e}", exc_info=True)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return frames


# ─── Публичные функции ────────────────────────────────────────────────────────

async def analyze_photo(image_bytes: bytes, mime: str = "image/jpeg") -> Optional[str]:
    logger.info(f"[FREETHEAI] analyze_photo size={len(image_bytes)//1024}KB")
    return await _vision_request(
        [(image_bytes, mime)],
        "Подробно опиши что изображено на этом фото. Отвечай по-русски.",
    )


async def analyze_gif(gif_bytes: bytes) -> Optional[str]:
    logger.info(f"[FREETHEAI] analyze_gif size={len(gif_bytes)//1024}KB")
    frames = await asyncio.to_thread(_extract_frames, gif_bytes, 2)
    if frames:
        imgs = [(f, "image/jpeg") for f in frames]
        return await _vision_request(
            imgs,
            "Это кадры из GIF-анимации (хронологически). Опиши что в ней происходит. Отвечай по-русски.",
            max_tokens=600,
        )
    # fallback — первый байт как изображение
    return await _vision_request(
        [(gif_bytes[:1_000_000], "image/jpeg")],
        "Опиши это изображение. Отвечай по-русски.",
        max_tokens=600,
    )


async def analyze_video(video_bytes: bytes, mime: str = "video/mp4") -> Optional[str]:
    logger.info(f"[FREETHEAI] analyze_video size={len(video_bytes)//1024}KB mime={mime}")

    # ── Попытка 1: слать видео напрямую как base64 data URL ───────────────────
    # Некоторые Gemini-прокси пропускают video/* через image_url поле.
    SIZE_LIMIT = 19 * 1024 * 1024  # >19 MB — не пробуем инлайн
    if len(video_bytes) <= SIZE_LIMIT:
        logger.info("[FREETHEAI] пробую видео напрямую (base64 inline)…")
        result = await _vision_request(
            [(video_bytes, mime)],
            "Это видео. Подробно опиши: что происходит, кто или что в нём, "
            "контекст, настроение. Если есть речь — передай суть. Отвечай по-русски.",
            max_tokens=1000,
        )
        if result:
            logger.info("[FREETHEAI] ✅ видео принято напрямую")
            return result
        logger.warning("[FREETHEAI] inline видео не сработало, пробую ffmpeg…")

    # ── Попытка 2: извлечь кадры через ffmpeg ─────────────────────────────────
    frames = await asyncio.to_thread(_extract_frames, video_bytes, 4)
    if not frames:
        return "❌ Прямая отправка видео не поддерживается сервисом, а ffmpeg не найден.\nУстанови: <code>apt install ffmpeg</code>"
    imgs = [(f, "image/jpeg") for f in frames]
    return await _vision_request(
        imgs,
        f"Это {len(frames)} кадра из видео (хронологически). "
        "Подробно опиши: что происходит, кто или что в видео, контекст, настроение. "
        "Используй все кадры чтобы понять сюжет. Отвечай по-русски.",
        max_tokens=1000,
    )


async def analyze_sticker(sticker_bytes: bytes, mime: str = "image/webp", is_video: bool = False) -> Optional[str]:
    logger.info(f"[FREETHEAI] analyze_sticker is_video={is_video}")
    if is_video:
        frames = await asyncio.to_thread(_extract_frames, sticker_bytes, 1)
        if frames:
            return await _vision_request(
                [(frames[0], "image/jpeg")],
                "Это кадр из анимированного стикера Telegram. Опиши что на нём. Отвечай по-русски.",
                max_tokens=300,
            )
    return await _vision_request(
        [(sticker_bytes, mime)],
        "Опиши этот стикер Telegram: что нарисовано, эмоция или сцена. Отвечай по-русски.",
        max_tokens=300,
    )


async def _groq_whisper(audio_data: bytes, filename: str = "audio.ogg") -> Optional[str]:
    """Транскрипция через Groq Whisper с автоматическим retry при 429."""
    url = f"{GROQ_BASE}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            form = aiohttp.FormData()
            form.add_field("model", GROQ_WHISPER)
            form.add_field("response_format", "text")
            form.add_field("language", "ru")
            form.add_field("file", audio_data, filename=filename, content_type="audio/ogg")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, data=form,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    body = await r.text()
                    logger.info(f"[GROQ-WHISPER] HTTP {r.status} attempt={attempt} | {body[:200]}")

                    if r.status == 429:
                        wait = _parse_retry_after(r.headers)
                        logger.warning(f"[GROQ-WHISPER] rate limit, жду {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue

                    if r.status == 200:
                        return body.strip()

                    logger.error(f"[GROQ-WHISPER] ошибка {r.status}: {body[:400]}")
                    return None

        except Exception as e:
            logger.error(f"[GROQ-WHISPER] {e}", exc_info=True)
            return None

    return None


async def analyze_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
    logger.info(f"[FREETHEAI] analyze_audio filename={filename} size={len(audio_bytes)//1024}KB")

    transcript = await _groq_whisper(audio_bytes, filename=filename)
    if not transcript:
        return "❌ Не удалось транскрибировать аудио. Попробуй позже."

    if len(transcript.split()) < 10:
        return f"🎤 <b>Транскрипция:</b>\n<i>{transcript}</i>"

    summary = await _vision_request(
        [],
        f"Транскрипция аудиосообщения:\n\n{transcript}\n\n"
        "Напиши краткое резюме (1-2 предложения). Отвечай по-русски.",
        max_tokens=200,
    )
    result = f"🎤 <b>Транскрипция:</b>\n<i>{transcript}</i>"
    if summary:
        result += f"\n\n💬 <b>Суть:</b> {summary}"
    return result
