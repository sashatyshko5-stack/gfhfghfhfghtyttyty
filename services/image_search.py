from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


# ---------- DuckDuckGo ----------
def _ddg_image_urls(query: str, max_results: int = 25) -> List[str]:
    """Получаем список URL картинок через duckduckgo_search (синхронно)."""
    try:
        from duckduckgo_search import DDGS  # lazy import
    except Exception as e:
        logger.warning(f"[image_search] duckduckgo_search недоступен: {e}")
        return []
    urls: List[str] = []
    try:
        with DDGS() as ddgs:
            it = ddgs.images(
                query,
                region="ru-ru",
                safesearch="off",
                size=None,
                type_image=None,
                layout=None,
                license_image=None,
                max_results=max_results,
            )
            for item in it or []:
                u = item.get("image") or item.get("thumbnail")
                if u and u.startswith("http"):
                    urls.append(u)
    except Exception as e:
        logger.warning(f"[image_search] DDG error: {e}")
    return urls


async def _ddg_image_urls_async(query: str, max_results: int = 25) -> List[str]:
    return await asyncio.to_thread(_ddg_image_urls, query, max_results)


# ---------- Bing fallback ----------
async def _bing_image_urls(session: aiohttp.ClientSession, query: str) -> List[str]:
    url = "https://www.bing.com/images/async"
    params = {"q": query, "first": "1", "count": "35", "mmasync": "1"}
    try:
        async with session.get(url, params=params, headers=_HEADERS, timeout=10) as r:
            if r.status != 200:
                return []
            html = await r.text()
    except Exception as e:
        logger.warning(f"[image_search] Bing error: {e}")
        return []
    # ищем murl="..."
    return list(dict.fromkeys(re.findall(r'murl&quot;:&quot;(https?://[^&]+?)&quot;', html)))


# ---------- Pixabay (резерв) ----------
async def _pixabay_urls(session: aiohttp.ClientSession, query: str) -> List[str]:
    try:
        from .ai_module import PIXABAY_API_KEY  # type: ignore
    except Exception:
        return []
    if not PIXABAY_API_KEY:
        return []
    params = {"key": PIXABAY_API_KEY, "q": query, "image_type": "photo", "per_page": 20, "lang": "ru"}
    try:
        async with session.get("https://pixabay.com/api/", params=params, timeout=10) as r:
            if r.status != 200:
                return []
            data = await r.json()
    except Exception as e:
        logger.warning(f"[image_search] Pixabay error: {e}")
        return []
    return [h.get("largeImageURL") for h in data.get("hits", []) if h.get("largeImageURL")]


# ---------- Загрузка одного URL ----------
async def _download(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url, headers=_HEADERS, timeout=15) as r:
            if r.status != 200:
                return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "image" not in ctype and not re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url, re.I):
                return None
            data = await r.read()
            if len(data) < 1024 or len(data) > 12 * 1024 * 1024:
                return None
            return data
    except Exception:
        return None


# ---------- Публичный API ----------
async def search_image(query: str) -> Tuple[Optional[bytes], str]:
    """Возвращает (image_bytes, source_url). Если ничего не нашли — (None, '')."""
    query = (query or "").strip()
    if not query:
        return None, ""

    # 1) DDG
    urls = await _ddg_image_urls_async(query, max_results=30)
    random.shuffle(urls)

    async with aiohttp.ClientSession() as s:
        # пробуем DDG
        for u in urls[:12]:
            img = await _download(s, u)
            if img:
                return img, u

        # 2) Bing
        bing_urls = await _bing_image_urls(s, query)
        random.shuffle(bing_urls)
        for u in bing_urls[:12]:
            img = await _download(s, u)
            if img:
                return img, u

        # 3) Pixabay
        px_urls = await _pixabay_urls(s, query)
        random.shuffle(px_urls)
        for u in px_urls[:8]:
            img = await _download(s, u)
            if img:
                return img, u

    return None, ""