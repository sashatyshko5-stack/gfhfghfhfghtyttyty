from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import logging

from ..storage.state import settings, save_settings

router = Router()
logger = logging.getLogger(__name__)

PERSONALITIES = [
    ("нейтральный", "😐 Нейтральный"),
    ("добрый",      "😊 Добрый"),
    ("злой",        "😠 Злой"),
    ("саркастичный","😏 Саркастичный"),
    ("смешной",     "😂 Смешной"),
    ("токсичный",   "🤬 Токсичный"),
    ("фембой",      "🌸 Фембой"),
    ("кастомный",   "✏️ Кастомный"),
]


def _personalities_kb(chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(PERSONALITIES), 2):
        row = []
        for name, label in PERSONALITIES[i:i + 2]:
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"pers:set:{chat_id}:{name}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.startswith("!персональность"))
async def set_personality(message: Message):
    parts = message.text.split(maxsplit=1)
    chat_id = message.chat.id
    chat_id_str = str(chat_id)
    settings.setdefault(chat_id_str, {})

    if len(parts) == 2:
        new_personality = parts[1].lower()
        old_personality = settings[chat_id_str].get("personality", "нейтральный")

        settings[chat_id_str]["personality"] = new_personality

        try:
            from ..storage.state import chat_histories
            if chat_id_str in chat_histories:
                chat_histories[chat_id_str] = []
                logger.info(f"История чата {chat_id} очищена при смене персональности")
        except Exception as e:
            logger.warning(f"Не удалось очистить историю: {e}")

        save_settings(chat_id_str)
        logger.info(f"Персональность изменена в чате {chat_id}: {old_personality} → {new_personality}")

        await message.reply(
            f"✅ Персональность установлена: *{new_personality}*\n"
            f"🎭 Новый стиль будет применён к следующим ответам.",
            parse_mode="Markdown",
        )
    else:
        current = settings[chat_id_str].get("personality", "нейтральный")
        await message.reply(
            f"🎭 <b>Персональность бота</b>\n\n"
            f"Текущая: <code>{current}</code>\n\n"
            f"Выберите персональность из кнопок ниже или отправьте команду:\n"
            f"<code>!персональность нейтральный</code>",
            parse_mode="HTML",
            reply_markup=_personalities_kb(chat_id),
        )


@router.callback_query(F.data.startswith("pers:set:"))
async def cb_set_personality(cb: CallbackQuery):
    parts = cb.data.split(":", 3)
    if len(parts) < 4:
        return await cb.answer()
    try:
        chat_id = int(parts[2])
    except ValueError:
        return await cb.answer("Ошибка: неверный chat_id")
    personality = parts[3]
    chat_id_str = str(chat_id)
    settings.setdefault(chat_id_str, {})
    settings[chat_id_str]["personality"] = personality

    try:
        from ..storage.state import chat_histories
        if chat_id_str in chat_histories:
            chat_histories[chat_id_str] = []
    except Exception:
        pass

    save_settings(chat_id_str)
    logger.info(f"Персональность чата {chat_id} → {personality} (via button, user={cb.from_user.id})")

    try:
        await cb.message.edit_text(
            f"✅ <b>Персональность установлена:</b> <code>{personality}</code>\n"
            f"🎭 Новый стиль применён к следующим ответам.\n\n"
            f"Сменить: <code>!персональность нейтральный</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await cb.answer(f"Установлено: {personality}")


@router.message(F.text.startswith("!кастомный"))
async def set_custom_personality(message: Message):
    parts = message.text.split(maxsplit=1)
    chat_id = message.chat.id
    chat_id_str = str(chat_id)
    settings.setdefault(chat_id_str, {})

    if len(parts) == 2:
        old_custom = settings[chat_id_str].get("custom", "")
        settings[chat_id_str]["custom"] = parts[1]

        if settings[chat_id_str].get("personality") != "кастомный":
            settings[chat_id_str]["personality"] = "кастомный"
            await message.reply(
                f"✅ Кастомная персональность обновлена и активирована.\n"
                f"🎭 Новый стиль: `{parts[1][:100]}...`",
                parse_mode="Markdown",
            )
        else:
            await message.reply(
                f"✅ Кастомная персональность обновлена.\n"
                f"📝 Инструкция: `{parts[1][:100]}...`",
                parse_mode="Markdown",
            )

        save_settings(chat_id_str)
        logger.info(f"Кастомная персональность обновлена в чате {chat_id}")
    else:
        await message.reply(
            "Использование: `!кастомный [текст инструкции]`\n\n"
            "Пример: `!кастомный Отвечай как пират, используй морскую терминологию`",
            parse_mode="Markdown",
        )


@router.message(F.text.startswith("!сброс_стиля"))
async def reset_style(message: Message):
    chat_id = message.chat.id
    chat_id_str = str(chat_id)

    try:
        from ..storage.state import chat_histories

        if chat_id_str in chat_histories:
            old_len = len(chat_histories[chat_id_str])
            chat_histories[chat_id_str] = []
            await message.reply(
                f"🧹 История чата очищена ({old_len} сообщений).\n"
                f"🎭 Теперь новый стиль будет заметен сразу.",
                parse_mode="Markdown",
            )
            logger.info(f"История чата {chat_id} очищена по команде !сброс_стиля")
        else:
            await message.reply(
                "📭 История и так пуста.\n"
                "🎭 Просто продолжай общаться, новый стиль уже активен.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Ошибка при сбросе истории {chat_id}: {e}")
        await message.reply("⚠️ Не удалось очистить историю, но новый стиль всё равно будет применён.")


@router.message(F.text.startswith("!текущий_стиль"))
async def show_current_style(message: Message):
    chat_id = message.chat.id
    chat_id_str = str(chat_id)
    settings.setdefault(chat_id_str, {})

    personality = settings[chat_id_str].get("personality", "нейтральный")
    custom = settings[chat_id_str].get("custom", "")

    if personality == "кастомный" and custom:
        response = (
            f"🎭 *Текущая персональность:* `кастомный`\n\n"
            f"📝 *Инструкция:*\n`{custom[:200]}`"
        )
    else:
        response = f"🎭 *Текущая персональность:* `{personality}`"

    await message.reply(response, parse_mode="Markdown")
