# Деплой @defende125_bot

## Переменные окружения (нужны везде)

```
BOT_TOKEN=токен_от_botfather
WEB_ADMIN_USERNAME=ваш_логин
WEB_ADMIN_PASSWORD=ваш_пароль
WEB_JWT_SECRET=любая_строка_минимум_32_символа
```

---

## Hugging Face Spaces

> У вас уже есть `hf-pinger.py` — значит вы знакомы с HF. Для бота нужен **Docker Space**, потому что Gradio/Streamlit не поддерживают фоновые процессы.

**1.** Создайте новый Space → SDK: **Docker**

**2.** Создайте файл `Dockerfile` в корне бота:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 7860

CMD ["python", "main.py"]
```

**3.** Задайте секреты в Space → Settings → Repository secrets:

```
BOT_TOKEN=...
WEB_ADMIN_USERNAME=...
WEB_ADMIN_PASSWORD=...
WEB_JWT_SECRET=...
PORT=7860
```

> `PORT=7860` обязательно — HF Spaces пробрасывает именно этот порт наружу. Бот сам его подхватит.

**4.** Залейте код в репозиторий Space через Git или загрузите файлы вручную через UI.

**5.** Space запустится автоматически. Веб-панель будет доступна по URL вашего Space:  
`https://ВАШ_ПРОФИЛЬ-ИМЯ_SPACE.hf.space`

**Логин:** те данные, что указали в секретах (`WEB_ADMIN_USERNAME` / `WEB_ADMIN_PASSWORD`).

---

## Railway

**1.** Зайдите на [railway.app](https://railway.app) → New Project → Deploy from GitHub (подключите репозиторий с ботом).

**2.** В настройках сервиса → Variables добавьте:

```
BOT_TOKEN=...
WEB_ADMIN_USERNAME=...
WEB_ADMIN_PASSWORD=...
WEB_JWT_SECRET=...
```

> `PORT` Railway выставляет сам — ничего дополнительно не нужно, бот его подхватит автоматически.

**3.** В Settings → Start Command укажите:

```
python main.py
```

**4.** Deploy. Railway выдаст публичный URL — это и есть адрес веб-панели.

---

## VPS (Ubuntu/Debian)

**1.** Загрузите файлы бота на сервер (через FileZilla, scp или git clone).

**2.** Установите зависимости:

```bash
pip install -r requirements.txt
```

**3.** Создайте `.env` в корне бота:

```
BOT_TOKEN=...
WEB_ADMIN_USERNAME=...
WEB_ADMIN_PASSWORD=...
WEB_JWT_SECRET=...
WEB_PORT=8080
```

**4.** Запустите бота в фоне:

```bash
screen -S bot
python main.py
# Ctrl+A, D — чтобы выйти не убивая процесс
```

**5.** Откройте порт 8080 в фаерволе:

```bash
ufw allow 8080
```

**6.** Веб-панель доступна по адресу: `http://IP-СЕРВЕРА:8080`

---

## Pterodactyl

**1.** Загрузите файлы бота в файловый менеджер панели.

**2.** В настройках сервера → Startup задайте переменные:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен от BotFather |
| `WEB_ADMIN_USERNAME` | логин для панели |
| `WEB_ADMIN_PASSWORD` | пароль для панели |
| `WEB_JWT_SECRET` | любая строка ≥32 символов |
| `WEB_PORT` | 8080 (или любой открытый порт сервера) |

**3.** Startup Command:

```
python main.py
```

**4.** Запустите сервер. Веб-панель будет на IP:порт вашего сервера.

---

## ⚠️ Не забудьте

- Смените токен бота — в исходниках был открытый `BOT_TOKEN`.
- `WEB_JWT_SECRET` — любой набор символов ≥32, например:  
  `mK9sLpQ2xNvR7hGdWbTyUeAcZfJo3iE8`
