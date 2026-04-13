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

## Надёжность при 25+ пользователях

```bash
python3 audit.py   # проверка перед push
git add -A && git commit -m "..." && git push origin main
```

Railway подхватывает push автоматически.

---

## Поиск в интернете

Трёхслойная архитектура: **кэш → Tavily → DuckDuckGo fallback**

| Слой | Что делает |
|---|---|
| TTL-кэш (10 мин) | Срезает повторные запросы. При 25+ пользователях экономит 60–80% вызовов API |
| Tavily | Основной движок — высокое качество, платный |
| DuckDuckGo | Бесплатный fallback при ошибке Tavily или исчерпании квоты (429) |

**Свежесть данных:**
- `_enrich_query()` — автоматически добавляет год к time-sensitive запросам (`"курс доллара"` → `"курс доллара 2026"`), чтобы поисковик ранжировал свежие результаты выше архивных
- `_freshness_header()` — добавляет метку `[Данные получены: 12.04.2026 16:40 UTC]` к результатам, чтобы LLM знал что информация актуальна и не подменял её знаниями из обучения
- `_build_date_block()` в raya_agent — явно сообщает модели текущую дату и инструктирует доверять поиску больше чем обучающим данным

- **Rate limiting** — 1 запрос / 3 сек на пользователя
- **Глобальный семафор** — не более 20 параллельных LLM-запросов
- **SQLite WAL** — множественные читатели не блокируют запись
- **busy_timeout 15s** — при конкурентных запросах ждёт вместо падения
- **Retry с exponential backoff** — 5 попыток при `SQLITE_BUSY`
- **LRU-кэш** имён пользователей — не дёргаем БД на каждый запрос
- **Lazy init агентов** — создаются только при первом обращении

---

## Changelog

### 2026-04-13 — Улучшение агента поиска
- **`app/llm_service.py`** — переписан промпт LLM-классификатора поиска. Добавлено правило «при сомнении — ИСКАТЬ». Новые примеры с миссиями/проектами (Артемида, SpaceX, GPT). Явные триггеры: "запуск", "миссия", "статус", "artemis"
- **`app/search_service.py`** — расширен `_TIME_SENSITIVE_KW` (добавлены названия миссий и проектов). Раздельные TTL-кэши: обычные 10 мин, событийные/новостные 2 мин. Метод `_is_event_query()`
- **`app/agents/research_agent.py`** — `_build_search_queries()` строит 2–4 параллельных запроса. Используется `multi_search()` вместо одного `search()`. Контекст поиска 2000 → 4000 символов. Если поиск пуст — LLM явно предупреждается об устаревших данных. Системные промпты: `[Данные из поиска]` = приоритет над знаниями модели
- **`app/agents/raya_agent.py`** — жёсткие правила: обязательно использовать `[Данные из поиска]` как приоритетный источник; для событий указывать дату актуальности

### 2026-04-13 — Улучшения агента поиска #2: iterative search, bilingual, freshness
- **`app/search_service.py`**
  - `iterative_search()` — если первый раунд нерелевантен, `_refine_query()` (router_model) переформулирует запрос и ищет снова; результаты обоих раундов объединяются
  - `_should_retry()` — эвристика: retry если мало результатов, контент короткий или <40% слов запроса встречается в ответах
  - `_refine_query()` — LLM (router_model, T=0.2) генерирует альтернативный запрос на основе того что нашли в 1-м раунде
  - `_is_international()` / `_translate_key_terms()` — определяет международные темы и переводит ключевые термины на английский
  - `_tavily_search()` — freshness scoring: +0.15 к score если в тексте текущий год, штраф за упоминание старых годов
  - `_format_raw()` — теперь показывает дату публикации источника если Tavily её вернул
- **`app/agents/research_agent.py`**
  - `_build_search_queries()` — для международных тем добавляет английский вариант запроса
  - `_execute()` — использует `iterative_search()` как основной метод + `multi_search()` для дополнительных углов

### 2026-04-13 — Улучшения агента поиска #3: специализированные источники, full-page fetch, evaluate pipeline
- **`app/search_service.py`**
  - `news_search()` — специализированный поиск через Tavily `topic="news"`, TTL-кэш 2 мин. Приоритизирует свежие новостные публикации
  - `academic_search()` — поиск с site-hints (`arxiv.org`, `pubmed`, `nature.com`), переключается на английский для международных тем
  - `_tavily_search()` — принимает `topic` параметр, передаёт в Tavily API
  - `_fetch_full_page()` — скачивает полный текст страницы через `httpx` + `trafilatura`, до 3000 символов. Fallback через regex-очистку если trafilatura недоступен
  - `enrich_top_result()` — обогащает топ-1 результат полным текстом если сниппет < 300 символов
  - `smart_search()` — главный pipeline: выбор метода по mode → iterative retry → LLM-оценка качества → EN-fallback если score < 0.4 → full-page fetch → форматирование
  - `_evaluate_results()` — router_model оценивает релевантность топ-3 результатов (float 0.0–1.0). При score < 0.4 триггерится EN-поиск
- **`app/agents/research_agent.py`** — использует `smart_search()` вместо `iterative_search()`. Контекст поиска увеличен до 5000 символов
- **`requirements.txt`** — добавлен `trafilatura` для full-page fetch

### 2026-04-13 — Улучшения агента поиска #4: semantic dedup, structured extraction, persistent cache
- **`app/search_service.py`**
  - `semantic_deduplicate()` — дедупликация по смыслу через TF-IDF + cosine similarity (без внешних API). Порог 0.72 — убирает парафразы, оставляет разные точки зрения. Дубли учитываются в поле `confirmed_by` (показывается как `✓×N` в результатах)
  - `extract_event_facts()` — для событийных запросов (миссии, релизы, события) router_model извлекает структурированный JSON: статус, дата, место, участники, ключевые факты. Добавляется в начало контекста поиска перед сниппетами
  - `smart_search()` — pipeline расширен: шаг 0 — persistent knowledge cache; шаг 2.5 — semantic dedup; шаг 4.5 — structured extraction
  - `__init__()` — при старте чистит истёкшие записи knowledge cache
- **`app/database.py`**
  - Новая таблица `knowledge_cache` (query_hash, query, result, mode, hits, expires_at)
  - `kc_get()` — читает из persistent cache, инкрементирует hits
  - `kc_set()` — сохраняет результат; TTL: 2 ч для событий, 24 ч для остального
  - `kc_cleanup()` — удаляет истёкшие записи
  - `kc_stats()` — статистика: всего записей, активных, топ-5 по hits
