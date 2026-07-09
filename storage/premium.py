"""Хранилище премиум-подписок пользователей и чатов."""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_PREMIUM_FILE = os.path.join("settings", "premium.json")

_MAX_CHATS_BY_PLAN: dict[str, int] = {
    "monthly": 5,
    "yearly": 10,
    "test": 5,  # тестовый тариф для разработчика
}
_MAX_PREMIUM_CHATS = 5  # обратная совместимость

_data: dict = {}


def _load() -> None:
    global _data
    try:
        with open(_PREMIUM_FILE, encoding="utf-8") as f:
            _data = json.load(f)
        logger.info(
            f"[PREMIUM] Загружено {len(_data.get('users', {}))} пользователей, "
            f"{len(_data.get('chats', {}))} чатов"
        )
    except FileNotFoundError:
        _data = {"users": {}, "chats": {}}
    except Exception as e:
        _data = {"users": {}, "chats": {}}
        logger.error(f"[PREMIUM] Ошибка загрузки: {e}")


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(_PREMIUM_FILE) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".premium_", suffix=".tmp",
            dir=os.path.dirname(_PREMIUM_FILE) or ".",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _PREMIUM_FILE)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.error(f"[PREMIUM] Ошибка сохранения: {e}")


_load()


def _users() -> dict:
    return _data.setdefault("users", {})


def _chats() -> dict:
    return _data.setdefault("chats", {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_active(expires_iso: str) -> bool:
    try:
        exp = datetime.fromisoformat(expires_iso)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < exp
    except Exception:
        return False


# ─── Пользовательский премиум ─────────────────────────────────────────────────

def has_premium(user_id: int) -> bool:
    rec = _users().get(str(user_id))
    if not rec:
        return False
    return _is_active(rec.get("expires", ""))


def get_premium_info(user_id: int) -> dict | None:
    rec = _users().get(str(user_id))
    if not rec or not has_premium(user_id):
        return None
    return rec


def get_plan(user_id: int) -> str:
    return _users().get(str(user_id), {}).get("plan", "monthly")


def get_chat_limit(user_id: int) -> int:
    return _MAX_CHATS_BY_PLAN.get(get_plan(user_id), 5)


def activate_premium(user_id: int, plan: str) -> dict:
    now = datetime.now(timezone.utc)
    days = 365 if plan == "yearly" else 30
    uid = str(user_id)
    existing_chats = _users().get(uid, {}).get("chats", [])
    rec = {
        "plan": plan,
        "activated_at": _now_iso(),
        "expires": (now + timedelta(days=days)).isoformat(),
        "chats": existing_chats,
    }
    _users()[uid] = rec
    _save()
    return rec


def get_premium_chats(user_id: int) -> list[int]:
    return list(_users().get(str(user_id), {}).get("chats", []))


def can_add_premium_chat(user_id: int) -> bool:
    if not has_premium(user_id):
        return False
    return len(get_premium_chats(user_id)) < get_chat_limit(user_id)


def register_premium_chat(user_id: int, chat_id: int) -> bool:
    if not has_premium(user_id):
        return False
    uid = str(user_id)
    rec = _users().setdefault(uid, {})
    chats = rec.setdefault("chats", [])
    if chat_id in chats:
        return True
    if len(chats) >= get_chat_limit(user_id):
        return False
    chats.append(chat_id)
    _save()
    return True


def unregister_premium_chat(user_id: int, chat_id: int) -> None:
    uid = str(user_id)
    rec = _users().get(uid)
    if not rec:
        return
    try:
        rec.get("chats", []).remove(chat_id)
        _save()
    except ValueError:
        pass


def is_premium_chat(chat_id: int) -> tuple[bool, int | None]:
    for uid_str, rec in _users().items():
        if chat_id in rec.get("chats", []):
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            if has_premium(uid):
                return True, uid
    if has_chat_premium(chat_id):
        info = get_chat_premium_info(chat_id)
        owner = info.get("owner_user_id") if info else None
        return True, owner
    return False, None


def get_expires_str(user_id: int) -> str:
    rec = _users().get(str(user_id))
    if not rec:
        return "нет"
    expires = rec.get("expires", "")
    try:
        return datetime.fromisoformat(expires).strftime("%d.%m.%Y")
    except Exception:
        return expires[:10] if expires else "нет"


def get_all_premium_users() -> list[dict]:
    return [
        {"user_id": int(uid), **rec}
        for uid, rec in _users().items()
        if has_premium(int(uid))
    ]


# ─── Чат-премиум ─────────────────────────────────────────────────────────────

def has_chat_premium(chat_id: int) -> bool:
    rec = _chats().get(str(chat_id))
    if not rec:
        return False
    return _is_active(rec.get("expires", ""))


def get_chat_premium_info(chat_id: int) -> dict | None:
    rec = _chats().get(str(chat_id))
    if not rec or not has_chat_premium(chat_id):
        return None
    return rec


def activate_chat_premium(chat_id: int, plan: str, owner_user_id: int) -> dict:
    now = datetime.now(timezone.utc)
    days = 365 if plan == "yearly" else 30
    rec = {
        "plan": plan,
        "activated_at": _now_iso(),
        "expires": (now + timedelta(days=days)).isoformat(),
        "owner_user_id": owner_user_id,
    }
    _chats()[str(chat_id)] = rec
    _save()
    return rec


def get_chat_expires_str(chat_id: int) -> str:
    rec = _chats().get(str(chat_id))
    if not rec:
        return "нет"
    expires = rec.get("expires", "")
    try:
        return datetime.fromisoformat(expires).strftime("%d.%m.%Y")
    except Exception:
        return expires[:10] if expires else "нет"
