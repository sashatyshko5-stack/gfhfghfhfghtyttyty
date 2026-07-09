import json
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODS_FILE = os.path.join(_PROJECT_ROOT, "moderators.json")

LEVEL_NAMES = {1: "хелпер", 2: "модер", 3: "админ", 4: "владелец"}

REPLY_TITLES = {
    1: "📬 Ответ от поддержки:",
    2: "📬 Ответ от модератора:",
    3: "📬 Ответ от администрации:",
    4: "📬 Ответ от владельца:",
}

_mods: dict = {}


def _load() -> dict:
    global _mods
    if not os.path.exists(MODS_FILE):
        _mods = {}
        return _mods
    try:
        with open(MODS_FILE, "r", encoding="utf-8") as f:
            _mods = json.load(f) or {}
    except Exception as e:
        logger.error(f"[MODS] load fail: {e}")
        _mods = {}
    return _mods


def _save() -> None:
    try:
        with open(MODS_FILE, "w", encoding="utf-8") as f:
            json.dump(_mods, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[MODS] save fail: {e}")


_load()


def get_level(user_id: int) -> int:
    rec = _mods.get(str(user_id))
    if not rec:
        return 0
    try:
        return int(rec.get("level", 0))
    except Exception:
        return 0


def is_moderator(user_id: int, min_level: int = 1) -> bool:
    return get_level(user_id) >= min_level


def assign(user_id: int, by_user_id: int, level: int = 1) -> dict:
    level = max(1, min(4, int(level)))
    _mods[str(user_id)] = {
        "level": level,
        "added_at": datetime.now().isoformat(timespec="seconds"),
        "added_by": by_user_id,
    }
    _save()
    return _mods[str(user_id)]


def set_level(user_id: int, level: int, by_user_id: int) -> Optional[dict]:
    level = max(1, min(4, int(level)))
    rec = _mods.get(str(user_id))
    if not rec:
        return assign(user_id, by_user_id, level)
    rec["level"] = level
    rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
    rec["updated_by"] = by_user_id
    _save()
    return rec


def demote(user_id: int, by_user_id: int):
    rec = _mods.get(str(user_id))
    if not rec:
        return False, "Пользователь не в списке модераторов."
    cur = int(rec.get("level", 0))
    if cur <= 1:
        _mods.pop(str(user_id), None)
        _save()
        return True, "Уровень был 1 — пользователь удалён из модераторов."
    new_lvl = cur - 1
    rec["level"] = new_lvl
    rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
    rec["updated_by"] = by_user_id
    _save()
    return True, f"Уровень понижен: {cur} → {new_lvl} ({LEVEL_NAMES.get(new_lvl, '?')})"


def remove(user_id: int) -> bool:
    if str(user_id) in _mods:
        _mods.pop(str(user_id), None)
        _save()
        return True
    return False


def list_all():
    out = []
    for uid_str, rec in _mods.items():
        try:
            out.append((int(uid_str), rec))
        except ValueError:
            continue
    out.sort(key=lambda x: (-int(x[1].get("level", 0)), x[0]))
    return out


def all_ids(min_level: int = 1):
    return [uid for uid, rec in list_all() if int(rec.get("level", 0)) >= min_level]


def reply_title_for(user_id: int, owner_id: int, support_id: int) -> str:
    if user_id == owner_id:
        return "📬 Ответ от разработчика:"
    lvl = get_level(user_id)
    if lvl >= 1:
        return REPLY_TITLES.get(lvl, "📬 Ответ от поддержки:")
    if user_id == support_id:
        return "📬 Ответ от поддержки:"
    return "📬 Ответ от поддержки:"
# ============================================================
#  ПРАВА НА КОМАНДЫ В ЛС
# ============================================================
# Минимальный уровень для каждой DM-команды.
# 1 — хелпер, 2 — модер, 3 — админ, 4 — владелец.
# Чем выше число — тем уже круг.
DM_COMMAND_MIN_LEVEL = {
    # читать/смотреть — модер и выше
    "!список_соо":     2,
    "!чаты":           2,
    "!участники":      2,
    "!кто_в_сети":     2,
    "!сообщения":      2,
    "!получить_айди":  2,

    # банить — админ и выше
    "!бан":            3,
    "!разбан":         3,
    "!массбан":        3,
    "!ссылка":         3,
    "!реплай":         3,

    # тяжёлая артиллерия — только владелец
    "!снос_чата":      4,
    "!глобалсоо":      4,
    "!глобал_соо":     4,
}


def can_use_dm_command(user_id: int, command: str, owner_id: int) -> bool:
    """OWNER может всё. Остальным — по таблице DM_COMMAND_MIN_LEVEL."""
    if user_id == owner_id:
        return True
    cmd = command.lower().lstrip(".").lstrip("!")
    cmd = "!" + cmd.split()[0] if cmd else ""
    min_lvl = DM_COMMAND_MIN_LEVEL.get(cmd)
    if min_lvl is None:
        return False  # неизвестные команды — только OWNER
    return get_level(user_id) >= min_lvl


def actor_label(user_id: int, owner_id: int) -> str:
    """Человекочитаемая роль исполнителя — для логов и уведомлений."""
    if user_id == owner_id:
        return "владелец"
    lvl = get_level(user_id)
    return LEVEL_NAMES.get(lvl, "пользователь")


def is_protected(target_user_id: int, owner_id: int) -> bool:
    """Нельзя банить владельца и любого модератора."""
    if target_user_id == owner_id:
        return True
    return get_level(target_user_id) >= 1