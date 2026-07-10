# Деплой веб-интерфейса AI Defender

Фронтенд отделён от бота, но теперь умеет применять настройки через маленький JSON-backend `server.py`: он пишет настройки в `group_settings/<chat_id>/settings.json` тем же атомарным способом, который использует бот. Если backend недоступен после авторизации, интерфейс работает в локальном режиме: настройки сохраняются в `localStorage`, их можно экспортировать в `settings.json`.

## Быстрый локальный запуск

Подробная локальная инструкция вынесена в `LOCAL_SETUP.md`. Минимальная команда:

```bash
cd /workspace/gfhfghfhfghtyttyty
AI_DEFENDER_SETTINGS_DIR=/workspace/group_settings PORT=8765 python3 web-interface/server.py
```

После запуска откройте `http://127.0.0.1:8765`.

## Локальный запуск с реальным применением настроек

```bash
cd web-interface
python3 server.py
```

По умолчанию сервер слушает `http://127.0.0.1:8765` и пишет в папку `../group_settings` относительно корня репозитория бота. Чтобы указать другое хранилище:

```bash
AI_DEFENDER_SETTINGS_DIR=/path/to/group_settings python3 server.py
```

Доступные endpoints:

- `GET /api/me` — служебная проверка API; браузерный режим использует кнопку входа через Telegram и локальный демо-вход.
- `GET /api/chats` — список чатов из `group_settings`.
- `GET /api/chats/:chatId/settings` — настройки чата.
- `PUT /api/chats/:chatId/settings` — сохранить настройки в `group_settings/<chatId>/settings.json`.

## Авторизация

- В Telegram Mini App интерфейс берёт пользователя из `window.Telegram.WebApp.initDataUnsafe.user`.
- В обычном браузере интерфейс показывает вход через Telegram и демо-вход для локальной настройки JSON.
- Для production нужно добавить серверную проверку подписи Telegram initData перед выдачей списка чатов.

## Метод 1: Hugging Face Spaces

1. Создайте Space на Hugging Face.
2. Если нужен только статический фронтенд с экспортом JSON, выберите SDK **Static** и загрузите:
   - `index.html`
   - `styles.css`
   - `app.js`
   - `DEPLOY.md`
3. Если нужно реальное сохранение JSON, выберите Python Space/Docker Space и запускайте `python3 web-interface/server.py`.
4. Дождитесь билда. Space выдаст URL вида `https://<user>-<space>.hf.space`.
5. В BotFather настройте Mini App/Web App URL на этот адрес.
6. Если нужен браузерный Telegram Login Widget, добавьте домен Space в BotFather через `/setdomain`.

## Метод 2: Replit

1. Создайте новый Repl.
2. Для отдельного фронтенда можно выбрать **HTML, CSS, JS** и скопировать `index.html`, `styles.css`, `app.js` в корень Repl.
3. Для реального сохранения выберите Python Repl, скопируйте папку `web-interface` и запустите:

```bash
python3 web-interface/server.py
```

4. В переменных окружения Replit можно задать `AI_DEFENDER_SETTINGS_DIR`, если папка настроек лежит не рядом с репозиторием.
5. Откройте публичный URL Replit и укажите его в BotFather как URL Mini App/Web App.
6. Для браузерного Telegram Login Widget добавьте домен Replit в BotFather через `/setdomain`.

## Как применить настройки в боте

1. Запустите `server.py` рядом с backend-хранилищем или задайте `AI_DEFENDER_SETTINGS_DIR`.
2. Откройте панель, выберите чат или добавьте новый chat id.
3. Настройте антиспам, антирейд, 18+ защиту и ИИ.
4. Нажмите **Сохранить** — при доступном backend настройки будут записаны в `group_settings/<chat_id>/settings.json`.
5. Если backend недоступен, после Telegram-входа нажмите **Экспорт** и перенесите JSON вручную.
