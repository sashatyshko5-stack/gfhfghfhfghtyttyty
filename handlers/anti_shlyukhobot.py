from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Optional

import aiohttp

from aiogram import Router, F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions,
)

from ..core.loader import bot
from ..core.utils import is_admin
from ..core.ai_client import ai_text, ai_text_json, ai_vision_bytes

logger = logging.getLogger(__name__)
router = Router()

_LOG_DIR = "logs"
_LOG_PATH = os.path.join(_LOG_DIR, "antishlyukhobot.log")
os.makedirs(_LOG_DIR, exist_ok=True)
_shlog = logging.getLogger("bot.antishlyukhobot")
_shlog.setLevel(logging.INFO)
_shlog.propagate = False
if not _shlog.handlers:
    _fh = RotatingFileHandler(_LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _shlog.addHandler(_fh)

def _slog(msg: str) -> None:
    _shlog.info(msg)

# ── JSON-ХРАНИЛИЩЕ НАСТРОЕК ───────────────────────────────────────────────────
_SETTINGS_FILE = os.path.join("settings", "antishlyukhobot.json")
_cfg_data: dict = {}   # ключи: str(chat_id) и "__global__"

def _load_cfg_file() -> None:
    global _cfg_data
    try:
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            _cfg_data = json.load(f)
        _slog(f"[настройки] Загружены из {_SETTINGS_FILE} ({len(_cfg_data)} чатов/ключей)")
    except FileNotFoundError:
        _cfg_data = {}
        _slog(f"[настройки] Файл {_SETTINGS_FILE} не найден, используем пустые настройки")
    except Exception as e:
        _cfg_data = {}
        logger.error(f"[АНТИШЛЮХОБОТ] Ошибка чтения настроек: {e}")

def _save_cfg_file() -> None:
    try:
        os.makedirs(os.path.dirname(_SETTINGS_FILE) or ".", exist_ok=True)
        tmp = _SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cfg_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SETTINGS_FILE)
    except Exception as e:
        logger.error(f"[АНТИШЛЮХОБОТ] Ошибка сохранения настроек: {e}")
        _slog(f"[настройки] ОШИБКА сохранения: {e}")

_load_cfg_file()

# ── RUNTIME-СОСТОЯНИЕ ─────────────────────────────────────────────────────────
KEYWORDS = ["работа","подработка","₽","$","знакомства","познакомиться","девушку","девушки","парня","ищу","работа", "порно", "Здесь есть настоящие ценители женского тела?","пошлые","сочные",]

_pending: dict[tuple[int, int], dict] = {}
_stats: dict[int, dict[str, int]] = {}
_whitelist: set[tuple[int, int]] = set()          # (chat_id, user_id)
_user_msgs: dict[tuple[int, int], list[int]] = {}  # (chat_id, user_id) -> [msg_id, ...]
_USER_MSGS_LIMIT = 50

def _inc(chat_id: int, key: str) -> None:
    bucket = _stats.setdefault(chat_id, {"detected": 0, "banned": 0, "passed": 0})
    bucket[key] = bucket.get(key, 0) + 1

# ── PER-CHAT НАСТРОЙКИ ────────────────────────────────────────────────────────

def _get_cfg(chat_id: int) -> dict:
    key = str(chat_id)
    if key not in _cfg_data:
        _cfg_data[key] = {"enabled": False, "test_mode": False}
    return _cfg_data[key]

def _is_enabled(chat_id: int) -> bool:
    return bool(_get_cfg(chat_id).get("enabled", False))

def _set_enabled(chat_id: int, value: bool) -> None:
    _get_cfg(chat_id)["enabled"] = value
    _save_cfg_file()

def _is_test_mode(chat_id: int) -> bool:
    return bool(_get_cfg(chat_id).get("test_mode", False))

def _set_test_mode(chat_id: int, value: bool) -> None:
    _get_cfg(chat_id)["test_mode"] = value
    _save_cfg_file()

def _apply_to_all_chats(enabled: Optional[bool] = None, test_mode: Optional[bool] = None) -> int:
    """Применяет enabled/test_mode ко всем известным чатам. Возвращает кол-во затронутых чатов."""
    count = 0
    for key in list(_cfg_data.keys()):
        if key == "__global__":
            continue
        cfg = _cfg_data[key]
        if enabled is not None:
            cfg["enabled"] = enabled
        if test_mode is not None:
            cfg["test_mode"] = test_mode
        count += 1
    _save_cfg_file()
    _slog(f"[глобал] применено к {count} чатам: enabled={enabled}, test_mode={test_mode}")
    return count

_SUPERADMIN_ID: int = 7273433468

def _is_superadmin(user_id: int) -> bool:
    return user_id == _SUPERADMIN_ID

def _is_whitelisted(chat_id: int, user_id: int) -> bool:
    if (chat_id, user_id) in _whitelist:
        return True
    wl = _get_cfg(chat_id).get("whitelist", [])
    if user_id in wl:
        _whitelist.add((chat_id, user_id))
        return True
    return False

def _add_to_whitelist(chat_id: int, user_id: int) -> None:
    _whitelist.add((chat_id, user_id))
    cfg = _get_cfg(chat_id)
    wl = cfg.setdefault("whitelist", [])
    if user_id not in wl:
        wl.append(user_id)
    _save_cfg_file()
    _slog(f"[вайтлист] user_id={user_id} добавлен в белый список (chat={chat_id})")


async def _download_file_id(file_id: str, label: str) -> Optional[bytes]:
    try:
        file = await bot.get_file(file_id)
        _slog(f"[аватарка] {label} get_file OK, path={file.file_path!r}")
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        data = buf.getvalue()
        _slog(f"[аватарка] {label} скачано {len(data)} байт")
        return data
    except Exception as e:
        _slog(f"[аватарка] {label} ошибка скачивания: {type(e).__name__}: {e}")
        return None


async def _get_avatar_bytes(user_id: int) -> Optional[bytes]:
    # ── Метод 1: getUserProfilePhotos (Bot API) ───────────────────────────────
    try:
        _slog(f"[аватарка] Метод 1: get_user_profile_photos({user_id})...")
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        _slog(f"[аватарка] Метод 1 вернул: total_count={photos.total_count}, наборов={len(photos.photos)}")
        if photos.total_count > 0 and photos.photos:
            file_id = photos.photos[0][-1].file_id
            data = await _download_file_id(file_id, "Метод1")
            if data:
                return data
            _slog("[аватарка] Метод 1: скачать не удалось — пробуем дальше")
        else:
            _slog("[аватарка] Метод 1: фото нет (приватность или аватарка не установлена)")
    except Exception as e:
        _slog(f"[аватарка] Метод 1: ИСКЛЮЧЕНИЕ {type(e).__name__}: {e}")

    # ── Метод 2: getChat (Bot API) ────────────────────────────────────────────
    try:
        _slog(f"[аватарка] Метод 2: get_chat({user_id})...")
        chat = await bot.get_chat(user_id)
        _slog(f"[аватарка] Метод 2 вернул: photo={'есть' if chat.photo else 'None'}")
        if chat.photo:
            data = await _download_file_id(chat.photo.big_file_id, "Метод2")
            if data:
                return data
            _slog("[аватарка] Метод 2: скачать не удалось — пробуем дальше")
        else:
            _slog("[аватарка] Метод 2: chat.photo=None (нет приватного диалога с ботом или фото скрыто)")
    except Exception as e:
        _slog(f"[аватарка] Метод 2: ИСКЛЮЧЕНИЕ {type(e).__name__}: {e}")

    # ── Метод 3: Telethon userbot (MTProto — видит личные фото) ──────────────
    try:
        _slog(f"[аватарка] Метод 3: get_avatar_via_userbot({user_id})...")
        from bottt import get_avatar_via_userbot
        data = await get_avatar_via_userbot(user_id)
        if data:
            _slog(f"[аватарка] Метод 3: получено {len(data)} байт через юзербот ✓")
            return data
        else:
            _slog("[аватарка] Метод 3: юзербот тоже не нашёл фото (личное фото видно только контактам)")
    except Exception as e:
        _slog(f"[аватарка] Метод 3: ИСКЛЮЧЕНИЕ {type(e).__name__}: {e}")

    _slog(f"[аватарка] Все методы не дали фото для user_id={user_id}")
    return None


_SYS_TEXT = (
    "Ты — фильтр спам-аккаунтов Telegram-групп. "
    "Отвечай ТОЛЬКО одним словом: «да» или «нет». "
    "«да» — если сообщение типично для спам-аккаунта: реклама интима, "
    "поиск клиентов на услуги, предложение сомнительных знакомств или работы. "
    "«нет» — если сообщение обычное."
)
_SYS_PROFILE = (
    "Ты — фильтр спам-аккаунтов Telegram.\n"
    "Отвечай ТОЛЬКО одним словом: «да» или «нет». Никаких пояснений.\n\n"

    "«да» — если аккаунт похож на шлюхобота. Признаки:\n"
    "  1. ИМЯ: отображаемое имя выглядит как РУССКОЕ женское или мужское имя,\n"
    "     либо РУССКОЕ имя + РУССКАЯ фамилия.\n"
    "     Примеры русских имён: Кристина, Анастасия, Марина, Валерия, Дарья,\n"
    "     Алина, Екатерина, Диана, Ирина, Юлия, Виктория, Наталья.\n"
    "     Примеры подозрительных комбо: «Кристина Петрова», «Марина Иванова».\n"
    "  2. АВАТАРКА: портрет реальной девушки или молодого человека\n"
    "     в соблазнительном/постановочном стиле (типичное эскорт-фото).\n\n"

    "«нет» — ОБЯЗАТЕЛЬНО отвечай «нет» если:\n"
    "  - Username или имя написаны ЛАТИНИЦЕЙ и не имитируют русское имя\n"
    "    (примеры безопасных: @user123, @john_doe, @xXx_gamer, @cool_guy, @player_777,\n"
    "     Mike, Alex, John, David, любые латинские прозвища/никнеймы).\n"
    "  - Имя явно техническое, игровое, абстрактное или не похоже на ФИО.\n"
    "  - На аватарке нет портрета (картинка, мем, животное, аниме, логотип и т.п.).\n"
    "  - Аккаунт выглядит как обычный пользователь.\n\n"

    "Про аватарку:\n"
    "  - Если аватарка ПРИКРЕПЛЕНА — анализируй изображение.\n"
    "  - Если аватарка НЕДОСТУПНА — решай ТОЛЬКО по имени и нику.\n\n"

    "Главное правило: латинский/английский ник или имя — НЕ является признаком шлюхобота."
)
_SYS_VISION = (
    "Ты — фильтр спам-аккаунтов Telegram. Анализируй аватарку и данные профиля.\n\n"

    "Признаки шлюхобота:\n"
    "  1. ИМЯ: русское женское/мужское имя или имя+фамилия (Кристина, Марина, Дарья и т.п.).\n"
    "  2. АВАТАРКА: портрет реальной девушки/парня в соблазнительном или постановочном стиле.\n\n"

    "НЕ шлюхобот если имя/ник написаны латиницей, абстрактные, игровые, или на аватарке нет человека.\n\n"

    "Отвечай СТРОГО в формате (две строки, без лишнего текста):\n"
    "вердикт: да\n"
    "причина: <опиши что именно видно на аватарке (пол, стиль фото, одежда) и что в имени указывает на шлюхобота>\n\n"
    "или:\n"
    "вердикт: нет\n"
    "причина: <опиши что видно на аватарке (пол, стиль, объект) и почему имя/ник не соответствуют критериям>"
)
_SYS_TEST = (
    "Ты — детектор спам-аккаунтов Telegram в режиме диагностики. "
    "Проанализируй сообщение и данные пользователя. "
    "Верни ТОЛЬКО валидный JSON без markdown и пояснений:\n"
    '{"verdict":"шлюхобот"|"чист","confidence":<0-100>,"reason":"<1 предложение>"}'
)


async def _ai_check_text(chat_id: int, text: str) -> bool:
    try:
        _slog(
            f"[текст→ИИ] Отправляем в ИИ (chat={chat_id}), длина={len(text)} симв.\n"
            f"  Текст: {text[:800]!r}"
        )
        answer = await ai_text(chat_id, text[:800], system=_SYS_TEXT)
        if not answer:
            _slog(f"[текст→ИИ] ИИ вернул пустой ответ (chat={chat_id})")
            return False
        raw = answer.strip()
        result = raw.lower().startswith("да")
        _slog(
            f"[текст→ИИ] Сырой ответ ИИ: {raw!r}\n"
            f"  Вывод: {'🚨 ПОДОЗРИТЕЛЬНЫЙ' if result else '✅ чистый'} (chat={chat_id})"
        )
        return result
    except Exception as e:
        _slog(f"[текст→ИИ] ИСКЛЮЧЕНИЕ: {type(e).__name__}: {e} (chat={chat_id})")
        logger.error(f"[АНТИШЛЮХОБОТ] ai_text ошибка: {e}", exc_info=True)
        return False


def _build_profile_prompt(username: str, full_name: str, has_avatar: bool) -> str:
    username_line = f"@{username}" if username else "(username не задан)"
    name_line = full_name if full_name else "(имя не задано)"

    has_cyrillic_name = any("\u0400" <= ch <= "\u04FF" for ch in full_name)
    has_cyrillic_nick = any("\u0400" <= ch <= "\u04FF" for ch in (username or ""))
    lang_hint = []
    if has_cyrillic_name:
        lang_hint.append("имя написано кириллицей (русское)")
    else:
        lang_hint.append("имя написано латиницей (не русское)")
    if username:
        if has_cyrillic_nick:
            lang_hint.append("ник кириллический")
        else:
            lang_hint.append("ник латинский")
    hint_str = "; ".join(lang_hint)

    avatar_str = (
        "Аватарка ПРИКРЕПЛЕНА — проанализируй изображение выше."
        if has_avatar else
        "Аватарка НЕДОСТУПНА (скрыта или не установлена) — анализируй только по имени и нику."
    )

    return (
        f"Данные аккаунта:\n"
        f"• Username (ник): {username_line}\n"
        f"• Отображаемое имя: {name_line}\n"
        f"• Язык: {hint_str}\n"
        f"• Аватарка: {avatar_str}\n\n"
        f"Является ли этот аккаунт шлюхоботом? Ответь ОДНИМ словом: «да» или «нет»."
    )


_GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def _parse_vision_response(raw: str) -> tuple[str, str]:
    """Парсит ответ вида 'вердикт: да\\nпричина: ...' → (вердикт, причина).
    Если формат не распознан — возвращает (raw, '')."""
    verdict = ""
    reason = ""
    for line in raw.splitlines():
        line = line.strip().lower()
        if line.startswith("вердикт:"):
            verdict = line.split(":", 1)[1].strip()
        elif line.startswith("причина:"):
            reason = line.split(":", 1)[1].strip()
    if not verdict:
        verdict = raw.strip().lower()
    return verdict, reason


async def _groq_vision_check(
    api_key: str,
    avatar_bytes: bytes,
    prompt: str,
    max_retries: int = 3,
) -> Optional[str]:
    b64 = base64.b64encode(avatar_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    messages = [
        {"role": "system", "content": _SYS_VISION},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            ],
        },
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _GROQ_VISION_MODEL,
        "messages": messages,
        "max_tokens": 150,
        "temperature": 0.0,
    }

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _GROQ_API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("retry-after", "5"))
                        retry_after = min(max(retry_after, 2), 30)
                        _slog(
                            f"Groq: лимит запросов, ждём {retry_after:.1f} сек "
                            f"(попытка {attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    body = await resp.text()
                    _slog(f"Groq вернул неожиданный статус {resp.status}: {body[:200]}")
                    return None

        except asyncio.TimeoutError:
            _slog(f"Groq: таймаут при ожидании ответа (попытка {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
        except Exception as e:
            _slog(f"Groq: неожиданная ошибка — {e}")
            return None

    _slog(f"Groq: все {max_retries} попытки исчерпаны, ответа нет")
    return None


def _log_vision_raw(label: str, raw: str, chat_id: int) -> tuple[str, str]:
    """Логирует сырой ответ vision ИИ, парсит вердикт+причину, возвращает (вердикт, причина)."""
    raw_stripped = raw.strip()
    _slog(f"[vision сырой ответ | {label}] (chat={chat_id}):\n---\n{raw_stripped}\n---")
    verdict, reason = _parse_vision_response(raw_stripped)
    _slog(f"[vision парсинг | {label}] вердикт={verdict!r}, причина={reason!r} (chat={chat_id})")
    return verdict, reason


async def _ai_check_profile(chat_id: int, username: str, full_name: str, avatar_bytes: Optional[bytes]) -> bool:
    from ..core.global_groq import get_global_groq_key

    display_name = f"@{username}" if username else full_name or "?"
    prompt = _build_profile_prompt(username, full_name, has_avatar=bool(avatar_bytes))

    _slog(
        f"[профиль] Начинаем проверку {display_name!r} "
        f"(аватарка: {'есть, {} байт'.format(len(avatar_bytes)) if avatar_bytes else 'нет'}) "
        f"(chat={chat_id})"
    )
    _slog(f"[профиль] Промпт для ИИ (chat={chat_id}):\n{prompt}")

    try:
        raw_answer: Optional[str] = None
        verdict_str: str = ""
        reason_str: str = ""
        groq_key = get_global_groq_key()
        provider_used = "—"

        # ── Groq Vision (основной) ─────────────────────────────────────────────
        if groq_key and avatar_bytes:
            _slog(
                f"[профиль] Шаг 1: Groq Vision ({_GROQ_VISION_MODEL}), "
                f"аватарка {len(avatar_bytes)} байт (chat={chat_id})..."
            )
            raw_answer = await _groq_vision_check(groq_key, avatar_bytes, prompt)
            if raw_answer:
                verdict_str, reason_str = _log_vision_raw("Groq Vision", raw_answer, chat_id)
                provider_used = "Groq Vision"
            else:
                _slog(f"[профиль] Groq Vision не ответил (chat={chat_id})")
        else:
            if not groq_key:
                _slog(f"[профиль] Шаг 1: Groq Vision пропущен — ключ не задан (chat={chat_id})")
            if not avatar_bytes:
                _slog(f"[профиль] Шаг 1: Groq Vision пропущен — нет аватарки (chat={chat_id})")

        # ── Резервный vision ИИ ────────────────────────────────────────────────
        if raw_answer is None and avatar_bytes:
            _slog(f"[профиль] Шаг 2: резервный vision ИИ (chat={chat_id})...")
            raw_answer = await ai_vision_bytes(
                chat_id, avatar_bytes, prompt,
                system=_SYS_VISION,
                max_tokens=150,
            )
            if raw_answer:
                verdict_str, reason_str = _log_vision_raw("резервный vision ИИ", raw_answer, chat_id)
                provider_used = "резервный vision ИИ"
            else:
                _slog(f"[профиль] Резервный vision ИИ не ответил (chat={chat_id})")

        # ── Текстовый ИИ (аватарки нет / оба vision недоступны) ───────────────
        if raw_answer is None:
            _slog(f"[профиль] Шаг 3: текстовый ИИ (vision недоступен или аватарки нет) (chat={chat_id})...")
            raw_answer = await ai_text(chat_id, prompt, system=_SYS_PROFILE, max_tokens=20)
            if raw_answer:
                _slog(
                    f"[профиль] Текстовый ИИ ответил: «{raw_answer.strip()}» (chat={chat_id})"
                )
                verdict_str = raw_answer.strip().lower()
                provider_used = "текстовый ИИ"
            else:
                _slog(f"[профиль] Текстовый ИИ не ответил (chat={chat_id})")

        if not raw_answer:
            _slog(f"[профиль] ВСЕ методы не дали ответа — считаем чистым (chat={chat_id})")
            return False

        result = verdict_str.startswith("да")
        _slog(
            f"[профиль] ═══════════════════════════════════════════════\n"
            f"  Аккаунт  : {display_name!r}\n"
            f"  Провайдер: {provider_used}\n"
            f"  Вердикт  : {'🚨 ШЛЮХОБОТ' if result else '✅ чист'} ({verdict_str!r})\n"
            f"  Причина  : {reason_str or '(не указана)'}\n"
            f"  chat     : {chat_id}\n"
            f"═══════════════════════════════════════════════════════"
        )
        return result

    except Exception as e:
        _slog(f"[профиль] ИСКЛЮЧЕНИЕ при проверке {display_name!r}: {type(e).__name__}: {e} (chat={chat_id})")
        logger.error(f"[АНТИШЛЮХОБОТ] ai_profile ошибка: {e}", exc_info=True)
        return False


async def _ai_ping(chat_id: int) -> str:
    try:
        answer = await ai_text(chat_id, "Ответь одним словом: работаю", system="Ты тест.")
        if answer is None:
            return "ИИ вернул None"
        if answer == "":
            return "ИИ вернул пустую строку"
        return answer.strip()[:200]
    except Exception as e:
        return f"{type(e).__name__}: {e}"


async def _ai_test_analyze(chat_id: int, text: str, username: str, full_name: str) -> dict:
    prompt = (
        f"Сообщение: «{text[:500]}»\n"
        f"Ник: «{username or '(нет)'}»\n"
        f"Имя: «{full_name}»"
    )
    raw = None
    try:
        _slog(f"Тест: отправляем сообщение в ИИ (chat={chat_id})")
        raw = await ai_text(chat_id, prompt, system=_SYS_TEST, max_tokens=200)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _slog(f"Тест: ошибка при запросе к ИИ — {err} (chat={chat_id})")
        return {"verdict": "", "confidence": 0, "reason": "", "no_response": True, "error": err}

    _slog(f"Тест: сырой ответ ИИ — {raw!r} (chat={chat_id})")

    if not raw:
        _slog(f"Тест: ИИ вернул пустой ответ (chat={chat_id})")
        return {"verdict": "", "confidence": 0, "reason": "", "no_response": True, "error": "пустой ответ"}

    json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
    if not json_match:
        _slog(f"Тест: в ответе ИИ нет JSON — полный ответ: {raw[:200]!r} (chat={chat_id})")
        return {"verdict": "", "confidence": 0, "reason": "", "no_response": True, "error": f"нет JSON: {raw[:100]}"}

    try:
        parsed = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        _slog(f"Тест: не удалось разобрать JSON — {e}. Строка: {json_match.group()!r} (chat={chat_id})")
        return {"verdict": "", "confidence": 0, "reason": "", "no_response": True, "error": str(e)}

    _slog(f"Тест: ИИ вернул — вердикт={parsed.get('verdict')!r}, уверенность={parsed.get('confidence')}%, причина={parsed.get('reason')!r} (chat={chat_id})")
    return {
        "verdict":     parsed.get("verdict", ""),
        "confidence":  int(parsed.get("confidence", 0)),
        "reason":      parsed.get("reason", ""),
        "no_response": False,
        "error":       None,
    }


async def _mute(chat_id: int, user_id: int) -> None:
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False))
    except Exception as e:
        logger.error(f"[АНТИШЛЮХОБОТ] Мут: {e}")

async def _unmute(chat_id: int, user_id: int) -> None:
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(
            can_send_messages=True, can_send_media_messages=True,
            can_send_other_messages=True, can_add_web_page_previews=True,
        ))
    except Exception as e:
        logger.error(f"[АНТИШЛЮХОБОТ] Размут: {e}")

async def _ban(chat_id: int, user_id: int) -> None:
    try:
        await bot.ban_chat_member(chat_id, user_id)
    except Exception as e:
        logger.error(f"[АНТИШЛЮХОБОТ] Бан: {e}")

def _captcha_kb(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я не бот — нажми здесь", callback_data=f"shlyukha_captcha:{user_id}:{chat_id}")
    ]])


async def _ban_after_timeout(chat_id: int, user_id: int, msg_id: int, display: str, timeout: int = 60) -> None:
    await asyncio.sleep(timeout)
    if (chat_id, user_id) not in _pending:
        return
    _pending.pop((chat_id, user_id), None)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass
    await _ban(chat_id, user_id)
    _inc(chat_id, "banned")
    _slog(f"Забанен {display} — не прошёл капчу за {timeout} сек (chat={chat_id}, user_id={user_id})")
    try:
        await bot.send_message(chat_id, f"🚫 <b>{display}</b> забанен — капча не пройдена за {timeout} сек.\n#антишлюхобот")
    except Exception:
        pass


async def _process_suspect(message: Message) -> None:
    user = message.from_user
    chat_id = message.chat.id
    text = message.text or message.caption or ""
    username = user.username or ""
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    display = f"@{username}" if username else full_name or str(user.id)

    triggered_kw = [kw for kw in KEYWORDS if kw in text.lower()]

    _slog(
        f"\n{'═'*55}\n"
        f"🔍 НОВЫЙ ПОДОЗРЕВАЕМЫЙ\n"
        f"  chat_id   : {chat_id}\n"
        f"  user_id   : {user.id}\n"
        f"  username  : @{username or '—'}\n"
        f"  full_name : {full_name or '—'}\n"
        f"  msg_id    : {message.message_id}\n"
        f"  ключ.слова: {triggered_kw}\n"
        f"  текст     : {text!r}\n"
        f"{'═'*55}"
    )

    if not await _ai_check_text(chat_id, text):
        _slog(f"[итог] ✅ ТЕКСТ ЧИСТ — пропускаем {display} (user_id={user.id}, chat={chat_id})")
        return

    _slog(f"[шаг 2] Текст подозрительный — проверяем профиль {display} (user_id={user.id}, chat={chat_id})")
    _slog(f"[шаг 2] Загружаем аватарку user_id={user.id}...")
    avatar = await _get_avatar_bytes(user.id)
    _slog(f"[шаг 2] Аватарка: {'загружена, {} байт'.format(len(avatar)) if avatar else 'недоступна'} (user_id={user.id})")

    if not await _ai_check_profile(chat_id, username, full_name, avatar):
        _slog(f"[итог] ✅ ПРОФИЛЬ ЧИСТ — пропускаем {display} (user_id={user.id}, chat={chat_id})")
        return

    _slog(
        f"\n{'!'*55}\n"
        f"🚨 ШЛЮХОБОТ ПОДТВЕРЖДЁН\n"
        f"  user: {display} (user_id={user.id}, chat={chat_id})\n"
        f"{'!'*55}"
    )
    _inc(chat_id, "detected")
    await _mute(chat_id, user.id)
    _slog(f"[действие] Мут применён к {display} (user_id={user.id}, chat={chat_id})")

    key = (chat_id, user.id)
    msg_ids = list(_user_msgs.pop(key, []))
    if message.message_id not in msg_ids:
        msg_ids.append(message.message_id)
    _slog(f"[действие] Удаляем {len(msg_ids)} сообщений от {display}: {msg_ids}")
    for i in range(0, len(msg_ids), 100):
        chunk = msg_ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
        except Exception:
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass

    captcha_msg = await message.answer(
        f"🤖 <b>Обнаружен возможный спам-аккаунт:</b> {display}\n\n"
        f"Если ты реальный человек — нажми кнопку ниже в течение <b>60 секунд</b>.\n"
        f"Иначе — бан. #антишлюхобот",
        reply_markup=_captcha_kb(user.id, chat_id),
    )
    _slog(f"[действие] Капча отправлена для {display} (user_id={user.id}), msg_id={captcha_msg.message_id} (chat={chat_id})")

    if key in _pending:
        _pending[key]["task"].cancel()
    task = asyncio.create_task(_ban_after_timeout(chat_id, user.id, captcha_msg.message_id, display))
    _pending[key] = {"task": task, "msg_id": captcha_msg.message_id}


async def _process_test(message: Message) -> None:
    """Тест-режим: анализирует ИИ, результат ТОЛЬКО в лог."""
    chat_id = message.chat.id
    user = message.from_user
    text = message.text or message.caption or ""
    if not text.strip():
        return

    username = user.username or ""
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    display = f"@{username}" if username else full_name or str(user.id)

    _slog(f"Тест: анализируем сообщение от {display} (chat={chat_id}): {text[:120]!r}")

    try:
        result = await _ai_test_analyze(chat_id, text, username, full_name)
    except Exception as e:
        _slog(f"Тест: ошибка при анализе сообщения от {display} — {type(e).__name__}: {e} (chat={chat_id})")
        logger.error(f"[АНТИШЛЮХОБОТ ТЕСТ] Ошибка: {e}", exc_info=True)
        return

    if result.get("no_response"):
        err_detail = result.get("error") or "неизвестно"
        _slog(f"Тест: ИИ не ответил для {display} — причина: {err_detail} (chat={chat_id})")
        return

    verdict    = result["verdict"]
    confidence = result["confidence"]
    reason     = result["reason"]

    label = "🚨 ШЛЮХОБОТ" if verdict == "шлюхобот" else "⚠️ ПОДОЗРИТЕЛЬНО" if confidence >= 40 else "✅ ЧИСТ"
    bar = "█" * round(confidence / 10) + "░" * (10 - round(confidence / 10))

    _slog(
        f"Тест: результат для {display} — {label} [{bar}] {confidence}%\n"
        f"  Причина: {reason} (chat={chat_id})"
    )


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _mode_label(chat_id: int) -> str:
    cfg = _get_cfg(chat_id)
    if cfg.get("test_mode"):
        return "🔬 ТЕСТ"
    if cfg.get("enabled"):
        return "✅ Включен"
    return "❌ выключен"


def _status_text(chat_id: int) -> str:
    s        = _stats.get(chat_id, {"detected": 0, "banned": 0, "passed": 0})
    cfg      = _get_cfg(chat_id)
    wl_count = len(cfg.get("whitelist", []))
    mode     = _mode_label(chat_id)
    sa_hint  = (
        f"  <code>!глобал_антишлюхобот вкл/выкл/тест вкл/тест выкл</code>\n"
        f"  <i>(только для суперадмина, id: <code>{_SUPERADMIN_ID}</code>)</i>"
        if _SUPERADMIN_ID else
        "  <i>⚠️ SUPERADMIN_ID не задан — глобальные команды недоступны</i>"
    )

    return (
        f"🛡 <b>Антишлюхобот</b> — <b>{mode}</b>\n"
        f"<code>chat_id: {chat_id}</code>\n\n"
        f"<b>📊 Статистика чата:</b>\n"
        f"  ├ Обнаружено:    <b>{s['detected']}</b>\n"
        f"  ├ Прошли капчу:  <b>{s['passed']}</b>\n"
        f"  ├ Забанено:      <b>{s['banned']}</b>\n"
        f"  └ Вайтлист:      <b>{wl_count}</b> чел.\n\n"
        f"<b>📋 Команды этого чата:</b>\n"
        f"  <code>!антишлюхобот вкл</code>       — включить боевой режим\n"
        f"  <code>!антишлюхобот выкл</code>      — выключить\n"
        f"  <code>!антишлюхобот тест вкл</code>  — тест-режим (только лог)\n"
        f"  <code>!антишлюхобот тест выкл</code> — выключить тест\n"
        f"  <code>!антишлюхобот пинг</code>      — проверить ИИ\n"
     
        
    )


# ── КОМАНДЫ ───────────────────────────────────────────────────────────────────

@router.message(F.chat.type.in_({"group", "supergroup"}), F.text.startswith("!антишлюхобот"))
async def cmd_dispatch(message: Message) -> None:
    if not message.from_user or not await is_admin(message):
        return

    raw = (message.text or "").strip()
    arg = raw[len("!антишлюхобот"):].strip().lower()
    chat_id = message.chat.id
    _slog(f"Команда от пользователя {message.from_user.id}: «{arg}» (chat={chat_id})")

    if arg in ("вкл", "включить", "on"):
        from ..storage.premium import has_premium, has_chat_premium, register_premium_chat, get_chat_limit
        uid = message.from_user.id
        user_prem = has_premium(uid)
        chat_prem = has_chat_premium(chat_id)
        if not user_prem and not chat_prem:
            return await message.answer(
                "🔒 <b>Антишлюхобот</b> — премиум-функция.\n\n"
                "Для использования необходима подписка.\n"
                "• Личный премиум: напишите <code>!премиум</code> боту в ЛС\n"
                "• Чат-премиум: введите <code>!чат_премиум</code> здесь",
                parse_mode="HTML",
            )
        if user_prem and not chat_prem and not register_premium_chat(uid, chat_id):
            limit = get_chat_limit(uid)
            return await message.answer(
                f"🔒 Достигнут лимит премиум-чатов (<b>{limit}</b>).\n"
                "Отключите антишлюхобот в другом чате, чтобы освободить место.",
                parse_mode="HTML",
            )
        _set_enabled(chat_id, True)
        _set_test_mode(chat_id, False)
        _slog(f"Включён боевой режим (chat={chat_id})")
        await message.answer(
            "✅ <b>Антишлюхобот включён</b> (боевой режим).\n"
            "",
            parse_mode="HTML",
        )

    elif arg in ("выкл", "выключить", "off"):
        _set_enabled(chat_id, False)
        _set_test_mode(chat_id, False)
        _slog(f"Выключен (chat={chat_id})")
        await message.answer(
            "❌ <b>Антишлюхобот выключен.</b>\n"
            "<i>Настройки сохранены в JSON.</i>",
            parse_mode="HTML",
        )

    elif arg in ("тест вкл", "тест включить", "test on"):
        _set_test_mode(chat_id, True)
        _slog(f"Тест-режим включён (chat={chat_id})")
        await message.answer(
            "🔬 <b>Тест-режим включён.</b>\n\n"
            "ИИ анализирует каждое сообщение — результат пишется только в лог.\n"
            "Чат не трогается. Бота и мутов нет.\n\n"
            "<i>Выключить: <code>!антишлюхобот тест выкл</code></i>",
            parse_mode="HTML",
        )

    elif arg in ("тест выкл", "тест выключить", "test off"):
        _set_test_mode(chat_id, False)
        _slog(f"Тест-режим выключен (chat={chat_id})")
        await message.answer(
            "🔬 <b>Тест-режим выключен.</b>",
            parse_mode="HTML",
        )

    elif arg in ("пинг", "ping", "проверка"):
        wait_msg = await message.answer("⏳ Проверяю ИИ...")
        _slog(f"Запускаем пинг ИИ (chat={chat_id})")
        ping_result = await _ai_ping(chat_id)
        _slog(f"Пинг завершён, ответ ИИ: {ping_result!r} (chat={chat_id})")
        await wait_msg.edit_text(
            f"🤖 <b>Диагностика ИИ | Антишлюхобот</b>\n\n"
            f"<b>Сырой ответ:</b>\n<code>{ping_result}</code>",
            parse_mode="HTML",
        )

    elif arg in ("настройки", "json", "конфиг"):
        cfg = _get_cfg(chat_id)
        dump = json.dumps({str(chat_id): cfg}, ensure_ascii=False, indent=2)
        await message.answer(
            f"⚙️ <b>Настройки чата {chat_id}:</b>\n<pre>{dump}</pre>",
            parse_mode="HTML",
        )

    else:
        await message.answer(_status_text(chat_id), parse_mode="HTML")


@router.message(F.text.startswith("!глобал_антишлюхобот"))
async def cmd_global(message: Message) -> None:
    if not message.from_user:
        return

    uid = message.from_user.id
    if not _is_superadmin(uid):
        if _SUPERADMIN_ID is None:
            await message.answer(
                "⛔ <b>Команда недоступна.</b>\n"
                "Задайте переменную окружения <code>SUPERADMIN_ID</code> со своим Telegram ID.",
                parse_mode="HTML",
            )
        return

    raw = (message.text or "").strip()
    arg = raw[len("!глобал_антишлюхобот"):].strip().lower()
    _slog(f"[глобал] Суперадмин {uid}: «{arg}»")

    if arg in ("вкл", "включить", "on"):
        n = _apply_to_all_chats(enabled=True, test_mode=False)
        await message.answer(
            f"🌐 <b>Антишлюхобот ВКЛЮЧЁН</b> в {n} чатах.\n"
            f"<i>Настройка записана в каждый чат — <code>!антишлюхобот</code> по-прежнему работает.</i>",
            parse_mode="HTML",
        )

    elif arg in ("выкл", "выключить", "off"):
        n = _apply_to_all_chats(enabled=False, test_mode=False)
        await message.answer(
            f"🌐 <b>Антишлюхобот ВЫКЛЮЧЕН</b> в {n} чатах.\n"
            f"<i>Настройка записана в каждый чат — включить обратно можно командой <code>!антишлюхобот вкл</code>.</i>",
            parse_mode="HTML",
        )

    elif arg in ("тест вкл", "тест включить", "test on"):
        n = _apply_to_all_chats(test_mode=True)
        await message.answer(
            f"🔬 <b>Тест-режим включён</b> в {n} чатах.\n"
            f"ИИ анализирует, результаты — только в лог. Чаты чистые.\n"
            f"<i>Выключить: <code>!глобал_антишлюхобот тест выкл</code></i>",
            parse_mode="HTML",
        )

    elif arg in ("тест выкл", "тест выключить", "test off"):
        n = _apply_to_all_chats(test_mode=False)
        await message.answer(
            f"🔬 <b>Тест-режим выключен</b> в {n} чатах.\n"
            f"<i>Каждый чат работает по своим настройкам enabled.</i>",
            parse_mode="HTML",
        )

    else:
        chats_count   = len(_cfg_data)
        enabled_count = sum(1 for v in _cfg_data.values() if isinstance(v, dict) and v.get("enabled"))
        test_count    = sum(1 for v in _cfg_data.values() if isinstance(v, dict) and v.get("test_mode"))
        await message.answer(
            f"🌐 <b>Глобальный антишлюхобот</b>\n\n"
            f"<b>Чатов в базе:</b> {chats_count}\n"
            f"<b>Включён в:</b> {enabled_count} чатах\n"
            f"<b>Тест-режим в:</b> {test_count} чатах",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("shlyukha_captcha:"))
async def on_captcha_press(callback: CallbackQuery) -> None:
    _, uid_str, cid_str = callback.data.split(":", 2)
    target_uid, chat_id = int(uid_str), int(cid_str)

    if callback.from_user.id != target_uid:
        _slog(f"Чужая капча: нажал {callback.from_user.id}, а капча для {target_uid} (chat={chat_id})")
        await callback.answer("Это не твоя капча!", show_alert=True)
        return

    entry = _pending.pop((chat_id, target_uid), None)
    if entry:
        entry["task"].cancel()
        try:
            await bot.delete_message(chat_id, entry["msg_id"])
        except Exception:
            pass

    await _unmute(chat_id, target_uid)
    _inc(chat_id, "passed")
    _add_to_whitelist(chat_id, target_uid)
    _slog(f"Капча пройдена, мут снят, добавлен в вайтлист: user_id={target_uid} (chat={chat_id})")
    await callback.answer("✅ Капча пройдена! Мут снят.", show_alert=True)


# ── ОСНОВНОЙ ХЭНДЛЕР ──────────────────────────────────────────────────────────

@router.message(F.chat.type.in_({"group", "supergroup"}), F.text | F.caption, ~F.text.startswith("!"))
async def on_group_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return UNHANDLED

    chat_id = message.chat.id

    if _is_test_mode(chat_id):
        await _process_test(message)
        return UNHANDLED

    if not _is_enabled(chat_id):
        return UNHANDLED

    if _is_whitelisted(chat_id, message.from_user.id):
        return UNHANDLED

    key = (chat_id, message.from_user.id)
    bucket = _user_msgs.setdefault(key, [])
    bucket.append(message.message_id)
    if len(bucket) > _USER_MSGS_LIMIT:
        bucket[:] = bucket[-_USER_MSGS_LIMIT:]

    text = (message.text or message.caption or "").lower()
    if not any(kw in text for kw in KEYWORDS):
        return UNHANDLED

    _slog(f"Сработало ключевое слово в чате {chat_id}, запускаем проверку...")
    asyncio.create_task(_process_suspect(message))
    return UNHANDLED
