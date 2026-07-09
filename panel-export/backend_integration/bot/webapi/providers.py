"""
AI provider/model catalogue, exposed to the panel via GET /api/ai/providers.

If your `bot/services/chat_ai_router.py` already keeps a `PROVIDER_MODELS`
(or similarly named) dict, prefer importing and reusing it here instead of
duplicating the list, so the panel never drifts from what the bot actually
supports:

    from bot.services.chat_ai_router import PROVIDER_MODELS, DEFAULT_MODELS

    def catalogue() -> list[dict]:
        return [
            {
                "id": provider_id,
                "label": provider_id,
                "models": models,
                "defaultModel": DEFAULT_MODELS.get(provider_id, models[0] if models else ""),
            }
            for provider_id, models in PROVIDER_MODELS.items()
        ]

The static list below is a ready-to-use fallback (matches the free-tier
models catalogue observed in the bot's OpenRouter/Groq/etc. usage) in case
`PROVIDER_MODELS` isn't structured exactly like this in your version -- adjust
freely to match your actual catalogue.
"""

CATALOGUE = [
    {
        "id": "laozhang",
        "label": "LaoZhang (OpenAI-совместимый)",
        "defaultModel": "gpt-4o-mini",
        "models": [
            "gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
            "claude-3.5-sonnet", "claude-3.5-haiku", "deepseek-chat", "deepseek-r1",
        ],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "defaultModel": "meta-llama/llama-3.3-70b-instruct:free",
        "models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "meta-llama/llama-3.1-405b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "mistralai/mistral-7b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "deepseek/deepseek-chat:free",
            "deepseek/deepseek-r1:free",
        ],
    },
    {
        "id": "google",
        "label": "Google Gemini",
        "defaultModel": "gemini-2.0-flash",
        "models": [
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash",
            "gemini-2.0-flash-lite", "gemini-1.5-pro", "gemini-1.5-flash",
        ],
    },
    {
        "id": "huggingface",
        "label": "Hugging Face",
        "defaultModel": "meta-llama/Llama-3.1-8B-Instruct",
        "models": [
            "meta-llama/Llama-3.1-8B-Instruct", "meta-llama/Llama-3.1-70B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3", "Qwen/Qwen2.5-72B-Instruct",
        ],
    },
    {
        "id": "groq",
        "label": "Groq",
        "defaultModel": "llama-3.3-70b-versatile",
        "models": [
            "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
            "mixtral-8x7b-32768", "gemma2-9b-it",
        ],
    },
    {
        "id": "custom",
        "label": "Свой провайдер (OpenAI-совместимый)",
        "defaultModel": "",
        "models": [],
    },
]


def catalogue() -> list[dict]:
    return CATALOGUE


def is_valid(provider_id: str, model: str) -> bool:
    if provider_id == "custom":
        return True
    entry = next((p for p in CATALOGUE if p["id"] == provider_id), None)
    return bool(entry and model in entry["models"])
