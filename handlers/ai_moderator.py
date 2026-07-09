import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Router, F
from aiogram.types import Message, TelegramObject, ChatPermissions
from aiogram.enums import ChatType

from ..storage.state import settings, save_settings, punished_users
from ..core.utils import is_admin, mark_punished
from ..services.laozhang_client import get_client_for_chat

# ────────────────────────────────────────────────────────────────────────────
# ЛОГГЕР
# ────────────────────────────────────────────────────────────────────────────
_LOG_DIR = "logs"
_LOG_PATH = os.path.join(_LOG_DIR, "ai_moderator.log")
os.makedirs(_LOG_DIR, exist_ok=True)

logger = logging.getLogger("bot.ai_moderator")
logger.setLevel(logging.DEBUG)
logger.propagate = True
if not any(isinstance(h, RotatingFileHandler) and getattr(h, "_aim", False)
           for h in logger.handlers):
    _fh = RotatingFileHandler(_LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    _fh._aim = True
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(_fh)

logger.info("=" * 70)
logger.info("AI-модератор: модуль загружен, лог: %s", _LOG_PATH)
logger.info("=" * 70)


router = Router()
SETTINGS_KEY = "ai_moderator"

DEFAULT_RULES = (
    "Запрещены: оскорбления, мат, флуд/спам, реклама, ссылки на сторонние "
    "чаты/каналы, NSFW-контент, политика, провокации, угрозы, "
    "выдача персональных данных третьих лиц.\n"
    "Сам выбирай наказание и срок исходя из тяжести нарушения."
)

DEFAULT_CFG = {
    "enabled": False,
    "rules": DEFAULT_RULES,
    "cooldown_seconds": 2,
}

_last_call_ts: dict[tuple[str, int], float] = {}

_stats = {
    "seen": 0, "analyzed": 0,
    "ok": 0, "warn": 0, "delete": 0, "mute": 0, "ban": 0,
    "errors": 0, "no_key": 0, "timeouts": 0,
}


def _cfg(chat_id_str: str) -> dict:
    settings.setdefault(chat_id_str, {})
    cfg = settings[chat_id_str].get(SETTINGS_KEY)
    if not isinstance(cfg, dict):
        cfg = dict(DEFAULT_CFG)
        settings[chat_id_str][SETTINGS_KEY] = cfg
    else:
        for k, v in DEFAULT_CFG.items():
            cfg.setdefault(k, v)
    return cfg


def _short(s: Any, n: int = 200) -> str:
    if s is None:
        return ""
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


# ────────────────────────────────────────────────────────────────────────────
# КОНТЕКСТ ДРУГИХ ЗАЩИТ
# ────────────────────────────────────────────────────────────────────────────
def _collect_protections_context(chat_id_str: str) -> str:
    s = settings.get(chat_id_str, {}) or {}
    lines = []

    def _b(key, label):
        node = s.get(key)
        if isinstance(node, dict):
            lines.append(f"- {label}: {'ВКЛ' if node.get('enabled') else 'выкл'}")

    _b("antispam", "Антиспам")
    _b("anti_link_leak", "Антислив")
    _b("anti_advertising", "Антиреклама")
    _b("anti_insults", "Антимат")
    _b("anti_politics", "Антиполитика")
    _b("anti_nsfw", "Анти-NSFW")
    _b("anti_raid", "Антирейд")

    mutes, bans = [], []
    for (cid, uid, ptype) in list(punished_users):
        if str(cid) != chat_id_str:
            continue
        pl = str(ptype).lower()
        if "мут" in pl or "mute" in pl:
            mutes.append(uid)
        elif "бан" in pl or "ban" in pl:
            bans.append(uid)
    if mutes:
        lines.append(f"- В муте (user_id): {', '.join(str(x) for x in mutes[:15])}")
    if bans:
        lines.append(f"- В бане (user_id): {', '.join(str(x) for x in bans[:15])}")
    return "\n".join(lines) if lines else "- (другие защиты не настроены)"


# ────────────────────────────────────────────────────────────────────────────
# ПРОМПТ
# ────────────────────────────────────────────────────────────────────────────
_VERDICT_SCHEMA = (
    'Ответь СТРОГО в формате JSON, без пояснений, одной строкой:\n'
    '{"action":"ok|warn|delete|mute|ban",'
    '"reason":"короткая причина по-русски (до 200 символов)",'
    '"mute_minutes":<целое число минут, только если action=mute>}'
)


def _build_system_prompt(rules: str, prot_ctx: str) -> str:
    return (
        "Ты — главный ИИ-модератор Telegram-чата. Сам выбираешь наказание "
        "и его срок согласно ПРАВИЛАМ и тяжести нарушения. Не спрашивай — действуй.\n\n"
        "ПРАВИЛА ЧАТА (заданы админом):\n"
        f"{rules}\n\n"
        "СТАТУС ДРУГИХ ЗАЩИТ:\n"
        f"{prot_ctx}\n\n"
        "Ты контролируешь работу других защит: если кого-то уже наказала "
        "другая защита, и сообщение само по себе не нарушение — верни ok. "
        "Если другая защита слабовата — ужесточи сам.\n\n"
        "Действия:\n"
        "- ok     — не нарушает правила;\n"
        "- warn   — устное предупреждение;\n"
        "- delete — удалить сообщение;\n"
        "- mute   — замутить. Сам выбери mute_minutes (1..527040 — до 366 дней);\n"
        "- ban    — забанить навсегда.\n\n"
        + _VERDICT_SCHEMA
    )


def _parse_verdict(raw: str) -> dict:
    if not raw:
        return {"action": "ok", "reason": "empty_response"}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        logger.warning("[PARSE] нет JSON: %s", _short(raw, 400))
        return {"action": "ok", "reason": "no_json"}
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        logger.warning("[PARSE] bad JSON (%s): %s", e, _short(m.group(0), 400))
        return {"action": "ok", "reason": "bad_json"}

    action = str(data.get("action", "ok")).lower().strip()
    if action not in {"ok", "warn", "delete", "mute", "ban"}:
        logger.warning("[PARSE] unknown action=%r", action)
        action = "ok"
    out = {"action": action, "reason": str(data.get("reason") or "").strip()[:300]}
    if action == "mute":
        try:
            mm = int(data.get("mute_minutes") or 0)
        except Exception:
            mm = 0
        if mm <= 0:
            mm = 30
        out["mute_minutes"] = max(1, min(mm, 527040))
    return out


# ────────────────────────────────────────────────────────────────────────────
# МЕДИА
# ────────────────────────────────────────────────────────────────────────────
async def _download_media_b64(message: Message) -> Optional[str]:
    try:
        file_id, mime, kind = None, "image/png", ""
        if message.photo:
            file_id, mime, kind = message.photo[-1].file_id, "image/jpeg", "photo"
        elif message.sticker:
            file_id, mime, kind = message.sticker.file_id, "image/webp", "sticker"
        elif message.animation and message.animation.thumbnail:
            file_id, mime, kind = message.animation.thumbnail.file_id, "image/jpeg", "gif"
        elif message.video and message.video.thumbnail:
            file_id, mime, kind = message.video.thumbnail.file_id, "image/jpeg", "video"
        elif message.document and message.document.thumbnail:
            file_id, mime, kind = message.document.thumbnail.file_id, "image/jpeg", "doc"
        if not file_id:
            return None
        buf = io.BytesIO()
        await message.bot.download(file_id, destination=buf)
        logger.debug("[MEDIA] kind=%s size=%d", kind, buf.getbuffer().nbytes)
        return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception as e:
        logger.warning("[MEDIA] fail: %s", e)
        return None


# ────────────────────────────────────────────────────────────────────────────
# ЗАПРОС К ИИ
# ────────────────────────────────────────────────────────────────────────────
async def _ask_ai(message: Message, cfg: dict) -> dict:
    chat_id_str = str(message.chat.id)
    prot_ctx = _collect_protections_context(chat_id_str)
    system_prompt = _build_system_prompt(cfg.get("rules") or DEFAULT_RULES, prot_ctx)

    u = message.from_user
    parts = [f"АВТОР: id={u.id} username=@{u.username or '-'} имя={u.full_name!r}"]
    if message.text:    parts.append(f"ТЕКСТ:\n{message.text[:2000]}")
    if message.caption: parts.append(f"ПОДПИСЬ:\n{message.caption[:1000]}")
    if message.sticker:
        parts.append(f"СТИКЕР: emoji={message.sticker.emoji!r} set={message.sticker.set_name!r}")
    if message.animation: parts.append("МЕДИА: GIF/анимация")
    if message.video:     parts.append("МЕДИА: видео")
    if message.photo:     parts.append("МЕДИА: фото")
    if message.document:  parts.append(f"МЕДИА: документ {message.document.mime_type}")
    user_text = "\n".join(parts) or "(пусто)"

    logger.info("[PROMPT.user] %s", _short(user_text, 500))

    has_media = bool(
        message.photo or message.sticker or message.animation
        or message.video or (message.document and message.document.thumbnail)
    )

    started = time.time()

    if has_media:
        vision = get_client_for_chat(chat_id_str, "vision")
        if vision:
            data_url = await _download_media_b64(message)
            if data_url:
                msgs = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_text + "\n\nНиже само медиа:"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]},
                ]
                logger.info("[AI] -> vision chat=%s", chat_id_str)
                raw = await vision.chat_messages(msgs, max_tokens=250, temperature=0.0)
                logger.info("[AI] <- vision raw=%s | %.2fs",
                            _short(raw, 400), time.time() - started)
                return _parse_verdict(raw)
        else:
            logger.info("[AI] vision-ключ не выставлен в чате %s", chat_id_str)

    text_client = get_client_for_chat(chat_id_str, "text")
    if not text_client:
        _stats["no_key"] += 1
        logger.warning("[AI] text-ключ не выставлен в чате %s — пропуск", chat_id_str)
        return {"action": "ok", "reason": "no_key"}

    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    logger.info("[AI] -> text chat=%s", chat_id_str)
    raw = await text_client.chat_messages(msgs, max_tokens=250, temperature=0.0)
    logger.info("[AI] <- text raw=%s | %.2fs", _short(raw, 400), time.time() - started)
    return _parse_verdict(raw)


def _fmt_duration(minutes: int) -> str:
    if minutes < 60: return f"{minutes} мин"
    if minutes < 60 * 24:
        h, m = divmod(minutes, 60)
        return f"{h} ч" + (f" {m} мин" if m else "")
    if minutes < 60 * 24 * 30: return f"{minutes // (60 * 24)} дн"
    if minutes < 60 * 24 * 365: return f"~{minutes // (60 * 24 * 30)} мес"
    return f"~{minutes // (60 * 24 * 365)} лет"


# ────────────────────────────────────────────────────────────────────────────
# ПРИМЕНЕНИЕ
# ────────────────────────────────────────────────────────────────────────────
async def _apply_verdict(message: Message, verdict: dict):
    action = verdict.get("action", "ok")
    reason = verdict.get("reason") or "нарушение правил"
    if action == "ok":
        _stats["ok"] += 1
        return

    _stats[action] = _stats.get(action, 0) + 1
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_tag = (f"@{message.from_user.username}" if message.from_user.username
                else message.from_user.full_name)

    logger.info("[APPLY] action=%s user=%s(id=%d) chat=%d reason=%r",
                action, user_tag, user_id, chat_id, reason)

    try:
        if action == "warn":
            try:
                await message.reply(f"⚠️ {user_tag}, предупреждение от ИИ-модератора: {reason}")
            except Exception as e:
                logger.warning("[APPLY.warn] %s", e)

        elif action == "delete":
            try:
                await message.delete()
                logger.info("[APPLY.delete] OK")
            except Exception as e:
                logger.warning("[APPLY.delete] %s", e)
            try:
                await message.answer(f"🧹 ИИ-модератор удалил сообщение от {user_tag}: {reason}")
            except Exception:
                pass

        elif action == "mute":
            minutes = int(verdict.get("mute_minutes") or 30)
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            perms = ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
            )
            try:
                await message.bot.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=user_id,
                    permissions=perms,
                    until_date=until,
                )
                logger.info("[APPLY.mute] %s на %s (до %s)",
                            user_tag, _fmt_duration(minutes), until.isoformat())
            except Exception as e:
                logger.warning("[APPLY.mute] restrict fail: %s", e)
            try: await message.delete()
            except Exception: pass
            await mark_punished(chat_id, user_id, "мут",
                                reason=f"ИИ-мод: {reason}",
                                duration=_fmt_duration(minutes),
                                by="ai-moderator",
                                username=message.from_user.username or "")
            try:
                await message.answer(
                    f"🤖 ИИ-модератор: {user_tag} замучен на "
                    f"{_fmt_duration(minutes)}.\nПричина: {reason}"
                )
            except Exception:
                pass

        elif action == "ban":
            try:
                await message.chat.ban(user_id)
                logger.info("[APPLY.ban] %s забанен", user_tag)
            except Exception as e:
                logger.warning("[APPLY.ban] %s", e)
            try: await message.delete()
            except Exception: pass
            await mark_punished(chat_id, user_id, "бан",
                                reason=f"ИИ-мод: {reason}",
                                by="ai-moderator",
                                username=message.from_user.username or "")
            try:
                await message.answer(f"🤖 ИИ-модератор: {user_tag} забанен.\nПричина: {reason}")
            except Exception:
                pass

    except Exception as e:
        _stats["errors"] += 1
        logger.exception("[APPLY] crit: %s", e)


# ────────────────────────────────────────────────────────────────────────────
# ФОНОВАЯ ОБРАБОТКА (без блокировки роутеров)
# ────────────────────────────────────────────────────────────────────────────
async def _moderate_in_background(message: Message):
    """Полный цикл модерации, запускаемый из middleware через create_task."""
    try:
        chat_id_str = str(message.chat.id)
        cfg = settings.get(chat_id_str, {}).get(SETTINGS_KEY)
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return

        # Не модерируем админов и ботов
        if message.from_user.is_bot:
            return
        if await is_admin(message):
            logger.info("[SKIP] %s — админ", message.from_user.id)
            return

        # Анти-флуд самого ИИ
        key = (chat_id_str, message.from_user.id)
        now = time.time()
        cd = cfg.get("cooldown_seconds", 2)
        if now - _last_call_ts.get(key, 0) < cd:
            logger.info("[SKIP] cooldown user=%d", message.from_user.id)
            return
        _last_call_ts[key] = now

        _stats["analyzed"] += 1

        try:
            verdict = await asyncio.wait_for(_ask_ai(message, cfg), timeout=25)
        except asyncio.TimeoutError:
            _stats["timeouts"] += 1
            logger.warning("[AI] TIMEOUT chat=%s user=%d", chat_id_str, message.from_user.id)
            return
        except Exception as e:
            _stats["errors"] += 1
            logger.exception("[AI] err: %s", e)
            return

        logger.info("[VERDICT] chat=%s user=%d -> %s",
                    chat_id_str, message.from_user.id, verdict)
        await _apply_verdict(message, verdict)
    except Exception as e:
        logger.exception("[BG] фоновая обработка упала: %s", e)


# ────────────────────────────────────────────────────────────────────────────
# OUTER MIDDLEWARE — НЕ перехватывает, а просто запускает фоновый таск
# ────────────────────────────────────────────────────────────────────────────
class AIModeratorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Сразу пропускаем дальше, ничего не блокируя
        if not isinstance(event, Message):
            return await handler(event, data)

        msg: Message = event

        # Только группы
        if msg.chat.type not in ("group", "supergroup"):
            return await handler(event, data)

        # Не реагируем на собственные команды !ии-модер (их обработает роутер)
        # и на любые командные сообщения (! . /) — это привычно не модерируется ИИ
        text_for_check = msg.text or msg.caption or ""
        if text_for_check.startswith(("!", ".", "/")):
            return await handler(event, data)

        # Нужен реальный автор
        if not msg.from_user:
            return await handler(event, data)

        chat_id_str = str(msg.chat.id)
        cfg = settings.get(chat_id_str, {}).get(SETTINGS_KEY)
        if isinstance(cfg, dict) and cfg.get("enabled"):
            _stats["seen"] += 1

            # лог факта получения — здесь, чтобы видеть его всегда
            kinds = []
            if msg.text: kinds.append("text")
            if msg.caption: kinds.append("caption")
            if msg.photo: kinds.append("photo")
            if msg.sticker: kinds.append("sticker")
            if msg.animation: kinds.append("gif")
            if msg.video: kinds.append("video")
            if msg.document: kinds.append("doc")
            logger.info(
                "[SEEN] chat=%s msg_id=%s user=%s(id=%d) kind=%s text=%r",
                chat_id_str, msg.message_id,
                msg.from_user.username or msg.from_user.full_name, msg.from_user.id,
                ",".join(kinds) or "empty",
                _short(text_for_check, 120),
            )

            # ЗАПУСКАЕМ В ФОНЕ — middleware МГНОВЕННО отдаёт сообщение дальше
            asyncio.create_task(_moderate_in_background(msg))

        # ВАЖНО: всегда пропускаем сообщение остальным роутерам
        return await handler(event, data)


# Готовый экземпляр middleware для подключения в main.py
ai_moderator_middleware = AIModeratorMiddleware()


# ────────────────────────────────────────────────────────────────────────────
# КОМАНДЫ УПРАВЛЕНИЯ (обычный роутер — конфликтов нет, фильтр по префиксу)
# ────────────────────────────────────────────────────────────────────────────
@router.message(F.text.regexp(r"^[!.](ии[-_ ]?модер|ai[-_ ]?mod)(\s|$)"))
async def cmd_ai_moderator(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return await message.reply("⚠️ Команда доступна только в группах.")
    if not await is_admin(message):
        return await message.reply("❗ Только администратор может управлять ИИ-модератором.")

    chat_id_str = str(message.chat.id)
    cfg = _cfg(chat_id_str)

    raw = message.text.strip()
    rest = re.sub(r"^[!.](ии[-_ ]?модер|ai[-_ ]?mod)\s*", "", raw, flags=re.I)
    low = rest.lower()

    if not rest:
        status = "✅ ВКЛЮЧЕН" if cfg.get("enabled") else "❌ ВЫКЛЮЧЕН"
        rules_short = (cfg.get("rules") or "")[:500]
        more = "…" if len(cfg.get("rules", "")) > 500 else ""
        return await message.reply(
            "🤖 **ИИ-модератор**\n"
         
            f"Статус: {status}\n\n"
            f"📜 Правила:\n{rules_short}{more}\n\n"
            f"📊 Статистика (с момента старта бота):\n"
            f"• увидено: {_stats['seen']}\n"
            f"• в ИИ: {_stats['analyzed']}\n"
            f"• ok/warn/delete/mute/ban: "
            f"{_stats['ok']}/{_stats['warn']}/{_stats['delete']}/{_stats['mute']}/{_stats['ban']}\n"
            f"• ошибок: {_stats['errors']} | таймаутов: {_stats['timeouts']} | нет ключа: {_stats['no_key']}\n\n"
            "Команды:\n"
            "• `!ии-модер вкл` / `!ии-модер выкл`\n"
            "• `!ии-модер правила \"текст\"`\n",
            
            
            parse_mode="Markdown",
        )

    if low.startswith(("вкл", "on", "enable")):
        cfg["enabled"] = True
        save_settings(chat_id_str)
        logger.info("[CMD] ВКЛЮЧЁН в чате %s", chat_id_str)
        return await message.reply(
            "✅ ИИ-модератор ВКЛЮЧЁН.\n"
            f"📜 Правила:\n{cfg.get('rules', DEFAULT_RULES)}\n\n"
            f"`",
            parse_mode="Markdown",
        )

    if low.startswith(("выкл", "off", "disable")):
        cfg["enabled"] = False
        save_settings(chat_id_str)
        logger.info("[CMD] ВЫКЛЮЧЕН в чате %s", chat_id_str)
        return await message.reply("❌ ИИ-модератор выключен.")

    if low.startswith("правила"):
        m = re.search(r'["«“](.+?)["»”]', rest, re.S)
        new_rules = m.group(1).strip() if m else re.sub(r"^правила\s*", "", rest, flags=re.I).strip()
        if not new_rules:
            return await message.reply(
                "❗ Использование: `!ии-модер правила \"текст правил\"`",
                parse_mode="Markdown",
            )
        cfg["rules"] = new_rules[:4000]
        save_settings(chat_id_str)
        logger.info("[CMD] ПРАВИЛА в чате %s: %s", chat_id_str, _short(new_rules, 200))
        return await message.reply(f"✅ Правила обновлены.\n\n📜 {cfg['rules']}")

    if low.startswith("лог"):
        try:
            with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-20:]
            text = "".join(tail) or "(пусто)"
            if len(text) > 3800:
                text = text[-3800:]
            return await message.reply(f"📋 Последние строки:\n<pre>{text}</pre>", parse_mode="HTML")
        except FileNotFoundError:
            return await message.reply("Лог-файла пока нет.")
        except Exception as e:
            return await message.reply(f"Ошибка чтения лога: {e}")

    if low.startswith("тест"):
        target = message.reply_to_message
        if not target:
            return await message.reply("❗ Ответь этой командой на сообщение.")
        logger.info("[CMD.тест] msg_id=%s", target.message_id)
        try:
            verdict = await asyncio.wait_for(_ask_ai(target, cfg), timeout=25)
        except Exception as e:
            return await message.reply(f"Ошибка ИИ: {e}")
        return await message.reply(
            "🧪 Результат:\n<pre>" + json.dumps(verdict, ensure_ascii=False, indent=2) + "</pre>",
            parse_mode="HTML",
        )

    return await message.reply(
        "❌ Неизвестная подкоманда. Введи `!ии-модер` чтобы увидеть список.",
        parse_mode="Markdown",
    )