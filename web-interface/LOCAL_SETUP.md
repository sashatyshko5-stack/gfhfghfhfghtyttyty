# Как поднять веб-панель AI Defender локально

## 1. Запуск с реальным применением настроек

Этот режим поднимает фронтенд и маленький JSON-backend. Backend сохраняет настройки в `group_settings/<chat_id>/settings.json`.

```bash
cd /workspace/gfhfghfhfghtyttyty
AI_DEFENDER_SETTINGS_DIR=/workspace/group_settings PORT=8765 python3 web-interface/server.py
```

После запуска откройте:

```text
http://127.0.0.1:8765
```

Что важно:

- `AI_DEFENDER_SETTINGS_DIR` — путь к папке, где бот хранит настройки чатов.
- `PORT` — порт веб-панели, по умолчанию `8765`.
- Если папки чата нет, она создастся при первом сохранении.
- Сохранение происходит в JSON-файл `group_settings/<chat_id>/settings.json`.

## 2. Проверка backend API

Список чатов:

```bash
curl http://127.0.0.1:8765/api/chats
```

Проверочное сохранение настроек:

```bash
curl -X PUT http://127.0.0.1:8765/api/chats/-1001234567890/settings \
  -H 'Content-Type: application/json' \
  --data '{"settings":{"antispam":{"enabled":true},"ai_enabled":true}}'
```

Проверка созданного файла:

```bash
cat /workspace/group_settings/-1001234567890/settings.json
```

## 3. Запуск только фронтенда без backend

Если нужно просто открыть интерфейс и потом экспортировать JSON вручную:

```bash
cd /workspace/gfhfghfhfghtyttyty/web-interface
python3 -m http.server 8765
```

Откройте:

```text
http://127.0.0.1:8765
```

В таком режиме кнопка **Сохранить** не сможет применить настройки в backend, но можно использовать **Экспорт** и перенести JSON вручную.

## 4. Авторизация на локалке

- В Telegram Mini App авторизация идёт автоматически через Telegram WebApp.
- В обычном браузере доступны кнопка входа через Telegram и демо-вход для локальной настройки JSON.
- Для локальной разработки можно временно работать через Mini App URL с туннелем, например `ngrok` или аналогом, и указать HTTPS URL в BotFather.
