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

### 2026-04-14 — Персональные настройки пользователя (/settings)

**Новые файлы:**
- **`app/user_settings.py`** — схема 21 настройки в 6 разделах (`UserSettings` dataclass), DB-функции (`get_settings`, `save_settings`, `update_setting`, `reset_settings`), LRU-кэш (512 слотов)
- **`app/settings_ui.py`** — полный inline-UI для Telegram: главное меню → раздел → настройка → изменить. Типы: bool (toggle), choice (выбор из списка), int_range (+/−). Callback data: `s:main`, `s:sec:{i}`, `s:set:{key}`, `s:val:{key}:{v}`, `s:inc:{key}:{delta}`, `s:rst`

**Изменённые файлы:**
- **`app/database.py`** — `init_db()` создаёт таблицу `user_settings` при старте
- **`app/handlers.py`** — команда `/settings` открывает меню; callback `s:*` маршрутизируется в `settings_ui`
- **`app/agents/raya_agent.py`** — `_build_hard_rules()` читает настройки пользователя: язык (`ru`/`en`), длина ответа (`short`/`medium`/`long`), стиль (`friendly`/`formal`/`concise`)
- **`app/proactive_service.py`** — `check_all_triggers()` проверяет `us.reminder_warning`, `us.task_reminders` перед каждым триггером
- **`app/feature_flags.py`** — добавлена `get_user_features(user_id)`: объединяет глобальные флаги с персональными настройками

**21 настройка в 6 разделах:**

| Раздел | Настройки |
|---|---|
| 🌐 Общие | Язык (ru/en), Длина ответов, Стиль общения, Часовой пояс (UTC±) |
| 🔍 Поиск | Включить поиск, Глубина (basic/advanced), Язык поиска (auto/ru/en) |
| 🌅 Проактивность | Утренний дайджест, Время дайджеста, Писать при молчании, Часов тишины, Напоминания о дедлайнах, Предупреждение за 30 мин |
| 🤖 Агенты | Генерация изображений, Брейнсторм, Проверка качества (critic) |
| 🧠 Память | Запоминать факты, Отслеживать настроение, Адаптировать стиль |
| 🔊 Медиа | Голосовые ответы (TTS), Индикатор «печатает...» |

---

### 2026-04-21 — Исправление памяти контекста + команда «забудь»

**Проблема:** бот запоминал `open_threads` (незавершённые темы) и навязчиво возвращался к ним в каждом следующем сообщении, даже если тема давно закрылась. Дополнительно: `NameError: name '_json' is not defined` в `save_conversation_context` — `json` был импортирован только локально внутри `get_conversation_context`, а использовался снаружи.

**Изменённые файлы:**

**`app/database.py`**
- Добавлен `import json as _json` на уровне модуля (был только локальный импорт внутри `get_conversation_context` — вызывал `NameError` при любом вызове `save_conversation_context` из другого места)
- Удалён дублирующий локальный `import json as _json` внутри `get_conversation_context`
- `format_context_for_prompt` переписан лаконично: вместо многострочного блока с заголовком — одна строка вида `💬 Контекст: тема: X | не закрыто: Y | Z`; лимит `open_threads` снижен с 3 до 2
- Добавлена функция `clear_open_threads(user_id)` — сбрасывает только `open_threads`, не трогая остальную память

**`app/llm_service.py`**
- В `_chat_inner` добавлен детектор намерения «забудь»: фразы «не запоминай», «забудь», «не надо», «не спрашивай об этом» и т.п. → вызывает `clear_open_threads(user_id)`
- Отдельно ловится «забудь всё / очисти память» → дополнительно вызывает `clear_structured_memory(user_id)`

---

### 2026-04-21 — Имя пользователя во всех агентах + оптимизация

**Проблема:** только `raya_agent` получал реальное имя из БД. Остальные агенты (`text`, `explain`, `ideas` и т.д.) получали абстрактную инструкцию "обращайся по имени" без самого имени — LLM брал что попадётся в истории или памяти, отсюда случайные "Иван", "Gleb63RSF" в разных сообщениях.

**Архитектурное решение:** имя теперь запрашивается один раз в `Orchestrator.run()` через кэшированный `get_user_name()` и кладётся в `AgentContext.user_name`. `BaseAgent._build_messages()` жёстко инжектирует его в системный промпт перед вызовом любого агента.

**Изменённые файлы:**

**`app/agents/base_agent.py`**
- Добавлено поле `user_name: str = "друг"` в `AgentContext`
- `_build_messages()` инжектирует жёсткое правило имени в системный промпт для всех агентов: `"Обращайся к пользователю ТОЛЬКО по имени '{ctx.user_name}'"`

**`app/agents/orchestrator.py`**
- Импортирован `get_user_name` из `app.database`
- `AgentContext` создаётся с `user_name=get_user_name(user_id)` — одна дешёвая операция (LRU-кэш 256 слотов), результат используется всеми агентами в цепочке

**`app/agents/raya_agent.py`**
- Правило #1 в `_build_hard_rules` обновлено: убрано дублирование, теперь только уточняет имя без противоречия с `base_agent`

**`app/agents/text_agent.py`**
- Убрана расплывчатая фраза "Обращайся к пользователю по имени" (без имени) — она провоцировала LLM угадывать

**Оптимизация для 25+ пользователей:**
- `get_user_name()` → LRU-кэш 256 слотов, обращение к БД только при первом запросе или после `invalidate_user_name_cache()`
- Один вызов `get_user_name` на всю цепочку агентов (в оркестраторе), а не в каждом агенте отдельно
