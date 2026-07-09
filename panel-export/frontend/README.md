# Панель управления @defende125_bot — Фронтенд

## Быстрый старт

```bash
npm install
VITE_API_BASE_URL=https://ВАШ-ДОМЕН-БОТА.example.com npm run build
# Результат сборки: dist/public/
```

## Зависимости

Этот фронтенд использует локальные пакеты из monorepo.
При отдельном деплое замените импорты из `@workspace/api-client-react`
на скопированные файлы из `lib/api-client-react/src/`.

## Переменные окружения

| Переменная | Описание |
|---|---|
| `VITE_API_BASE_URL` | URL Python-бэкенда (например `https://mybot.example.com`) |

Если не задана — запросы идут на тот же хост (нужен reverse-proxy).
