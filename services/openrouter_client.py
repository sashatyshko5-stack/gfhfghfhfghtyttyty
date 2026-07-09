import logging
import aiohttp
import asyncio

from ..storage.state import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

OPENROUTER_FREE_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3.1:free",
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-r1-distill-llama-70b:free",
    "google/gemini-2.0-flash-exp:free",
    "google/gemma-2-9b-it:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-7b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "mistralai/mistral-nemo:free",
    "qwen/qwen-2.5-7b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "qwen/qwq-32b:free",
    "nvidia/llama-3.1-nemotron-70b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "openchat/openchat-7b:free",
    "microsoft/phi-3-mini-128k-instruct:free",
    "microsoft/phi-3-medium-128k-instruct:free",
    "huggingfaceh4/zephyr-7b-beta:free",
]

DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


class OpenRouterClient:
    def __init__(self, tokens, model: str = None):
        self.tokens = [t for t in (tokens or []) if t]
        self._idx = 0
        self.model = model or DEFAULT_MODEL
        logger.info(f"[OpenRouter] init, ключей: {len(self.tokens)}, модель: {self.model}")

    def _rotate(self):
        if self.tokens:
            self._idx = (self._idx + 1) % len(self.tokens)

    async def chat_messages(self, messages: list, model: str = None,
                            max_tokens: int = 1000, temperature: float = 0.7) -> str:
        if not self.tokens:
            return None
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        tried = 0
        while tried < len(self.tokens):
            token = self.tokens[self._idx]
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me",
                "X-Title": "TG-Bot-Defender",
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        OPENROUTER_URL, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            try:
                                return data["choices"][0]["message"]["content"]
                            except (KeyError, IndexError, TypeError):
                                return None
                        text = await resp.text()
                        logger.warning(f"[OpenRouter] статус {resp.status}: {text[:200]}")
                        self._rotate()
                        tried += 1
            except Exception as e:
                logger.warning(f"[OpenRouter] сеть: {e}")
                self._rotate()
                tried += 1
        return None

    def get_status(self) -> str:
        return (
            f"🛣 **OpenRouter** ({self.model})\n"
            f"• Ключей: {len(self.tokens)}\n"
            f"• Бесплатных моделей: {len(OPENROUTER_FREE_MODELS)}"
        )


def get_openrouter_client_for_chat(chat_id, model: str = None):
    cid = str(chat_id)
    s = settings.get(cid, {}) or {}
    key = (s.get("openrouter_key") or "").strip()
    if not key:
        return None
    return OpenRouterClient([key], model=model or DEFAULT_MODEL)


async def validate_openrouter_key(api_key: str, timeout: int = 20) -> tuple[bool, str]:
    if not api_key or not api_key.startswith("sk-or-"):
        return False, "Ключ должен начинаться с `sk-or-`."
    payload = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me",
        "X-Title": "TG-Bot-Defender",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return True, "ok"
                text = await resp.text()
                if resp.status in (401, 403):
                    return False, "Ключ отклонён (401/403)."
                return False, f"HTTP {resp.status}: {text[:150]}"
    except asyncio.TimeoutError:
        return False, "Таймаут проверки."
    except Exception as e:
        return False, f"Ошибка сети: {e}"