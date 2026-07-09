"""
Telegram authentication for the web panel.

Two flows are supported, mirroring Telegram's own documented algorithms:

- Mini App: validates the `initData` string a Telegram WebView passes to
  `window.Telegram.WebApp.initData`.
  https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

- Login Widget: validates the payload posted by the Telegram Login Widget on
  a regular website.
  https://core.telegram.org/widgets/login#checking-authorization

Sessions are simple bearer tokens kept in memory (`_sessions`), signed with
`WEBAPI_SESSION_SECRET` so they survive being handed to the browser but can't
be forged. They intentionally are NOT persisted to disk -- if the bot process
restarts, panel users just sign in again (same as any short session).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
_sessions: dict[str, "Session"] = {}


@dataclass
class Session:
    token: str
    user_id: int
    first_name: str
    last_name: Optional[str]
    username: Optional[str]
    photo_url: Optional[str]
    created_at: float


def _session_secret() -> bytes:
    secret = os.environ.get("WEBAPI_SESSION_SECRET")
    if not secret:
        raise RuntimeError(
            "WEBAPI_SESSION_SECRET is not set. Generate one with "
            "`python -c \"import secrets;print(secrets.token_hex(32))\"` "
            "and set it in the bot's environment before starting the web API."
        )
    return secret.encode("utf-8")


def issue_session(user_id: int, first_name: str, last_name: Optional[str],
                   username: Optional[str], photo_url: Optional[str]) -> Session:
    raw = f"{user_id}:{time.time_ns()}".encode("utf-8")
    token = hmac.new(_session_secret(), raw, hashlib.sha256).hexdigest() + f".{user_id}"
    session = Session(
        token=token,
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        photo_url=photo_url,
        created_at=time.time(),
    )
    _sessions[token] = session
    return session


def get_session(token: str) -> Optional[Session]:
    session = _sessions.get(token)
    if session is None:
        return None
    if time.time() - session.created_at > SESSION_TTL_SECONDS:
        _sessions.pop(token, None)
        return None
    return session


def revoke_session(token: str) -> None:
    _sessions.pop(token, None)


def verify_webapp_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> Optional[dict]:
    """Validates `initData` from Telegram.WebApp. Returns the parsed `user`
    dict on success, or None if the signature/age check fails."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if not auth_date or time.time() - int(auth_date) > max_age_seconds:
        return None

    import json
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except ValueError:
        return None


def verify_login_widget(payload: dict, bot_token: str, max_age_seconds: int = 86400) -> bool:
    """Validates the payload posted by the Telegram Login Widget."""
    data = {k: v for k, v in payload.items() if k != "hash" and v is not None}
    received_hash = payload.get("hash")
    if not received_hash:
        return False

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return False

    auth_date = payload.get("auth_date")
    if not auth_date or time.time() - int(auth_date) > max_age_seconds:
        return False

    return True
