"""Глобальный чёрный список рейдеров.

Структура blacklist.json:
{
    "raiders": [123, 456, 789],          # ГЛОБАЛЬНО — на все группы
    "chats": {
        "<chat_id>": {"enabled": true}    # пер-чат: применять ЧС в этом чате или нет
    }
}

Логика:
    • Список рейдеров — глобальный (один на все группы).
    • Управлять списком (добавлять / чистить / удалять) может ТОЛЬКО владелец бота
      и ТОЛЬКО в ЛС с ботом.
    • В каждой группе админ может включить/выключить применение ЧС
      командой !список вкл/выкл.
"""
import json
import logging
import os
import tempfile
from typing import Iterable, Set

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BLACKLIST_FILE = os.path.join(_PROJECT_ROOT, "blacklist.json")

# В памяти
_raiders: Set[int] = set()
_chats: dict = {}  # {chat_id_str: {"enabled": bool}}


def load_blacklist() -> None:
    global _raiders, _chats
    try:
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Обратная совместимость со старым форматом (per-chat raiders)
            if isinstance(data, dict) and "raiders" in data and isinstance(data["raiders"], list):
                _raiders = set(int(x) for x in data["raiders"] or [])
                _chats = {}
                for cid, payload in (data.get("chats") or {}).items():
                    _chats[str(cid)] = {"enabled": bool(payload.get("enabled", False))}
            elif isinstance(data, dict):
                # Старый формат: {chat_id: {"enabled": bool, "raiders": [...]}}
                _raiders = set()
                _chats = {}
                for cid, payload in data.items():
                    if not isinstance(payload, dict):
                        continue
                    _chats[str(cid)] = {"enabled": bool(payload.get("enabled", False))}
                    for uid in (payload.get("raiders") or []):
                        try:
                            _raiders.add(int(uid))
                        except (TypeError, ValueError):
                            pass
                # Сразу пересохраним в новом формате
                save_blacklist()
            else:
                _raiders = set()
                _chats = {}
            logger.info(
                f"[BLACKLIST] Загружено: рейдеров={len(_raiders)}, чатов с настройкой={len(_chats)}"
            )
        else:
            _raiders = set()
            _chats = {}
            logger.info(f"[BLACKLIST] Файл {BLACKLIST_FILE} не найден — стартуем с пустого ЧС")
    except Exception as e:
        logger.error(f"[BLACKLIST] Ошибка загрузки: {e}")
        _raiders = set()
        _chats = {}


def save_blacklist() -> None:
    try:
        directory = os.path.dirname(BLACKLIST_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        data = {
            "raiders": sorted(list(_raiders)),
            "chats": {cid: {"enabled": bool(v.get("enabled", False))} for cid, v in _chats.items()},
        }
        fd, tmp_path = tempfile.mkstemp(prefix=".blacklist_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, BLACKLIST_FILE)
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.error(f"[BLACKLIST] Ошибка сохранения: {e}")


# ─── Глобальный список рейдеров ──────────────────────────────
def is_raider(user_id: int) -> bool:
    try:
        return int(user_id) in _raiders
    except (TypeError, ValueError):
        return False


def add_raiders(user_ids: Iterable) -> int:
    before = len(_raiders)
    for uid in user_ids:
        try:
            _raiders.add(int(uid))
        except (TypeError, ValueError):
            continue
    added = len(_raiders) - before
    if added:
        save_blacklist()
    return added


def remove_raider(user_id: int) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in _raiders:
        _raiders.discard(uid)
        save_blacklist()
        return True
    return False


def get_raiders() -> Set[int]:
    return set(_raiders)


def total_raiders() -> int:
    return len(_raiders)


def clear_all() -> int:
    n = len(_raiders)
    _raiders.clear()
    save_blacklist()
    return n


# ─── Пер-чатовое включение ────────────────────────────────────
def is_enabled(chat_id) -> bool:
    return bool(_chats.get(str(chat_id), {}).get("enabled", False))


def set_enabled(chat_id, value: bool) -> None:
    cid = str(chat_id)
    if cid not in _chats:
        _chats[cid] = {}
    _chats[cid]["enabled"] = bool(value)
    save_blacklist()


def enabled_chats() -> list:
    return [int(cid) for cid, v in _chats.items() if v.get("enabled")]
