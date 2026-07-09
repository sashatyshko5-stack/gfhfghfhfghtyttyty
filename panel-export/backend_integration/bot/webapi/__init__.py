"""
REST API for the web control panel.

This package is added into the bot's own codebase so it can read and write
the exact same in-memory/disk state the bot uses for moderation
(`bot.storage.state.settings`, anti-raid settings, AI provider config, etc.).
See `README.md` (in the delivered archive, next to this folder) for wiring
instructions.
"""

from .server import start_webapi_server

__all__ = ["start_webapi_server"]
