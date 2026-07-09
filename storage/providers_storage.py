import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ai_providers.json",
)
_LOCK = threading.RLock()
_DATA: Dict[str, Any] = {"providers": {}, "chats": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> None:
    global _DATA
    with _LOCK:
        if not os.path.exists(_FILE):
            _DATA = {"providers": {}, "chats": {}}
            return
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {"providers": {}, "chats": {}}
            data.setdefault("providers", {})
            data.setdefault("chats", {})
            _DATA = data
            logger.info(
                f"[PROVIDERS] загружено: providers={len(_DATA['providers'])}, "
                f"chats={len(_DATA['chats'])}"
            )
        except Exception as e:
            logger.error(f"[PROVIDERS] read fail {_FILE}: {e}")
            _DATA = {"providers": {}, "chats": {}}


def _save() -> None:
    try:
        d = os.path.dirname(_FILE) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".providers_", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_DATA, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _FILE)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.error(f"[PROVIDERS] save fail {_FILE}: {e}")


def register_provider(key: str, label: str, models: List[str], default_model: str) -> None:
    with _LOCK:
        _DATA["providers"][key] = {
            "label": label,
            "models": list(models or []),
            "default_model": default_model or "",
        }
        _save()


def all_providers() -> Dict[str, Any]:
    with _LOCK:
        return dict(_DATA["providers"])


def _chat_node(chat_id) -> Dict[str, Any]:
    cid = str(chat_id)
    node = _DATA["chats"].get(cid)
    if not isinstance(node, dict):
        node = {
            "active_provider": None,
            "active_model": None,
            "api_keys": {},
            "custom": {},
            "updated_at": _now(),
        }
        _DATA["chats"][cid] = node
    node.setdefault("api_keys", {})
    node.setdefault("custom", {})
    return node


def save_active(chat_id, provider: str, model: str) -> None:
    with _LOCK:
        node = _chat_node(chat_id)
        node["active_provider"] = provider
        node["active_model"] = model
        node["updated_at"] = _now()
        _save()


def save_api_key(chat_id, provider: str, api_key: str) -> None:
    with _LOCK:
        node = _chat_node(chat_id)
        node["api_keys"][provider] = api_key
        node["updated_at"] = _now()
        _save()


def save_custom(chat_id, endpoint: str, api_key: str, model: str) -> None:
    with _LOCK:
        node = _chat_node(chat_id)
        node["custom"] = {"endpoint": endpoint, "api_key": api_key, "model": model}
        node["updated_at"] = _now()
        _save()


def get_chat(chat_id) -> Optional[Dict[str, Any]]:
    with _LOCK:
        node = _DATA["chats"].get(str(chat_id))
        return dict(node) if isinstance(node, dict) else None