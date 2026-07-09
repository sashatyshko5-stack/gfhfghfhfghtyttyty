from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_GLOBAL_GROQ_FILE = Path(__file__).resolve().parent.parent.parent / "global_groq.json"
_global_groq_key: str = ""


def load_global_groq() -> None:
    global _global_groq_key
    try:
        if _GLOBAL_GROQ_FILE.exists():
            with open(_GLOBAL_GROQ_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _global_groq_key = str(data.get("api_key") or "").strip()
    except Exception as e:
        logger.error(f"[GLOBAL-GROQ] load fail: {e}")


def save_global_groq() -> None:
    try:
        _GLOBAL_GROQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GLOBAL_GROQ_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"api_key": _global_groq_key}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _GLOBAL_GROQ_FILE)
    except Exception as e:
        logger.error(f"[GLOBAL-GROQ] save fail: {e}")


def get_global_groq_key() -> str:
    return _global_groq_key


def set_global_groq_key(key: str) -> None:
    global _global_groq_key
    _global_groq_key = key.strip()
    save_global_groq()


load_global_groq()
