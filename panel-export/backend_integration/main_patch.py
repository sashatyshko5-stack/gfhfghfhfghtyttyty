"""
NOT meant to be run directly / copied verbatim -- this shows the shape of the
change to make in your bot's real entrypoint (wherever you currently call
something like `dp.start_polling(bot)` or `executor.start_polling(dp)`).

The web API and the bot's polling loop are both `asyncio` coroutines, so they
run inside the SAME event loop via `asyncio.gather(...)`. No separate process
or thread is needed.
"""

import asyncio

from bot.webapi import start_webapi_server


async def main():
    # ... your existing setup: load config, build `bot` (aiogram.Bot) and
    # `dp` (aiogram.Dispatcher) exactly as you already do ...
    from bot.core.loader import bot, dp  # adjust to your actual module layout

    webapi_runner = await start_webapi_server(bot, dp)

    try:
        # Replace this with however you currently start polling, e.g.:
        #   await dp.start_polling(bot)
        await dp.start_polling(bot)
    finally:
        await webapi_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
