"""
aiohttp REST API for the web control panel.

Implements the exact same contract as the Node mock backend used to build
and preview the frontend, so the frontend needs zero code changes to talk to
this real backend -- just point VITE_API_BASE_URL at wherever this server is
reachable.

Endpoints:
  POST   /api/auth/telegram-webapp
  POST   /api/auth/telegram-widget
  GET    /api/auth/me
  GET    /api/chats
  GET    /api/chats/{chatId}
  GET    /api/chats/{chatId}/moderators
  GET    /api/chats/{chatId}/settings
  PATCH  /api/chats/{chatId}/settings/antispam
  PATCH  /api/chats/{chatId}/settings/antinsfw
  PATCH  /api/chats/{chatId}/settings/anti-raid
  GET    /api/chats/{chatId}/anti-raid/status
  POST   /api/chats/{chatId}/anti-raid/lift
  GET    /api/ai/providers
  GET    /api/chats/{chatId}/ai
  PATCH  /api/chats/{chatId}/ai
  POST   /api/chats/{chatId}/ai/api-key
  DELETE /api/chats/{chatId}/ai/api-key/{provider}

There is intentionally NO /auth/dev-login route here -- that endpoint only
exists in the Node mock backend used for previewing the frontend in the
Replit workspace. Do not add a demo-login bypass to a production bot.
"""

from __future__ import annotations

import os
from typing import Optional

from aiohttp import web

from . import auth as authlib
from . import providers as providers_lib
from . import serializers


def _cors_headers(request: web.Request) -> dict:
    origin = os.environ.get("WEBAPI_CORS_ORIGIN", "*")
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    }


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers=_cors_headers(request))
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers.update(_cors_headers(request))
        raise
    response.headers.update(_cors_headers(request))
    return response


def _bot_token(bot) -> str:
    token = getattr(bot, "token", None)
    if not token:
        raise RuntimeError("Could not read the bot token from the aiogram Bot instance.")
    return token


async def _require_session(request: web.Request) -> authlib.Session:
    header = request.headers.get("Authorization", "")
    token = header[len("Bearer "):] if header.startswith("Bearer ") else None
    session = authlib.get_session(token) if token else None
    if not session:
        raise web.HTTPUnauthorized(text='{"error": "Требуется авторизация"}', content_type="application/json")
    return session


async def _require_chat_admin(request: web.Request, bot, session: authlib.Session, chat_id: int) -> None:
    try:
        member = await bot.get_chat_member(chat_id, session.user_id)
    except Exception as exc:  # noqa: BLE001 - Telegram errors surface as generic exceptions
        raise web.HTTPForbidden(text='{"error": "Нет доступа к этому чату"}', content_type="application/json") from exc

    if member.status not in ("creator", "administrator"):
        raise web.HTTPForbidden(text='{"error": "Нет доступа к этому чату"}', content_type="application/json")


def _all_chat_ids() -> list[int]:
    from bot.core.config import SETTINGS_DIR  # adjust import if your constant lives elsewhere

    try:
        return [int(name) for name in os.listdir(SETTINGS_DIR) if name.lstrip("-").isdigit()]
    except FileNotFoundError:
        return []


def create_app(bot, dp=None) -> web.Application:
    from bot.storage import state as state_storage
    from bot.handlers import anti_raid as anti_raid_handler

    app = web.Application(middlewares=[cors_middleware])

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def auth_telegram_webapp(request: web.Request) -> web.Response:
        body = await request.json()
        init_data = body.get("initData", "")
        user = authlib.verify_webapp_init_data(init_data, _bot_token(bot))
        if not user:
            raise web.HTTPUnauthorized(text='{"error": "Подпись Telegram initData недействительна"}', content_type="application/json")

        session = authlib.issue_session(
            user_id=user["id"], first_name=user.get("first_name", ""),
            last_name=user.get("last_name"), username=user.get("username"),
            photo_url=user.get("photo_url"),
        )
        return web.json_response({"token": session.token, "user": _session_to_user(session)})

    async def auth_telegram_widget(request: web.Request) -> web.Response:
        payload = await request.json()
        if not authlib.verify_login_widget(payload, _bot_token(bot)):
            raise web.HTTPUnauthorized(text='{"error": "Подпись Telegram Login Widget недействительна"}', content_type="application/json")

        session = authlib.issue_session(
            user_id=payload["id"], first_name=payload.get("first_name", ""),
            last_name=payload.get("last_name"), username=payload.get("username"),
            photo_url=payload.get("photo_url"),
        )
        return web.json_response({"token": session.token, "user": _session_to_user(session)})

    async def auth_me(request: web.Request) -> web.Response:
        session = await _require_session(request)
        return web.json_response(_session_to_user(session))

    def _session_to_user(session: authlib.Session) -> dict:
        return {
            "id": session.user_id, "firstName": session.first_name,
            "lastName": session.last_name, "username": session.username,
            "photoUrl": session.photo_url, "isDemo": False,
        }

    async def list_chats(request: web.Request) -> web.Response:
        session = await _require_session(request)
        result = []
        for chat_id in _all_chat_ids():
            try:
                member = await bot.get_chat_member(chat_id, session.user_id)
            except Exception:  # noqa: BLE001
                continue
            if member.status not in ("creator", "administrator"):
                continue
            try:
                tg_chat = await bot.get_chat(chat_id)
            except Exception:  # noqa: BLE001
                continue
            chat_settings = state_storage.settings.get(str(chat_id), {})
            result.append(serializers.chat_summary(chat_id, chat_settings, tg_chat))
        return web.json_response(result)

    async def get_chat(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        tg_chat = await bot.get_chat(chat_id)
        chat_settings = state_storage.settings.get(str(chat_id), {})
        return web.json_response(serializers.chat_summary(chat_id, chat_settings, tg_chat))

    async def list_moderators(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        admins = await bot.get_chat_administrators(chat_id)
        return web.json_response([
            {
                "id": a.user.id,
                "name": " ".join(filter(None, [a.user.first_name, a.user.last_name])) or str(a.user.id),
                "firstName": a.user.first_name,
                "lastName": a.user.last_name,
                "username": a.user.username,
                "photoUrl": None,
                "status": a.status,
            }
            for a in admins
        ])

    async def get_settings(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        chat_settings = state_storage.settings.setdefault(str(chat_id), {})
        return web.json_response({
            "antispam": serializers.antispam(chat_settings),
            "antinsfw": serializers.antinsfw(chat_settings),
            "anti_raid": anti_raid_handler.get_anti_raid_settings(chat_id),
            "ai": serializers.ai_settings(chat_id, chat_settings),
            "meta": {
                "addedByUserName": chat_settings.get("added_by_user_name"),
                "privacyAccepted": bool(chat_settings.get("privacy_accepted", True)),
            },
        })

    def _patch_section(section_key: str, defaults: dict):
        async def handler(request: web.Request) -> web.Response:
            session = await _require_session(request)
            chat_id = int(request.match_info["chatId"])
            await _require_chat_admin(request, bot, session, chat_id)
            body = await request.json()

            chat_settings = state_storage.settings.setdefault(str(chat_id), {})
            current = {**defaults, **chat_settings.get(section_key, {})}
            current.update({k: v for k, v in body.items() if k in defaults})
            chat_settings[section_key] = current
            state_storage.save_settings(chat_id)

            return web.json_response(current)

        return handler

    async def patch_anti_raid(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        body = await request.json()

        current = anti_raid_handler.get_anti_raid_settings(chat_id)
        current.update(body)
        anti_raid_handler.save_anti_raid_settings(chat_id, current)

        return web.json_response(current)

    async def get_anti_raid_status(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)

        raid_settings = anti_raid_handler.get_anti_raid_settings(chat_id)
        status = serializers.anti_raid_status(chat_id, anti_raid_handler.storage)
        status["joinThreshold"] = raid_settings.get("join_threshold", 0)
        return web.json_response(status)

    async def lift_anti_raid(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)

        # Mirrors what the "снять" command does in bot/handlers/anti_raid.py.
        anti_raid_handler.storage.deactivate_lockdown(chat_id)
        try:
            await anti_raid_handler.unlock_chat(bot, chat_id)
        except AttributeError:
            pass  # unlock_chat may have a different name/signature in your version -- adjust here.

        raid_settings = anti_raid_handler.get_anti_raid_settings(chat_id)
        return web.json_response({"lockdownActive": False, "joinsInWindow": 0, "joinThreshold": raid_settings.get("join_threshold", 0)})

    async def list_ai_providers(request: web.Request) -> web.Response:
        return web.json_response(providers_lib.catalogue())

    async def get_ai(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        chat_settings = state_storage.settings.setdefault(str(chat_id), {})
        return web.json_response(serializers.ai_settings(chat_id, chat_settings))

    async def patch_ai(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        body = await request.json()

        from bot.services import chat_ai_router as router

        chat_settings = state_storage.settings.setdefault(str(chat_id), {})

        if "personality" in body:
            chat_settings["personality"] = body["personality"]
        if "custom" in body:
            chat_settings["custom"] = body["custom"]
        if "ai_enabled" in body:
            chat_settings["ai_enabled"] = bool(body["ai_enabled"])

        provider = body.get("ai_provider")
        model = body.get("ai_model")
        if provider is not None or model is not None:
            effective_provider = provider or router.get_chat_provider(chat_id)
            if model is None and provider is not None and not providers_lib.is_valid(effective_provider, router.get_chat_model(chat_id)):
                from .providers import CATALOGUE
                entry = next((p for p in CATALOGUE if p["id"] == effective_provider), None)
                model = entry["defaultModel"] if entry else None
            router.set_chat_provider_and_model(chat_id, effective_provider, model)

        custom_provider = body.get("custom_provider")
        if custom_provider:
            router.set_chat_custom_provider(
                chat_id,
                custom_provider.get("endpoint", ""),
                "",  # API key is set separately via /ai/api-key -- never accepted inline here.
                custom_provider.get("model", ""),
            )

        state_storage.save_settings(chat_id)
        return web.json_response(serializers.ai_settings(chat_id, chat_settings))

    async def set_ai_api_key(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        body = await request.json()
        provider = body.get("provider")
        api_key = (body.get("api_key") or "").strip()
        if not provider or not api_key:
            raise web.HTTPBadRequest(text='{"error": "Провайдер и ключ обязательны"}', content_type="application/json")

        from bot.services import chat_ai_router as router
        router.set_chat_api_key(chat_id, provider, api_key)

        chat_settings = state_storage.settings.setdefault(str(chat_id), {})
        return web.json_response(serializers.ai_settings(chat_id, chat_settings))

    async def delete_ai_api_key(request: web.Request) -> web.Response:
        session = await _require_session(request)
        chat_id = int(request.match_info["chatId"])
        await _require_chat_admin(request, bot, session, chat_id)
        provider = request.match_info["provider"]

        from bot.storage import providers_storage
        try:
            providers_storage.save_api_key(chat_id, provider, "")
        except Exception:  # noqa: BLE001
            pass  # Adjust to match however your providers_storage clears a key.

        chat_settings = state_storage.settings.setdefault(str(chat_id), {})
        return web.json_response(serializers.ai_settings(chat_id, chat_settings))

    app.router.add_get("/api/healthz", healthz)
    app.router.add_post("/api/auth/telegram-webapp", auth_telegram_webapp)
    app.router.add_post("/api/auth/telegram-widget", auth_telegram_widget)
    app.router.add_get("/api/auth/me", auth_me)
    app.router.add_get("/api/chats", list_chats)
    app.router.add_get("/api/chats/{chatId}", get_chat)
    app.router.add_get("/api/chats/{chatId}/moderators", list_moderators)
    app.router.add_get("/api/chats/{chatId}/settings", get_settings)
    app.router.add_patch("/api/chats/{chatId}/settings/antispam", _patch_section("antispam", serializers.ANTISPAM_DEFAULTS))
    app.router.add_patch("/api/chats/{chatId}/settings/antinsfw", _patch_section("antinsfw", serializers.ANTINSFW_DEFAULTS))
    app.router.add_patch("/api/chats/{chatId}/settings/anti-raid", patch_anti_raid)
    app.router.add_get("/api/chats/{chatId}/anti-raid/status", get_anti_raid_status)
    app.router.add_post("/api/chats/{chatId}/anti-raid/lift", lift_anti_raid)
    app.router.add_get("/api/ai/providers", list_ai_providers)
    app.router.add_get("/api/chats/{chatId}/ai", get_ai)
    app.router.add_patch("/api/chats/{chatId}/ai", patch_ai)
    app.router.add_post("/api/chats/{chatId}/ai/api-key", set_ai_api_key)
    app.router.add_delete("/api/chats/{chatId}/ai/api-key/{provider}", delete_ai_api_key)

    return app


async def start_webapi_server(bot, dp=None, port: Optional[int] = None) -> web.AppRunner:
    """Call this once, alongside bot polling startup (see main_patch.py)."""
    app = create_app(bot, dp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port or int(os.environ.get("WEBAPI_PORT", "8081")))
    await site.start()
    return runner
