
from aiogram import Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.enums import ChatType
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.types import BufferedInputFile
import html
from ..storage.state import settings, save_settings
from ..core.utils import is_admin
from ..services.ai_module import fetch_image_pixabay
from ..services.chat_ai_router import (
    set_chat_provider_and_model,
    set_chat_api_key,
    set_chat_custom_provider,
    get_chat_ai_status,
    format_providers_list,
    format_models_list,
    normalize_provider,
)

router = Router()

_BOT_GUIDE = "https://teletype.in/@chelik01/jOToRQLsy8m"
_AI_GUIDE = "https://telegra.ph/POLNYJ-GAJD-PO-PRAVILNOJ-NASTROJKE-II-05-31"
_CHANNEL = "https://t.me/AiDefender_125"

_bot_username_cache: str | None = None


async def _get_bot_username(message: Message) -> str:
    global _bot_username_cache
    if not _bot_username_cache:
        me = await message.bot.get_me()
        _bot_username_cache = me.username or "bot"
    return _bot_username_cache


@router.message(Command("help"))
@router.message(Command("commands"))
async def commands_list_slash(message: Message):
    await message.reply(
        "Основные команды:\n\n"
        "`!антиспам вкл/выкл`\n"
        "`!антирейд` — команды антирейда\n"
        "`!список вкл/выкл` — включить глобальный чс рейдеров\n"
        "`!правила <текст>` — правила для новеньких\n"
        "`!фото <запрос>` — поиск фото\n\n"
        "🤖 **AI:**\n"

        "`!ии вкл/выкл` — общение ИИ в чате\n"
        "`!провайдеры` — список провайдеров\n"
        "`!модели <провайдер>` — список моделей провайдера\n"
        "`!модель <модель> <провайдер>` — переключить модель и провайдера\n"
        "`!модель кастом` — кастомный провайдер\n"
        "`!ключ <провайдер> <api_key>` — задать API-ключ\n"
        "`!кастом_провайдер <endpoint> <api_key> <модель>` — задать кастомного провайдера\n"
        "`!статус` — статус AI в этом чате\n\n"
        "`!ии-модер` — Ии-модератор\n\n"
        "`!антишлюхобот` - список команд антишлюхобота\n\n"
        "Префикс `.` тоже поддерживается.\n",
        parse_mode="Markdown",
    )


@router.message(F.text.in_({"!команды", ".команды"}))
async def commands_list(message: Message):
    await commands_list_slash(message)


@router.message(CommandStart())
async def start_command(message: Message, command: CommandObject = None):
    if command and command.args:
        args = command.args.strip()
        if args.startswith("setup_") or args.startswith("setup"):
            return
    if message.chat.type == ChatType.PRIVATE:
        username = await _get_bot_username(message)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить в группу",
                    url=f"https://t.me/{username}?startgroup=true",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Премиум тарифы",
                    callback_data="start:premium",
                ),
                InlineKeyboardButton(
                    text="📩 Обратная связь",
                    callback_data="start:feedback",
                ),
            ],
            [
                InlineKeyboardButton(text="📖 Гайд по боту", url=_BOT_GUIDE),
                InlineKeyboardButton(text="🤖 Гайд по ИИ", url=_AI_GUIDE),
            ],
        ])
        await message.answer(
            "👋 Привет! Я <b>AI Defender</b> — умею защищать от рейдов, спама и шлюхоботов.\n\n"
            "Напиши <code>!команды</code>, чтобы увидеть список команд.\n"
            "Для премиум-функций — нажми <b>«Премиум тарифы»</b> 👇",
            reply_markup=kb,
            parse_mode="HTML",
        )


@router.callback_query(F.data == "start:premium")
async def cb_premium_menu(cb: CallbackQuery):
    from ..handlers.premium_handler import _main_menu_text, _main_menu_kb
    await cb.message.answer(
        _main_menu_text(),
        reply_markup=_main_menu_kb(),
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(F.data == "start:feedback")
async def cb_feedback(cb: CallbackQuery):
    await cb.message.answer(
        "📩 <b>Обратная связь</b>\n\n"
        "Напиши следующим сообщением:\n"
        "<code>!связь &lt;текст вопроса&gt;</code>\n\n"
        "Можно прикрепить фото, видео или документ.\n\n"
        "<i>Пример:</i> <code>!связь Бот не реагирует на команду !антиспам</code>",
        parse_mode="HTML",
    )
    await cb.answer()


@router.message(F.text.in_({"!статус", ".статус"}))
async def show_status(message: Message):
    await message.reply(get_chat_ai_status(message.chat.id), parse_mode="Markdown")


# ── !провайдеры — список всех провайдеров ──────────────────────────────────
@router.message(F.text.in_({"!провайдеры", ".провайдеры"}))
async def providers_list_cmd(message: Message):
    await message.reply(format_providers_list(message.chat.id), parse_mode="Markdown")


# ── !модели <провайдер> — список моделей провайдера ────────────────────────
@router.message(F.text.regexp(r"^[!\.]модели(\s.*)?$"))
async def models_list_cmd(message: Message):
    text = (message.text or "").strip()
    body = text[len("!модели"):] if text.startswith("!модели") else text[len(".модели"):]
    arg = body.strip()
    if not arg:
        return await message.reply(
            "Использование: `!модели <провайдер>`\n"
            "Пример: `!модели openrouter`\n\n"
            "Доступные: laozhang, openrouter, google, huggingface, кастом",
            parse_mode="Markdown",
        )
    await message.reply(format_models_list(arg), parse_mode="Markdown")


# ── !модель <модель> <провайдер>   или   !модель кастом ────────────────────
@router.message(F.text.regexp(r"^[!\.]модель(\s.*)?$"))
async def switch_model(message: Message):
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(message):
            return await message.reply("❗ Только админ может менять модель.")

    text = (message.text or "").strip()
    body = text[len("!модель"):] if text.startswith("!модель") else text[len(".модель"):]
    parts = body.strip().split()

    if not parts:
        return await message.reply(
            format_providers_list(message.chat.id),
            parse_mode="Markdown",
        )

    if len(parts) == 1 and normalize_provider(parts[0]) == "custom":
        ok, msg = set_chat_provider_and_model(message.chat.id, "custom", None)
        return await message.reply(msg, parse_mode="Markdown")

    if len(parts) == 1 and normalize_provider(parts[0]):
        ok, msg = set_chat_provider_and_model(message.chat.id, parts[0], None)
        return await message.reply(msg, parse_mode="Markdown")

    if len(parts) < 2:
        return await message.reply(
            "❗ Формат: `!модель <модель> <провайдер>`\n"
            "Пример: `!модель llama-3.3-70b-instruct:free openrouter`\n"
            "Список провайдеров: `!провайдеры`",
            parse_mode="Markdown",
        )

    provider = None
    model = None
    if normalize_provider(parts[-1]):
        provider = parts[-1]
        model = " ".join(parts[:-1])
    elif normalize_provider(parts[0]):
        provider = parts[0]
        model = " ".join(parts[1:])
    else:
        return await message.reply(
            "❌ Не понял провайдера.\n"
            "Доступно: laozhang, openrouter, google, huggingface, кастом\n"
            "Список: `!провайдеры`",
            parse_mode="Markdown",
        )

    ok, msg = set_chat_provider_and_model(message.chat.id, provider, model)
    await message.reply(msg, parse_mode="Markdown")


# ── !ключ <провайдер> <api_key> ────────────────────────────────────────────
@router.message(F.text.regexp(r"^[!\.]ключ(\s.*)?$"))
async def set_key(message: Message):
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(message):
            return await message.reply("❗ Только админ может задавать ключ.")

    text = (message.text or "").strip()
    body = text[len("!ключ"):] if text.startswith("!ключ") else text[len(".ключ"):]
    parts = body.strip().split()

    if len(parts) < 2:
        return await message.reply(
            "Использование: `!ключ <провайдер> <api_key>`\n"
            "Провайдеры: laozhang, openrouter, google, huggingface\n"
            "Пример: `!ключ openrouter sk-or-v1-...`",
            parse_mode="Markdown",
        )

    provider = parts[0]
    api_key = " ".join(parts[1:]).strip()
    ok, msg = set_chat_api_key(message.chat.id, provider, api_key)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(msg, parse_mode="Markdown")


# ── !кастом_провайдер <endpoint> <api_key> <model> ─────────────────────────
@router.message(F.text.regexp(r"^[!\.]кастом_провайдер(\s.*)?$"))
async def set_custom_provider_cmd(message: Message):
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(message):
            return await message.reply("❗ Только админ.")

    text = (message.text or "").strip()
    body = text[len("!кастом_провайдер"):] if text.startswith("!кастом_провайдер") else text[len(".кастом_провайдер"):]
    parts = body.strip().split()

    if len(parts) < 3:
        return await message.reply(
            "Использование:\n"
            "`!кастом_провайдер <endpoint> <api_key> <model>`\n"
            "Пример:\n"
            "`!кастом_провайдер https://api.example.com/v1/chat/completions sk-xxx my-model-7b`",
            parse_mode="Markdown",
        )

    endpoint = parts[0]
    api_key = parts[1]
    model = " ".join(parts[2:])
    ok, msg = set_chat_custom_provider(message.chat.id, endpoint, api_key, model)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(msg, parse_mode="Markdown")


@router.message(F.text.startswith(("!правила", ".правила")))
async def set_rules(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    member = await message.bot.get_chat_member(chat_id, user_id)
    if member.status not in ("administrator", "creator"):
        return await message.reply("❗ Только админ может задавать правила.")

    if message.text.startswith("!правила"):
        text = message.text[len("!правила"):].strip()
    else:
        text = message.text[len(".правила"):].strip()
    if not text:
        return await message.reply(
            "📌 Укажи текст правил. Пример:\n`!правила Добро пожаловать! Не спамить.`",
            parse_mode="Markdown",
        )

    cid = str(chat_id)
    settings.setdefault(cid, {})
    settings[cid]["rules"] = text
    save_settings(cid)
    await message.reply("✅ Правила установлены.")


@router.message(F.text.startswith(("!фото", ".фото")))
async def photo_command(msg: Message):
    text = (msg.text or "").strip()
    if text.lower().startswith("!фото"):
        query = text[len("!фото"):].strip()
    else:
        query = text[len(".фото"):].strip()
    if not query:
        return await msg.reply("❗ Укажи тему после `!фото`\nПример: `!фото красный закат над горами`",
                               parse_mode="Markdown")

    info = await msg.reply("🔍 Ищу фото...")

    try:
        from ..services.image_search import search_image
        image_bytes, src = await search_image(query)
    except Exception as e:
        return await info.edit_text(f"❌ Ошибка поиска: {e}")

    if not image_bytes:
        return await info.edit_text("❌ Фото не найдено. Попробуй другой запрос.")

    try:
        await info.delete()
    except Exception:
        pass

    photo_file = BufferedInputFile(image_bytes, filename="image.jpg")
    cap = f"🖼 <b>{html.escape(query)}</b>"
    try:
        await msg.answer_photo(photo=photo_file, caption=cap, parse_mode="HTML")
    except Exception:
        await msg.answer_document(document=photo_file, caption=cap, parse_mode="HTML")
