from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def ai_text(
    chat_id: int,
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 200,
    temperature: float = 0.0,
) -> Optional[str]:
    from ..services.chat_ai_router import generate_for_chat
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    result = await generate_for_chat(chat_id, messages, max_tokens=max_tokens, temperature=temperature)
    if not result or result.startswith("❌"):
        return None
    return result


async def ai_text_json(
    chat_id: int,
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 300,
) -> dict:
    raw = await ai_text(chat_id, prompt, system=system, max_tokens=max_tokens)
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except Exception as e:
        logger.warning(f"[AI-CLIENT] JSON parse error: {e} | raw={raw[:200]}")
        return {}


async def ai_vision_bytes(
    chat_id: int,
    image_bytes: bytes,
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 20,
    temperature: float = 0.0,
) -> Optional[str]:
    """
    Отправляет изображение + текст в ИИ.
    system — системный промпт (инструкция).
    prompt — пользовательский текст (данные для анализа).
    Если нет vision-клиента — fallback на текстовый ИИ (без картинки).
    """
    import base64
    from ..services.laozhang_client import get_client_for_chat

    client = get_client_for_chat(chat_id, kind="vision") or get_client_for_chat(chat_id, kind="text")
    if client:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            ],
        })

        payload = {
            "model": client.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        result = await client._request(payload, timeout=60)
        if result:
            logger.debug(f"[AI-VISION] vision-ответ: {result!r}")
            return result

    # Fallback: Laozhang недоступен — пробуем vision через глобальный провайдер чата (Groq, OpenRouter)
    from ..services.chat_ai_router import generate_vision_for_chat, generate_for_chat
    messages_for_vision = []
    if system:
        messages_for_vision.append({"role": "system", "content": system})
    messages_for_vision.append({"role": "user", "content": prompt})

    result = await generate_vision_for_chat(chat_id, image_bytes, messages_for_vision, max_tokens=max_tokens)
    if result:
        logger.debug(f"[AI-VISION] vision через глобальный провайдер: {result!r}")
        return result

    # Финальный fallback: совсем нет vision — текстовый анализ без картинки
    logger.warning(f"[AI-VISION] vision недоступен для chat={chat_id}, fallback на текст")
    fallback_prompt = f"[Аватарка недоступна для визуального анализа — оценивай только по тексту]\n\n{prompt}"
    text_messages = []
    if system:
        text_messages.append({"role": "system", "content": system})
    text_messages.append({"role": "user", "content": fallback_prompt})
    result = await generate_for_chat(chat_id, text_messages, max_tokens=max_tokens, temperature=temperature)
    if not result or result.startswith("❌"):
        return None
    return result
