import logging
import aiohttp
import asyncio

from ..storage.state import settings

logger = logging.getLogger(__name__)

GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

GOOGLE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-thinking-exp",
    "gemini-2.0-flash-exp",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
]

DEFAULT_MODEL = "gemini-2.5-flash"


def _to_google_messages(messages: list) -> tuple:
    system_text = []
    contents = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        if role == "system":
            system_text.append(str(content))
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": str(content)}]})
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": str(content)}]})
    system_instruction = {"parts": [{"text": "\n\n".join(system_text)}]} if system_text else None
    return system_instruction, contents


class GoogleClient:
    def __init__(self, tokens, model: str = None):
        self.tokens = [t for t in (tokens or []) if t]
        self._idx = 0
        self.model = model or DEFAULT_MODEL
        logger.info(f"[Google] init, ключей: {len(self.tokens)}, модель: {self.model}")

    def _rotate(self):
        if self.tokens:
            self._idx = (self._idx + 1) % len(self.tokens)

    async def chat_messages(self, messages: list, model: str = None,
                            max_tokens: int = 1000, temperature: float = 0.7) -> str:
        if not self.tokens:
            return None
        target_model = model or self.model
        system_instruction, contents = _to_google_messages(messages)
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        tried = 0
        while tried < len(self.tokens):
            token = self.tokens[self._idx]
            url = GOOGLE_URL.format(model=target_model) + f"?key={token}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            try:
                                parts = data["candidates"][0]["content"]["parts"]
                                return "".join(p.get("text", "") for p in parts)
                            except (KeyError, IndexError, TypeError):
                                return None
                        text = await resp.text()
                        logger.warning(f"[Google] статус {resp.status}: {text[:200]}")
                        self._rotate()
                        tried += 1
            except Exception as e:
                logger.warning(f"[Google] ошибка: {e}")
                self._rotate()
                tried += 1
        return None

    def get_status(self) -> str:
        return (
            f"🟦 **Google AI Studio** ({self.model})\n"
            f"• Ключей: {len(self.tokens)}\n"
            f"• Бесплатных моделей: {len(GOOGLE_MODELS)}"
        )


def get_google_client_for_chat(chat_id, model: str = None):
    cid = str(chat_id)
    s = settings.get(cid, {}) or {}
    key = (s.get("google_key") or "").strip()
    if not key:
        return None
    return GoogleClient([key], model=model or DEFAULT_MODEL)


async def validate_google_key(api_key: str, timeout: int = 20) -> tuple[bool, str]:
    if not api_key or len(api_key) < 20:
        return False, "Ключ слишком короткий."
    url = GOOGLE_URL.format(model="gemini-2.5-flash") + f"?key={api_key.strip()}"
    payload = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return True, "ok"
                text = await resp.text()
                if resp.status in (400, 401, 403):
                    return False, f"Ключ отклонён (HTTP {resp.status}). Получи на aistudio.google.com"
                return False, f"HTTP {resp.status}: {text[:150]}"
    except asyncio.TimeoutError:
        return False, "Таймаут проверки."
    except Exception as e:
        return False, f"Ошибка сети: {e}"