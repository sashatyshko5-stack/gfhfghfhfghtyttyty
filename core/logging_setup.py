import logging
import os
import re
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
GROUP_LOG_DIR = os.path.join(LOG_DIR, "groups")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(GROUP_LOG_DIR, exist_ok=True)

FULL_LOG_PATH = os.path.join(LOG_DIR, "bot_full.log")
SHORT_LOG_PATH = "bot.log"
AI_LOG_PATH = os.path.join(LOG_DIR, "ai_requests.log")

# Кэш логгеров для каждой группы
_GROUP_LOGGERS: dict = {}


def _build_group_logger(chat_id) -> logging.Logger:
    """Отдельный лог-файл для каждой группы: logs/groups/group_<chat_id>.log"""
    name = f"bot.group.{chat_id}"
    if name in _GROUP_LOGGERS:
        return _GROUP_LOGGERS[name]
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if not lg.handlers:
        path = os.path.join(GROUP_LOG_DIR, f"group_{chat_id}.log")
        fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        lg.addHandler(fh)
    _GROUP_LOGGERS[name] = lg
    return lg


# ─── Шумные библиотеки — молчат ───────────────────────────────────────────
_NOISY = [
    "httpx", "httpcore", "aiohttp.access", "aiohttp.client",
    "asyncio", "g4f.debug", "g4f.requests", "g4f.Provider",
    "pyrogram.session", "pyrogram.connection", "pyrogram.crypto",
    "aiogram.event", "urllib3", "charset_normalizer",
    "hpack", "h2", "websockets",
]


def _suppress_noisy():
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.ERROR)


_GARBAGE_RE = re.compile(
    r"(SSL|TLS|handshake|TCP|socket|Event loop|future:|coroutine|"
    r"task exception|was never retrieved|CancelledError|ConnectionReset|"
    r"BrokenPipe|NoneType: None|Traceback \(most)",
    re.IGNORECASE,
)


def _build_short_logger() -> logging.Logger:
    lg = logging.getLogger("bot.short")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if lg.handlers:
        return lg
    fh = RotatingFileHandler(SHORT_LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | chat=%(chat_id)s | %(message)s",
        datefmt="%H:%M:%S"
    ))
    lg.addHandler(fh)
    return lg


def _build_full_logger() -> logging.Logger:
    lg = logging.getLogger("bot.full")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if lg.handlers:
        return lg
    fh = RotatingFileHandler(FULL_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-30s | chat=%(chat_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    lg.addHandler(fh)
    return lg


def _build_ai_logger() -> logging.Logger:
    lg = logging.getLogger("bot.ai")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if lg.handlers:
        return lg
    fh = RotatingFileHandler(AI_LOG_PATH, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | chat=%(chat_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    lg.addHandler(fh)
    return lg


_short = _build_short_logger()
_full = _build_full_logger()
_ai = _build_ai_logger()


class _RootShortHandler(logging.Handler):
    _re_chat = re.compile(r"(?:chat_id|chat|Чат)[=:\s]+(-?\d+)")
    _skip = frozenset({"bot.short", "bot.full", "bot.ai"})
    _skip_starts = (
        "httpx", "httpcore", "aiohttp", "asyncio", "g4f.debug",
        "g4f.requests", "pyrogram.session", "pyrogram.connection",
        "pyrogram.crypto", "aiogram.event", "urllib3", "charset_normalizer",
        "hpack", "h2", "websockets", "bot.group.",
    )

    def __init__(self):
        super().__init__(level=logging.DEBUG)

    def emit(self, record: logging.LogRecord):
        try:
            if record.name in self._skip:
                return
            if record.name.startswith(self._skip_starts):
                return
            if record.levelno < logging.INFO and not record.name.startswith("bot."):
                return

            msg = record.getMessage()
            if _GARBAGE_RE.search(msg):
                return

            short_msg = msg if len(msg) <= 300 else msg[:297] + "..."
            m = self._re_chat.search(msg)
            chat_id = m.group(1) if m else "-"
            mod = record.name.split(".")[-1]

            _short.info(f"{mod}: {short_msg}", extra={"chat_id": chat_id})
            _full.log(record.levelno, f"{record.name}: {msg[:800]}", extra={"chat_id": chat_id})
            # Зеркалим в per-group файл
            if chat_id != "-":
                try:
                    lg = _build_group_logger(chat_id)
                    lg.log(record.levelno, f"{mod}: {short_msg}")
                except Exception:
                    pass
        except Exception:
            pass


_root = logging.getLogger()
_root.setLevel(logging.DEBUG)

if not any(isinstance(h, _RootShortHandler) for h in _root.handlers):
    _root.addHandler(_RootShortHandler())
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    ))
    _root.addHandler(_ch)

_suppress_noisy()


# ─── Публичные функции ────────────────────────────────────────────────────
def _group_log(chat_id, level: str, message: str):
    """Пишет запись в отдельный файл для каждой группы (если chat_id валидный)."""
    try:
        if chat_id is None or chat_id == "-":
            return
        s = str(chat_id)
        if not (s.lstrip("-").isdigit()):
            return
        lg = _build_group_logger(s)
        fn = getattr(lg, level.lower(), lg.info)
        fn(message)
    except Exception:
        pass


def log_short(chat_id, message: str):
    msg = (message or "").strip().replace("\n", " ")[:300]
    extra = {"chat_id": chat_id if chat_id is not None else "-"}
    _short.info(msg, extra=extra)
    _full.info(msg, extra=extra)
    _group_log(chat_id, "info", msg)


def log_full(chat_id, level: str, message: str):
    fn = getattr(_full, level.lower(), _full.info)
    fn(message, extra={"chat_id": chat_id if chat_id is not None else "-"})
    _group_log(chat_id, level, message)


def log_ai_request(chat_id, provider: str, prompt, response: str, ok: bool):
    status = "OK" if ok else "FAIL"
    chat = chat_id if chat_id is not None else "-"
    extra = {"chat_id": chat}

    if isinstance(prompt, list):
        last_user = next(
            (m.get("content", "") for m in reversed(prompt) if m.get("role") == "user"),
            str(prompt)
        )
        prompt_str = last_user
    else:
        prompt_str = str(prompt or "")

    p_short = prompt_str.replace("\n", " ")[:200]
    r_short = (response or "").replace("\n", " ")[:300]

    _full.info(
        f"[AI {status}] {provider} | prompt={p_short!r} | resp={r_short!r}",
        extra=extra
    )

    _ai.info(
        f"[{status}] provider={provider}\n"
        f"  >> PROMPT ({len(prompt_str)} chars): {prompt_str[:1000]}\n"
        f"  << RESPONSE ({len(response or '')} chars): {(response or '')[:2000]}",
        extra=extra
    )


def log_user_message(chat_id, user_id, username: str, text: str):
    t = (text or "").replace("\n", " ")[:250]
    extra = {"chat_id": chat_id if chat_id is not None else "-"}
    _full.info(f"[USER] id={user_id} @{username} | {t!r}", extra=extra)
    _ai.info(f"[USER] id={user_id} @{username} | {t!r}", extra=extra)


__all__ = [
    "log_short", "log_full", "log_ai_request", "log_user_message",
    "FULL_LOG_PATH", "SHORT_LOG_PATH", "AI_LOG_PATH",
]