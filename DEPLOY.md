# Деплой RaYa + DEEper на Railway

## Что понадобится (3 ключа)

| Ключ | Где получить | Бесплатно |
|------|-------------|-----------|
| `TELEGRAM_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` | ✅ |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys | ✅ (лимиты) |
| `TAVILY_API_KEY` | [tavily.com](https://tavily.com) → Dashboard | ✅ 1000 req/мес |

---

## Шаг 1 — Получить ключи

### Telegram Bot Token
1. Открыть [@BotFather](https://t.me/BotFather) в Telegram
2. Отправить `/newbot`
3. Придумать имя бота (напр. `MyRaYa Bot`)
4. Придумать username (напр. `my_raya_bot`) — должен заканчиваться на `bot`
5. Скопировать токен вида `7123456789:AAHxxx...`

### Groq API Key
1. Зайти на [console.groq.com](https://console.groq.com)
2. Зарегистрироваться (бесплатно)
3. API Keys → Create API Key
4. Скопировать ключ вида `gsk_xxx...`

### Tavily API Key
1. Зайти на [tavily.com](https://tavily.com)
2. Sign Up → Dashboard
3. Скопировать API Key вида `tvly-xxx...`

---

## Шаг 2 — Загрузить проект на GitHub

```bash
# В папке проекта
git init
git add .
git commit -m "Initial commit"

# Создать репо на github.com и подключить
git remote add origin https://github.com/USERNAME/raya-bot.git
git push -u origin main
```

---

## Шаг 3 — Создать проект на Railway

1. Зайти на [railway.app](https://railway.app) → New Project
2. Deploy from GitHub repo → выбрать репозиторий
3. Railway автоматически определит Python-проект

---

## Шаг 4 — Добавить Persistent Volume

1. В проекте Railway: **+ New** → **Volume**
2. Mount Path: `/app/data`
3. Нажать **Create**

> Это важно — без volume данные (база, настройки, DEEper KB) сбрасываются при каждом деплое.

---

## Шаг 5 — Переменные окружения

В Railway: Settings → Variables → **Add Variable** для каждой:

```
TELEGRAM_TOKEN      = <твой токен>
GROQ_API_KEY        = <твой groq ключ>
TAVILY_API_KEY      = <твой tavily ключ>
OWNER_USER_ID       = 0
MODEL_NAME          = llama-3.3-70b-versatile
ROUTER_MODEL        = llama-3.1-8b-instant
DEEPER_DATA_DIR     = /app/data/deeper
SETTINGS_FILE       = /app/data/user_settings.json
DB_PATH             = /app/data/database.db
```

> `OWNER_USER_ID = 0` пока — позже узнаешь свой id.

---

## Шаг 6 — Деплой

Railway задеплоит автоматически после добавления переменных.

Смотри логи: **Deployments** → последний деплой → **View Logs**

Сразу после деплоя (до первого сообщения боту) должно появиться:
```
INFO | app.database | ✅ База данных готова: ...
INFO | app.core | 🤖 RaYa запущена | модель: llama-3.3-70b-versatile | ...
INFO | app.proactive_service | 🌅 Проактивный сервис запущен | ...
INFO | app.health | 🩺 Health-check слушает на 0.0.0.0:PORT/health
```

Строка `🧠 Оркестратор инициализирован` появится **позже** — только после первого сообщения боту (агенты создаются лениво, не при старте). Не пугайся если её нет сразу.

---

## Шаг 7 — Узнать свой Telegram ID

1. Написать боту `/start`
2. В логах Railway найти строку:
   ```
   WARNING | app.middleware | 🚫 Отклонён user_id=123456789
   ```
   Или если `OWNER_USER_ID=0` — бот ответит всем, id будет в логах.
3. Скопировать number, вставить в Railway Variables: `OWNER_USER_ID = 123456789`
4. Railway перезапустит автоматически

---

## Шаг 8 — Проверить что всё работает

Написать боту:
- `/start` — должен поздороваться
- `/help` — список команд
- `/settings` — inline-меню настроек
- `/memory` — состояние памяти (пустая — это норма)
- `Привет, как дела?` — ответ от LLM
- `/deeper что такое MCP протокол` — запустит DEEper

---

## Лимиты бесплатных планов

| Сервис | Лимит | Хватит на |
|--------|-------|-----------|
| Railway | $5/мес кредитов | ~500 часов работы бота |
| Groq | 30 req/min, 6000 req/day | комфортное личное использование |
| Tavily | 1000 req/мес | ~33 поиска в день |

---

## Troubleshooting

**Бот не отвечает:**
- Проверь логи Railway — нет ли ошибки `TELEGRAM_TOKEN`
- Проверь что `OWNER_USER_ID` правильный (или = 0)

**DEEper не находит информацию:**
- Проверь `TAVILY_API_KEY` — он точно правильный?
- Tavily ключ начинается с `tvly-`

**MemoryError / OOM на Railway:**
- Railway free tier = 512MB RAM
- Если превышает — перейди на Starter ($5/мес = 8GB RAM)
