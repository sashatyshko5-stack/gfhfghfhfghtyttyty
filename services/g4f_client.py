

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from ..core.logging_setup import log_ai_request
from .laozhang_client import get_client_for_chat

logger = logging.getLogger(__name__)


# === Лимиты ответа ===
MAX_RESPONSE_CHARS = 1500
MAX_TOKENS = 700
REQUEST_TIMEOUT = 30


def _truncate(text: str, limit: int = MAX_RESPONSE_CHARS) -> str:
    if not text:
        return text
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n\n", "\n", ". ", "! ", "? ", " "):
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            cut = cut[:idx + len(sep)]
            break
    return cut.rstrip() + "…"


class ProviderStatus(Enum):
    ACTIVE = "active"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass
class ProviderState:
    status: ProviderStatus = ProviderStatus.ACTIVE
    error_count: int = 0
    last_error_time: float = 0.0
    cooldown_until: float = 0.0
    success_count: int = 0


# Только laozhang — ИИ работает строго от ключей из ЛС.
SUPPORTED_PROVIDERS: Dict[str, Tuple[Optional[object], str, str]] = {
    "laozhang": (None, "gpt-4o-mini", "Laozhang.ai"),
}

PROVIDER_ALIASES = {
    "lz": "laozhang",
    "laozhang": "laozhang",
    "main": "laozhang",
    "primary": "laozhang",
    "основной": "laozhang",
    "резерв": "laozhang",
    "backup": "laozhang",
    "fallback": "laozhang",
}

DEFAULT_PROVIDER_KEY = "laozhang"

_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "ai_provider_state.json",
)


def _load_state_from_disk() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[g4f] не удалось прочитать состояние провайдера: {e}")
    return {}


def _save_state_to_disk(data: dict) -> None:
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[g4f] не удалось сохранить состояние провайдера: {e}")


class G4FClient:
    """ИИ-клиент. Использует Laozhang.ai с ключами ТОЛЬКО из настроек чата (ЛС)."""

    def __init__(self):
        self.provider_states: Dict[str, ProviderState] = {
            key: ProviderState() for key in SUPPORTED_PROVIDERS
        }
        self.cooldown_time = 180

        st = _load_state_from_disk()
        self.active_provider: str = st.get("active_provider") or DEFAULT_PROVIDER_KEY
        if self.active_provider not in SUPPORTED_PROVIDERS:
            self.active_provider = DEFAULT_PROVIDER_KEY

        self.active_model: str = st.get("active_model") or SUPPORTED_PROVIDERS[self.active_provider][1]

        logger.info(
            f"G4F клиент инициализирован | активный: {self.active_provider} | модель: {self.active_model}"
        )

    # ---------- управление ----------

    def list_providers(self) -> List[Tuple[str, str, str]]:
        return [(k, v[2], v[1]) for k, v in SUPPORTED_PROVIDERS.items()]

    def get_active(self) -> Tuple[str, str, str]:
        prov, model, label = SUPPORTED_PROVIDERS[self.active_provider]
        return self.active_provider, label, self.active_model

    def set_active_provider(self, name: str, model: Optional[str] = None) -> Tuple[bool, str]:
        key = PROVIDER_ALIASES.get((name or "").strip().lower())
        if not key:
            return False, f"Неизвестный провайдер «{name}». Доступно: {', '.join(SUPPORTED_PROVIDERS)}."
        self.active_provider = key
        self.provider_states[key] = ProviderState()
        if model:
            self.active_model = model
        else:
            self.active_model = SUPPORTED_PROVIDERS[key][1]
        _save_state_to_disk({
            "active_provider": self.active_provider,
            "active_model": self.active_model,
        })
        label = SUPPORTED_PROVIDERS[key][2]
        return True, f"✅ Провайдер: {label}, модель: {self.active_model}"

    def set_active_model(self, model: str) -> Tuple[bool, str]:
        if not model:
            return False, "Укажи модель"
        self.active_model = model.strip()
        _save_state_to_disk({
            "active_provider": self.active_provider,
            "active_model": self.active_model,
        })
        return True, f"✅ Модель: {self.active_model}"

    # ---------- внутреннее ----------

    def _ordered_providers(self) -> List[str]:
        order = [self.active_provider]
        for k in SUPPORTED_PROVIDERS:
            if k != self.active_provider:
                order.append(k)
        return order

    def _resolve_model(self, key: str, override: Optional[str] = None) -> str:
        if override:
            return override
        if key == self.active_provider:
            return self.active_model
        return SUPPORTED_PROVIDERS[key][1]

    def _mark_error(self, key: str):
        st = self.provider_states[key]
        st.error_count += 1
        st.last_error_time = time.time()
        if st.error_count >= 2:
            st.status = ProviderStatus.RATE_LIMITED
            st.cooldown_until = time.time() + self.cooldown_time

    def _mark_success(self, key: str):
        st = self.provider_states[key]
        st.status = ProviderStatus.ACTIVE
        st.error_count = 0
        st.cooldown_until = 0
        st.success_count += 1

    def _is_available(self, key: str) -> bool:
        st = self.provider_states[key]
        if st.cooldown_until > time.time():
            return False
        return True

    async def _try_with_laozhang(self, key: str, messages: List[Dict], model: str,
                                 chat_id: Optional[int] = None) -> Optional[str]:
        label = SUPPORTED_PROVIDERS[key][2]
        if chat_id is None:
            logger.warning(f"[{label}] нет chat_id — пропуск (ключи только из настроек чата).")
            self._mark_error(key)
            return None
        client = get_client_for_chat(chat_id, "text")
        if client is None:
            logger.warning(f"[{label}] нет ключа Laozhang в настройках чата {chat_id} — пропуск.")
            self._mark_error(key)
            return None
        try:
            logger.info(f"try [{label}/{model}] msgs={len(messages)} chat={chat_id}")
            content = await client.chat_messages(
                messages=messages, model=model,
                max_tokens=MAX_TOKENS, temperature=0.7,
            )
            if content and content.strip():
                self._mark_success(key)
                content = _truncate(content)
                try:
                    log_ai_request(None, f"g4f/{label}/{model}", messages, content, True)
                except Exception:
                    pass
                logger.info(f"ok [{label}] {len(content)} симв")
                return content
            raise Exception("пустой ответ")
        except asyncio.TimeoutError:
            logger.warning(f"timeout {label}")
            self._mark_error(key)
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"{label}: {err}")
            try:
                log_ai_request(None, f"g4f/{label}/{model}", messages, err, False)
            except Exception:
                pass
            self._mark_error(key)
        return None

    async def _try_with_provider(self, key: str, messages: List[Dict],
                                 model: Optional[str],
                                 chat_id: Optional[int] = None) -> Optional[str]:
        if not self._is_available(key):
            return None
        target_model = self._resolve_model(key, model)
        return await self._try_with_laozhang(key, messages, target_model, chat_id=chat_id)

    # ---------- публичные методы ----------

    async def generate_text(self, prompt: str, model: Optional[str] = None,
                            chat_id: Optional[int] = None) -> str:
        return await self.generate_text_with_history(
            [{"role": "user", "content": prompt}], model=model, chat_id=chat_id
        )

    async def generate_text_with_history(self, messages: List[Dict],
                                         model: Optional[str] = None,
                                         chat_id: Optional[int] = None) -> str:
        last_error = None
        for key in self._ordered_providers():
            res = await self._try_with_provider(key, messages, model, chat_id=chat_id)
            if res is not None:
                return res
            last_error = SUPPORTED_PROVIDERS[key][2]
        await asyncio.sleep(0.4)
        for key in self._ordered_providers():
            self.provider_states[key].cooldown_until = 0
            res = await self._try_with_provider(key, messages, model, chat_id=chat_id)
            if res is not None:
                return res
        logger.error("все провайдеры недоступны")
        return "❌ ИИ недоступен. Админ должен задать API-ключ Laozhang.ai в ЛС бота через /start setup_<chat_id>."

    async def generate_smart(self, prompt: str, context: str = None,
                             chat_id: Optional[int] = None) -> str:
        full_prompt = prompt
        if context:
            full_prompt = f"Контекст: {context}\n\nВопрос: {prompt}"
        return await self.generate_text(full_prompt, chat_id=chat_id)

    def get_provider_status(self) -> str:
        lines = ["📊 **Статус AI провайдеров:**\n"]
        now = time.time()
        for key, (_p, default_model, label) in SUPPORTED_PROVIDERS.items():
            st = self.provider_states[key]
            mark = "👉" if key == self.active_provider else "  "
            if st.cooldown_until > now:
                status = f"⏳ кулдаун ({int(st.cooldown_until - now)}с)"
            elif st.status == ProviderStatus.ACTIVE:
                status = "✅ активен"
            else:
                status = f"⚠️ ошибок: {st.error_count}"
            model = self.active_model if key == self.active_provider else default_model
            lines.append(f"{mark} **{label}** [{key}] · модель `{model}` · {status} · успехов: {st.success_count}")
        lines.append(f"\n🎯 Активный: **{SUPPORTED_PROVIDERS[self.active_provider][2]}** · модель `{self.active_model}`")
        return "\n".join(lines)


g4f_client = G4FClient()