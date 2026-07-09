
import asyncio
import base64
import io
import logging
from dataclasses import dataclass

import g4f
from g4f.client import Client as G4FClientBase

try:
    import PIL.Image
except Exception:
    PIL = None

from ..core.logging_setup import log_ai_request, log_full

logger = logging.getLogger(__name__)


@dataclass
class _State:
    requests: int = 0
    errors: int = 0


class YqcloudClient:
    def __init__(self):
        self.provider = g4f.Provider.Yqcloud
        self.model = "gpt-4"
        self.state = _State()
        try:
            self.client = G4FClientBase(provider=self.provider)
            log_full(None, "info", "[YQCLOUD] клиент инициализирован")
        except Exception as e:
            log_full(None, "error", f"[YQCLOUD] init error: {e}")
            self.client = None

    @staticmethod
    def _image_meta(image_data: bytes) -> str:
        if not PIL:
            return f"size={len(image_data)}B"
        try:
            img = PIL.Image.open(io.BytesIO(image_data))
            img.load()
            w, h = img.size
            fmt = img.format or "?"
            try:
                small = img.convert("RGB").resize((1, 1))
                r, g, b = small.getpixel((0, 0))
                color = f"rgb({r},{g},{b})"
            except Exception:
                color = "?"
            return f"{fmt} {w}x{h}, ~{len(image_data)}B, dominant={color}"
        except Exception as e:
            return f"meta-error={e}"

    # Лимит ответа чтобы не ловить 429 от Yqcloud
    MAX_RESPONSE_CHARS = 1500
    MAX_TOKENS = 600
    REQUEST_TIMEOUT = 25

    @staticmethod
    def _truncate(text: str) -> str:
        if not text:
            return text
        text = text.strip()
        limit = YqcloudClient.MAX_RESPONSE_CHARS
        if len(text) <= limit:
            return text
        cut = text[:limit]
        for sep in ("\n\n", "\n", ". ", "! ", "? ", " "):
            idx = cut.rfind(sep)
            if idx > limit * 0.6:
                cut = cut[:idx + len(sep)]
                break
        return cut.rstrip() + "…"

    async def _chat(self, messages: list) -> str:
        if not self.client:
            raise RuntimeError("Yqcloud client is not initialized")

        def _call():
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=False,
                    max_tokens=self.MAX_TOKENS,
                )
            except TypeError:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=False,
                )

        resp = await asyncio.wait_for(asyncio.to_thread(_call), timeout=self.REQUEST_TIMEOUT)
        try:
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            text = str(resp)
        return self._truncate(text)

    async def analyze_image(self, image_data: bytes, prompt: str, model: str = None) -> str:
        self.state.requests += 1
        meta = self._image_meta(image_data)
        b64 = base64.b64encode(image_data).decode()
        try:
            messages = [
                {"role": "system", "content": "Ты — модератор чата. Коротко и строго по делу."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt + f"\n(метаданные: {meta})"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ]
            text = await self._chat(messages)
            if text:
                log_ai_request(None, "yqcloud/vision", prompt, text, True)
                return text
        except Exception as e:
            log_full(None, "warning", f"[YQCLOUD vision-try] {type(e).__name__}: {e}")

        try:
            fallback_prompt = (
                f"{prompt}\n\nУ меня нет прямого доступа к пикселям, но есть метаданные "
                f"изображения: {meta}.\nДай краткий вывод и решение "
                "(например: допустимо/запрещено/подозрительно)."
            )
            messages = [
                {"role": "system", "content": "Ты — модератор чата. Коротко и строго по делу."},
                {"role": "user", "content": fallback_prompt},
            ]
            text = await self._chat(messages)
            log_ai_request(None, "yqcloud/meta", prompt, text, True)
            return text or "нет ответа"
        except Exception as e:
            self.state.errors += 1
            log_ai_request(None, "yqcloud", prompt, str(e), False)
            return f"❌ Yqcloud недоступен: {e}"

    async def generate_text(self, prompt: str, model: str = None) -> str:
        self.state.requests += 1
        try:
            text = await self._chat([{"role": "user", "content": prompt}])
            log_ai_request(None, "yqcloud/text", prompt, text, True)
            return text
        except Exception as e:
            self.state.errors += 1
            log_ai_request(None, "yqcloud/text", prompt, str(e), False)
            return f"❌ Ошибка Yqcloud: {e}"

    def get_status(self) -> str:
        return (f"🔑 **Статус Yqcloud:**\n• Запросов: {self.state.requests}\n"
                f"• Ошибок: {self.state.errors}\n• Модель: {self.model}")

    def get_keys_count(self) -> int: return 1
    def add_api_key(self, key: str) -> bool: return False
    def remove_api_key(self, key: str) -> bool: return False


yqcloud_client = YqcloudClient()

__all__ = ["yqcloud_client", "YqcloudClient"]