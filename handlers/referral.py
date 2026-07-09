import logging
from aiogram import Router, Bot, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.utils.deep_linking import create_start_link, decode_payload
from aiogram.enums import ParseMode

referral_router = Router(name="referral")
logger = logging.getLogger(__name__)

COMMISSION_PERCENT = 0.1  # 0.1%

# Память
referrals = {}


async def setup_ref_program(bot: Bot):
    """Установить комиссию 0.1% через Telegram API."""
    await bot.update_star_ref_program(commission_permille=1)
    logger.info("Партнёрка 0.1% активирована")


@referral_router.message(Command("ref"))
async def cmd_ref(message: Message):
    user_id = message.from_user.id
    ref_link = await create_start_link(message.bot, str(user_id), encode=True)
    count = referrals.get(user_id, {}).get("count", 0)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Поделиться", url=f"https://t.me/share/url?url={ref_link}")]
    ])

    text = (
        f"🎁 *Ваша партнёрская ссылка:*\n\n"
        f"`{ref_link}`\n\n"
        f"💸 *Комиссия:* `{COMMISSION_PERCENT}%`\n"
        f"👥 *Приглашено:* `{count}`\n\n"
        f"_Вы получаете процент с покупок приглашённых пользователей_"
    )

    await message.answer(
        text,
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )


# ВАЖНО: явно исключаем setup_* payload — его обрабатывает dm_setup_router.
@referral_router.message(
    CommandStart(deep_link=True),
    ~F.text.contains("setup_"),
)
async def start_deep(message: Message, bot: Bot):
    try:
        ref_id = int(decode_payload(message.text.split()[1]))
        if ref_id != message.from_user.id:
            if ref_id not in referrals:
                referrals[ref_id] = {"count": 0, "referees": []}
            if message.from_user.id not in referrals[ref_id]["referees"]:
                referrals[ref_id]["count"] += 1
                referrals[ref_id]["referees"].append(message.from_user.id)
                await bot.send_message(
                    ref_id,
                    f"🎉 *Новый реферал!*\nВсего: `{referrals[ref_id]['count']}`",
                    parse_mode=ParseMode.MARKDOWN
                )
    except Exception:
        pass

    welcome_text = (
        "👋 *Добро пожаловать!*\n\n"
        "Добавьте меня в группу и активируйте:\n"
        "`!антирейд вкл`"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.MARKDOWN)