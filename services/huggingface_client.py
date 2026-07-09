import logging
import base64
import aiohttp

logger = logging.getLogger(__name__)

# ── Ротация ключей Hugging Face ────────────────────────────────────────────
HF_TOKENS = [
    "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"
    "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",
    "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",
    "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",
]

HF_API_URL = "https://router.huggingface.co/v1/chat/completions"
VISION_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"

# Коды ответов, при которых нужно переключиться на следующий ключ
_ROTATE_STATUSES = {401, 402, 403, 429, 500, 502, 503, 504}


class HuggingFaceClient:
    """Клиент Hugging Face с ротацией ключей при ошибках."""

    def __init__(self):
        self.tokens = list(HF_TOKENS)
        self._idx = 0
        logger.info(f"[HF] Клиент инициализирован, ключей: {len(self.tokens)}")

    def _rotate(self):
        self._idx = (self._idx + 1) % len(self.tokens)
        logger.warning(f"[HF] Переключаемся на ключ #{self._idx + 1}/{len(self.tokens)}")

    async def _request(self, payload: dict, timeout: int = 60):
        """Делает POST, при ошибке/лимите перебирает все ключи по кругу.
        Возвращает (content_str | None)."""
        tried = 0
        while tried < len(self.tokens):
            token = self.tokens[self._idx]
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        HF_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["choices"][0]["message"]["content"]
                        text = await resp.text()
                        logger.warning(
                            f"[HF] Ключ #{self._idx + 1} статус {resp.status}: {text[:200]}"
                        )
                        if resp.status in _ROTATE_STATUSES:
                            self._rotate()
                            tried += 1
                            continue
                        self._rotate()
                        tried += 1
                        continue
            except Exception as e:
                logger.warning(f"[HF] Ошибка сети с ключом #{self._idx + 1}: {e}")
                self._rotate()
                tried += 1
                continue
        return None

    async def analyze_image(self, image_bytes: bytes, prompt: str = None) -> str:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/png;base64,{base64_image}"
        if not prompt:
            prompt = "Опиши подробно что изображено на картинке. Отвечай на русском языке."
        payload = {
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": 500,
            "temperature": 0.7,
        }
        content = await self._request(payload, timeout=60)
        if content:
            logger.info(f"[HF] Успех analyze_image: {content[:50]}...")
        return content

    async def check_nsfw(self, image_bytes: bytes) -> dict:
        prompt = (
            "Это изображение содержит NSFW контент (порнография, обнажёнка, откровенный сексуальный контент, "
            "гениталии, сексуальные действия)? Включая мультяшный и аниме контент. "
            "Ответь ТОЛЬКО одним словом: YES или NO."
        )
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/png;base64,{base64_image}"
        payload = {
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": 10,
            "temperature": 0.0,
        }
        content = await self._request(payload, timeout=30)
        if content is None:
            return {"is_nsfw": False, "nsfw_score": 0, "error": "Не удалось проверить"}
        upper = content.upper().strip()
        is_nsfw = "YES" in upper or "ДА" in upper
        logger.info(f"[HF] NSFW ответ: {upper}, is_nsfw={is_nsfw}")
        return {"is_nsfw": is_nsfw, "nsfw_score": 1.0 if is_nsfw else 0.0, "response": upper}

    async def analyze_for_raid(self, image_data_url: str, raid_prompt: str) -> str:
        """Универсальный вызов для анти-рейда (картинка уже в data_url)."""
        payload = {
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": raid_prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }],
            "max_tokens": 10,
            "temperature": 0.0,
        }
        return await self._request(payload, timeout=30)


hf_client = HuggingFaceClient()