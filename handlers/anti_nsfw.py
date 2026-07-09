


import io
import gzip
import logging
import tempfile
from datetime import datetime, timedelta

import aiohttp
import cv2

from aiogram import Router, F
from aiogram.types import Message, ChatPermissions

from ..core.loader import bot
from ..storage.state import settings, save_settings
from ..core.utils import (
    is_already_punished,
    mark_punished,
    get_duration_seconds,
    can_bot_restrict_members,
)
from ..services.laozhang_client import get_client_for_chat

logger = logging.getLogger(__name__)
router = Router()


# ──────────────────────────────────────────────────────────────────────────────
# Фильтр «тупых» срабатываний
# ──────────────────────────────────────────────────────────────────────────────
NSFW_MIN_SCORE = 0.75

# Если в analysis говорится ТОЛЬКО про текст / надписи / мат / оскорбления —
# это не NSFW, не мутим.
TEXT_ONLY_MARKERS = (
    "оскорбл", "ругатель", "мат", "матерн", "нецензурн", "брань", "обзыв",
    "надпись", "текст", "слов", "букв", "caption", "watermark",
    "insult", "swear", "profan", "vulgar", "rude text", "offensive text",
    "bad word", "curse word", "obscene language", "obscene word",
)

# А вот это — реально визуальный NSFW.
VISUAL_NSFW_MARKERS = (
    "обнаж", "голы", "голая", "голый", "нагот", "половой", "генитал",
    "грудь", "соск", "порн", "эрек", "интим", "сексуальн", "поза", "акт",
    "кров", "расчленен", "насил", "увечь", "труп", "ранен",
    "nudity", "naked", "nude", "genital", "breast", "nipple", "porn",
    "sexual", "intercourse", "erection", "intimate", "explicit",
    "gore", "blood", "dismember", "violence", "corpse", "wound",
)


def _is_real_visual_nsfw(analysis: str, score: float) -> bool:
    """True только если контент действительно визуально NSFW."""
    if score < NSFW_MIN_SCORE:
        return False
    if not analysis:
        return True  # анализа нет — доверяем флагу + порогу
    text = analysis.lower()
    has_visual = any(m in text for m in VISUAL_NSFW_MARKERS)
    has_text_only = any(m in text for m in TEXT_ONLY_MARKERS)
    if has_visual:
        return True
    if has_text_only and not has_visual:
        # модель прицепилась только к надписям/мату — игнор
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Рендер TGS (Lottie) → PNG-байты первого кадра
# ──────────────────────────────────────────────────────────────────────────────
def _render_tgs_first_frame(tgs_bytes: bytes) -> bytes | None:
    # TGS = gzip(JSON Lottie)
    try:
        raw_json = gzip.decompress(tgs_bytes)
    except OSError:
        raw_json = tgs_bytes  # вдруг уже распакован

    # 1) rlottie-python
    try:
        from rlottie_python import LottieAnimation
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp.write(raw_json)
            tmp_path = tmp.name
        anim = LottieAnimation.from_file(tmp_path)
        pil_img = anim.render_pillow_frame(frame_num=0)
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"[NSFW] rlottie не сработал: {e}")

    # 2) python-lottie (фолбэк)
    try:
        from lottie.parsers.tgs import parse_tgs
        from lottie.exporters.cairo import export_png
        anim = parse_tgs(io.BytesIO(tgs_bytes))
        buf = io.BytesIO()
        export_png(anim, buf, frame=0)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"[NSFW] python-lottie не сработал: {e}")

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Кадр из середины видео/GIF/webm
# ──────────────────────────────────────────────────────────────────────────────
def _extract_middle_frame(video_bytes: bytes, suffix: str) -> bytes | None:
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            cap.release()
            return None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        h, w = frame.shape[:2]
        scale = 320 / max(h, w)
        if scale < 1:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        return buf.tobytes()
    except Exception as e:
        logger.error(f"[NSFW] Ошибка извлечения среднего кадра: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Команда настройки
# ──────────────────────────────────────────────────────────────────────────────
async def configure_anti_nsfw_manual(text: str, message: Message):
    chat_id = str(message.chat.id)
    user_id = message.from_user.id
    parts = message.text.strip().lower().split()

    member = await message.bot.get_chat_member(message.chat.id, user_id)
    if member.status not in ("administrator", "creator"):
        return await message.reply("❗ Только админ может настраивать защиту от 18+.")

    settings.setdefault(chat_id, {})
    settings[chat_id].setdefault(
        "antinsfw",
        {"enabled": False, "punishment": "мут", "duration": 30, "unit": "мин"},
    )
    nsfw_cfg = settings[chat_id]["antinsfw"]

    # Нормализуем варианты: "18+вкл" / "18+ вкл" / "nsfwвкл" → действие
    action = None
    if len(parts) == 3 and parts[2] in ("вкл", "выкл"):
        action = parts[2]
    elif len(parts) == 2:
        p = parts[1]
        if p.endswith("вкл"):
            action = "вкл"
        elif p.endswith("выкл"):
            action = "выкл"

    if action == "вкл":
        if nsfw_cfg.get("enabled"):
            return await message.reply("⚠️ Защита от 18+ уже включена.")
        nsfw_cfg["enabled"] = True
        save_settings(chat_id)
        p = nsfw_cfg.get("punishment", "мут")
        if p == "бан":
            return await message.reply("🔞 Защита от 18+ включена. Наказание: бан навсегда.")
        return await message.reply(
            f"🔞 Защита от 18+ включена. Наказание: мут на "
            f"{nsfw_cfg.get('duration', 30)} {nsfw_cfg.get('unit', 'мин')}."
        )

    if action == "выкл":
        if not nsfw_cfg.get("enabled"):
            return await message.reply("⚠️ Защита от 18+ уже выключена.")
        nsfw_cfg["enabled"] = False
        save_settings(chat_id)
        return await message.reply("🔞 Защита от 18+ выключена.")

    # Настройка наказания: !защита 18+ мут 30 мин   |   !защита 18+ бан
    if len(parts) >= 3:
        punishment = parts[2]
        if punishment not in ("мут", "бан"):
            return await message.reply("❌ Укажите наказание: мут или бан")

        current_punishment = nsfw_cfg.get("punishment", "мут")
        current_duration = nsfw_cfg.get("duration", 30)
        current_unit = nsfw_cfg.get("unit", "мин")

        if punishment == "бан":
            if current_punishment == "бан":
                return await message.reply("⚠️ Наказание уже установлено: бан навсегда")
            nsfw_cfg.update({"punishment": "бан", "duration": None, "unit": None})
            save_settings(chat_id)
            return await message.reply("✅ Защита от 18+: бан навсегда")

        if len(parts) >= 4:
            try:
                duration = int(parts[3])
            except ValueError:
                return await message.reply("❗ Время должно быть числом.")
            unit = parts[4] if len(parts) > 4 else "мин"

            if (current_punishment == "мут"
                    and current_duration == duration
                    and current_unit == unit):
                return await message.reply(f"⚠️ Время мута уже установлено: {duration} {unit}")
            nsfw_cfg.update({"punishment": "мут", "duration": duration, "unit": unit})
            save_settings(chat_id)
            return await message.reply(f"✅ Защита от 18+: мут на {duration} {unit}")

        if (current_punishment == "мут"
                and current_duration == 30
                and current_unit == "мин"):
            return await message.reply("⚠️ Время мута уже установлено: 30 мин")
        nsfw_cfg.update({"punishment": "мут", "duration": 30, "unit": "мин"})
        save_settings(chat_id)
        return await message.reply("✅ Защита от 18+: мут на 30 мин")

    await message.reply(
        "Использование:\n"
        "`!защита 18+ вкл/выкл`\n"
        "`!защита 18+ мут 30 мин`\n"
        "`!защита 18+ бан`",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Основной сканер
# ──────────────────────────────────────────────────────────────────────────────
async def nsfw_scan(message: Message):
    chat_id = str(message.chat.id)
    user_id = message.from_user.id
    logger.info(f"[NSFW] Запуск проверки. Чат: {chat_id}, Пользователь: {user_id}")

    chat_settings = settings.setdefault(chat_id, {})
    chat_settings.setdefault("antinsfw", {
        "enabled": False, "punishment": "мут", "duration": 30, "unit": "мин",
    })
    cfg = chat_settings["antinsfw"]
    if not cfg.get("enabled", False):
        return

    try:
        file_id = None
        media_kind = None  # "photo" | "webp_sticker" | "tgs_sticker" | "webm_sticker" | "animation" | "video" | "document"

        if message.sticker:
            st = message.sticker
            if getattr(st, "is_animated", False):
                media_kind = "tgs_sticker"
                file_id = st.file_id
            elif getattr(st, "is_video", False):
                media_kind = "webm_sticker"
                file_id = st.file_id
            else:
                media_kind = "webp_sticker"
                file_id = st.file_id
        elif message.animation:
            media_kind = "animation"
            file_id = message.animation.file_id
        elif message.photo:
            media_kind = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_kind = "video"
            file_id = message.video.file_id
        elif message.document:
            media_kind = "document"
            file_id = message.document.file_id

        if not file_id:
            return

        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                file_bytes = await resp.read()

        logger.info(f"[NSFW] Скачан {media_kind}: {len(file_bytes)} байт")

        # Приводим к статичной картинке (PNG/JPEG-байты)
        image_bytes = None

        if media_kind == "tgs_sticker":
            image_bytes = _render_tgs_first_frame(file_bytes)
            if not image_bytes:
                # последний шанс — thumbnail
                thumb = getattr(message.sticker, "thumbnail", None)
                if thumb:
                    tf = await bot.get_file(thumb.file_id)
                    tf_url = f"https://api.telegram.org/file/bot{bot.token}/{tf.file_path}"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(tf_url) as resp:
                            image_bytes = await resp.read()
                if not image_bytes:
                    logger.warning("[NSFW] TGS не удалось отрендерить и нет thumbnail — пропуск")
                    return

        elif media_kind in ("webm_sticker", "animation", "video"):
            suffix = {".webm_sticker": ".webm", "animation": ".mp4", "video": ".mp4",
                      "webm_sticker": ".webm"}.get(media_kind, ".mp4")
            image_bytes = _extract_middle_frame(file_bytes, suffix)
            if not image_bytes:
                logger.warning(f"[NSFW] Не удалось извлечь средний кадр из {media_kind}")
                return

        else:
            # photo / webp_sticker / document → отдаём как есть
            image_bytes = file_bytes

        # Проверка
        nsfw_client = get_client_for_chat(message.chat.id, "vision")
        if not nsfw_client:
            logger.warning("[NSFW] Нет ключа Laozhang (vision) в настройках чата — пропуск.")
            return

        logger.info("[NSFW] Отправляем на проверку в Laozhang.ai...")
        result = await nsfw_client.check_nsfw(image_bytes)

        if result.get("error"):
            logger.error(f"[NSFW] Ошибка проверки: {result['error']}")
            return

        is_nsfw = bool(result.get("is_nsfw", False))
        nsfw_score = float(result.get("nsfw_score", 0) or 0)
        nsfw_response = result.get("response", "N/A")
        nsfw_analysis = result.get("analysis", "") or ""

        logger.info(
            f"[NSFW] Результат: is_nsfw={is_nsfw}, score={nsfw_score}, "
            f"response={nsfw_response}"
        )
        if nsfw_analysis:
            logger.info(f"[NSFW] Анализ: {nsfw_analysis[:500]}")

        # ── фильтр «тупых» срабатываний ─────────────────────────────────────
        if is_nsfw and not _is_real_visual_nsfw(nsfw_analysis, nsfw_score):
            logger.info(
                f"[NSFW] Игнор: score={nsfw_score:.2f} либо анализ про текст/оскорбления, "
                f"а не визуальный NSFW."
            )
            return

        if not is_nsfw:
            logger.info("[NSFW] Контент безопасен")
            return

        # ── наказание ───────────────────────────────────────────────────────
        punishment = cfg.get("punishment", "мут")

        if await is_already_punished(chat_id, user_id, punishment):
            logger.info(f"[NSFW] Уже наказан ({punishment}), только удаляем")
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            return

        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            logger.warning(f"[NSFW] Не удалось удалить сообщение: {e}")

        if punishment == "мут":
            ok, reason = await can_bot_restrict_members(message)
            if not ok:
                await message.answer(f"❌ Не удалось замутить: {reason}")
                return
            seconds = get_duration_seconds(cfg.get("duration", 30), cfg.get("unit", "мин"))
            until = datetime.now() + timedelta(seconds=seconds)
            try:
                await bot.restrict_chat_member(
                    message.chat.id,
                    user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until.timestamp(),
                )
                await mark_punished(chat_id, user_id, "мут")
                await message.answer(f"🔇 {message.from_user.full_name} замучен за NSFW.")
                try:
                    from ..storage.ai_context_events import log_chat_event, format_user_tg
                    analysis_short = (nsfw_analysis[:120] + "...") if len(nsfw_analysis) > 120 else nsfw_analysis
                    log_chat_event(
                        message.chat.id,
                        f"NSFW МУТ: {format_user_tg(message.from_user)} | "
                        f"response={nsfw_response}, score={nsfw_score}, analysis={analysis_short}",
                    )
                except Exception:
                    pass
            except Exception as e:
                if "method is available only for supergroups" in str(e).lower():
                    await message.answer("❌ Мут работает только в супергруппах.")
                else:
                    logger.exception(f"[NSFW] Ошибка мута: {e}")

        elif punishment == "бан":
            try:
                await bot.ban_chat_member(message.chat.id, user_id)
                await mark_punished(chat_id, user_id, "бан")
                await message.answer(f"🚫 {message.from_user.full_name} забанен за NSFW.")
                try:
                    from ..storage.ai_context_events import log_chat_event, format_user_tg
                    analysis_short = (nsfw_analysis[:120] + "...") if len(nsfw_analysis) > 120 else nsfw_analysis
                    log_chat_event(
                        message.chat.id,
                        f"NSFW БАН: {format_user_tg(message.from_user)} | "
                        f"response={nsfw_response}, score={nsfw_score}, analysis={analysis_short}",
                    )
                except Exception:
                    pass
            except Exception as e:
                await message.answer(f"❌ Не удалось забанить: {e}")

    except Exception as e:
        logger.exception(f"[NSFW] Общая ошибка при анализе: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Роутер
# ──────────────────────────────────────────────────────────────────────────────
@router.message(F.text.startswith(("!защита", ".защита")))
async def handle_protection_command(message: Message):
    text = message.text.lower()
    parts = text.strip().split()
    if len(parts) < 2:
        return
    subject = parts[1]

    if subject in ("антислив", "антислив_инфы"):
        from .anti_leak import configure_anti_leak
        await configure_anti_leak(message)
    elif subject.startswith("18+") or subject.startswith("nsfw"):
        await configure_anti_nsfw_manual(text, message)