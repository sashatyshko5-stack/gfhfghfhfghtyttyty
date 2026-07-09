"""Ротация Gemini-ключей через laozhang.ai API. При 429/квоте переключается на следующий."""
import logging
import random
from itertools import cycle
from threading import Lock
import aiohttp

log = logging.getLogger(__name__)

GEMINI_API_KEYS = [
    
    "",
]

_lock = Lock()
_cycle = cycle(GEMINI_API_KEYS)
_current = {"key": GEMINI_API_KEYS[0]}

# дефолтная модель — поддерживает audio+video вход
MODEL_NAME = "gemini-2.5-flash"
API_BASE_URL = "https://api.laozhang.ai/v1"


def _set(key: str):
    _current["key"] = key


def rotate() -> str:
    with _lock:
        new_key = next(_cycle)
        _set(new_key)
        log.info(f"[gemini] rotated to {new_key[:12]}...")
        return new_key


def get_current_key() -> str:
    """Возвращает текущий ключ."""
    return _current["key"]


async def safe_generate(messages: list, max_retries: int = 3):
    """Вызов через laozhang.ai API с ротацией при ошибках."""
    import asyncio
    last_exc = None
    
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {get_current_key()}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": MODEL_NAME,
                    "messages": messages
                }
                
                async with session.post(
                    f"{API_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60
                ) as resp:
                    if resp.status == 429 or resp.status == 429:
                        rotate()
                        await asyncio.sleep(0.5)
                        continue
                    
                    resp.raise_for_status()
                    data = await resp.json()
                    
                    # Парсим ответ в формате OpenAI
                    if "choices" in data and len(data["choices"]) > 0:
                        content = data["choices"][0].get("message", {}).get("content", "")
                        # Создаем объект с атрибутом text для совместимости
                        class Response:
                            def __init__(self, text):
                                self.text = text
                        return Response(content)
                    else:
                        raise RuntimeError("Invalid response format")
                        
        except aiohttp.ClientError as e:
            last_exc = e
            msg = str(e).lower()
            if any(x in msg for x in ("429", "quota", "exhaust", "rate", "resource")):
                rotate()
                await asyncio.sleep(0.5)
                continue
            raise
            
    raise RuntimeError(f"Gemini failed after {max_retries} retries: {last_exc}")