# RaYa — Personal AI Assistant

Telegram-бот + веб-интерфейс на мультиагентной архитектуре.  
Помнит контекст разговора, управляет задачами и напоминаниями, анализирует файлы и изображения.

**Деплой:** Railway.app  
**Бот:** публичный — любой может написать

---

## Стек

| Компонент | Технология |
|---|---|
| LLM (основная) | Groq — `llama-3.3-70b-versatile` |
| LLM (роутер) | Groq — `llama-3.1-8b-instant` |
| Speech-to-Text | Groq Whisper — через VoiceService |
| Vision | Groq Vision — через VisionService |
| Image Generation | Hugging Face FLUX.1-schnell |
| TTS | gTTS |
| Telegram | aiogram 3.17.0 |
| Database | SQLite WAL — Railway Volume `/data/database.db` |
| Web | FastAPI + uvicorn |
| Поиск | Tavily API (опционально) |
| Хостинг | Railway.app |

---

## Архитектура

```
Сообщение пользователя
        │
   AccessMiddleware (допуск)
   Rate Limiting (1 req/3s per user)
        │
   Handlers (handlers.py)
        │
   LLMService → Router (keyword match → LLM fallback)
        │
   Orchestrator
        │
   raya / code / image / research / todo / text / ideas / explain
                                                          │
                                                     critic (если нужен)
```

### Агенты (10 активных)

| Агент | Что делает | Триггеры |
|---|---|---|
| **raya** | Главный fallback — общий диалог, поиск | всё остальное |
| **code** | Python/JS/SQL/bash — пишет, отлаживает | код, баг, функция, скрипт |
| **image** | Генерация через FLUX.1-schnell | нарисуй, картинку |
| **research** | Tavily + fact-check | исследуй, проверь факт |
| **todo** | Задачи: добавить, показать, выполнить | задача, список, дедлайн |
| **text** | Резюме, редактура, перевод, письма | перепиши, переведи |
| **ideas** | Брейнсторм, SCAMPER | идеи, придумай |
| **explain** | Объяснения + планы с шагами | объясни, план, как сделать |
| **morning** | Утренний дайджест (авто 6:45 МСК) | только автоматически |
| **critic** | Финальная проверка качества | только программно |

---

## База данных (SQLite WAL)

| Таблица | Содержимое |
|---|---|
| `history` | История переписки (role: human/ai) |
| `user_memory` | Простые факты (legacy) |
| `structured_memory` | Категоризованная память (факты, интересы, проекты, цели) |
| `interaction_memory` | Топ-темы и паттерны разговора |
| `conversation_context` | Текущая тема, цель, незавершённые треды |
| `reminders` | Напоминания с поддержкой повторений (daily/weekly/weekday/monthly) |
| `diary` | Записи дневника с настроением |
| `tasks` | Задачи |
| `mood_log` | Трекинг настроения |
| `events` | События календаря |
| `users` | Профили пользователей |

---

## Telegram команды

| Команда | Что делает |
|---|---|
| `/start` | Приветствие, регистрация |
| `/help` | Список возможностей |
| `/memory` | Что бот знает о тебе |
| `/forget` | Удалить всю память |
| `/clear` | Очистить историю (память сохраняется) |
| `/reminders` | Список активных напоминаний |

Бот также принимает: голосовые сообщения (Whisper), фото (Vision), PDF и Word документы.

---

## Веб-интерфейс (FastAPI)

Доступен по адресу деплоя. Защита через `?token=YOUR_WEB_TOKEN`.

Основные эндпоинты: `/api/chat`, `/api/history`, `/api/memory`, `/api/reminders`, `/api/context`, `/api/diary`, `/api/voice`, `/api/tasks`, `/api/calendar/...`, `/api/search`, `/api/status`, `/api/features`.

---

## Проактивные функции

| Триггер | Когда | Флаг |
|---|---|---|
| Утренний дайджест | 6:45 МСК | `FEATURE_MORNING_DIGEST` |
| Дедлайны задач | Раз в час | `FEATURE_TASK_DEADLINES` |
| Напоминание за 30 мин | Каждую минуту | `FEATURE_REMINDER_WARNING` |
| Тишина > 4ч | Каждые 4ч | `FEATURE_PROACTIVE_SILENCE` |
| Follow-up идей из дневника | Раз в 12ч | `FEATURE_PROACTIVE_IDEA` |
| Предложения по паттернам | Раз в 24ч | `FEATURE_PROACTIVE_ACTIVITY` |

Состояние сохраняется в `proactive_state.json` — переживает рестарты Railway.

---

## Railway Variables

```
# Обязательные
GROQ_API_KEY=
TELEGRAM_TOKEN=

# Рекомендуемые
TELEGRAM_USER_ID=       # твой user_id (написать /start, смотреть логи)
WEB_TOKEN=              # токен для веб-интерфейса

# Доступ (пусто = публичный бот)
ALLOWED_USER_IDS=       # пример: 123456789,987654321

# Опциональные
TAVILY_API_KEY=         # поиск в интернете
HF_TOKEN=               # генерация изображений

# Можно не трогать (дефолты)
MODEL_NAME=llama-3.3-70b-versatile
ROUTER_MODEL=llama-3.1-8b-instant
TEMPERATURE=0.7
MAX_HISTORY=20
DB_PATH=/data/database.db
```

---

## Feature Flags

```
FEATURE_IMAGE_AGENT=1
FEATURE_IDEAS_AGENT=1
FEATURE_MORNING_DIGEST=1
FEATURE_TASK_DEADLINES=1
FEATURE_REMINDER_WARNING=1
FEATURE_PROACTIVE_SILENCE=0
FEATURE_PROACTIVE_IDEA=0
FEATURE_PROACTIVE_ACTIVITY=0
FEATURE_PERSONA_VERBOSE=1
FEATURE_EMOTIONAL_SYSTEM=1
```

---

## Структура проекта

```
RaYa-Assist/
├── main.py
├── persona.txt                 ← системный промпт / личность бота
├── audit.py                    ← проверка перед деплоем
├── requirements.txt
├── Procfile
├── nixpacks.toml
└── app/
    ├── config.py               ← pydantic-settings
    ├── core.py                 ← инициализация сервисов
    ├── database.py             ← SQLite WAL, все таблицы
    ├── handlers.py             ← Telegram хендлеры + rate limiting
    ├── middleware.py           ← AccessMiddleware
    ├── llm_service.py          ← точка входа LLM
    ├── llm_pipeline.py         ← Memory/Context/ToneController
    ├── proactive_service.py    ← фоновые триггеры + планировщик
    ├── search_service.py       ← Tavily
    ├── voice_service.py        ← Whisper
    ├── vision_service.py       ← анализ изображений
    ├── tts_service.py          ← gTTS
    ├── document_service.py     ← PDF + Word
    ├── calendar_service.py
    ├── personality_service.py
    ├── feature_flags.py
    ├── utils.py
    ├── web_server.py           ← FastAPI
    └── agents/
        ├── registry.py         ← реестр + keyword matching
        ├── router.py           ← двухуровневый роутинг
        ├── orchestrator.py     ← координатор
        ├── base_agent.py       ← AgentContext / AgentResult
        ├── raya_agent.py
        ├── code_agent.py
        ├── image_agent.py
        ├── research_agent.py
        ├── todo_agent.py
        ├── text_agent.py
        ├── ideas_agent.py
        ├── explain_agent.py
        ├── morning_agent.py
        └── critic_agent.py
```

---

## Деплой

```bash
python3 audit.py   # проверка перед push
git add -A && git commit -m "..." && git push origin main
```

Railway подхватывает push автоматически.

---

## Надёжность при 25+ пользователях

- **Rate limiting** — 1 запрос / 3 сек на пользователя
- **Глобальный семафор** — не более 20 параллельных LLM-запросов
- **SQLite WAL** — множественные читатели не блокируют запись
- **busy_timeout 15s** — при конкурентных запросах ждёт вместо падения
- **Retry с exponential backoff** — 5 попыток при `SQLITE_BUSY`
- **LRU-кэш** имён пользователей — не дёргаем БД на каждый запрос
- **Lazy init агентов** — создаются только при первом обращении
