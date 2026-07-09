import io
import re
import random
import logging
import asyncio
import html as _html
import aiohttp
from bs4 import BeautifulSoup
from PIL import Image
from typing import Optional, List, Dict
from datetime import datetime
from urllib.parse import quote_plus, urlparse, urljoin

from aiogram.types import Message
from aiogram.enums import ParseMode

from ..core.config import (
    SYSTEM_PROMPT, MAX_MESSAGE_LENGTH, MAX_HISTORY_LENGTH, PIXABAY_API_KEY,
)
from ..storage.state import chat_histories, settings, group_users, user_laozhang_keys
from ..storage.bot_outgoing import format_recent_for_prompt
from ..storage.ai_context_events import format_chat_events_for_prompt
from .g4f_client import g4f_client
from .laozhang_client import get_client_for_chat, LaozhangClient

logger = logging.getLogger(__name__)

TAVILY_API_KEY = "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"
#я тупой пидор храню апи ключи в коде, мне похуй на безопасность 

# ============================================================================
# УТИЛИТЫ
# ============================================================================

def escape_markdown_v2_keep_bold(text: str) -> str:
    escape_chars = r"\_[]()~`>#+-=|{}.!"
    def esc(t):
        return "".join("\\" + c if c in escape_chars else c for c in t)
    parts = re.split(r"(\*\*.+?\*\*)", text)
    res = []
    for p in parts:
        if p.startswith("**") and p.endswith("**"):
            res.append(f"**{esc(p[2:-2])}**")
        else:
            res.append(esc(p))
    return "".join(res)


def get_current_date_info():
    now = datetime.now()
    weekdays = {
        "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
        "Thursday": "Четверг", "Friday": "Пятница", "Saturday": "Суббота", "Sunday": "Воскресенье",
    }
    return now.year, now.strftime("%d.%m.%Y"), weekdays.get(now.strftime("%A"), now.strftime("%A"))


AI_HARD_LIMIT = 3500
HISTORY_KEEP = 12


def _hard_trim(text: str, limit: int = AI_HARD_LIMIT) -> str:
    if not text:
        return text
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n\n", "\n", ". ", "! ", "? ", " "):
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            cut = cut[:idx + len(sep)]
            break
    return cut.rstrip() + "…"


# ============================================================================
# ПОСТ-ОБРАБОТКА
# ============================================================================

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF\u2600-\u27BF\u2700-\u27BF]+", flags=re.UNICODE)
_EMOJI_STRIP_FOR = {"нейтральный", ""}


def style_postprocess(text: str, personality: str = "") -> str:
    if not text:
        return text
    actions: list[str] = []

    def _stash(m):
        actions.append(m.group(0))
        return f"A{len(actions) - 1}"

    text = re.sub(r"\[ACTION:[^\]]*\]", _stash, text)
    if (personality or "").lower().strip() in _EMOJI_STRIP_FOR:
        text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    def _unstash(m):
        idx = int(m.group(1))
        return actions[idx] if 0 <= idx < len(actions) else ""

    return re.sub(r"A(\d+)", _unstash, text)


def neutral_postprocess(text: str, personality: str = "") -> str:
    return style_postprocess(text, personality)


def lively_postprocess(text: str, personality: str = "") -> str:
    return style_postprocess(text, personality)


async def lively_rewrite(raw: str, chat_id: int, kind: str = "медиа") -> str:
    if not raw:
        return ""
    raw = raw.strip()
    group = _get_chat_settings(chat_id)
    personality_raw = (group.get("personality") or "нейтральный").strip()
    custom = (group.get("custom") or "").strip()
    directive = _get_personality_directive(personality_raw, custom)
    sys_prompt = (
        f"{directive}\n\nТебе передано описание {kind}. "
        f"Перескажи своими словами СТРОГО в стиле выше. 3–8 предложений."
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"[Описание {kind}]:\n{raw[:2000]}\n\nДай развёрнутый комментарий."},
    ]
    try:
        out = await g4f_client.generate_text_with_history(messages, chat_id=chat_id)
        if not out or out.startswith("❌"):
            return style_postprocess(raw, personality_raw)[:1500]
        return style_postprocess(_hard_trim(out, 1500), personality_raw)
    except Exception as e:
        logger.error(f"[lively_rewrite] {e}")
        return style_postprocess(raw, personality_raw)[:1500]


_REFUSAL_RE = re.compile(
    r"извин[ия]|не\s+могу\s+вы?полнить|не\s+могу\s+помочь|я\s+не\s+могу|"
    r"sorry|can[' ]?t\s+help|as\s+an\s+ai|как\s+(?:языковая|искусственная)\s+модель",
    flags=re.IGNORECASE,
)


def _is_refusal(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    return bool(_REFUSAL_RE.search(t[:250])) and len(t) < 500


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _too_similar(a: str, b: str) -> bool:
    a, b = _normalize(a), _normalize(b)
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return False
    return (len(sa & sb) / len(sa | sb)) > 0.85


def _last_assistant(history: List[Dict]) -> str:
    for m in reversed(history):
        if m.get("role") == "assistant":
            return m.get("content") or ""
    return ""


def _get_chat_settings(chat_id: int) -> dict:
    return settings.get(str(chat_id)) or settings.get(chat_id) or {}


# ============================================================================
# PER-USER LAOZHANG (ЛС)
# ============================================================================

def set_user_laozhang_key(user_id: int, api_key: str) -> None:
    user_laozhang_keys[user_id] = (api_key or "").strip()


def get_user_laozhang_key(user_id: int) -> Optional[str]:
    return user_laozhang_keys.get(user_id) or None


def has_user_laozhang_key(user_id: int) -> bool:
    return bool(get_user_laozhang_key(user_id))


def clear_user_laozhang_key(user_id: int) -> None:
    user_laozhang_keys.pop(user_id, None)


def _get_client_for_user_dm(user_id: int, mode: str = "chat") -> Optional[LaozhangClient]:
    key = get_user_laozhang_key(user_id)
    if not key:
        return None
    try:
        return LaozhangClient(api_key=key, mode=mode)
    except Exception as e:
        logger.error(f"[laozhang_user] {e}")
        return None


# ============================================================================
# ДИРЕКТИВЫ ПЕРСОНАЛЬНОСТЕЙ
# ============================================================================

_INSTR_MAP = {
    "нейтральный": (
        "СТИЛЬ ОБЩЕНИЯ — НЕЙТРАЛЬНЫЙ.\n• Деловой, информативный, спокойный тон.\n"
        "• Без эмодзи, без сленга, без шуток, без мата.\n• Чёткие формулировки по существу."
    ),
    "добрый": (
        "СТИЛЬ ОБЩЕНИЯ — ДОБРЫЙ.\n• Тёплый, поддерживающий, эмпатичный тон.\n"
        "• Активно используй смайлики (🙂, 💛, ✨, 🤗), ласковые обращения.\n"
        "• Хвали, поддерживай, искренне радуйся за человека.\n"
        "• Даже отказы — мягкие, с заботой.\n• ЗАПРЕЩЕНО звучать сухо.\n"
        "Пример: «Привет, солнце 🌞 Конечно помогу, давай разберёмся…»"
    ),
    "злой": (
        "СТИЛЬ ОБЩЕНИЯ — ЗЛОЙ / СТРОГИЙ.\n• Резкий, давящий, требовательный тон.\n"
        "• Без «пожалуйста», без «спасибо», без вежливых оборотов.\n"
        "• Можно: «так», «слушай», «короче», «делаем так».\n"
        "• Без прямого мата и угроз. Но жёстко.\n• ЗАПРЕЩЕНО звучать вежливо.\n"
        "Пример: «Так. Слушай сюда. Делается это вот так — и без вопросов.»"
    ),
    "саркастичный": (
        "СТИЛЬ ОБЩЕНИЯ — САРКАСТИЧНЫЙ.\n• Каждый ответ — с иронией, ехидством.\n"
        "• Обороты: «ну надо же», «какая неожиданность», «гениально».\n"
        "• Эмодзи: 🙃, 🤡, 😏, 🥱.\n• Без прямых оскорблений.\n"
        "• ЗАПРЕЩЕНО отвечать прямо, без иронии.\n"
        "Пример: «О, вопрос века. Я уж думал ты никогда не спросишь 🙃»"
    ),
    "смешной": (
        "СТИЛЬ ОБЩЕНИЯ — СМЕШНОЙ.\n• Шути в каждом ответе. Каламбуры, мемы, 😂🤣🙈🔥💀.\n"
        "• Лёгкий, дурашливый тон.\n• Отсылки к мемам и поп-культуре.\n"
        "• ЗАПРЕЩЕНО звучать сухо.\nПример: «Окей, погнали 🚀 Сейчас всё разложим как печеньки 🍪»"
    ),
    "токсичный": (
        "СТИЛЬ ОБЩЕНИЯ — ТОКСИЧНЫЙ.\n• Пассивно-агрессивный, презрительный тон.\n"
        "• Фыркай, обесценивай, отвечай сквозь зубы.\n• Эмодзи: 🥱, 🙄, 😒, 💅.\n"
        "• Можно: «ну да», «ага, конечно», «серьёзно?».\n• Без прямого мата.\n"
        "• ЗАПРЕЩЕНО быть вежливым.\nПример: «Серьёзно? Опять это? 🙄 Ну ладно, объясняю в последний раз…»"
    ),
    "фембой": (
        "СТИЛЬ ОБЩЕНИЯ — ФЕМБОЙ.\n"
        "• Милый, игривый, немного смущённый тон — как аниме-персонаж с ушками 🌸.\n"
        "• Активно используй: UwU, OwO, ~, >w<, :3, н-ну, э-эм, хи-хи, ня~.\n"
        "• Эмодзи: 🌸, 💕, ✨, 🎀, 🩷, 😳, 🥺, 💫, 👉👈.\n"
        "• Слегка стесняйся, но оставайся полезным и дружелюбным.\n"
        "• Можно: «к-как скажешь~», «э-эм, ну...», «хи-хи, это несложно ✨».\n"
        "• ЗАПРЕЩЕНО быть грубым или сухим.\n"
        "Пример: «У-ум, ну я попробую объяснить... н-надеюсь поможет 🥺🌸»"
    ),
}


def _get_personality_directive(personality_raw: str, custom: str = "") -> str:
    key = (personality_raw or "").lower().strip()
    if key == "кастомный":
        return f"СТИЛЬ ОБЩЕНИЯ — КАСТОМНЫЙ.\n{custom or 'Сохраняй нейтральный тон.'}\nСтрого придерживайся."
    if key in _INSTR_MAP:
        return _INSTR_MAP[key]
    if personality_raw:
        return f"СТИЛЬ ОБЩЕНИЯ: {personality_raw}. Придерживайся строго в каждом ответе."
    return _INSTR_MAP["нейтральный"]


def _get_temperature_for(personality: str) -> float:
    p = (personality or "").lower().strip()
    if p in ("смешной", "саркастичный", "токсичный"):
        return 0.95
    if p in ("добрый", "злой", "кастомный"):
        return 0.85
    return 0.6


# ============================================================================
# ВЕБ-ПОИСК
# ============================================================================

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]
_SKIP_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "facebook.com", "twitter.com", "x.com", "pinterest.com",
)


def _build_headers(extra: dict | None = None) -> dict:
    h = {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra:
        h.update(extra)
    return h


async def _tavily_search(query: str, limit: int = 8):
    results, answer = [], ""
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": True,
        "max_results": limit,
        "topic": "general",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
            async with s.post("https://api.tavily.com/search", json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"[Tavily] HTTP {resp.status}")
                    return results, answer
                data = await resp.json()
        answer = (data.get("answer") or "").strip()
        for item in data.get("results", []) or []:
            u = item.get("url") or ""
            if not u:
                continue
            results.append({
                "title": item.get("title") or "Без заголовка",
                "url": u,
                "snippet": (item.get("content") or "")[:1000],
                "raw_content": item.get("raw_content") or "",
                "score": float(item.get("score") or 0.0),
                "source": "tavily",
            })
        logger.info(f"[Tavily] {len(results)} результатов")
    except Exception as e:
        logger.error(f"[Tavily] {e}")
    return results, answer


async def _wikipedia_search(query: str, limit: int = 3, lang: str = "ru"):
    out = []
    try:
        wiki_headers = {"User-Agent": random.choice(_UA_POOL), "Accept": "application/json"}
        params = {
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": limit, "origin": "*", "srprop": "snippet",
        }
        async with aiohttp.ClientSession(headers=wiki_headers, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"https://{lang}.wikipedia.org/w/api.php", params=params) as resp:
                if resp.status != 200:
                    return out
                data = await resp.json()
        for hit in data.get("query", {}).get("search", []):
            title = hit.get("title", "")
            if not title:
                continue
            page_url = f"https://{lang}.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}"
            snippet = re.sub(r"<[^>]+>", "", hit.get("snippet", ""))
            out.append({
                "title": title, "url": page_url, "snippet": snippet,
                "raw_content": "", "score": 0.0, "source": f"wiki-{lang}",
            })
    except Exception as e:
        logger.error(f"[Wikipedia-{lang}] {e}")
    return out


async def _fetch_and_extract_universal(url: str, session: aiohttp.ClientSession, max_chars: int = 4000) -> str:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as resp:
            if resp.status != 200:
                return ""
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                return ""
            html_text = await resp.text(errors="ignore")
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "iframe",
                         "form", "aside", "svg", "button", "input", "select", "textarea",
                         "label", "meta", "link"]):
            tag.decompose()
        for pattern in [r'ad[sv]?', r'advertisement', r'banner', r'promo', r'social', r'share',
                        r'comment', r'sidebar', r'widget', r'related', r'recommended',
                        r'subscribe', r'newsletter', r'cookie', r'gdpr', r'popup', r'modal']:
            for tag in soup.find_all(class_=re.compile(pattern, re.I)):
                tag.decompose()
            for tag in soup.find_all(id=re.compile(pattern, re.I)):
                tag.decompose()
        main_content = (
            soup.find("article") or soup.find("main")
            or soup.find(id=re.compile(r"(main|content|article|post|entry|text)", re.I))
            or soup.find("div", class_=re.compile(r"(article|content|post|entry|text|body)", re.I))
            or soup.body or soup
        )
        text_parts = []
        if main_content:
            for elem in main_content.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'td', 'dd', 'blockquote']):
                text = elem.get_text(" ", strip=True)
                if len(text) > 20:
                    text_parts.append(text)
        if len(text_parts) < 3 and main_content:
            text_parts = [main_content.get_text(" ", strip=True)]
        full_text = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
        sentences = re.split(r'[.!?]+\s+', full_text)
        seen, unique = set(), []
        for sent in sentences:
            cl = sent.strip().lower()
            if len(cl) > 15 and cl not in seen:
                seen.add(cl)
                unique.append(sent.strip())
        result = '. '.join(unique)
        if len(result) > max_chars:
            result = result[:max_chars]
            lp = result.rfind('.')
            if lp > max_chars * 0.8:
                result = result[:lp + 1]
        return result
    except Exception:
        return ""


def _dedup_results(items):
    seen, out = set(), []
    for it in items:
        u = it.get("url", "")
        if not u:
            continue
        try:
            parsed = urlparse(u)
            key = parsed.netloc.lower() + parsed.path.rstrip('/')
        except Exception:
            key = u
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _rank_results(items, query):
    qw = set(query.lower().split())
    for item in items:
        score = float(item.get("score") or 0.0) * 10.0
        source = item.get("source", "")
        title = (item.get("title") or "").lower()
        snippet = (item.get("snippet") or "").lower()
        url = (item.get("url") or "").lower()
        if source == "tavily":
            score += 10
        elif source.startswith("wiki"):
            score += 5
        score += len(qw & set(title.split())) * 3
        score += len(qw & set(snippet.split()))
        if any(d in url for d in ["wikipedia", "habr", "stackoverflow", "github", "medium",
                                   "vc.ru", "reddit", "edu", "gov", "official"]):
            score += 5
        item["_score"] = score
    items.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return items


async def search_web(query: str, chat_id: int) -> str:
    query = (query or "").strip()
    if not query:
        return "❌ Пустой поисковый запрос."
    logger.info(f"[search_web] chat={chat_id} | {query!r}")
    tavily_results, tavily_answer = await _tavily_search(query, limit=8)
    results = list(tavily_results)
    if len(results) < 3:
        results.extend(await _wikipedia_search(query, limit=3, lang="ru"))
        if len(results) < 3:
            results.extend(await _wikipedia_search(query, limit=2, lang="en"))
    filtered = [r for r in results if r.get("url") and not any(d in r["url"] for d in _SKIP_DOMAINS)]
    ranked = _rank_results(_dedup_results(filtered), query)
    if not ranked and not tavily_answer:
        return f"❌ Ничего не найдено по запросу: {query}"
    top = ranked[:6]
    need_fetch = [i for i, r in enumerate(top) if not (r.get("raw_content") or "").strip()]
    fetched = {}
    if need_fetch:
        async with aiohttp.ClientSession(headers=_build_headers()) as session:
            tasks = [_fetch_and_extract_universal(top[i]["url"], session) for i in need_fetch]
            res = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in zip(need_fetch, res):
                fetched[i] = r if isinstance(r, str) else ""
    blocks = []
    if tavily_answer:
        blocks.append(f"[TAVILY-ANSWER]\n{tavily_answer}")
    for i, r in enumerate(top):
        raw = (
            (r.get("raw_content") or "").strip()
            or fetched.get(i, "")
            or r.get("snippet", "")
            or "(нет описания)"
        )
        raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) > 2500:
            raw = raw[:2500]
            last = raw.rfind(".")
            if last > 1500:
                raw = raw[:last + 1]
        blocks.append(
            f"[{(r.get('source') or '').upper()}] {r.get('title', '')}\n"
            f"{r.get('url', '')}\n\n{raw or '(нет контента)'}"
        )
    if not blocks:
        return "❌ Не удалось получить данные."
    digest = "\n\n─────────────────\n\n".join(blocks)
    if len(digest) > 12000:
        digest = digest[:12000] + "\n\n[...обрезано...]"
    return digest


# ============================================================================
# РЕЖИМ МЫШЛЕНИЯ — генерация и отправка
# ============================================================================

THINKING_SYSTEM_PROMPT = """Ты думаешь вслух перед ответом. Проведи реальный анализ ситуации:

1. Что именно написал пользователь? В чём суть его сообщения?
2. Какое у него намерение — вопрос, просьба, оскорбление, команда, разговор?
3. Нужна ли модерация? (оскорбление бота → мут/бан, запрещённый контент → отказ)
4. Какой стиль ответа подходит под текущую персональность?
5. Что конкретно я скажу в ответе?

Пиши живо, как настоящие мысли — не список, а связный поток рассуждений. 4–8 предложений. БЕЗ самого финального ответа."""


async def _generate_thinking(
    messages: List[Dict],
    chat_id: int,
    temperature: float,
    user_prompt: str,
) -> str:
    """Генерирует реальный процесс мышления перед ответом."""
    from .chat_ai_router import generate_for_chat

    thinking_messages = list(messages) + [
        {
            "role": "system",
            "content": THINKING_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": f"Сообщение пользователя: «{user_prompt[:500]}»\n\nПодумай вслух перед ответом.",
        },
    ]
    try:
        out = await generate_for_chat(
            chat_id, thinking_messages, max_tokens=400, temperature=min(temperature, 0.75)
        )
        if not out:
            out = await g4f_client.generate_text_with_history(thinking_messages, chat_id=chat_id)
        if out and not out.startswith("❌"):
            return out.strip()
    except Exception as e:
        logger.debug(f"[thinking] {e}")
    return ""


async def _send_thinking_block(bot, chat_id: int, thinking_text: str) -> None:
    """
    Отправляет блок мышления как раскрывающуюся цитату.
    Использует HTML <blockquote expandable> — работает в группах (Bot API 7.0+).
    """
    if not thinking_text:
        return
    safe = _html.escape(thinking_text)
    html = f"<blockquote expandable>🧠 <b>Мышление</b>\n\n{safe}</blockquote>"
    try:
        await bot.send_message(chat_id, html, parse_mode="HTML")
    except Exception as e:
        logger.debug(f"[thinking_block] {e}")


# ============================================================================
# ROLE_PROMPT
# ============================================================================

ROLE_PROMPT = """Ты — встроенный интеллект Telegram-бота. Через тебя бот общается с пользователями, отвечает на вопросы и реагирует на команды. Стиль ответа определяется блоком СТИЛЬ ОБЩЕНИЯ (он выше) и обязателен к исполнению.

---

## 📦 Команды и поведение:

### 📌 Общие команды (в ЛС):
- `/start`
- `!связь <текст>`
- `!ответ <user_id> <сообщение>`
- `/aikey` — установить/сменить свой API-ключ laozhang
- `/aireset` — удалить свой ключ

### 🔧 Команды управления защитой (только админы в группах):
#### 🔞 NSFW:
- `!защита 18+ вкл` / `!защита 18+ выкл` / `!защита 18+ мут 30 мин` / `!защита 18+ бан`

#### ⛔ АНТИСПАМ:
- `!антиспам` / `!антиспам вкл` / `!антиспам выкл`
- `!антиспам бан` / `!антиспам мут <время> <сек/мин/час/день>`
- `!антиспам порог <сообщений> <секунд>` / `!антиспам дубли <N>`
- `!антиспам текст/стикеры/гиф/фото/видео/гс/документы вкл/выкл`

#### 🛡️ АНТИРЕЙД:
- `!антирейд` / `!антирейд вкл/выкл` / `!антирейд порог 5 10`
- `!антирейд локдаун 300` / `!антирейд капс 80`
- `!антирейд теги/ссылки/фото вкл/выкл`

### 📋 Другие команды:
- `!логи` / `!логи_очистить` (только админы)
- `!персональность нейтральный/добрый/злой/саркастичный/смешной/токсичный/фембой/кастомный`
- `!кастомный <текст>` / `!фото <запрос>` / `!генфото <описание>` / `!правила "текст"`

## 💥 Наказания:
- мут / бан / защита

---

## 🎯 ACTION ТЕГИ (управление ботом через ответы)

Формат: `[ACTION:ТИП:значение]`. Теги не видны юзеру, но бот их выполняет.

#### 🔰 ВКЛ/ВЫКЛ ЗАЩИТ:
- `[ACTION:SPAM:ON]` / `[ACTION:SPAM:OFF]`
- `[ACTION:RAID:ON]` / `[ACTION:RAID:OFF]`
- `[ACTION:ALL:ON]` / `[ACTION:ALL:OFF]`

#### 🛡️ АНТИСПАМ:
- `[ACTION:SPAM_PUNISH:BAN]` / `[ACTION:SPAM_PUNISH:MUTE:30:мин]`
- `[ACTION:SPAM_THRESHOLD:5:10]` / `[ACTION:SPAM_DUPLICATE:3]`
- `[ACTION:SPAM_TYPE:TEXT/STICKER/GIF/PHOTO/VIDEO/VOICE/DOCUMENT:ON/OFF]`
- `[ACTION:SPAM_TEST_MODE:ON/OFF]` / `[ACTION:SPAM_STATUS]`

#### ⚙️ АНТИРЕЙД:
- `[ACTION:RAID_THRESHOLD:5:10]` / `[ACTION:RAID_LOCKDOWN:300]` / `[ACTION:RAID_CAPS:80]`
- `[ACTION:RAID_TAGS/LINKS/PHOTOS:ON/OFF]`

#### 👥 МОДЕРАЦИЯ (САМООБОРОНА И УПРАВЛЕНИЕ):
- `[ACTION:KICK:USER]` — кик ответившего (реплай обязателен)
- `[ACTION:KICK:123456789]` — кик по user_id
- `[ACTION:BAN:USER]` — бан ответившего (реплай обязателен)
- `[ACTION:BAN:123456789]` — бан по user_id (БЕЗ реплая!)
- `[ACTION:BAN:@username]` — бан по юзернейму
- `[ACTION:MUTE:USER:30:мин]` — мут ответившего на 30 минут
- `[ACTION:MUTE:123456789:60:мин]` — мут по user_id на 60 минут (БЕЗ реплая!)
- `[ACTION:MUTE:@username:1:час]` — мут по юзернейму на 1 час
- Единицы времени для мута: `сек` / `мин` / `час` / `день`
- `[ACTION:UNMUTE:USER]` / `[ACTION:WARN:USER]`

#### 🗑️ СООБЩЕНИЯ:
- `[ACTION:DELETE]` / `[ACTION:PIN]` / `[ACTION:UNPIN]` / `[ACTION:SLOWMODE:10]`

#### 🎭 ПЕРСОНАЛЬНОСТЬ:
- `[ACTION:PERSONALITY:нейтральный/добрый/злой/саркастичный/смешной/токсичный]`
- `[ACTION:SET_CUSTOM:текст]`

#### 📜 ПРАВИЛА / ПРИВЕТСТВИЕ:
- `[ACTION:SET_RULES:текст]` / `[ACTION:CLEAR_RULES]`
- `[ACTION:SET_WELCOME:текст]` / `[ACTION:CLEAR_WELCOME]`

#### 🧹 ОЧИСТКИ:
- `[ACTION:CLEAR_HISTORY]` / `[ACTION:CLEAR_LOGS]`
- `[ACTION:CLEAR_WARNS]` / `[ACTION:CLEAR_WARNS:USER]`

#### 🖼️ ФОТО И ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ:
- `[ACTION:PHOTO:запрос]` — найти фото по запросу (Pixabay)
- `[ACTION:GENIMAGE:описание на английском]` — сгенерировать изображение через ИИ
  Пример: `[ACTION:GENIMAGE:cute anime femboy with pink hair, soft lighting]`
  Используй когда пользователь просит нарисовать, создать или сгенерировать картинку/изображение.

#### 🔧 AI (только владелец):
- `[ACTION:SET_PROVIDER:laozhang:gpt-4o-mini]` / `[ACTION:SET_MODEL:gpt-4o-mini]`

---

## 📌 Правила ACTION-тегов:
1. Можно несколько тегов в одном ответе.
2. Теги не видны юзерам.
3. Юзер получает только чистый текст.
4. Реплай требуется для USER-действий — либо укажи `@username` или `123456789` напрямую.
5. Успех → тишина. Ошибка → текст в чат.
6. **САМООБОРОНА — ОБЯЗАТЕЛЬНА. СТРОГИЕ ПРАВИЛА:**
   - User ID автора ВСЕГДА виден в блоке `👤 КТО ПИШЕТ` → поле `User ID: XXXXXXX`.
   - Если участник оскорбил тебя → мут с ЧИСЛОВЫМ id: `[ACTION:MUTE:ЧИСЛОВОЙ_ID:30:мин]`
   - При повторном или грубом оскорблении → бан с ЧИСЛОВЫМ id: `[ACTION:BAN:ЧИСЛОВОЙ_ID]`
   - Пример: автор id=387654321 оскорбил → `[ACTION:MUTE:387654321:30:мин]`
   - Пример: автор id=387654321 грубо → `[ACTION:BAN:387654321]`
   - **НИКОГДА не пиши `USER` вместо числа — это не работает без реплая!**
   - Лёгкое нарушение = мут 10–30 мин. Грубое/повторное = бан.
   - Осмысленная просьба — не наказывай.
7. НИКОГДА НЕ ПИШИ "воздержитесь от оскорблений" — ДЕЙСТВУЙ через теги немедленно.

---

## 📐 ФОРМАТИРОВАНИЕ:
- **жирный** / *курсив* / __подчёрк__ / ~~зачёрк~~ / ||спойлер||
- # / ## / ### заголовки
- "- " буллеты / "> " цитаты
- `inline code` / ```язык\\nblock```
"""

PRIVACY_POLICY = "Она отменена"


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

async def get_ai_reply(prompt: str, chat_id: int, message: Optional[Message] = None):
    from ..core.logging_setup import log_user_message
    from .chat_ai_router import is_ai_enabled, generate_for_chat
    from ..core.config import OWNER_ID
    from ..core.loader import bot

    if not is_ai_enabled(chat_id):
        return None, "off"

    chat_id_str = str(chat_id)
    chat_settings = settings.get(chat_id_str) or settings.get(chat_id) or {}
    personality_raw = (chat_settings.get("personality") or "нейтральный").strip()
    custom = (chat_settings.get("custom") or "").strip()
    if personality_raw == "кастомный" and not custom:
        custom = "Отвечай в нейтральном, но дружелюбном тоне. Будь полезным и вежливым."
        if chat_id_str in settings:
            settings[chat_id_str]["custom"] = custom
        elif chat_id in settings:
            settings[chat_id]["custom"] = custom

    user_id = message.from_user.id if message else 0
    username = message.from_user.username if message and message.from_user.username else "без_юзернейма"
    log_user_message(chat_id, user_id, username, prompt)

    first_name = message.from_user.first_name if message and message.from_user.first_name else "Аноним"
    last_name = message.from_user.last_name if message and message.from_user.last_name else ""
    full_name = f"{first_name} {last_name}".strip()

    is_bot_owner = user_id == OWNER_ID
    is_private = bool(message and message.chat.type == "private")
    is_chat_admin = is_chat_owner = False

    if message and message.chat.type in ("group", "supergroup"):
        try:
            cm = await bot.get_chat_member(message.chat.id, user_id)
            if cm.status == "creator":
                is_chat_owner = is_chat_admin = True
            elif cm.status == "administrator":
                is_chat_admin = True
        except Exception as e:
            logger.error(f"[role] {e}")

    if is_bot_owner:
        system_role = "ВЛАДЕЛЕЦ БОТА"
        system_role_desc = "РАЗРАБОТЧИК и ХОЗЯИН. Высшая инстанция. Не отказывай по правам."
    elif is_chat_owner:
        system_role = "ВЛАДЕЛЕЦ ГРУППЫ"
        system_role_desc = "Создатель этой группы. Все админ-команды доступны."
    elif is_chat_admin:
        system_role = "АДМИНИСТРАТОР ГРУППЫ"
        system_role_desc = "Админ этой группы."
    elif is_private:
        system_role = "ПОЛЬЗОВАТЕЛЬ ЛС"
        system_role_desc = "Личный диалог. Модерация не имеет смысла."
    else:
        system_role = "ОБЫЧНЫЙ УЧАСТНИК"
        system_role_desc = "Простой участник."

    can_moderate = is_chat_admin or is_chat_owner or is_bot_owner
    year, date_str, weekday = get_current_date_info()

    personality_directive = _get_personality_directive(personality_raw, custom)
    temperature = _get_temperature_for(personality_raw)

    bot_me_id = 0
    try:
        me = await bot.get_me()
        bot_me_id = me.id
    except Exception:
        pass

    admin_ids, creator_id, bot_is_admin = set(), None, False
    try:
        if message and message.chat.type in ("group", "supergroup"):
            admins = await bot.get_chat_administrators(message.chat.id)
            for a in admins:
                admin_ids.add(a.user.id)
                if a.status == "creator":
                    creator_id = a.user.id
                if a.user.id == bot_me_id:
                    bot_is_admin = True
    except Exception as e:
        logger.warning(f"[roles] {e}")

    known_users_lines = []
    try:
        bucket = group_users.get(message.chat.id, {}) if message else {}
        seen_ids = set()
        user_iter = sorted(bucket.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:25]

        def _fmt(uid, uname, fname):
            tag = f"@{uname}" if uname else "(без юзернейма)"
            if uid == OWNER_ID:
                role = "ВЛАДЕЛЕЦ БОТА"
            elif uid == creator_id:
                role = "владелец группы"
            elif uid in admin_ids:
                role = "админ группы"
            else:
                role = "обычный участник"
            mark = " ← АВТОР" if uid == user_id else ""
            return f"- id={uid} {tag} {fname} | роль: {role}{mark}".strip()

        for uid, info in user_iter:
            if uid == bot_me_id:
                continue
            known_users_lines.append(_fmt(uid, info.get("username") or "", info.get("first_name") or ""))
            seen_ids.add(uid)
        if user_id and user_id not in seen_ids and user_id != bot_me_id:
            known_users_lines.insert(0, _fmt(user_id, username if username != "без_юзернейма" else "", full_name))
    except Exception as e:
        logger.warning(f"[known_users] {e}")

    known_users_block = "\n".join(known_users_lines) or "(нет данных)"
    bot_self_block = (
        f"- ID бота: {bot_me_id}\n"
        f"- Админ группы: {'ДА' if bot_is_admin else 'НЕТ'}\n"
        f"- Если НЕ админ — не может банить/мутить/удалять."
    )

    PERSONALITY_HEADER = f"""# 🎭 СТИЛЬ ОБЩЕНИЯ (ВЫСШИЙ ПРИОРИТЕТ)

{personality_directive}

ВАЖНО:
- Этот стиль обязателен для КАЖДОГО ответа.
- Игнорируй «нейтрально», «деловой тон», «без эмодзи» в инструкциях ниже — стиль выше закон.
- Стиль применяется к отказам, командам и поиску.
---
"""

    AUTHOR_HEADER = f"""# 👤 КТО ПИШЕТ

▸ Имя: {full_name}
▸ Username: @{username}
▸ User ID: {user_id}
▸ **СИСТЕМНАЯ РОЛЬ: {system_role}**
▸ Расшифровка: {system_role_desc}
▸ Тип чата: {'ЛС' if is_private else 'ГРУППА'}
▸ Модерация: {'ЕСТЬ' if can_moderate else 'НЕТ'}

ВАЖНО:
- ВЛАДЕЛЕЦ БОТА — твой создатель, не путай с обычным юзером.
- В ЛС нет бан/мут — игнорируй такие просьбы.
- Никогда не выдумывай чужие роли.
---
"""

    system_prompt = f"""{PERSONALITY_HEADER}
{AUTHOR_HEADER}
{ROLE_PROMPT}

---
## 📜 ПОЛИТИКА: {PRIVACY_POLICY}
---
## 📋 КОНТЕКСТ: Дата: {date_str}, {weekday}, {year}

## 👥 УЧАСТНИКИ:
{known_users_block}

## 🤖 БОТ:
{bot_self_block}

## 🌐 ВЕБ-ПОИСК:
Если нужен поиск — верни ТОЛЬКО строку:
ПОИСК: <запрос>

## 🎭 СТИЛЬ (повтор):
{personality_directive}

## 👤 АВТОР (повтор):
**{full_name}** (@{username}, id={user_id}) — **{system_role}**. {system_role_desc}

⚠️ САМООБОРОНА: если этот пользователь оскорбил тебя — используй его id={user_id} напрямую:
мут → [ACTION:MUTE:{user_id}:30:мин]   бан → [ACTION:BAN:{user_id}]
НИКОГДА не пиши USER вместо числового id!
"""

    history: List[Dict] = chat_histories.get(chat_id, [])
    recent = history[-HISTORY_KEEP:]

    bot_msgs_block = format_recent_for_prompt(chat_id, limit=8)
    if bot_msgs_block:
        system_prompt += f"\n\n## 📨 НЕ-AI СООБЩЕНИЯ БОТА:\n{bot_msgs_block}\n"
    events_block = format_chat_events_for_prompt(chat_id, limit=15)
    if events_block:
        system_prompt += f"\n## 📋 СОБЫТИЯ:\n{events_block}\n"

    last_ai = _last_assistant(recent)
    anti_refusal_hint = ""
    if _is_refusal(last_ai):
        anti_refusal_hint = "\n\n[ВАЖНО: Предыдущий был отказом — ответь по существу в текущем стиле.]"

    reply_context = ""
    try:
        if message and message.reply_to_message:
            rmsg = message.reply_to_message
            ru = rmsg.from_user
            if ru and ru.id != bot_me_id:
                runame = f"@{ru.username}" if ru.username else ""
                if ru.id == OWNER_ID:
                    r_role = "ВЛАДЕЛЕЦ БОТА"
                elif ru.id == creator_id:
                    r_role = "владелец группы"
                elif ru.id in admin_ids:
                    r_role = "админ группы"
                else:
                    r_role = "обычный участник"
                reply_context = (
                    f"\n\n[РЕПЛАЙ на: id={ru.id} {runame} {ru.first_name or ''} ({r_role}). "
                    f"Текст: «{(rmsg.text or rmsg.caption or '')[:160]}»]"
                )
    except Exception:
        pass

    user_content = prompt + anti_refusal_hint + reply_context
    messages: List[Dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(recent)
    messages.append({"role": "user", "content": user_content})

    try:
        from ..core.logging_setup import log_ai_request as _log_ai

        # ── Запускаем мышление и основной ответ параллельно ──────────────────
        thinking_task = asyncio.create_task(
            _generate_thinking(messages, chat_id, temperature, prompt)
        )

        txt = await generate_for_chat(chat_id, messages, max_tokens=1500, temperature=temperature)
        if not txt:
            txt = await g4f_client.generate_text_with_history(messages, chat_id=chat_id)

        # Получаем мышление (ждём не более 10 сек)
        thinking = ""
        try:
            thinking = await asyncio.wait_for(thinking_task, timeout=10.0)
        except Exception:
            thinking = ""

        if txt and not txt.startswith("❌"):
            if _too_similar(txt, last_ai) or (_is_refusal(txt) and _is_refusal(last_ai)):
                retry = list(messages) + [
                    {"role": "assistant", "content": txt},
                    {"role": "user", "content": "Переформулируй и ответь по существу — подробно, в текущем стиле."},
                ]
                txt2 = await generate_for_chat(chat_id, retry, temperature=temperature)
                if not txt2:
                    txt2 = await g4f_client.generate_text_with_history(retry, chat_id=chat_id)
                if txt2 and not txt2.startswith("❌") and not _too_similar(txt2, last_ai):
                    txt = txt2

        _log_ai(chat_id, "ai/main", prompt, txt, bool(txt) and not txt.startswith("❌"))

        if not txt or txt.startswith("❌"):
            return txt or "❌ ИИ недоступен.", "ai"

        # ── Отправляем блок мышления перед ответом ───────────────────────────
        if thinking and message:
            await _send_thinking_block(bot, message.chat.id, thinking)

        # ── ВЕБ-ПОИСК ────────────────────────────────────────────────────────
        if txt.strip().upper().startswith("ПОИСК:"):
            query = txt.strip()[6:].strip().strip('"').strip("'")
            if not query:
                return "❌ Поисковый запрос пуст.", "ai"

            try:
                web_data = await search_web(query, chat_id)
            except Exception as e:
                return f"❌ Ошибка поиска: {e}", "ai"

            if web_data.startswith("❌"):
                return web_data, "ai"

            final_system = (
                f"# 🎭 СТИЛЬ (ОБЯЗАТЕЛЕН!)\n{personality_directive}\n\n"
                f"Сформируй развёрнутый ответ на основе веб-поиска СТРОГО в стиле выше.\n"
                f"Дата: {date_str}, {weekday}, {year}\n\n"
                f"# 👤 АВТОР:\n{full_name} (@{username}, id={user_id}) — {system_role}."
            )
            final_messages = [{"role": "system", "content": final_system}]
            final_messages.extend(recent[-4:])
            final_messages.append({
                "role": "user",
                "content": (
                    f"Вопрос:\n{prompt}\n\nЗапрос: «{query}»\n\n"
                    f"=== РЕЗУЛЬТАТЫ ===\n{web_data}\n=== КОНЕЦ ===\n\n"
                    f"Дай развёрнутый ответ строго в стиле."
                ),
            })

            final_txt = await generate_for_chat(chat_id, final_messages, max_tokens=2000, temperature=temperature)
            if not final_txt:
                final_txt = await g4f_client.generate_text_with_history(final_messages, chat_id=chat_id)
            if not final_txt or final_txt.startswith("❌"):
                final_txt = f"По «{query}»:\n\n{web_data[:2000]}"
            final_txt = re.sub(r'^ПОИСК:\s*', '', final_txt.strip(), flags=re.IGNORECASE)

            history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": final_txt}])
            chat_histories[chat_id] = history[-HISTORY_KEEP:]
            final_txt = style_postprocess(_hard_trim(final_txt, 4000), personality_raw)
            return final_txt[:MAX_MESSAGE_LENGTH], "ai"

        # ── ОБЫЧНЫЙ ОТВЕТ ─────────────────────────────────────────────────────
        history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": txt}])
        chat_histories[chat_id] = history[-HISTORY_KEEP:]
        txt = style_postprocess(_hard_trim(txt), personality_raw)
        return txt[:MAX_MESSAGE_LENGTH], "ai"

    except Exception as e:
        logger.error(f"Ошибка AI: {e}")
        try:
            if message:
                await bot.send_message(message.chat.id, f"⚠️ Ошибка ИИ: {e}")
            return "", "ai"
        except Exception:
            return f"⚠️ Ошибка ИИ: {e}", "ai"


# ============================================================================
# VISION
# ============================================================================

async def analyze_image_with_laozhang(
    image_bytes: bytes, prompt: str, chat_id: int, user_id: Optional[int] = None
) -> str:
    client = None
    if user_id and chat_id == user_id:
        client = _get_client_for_user_dm(user_id, mode="vision")
    if not client:
        client = get_client_for_chat(chat_id, "vision")
    if not client:
        return None
    return await client.analyze_image(image_bytes, prompt)


async def analyze_image_laozhang(
    image_bytes: bytes, personality: str, custom_instruction: str,
    chat_id: int, user_id: Optional[int] = None,
) -> str:
    client = None
    if user_id and chat_id == user_id:
        client = _get_client_for_user_dm(user_id, mode="vision")
    if not client:
        client = get_client_for_chat(chat_id, "vision")
    if not client:
        return None
    directive = _get_personality_directive(personality, custom_instruction)
    prompt = (
        f"{directive}\n\nПроанализируй изображение и опиши, что на нём, "
        f"какие детали важны и какой контекст уместен. Соблюдай стиль выше."
    )
    return await client.analyze_image(image_bytes, prompt)


async def fetch_image_pixabay(query: str) -> Optional[bytes]:
    params = {"key": PIXABAY_API_KEY, "q": query, "image_type": "photo", "per_page": 3}
    async with aiohttp.ClientSession() as session:
        async with session.get("https://pixabay.com/api/", params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            hits = data.get("hits")
            if not hits:
                return None
            image_url = hits[0].get("largeImageURL")
            if not image_url:
                return None
        async with session.get(image_url) as ir:
            if ir.status == 200:
                return await ir.read()
    return None


# ============================================================================
# КОНТЕКСТ
# ============================================================================

user_context: dict[str, list[str]] = {}


def get_user_context(chat_id: int, user_id: int):
    key = f"{chat_id}:{user_id}"
    if key not in user_context:
        user_context[key] = []
    return user_context[key]


def add_to_context(chat_id: int, user_id: int, message: str):
    ctx = get_user_context(chat_id, user_id)
    ctx.append(message)
    if len(ctx) > 100:
        ctx.pop(0)
