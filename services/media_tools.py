"""Описание аудио и анализ видео через laozhang.ai API (ключ из gemini_pool)."""
import asyncio
import logging
import os
import time
import base64
import aiohttp

log = logging.getLogger(__name__)

_GEMINI_MEDIA_MODEL = os.environ.get("GEMINI_MEDIA_MODEL", "gemini-2.5-flash")
API_BASE_URL = "https://api.laozhang.ai/v1"


def _get_api_key() -> str:
    """Берёт ключ из gemini_pool для ротации."""
    from .gemini_pool import get_current_key
    return get_current_key()


def _file_to_base64(file_path: str) -> str:
    """Конвертирует файл в base64."""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ======================================================================
#  Описание аудио/видео (Gemini по GEMINI_API_KEY)
# ======================================================================
_DESCRIBE_PROMPT = """Ты — музыкальный критик с тонким слухом.
Проанализируй приложенный аудио-файл и ответь строго в таком формате (на русском):

🎧 Звучание: опиши стиль, настроение, темп, инструменты, вокал (1-3 предложения)
🎼 Жанр и эпоха: предположи жанр/поджанр и примерный период
✨ Впечатление: твоё личное субъективное мнение — цепляет или нет, чем (2-3 предложения)
📝 Текст (если поёт на понятном языке): разборчивые строки, максимум 8 строк. Если инструментал — напиши «инструментал»
🎯 Ассоциации: с чем у тебя это ассоциируется (фильм/место/эмоция/похожий артист)

Будь искренним и не пресным. Без воды.
"""


async def _describe_audio_async(audio_path: str) -> str:
    """Описание аудио через laozhang.ai API."""
    try:
        # Конвертируем аудио в base64
        audio_base64 = _file_to_base64(audio_path)
        
        # Определяем MIME тип по расширению
        import mimetypes
        mime_type, _ = mimetypes.guess_type(audio_path)
        if not mime_type:
            mime_type = "audio/mpeg"
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {_get_api_key()}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": _GEMINI_MEDIA_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": _DESCRIBE_PROMPT
                            },
                            {
                                "type": "image_url",  # для мультимодальных моделей используется image_url
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{audio_base64}"
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
                timeout=120
            ) as resp:
                if resp.status == 429:
                    from .gemini_pool import rotate
                    rotate()
                    return "❌ Превышен лимит запросов, попробуйте позже."
                
                resp.raise_for_status()
                data = await resp.json()
                
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    return content.strip() or "🤷 Пусто."
                else:
                    return "❌ Неверный формат ответа API."
                    
    except Exception as e:
        log.exception(f"[describe-audio] {e}")
        return f"❌ Ошибка анализа: {e}"


async def describe_audio_via_gemini(audio_path: str) -> str:
    """Описание аудио через laozhang.ai API."""
    try:
        return await _describe_audio_async(audio_path)
    except Exception as e:
        log.exception(f"[describe-audio] {e}")
        return f"❌ Ошибка анализа: {e}"


async def _analyze_video_async(video_path: str, user_prompt: str | None) -> str:
    """Анализ видео через laozhang.ai API."""
    try:
        # Конвертируем видео в base64
        video_base64 = _file_to_base64(video_path)
        
        # Определяем MIME тип по расширению
        import mimetypes
        mime_type, _ = mimetypes.guess_type(video_path)
        if not mime_type:
            mime_type = "video/mp4"
        
        prompt = user_prompt or (
            "Опиши это видео: что происходит, кто в кадре, какая атмосфера, "
            "есть ли музыка (если да — опиши звучание). На русском, без воды."
        )
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {_get_api_key()}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": _GEMINI_MEDIA_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",  # для мультимодальных моделей используется image_url
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{video_base64}"
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
                timeout=180
            ) as resp:
                if resp.status == 429:
                    from .gemini_pool import rotate
                    rotate()
                    return "❌ Превышен лимит запросов, попробуйте позже."
                
                resp.raise_for_status()
                data = await resp.json()
                
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    return content.strip() or "🤷 Пусто."
                else:
                    return "❌ Неверный формат ответа API."
                    
    except Exception as e:
        log.exception(f"[analyze-video] {e}")
        return f"❌ Ошибка анализа видео: {e}"


async def analyze_video_via_gemini(video_path: str, user_prompt: str | None = None) -> str:
    """Анализ видео через laozhang.ai API."""
    try:
        return await _analyze_video_async(video_path, user_prompt)
    except Exception as e:
        log.exception(f"[analyze-video] {e}")
        return f"❌ Ошибка анализа видео: {e}"
