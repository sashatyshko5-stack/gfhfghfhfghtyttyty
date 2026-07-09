import json
import logging
import os
import shutil
import tempfile

from ..core.config import DEFAULT_SETTINGS, SETTINGS_DIR, SETTINGS_FILE

logger = logging.getLogger(__name__)

punished_users = set()
user_laozhang_keys: dict[int, str] = {}
user_messages = {}
chat_histories = {}
settings = {}
user_api_keys = {}
active_api_key = {}
user_recent_messages = {}
user_models = {}
chat_messages = {}
message_pages = {}
group_users = {}
user_last_seen = {}


def _chat_settings_path(chat_id):
    return os.path.join(SETTINGS_DIR, str(chat_id), "settings.json")


def _migrate_from_old_file():
    """Миграция старого единого settings.json в отдельные папки по чатам."""
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            logger.warning("Старый settings.json не является словарём, пропускаем миграцию")
            return
        for chat_id, chat_settings in loaded.items():
            if not isinstance(chat_settings, dict):
                continue
            chat_dir = os.path.join(SETTINGS_DIR, str(chat_id))
            os.makedirs(chat_dir, exist_ok=True)
            chat_file = os.path.join(chat_dir, "settings.json")
            with open(chat_file, "w", encoding="utf-8") as f:
                json.dump(chat_settings, f, ensure_ascii=False, indent=2)
        backup_path = SETTINGS_FILE + ".backup"
        shutil.move(SETTINGS_FILE, backup_path)
        logger.info(
            f"Миграция завершена: старый файл перенесён в {backup_path}, "
            f"чатов: {len(loaded)}"
        )
    except Exception as e:
        logger.error(f"Ошибка миграции старых настроек: {e}")


def load_settings():
    global settings
    settings.clear()
    _migrate_from_old_file()
    if not os.path.exists(SETTINGS_DIR):
        logger.info(
            f"Директория настроек {SETTINGS_DIR} не найдена, начинаем с пустых настроек"
        )
        return
    loaded_count = 0
    for entry in os.listdir(SETTINGS_DIR):
        chat_dir = os.path.join(SETTINGS_DIR, entry)
        if not os.path.isdir(chat_dir):
            continue
        chat_file = os.path.join(chat_dir, "settings.json")
        if not os.path.exists(chat_file):
            continue
        try:
            with open(chat_file, "r", encoding="utf-8") as f:
                chat_settings = json.load(f)
                # Нормализуем настройки: исправляем "мін" -> "мин"
                for key, value in chat_settings.items():
                    if isinstance(value, dict) and "unit" in value:
                        if value["unit"] == "мін":
                            value["unit"] = "мін"
                # Поверх DEFAULT_SETTINGS накладываем сохранённые поля,
                # чтобы такие флаги как privacy_accepted переживали рестарт.
                settings[entry] = {**DEFAULT_SETTINGS, **chat_settings}
                loaded_count += 1
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек для чата {entry}: {e}")
    accepted = sum(1 for v in settings.values() if v.get("privacy_accepted"))
    logger.info(
        f"Настройки загружены из {SETTINGS_DIR} "
        f"(чатов: {loaded_count}, принявших политику: {accepted})"
    )


def _save_chat_settings(chat_id):
    chat_dir = os.path.join(SETTINGS_DIR, str(chat_id))
    os.makedirs(chat_dir, exist_ok=True)
    chat_file = os.path.join(chat_dir, "settings.json")
    fd, tmp_path = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=chat_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings.get(str(chat_id), {}), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, chat_file)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def save_settings(chat_id=None):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        if chat_id is not None:
            _save_chat_settings(str(chat_id))
            logger.info(f"Настройки чата {chat_id} успешно сохранены")
        else:
            for cid in list(settings.keys()):
                _save_chat_settings(str(cid))
            logger.info(f"Настройки всех чатов успешно сохранены в {SETTINGS_DIR}")
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")
        raise


def save_chat_settings(chat_id):
    save_settings(chat_id)
