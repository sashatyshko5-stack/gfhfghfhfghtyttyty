
import asyncio
import logging
import re
import time
import unicodedata
import html
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple, Any, Awaitable, Callable
from collections import defaultdict, deque, Counter

from aiogram import Router, F, Bot, BaseMiddleware
from aiogram.types import (
    Message, ChatMemberUpdated, ChatMemberAdministrator, ChatMemberOwner,
    TelegramObject, ChatPermissions,
)
from aiogram.exceptions import TelegramAPIError

from ..core.loader import bot
from ..storage.state import settings, save_settings
from ..core.utils import is_admin

logger = logging.getLogger(__name__)
print("ANTI-RAID МОДУЛЬ ЗАГРУЖЕН (v3)!")
router = Router()

# ============================================================
# Настройки по умолчанию
# ============================================================
DEFAULT_SETTINGS = {
    "enabled": False,
    "join_threshold": 5,           # массвход: N юзеров
    "join_window": 300,            # за окно секунд (5 минут по умолчанию)
    "lockdown_duration": 600,
    "ban_new_joins": True,
    "restrict_chat": True,         # закрыть @everyone на запись на время локдауна
    "ban_during_lockdown": True,   # банить любого, кто пытается войти при локдауне
    "notify_admins": True,
    "pin_alert": True,             # ЗАКРЕПЛЯТЬ сообщение о рейде
    "ban_for_tags": True,          # банить за запрещённые теги в нике
    "delete_links": True,          # удалять сообщения со ссылками от новых участников
    "analyze_photos": True,
    # пороги умного детекта
    "same_tag_threshold": 3,       # 3+ одинаковых тега у новых юзеров
    "same_msg_threshold": 4,       # 4+ одинаковых сообщений от разных
    "same_sticker_threshold": 5,   # 5+ одинаковых стикеров от разных
    "msg_window": 60,              # окно для текстов/стикеров — 60 сек
    "laozhang_api_url": "https://api.laozhang.ai/v1/chat/completions",
    "laozhang_api_token": "legacy",
    "test_mode": False,
}

# Базовые рейд-теги (после нормализации)
FORBIDDEN_TAGS = [
    "marf", "pdvl", "kxd", "kind",
    "#marf", "#pdvl", "#kxd", "#kind",
]

LINK_PATTERNS = [r't\.me/', r'telegram\.me/', r'http://', r'https://']


# ============================================================
# UNICODE НОРМАЛИЗАЦИЯ (банить теги, написанные другим шрифтом)
# ============================================================
_CONFUSABLE_MAP = {
    # mathematical bold/italic/sans/script/fraktur/double-struck буквы → ascii
}


def _build_confusable_map() -> Dict[int, str]:
    """Строим карту 'красивый юникод буква → латиница/цифра' для нормализации."""
    out: Dict[int, str] = {}
    for cp in range(0x1D400, 0x1D800):
        ch = chr(cp)
        norm = unicodedata.normalize("NFKD", ch)
        norm = "".join(c for c in norm if c.isascii() and (c.isalnum()))
        if norm:
            out[cp] = norm.lower()
    for cp in range(0xFF10, 0xFF5B):
        ch = chr(cp)
        norm = unicodedata.normalize("NFKC", ch)
        if norm.isascii() and norm.isalnum():
            out[cp] = norm.lower()
    cyr_to_lat = {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
        "у": "y", "к": "k", "м": "m", "н": "h", "в": "b", "т": "t",
        "А": "a", "Е": "e", "О": "o", "Р": "p", "С": "c", "Х": "x",
        "У": "y", "К": "k", "М": "m", "Н": "h", "В": "b", "Т": "t",
    }
    for k, v in cyr_to_lat.items():
        out[ord(k)] = v
    return out


_CONFUSABLES = _build_confusable_map()


def normalize_text(text: str) -> str:
    """Жёсткая нормализация: убираем zero-width, приводим юникод к ascii, в lower."""
    if not text:
        return ""
    text = re.sub(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF]", "", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_CONFUSABLES)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def has_forbidden_tags(username: str, first_name: str, last_name: str = "") -> bool:
    """Ловим запрещённые теги даже если написаны 𝓯𝓻𝓪𝓴𝓽𝓾𝓻'ом, full-width
    или кириллицей-похожей-на-латиницу."""
    raw = f"{username or ''} {first_name or ''} {last_name or ''}"
    n = normalize_text(raw)
    compact = re.sub(r"[^a-z0-9#]+", "", n)
    for tag in FORBIDDEN_TAGS:
        t = normalize_text(tag).lstrip("#")
        if t and t in compact:
            return True
    return False


def has_links(text: str) -> bool:
    if not text:
        return False
    for p in LINK_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def extract_user_tags(username: str, first_name: str, last_name: str = "") -> Set[str]:
    """Достаём 'теги' — буквенные группы 3-12 символов из ника (нормализованного)."""
    n = normalize_text(f"{username or ''} {first_name or ''} {last_name or ''}")
    return set(re.findall(r"[a-z0-9]{3,12}", n))


# ============================================================
# Storage
# ============================================================
class AntiRaidStorage:
    def __init__(self):
        self._join_times: Dict[int, deque] = defaultdict(deque)
        self._join_message_ids: Dict[int, deque] = defaultdict(deque)
        self._new_users: Dict[int, Dict[int, datetime]] = defaultdict(dict)
        self._lockdown_until: Dict[int, Optional[datetime]] = {}
        self._processed_joins: Dict[int, Set[int]] = defaultdict(set)
        # для умного детекта
        self._recent_user_tags: Dict[int, deque] = defaultdict(deque)
        self._recent_messages: Dict[int, deque] = defaultdict(deque)
        self._recent_stickers: Dict[int, deque] = defaultdict(deque)
        # учёт того, кого уже забанили в текущей вспышке
        self._raiders_in_wave: Dict[int, Set[int]] = defaultdict(set)
        # сохранённые перед локдауном пермишены чата (чтобы корректно вернуть)
        self._saved_perms: Dict[int, Optional[dict]] = {}
        # последнее закреплённое нами сообщение (id) — чтобы открепить после
        self._pinned_alert: Dict[int, int] = {}
        # флаг: снято ли вручную (чтобы автотаймер не дублировал)
        self._manually_deactivated: Set[int] = set()
        self.test_mode: Dict[int, dict] = {}

    # --- джойны ---
    def add_join(self, chat_id: int, user_id: int) -> bool:
        if user_id in self._processed_joins[chat_id]:
            return False
        self._processed_joins[chat_id].add(user_id)
        if len(self._processed_joins[chat_id]) > 2000:
            self._processed_joins[chat_id] = set(list(self._processed_joins[chat_id])[-1000:])
        self._join_times[chat_id].append((time.time(), user_id))
        self._new_users[chat_id][user_id] = datetime.now()
        return True

    def add_join_message(self, chat_id: int, message_id: int):
        self._join_message_ids[chat_id].append((time.time(), message_id))

    def add_user_tags(self, chat_id: int, user_id: int, tags: Set[str]):
        self._recent_user_tags[chat_id].append((time.time(), user_id, tags))

    def add_message(self, chat_id: int, user_id: int, text_norm: str):
        if not text_norm:
            return
        self._recent_messages[chat_id].append((time.time(), user_id, text_norm))

    def add_sticker(self, chat_id: int, user_id: int, sticker_id: str):
        self._recent_stickers[chat_id].append((time.time(), user_id, sticker_id))

    def cleanup(self, chat_id: int, join_window: int, msg_window: int):
        now = time.time()
        for q, w in (
            (self._join_times[chat_id], join_window),
            (self._join_message_ids[chat_id], join_window),
            (self._recent_user_tags[chat_id], join_window),
            (self._recent_messages[chat_id], msg_window),
            (self._recent_stickers[chat_id], msg_window),
        ):
            while q and now - q[0][0] > w:
                q.popleft()

    def get_recent_joins(self, chat_id: int, window: int) -> List[int]:
        now = time.time()
        return [u for t, u in self._join_times[chat_id] if now - t <= window]

    def get_join_message_ids(self, chat_id: int, window: int) -> List[int]:
        now = time.time()
        return [m for t, m in self._join_message_ids[chat_id] if now - t <= window]

    def count_joins(self, chat_id: int) -> int:
        return len(self._join_times[chat_id])

    # --- локдаун ---
    def is_lockdown_active(self, chat_id: int) -> bool:
        u = self._lockdown_until.get(chat_id)
        return u is not None and datetime.now() < u

    def activate_lockdown(self, chat_id: int, seconds: int):
        self._lockdown_until[chat_id] = datetime.now() + timedelta(seconds=seconds)
        # сбрасываем флаг ручного снятия при новом локдауне
        self._manually_deactivated.discard(chat_id)

    def deactivate_lockdown(self, chat_id: int):
        """Ручное снятие локдауна — помечаем как снятое вручную."""
        self._lockdown_until[chat_id] = None
        self._manually_deactivated.add(chat_id)

    def deactivate_lockdown_auto(self, chat_id: int):
        """Автоматическое снятие по таймеру."""
        self._lockdown_until[chat_id] = None
        self._manually_deactivated.discard(chat_id)

    def clear_joins(self, chat_id: int):
        self._join_times[chat_id].clear()
        self._join_message_ids[chat_id].clear()
        self._raiders_in_wave[chat_id].clear()


storage = AntiRaidStorage()


# ============================================================
# Settings helpers
# ============================================================
def get_anti_raid_settings(chat_id: int) -> dict:
    cid_s = str(chat_id)
    cid_i = int(chat_id)
    anti = None
    if cid_s in settings and "anti_raid" in settings[cid_s]:
        anti = settings[cid_s]["anti_raid"]
    elif cid_i in settings and "anti_raid" in settings[cid_i]:
        anti = settings[cid_i]["anti_raid"]
    if anti is None:
        anti = {}
        changed = True
    else:
        anti = dict(anti)
        changed = False
    for k, v in DEFAULT_SETTINGS.items():
        if k not in anti:
            anti[k] = v
            changed = True
    if changed:
        settings.setdefault(cid_s, {})["anti_raid"] = anti
        save_settings(cid_s)
    return anti


def save_anti_raid_settings(chat_id: int, anti: dict):
    cid_s = str(chat_id)
    settings.setdefault(cid_s, {})["anti_raid"] = anti
    save_settings(cid_s)


# ============================================================
# Действия
# ============================================================
async def ban_user(bot_: Bot, chat_id: int, user_id: int, reason: str):
    try:
        await bot_.ban_chat_member(chat_id, user_id)
        storage._raiders_in_wave[chat_id].add(user_id)
        try:
            from ..storage.ai_context_events import log_chat_event
            log_chat_event(chat_id, f"АНТИРЕЙД БАН: id={user_id} — {reason}")
        except Exception:
            pass
        logger.info(f"[ANTI-RAID] ban {user_id}: {reason}")
    except TelegramAPIError as e:
        logger.error(f"[ANTI-RAID] ban {user_id} err: {e}")


async def restrict_chat_for_everyone(bot_: Bot, chat_id: int) -> bool:
    """Закрываем @everyone на запись (локдаун чата)."""
    try:
        try:
            chat = await bot_.get_chat(chat_id)
            cur = chat.permissions
            if cur:
                storage._saved_perms[chat_id] = {
                    "can_send_messages": cur.can_send_messages,
                    "can_send_media_messages": cur.can_send_media_messages,
                    "can_send_other_messages": cur.can_send_other_messages,
                    "can_add_web_page_previews": cur.can_add_web_page_previews,
                    "can_invite_users": cur.can_invite_users,
                    "can_change_info": cur.can_change_info,
                    "can_pin_messages": cur.can_pin_messages,
                }
        except Exception:
            pass

        await bot_.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_change_info=False,
            ),
        )
        return True
    except TelegramAPIError as e:
        logger.error(f"[ANTI-RAID] restrict_chat err: {e}")
        return False


async def unlock_chat(bot_: Bot, chat_id: int):
    perms = storage._saved_perms.get(chat_id)
    try:
        if perms:
            await bot_.set_chat_permissions(chat_id, ChatPermissions(**perms))
        else:
            await bot_.set_chat_permissions(
                chat_id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_invite_users=True,
                    can_pin_messages=False,
                    can_change_info=False,
                ),
            )
    except TelegramAPIError as e:
        logger.error(f"[ANTI-RAID] unlock_chat err: {e}")


async def notify_admins(bot_: Bot, chat_id: int, text: str):
    try:
        admins = await bot_.get_chat_administrators(chat_id)
        for adm in admins:
            if adm.user.id == bot_.id:
                continue
            try:
                await bot_.send_message(adm.user.id, text, parse_mode="HTML")
            except TelegramAPIError:
                pass
    except TelegramAPIError as e:
        logger.error(f"[ANTI-RAID] notify_admins err: {e}")


async def pin_raid_alert(bot_: Bot, chat_id: int, html_text: str):
    """Отправляем + закрепляем сообщение о рейде."""
    try:
        m = await bot_.send_message(chat_id, html_text, parse_mode="HTML",
                                    disable_web_page_preview=True)
        try:
            await bot_.pin_chat_message(chat_id, m.message_id, disable_notification=False)
            storage._pinned_alert[chat_id] = m.message_id
        except TelegramAPIError as e:
            logger.warning(f"[ANTI-RAID] pin err: {e}")
        return m
    except TelegramAPIError as e:
        logger.error(f"[ANTI-RAID] send_alert err: {e}")
        return None


async def _do_unpin_alert(chat_id: int):
    """Открепляем и удаляем сохранённый пин-алерт (если есть)."""
    pid = storage._pinned_alert.pop(chat_id, None)
    if pid:
        try:
            await bot.unpin_chat_message(chat_id, pid)
        except Exception as e:
            logger.warning(f"[ANTI-RAID] unpin err: {e}")


# ============================================================
# УМНЫЙ ДЕТЕКТ
# ============================================================
def detect_signals(chat_id: int, cfg: dict) -> Tuple[bool, str, List[int]]:
    """Возвращает (is_raid, reason_html, suspect_user_ids).

    Алгоритмы:
      1) MASS-JOIN: >= join_threshold за join_window сек.
      2) SAME-TAG: same_tag_threshold юзеров с одинаковым тегом в нике
         за join_window сек.
      3) SAME-MSG: same_msg_threshold одинаковых нормализованных сообщений
         от разных юзеров за msg_window сек.
      4) SAME-STICKER: same_sticker_threshold одинаковых стикеров от разных
         юзеров за msg_window сек.
    """
    storage.cleanup(chat_id, cfg["join_window"], cfg["msg_window"])

    # --- 1) MASS-JOIN ---
    joins = list(storage._join_times[chat_id])
    if len(joins) >= cfg["join_threshold"]:
        suspects = [u for _, u in joins]
        return True, (
            f"🚨 <b>МАССВХОД:</b> {len(joins)} входов за {cfg['join_window']}с "
            f"(порог {cfg['join_threshold']})"
        ), suspects

    # --- 2) SAME-TAG ---
    tag_to_users: Dict[str, Set[int]] = defaultdict(set)
    for _, uid, tags in storage._recent_user_tags[chat_id]:
        for t in tags:
            tag_to_users[t].add(uid)
    bad_tags = [(t, us) for t, us in tag_to_users.items()
                if len(us) >= cfg["same_tag_threshold"] and len(t) >= 3]
    if bad_tags:
        bad_tags.sort(key=lambda x: len(x[1]), reverse=True)
        tag, users = bad_tags[0]
        return True, (
            f"🚨 <b>Рейдерский тег в никах:</b> «<code>{html.escape(tag)}</code>» "
            f"у <b>{len(users)}</b> юзеров"
        ), list(users)

    # --- 3) SAME-MSG ---
    msg_counter: Dict[str, Set[int]] = defaultdict(set)
    for _, uid, txt in storage._recent_messages[chat_id]:
        if len(txt) >= 4:
            msg_counter[txt].add(uid)
    flood_msg = [(t, us) for t, us in msg_counter.items()
                 if len(us) >= cfg["same_msg_threshold"]]
    if flood_msg:
        flood_msg.sort(key=lambda x: len(x[1]), reverse=True)
        t, users = flood_msg[0]
        prev = (t[:30] + "…") if len(t) > 30 else t
        return True, (
            f"🚨 <b>СПАМ-ФЛУД:</b> «<code>{html.escape(prev)}</code>» "
            f"от <b>{len(users)}</b> юзеров"
        ), list(users)

    # --- 4) SAME-STICKER ---
    st_counter: Dict[str, Set[int]] = defaultdict(set)
    for _, uid, sid in storage._recent_stickers[chat_id]:
        st_counter[sid].add(uid)
    flood_st = [(s, us) for s, us in st_counter.items()
                if len(us) >= cfg["same_sticker_threshold"]]
    if flood_st:
        flood_st.sort(key=lambda x: len(x[1]), reverse=True)
        sid, users = flood_st[0]
        return True, (
            f"🚨 <b>СТИКЕР-ФЛУД:</b> один и тот же стикер от "
            f"<b>{len(users)}</b> юзеров"
        ), list(users)

    return False, "", []


# ============================================================
# ОБРАБОТКА РЕЙДА
# ============================================================
async def handle_raid(chat_id: int, cfg: dict, reason_html: str, suspects: List[int]):
    if storage.is_lockdown_active(chat_id):
        return

    duration = int(cfg.get("lockdown_duration", DEFAULT_SETTINGS["lockdown_duration"]))
    storage.activate_lockdown(chat_id, duration)
    logger.warning(f"[ANTI-RAID] 🚨 LOCKDOWN chat={chat_id} for {duration}s :: {reason_html}")

    # 1) баним массвходные ID
    banned = 0
    if cfg.get("ban_new_joins", True):
        for uid in set(suspects) | set(storage.get_recent_joins(chat_id, cfg["join_window"])):
            await ban_user(bot, chat_id, uid, "Рейд (анти-рейд авто)")
            banned += 1
            await asyncio.sleep(0.03)

    # 2) удаляем join-сервисные сообщения
    for mid in storage.get_join_message_ids(chat_id, cfg["join_window"]):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass

    # 3) закрываем чат на запись
    chat_restricted = False
    if cfg.get("restrict_chat", True):
        chat_restricted = await restrict_chat_for_everyone(bot, chat_id)

    # 4) детальный отчёт + ЗАКРЕП
    sample = ", ".join(f"<code>{u}</code>" for u in list(suspects)[:10])
    extra = f" (+{len(suspects)-10})" if len(suspects) > 10 else ""
    details = (
        "🛡 <b>АНТИ-РЕЙД АКТИВИРОВАН</b>\n\n"
        f"{reason_html}\n\n"
        f"⏱ Длительность локдауна: <b>{duration}с</b>\n"
        f"🔨 Забанено сейчас: <b>{banned}</b>\n"
        f"🔒 Чат закрыт на запись: {'✅' if chat_restricted else '❌'}\n"
        f"🚫 Бан входящих во время локдауна: "
        f"{'✅' if cfg.get('ban_during_lockdown', True) else '❌'}\n"
        f"👥 Подозрительные ID: {sample}{extra}\n\n"
        f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    if cfg.get("pin_alert", True):
        await pin_raid_alert(bot, chat_id, details)
    else:
        try:
            await bot.send_message(chat_id, details, parse_mode="HTML")
        except Exception:
            pass

    if cfg.get("notify_admins", True):
        await notify_admins(bot, chat_id, details)

    storage.clear_joins(chat_id)

    # планируем авто-разлок через duration
    asyncio.create_task(_lockdown_autoexpire(chat_id, duration))


async def _lockdown_autoexpire(chat_id: int, duration: int):
    await asyncio.sleep(duration + 1)

    # Если снято ВРУЧНУЮ — всё уже обработано в команде !антирейд снять, выходим
    if chat_id in storage._manually_deactivated:
        storage._manually_deactivated.discard(chat_id)
        return

    # Если каким-то образом снова активен (новый рейд запустился) — не трогаем
    if storage.is_lockdown_active(chat_id):
        return

    # Помечаем как автоматически снятый
    storage.deactivate_lockdown_auto(chat_id)

    cfg = get_anti_raid_settings(chat_id)
    if cfg.get("restrict_chat", True):
        await unlock_chat(bot, chat_id)

    # ОТКРЕПЛЯЕМ алерт — FIX: вызываем единую функцию открепа
    await _do_unpin_alert(chat_id)

    try:
        await bot.send_message(
            chat_id,
            "🟢 <b>Локдаун снят.</b> Чат снова открыт для участников.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ============================================================
# Обработка join
# ============================================================
async def _process_join(chat_id: int, user_id: int, username: str,
                       first_name: str, last_name: str = "",
                       message_id: Optional[int] = None):
    cfg = get_anti_raid_settings(chat_id)
    if not cfg.get("enabled", False):
        return

    # Бан во время локдауна
    if storage.is_lockdown_active(chat_id):
        if cfg.get("ban_during_lockdown", True):
            await ban_user(bot, chat_id, user_id, "Вход во время локдауна")
            if message_id:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
        return

    # Бан за теги (с нормализацией шрифтов!)
    if cfg.get("ban_for_tags", True):
        if has_forbidden_tags(username, first_name, last_name):
            await ban_user(bot, chat_id, user_id, "Запрещённый тег в нике")
            if message_id:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
            return

    added = storage.add_join(chat_id, user_id)
    if message_id:
        storage.add_join_message(chat_id, message_id)
    if added:
        storage.add_user_tags(chat_id, user_id,
                              extract_user_tags(username, first_name, last_name))

    storage.cleanup(chat_id, cfg["join_window"], cfg["msg_window"])

    is_raid, reason, suspects = detect_signals(chat_id, cfg)
    if is_raid:
        await handle_raid(chat_id, cfg, reason, suspects)


# ============================================================
# MIDDLEWARE — джойны + накопление сообщений/стикеров для детекта
# ============================================================
class AntiRaidJoinMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            if isinstance(event, ChatMemberUpdated):
                old = str(event.old_chat_member.status)
                new = str(event.new_chat_member.status)
                left = {"left", "kicked", "ChatMemberStatus.LEFT", "ChatMemberStatus.KICKED"}
                joined = {"member", "restricted", "ChatMemberStatus.MEMBER",
                          "ChatMemberStatus.RESTRICTED"}
                if old in left and new in joined:
                    u = event.new_chat_member.user
                    if not (u.is_bot and u.id == bot.id):
                        await _process_join(
                            event.chat.id, u.id, u.username or "",
                            u.first_name or "", u.last_name or "",
                            None,
                        )

            elif isinstance(event, Message):
                # сервисное "вошёл в чат"
                if event.new_chat_members:
                    for u in event.new_chat_members:
                        if u.is_bot and u.id == bot.id:
                            continue
                        await _process_join(
                            event.chat.id, u.id, u.username or "",
                            u.first_name or "", u.last_name or "",
                            event.message_id,
                        )
                # обычное сообщение — реакция на ссылки + детект спам/стикер флуда
                elif event.chat and event.chat.type in ("group", "supergroup"):
                    cfg = get_anti_raid_settings(event.chat.id)
                    if cfg.get("enabled", False) and event.from_user:
                        msg_text = event.text or event.caption or ""

                        # --- УДАЛЕНИЕ ССЫЛОК ---
                        if cfg.get("delete_links", True) and has_links(msg_text):
                            # проверяем: пользователь не-администратор
                            try:
                                member = await bot.get_chat_member(
                                    event.chat.id, event.from_user.id
                                )
                                if not isinstance(
                                    member, (ChatMemberAdministrator, ChatMemberOwner)
                                ):
                                    try:
                                        await bot.delete_message(
                                            event.chat.id, event.message_id
                                        )
                                    except Exception:
                                        pass
                                    return await handler(event, data)
                            except Exception:
                                pass

                        # накапливаем тексты/стикеры для умного детекта
                        if msg_text:
                            norm = normalize_text(msg_text)
                            norm = re.sub(r"\s+", " ", norm).strip()
                            storage.add_message(event.chat.id, event.from_user.id, norm)
                        if event.sticker:
                            sid = event.sticker.file_unique_id or event.sticker.file_id
                            storage.add_sticker(event.chat.id, event.from_user.id, sid)

                        # триггерим детект на каждое сообщение тоже
                        is_raid, reason, suspects = detect_signals(event.chat.id, cfg)
                        if is_raid:
                            await handle_raid(event.chat.id, cfg, reason, suspects)
        except Exception as e:
            logger.error(f"[ANTI-RAID middleware] err: {e}")

        return await handler(event, data)


anti_raid_middleware = AntiRaidJoinMiddleware()


# ============================================================
# КОМАНДА !антирейд
# ============================================================
@router.message(F.text & (F.text.startswith('!антирейд') | F.text.startswith('.антирейд')))
async def handle_anti_raid_command(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return await message.reply("❌ Только в группах.")

    chat_id = message.chat.id
    user_id = message.from_user.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if not isinstance(member, (ChatMemberAdministrator, ChatMemberOwner)):
            return await message.reply("❗ Только админ может настраивать анти-рейд.")
    except Exception:
        return await message.reply("❌ Ошибка проверки прав.")

    args = message.text.split()[1:]
    cfg = get_anti_raid_settings(chat_id)

    if not args:
        ldown = "🔒 АКТИВЕН" if storage.is_lockdown_active(chat_id) else "🔓 неактивен"
        text = (
            f"<b>🛡 Анти-рейд: {'✅ ВКЛ' if cfg['enabled'] else '❌ ВЫКЛ'}</b>\n"
            f"Локдаун: {ldown}\n\n"
            f"📊 Порог массвхода: <b>{cfg['join_threshold']}</b> входов за "
            f"<b>{cfg['join_window']}</b>с\n"
            f"🏷 Порог тегов: <b>{cfg['same_tag_threshold']}</b> юзеров с одним тегом\n"
            f"💬 Порог соо: <b>{cfg['same_msg_threshold']}</b> одинаковых сооб./"
            f"<b>{cfg['msg_window']}</b>с\n"
            f"🎨 Порог стикеров: <b>{cfg['same_sticker_threshold']}</b> "
            f"одинаковых стикеров/<b>{cfg['msg_window']}</b>с\n"
            f"⏰ Локдаун: <b>{cfg['lockdown_duration']}</b>с\n"
            f"🔒 Закрывать чат: {'✅' if cfg['restrict_chat'] else '❌'}\n"
            f"📌 Закреплять уведомление о рейде: {'✅' if cfg['pin_alert'] else '❌'}\n"
            f"🚫 Бан входов во время локдауна: "
            f"{'✅' if cfg['ban_during_lockdown'] else '❌'}\n"
            f"🏷 Бан за теги в нике: "
            f"{'✅' if cfg['ban_for_tags'] else '❌'}\n"
            f"🔗 Удалять ссылки от не-админов: "
            f"{'✅' if cfg['delete_links'] else '❌'}\n\n"
            f"<b>Команды:</b>\n"
            f"<code>!антирейд вкл|выкл</code>\n"
            f"<code>!антирейд порог &lt;N&gt; &lt;сек&gt;</code>\n"
            f"<code>!антирейд тег &lt;N&gt;</code>\n"
            f"<code>!антирейд флуд &lt;N&gt;</code>\n"
            f"<code>!антирейд стикер &lt;N&gt;</code>\n"
            f"<code>!антирейд окно &lt;сек&gt;</code>\n"
            f"<code>!антирейд локдаун &lt;сек&gt;</code>\n"
            f"<code>!антирейд закрепить вкл|выкл</code>\n"
            f"<code>!антирейд закрытьчат вкл|выкл</code>\n"
            f"<code>!антирейд теги вкл|выкл</code> — бан за теги в нике\n"
            f"<code>!антирейд ссылки вкл|выкл</code> — удалять ссылки\n"
            f"<code>!антирейд снять</code>\n"
            f"<code>!антирейд статус</code>"
        )
        return await message.reply(text, parse_mode="HTML")

    cmd = args[0].lower()

    if cmd == "вкл":
        cfg["enabled"] = True
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply("✅ Анти-рейд включён.")
    if cmd == "выкл":
        cfg["enabled"] = False
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply("❌ Анти-рейд выключен.")

    if cmd == "порог" and len(args) >= 3:
        try:
            th, win = int(args[1]), int(args[2])
            if th < 1 or win < 1:
                raise ValueError
            cfg["join_threshold"] = th
            cfg["join_window"] = win
            save_anti_raid_settings(chat_id, cfg)
            return await message.reply(f"✅ Порог: {th} входов за {win}с")
        except ValueError:
            return await message.reply("❌ Формат: <code>!антирейд порог 5 300</code>",
                                       parse_mode="HTML")

    if cmd == "тег" and len(args) >= 2 and args[1].isdigit():
        cfg["same_tag_threshold"] = max(2, int(args[1]))
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(f"✅ Same-tag порог: {cfg['same_tag_threshold']}")

    if cmd == "флуд" and len(args) >= 2 and args[1].isdigit():
        cfg["same_msg_threshold"] = max(2, int(args[1]))
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(f"✅ Same-msg порог: {cfg['same_msg_threshold']}")

    if cmd == "стикер" and len(args) >= 2 and args[1].isdigit():
        cfg["same_sticker_threshold"] = max(2, int(args[1]))
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(f"✅ Same-sticker порог: {cfg['same_sticker_threshold']}")

    if cmd == "окно" and len(args) >= 2 and args[1].isdigit():
        cfg["msg_window"] = max(10, int(args[1]))
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(f"✅ Окно для текстов/стикеров: {cfg['msg_window']}с")

    if cmd == "локдаун" and len(args) >= 2 and args[1].isdigit():
        cfg["lockdown_duration"] = max(10, int(args[1]))
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(f"✅ Локдаун: {cfg['lockdown_duration']}с")

    if cmd == "закрепить" and len(args) >= 2:
        cfg["pin_alert"] = args[1].lower() in ("вкл", "on", "true", "1")
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(f"✅ Закреп алерта: {'✅' if cfg['pin_alert'] else '❌'}")

    if cmd == "закрытьчат" and len(args) >= 2:
        cfg["restrict_chat"] = args[1].lower() in ("вкл", "on", "true", "1")
        save_anti_raid_settings(chat_id, cfg)
        return await message.reply(
            f"✅ Закрытие чата при рейде: {'✅' if cfg['restrict_chat'] else '❌'}"
        )

    # --- НОВАЯ КОМАНДА: бан за теги вкл/выкл ---
    if cmd == "теги" and len(args) >= 2:
        cfg["ban_for_tags"] = args[1].lower() in ("вкл", "on", "true", "1")
        save_anti_raid_settings(chat_id, cfg)
        state = "✅ включён" if cfg["ban_for_tags"] else "❌ выключен"
        return await message.reply(
            f"🏷 Бан за теги в нике: <b>{state}</b>", parse_mode="HTML"
        )

    # --- НОВАЯ КОМАНДА: удаление ссылок вкл/выкл ---
    if cmd == "ссылки" and len(args) >= 2:
        cfg["delete_links"] = args[1].lower() in ("вкл", "on", "true", "1")
        save_anti_raid_settings(chat_id, cfg)
        state = "✅ включено" if cfg["delete_links"] else "❌ выключено"
        return await message.reply(
            f"🔗 Удаление ссылок от не-админов: <b>{state}</b>", parse_mode="HTML"
        )

    # --- FIX: снятие локдауна с корректным откреплением ---
    if cmd == "снять":
        storage.deactivate_lockdown(chat_id)   # помечаем как снято вручную
        await unlock_chat(bot, chat_id)
        # открепляем алерт через единую функцию
        await _do_unpin_alert(chat_id)
        storage.clear_joins(chat_id)
        return await message.reply("🔓 Локдаун снят, чат открыт.")

    if cmd == "статус":
        return await message.reply(
            f"📊 Входов в окне: <b>{storage.count_joins(chat_id)}</b>/"
            f"{cfg['join_threshold']} (за {cfg['join_window']}с)\n"
            f"Локдаун: {'🔒 АКТИВЕН' if storage.is_lockdown_active(chat_id) else '🔓 нет'}\n"
            f"Анти-рейд: {'✅' if cfg['enabled'] else '❌'}\n"
            f"Бан за теги: {'✅' if cfg['ban_for_tags'] else '❌'}\n"
            f"Удалять ссылки: {'✅' if cfg['delete_links'] else '❌'}",
            parse_mode="HTML",
        )

    return await message.reply("❌ Неизвестная подкоманда. <code>!антирейд</code> для справки.",
                               parse_mode="HTML")
