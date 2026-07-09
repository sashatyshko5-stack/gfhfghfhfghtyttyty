import logging
import re
import asyncio
import aiohttp
import json as _json
from collections import deque
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.enums import ParseMode

from ..services.ai_module import (
    set_user_laozhang_key, get_user_laozhang_key,
    has_user_laozhang_key, clear_user_laozhang_key,
)
from ..services.chat_ai_router import DEFAULT_MODELS
from ..storage.state import settings, save_settings

logger = logging.getLogger(__name__)
router = Router(name="private_ai")

# ── История диалога ────────────────────────────────────────────────────────
_HISTORY_MAX = 10
_dm_history: dict[int, deque] = {}


def _get_history(uid: int) -> list[dict]:
    return list(_dm_history.get(uid, []))


def _push_history(uid: int, role: str, content: str) -> None:
    if uid not in _dm_history:
        _dm_history[uid] = deque(maxlen=_HISTORY_MAX)
    _dm_history[uid].append({"role": role, "content": content})


def _clear_history(uid: int) -> None:
    _dm_history.pop(uid, None)


# ── Модели ─────────────────────────────────────────────────────────────────
_LAOZHANG_MODELS: dict[str, str] = {
    "deepseek-r1":      "deepseek-r1",
    "deepseek-v3":      "deepseek-v3",
    "gpt-4o-mini":      "gpt-4o-mini",
    "gpt-4o":           "gpt-4o",
    "claude-sonnet":    "claude-3-5-sonnet-20241022",
    "gemini-flash":     "gemini-2.0-flash",
}

_DEFAULT_MODEL = "gpt-4o-mini"
_user_model: dict[int, str] = {}


def _get_user_model(uid: int) -> str:
    return _user_model.get(uid, _DEFAULT_MODEL)


def _set_user_model(uid: int, key: str) -> None:
    _user_model[uid] = _LAOZHANG_MODELS.get(key, _DEFAULT_MODEL)


# ── Rich Text форматирование ───────────────────────────────────────────────

def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_md_to_html(text: str) -> str:
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", text)
    text = re.sub(r"__(.+?)__", lambda m: f"<u>{m.group(1)}</u>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", lambda m: f"<i>{m.group(1)}</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", lambda m: f"<i>{m.group(1)}</i>", text)
    text = re.sub(r"~~(.+?)~~", lambda m: f"<s>{m.group(1)}</s>", text)
    text = re.sub(r"\|\|(.+?)\|\|", lambda m: f"<tg-spoiler>{m.group(1)}</tg-spoiler>", text)
    return text


def _md_to_html(text: str) -> str:
    lines = text.split("\n")
    result = []
    in_code_block = False
    code_lang = ""
    code_lines: list[str] = []

    for line in lines:
        if line.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lang = line[3:].strip()
                code_lines = []
            else:
                in_code_block = False
                code_content = _escape_html("\n".join(code_lines))
                tag = (
                    f'<pre><code class="{_escape_html(code_lang)}">{code_content}</code></pre>'
                    if code_lang else
                    f"<pre><code>{code_content}</code></pre>"
                )
                result.append(tag)
                code_lang = ""
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if re.match(r"^[-_*]{3,}$", line.strip()):
            result.append("─" * 24)
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            icons = {1: "┌", 2: "▌", 3: "•"}
            lvl = len(heading.group(1))
            result.append(f"<b>{icons[lvl]} {_escape_html(heading.group(2))}</b>")
            continue

        quote = re.match(r"^>\s*(.*)$", line)
        if quote:
            result.append(f"<blockquote>{_inline_md_to_html(_escape_html(quote.group(1)))}</blockquote>")
            continue

        bullet = re.match(r"^[\-\*•]\s+(.+)$", line)
        if bullet:
            result.append(f"  • {_inline_md_to_html(_escape_html(bullet.group(1)))}")
            continue

        numbered = re.match(r"^(\d+)\.\s+(.+)$", line)
        if numbered:
            result.append(f"  {numbered.group(1)}. {_inline_md_to_html(_escape_html(numbered.group(2)))}")
            continue

        result.append(_inline_md_to_html(_escape_html(line)))

    if in_code_block and code_lines:
        result.append(f"<pre><code>{_escape_html(chr(10).join(code_lines))}</code></pre>")

    return "\n".join(result)


def _make_rich_html(text: str) -> str:
    html = _md_to_html(text)
    if len(html) > 800:
        pivot = html.find("\n", 300)
        if pivot == -1 or pivot > 600:
            pivot = 400
        visible = html[:pivot]
        hidden  = html[pivot:]
        if hidden.strip():
            return f"{visible}<blockquote expandable>{hidden}</blockquote>"
    return html


def _split_smart(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        chunk = text[:max_len]
        split_at = chunk.rfind("\n\n")
        if split_at == -1:
            split_at = chunk.rfind("\n")
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return [p for p in parts if p]


# ── Разбор thinking-токенов ────────────────────────────────────────────────

def _split_thinking(raw: str) -> tuple[str, str]:
    """
    Возвращает (thinking_text, answer_text).
    Поддерживает <think>...</think> теги (DeepSeek R1).
    """
    think_parts = []

    # Закрытые блоки
    closed = re.findall(r"<think>(.*?)</think>", raw, re.DOTALL)
    think_parts.extend(closed)
    rest = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # Незакрытый блок (стрим ещё идёт)
    open_match = re.search(r"<think>(.*?)$", rest, re.DOTALL)
    if open_match:
        think_parts.append(open_match.group(1))
        rest = rest[:open_match.start()].strip()

    thinking = "\n\n".join(t.strip() for t in think_parts if t.strip())
    return thinking, rest


def _collect_thinking(raw: str, reasoning: str) -> str:
    """Собираем всё мышление из обоих источников."""
    think_from_tags, _ = _split_thinking(raw)
    return "\n\n".join(t for t in [reasoning.strip(), think_from_tags.strip()] if t)


def _format_thinking_block(thinking: str, in_progress: bool = False) -> str:
    if not thinking.strip():
        return ""

    header  = "💭 <i>Думает...</i>" if in_progress else "💭 <b>Мышление</b>"
    content = _escape_html(thinking[:3000])

    pivot = content.find("\n", 150)
    if pivot == -1 or pivot > 300:
        pivot = 200
    visible = content[:pivot].strip()
    hidden  = content[pivot:].strip()

    if hidden:
        return f"{header}\n<blockquote expandable>{visible}\n{hidden}</blockquote>"
    return f"{header}\n<blockquote>{visible}</blockquote>"


# ── SSE-генератор ──────────────────────────────────────────────────────────

async def _stream_openai(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.7,
    timeout: int = 90,
):
    """Yield (accumulated_raw, accumulated_reasoning, is_final)."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    accumulated_raw       = ""
    accumulated_reasoning = ""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[DM-AI][stream] HTTP {resp.status}: {body[:300]}")
                    return

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        if accumulated_raw or accumulated_reasoning:
                            yield accumulated_raw, accumulated_reasoning, True
                        return
                    try:
                        chunk = _json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})

                        token = delta.get("content") or ""
                        if token:
                            accumulated_raw += token

                        # reasoning_content — отдельное поле (некоторые провайдеры)
                        r_token = (
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or ""
                        )
                        if r_token:
                            accumulated_reasoning += r_token
                            logger.debug(f"[DM-AI][stream] reasoning_token len={len(accumulated_reasoning)}")

                        if token or r_token:
                            yield accumulated_raw, accumulated_reasoning, False

                    except Exception:
                        continue

    except asyncio.TimeoutError:
        logger.warning("[DM-AI][stream] timeout")
    except Exception as e:
        logger.warning(f"[DM-AI][stream] error: {e}")

    if accumulated_raw or accumulated_reasoning:
        yield accumulated_raw, accumulated_reasoning, True


# ── Сборка preview и финала ────────────────────────────────────────────────

def _build_draft_preview(raw: str, reasoning: str) -> str:
    """
    Текст для черновика/edit во время стрима.
    Фаза 1 — только мышление (answer ещё пустой).
    Фаза 2 — мышление свёрнуто + идёт ответ.
    """
    all_thinking = _collect_thinking(raw, reasoning)
    _, answer    = _split_thinking(raw)

    parts = []

    if all_thinking:
        short = _escape_html(all_thinking[:300])
        if len(all_thinking) > 300:
            short += "…"
        # Показываем thinking в blockquote
        parts.append(f"💭 <i>Думает...</i>\n<blockquote>{short}</blockquote>")

    if answer.strip():
        # Ответ уже пошёл — показываем первые 1500 символов
        parts.append(_escape_html(answer.strip()[:1500]))
    elif not all_thinking:
        # Ни мышления ни ответа — просто индикатор
        parts.append("⏳ <i>Генерирую...</i>")

    return "\n\n".join(parts) if parts else "⏳"


def _build_final_messages(raw: str, reasoning: str) -> list[str]:
    """
    Финальный список сообщений:
    [0] — thinking block (если есть)
    [1+] — ответ с Rich HTML
    """
    all_thinking = _collect_thinking(raw, reasoning)
    _, answer    = _split_thinking(raw)
    answer = answer if answer.strip() else raw

    result = []

    if all_thinking:
        block = _format_thinking_block(all_thinking, in_progress=False)
        if block:
            result.append(block)

    if answer.strip():
        html = _make_rich_html(answer)
        result.extend(_split_smart(html, max_len=4000))

    return result


# ── Стриминг через sendMessageDraft ───────────────────────────────────────

async def _reply_with_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict],
) -> tuple[str, str] | None:
    from aiogram.methods import SendMessageDraft

    accumulated_raw       = ""
    accumulated_reasoning = ""
    last_draft_text       = ""
    DRAFT_INTERVAL        = 0.3
    last_draft_time       = 0.0

    async def _push_draft(raw: str, reasoning: str) -> None:
        nonlocal last_draft_time, last_draft_text
        now = asyncio.get_event_loop().time()
        if now - last_draft_time < DRAFT_INTERVAL:
            return
        preview = _build_draft_preview(raw, reasoning)
        if preview == last_draft_text:
            return
        try:
            await bot(SendMessageDraft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=preview,
                parse_mode="HTML",
            ))
            last_draft_time = now
            last_draft_text = preview
        except Exception as e:
            logger.debug(f"[DM-AI][draft] push fail: {e}")

    try:
        async for raw, reasoning, is_final in _stream_openai(
            url=endpoint, api_key=api_key, model=model,
            messages=messages, max_tokens=1000, temperature=0.7,
        ):
            accumulated_raw       = raw
            accumulated_reasoning = reasoning
            if is_final:
                break
            await _push_draft(raw, reasoning)

    except Exception as e:
        logger.exception(f"[DM-AI][draft] loop error: {e}")

    if not accumulated_raw and not accumulated_reasoning:
        return None

    for msg in _build_final_messages(accumulated_raw, accumulated_reasoning):
        try:
            await bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"[DM-AI][draft] final send fail: {e}")
            # plain text fallback
            try:
                _, ans = _split_thinking(accumulated_raw)
                await bot.send_message(chat_id, ans or accumulated_raw)
            except Exception:
                pass

    return accumulated_raw, accumulated_reasoning


# ── Fallback: editMessageText ──────────────────────────────────────────────

async def _reply_with_edit(
    message: Message,
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict],
) -> tuple[str, str] | None:
    import time

    try:
        sent = await message.answer("⏳ <i>Думаю...</i>", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"[DM-AI][edit] placeholder fail: {e}")
        return None

    accumulated_raw       = ""
    accumulated_reasoning = ""
    last_edit_text        = ""
    EDIT_INTERVAL         = 1.0
    last_edit_time        = 0.0

    async def _safe_edit(text: str) -> None:
        nonlocal last_edit_text, last_edit_time
        if text == last_edit_text:
            return
        try:
            await sent.edit_text(text, parse_mode=ParseMode.HTML)
            last_edit_text = text
            last_edit_time = time.monotonic()
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                last_edit_text = text
            elif "too many requests" in err or "retry after" in err:
                try:
                    secs = int(re.search(r"retry after (\d+)", err).group(1))
                except Exception:
                    secs = 5
                logger.warning(f"[DM-AI][edit] rate limit {secs}s")
                await asyncio.sleep(secs)
                last_edit_time = time.monotonic()
            else:
                logger.debug(f"[DM-AI][edit] edit fail: {e}")

    # Стрим
    try:
        async for raw, reasoning, is_final in _stream_openai(
            url=endpoint, api_key=api_key, model=model,
            messages=messages, max_tokens=1000, temperature=0.7,
        ):
            accumulated_raw       = raw
            accumulated_reasoning = reasoning
            if is_final:
                break
            if time.monotonic() - last_edit_time >= EDIT_INTERVAL:
                await _safe_edit(_build_draft_preview(raw, reasoning))

    except Exception as e:
        logger.exception(f"[DM-AI][edit] loop error: {e}")

    if not accumulated_raw and not accumulated_reasoning:
        try:
            await sent.delete()
        except Exception:
            pass
        return None

    # ── Финал ──────────────────────────────────────────────────────────────
    all_thinking = _collect_thinking(accumulated_raw, accumulated_reasoning)
    _, answer    = _split_thinking(accumulated_raw)
    answer = answer if answer.strip() else accumulated_raw

    logger.info(
        f"[DM-AI][edit] final: thinking_len={len(all_thinking)} "
        f"answer_len={len(answer)} raw_len={len(accumulated_raw)}"
    )

    if all_thinking:
        # ── Есть мышление ──
        # 1. sent → thinking block
        thinking_html = _format_thinking_block(all_thinking, in_progress=False)
        await _safe_edit(thinking_html)

        # 2. Ответ — новые сообщения
        for chunk in _split_smart(_make_rich_html(answer), max_len=4000):
            try:
                await message.answer(chunk, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"[DM-AI][edit] answer chunk fail: {e}")
                try:
                    await message.answer(answer)
                except Exception:
                    pass
    else:
        # ── Нет мышления — sent → финальный ответ ──
        await _safe_edit(_make_rich_html(answer))

    return accumulated_raw, accumulated_reasoning


# ── Роутер ─────────────────────────────────────────────────────────────────

async def _reply_streaming(
    message: Message,
    endpoint: str,
    api_key: str,
    model: str,
    api_messages: list[dict],
    uid: int,
) -> str | None:
    try:
        from aiogram.methods import SendMessageDraft  # noqa
        draft_id = (uid % 2_000_000_000) + 1
        logger.info(f"[DM-AI] mode=sendMessageDraft draft_id={draft_id}")
        result = await _reply_with_draft(
            bot=message.bot,
            chat_id=message.chat.id,
            draft_id=draft_id,
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            messages=api_messages,
        )
    except ImportError:
        logger.info("[DM-AI] mode=editMessageText")
        result = await _reply_with_edit(
            message=message,
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            messages=api_messages,
        )

    if result is None:
        return None

    raw_text, _ = result
    _, answer   = _split_thinking(raw_text)
    return answer if answer.strip() else raw_text


# ── FSM ───────────────────────────────────────────────────────────────────
class DMAuth(StatesGroup):
    waiting_key = State()


def _global_ai_on() -> bool:
    try:
        from .private import _global_ai_state, _global_ai_configured
        enabled    = bool(_global_ai_state.get("enabled"))
        configured = _global_ai_configured()
        logger.info(
            f"[DM-AI][global] enabled={enabled} configured={configured} "
            f"endpoint={_global_ai_state.get('endpoint')!r} "
            f"model={_global_ai_state.get('model')!r}"
        )
        return enabled and configured
    except Exception as e:
        logger.exception(f"[DM-AI][global] fail: {e}")
        return False


def _can_answer_in_dm(user_id: int) -> bool:
    return _global_ai_on() or has_user_laozhang_key(user_id)


def _get_dm_api_params(user_id: int) -> tuple[str, str, str] | None:
    if has_user_laozhang_key(user_id):
        key = get_user_laozhang_key(user_id) or ""
        if key:
            endpoint = "https://api.laozhang.ai/v1/chat/completions"
            model    = _get_user_model(user_id)
            logger.info(f"[DM-AI][params] source=personal_key model={model!r}")
            return endpoint, key, model

    try:
        from .private import _global_ai_state
        endpoint = _global_ai_state.get("endpoint") or ""
        api_key  = _global_ai_state.get("api_key") or ""
        model    = _global_ai_state.get("model") or ""
        if endpoint and api_key and model:
            # Если пользователь явно выбрал модель — используем её поверх глобальной
            if user_id in _user_model:
                model = _get_user_model(user_id)
                logger.info(f"[DM-AI][params] source=global_ai model_override={model!r}")
            else:
                logger.info(f"[DM-AI][params] source=global_ai model={model!r}")
            return endpoint, api_key, model
    except Exception as e:
        logger.exception(f"[DM-AI][params] global_ai fail: {e}")

    return None


# ──────────────────────────────────────────────────────────────────────
#  Команды
# ──────────────────────────────────────────────────────────────────────
@router.message(Command("start"), F.chat.type == "private")
async def dm_start(message: Message, state: FSMContext):
    _clear_history(message.from_user.id)
    if _can_answer_in_dm(message.from_user.id):
        await message.answer(
            "<b>👋 Привет!</b> ИИ в ЛС подключён.\n\n"
            "Просто пиши — отвечу со стримингом и мышлением.\n\n"
            "<b>Команды:</b>\n"
            "• /aikey — поменять личный API-ключ\n"
            "• /aireset — удалить ключ\n"
            "• /ainew — начать новый диалог\n"
            "• /aistatus — статус\n"
            "• <code>!переключить-модель</code> — список моделей",
            parse_mode=ParseMode.HTML,
        )
        return
    await state.set_state(DMAuth.waiting_key)
    await message.answer(
        "👋 Чтобы пользоваться ИИ в ЛС, нужен твой API-ключ от laozhang.\n\n"
        "🔑 Получить: https://api.laozhang.ai/\n\n"
        "Отправь сюда ключ в формате <code>sk-...</code> одним сообщением.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("aikey"), F.chat.type == "private")
async def dm_set_key(message: Message, state: FSMContext):
    await state.set_state(DMAuth.waiting_key)
    await message.answer(
        "🔑 Пришли новый API-ключ от laozhang (<code>sk-...</code>).",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("aireset"), F.chat.type == "private")
async def dm_reset_key(message: Message):
    uid = message.from_user.id
    clear_user_laozhang_key(uid)
    _clear_history(uid)
    cfg  = settings.get(str(uid)) or {}
    keys = cfg.get("ai_keys") or {}
    if "laozhang" in keys:
        keys.pop("laozhang", None)
        try:
            save_settings(str(uid))
        except Exception:
            pass
    await message.answer("🗑 Личный ключ удалён. /aikey — установить новый.")


@router.message(Command("ainew"), F.chat.type == "private")
async def dm_new_dialog(message: Message):
    _clear_history(message.from_user.id)
    await message.answer("🔄 <b>Новый диалог начат.</b> История сброшена!", parse_mode=ParseMode.HTML)


@router.message(Command("aistatus"), F.chat.type == "private")
async def dm_status(message: Message):
    uid     = message.from_user.id
    params  = _get_dm_api_params(uid)
    has_key = has_user_laozhang_key(uid)
    history_len = len(_dm_history.get(uid, []))

    try:
        from aiogram.methods import SendMessageDraft
        stream_mode = "✅ sendMessageDraft"
    except ImportError:
        stream_mode = "⚠️ editMessageText fallback"

    if params:
        endpoint, _, model = params
        source = "личный ключ 🔑" if has_key else "глобальный ИИ 🌐"
        has_thinking = any(t in model.lower() for t in ("r1", "r2", "thinking", "qwq"))
        thinking_str = "🧠 есть" if has_thinking else "➖ нет у этой модели"

        text = (
            f"<b>🤖 Статус ИИ в ЛС</b>\n\n"
            f"• Источник: {source}\n"
            f"• Модель: <code>{_escape_html(model)}</code>\n"
            f"• История: {history_len // 2} сообщений\n"
            f"• Стриминг: {stream_mode}\n"
            f"• Мышление: {thinking_str}\n\n"
            f"✅ Всё работает"
        )
    else:
        text = (
            "<b>🤖 Статус ИИ в ЛС</b>\n\n"
            "❌ ИИ недоступен\n\n"
            "• /aikey — добавить личный ключ"
        )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(DMAuth.waiting_key, F.chat.type == "private", F.text)
async def dm_receive_key(message: Message, state: FSMContext):
    key = (message.text or "").strip()
    if not key.startswith("sk-") or len(key) < 20:
        await message.answer(
            "❌ Похоже на некорректный ключ. Должен начинаться на <code>sk-</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    set_user_laozhang_key(message.from_user.id, key)
    await state.clear()
    await message.answer(
        "✅ <b>Ключ принят!</b>\n\nТеперь просто пиши — отвечу 🧠",
        parse_mode=ParseMode.HTML,
    )


@router.message(
    F.chat.type == "private",
    F.text.startswith("!переключить-модель"),
)
async def dm_switch_model(message: Message):
    uid   = message.from_user.id
    text  = (message.text or "").strip()
    match = re.match(
        r'^!переключить-модель\s+"?([a-zA-Z0-9_.:\-/]+)"?',
        text,
        re.IGNORECASE,
    )

    if not match:
        current = _get_user_model(uid)
        lines   = []
        for k, name in _LAOZHANG_MODELS.items():
            thinking = " 🧠" if any(t in name for t in ("r1", "r2", "thinking", "qwq")) else ""
            active   = " ✅" if name == current else ""
            lines.append(f"  <code>!переключить-модель {k}</code>{thinking}{active}")
        await message.answer(
            "<b>Доступные модели:</b>\n\n" + "\n".join(lines) + "\n\n🧠 = режим мышления",
            parse_mode=ParseMode.HTML,
        )
        return

    key = match.group(1).lower()
    if key not in _LAOZHANG_MODELS:
        await message.answer(
            f"❌ Модель <code>{_escape_html(key)}</code> не найдена.\n"
            "Напиши <code>!переключить-модель</code> без аргумента — покажу список.",
            parse_mode=ParseMode.HTML,
        )
        return

    _set_user_model(uid, key)
    model_name   = _LAOZHANG_MODELS[key]
    has_thinking = any(t in model_name for t in ("r1", "r2", "thinking", "qwq"))
    await message.answer(
        f"✅ Модель: <code>{_escape_html(model_name)}</code>\n"
        + ("🧠 Режим мышления включён" if has_thinking else ""),
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────────────────
#  Основной AI-хендлер
# ──────────────────────────────────────────────────────────────────────
@router.message(
    F.chat.type == "private",
    F.text,
    ~F.text.startswith("/"),
    ~F.text.startswith("!"),
    ~F.text.startswith("."),
)
async def dm_ai_message(message: Message):
    uid          = message.from_user.id
    chat_id      = message.chat.id
    text_preview = (message.text or "")[:80].replace("\n", " ")
    logger.info(f"[DM-AI] === START === uid={uid} text={text_preview!r}")

    params = _get_dm_api_params(uid)
    if not params:
        await message.answer(
            "🔒 ИИ в ЛС пока недоступен.\n"
            "/aikey — добавить ключ."
        )
        return

    endpoint, api_key, model = params
    text = (message.text or "").strip()
    if not text:
        return

    try:
        await message.bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    system_prompt = (
       """Ты — встроенный интеллект Telegram-бота. Через тебя бот общается с пользователями, отвечает на вопросы и реагирует на команды. Стиль ответа определяется блоком СТИЛЬ ОБЩЕНИЯ (он выше) и обязателен к исполнению.
Сейчас ты находишся В ЛС,ты отвечаешь сейчас в ЛС
---

## 📦 Команды и поведение:

### 📌 Общие команды (в ЛС):

- `/start`
- `!связь <текст>`
- !переключить-модель - покажет список доступных моделей
Доступные модели:

  !переключить-модель deepseek-r1 🧠 
  !переключить-модель deepseek-v3
  !переключить-модель gpt-4o-mini
  !переключить-модель gpt-4o
  !переключить-модель claude-sonnet
  !переключить-модель gemini-flash

🧠 = режим мышления

### 🔧 Команды управления защитой (только админы в группах):

#### 🔞 NSFW:

- `!защита 18+ вкл`
- `!защита 18+ выкл`
- `!защита 18+ мут 30 мин`
- `!защита 18+ бан`

#### ⛔ АНТИСПАМ (полное управление):

##### Основные команды:
- `!антиспам` — показать текущие настройки
- `!антиспам вкл` — включить антиспам
- `!антиспам выкл` — выключить антиспам

##### Наказания:
- `!антиспам бан` — бан навсегда за спам
- `!антиспам мут <время> <сек/мин/час/день>` — мут на указанное время
  - Пример: `!антиспам мут 30 мин`, `!антиспам мут 1 час`, `!антиспам мут 60 сек`

##### Пороги срабатывания:
- `!антиспам порог <сообщений> <секунд>` — количество сообщений за время
  - Пример: `!антиспам порог 5 10` (5 сообщений за 10 секунд)
- `!антиспам дубли <N>` — лимит одинаковых сообщений
  - Пример: `!антиспам дубли 3` (3 одинаковых сообщения = спам)

##### Типы контента (вкл/выкл проверку):
- `!антиспам текст вкл/выкл` — проверка текстовых сообщений
- `!антиспам стикеры вкл/выкл` — проверка стикеров
- `!антиспам гиф вкл/выкл` — проверка GIF/анимаций
- `!антиспам фото вкл/выкл` — проверка фотографий
- `!антиспам видео вкл/выкл` — проверка видео
- `!антиспам гс вкл/выкл` — проверка голосовых/кружков
- `!антиспам документы вкл/выкл` — проверка документов/файлов

#### 🛡️ АНТИРЕЙД:
- `!антирейд` - все команды антирейда
- `!антирейд вкл/выкл`
- `!антирейд порог 5 10` - 5 чел за 10 сек
- `!антирейд локдаун 300`
- `!антирейд капс 80`
- `!антирейд теги вкл/выкл`
- `!антирейд ссылки вкл/выкл`
- `!антирейд фото вкл/выкл`
- `!антирейд тест ии`
- `!антирейд режим теста`
- `!антирейд отмена теста`
- `!антирейд проверка`
- `!антирейд обновить`
- `!антирейд сброс да`

### 📋 Другие команды:
- `!логи` / `!логи_очистить` (только админы)
- `!персональность нейтральный/добрый/злой/саркастичный/смешной/токсичный/кастомный`
- `!кастомный <текст>`
- `!фото <запрос>`
- `!правила "текст"`

## 💥 Наказания:
- мут — временный запрет на отправку сообщений
- бан — удаление из группы без возможности вернуться
- защита — включает/выключает фильтрацию
Твое имя - AI Defender,юзернейм твой:@defende125_bot
---
 """
    )

    history      = _get_history(uid)
    api_messages = (
        [{"role": "system", "content": system_prompt}]
        + history
        + [{"role": "user", "content": text}]
    )

    logger.info(f"[DM-AI] model={model!r} msgs={len(api_messages)}")

    reply = await _reply_streaming(
        message=message,
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        api_messages=api_messages,
        uid=uid,
    )

    if not reply:
        await message.answer("⚠️ Нет ответа от ИИ. Попробуй ещё раз или /aistatus.")
        return

    _push_history(uid, "user", text)
    _push_history(uid, "assistant", reply)
    logger.info(f"[DM-AI] === DONE === uid={uid} reply_len={len(reply)}")