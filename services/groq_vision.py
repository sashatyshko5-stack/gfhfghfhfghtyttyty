"""
Анализ медиа через Groq API — полностью бесплатно.

  • Фото / стикер / GIF → Llama 4 Scout (vision)
  • Видео              → ffmpeg извлекает кадры → Llama 4 Scout
  • Аудио / голос      → Whisper large-v3

При 429 (rate limit) читаем retry-after из заголовков и ждём,
затем повторяем запрос — пользователь ждёт, но получает результат.
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

from ..core.config import GROQ_API_KEY

GROQ_BASE     = "https://api.groq.com/openai/v1"
VISION_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"
WHISPER_MODEL = "whisper-large-v3"
MAX_WAIT      = 65       # секунд — максимальное ожидание одного кулдауна
MAX_RETRIES   = 4        # попыток при 429


# ─── Вспомогалки ─────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _parse_retry_after(headers: "aiohttp.ClientResponse.headers") -> float:
    """Читаем время ожидания из заголовков Groq."""
    for hdr in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        val = headers.get(hdr)
        if val:
            try:
                return min(float(val), MAX_WAIT)
            except ValueError:
                pass
    return 30.0  # fallback


# ─── Vision API ───────────────────────────────────────────────────────────────

async def _vision_request(
    images: list[tuple[bytes, str]],   # [(bytes, mime_type), ...]
    prompt: str,
    max_tokens: int = 900,
) -> Optional[str]:
    """
    Отправляет запрос к Groq vision (Llama 4 Scout).
    images — список (data, mime), обычно 1–4 кадра.
    При 429 ждёт retry-after и повторяет.
    """
    url = f"{GROQ_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
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
                    logger.info(f"[GROQ-VISION] HTTP {r.status} attempt={attempt} | {body[:200]}")

                    if r.status == 429:
                        wait = _parse_retry_after(r.headers)
                        logger.warning(f"[GROQ-VISION] rate limit, жду {wait:.1f}s (attempt {attempt})")
                        await asyncio.sleep(wait)
                        continue

                    if r.status != 200:
                        logger.error(f"[GROQ-VISION] ошибка {r.status}: {body[:500]}")
                        return None

                    resp = json.loads(body)
                    text = resp["choices"][0]["message"]["content"]
                    logger.info(f"[GROQ-VISION] ответ len={len(text)}")
                    return text

        except asyncio.TimeoutError:
            logger.warning(f"[GROQ-VISION] timeout attempt={attempt}")
        except Exception as e:
            logger.error(f"[GROQ-VISION] исключение: {e}", exc_info=True)
            return None

    logger.error("[GROQ-VISION] все попытки исчерпаны")
    return None


# ─── Whisper API ──────────────────────────────────────────────────────────────

async def _whisper(audio_data: bytes, filename: str = "audio.ogg") -> Optional[str]:
    """Транскрипция через Groq Whisper с ретраем при 429."""
    url = f"{GROQ_BASE}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            form = aiohttp.FormData()
            form.add_field("model", WHISPER_MODEL)
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

                    if r.status != 200:
                        logger.error(f"[GROQ-WHISPER] ошибка {r.status}: {body[:500]}")
                        return None

                    return body.strip()

        except asyncio.TimeoutError:
            logger.warning(f"[GROQ-WHISPER] timeout attempt={attempt}")
        except Exception as e:
            logger.error(f"[GROQ-WHISPER] исключение: {e}", exc_info=True)
            return None

    return None


# ─── Извлечение кадров из видео ───────────────────────────────────────────────

def _extract_frames(video_bytes: bytes, n_frames: int = 3) -> list[bytes]:
    """
    Извлекает до n_frames кадров через ffmpeg.
    Возвращает пустой список если ffmpeg недоступен.
    """
    tmp_in = tmp_dir = None
    frames: list[bytes] = []
    try:
        tmp_dir = tempfile.mkdtemp()
        tmp_in  = os.path.join(tmp_dir, "input.mp4")
        with open(tmp_in, "wb") as f:
            f.write(video_bytes)

        # 1 кадр каждые 2 секунды, берём первые n_frames
        out_pattern = os.path.join(tmp_dir, "frame_%03d.jpg")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", tmp_in,
                "-vf", f"fps=0.5,scale=1280:-2",
                "-frames:v", str(n_frames),
                "-q:v", "3",
                out_pattern,
            ],
            capture_output=True, timeout=60,
        )
        logger.info(f"[FFMPEG] returncode={result.returncode} stderr={result.stderr[-200:].decode(errors='ignore')}")

        for i in range(1, n_frames + 1):
            path = os.path.join(tmp_dir, f"frame_{i:03d}.jpg")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    frames.append(f.read())
                logger.info(f"[FFMPEG] кадр {i}: {len(frames[-1])} байт")

    except FileNotFoundError:
        logger.warning("[FFMPEG] ffmpeg не найден — видео-анализ недоступен")
    except subprocess.TimeoutExpired:
        logger.error("[FFMPEG] timeout при извлечении кадров")
    except Exception as e:
        logger.error(f"[FFMPEG] {e}", exc_info=True)
    finally:
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return frames


# ─── Публичные функции ────────────────────────────────────────────────────────

async def analyze_photo(image_bytes: bytes, mime: str = "image/jpeg") -> Optional[str]:
    logger.info(f"[GROQ] analyze_photo size={len(image_bytes)//1024}KB mime={mime}")
    return await _vision_request(
        [(image_bytes, mime)],
        "Подробно опиши что изображено на этом фото. Отвечай по-русски.",
    )


async def analyze_gif(gif_bytes: bytes) -> Optional[str]:
    logger.info(f"[GROQ] analyze_gif size={len(gif_bytes)//1024}KB")
    # GIF приходит как MP4 из Telegram — берём кадр
    frames = await asyncio.to_thread(_extract_frames, gif_bytes, 1)
    if frames:
        return await _vision_request(
            [(frames[0], "image/jpeg")],
            "Это кадр из GIF-анимации. Опиши что в ней происходит. Отвечай по-русски.",
            max_tokens=600,
        )
    # fallback — попробуем как изображение напрямую
    return await _vision_request(
        [(gif_bytes, "image/jpeg")],
        "Опиши это изображение. Отвечай по-русски.",
        max_tokens=600,
    )


async def analyze_video(video_bytes: bytes, mime: str = "video/mp4") -> Optional[str]:
    logger.info(f"[GROQ] analyze_video size={len(video_bytes)//1024}KB")
    frames = await asyncio.to_thread(_extract_frames, video_bytes, 3)

    if not frames:
        return "❌ Для анализа видео нужен ffmpeg на сервере. Установи командой:\n<code>apt install ffmpeg</code>"

    img_list = [(f, "image/jpeg") for f in frames]
    prompt = (
        f"Это {len(frames)} кадра из видео (хронологически). "
        "Подробно опиши: что происходит, кто или что в видео, контекст, настроение. "
        "Используй все кадры чтобы понять сюжет. Отвечай по-русски."
    )
    return await _vision_request(img_list, prompt, max_tokens=1000)


async def analyze_sticker(sticker_bytes: bytes, mime: str = "image/webp", is_video: bool = False) -> Optional[str]:
    logger.info(f"[GROQ] analyze_sticker mime={mime} is_video={is_video}")
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


async def analyze_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
    """Транскрибирует через Whisper, потом кратко резюмирует через vision-модель (текст)."""
    logger.info(f"[GROQ] analyze_audio filename={filename} size={len(audio_bytes)//1024}KB")

    transcript = await _whisper(audio_bytes, filename=filename)
    if not transcript:
        return None

    logger.info(f"[GROQ] транскрипция len={len(transcript)}")

    if len(transcript.split()) < 10:
        # Короткое — просто показываем текст
        return f"🎤 <b>Транскрипция:</b>\n<i>{transcript}</i>"

    # Длинное — добавляем краткое резюме
    summary_prompt = (
        f"Вот транскрипция аудиосообщения:\n\n{transcript}\n\n"
        "Напиши ТОЛЬКО краткое резюме (1-2 предложения) что обсуждается. Отвечай по-русски."
    )
    summary = await _vision_request([], summary_prompt, max_tokens=200)

    result = f"🎤 <b>Транскрипция:</b>\n<i>{transcript}</i>"
    if summary:
        result += f"\n\n💬 <b>Суть:</b> {summary}"
    return result
