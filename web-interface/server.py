#!/usr/bin/env python3
"""Small development backend for the static web panel.

It serves files from web-interface/ and applies settings directly to the bot's
JSON storage: group_settings/<chat_id>/settings.json.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

STATIC_DIR = Path(__file__).resolve().parent
REPO_ROOT = STATIC_DIR.parent
DEFAULT_SETTINGS_DIR = REPO_ROOT.parent / "group_settings"
SETTINGS_DIR = Path(os.environ.get("AI_DEFENDER_SETTINGS_DIR", DEFAULT_SETTINGS_DIR))
CHAT_ID_RE = re.compile(r"^-?\d+$")


def chat_path(chat_id: str) -> Path:
    if not CHAT_ID_RE.match(chat_id):
        raise ValueError("chat_id must be numeric")
    return SETTINGS_DIR / chat_id / "settings.json"


def read_settings(chat_id: str) -> dict:
    path = chat_path(chat_id)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("settings file must contain a JSON object")
    return data


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status=400):
        self.send_json({"error": message}, status)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/me":
            return self.send_json({"auth": "telegram_required"})
        if parsed.path == "/api/chats":
            return self.list_chats()
        match = re.fullmatch(r"/api/chats/([^/]+)/settings", parsed.path)
        if match:
            chat_id = unquote(match.group(1))
            try:
                return self.send_json({"id": chat_id, "settings": read_settings(chat_id)})
            except Exception as exc:  # noqa: BLE001
                return self.send_error_json(str(exc), 400)
        return super().do_GET()

    def do_PUT(self):
        match = re.fullmatch(r"/api/chats/([^/]+)/settings", urlparse(self.path).path)
        if not match:
            return self.send_error_json("Unknown endpoint", 404)
        chat_id = unquote(match.group(1))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            settings = payload.get("settings", payload)
            if not isinstance(settings, dict):
                raise ValueError("settings must be a JSON object")
            atomic_write_json(chat_path(chat_id), settings)
            return self.send_json({"ok": True, "chat_id": chat_id, "path": str(chat_path(chat_id))})
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(str(exc), 400)

    def list_chats(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        chats = []
        for entry in sorted(SETTINGS_DIR.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or not CHAT_ID_RE.match(entry.name):
                continue
            try:
                settings = read_settings(entry.name)
            except Exception:
                settings = {}
            title = settings.get("title") or settings.get("chat_title") or f"Чат {entry.name}"
            chats.append({"id": entry.name, "title": title, "settings": settings})
        return self.send_json({"chats": chats, "settings_dir": str(SETTINGS_DIR)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"AI Defender web panel: http://127.0.0.1:{port}")
    print(f"Settings dir: {SETTINGS_DIR}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
