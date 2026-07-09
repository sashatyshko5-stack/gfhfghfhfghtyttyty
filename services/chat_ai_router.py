

import logging
import aiohttp
import asyncio
from typing import Dict, List, Optional, Tuple

from ..storage.state import settings
from ..storage import providers_storage as _ps

try:
    _ps.load()
except Exception:  # noqa: BLE001
    pass
logger = logging.getLogger(__name__)

# ── Поддерживаемые провайдеры и алиасы ─────────────────────────────────────
PROVIDER_ALIASES: Dict[str, str] = {
    "laozhang": "laozhang", "lz": "laozhang", "лаочжанг": "laozhang", "лаожанг": "laozhang",
    "openrouter": "openrouter", "or": "openrouter",
    "опенроутер": "openrouter", "опен_роутер": "openrouter", "опен-роутер": "openrouter",
    "google": "google", "googleai": "google", "google_ai": "google",
    "google_ai_studio": "google", "gemini": "google",
    "гугл": "google", "гугл_аи": "google", "гугл-аи": "google",
    "huggingface": "huggingface", "hf": "huggingface",
    "хагинг": "huggingface", "хаггинг": "huggingface", "хаггиг": "huggingface", "хаггингфейс": "huggingface",
    "groq": "groq", "грок": "groq",
    "custom": "custom", "кастом": "custom", "своё": "custom", "свой": "custom",
}

PROVIDER_LABELS: Dict[str, str] = {
    "laozhang": "Laozhang.ai",
    "openrouter": "OpenRouter",
    "google": "Google AI Studio",
    "huggingface": "HuggingFace",
    "groq": "Groq",
    "custom": "Custom (HTTP)",
}

# Каталог моделей по провайдерам (можно расширять)
PROVIDER_MODELS: Dict[str, List[str]] = {
    "laozhang": [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4.1",
        "claude-3-5-sonnet",
        "claude-3-5-haiku",
        "deepseek-chat",
        "deepseek-r1",
    ],
    "openrouter": [
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
    ],
    "google": [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-thinking-exp",
        "gemini-2.0-flash-exp",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
        "gemini-1.5-pro",
    ],
    "huggingface": [
        "Qwen/Qwen2.5-72B-Instruct",
        "Qwen/Qwen2.5-VL-72B-Instruct",
        "meta-llama/Llama-3.3-70B-Instruct",
        "meta-llama/Llama-3.1-70B-Instruct",
        "mistralai/Mistral-7B-Instruct-v0.3",
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "deepseek-ai/DeepSeek-R1",
        "deepseek-ai/DeepSeek-V3",
    ],
    "groq": [
        "meta-llama/llama-4-scout-17b-16e-instruct",   # vision + text, бесплатно
        "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",  # vision + text
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it",
        "mixtral-8x7b-32768",
    ],
    "custom": [],
}

DEFAULT_MODELS: Dict[str, str] = {
    "laozhang": "gpt-4o-mini",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "google": "gemini-2.5-flash",
    "huggingface": "Qwen/Qwen2.5-72B-Instruct",
    "groq": "meta-llama/llama-4-scout-17b-16e-instruct",
    "custom": "",
}

# Модели Groq с поддержкой vision (image_url)
GROQ_VISION_MODELS = {
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
}


def normalize_provider(name: str) -> Optional[str]:
    if not name:
        return None
    return PROVIDER_ALIASES.get(name.strip().lower().replace(" ", "_"))


def list_providers() -> List[str]:
    return ["laozhang", "openrouter", "google", "huggingface", "groq", "custom"]


def list_models(provider: str) -> List[str]:
    p = normalize_provider(provider) or provider
    return list(PROVIDER_MODELS.get(p, []))


def _chat_cfg(chat_id) -> dict:
    cid = str(chat_id)
    settings.setdefault(cid, {})
    return settings[cid]


# ── API: переключение/ключи ────────────────────────────────────────────────
def get_chat_provider(chat_id) -> str:
    cfg = _chat_cfg(chat_id)
    prov = cfg.get("ai_provider") or "laozhang"
    return prov if prov in DEFAULT_MODELS else "laozhang"


def get_chat_model(chat_id) -> str:
    cfg = _chat_cfg(chat_id)
    prov = get_chat_provider(chat_id)
    model = cfg.get("ai_model")
    if model:
        return model
    if prov == "custom":
        return (cfg.get("custom_provider") or {}).get("model") or ""
    return DEFAULT_MODELS.get(prov, "")


def set_chat_provider_and_model(chat_id, provider: str, model: Optional[str]) -> Tuple[bool, str]:
    """Логика переключения:
    1) Нормализуем имя провайдера (алиасы).
    2) Если модель не указана — берём дефолтную или текущую (если уже была для этого провайдера).
    3) Сохраняем в settings.json чата.
    """
    from ..storage.state import save_settings
    p = normalize_provider(provider)
    if not p:
        return False, (
            f"❌ Неизвестный провайдер «{provider}».\n"
            f"Доступно: {', '.join(list_providers())}."
        )
    cfg = _chat_cfg(chat_id)
    cfg["ai_provider"] = p

    if p == "custom":
        cp = cfg.get("custom_provider") or {}
        cfg["ai_model"] = (model or cp.get("model") or "").strip()
    else:
        chosen = (model or "").strip()
        if not chosen:
            chosen = DEFAULT_MODELS[p]
        cfg["ai_model"] = chosen

    save_settings(str(chat_id))
    try:
        _ps.save_active(chat_id, p, cfg.get("ai_model") or "")
    except Exception:
        pass
    return True, (
        f"✅ Переключено\n"
        f"• Провайдер: **{PROVIDER_LABELS[p]}**\n"
        f"• Модель: `{cfg['ai_model'] or '—'}`"
    )


def set_chat_api_key(chat_id, provider: str, api_key: str) -> Tuple[bool, str]:
    from ..storage.state import save_settings
    p = normalize_provider(provider)
    if not p or p == "custom":
        return False, "❌ Укажи провайдера: laozhang, openrouter, google, huggingface."
    if not api_key:
        return False, "❌ Пустой ключ."
    cfg = _chat_cfg(chat_id)
    keys = cfg.get("ai_keys") or {}
    keys[p] = api_key.strip()
    cfg["ai_keys"] = keys
    save_settings(str(chat_id))
    try:
        _ps.save_api_key(chat_id, p, api_key.strip())
    except Exception:
        pass
    masked = api_key[:6] + "…" + api_key[-4:] if len(api_key) > 12 else "***"
    return True, f"✅ Ключ для {PROVIDER_LABELS[p]} сохранён ({masked})"


def set_chat_custom_provider(chat_id, endpoint: str, api_key: str, model: str) -> Tuple[bool, str]:
    from ..storage.state import save_settings
    if not endpoint or not endpoint.startswith(("http://", "https://")):
        return False, "❌ Endpoint должен начинаться с http:// или https://"
    if not model:
        return False, "❌ Укажи кодовое название модели."
    cfg = _chat_cfg(chat_id)
    cfg["custom_provider"] = {
        "endpoint": endpoint.strip(),
        "api_key": (api_key or "").strip(),
        "model": model.strip(),
    }
    if cfg.get("ai_provider") == "custom":
        cfg["ai_model"] = model.strip()
    save_settings(str(chat_id))
    try:
        _ps.save_custom(chat_id, endpoint.strip(), (api_key or "").strip(), model.strip())
    except Exception:
        pass
    return True, (
        f"✅ Кастомный провайдер сохранён:\n"
        f"• endpoint: `{endpoint}`\n"
        f"• модель: `{model}`\n"
        f"Активировать: `!модель кастом`"
    )


def is_ai_enabled(chat_id) -> bool:
    return bool(_chat_cfg(chat_id).get("ai_enabled", True))


# ── Форматирование списков для команд ──────────────────────────────────────
def format_providers_list(chat_id) -> str:
    active = get_chat_provider(chat_id)
    active_model = get_chat_model(chat_id)
    cfg = _chat_cfg(chat_id)
    keys = cfg.get("ai_keys") or {}
    cp = cfg.get("custom_provider") or {}

    lines = ["🤖 **Провайдеры AI:**\n"]
    for p in list_providers():
        mark = "👉" if p == active else "  "
        if p == "custom":
            has_key = "✅" if (cp.get("api_key") or cp.get("endpoint")) else "❌"
            default = cp.get("model") or "—"
        else:
            has_key = "✅" if keys.get(p) else "❌"
            default = DEFAULT_MODELS.get(p, "—")
        lines.append(
            f"{mark} `{p}` — **{PROVIDER_LABELS[p]}** · ключ: {has_key} · дефолт: `{default}`"
        )
    lines.append(
        f"\n🎯 Активный: **{PROVIDER_LABELS[active]}** · модель `{active_model or '—'}`"
    )
    lines.append("\nКоманды:")
    lines.append("• `!модель <модель> <провайдер>` — переключить")
    lines.append("• `!модель кастом` — кастомный провайдер")
    lines.append("• `!модели <провайдер>` — список моделей провайдера")
    lines.append("• `!ключ <провайдер> <api_key>` — задать ключ")
    lines.append("• `!кастом_провайдер`")
    
    return "\n".join(lines)


def format_models_list(provider: str) -> str:
    p = normalize_provider(provider)
    if not p:
        return f"❌ Неизвестный провайдер «{provider}». Доступно: {', '.join(list_providers())}"
    if p == "custom":
        return "🛠 У кастомного провайдера модель задаётся через `!кастом_провайдер <endpoint> <key> <model>`."
    models = PROVIDER_MODELS.get(p, [])
    if not models:
        return f"📭 Для {PROVIDER_LABELS[p]} нет встроенного списка моделей."
    lines = [f"📚 **Модели {PROVIDER_LABELS[p]}** (всего {len(models)}):\n"]
    for m in models:
        lines.append(f"• `{m}`")
    lines.append(f"\nПереключить: `!модель <модель> {p}`")
    lines.append(f"Дефолт: `{DEFAULT_MODELS[p]}`")
    return "\n".join(lines)


def get_chat_ai_status(chat_id) -> str:
    cfg = _chat_cfg(chat_id)
    prov = get_chat_provider(chat_id)
    model = get_chat_model(chat_id)
    keys = cfg.get("ai_keys") or {}
    cp = cfg.get("custom_provider") or {}
    enabled = "✅ вкл" if cfg.get("ai_enabled", True) else "❌ выкл"
    if prov == "custom":
        has_key = "✅" if (cp.get("api_key") or cp.get("endpoint")) else "❌"
    else:
        has_key = "✅" if keys.get(prov) else "❌"
    lines = [
        f"🤖 **AI для этого чата:** {enabled}",
        f"• Провайдер: **{PROVIDER_LABELS.get(prov, prov)}**",
        f"• Модель: `{model or '—'}`",
        f"• Ключ: {has_key}",
    ]
    if prov == "custom":
        lines.append(f"• Endpoint: `{cp.get('endpoint') or '—'}`")
    lines.append("\n`!провайдеры` — список провайдеров")
    lines.append(f"`!модели {prov}` — список моделей провайдера")
    return "\n".join(lines)


# ── HTTP вызовы провайдеров ────────────────────────────────────────────────
async def _post_openai_compat(url: str, api_key: str, model: str, messages: List[Dict],
                              max_tokens: int = 800, temperature: float = 0.7,
                              extra_headers: Optional[Dict[str, str]] = None,
                              timeout: int = 60) -> Optional[str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    
    # 🔥 Убрали temperature из payload
    payload = {
        "model": model, 
        "messages": messages, 
        "max_tokens": max_tokens
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"[chat_ai_router] {url} HTTP {resp.status}: {text[:200]}")
                    return None
                data = await resp.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    try:
                        return data["choices"][0].get("text")
                    except Exception:
                        return None
    except asyncio.TimeoutError:
        logger.warning(f"[chat_ai_router] timeout {url}")
        return None
    except Exception as e:
        logger.warning(f"[chat_ai_router] error {url}: {e}")
        return None


async def _call_google(api_key: str, model: str, messages: List[Dict],
                       max_tokens: int = 800, temperature: float = 0.7) -> Optional[str]:
    from .google_client import _to_google_messages, GOOGLE_URL
    system_instruction, contents = _to_google_messages(messages)
    payload = {"contents": contents,
               "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system_instruction:
        payload["systemInstruction"] = system_instruction
    url = GOOGLE_URL.format(model=model) + f"?key={api_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    headers={"Content-Type": "application/json"},
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"[chat_ai_router] Google HTTP {resp.status}: {text[:200]}")
                    return None
                data = await resp.json()
                try:
                    parts = data["candidates"][0]["content"]["parts"]
                    return "".join(p.get("text", "") for p in parts)
                except (KeyError, IndexError, TypeError):
                    return None
    except Exception as e:
        logger.warning(f"[chat_ai_router] Google error: {e}")
        return None


async def generate_for_chat(chat_id, messages: List[Dict],
                            max_tokens: int = 800, temperature: float = 0.7) -> Optional[str]:
    cfg = _chat_cfg(chat_id)
    prov = get_chat_provider(chat_id)
    model = get_chat_model(chat_id)
    keys = cfg.get("ai_keys") or {}

    if prov == "laozhang":
        from .laozhang_client import LaozhangClient
        api_key = (keys.get("laozhang") or "").strip()
        if not api_key:
            return None
        client = LaozhangClient([api_key], model=model or DEFAULT_MODELS["laozhang"])
        try:
            return await client.chat_messages(messages=messages, model=model,
                                              max_tokens=max_tokens, temperature=temperature)
        except Exception as e:
            logger.warning(f"[chat_ai_router] laozhang fail: {e}")
            return None

    if prov == "openrouter":
        api_key = (keys.get("openrouter") or "").strip()
        if not api_key:
            return None
        return await _post_openai_compat(
            "https://openrouter.ai/api/v1/chat/completions",
            api_key, model or DEFAULT_MODELS["openrouter"],
            messages, max_tokens=max_tokens, temperature=temperature,
            extra_headers={"HTTP-Referer": "https://t.me", "X-Title": "TG-Bot-Defender"},
        )

    if prov == "google":
        api_key = (keys.get("google") or "").strip()
        if not api_key:
            return None
        return await _call_google(api_key, model or DEFAULT_MODELS["google"],
                                  messages, max_tokens=max_tokens, temperature=temperature)

    if prov == "huggingface":
        api_key = (keys.get("huggingface") or "").strip()
        if not api_key:
            return None
        return await _post_openai_compat(
            "https://router.huggingface.co/v1/chat/completions",
            api_key, model or DEFAULT_MODELS["huggingface"],
            messages, max_tokens=max_tokens, temperature=temperature,
        )

    if prov == "groq":
        api_key = (keys.get("groq") or "").strip()
        if not api_key:
            return None
        return await _post_groq(
            api_key, model or DEFAULT_MODELS["groq"],
            messages, max_tokens=max_tokens,
        )

    if prov == "custom":
        cp = cfg.get("custom_provider") or {}
        endpoint = (cp.get("endpoint") or "").strip()
        api_key = (cp.get("api_key") or "").strip()
        model_name = (model or cp.get("model") or "").strip()
        if not endpoint or not model_name:
            return None
        return await _post_openai_compat(
            endpoint, api_key, model_name,
            messages, max_tokens=max_tokens, temperature=temperature,
        )

    return None


async def generate_vision_for_chat(
    chat_id,
    image_bytes: bytes,
    messages: List[Dict],
    max_tokens: int = 20,
) -> Optional[str]:
    """
    Отправляет изображение + текст в vision-ИИ чата.
    Картинка прикрепляется к последнему user-сообщению.
    Поддерживает: Groq (llama-4), OpenRouter (vision-модели).
    """
    import base64
    cfg = _chat_cfg(chat_id)
    prov = get_chat_provider(chat_id)
    model = get_chat_model(chat_id)
    keys = cfg.get("ai_keys") or {}

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    def _inject_image(msgs: List[Dict]) -> List[Dict]:
        """Добавляет изображение в последнее user-сообщение."""
        result = []
        for i, m in enumerate(msgs):
            if m["role"] == "user" and i == len(msgs) - 1:
                text = m["content"] if isinstance(m["content"], str) else ""
                result.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                })
            else:
                result.append(m)
        return result

    vision_messages = _inject_image(messages)

    if prov == "groq":
        api_key = (keys.get("groq") or "").strip()
        if not api_key:
            return None
        use_model = model or DEFAULT_MODELS["groq"]
        if use_model not in GROQ_VISION_MODELS:
            use_model = "meta-llama/llama-4-scout-17b-16e-instruct"
        return await _post_groq(api_key, use_model, vision_messages, max_tokens=max_tokens, with_image=True)

    if prov == "openrouter":
        api_key = (keys.get("openrouter") or "").strip()
        if not api_key:
            return None
        use_model = model or DEFAULT_MODELS["openrouter"]
        return await _post_openai_compat(
            "https://openrouter.ai/api/v1/chat/completions",
            api_key, use_model,
            vision_messages, max_tokens=max_tokens, temperature=0.0,
            extra_headers={"HTTP-Referer": "https://t.me", "X-Title": "TG-Bot-Defender"},
        )

    if prov == "laozhang":
        from .laozhang_client import LaozhangClient
        api_key = (keys.get("laozhang") or "").strip()
        if not api_key:
            return None
        client = LaozhangClient([api_key], model=model or DEFAULT_MODELS["laozhang"])
        return await client.chat_messages(messages=vision_messages, max_tokens=max_tokens, temperature=0.0)

    return None


async def _post_groq(
    api_key: str,
    model: str,
    messages: List[Dict],
    max_tokens: int = 200,
    with_image: bool = False,
    max_retries: int = 3,
) -> Optional[str]:
    """POST к Groq API с автоматическим retry при rate limit (429)."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            return data["choices"][0]["message"]["content"]
                        except (KeyError, IndexError, TypeError) as e:
                            logger.warning(f"[Groq] bad format: {e}")
                            return None

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("retry-after", "5"))
                        retry_after = min(max(retry_after, 2), 30)
                        logger.warning(
                            f"[Groq] rate limit (попытка {attempt + 1}/{max_retries}), "
                            f"жду {retry_after:.1f}с..."
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    text = await resp.text()
                    logger.warning(f"[Groq] HTTP {resp.status}: {text[:200]}")
                    return None

        except asyncio.TimeoutError:
            logger.warning(f"[Groq] timeout (попытка {attempt + 1})")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"[Groq] ошибка: {e}")
            return None

    logger.warning(f"[Groq] все {max_retries} попытки исчерпаны")
    return None


# ── Авто-регистрация каталога провайдеров в глобальном хранилище ──────────
try:
    for _p in list_providers():
        _ps.register_provider(
            _p,
            PROVIDER_LABELS.get(_p, _p),
            PROVIDER_MODELS.get(_p, []),
            DEFAULT_MODELS.get(_p, ""),
        )
except Exception:
    pass