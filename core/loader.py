from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import BOT_TOKEN
from .outgoing_logger import OutgoingLoggerMiddleware

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
# Лог исходящих non-AI сообщений бота для ИИ-памяти.
bot.session.middleware(OutgoingLoggerMiddleware())

dp = Dispatcher()

# Апдейты, которые бот должен получать от Telegram.
# КРИТИЧНО: без "chat_member" auto-reban работать не будет —
# Telegram по умолчанию не присылает эти события.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
]