# Деплой веб-интерфейса AI Defender

Фронтенд полностью отделён от бота: это статический Mini App/SPA, который хранит настройки в JSON и умеет импортировать/экспортировать файл `settings.json` в формате, совместимом с текущей схемой бота (`antispam`, `antiraid`, `antinsfw`, `ai_provider`, `ai_model`, `ai_keys`, `custom_provider`).

## Перед деплоем

1. Откройте `index.html` локально или через любой статический сервер.
2. В Telegram Mini App авторизация сработает автоматически через `window.Telegram.WebApp`.
3. В обычном браузере используйте Telegram Login Widget на своём домене или демо-вход для подготовки JSON.
4. После настройки нажмите **Экспорт** и перенесите JSON в backend/бота. Сейчас хранение сделано через `localStorage` + экспорт JSON, потому что в проекте нет отдельного HTTP API backend.

## Метод 1: Hugging Face Spaces

1. Создайте Space на Hugging Face: **New Space** → SDK: **Static**.
2. Загрузите в Space содержимое папки `web-interface`:
   - `index.html`
   - `styles.css`
   - `app.js`
   - `DEPLOY.md`
3. Дождитесь билда. Space выдаст URL вида `https://<user>-<space>.hf.space`.
4. В BotFather настройте Mini App/Web App URL на этот адрес.
5. Если нужен браузерный вход через Telegram Login Widget, добавьте домен Space в BotFather через `/setdomain`.

## Метод 2: Replit

1. Создайте новый Repl: **HTML, CSS, JS**.
2. Скопируйте файлы из `web-interface` в корень Repl.
3. Убедитесь, что `index.html` лежит в корне и подключает `./styles.css` и `./app.js`.
4. Нажмите **Run** и откройте публичный URL Replit.
5. В BotFather укажите этот URL как URL Mini App/Web App.
6. Для браузерного входа добавьте домен Replit в BotFather через `/setdomain`.

## Как применить настройки в боте

1. В интерфейсе выберите чат или добавьте новый чат по Telegram chat id.
2. Настройте антиспам, антирейд, 18+ защиту и ИИ.
3. Нажмите **Сохранить**, затем **Экспорт**.
4. Полученный `settings.json` можно использовать как источник для переноса в папку `group_settings/<chat_id>/settings.json` на backend стороне.

## Подключение настоящего backend позже

Чтобы сделать сохранение без ручного экспорта, добавьте backend API поверх текущего JSON-хранилища бота:

- `GET /api/me` — вернуть пользователя после проверки Telegram initData.
- `GET /api/chats` — список доступных чатов администратора.
- `GET /api/chats/:chatId/settings` — настройки конкретного чата.
- `PUT /api/chats/:chatId/settings` — сохранить JSON в `group_settings/<chatId>/settings.json`.

Фронтенд уже использует ту же структуру данных, поэтому интеграция сведётся к замене `localStorage` на HTTP-запросы.
