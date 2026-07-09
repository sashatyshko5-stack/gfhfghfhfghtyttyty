import logging
import re
from urllib.parse import quote

import aiohttp
from aiogram import Router, F
from aiogram.types import Message, BufferedInputFile

from ..core.loader import bot

logger = logging.getLogger(__name__)
router = Router()

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}?width=1024&height=1024&nologo=true&enhance=true&model=flux"


async def generate_image_pollinations(prompt: str) -> bytes | None:
    encoded = quote(prompt)
    url = POLLINATIONS_URL.format(prompt=encoded)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    ct = resp.headers.get("content-type", "")
                    if "image" in ct:
                        return await resp.read()
        return None
    except Exception as e:
        logger.warning(f"[IMAGEGEN] Pollinations error: {e}")
        return None


@router.message(F.text.regexp(r"^[!.](генфото|genphoto|genimage|рисуй)(\s+.+)?$"))
async def cmd_gen_image(message: Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await message.reply(
            "🖼 <b>Генерация изображения</b>\n\n"
            "Использование: <code>!генфото &lt;описание на любом языке&gt;</code>\n\n"
            "Пример: <code>!генфото аниме фембой с розовыми волосами</code>",
            parse_mode="HTML",
        )

    prompt = parts[1].strip()
    await bot.send_chat_action(message.chat.id, action="upload_photo")
    waiting = await message.reply("🎨 Генерирую изображение…")

    image_bytes = await generate_image_pollinations(prompt)

    try:
        await waiting.delete()
    except Exception:
        pass

    if not image_bytes:
        return await message.reply("❌ Не удалось сгенерировать изображение. Попробуйте другой запрос.")

    try:
        await message.reply_photo(
            BufferedInputFile(image_bytes, filename="generated.jpg"),
            caption=f"🖼 <b>Сгенерировано:</b> {prompt[:200]}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"[IMAGEGEN] send photo error: {e}")
        await message.reply("❌ Ошибка отправки изображения.")
