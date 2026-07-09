"""Персистентное SQLite-хранилище логов сообщений по группам.

Раньше журнал хранился в `message_logs.json` со структурой::

    {
      "<chat_id>": {
          "title": "Название чата",
          "messages": [{...}, ...]
      }
    }

Теперь данные пишутся сразу в SQLite-файл `message_logs.sqlite3`. Старый JSON
можно безопасно импортировать функцией `migrate_json_to_sqlite()`. При старте
`load_message_logs()` также автоматически подхватит существующий JSON и
перенесёт его в SQLite без дублей.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Старый файл с логами: оставлен для автоматической миграции.
MESSAGE_LOGS_FILE = os.environ.get("MESSAGE_LOGS_FILE", "message_logs.json")

# Новый SQLite-файл с логами сообщений.
MESSAGE_LOGS_DB_FILE = os.environ.get("MESSAGE_LOGS_DB_FILE", "message_logs.sqlite3")

# Сколько последних сообщений хранить на каждый чат.
MAX_PER_CHAT = 500

_lock = threading.RLock()
_initialized = False
_dirty = False

# Счётчик версии журнала — растёт на каждое новое сообщение.
# Используется SSE-эндпоинтом в web/server.py для авто-обновления фронта.
_version = 0


def configure_message_logs_paths(
    *,
    json_path: str | os.PathLike | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """Настраивает пути хранилища до первого обращения из бота или веб-панели."""
    global MESSAGE_LOGS_FILE, MESSAGE_LOGS_DB_FILE, _initialized
    changed = False
    if json_path is not None:
        new_json = str(json_path)
        changed = changed or new_json != MESSAGE_LOGS_FILE
        MESSAGE_LOGS_FILE = new_json
    if db_path is not None:
        new_db = str(db_path)
        changed = changed or new_db != MESSAGE_LOGS_DB_FILE
        MESSAGE_LOGS_DB_FILE = new_db
    if changed:
        _initialized = False


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id INTEGER,
    date TEXT,
    user_id INTEGER,
    user_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'other',
    file_id TEXT,
    thumbnail_file_id TEXT,
    file_name TEXT,
    reply_to INTEGER,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id),
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id_date ON messages(chat_id, date);
"""


def _connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    db_path = str(db_path or MESSAGE_LOGS_DB_FILE)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db(db_path: str | os.PathLike | None = None) -> None:
    global _initialized
    db_path = str(db_path or MESSAGE_LOGS_DB_FILE)
    if _initialized and db_path == MESSAGE_LOGS_DB_FILE:
        return

    Path(db_path).parent.mkdir(parents=True, exist_ok=True) if Path(db_path).parent != Path(".") else None
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()

    if db_path == MESSAGE_LOGS_DB_FILE:
        _initialized = True


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": entry.get("message_id"),
        "date": entry.get("date") or datetime.now().isoformat(),
        "user_id": entry.get("user_id"),
        "user_name": entry.get("user_name") or "",
        "username": entry.get("username") or "",
        "text": (entry.get("text") or "")[:500],
        "type": entry.get("type") or "other",
        "file_id": entry.get("file_id"),
        "thumbnail_file_id": entry.get("thumbnail_file_id"),
        "file_name": entry.get("file_name"),
        "reply_to": entry.get("reply_to"),
    }


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except json.JSONDecodeError:
        raw = {}

    raw.update(
        {
            "message_id": row["message_id"],
            "date": row["date"],
            "user_id": row["user_id"],
            "user_name": row["user_name"],
            "username": row["username"],
            "text": row["text"],
            "type": row["type"],
            "file_id": row["file_id"],
            "thumbnail_file_id": row["thumbnail_file_id"],
            "file_name": row["file_name"],
            "reply_to": row["reply_to"],
        }
    )
    return raw


def _upsert_chat(conn: sqlite3.Connection, chat_id: str, title: str = "") -> None:
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO chats(chat_id, title, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = CASE
                WHEN excluded.title != '' THEN excluded.title
                ELSE chats.title
            END,
            updated_at = excluded.updated_at
        """,
        (chat_id, title or "", now),
    )


def _insert_message(conn: sqlite3.Connection, chat_id: str, entry: dict[str, Any]) -> bool:
    normalized = _normalize_entry(entry)
    raw_entry = dict(entry)
    raw_entry.update(normalized)
    raw_json = json.dumps(raw_entry, ensure_ascii=False)
    now = datetime.now().isoformat()

    cursor = conn.execute(
        """
        INSERT INTO messages(
            chat_id,
            message_id,
            date,
            user_id,
            user_name,
            username,
            text,
            type,
            file_id,
            thumbnail_file_id,
            file_name,
            reply_to,
            raw_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
            date = excluded.date,
            user_id = excluded.user_id,
            user_name = excluded.user_name,
            username = excluded.username,
            text = excluded.text,
            type = excluded.type,
            file_id = excluded.file_id,
            thumbnail_file_id = excluded.thumbnail_file_id,
            file_name = excluded.file_name,
            reply_to = excluded.reply_to,
            raw_json = excluded.raw_json
        """,
        (
            chat_id,
            normalized["message_id"],
            normalized["date"],
            normalized["user_id"],
            normalized["user_name"],
            normalized["username"],
            normalized["text"],
            normalized["type"],
            normalized["file_id"],
            normalized["thumbnail_file_id"],
            normalized["file_name"],
            normalized["reply_to"],
            raw_json,
            now,
        ),
    )
    return cursor.rowcount > 0


def _trim_chat(conn: sqlite3.Connection, chat_id: str, max_per_chat: int = MAX_PER_CHAT) -> None:
    conn.execute(
        """
        DELETE FROM messages
        WHERE chat_id = ?
          AND id NOT IN (
              SELECT id
              FROM messages
              WHERE chat_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (chat_id, chat_id, max_per_chat),
    )


def _count_messages(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS total FROM messages").fetchone()
    return int(row["total"] if row else 0)


def migrate_json_to_sqlite(
    json_path: str | os.PathLike | None = None,
    db_path: str | os.PathLike | None = None,
    *,
    remove_json: bool = False,
) -> dict[str, int]:
    """Переносит текущий `message_logs.json` в SQLite без создания дублей."""
    json_path = str(json_path or MESSAGE_LOGS_FILE)
    db_path = str(db_path or MESSAGE_LOGS_DB_FILE)
    stats = {"chats": 0, "messages": 0, "skipped": 0}
    if not os.path.exists(json_path):
        return stats

    _ensure_db(db_path)
    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Некорректная структура {json_path}: ожидался объект")

    with _lock, _connect(db_path) as conn:
        for cid, data in raw.items():
            if not isinstance(data, dict):
                stats["skipped"] += 1
                continue

            chat_id = str(cid)
            title = data.get("title", "") or ""
            messages = data.get("messages", []) or []
            if not isinstance(messages, list):
                stats["skipped"] += 1
                continue

            _upsert_chat(conn, chat_id, title)
            stats["chats"] += 1

            for entry in messages[-MAX_PER_CHAT:]:
                if not isinstance(entry, dict):
                    stats["skipped"] += 1
                    continue
                _insert_message(conn, chat_id, entry)
                stats["messages"] += 1

            _trim_chat(conn, chat_id)

        conn.commit()

    if remove_json:
        os.remove(json_path)

    logger.info(
        "Миграция логов сообщений JSON→SQLite завершена: %s чатов, %s сообщений, %s пропущено",
        stats["chats"],
        stats["messages"],
        stats["skipped"],
    )
    return stats


def load_message_logs():
    """Инициализирует SQLite-хранилище и переносит старый JSON при наличии."""
    global _dirty, _version
    with _lock:
        try:
            _ensure_db()
            if os.path.exists(MESSAGE_LOGS_FILE):
                migrate_json_to_sqlite(MESSAGE_LOGS_FILE, MESSAGE_LOGS_DB_FILE)

            with _connect() as conn:
                _version = _count_messages(conn)

            logger.info("Логи сообщений загружены из %s", MESSAGE_LOGS_DB_FILE)
        except Exception as e:
            logger.error("Ошибка загрузки логов сообщений: %s", e)
            _ensure_db()
            _version = 0
        _dirty = False


def save_message_logs():
    """Совместимость со старым API: SQLite сохраняет каждое сообщение сразу."""
    global _dirty
    with _lock:
        try:
            _ensure_db()
            with _connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            _dirty = False
        except Exception as e:
            logger.error("Ошибка checkpoint SQLite-журнала сообщений: %s", e)


def log_message(chat_id: int, chat_title: str, entry: dict):
    """Добавляет одно сообщение в SQLite-журнал."""
    global _dirty, _version
    cid = str(chat_id)
    with _lock:
        try:
            _ensure_db()
            with _connect() as conn:
                _upsert_chat(conn, cid, chat_title)
                _insert_message(conn, cid, entry)
                _trim_chat(conn, cid)
                conn.commit()
            _version += 1
            _dirty = False
        except Exception as e:
            logger.error("Ошибка записи сообщения в SQLite-журнал: %s", e)
            _dirty = True


def get_chat_messages(chat_id: int, limit: int = 100) -> list:
    """Возвращает последние `limit` сообщений для чата (последние в конце списка)."""
    cid = str(chat_id)
    with _lock:
        _ensure_db()
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (cid, limit),
            ).fetchall()
        return [_row_to_entry(row) for row in reversed(rows)]


def get_chat_title(chat_id: int) -> str:
    cid = str(chat_id)
    with _lock:
        _ensure_db()
        with _connect() as conn:
            row = conn.execute(
                "SELECT title FROM chats WHERE chat_id = ?",
                (cid,),
            ).fetchone()
        return row["title"] if row else ""


def get_known_chats() -> list:
    with _lock:
        _ensure_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, title FROM chats ORDER BY chat_id"
            ).fetchall()
        return [(row["chat_id"], row["title"]) for row in rows]


def is_dirty() -> bool:
    return _dirty


def get_version() -> int:
    """Версия журнала: увеличивается при каждом новом сообщении. Для SSE/long-poll."""
    return _version


def extract_entry_from_message(message) -> dict:
    """Формирует запись журнала из aiogram Message."""
    ctype = "text"
    text = message.text or message.caption or ""
    if message.pinned_message:
        ctype = "pinned_message"
        text = message.pinned_message.text or message.pinned_message.caption or ""
    elif message.left_chat_member:
        ctype = "left_chat_member"
        text = ""
    elif message.new_chat_members:
        ctype = "new_chat_members"
        text = ""
    elif message.text:
        ctype = "text"
    elif message.photo:
        ctype = "photo"
    elif message.sticker:
        ctype = "sticker"
    elif message.video:
        ctype = "video"
    elif message.animation:
        ctype = "animation"
    elif message.document:
        ctype = "document"
    elif message.voice:
        ctype = "voice"
    elif message.audio:
        ctype = "audio"
    elif message.video_note:
        ctype = "video_note"
    else:
        ctype = "other"

    file_id = None
    file_name = None
    thumbnail_file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.sticker:
        file_id = message.sticker.file_id
        thumb = getattr(message.sticker, "thumbnail", None)
        if thumb:
            thumbnail_file_id = thumb.file_id
    elif message.video:
        file_id = message.video.file_id
        thumb = getattr(message.video, "thumbnail", None)
        if thumb:
            thumbnail_file_id = thumb.file_id
    elif message.animation:
        file_id = message.animation.file_id
        thumb = getattr(message.animation, "thumbnail", None)
        if thumb:
            thumbnail_file_id = thumb.file_id
    elif message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
        thumb = getattr(message.document, "thumbnail", None)
        if thumb:
            thumbnail_file_id = thumb.file_id
    elif message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.video_note:
        file_id = message.video_note.file_id

    # Определяем пользователя: для системных событий берём участника события
    user = message.from_user
    if message.left_chat_member:
        user = message.left_chat_member
    elif message.new_chat_members:
        user = message.new_chat_members[0] if message.new_chat_members else user

    entry = {
        "message_id": message.message_id,
        "date": (message.date or datetime.now()).isoformat() if hasattr(message, "date") and message.date else datetime.now().isoformat(),
        "user_id": user.id if user else None,
        "user_name": (user.full_name if user else "") or "",
        "username": (user.username if user else "") or "",
        "is_bot": bool(getattr(user, "is_bot", False)) if user else False,
        "text": text[:500],
        "type": ctype,
        "file_id": file_id,
        "thumbnail_file_id": thumbnail_file_id,
        "file_name": file_name,
        "reply_to": message.reply_to_message.message_id if message.reply_to_message else None,
    }
    return entry


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Миграция message_logs.json в SQLite")
    parser.add_argument("--json", default=MESSAGE_LOGS_FILE, help="Путь к старому message_logs.json")
    parser.add_argument("--db", default=MESSAGE_LOGS_DB_FILE, help="Путь к новому SQLite-файлу")
    parser.add_argument(
        "--remove-json",
        action="store_true",
        help="Удалить JSON после успешной миграции",
    )
    args = parser.parse_args()

    stats = migrate_json_to_sqlite(args.json, args.db, remove_json=args.remove_json)
    print(
        "JSON→SQLite migration done: "
        f"{stats['chats']} chats, {stats['messages']} messages, {stats['skipped']} skipped"
    )


if __name__ == "__main__":
    _main()