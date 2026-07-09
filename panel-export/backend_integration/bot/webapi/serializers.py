"""
The single place that knows how the bot's internal settings dicts map to the
JSON shapes the web panel expects. If a field name differs in your actual
`bot/core/config.py` / `bot/handlers/*.py`, fix it here rather than scattering
renames through `server.py`.
"""

from __future__ import annotations

ANTISPAM_DEFAULTS = {
    "enabled": False, "punishment": "мут", "duration": 30, "unit": "мин",
    "threshold_count": 5, "threshold_seconds": 10, "duplicate_limit": 3,
}

ANTINSFW_DEFAULTS = {
    "enabled": False, "punishment": "мут", "duration": 30, "unit": "мин",
}

def chat_summary(chat_id: int, chat_settings: dict, tg_chat) -> dict:
    """`tg_chat` is the aiogram Chat object from `bot.get_chat(chat_id)`."""
    return {
        "id": str(chat_id),
        "title": tg_chat.title or str(chat_id),
        "type": tg_chat.type,
        "photoUrl": None,  # Fetch via bot.get_chat(...).photo + get_file if you want real avatars.
        "memberCount": 0,  # aiogram does not expose member count cheaply; 0 is a valid sentinel.
        "isOwner": False,
        "isAdmin": True,
        "addedByUserName": chat_settings.get("added_by_user_name"),
        "privacyAccepted": bool(chat_settings.get("privacy_accepted", True)),
    }


def antispam(chat_settings: dict) -> dict:
    return {**ANTISPAM_DEFAULTS, **chat_settings.get("antispam", {})}


def antinsfw(chat_settings: dict) -> dict:
    return {**ANTINSFW_DEFAULTS, **chat_settings.get("antinsfw", {})}


def ai_settings(chat_id: int, chat_settings: dict) -> dict:
    # Reuses the bot's own AI-routing helpers so the panel's view of AI
    # config always matches what `chat_ai_router` will actually use.
    from bot.services import chat_ai_router as router
    from bot.storage import providers_storage

    provider = router.get_chat_provider(chat_id)
    model = router.get_chat_model(chat_id)
    custom_provider = chat_settings.get("custom_provider", {}) or {}

    provider_keys = {}
    for entry in ("laozhang", "openrouter", "google", "huggingface", "groq"):
        try:
            provider_keys[entry] = bool(providers_storage.has_chat_api_key(chat_id, entry))
        except AttributeError:
            # If your providers_storage doesn't expose has_chat_api_key, fall
            # back to checking the raw settings dict for a stored key.
            provider_keys[entry] = bool(chat_settings.get("provider_keys", {}).get(entry))

    return {
        "ai_enabled": bool(router.is_ai_enabled(chat_id)),
        "personality": chat_settings.get("personality", "нейтральный"),
        "custom": chat_settings.get("custom", ""),
        "ai_provider": provider,
        "ai_model": model,
        "custom_provider": {
            "endpoint": custom_provider.get("endpoint", ""),
            "model": custom_provider.get("model", ""),
            "hasKey": bool(custom_provider.get("has_key", False)),
        },
        "providerKeys": provider_keys,
    }


def anti_raid_status(chat_id: int, anti_raid_storage) -> dict:
    import datetime

    until = anti_raid_storage._lockdown_until.get(chat_id)  # noqa: SLF001
    active = bool(until and until > datetime.datetime.now())
    return {"lockdownActive": active, "joinsInWindow": 0, "joinThreshold": 0}
