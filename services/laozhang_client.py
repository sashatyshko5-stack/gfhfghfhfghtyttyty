import logging
import base64
import aiohttp

from ..storage.state import settings

logger = logging.getLogger(__name__)

LAOZHANG_API_URL = "https://api.laozhang.ai/v1/chat/completions"
MODEL = "gemini-2.5-flash"
TEXT_MODEL = "gpt-4o-mini"

_ROTATE_STATUSES = {401, 402, 403, 429, 500, 502, 503, 504}


class LaozhangClient:
    """Клиент Laozhang.ai. Ключи берутся ТОЛЬКО снаружи (из настроек чата, выставленных в ЛС).
    Никаких встроенных/захардкоженных ключей нет."""

    def __init__(self, tokens, model=None):
        self.tokens = [t for t in (tokens or []) if t]
        self._idx = 0
        self.model = model or MODEL
        logger.info(f"[Laozhang] init, ключей: {len(self.tokens)}, модель: {self.model}")

    def _rotate(self):
        if not self.tokens:
            return
        self._idx = (self._idx + 1) % len(self.tokens)

    async def _request(self, payload: dict, timeout: int = 60):
        if not self.tokens:
            logger.warning("[Laozhang] Нет API-ключей у клиента — запрос пропущен.")
            return None
        tried = 0
        while tried < len(self.tokens):
            token = self.tokens[self._idx]
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        LAOZHANG_API_URL, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            try:
                                return data["choices"][0]["message"]["content"]
                            except (KeyError, IndexError, TypeError) as e:
                                logger.warning(f"[Laozhang] bad format: {e} | data={str(data)[:400]}")
                                return None
                        text = await resp.text()
                        logger.warning(f"[Laozhang] ключ #{self._idx+1} статус {resp.status}: {text[:200]}")
                        self._rotate()
                        tried += 1
                        continue
            except Exception as e:
                logger.warning(f"[Laozhang] сеть #{self._idx+1}: {e}")
                self._rotate()
                tried += 1
                continue
        return None

    async def analyze_image(self, image_bytes: bytes, prompt: str = None) -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/png;base64,{b64}"
        if not prompt:
            prompt = "Опиши подробно что изображено на картинке. Отвечай на русском языке."
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": 500, "temperature": 0.7,
        }
        return await self._request(payload, timeout=60)

    async def check_nsfw(self, image_bytes: bytes) -> dict:
        prompt = (
            "Ты модератор изображений. Кратко опиши изображение (1-2 предложения) и определи, "
            "нарушает ли оно правила публичного чата. Нарушает, если: обнажённые люди, интимные "
            "контакты, гениталии или откровенный контент (в т.ч. в мультфильмах и аниме). "
            "В конце на новой строке напиши ровно одну строку: "
            "CLASS: FLAGGED или CLASS: CLEAN."
        )
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/png;base64,{b64}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": 1500, "temperature": 0.0,
        }
        content = await self._request(payload, timeout=60)
        if content is None:
            return {"is_nsfw": False, "nsfw_score": 0, "error": "Нет ключа или ошибка API"}

        lines = content.strip().splitlines()
        class_line = ""
        analysis_lines = lines[:]
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].upper().startswith("CLASS:"):
                class_line = lines[i]
                analysis_lines = lines[:i]
                break
        is_nsfw = "FLAGGED" in class_line.upper()
        return {
            "is_nsfw": is_nsfw,
            "nsfw_score": 1.0 if is_nsfw else 0.0,
            "response": class_line.strip(),
            "analysis": "\n".join(analysis_lines).strip(),
        }

    async def analyze_for_raid(self, image_data_url: str, raid_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": raid_prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]}],
            "max_tokens": 10, "temperature": 0.0,
        }
        return await self._request(payload, timeout=30)

    async def chat(self, prompt: str, max_tokens: int = 1000, temperature: float = 0.7) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temperature,
        }
        return await self._request(payload, timeout=60)

    async def chat_messages(self, messages: list, model: str = None,
                            max_tokens: int = 1000, temperature: float = 0.7) -> str:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        return await self._request(payload, timeout=60)

    def get_status(self) -> str:
        active_key = self._idx + 1 if self.tokens else 0
        return (
            f"🔑 **Laozhang.ai** ({self.model})\n"
            f"• Ключей: {len(self.tokens)}\n"
            f"• Активный ключ: #{active_key}\n"
            f"• API: {LAOZHANG_API_URL}"
        )


# ─── ПЕР-ЧАТНЫЕ КЛИЕНТЫ (ТОЛЬКО из ключей админа в ЛС) ────────────────────────
def _chat_key(chat_id, kind: str) -> str:
    cid = str(chat_id)
    s = settings.get(cid, {}) or {}
    if kind == "text":
        return (s.get("laozhang_text_key") or "").strip()
    if kind == "vision":
        return (s.get("laozhang_vision_key") or "").strip()
    return ""


def get_client_for_chat(chat_id, kind: str = "text"):
    """Возвращает LaozhangClient с ключом из настроек чата или None."""
    key = _chat_key(chat_id, kind)
    if not key:
        return None
    model = TEXT_MODEL if kind == "text" else MODEL
    return LaozhangClient([key], model=model)


def has_chat_keys(chat_id) -> bool:
    return bool(_chat_key(chat_id, "text") or _chat_key(chat_id, "vision"))


# Заглушки для обратной совместимости. Без ключей возвращают None.
nsfw_client = LaozhangClient([], model=TEXT_MODEL)
raid_client = LaozhangClient([], model=MODEL)
text_client = LaozhangClient([], model=TEXT_MODEL)
vision_client = LaozhangClient([], model=TEXT_MODEL)