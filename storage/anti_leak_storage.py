import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEAK_REGISTRY_PATH = os.path.join(_PROJECT_ROOT, "group_settings", "anti_leak_registry.json")
USER_RISK_PATH = os.path.join(_PROJECT_ROOT, "group_settings", "anti_leak_users.json")

# In-memory cache
_leak_registry: Optional[dict] = None
_user_risk_db: Optional[dict] = None


# ─── Helpers ────────────────────────────────────────────────────────────────

def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _atomic_json_save(path: str, data: dict):
    _ensure_dir(path)
    fd, tmp = tempfile.mkstemp(prefix=".leak_", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def _load_json(path: str, default: dict) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    return loaded
        except Exception as e:
            logger.error(f"[ANTI-LEAK-STORAGE] Ошибка загрузки {path}: {e}")
    return default.copy()


# ─── Leak Registry ────────────────────────────────────────────────────────

def get_leak_registry() -> dict:
    global _leak_registry
    if _leak_registry is None:
        _leak_registry = _load_json(LEAK_REGISTRY_PATH, {
            "invite_links": {},      # hash -> {chat_id, created_at, is_primary, is_revoked, ...}
            "leaked_links": [],       # список событий утечек
            "join_events": [],        # список входов
            "version": 1,
        })
    return _leak_registry


def save_leak_registry():
    reg = get_leak_registry()
    try:
        _atomic_json_save(LEAK_REGISTRY_PATH, reg)
    except Exception as e:
        logger.error(f"[ANTI-LEAK-STORAGE] Ошибка сохранения реестра: {e}")


# ─── Invite Links ─────────────────────────────────────────────────────────

def register_invite_link(chat_id: int, invite_hash: str, created_by: Optional[int] = None,
                         is_primary: bool = False, expire_date: Optional[str] = None,
                         member_limit: Optional[int] = None):
    """Регистрирует invite link как "наш" для мониторинга."""
    reg = get_leak_registry()
    reg.setdefault("invite_links", {})
    reg["invite_links"][invite_hash] = {
        "chat_id": chat_id,
        "created_at": datetime.now().isoformat(),
        "created_by": created_by,
        "is_primary": is_primary,
        "expire_date": expire_date,
        "member_limit": member_limit,
        "is_revoked": False,
        "revoked_at": None,
    }
    save_leak_registry()
    logger.info(f"[ANTI-LEAK] Invite link +{invite_hash} зарегистрирован для чата {chat_id}")


def revoke_invite_link_record(invite_hash: str):
    reg = get_leak_registry()
    links = reg.get("invite_links", {})
    if invite_hash in links:
        links[invite_hash]["is_revoked"] = True
        links[invite_hash]["revoked_at"] = datetime.now().isoformat()
        save_leak_registry()
        logger.info(f"[ANTI-LEAK] Invite link +{invite_hash} помечен как отозванный")


def get_invite_link_info(invite_hash: str) -> Optional[dict]:
    return get_leak_registry().get("invite_links", {}).get(invite_hash)


def get_chat_invite_links(chat_id: int) -> Dict[str, dict]:
    """Все invite links, привязанные к чату."""
    result = {}
    for h, info in get_leak_registry().get("invite_links", {}).items():
        if info.get("chat_id") == chat_id:
            result[h] = info
    return result


# ─── Leaked Links ─────────────────────────────────────────────────────────

def register_leaked_link(invite_hash: str, source_chat_id: Optional[int] = None,
                         source_chat_name: Optional[str] = None,
                         source_message_id: Optional[int] = None,
                         context_text: Optional[str] = None,
                         leak_confidence: float = 0.9):
    """Регистрирует утечку ссылки (вызывается userbot'ом)."""
    reg = get_leak_registry()
    reg.setdefault("leaked_links", [])

    # Дедупликация по hash+source_chat_id в течение 1 часа
    now = datetime.now()
    cutoff = now - timedelta(hours=1)
    for ll in reg["leaked_links"]:
        if (ll.get("hash") == invite_hash and
            ll.get("source_chat_id") == source_chat_id):
            try:
                t = datetime.fromisoformat(ll.get("found_at", "1970-01-01T00:00:00"))
                if t > cutoff:
                    return  # Уже зарегистрировано недавно
            except Exception:
                pass

    reg["leaked_links"].append({
        "hash": invite_hash,
        "found_at": now.isoformat(),
        "source_chat_id": source_chat_id,
        "source_chat_name": source_chat_name,
        "source_message_id": source_message_id,
        "context_text": (context_text or "")[:500],
        "leak_confidence": leak_confidence,
        "status": "active",
    })
    save_leak_registry()
    logger.warning(f"[ANTI-LEAK] УТЕЧКА: +{invite_hash} найдена в {source_chat_name} (conf={leak_confidence})")


def get_leaked_links_for_hash(invite_hash: str) -> List[dict]:
    return [ll for ll in get_leak_registry().get("leaked_links", []) if ll.get("hash") == invite_hash]


def is_link_leaked(invite_hash: str) -> bool:
    return len(get_leaked_links_for_hash(invite_hash)) > 0


def get_active_leaks_for_chat(chat_id: int) -> List[dict]:
    """Все активные утечки ссылок, принадлежащих данному чату."""
    links = get_chat_invite_links(chat_id)
    result = []
    for ll in get_leak_registry().get("leaked_links", []):
        if ll.get("hash") in links and ll.get("status") == "active":
            result.append({**ll, "chat_id": chat_id})
    return result


# ─── Join Events ──────────────────────────────────────────────────────────

def record_join_event(user_id: int, chat_id: int, invite_hash: Optional[str] = None,
                      join_method: str = "unknown", correlation_confidence: float = 0.0):
    reg = get_leak_registry()
    reg.setdefault("join_events", [])
    event = {
        "user_id": user_id,
        "chat_id": chat_id,
        "joined_at": datetime.now().isoformat(),
        "invite_hash": invite_hash,
        "join_method": join_method,
        "correlation_confidence": correlation_confidence,
    }
    reg["join_events"].append(event)
    # Ограничиваем размер join_events (храним последние 5000)
    if len(reg["join_events"]) > 5000:
        reg["join_events"] = reg["join_events"][-5000:]
    save_leak_registry()
    logger.info(f"[ANTI-LEAK] JOIN: user={user_id} chat={chat_id} hash={invite_hash} method={join_method}")


def get_recent_joins(chat_id: int, minutes: int = 10) -> List[dict]:
    cutoff = datetime.now() - timedelta(minutes=minutes)
    result = []
    for ev in get_leak_registry().get("join_events", []):
        if ev.get("chat_id") != chat_id:
            continue
        try:
            t = datetime.fromisoformat(ev.get("joined_at", "1970-01-01T00:00:00"))
            if t > cutoff:
                result.append(ev)
        except Exception:
            pass
    return result


def get_join_events_for_user(user_id: int, chat_id: Optional[int] = None) -> List[dict]:
    result = []
    for ev in get_leak_registry().get("join_events", []):
        if ev.get("user_id") != user_id:
            continue
        if chat_id is not None and ev.get("chat_id") != chat_id:
            continue
        result.append(ev)
    return result


def cleanup_old_join_events(hours: int = 48):
    """Удаляет join events старше N часов."""
    cutoff = datetime.now() - timedelta(hours=hours)
    reg = get_leak_registry()
    before = len(reg.get("join_events", []))
    reg["join_events"] = [
        ev for ev in reg.get("join_events", [])
        if _parse_iso(ev.get("joined_at")) > cutoff
    ]
    after = len(reg["join_events"])
    if before != after:
        save_leak_registry()
        logger.info(f"[ANTI-LEAK] Очистка join events: {before} -> {after}")


def _parse_iso(s: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat(s or "1970-01-01T00:00:00")
    except Exception:
        return datetime(1970, 1, 1)


# ─── User Risk DB ─────────────────────────────────────────────────────────

def get_user_risk_db() -> dict:
    global _user_risk_db
    if _user_risk_db is None:
        _user_risk_db = _load_json(USER_RISK_PATH, {"users": {}})
    return _user_risk_db


def save_user_risk_db():
    db = get_user_risk_db()
    try:
        _atomic_json_save(USER_RISK_PATH, db)
    except Exception as e:
        logger.error(f"[ANTI-LEAK-STORAGE] Ошибка сохранения risk DB: {e}")


def ensure_user_record(user_id: int, username: Optional[str] = None,
                       first_name: Optional[str] = None):
    db = get_user_risk_db()
    db.setdefault("users", {})
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "first_seen_at": datetime.now().isoformat(),
            "risk_score": 0.0,
            "risk_tier": "unknown",
            "has_avatar": None,
            "is_premium": None,
            "account_created_at": None,
            "join_count": 0,
            "banned_in": [],
            "factors_history": [],
        }
        save_user_risk_db()


def update_user_profile(user_id: int, has_avatar: Optional[bool] = None,
                        is_premium: Optional[bool] = None,
                        account_created_at: Optional[str] = None):
    ensure_user_record(user_id)
    db = get_user_risk_db()
    u = db["users"][str(user_id)]
    if has_avatar is not None:
        u["has_avatar"] = has_avatar
    if is_premium is not None:
        u["is_premium"] = is_premium
    if account_created_at is not None:
        u["account_created_at"] = account_created_at
    save_user_risk_db()


# ─── Risk Scoring ─────────────────────────────────────────────────────────

RISK_FACTORS = {
    "account_age_hours": {
        "< 1": 0.30,
        "< 24": 0.20,
        "< 168": 0.10,
        ">= 168": 0.00,
    },
    "has_avatar": {False: 0.15, True: 0.00},
    "is_premium": {False: 0.05, True: 0.00},
    "join_spike_correlated": {True: 0.25, False: 0.00},
    "phone_country_risk": {"high": 0.10, "normal": 0.00},
    "username_pattern": {
        "random_8char": 0.10,
        "sequential": 0.15,
        "normal": 0.00,
    },
    "previous_bans": {">= 1": 0.20, "0": 0.00},
    "leaked_link_join": {True: 0.35, False: 0.00},
}


def calculate_risk(user_id: int, join_context: Optional[dict] = None) -> dict:
    """Пересчитывает risk score пользователя. Возвращает {score, tier, factors}."""
    ensure_user_record(user_id)
    db = get_user_risk_db()
    u = db["users"][str(user_id)]

    factors = {}

    # 1. Account age
    acc_created = _parse_iso(u.get("account_created_at"))
    if acc_created.year < 2020:
        # Если неизвестно — эвристика по ID (Telegram ID примерно пропорционален времени)
        # ID > 5_000_000_000 ~ очень новые (2021+)
        # ID > 7_000_000_000 ~ 2022+
        # ID > 10_000_000_000 ~ 2024+
        uid = user_id
        if uid > 10_000_000_000:
            factors["account_age_hours"] = 0.30
        elif uid > 7_000_000_000:
            factors["account_age_hours"] = 0.20
        elif uid > 5_000_000_000:
            factors["account_age_hours"] = 0.10
        else:
            factors["account_age_hours"] = 0.00
    else:
        age_hours = (datetime.now() - acc_created).total_seconds() / 3600
        if age_hours < 1:
            factors["account_age_hours"] = 0.30
        elif age_hours < 24:
            factors["account_age_hours"] = 0.20
        elif age_hours < 168:
            factors["account_age_hours"] = 0.10
        else:
            factors["account_age_hours"] = 0.00

    # 2. Avatar
    if u.get("has_avatar") is False:
        factors["has_avatar"] = 0.15
    else:
        factors["has_avatar"] = 0.00

    # 3. Premium
    if u.get("is_premium") is False:
        factors["is_premium"] = 0.05
    else:
        factors["is_premium"] = 0.00

    # 4. Username pattern
    username = (u.get("username") or "").lower()
    if username:
        if re.match(r"^.*[_\-]?\d{3,}$", username):
            factors["username_pattern"] = 0.10
        elif re.match(r"^user\d+$", username):
            factors["username_pattern"] = 0.15
        else:
            factors["username_pattern"] = 0.00
    else:
        factors["username_pattern"] = 0.00

    # 5. Previous bans
    bans = u.get("banned_in", [])
    if len(bans) >= 1:
        factors["previous_bans"] = 0.20
    else:
        factors["previous_bans"] = 0.00

    # 6. Join context
    if join_context:
        # Spike correlation
        if join_context.get("spike_correlated"):
            factors["join_spike_correlated"] = 0.25
        else:
            factors["join_spike_correlated"] = 0.00

        # Leaked link join
        if join_context.get("leaked_link"):
            factors["leaked_link_join"] = 0.35
        else:
            factors["leaked_link_join"] = 0.00
    else:
        factors["join_spike_correlated"] = 0.00
        factors["leaked_link_join"] = 0.00

    total = sum(factors.values())
    score = min(total, 1.0)

    if score >= 0.85:
        tier = "critical"
    elif score >= 0.65:
        tier = "high"
    elif score >= 0.35:
        tier = "medium"
    elif score > 0.0:
        tier = "low"
    else:
        tier = "unknown"

    u["risk_score"] = round(score, 3)
    u["risk_tier"] = tier
    u["factors_history"].append({
        "at": datetime.now().isoformat(),
        "factors": factors,
        "score": round(score, 3),
    })
    # Ограничиваем историю
    if len(u["factors_history"]) > 50:
        u["factors_history"] = u["factors_history"][-50:]

    save_user_risk_db()
    return {"score": score, "tier": tier, "factors": factors}


import re


def get_user_risk(user_id: int) -> dict:
    ensure_user_record(user_id)
    db = get_user_risk_db()
    u = db["users"].get(str(user_id), {})
    return {
        "score": u.get("risk_score", 0.0),
        "tier": u.get("risk_tier", "unknown"),
        "factors": u.get("factors_history", [{}])[-1].get("factors", {}) if u.get("factors_history") else {},
    }


def mark_user_banned(user_id: int, chat_id: int):
    ensure_user_record(user_id)
    db = get_user_risk_db()
    u = db["users"][str(user_id)]
    bans = u.setdefault("banned_in", [])
    cid = str(chat_id)
    if cid not in bans:
        bans.append(cid)
        save_user_risk_db()


def increment_user_join_count(user_id: int):
    ensure_user_record(user_id)
    db = get_user_risk_db()
    db["users"][str(user_id)]["join_count"] = db["users"][str(user_id)].get("join_count", 0) + 1
    save_user_risk_db()
