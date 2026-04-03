# RaYa — Personal AI Manager

> "AI, который реально заставляет тебя делать задачи"

Персональный Telegram-бот + веб-интерфейс на мультиагентной архитектуре.
Хранит задачи, события, заметки и знания — связывает их через Obsidian vault.

**Деплой:** Railway.app  
**Веб:** `https://raya-assist-production.up.railway.app?token=sokrat`  
**WebDAV:** `https://raya-assist-production.up.railway.app/webdav`  
**Репо:** `github.com/MrBoomer1981/RaYa-Assist` (private)

---

## Стек

| Компонент | Технология |
|---|---|
| LLM (основная) | Groq — `llama-3.3-70b-versatile` |
| LLM (роутер) | Groq — `llama-3.1-8b-instant` (T=0) |
| Embeddings | Groq — `nomic-embed-text-v1_5` |
| Vision | Groq — `llama-4-scout-17b-16e-instruct` |
| Speech-to-Text | Groq Whisper — `whisper-large-v3-turbo` |
| Image Generation | Hugging Face — `FLUX.1-schnell` |
| Telegram | aiogram 3.17.0 |
| Database | SQLite WAL — Railway Volume `/data/database.db` |
| Web | FastAPI + uvicorn |
| Поиск | Tavily API |
| Vault sync | WebDAV встроен в FastAPI |
| Хостинг | Railway.app |

---

## Архитектура

```
Сообщение пользователя
        │
   Router (keyword match → LLM fallback)
        │
   Orchestrator
        │
   ┌────┴──────────────────────────────────────┐
   ▼    ▼      ▼        ▼      ▼     ▼    ▼   ▼
 raya  code  image  research  todo  obs  text  ideas
  │                                            ▲
  └── vault tool (19 операций, tool use API) ──┤
                                               │
                                           explain
                                           morning
                                           critic ←── needs_critic
```

### Агенты (11 активных)

| Агент | Что делает | Ключевые слова |
|---|---|---|
| **raya** | Главный fallback + vault tool use | всё остальное |
| **code** | Python/JS/SQL/bash — пишет, отлаживает | код, баг, функция |
| **image** | Генерация FLUX.1-schnell | нарисуй, картинку |
| **research** | Tavily + auto-save в Zettelkasten | исследуй, найди, проверь |
| **todo** | Матрица Эйзенхауэра, fuzzy match | задача, список, сделать |
| **obsidian** | Дневник, заметки, Zettelkasten, планы | запомни, запиши, zettel |
| **text** | Редактирование, перевод, письма | перепиши, переведи |
| **ideas** | Брейнсторм, SCAMPER | идеи, придумай |
| **explain** | Объяснение + планирование с дедлайнами | объясни, план |
| **morning** | Дайджест: погода + задачи + цитата + новости | (авто в 6:45 МСК) |
| **critic** | Финальная проверка качества | (только программно) |

---

## Obsidian Vault

**Единственный источник правды** для задач и знаний.  
Синхронизируется через WebDAV ↔ Remotely Save на Mac.

```
/data/obsidian_vault/RaYa-Vault/
├── Задачи/
│   ├── Q1.md  — 🔴 Срочно и важно
│   ├── Q2.md  — 🟡 Важно, не срочно
│   ├── Q3.md  — 🟠 Срочно, не важно
│   └── Q4.md  — ⚪ Не срочно, не важно
├── Дневник/YYYY-MM/YYYY-MM-DD.md  ← daily notes (НЕ в графе знаний)
├── Заметки/                        ← структурированные заметки
├── Zettelkasten/                   ← база знаний, граф [[ссылок]]
└── Планы/
    ├── Краткосрочные/
    └── Долгосрочные/
```

**Задачи:**
- fuzzy match при отметке выполненными
- drag & drop между квадрантами в UI
- кнопка ↩ для возврата выполненных в активные
- дедлайны в формате `до ДД.ММ` — подсвечиваются в UI
- синхронизация БД ↔ Obsidian при старте

**База знаний (Zettelkasten):**
- атомарные карточки с тегами и [[ссылками]]
- dedup по схожести слов — не создаёт дубли
- research_agent автоматически сохраняет найденное

---

## Веб-интерфейс

Главный экран — **Календарь** с месячным и дневным видом.

### Панели

| Панель | Что внутри |
|---|---|
| 📅 Календарь | Месячный вид, кликнуть на день → расписание + заметки |
| 💬 Чат | Диалог с RaYa, голосовые, markdown |
| ◈ Задачи | Матрица Эйзенхауэра, drag&drop, undo |
| **Дополнительно** | Память, Дневник, Напоминания, Поиск |

### Aside (правая панель)
- **Контекст** — текущая тема разговора, цель, задачи
- **Неделя** — план задач на 7 дней

---

## Календарь

События хранятся в SQLite (`events`). При создании записываются в Obsidian Daily Note (односторонняя запись, без синхронизации обратно).

**Через Telegram:** "добавь встречу с врачом в пятницу в 14:00"  
**Через UI:** клик на день → клик на слот → модалка события

---

## Утренний дайджест (6:45 МСК)

Без LLM вызовов — быстро и предсказуемо:

1. **Погода** — точные данные с wttr.in (макс/мин, влажность, совет что надеть)
2. **Задачи** — Q1 первые, потом Q2 из Obsidian
3. **Цитата** — 30 живых цитат (не корпоративные банальности)
4. **Философия** — 3 случайных из 26 глубоких вопросов (не банальности)
5. **Дайджест дня** — параллельный поиск по 4 случайным темам:
   - 🤖 AI и технологии
   - 💼 Бизнес и стартапы
   - 🔬 Наука
   - 🌍 Мировые события
   - 🧠 Психология и продуктивность

---

## Semantic Search

Семантический поиск по Obsidian vault через Groq embeddings.

- Модель: `nomic-embed-text-v1_5` (бесплатно)
- Индекс: `vault_index.json` на Railway Volume (инкрементальный)
- Кэш: 3600 сек в памяти
- Порог релевантности: cosine > 0.3
- Атомарная запись индекса (защита от crash)
- Пересборка: `POST /api/search/index?token=sokrat`

---

## Проактивность

| Триггер | Статус | Когда |
|---|---|---|
| Утренний дайджест | ✅ ON | 6:45 МСК |
| Дедлайны задач | ✅ ON | раз в час |
| Напоминания | ✅ ON | каждую минуту |
| Тишина > 4ч | 🔴 OFF | — |
| Idea follow-up | 🔴 OFF | — |
| Activity suggestion | 🔴 OFF | — |

Проактивное состояние сохраняется в `proactive_state.json` — переживает рестарты Railway.

---

## Feature Flags

Управляются через Railway Variables (1=ON, 0=OFF):

```env
FEATURE_IMAGE_AGENT=1          # генерация изображений
FEATURE_IDEAS_AGENT=1          # брейнсторм
FEATURE_MORNING_DIGEST=1       # утренний дайджест
FEATURE_TASK_DEADLINES=1       # напоминания о дедлайнах
FEATURE_REMINDER_WARNING=1     # напоминания
FEATURE_PROACTIVE_SILENCE=0    # писать первой при тишине
FEATURE_PROACTIVE_IDEA=0       # follow-up идей из дневника
FEATURE_PROACTIVE_ACTIVITY=0   # предлагать продолжить тему
FEATURE_PERSONA_VERBOSE=1      # personality mirroring
FEATURE_EMOTIONAL_SYSTEM=1     # mood tracking
```

---

## Надёжность

- **SQLite WAL** + retry при SQLITE_BUSY (5 попыток, exponential backoff)
- **File lock** при записи в vault (threading.Lock per-файл)
- **Атомарная запись** vault файлов (tmp → rename)
- **WebDAV auth** — пустой WEBDAV_PASSWORD = 401 (безопасный дефолт)
- **timing-safe** сравнение паролей (hmac.compare_digest)
- **Proactive state** персистентен между рестартами

---

## API Endpoints (41)

```
GET/DELETE  /api/history
POST        /api/chat
GET/DELETE  /api/memory
GET         /api/context
GET         /api/diary
GET/POST    /api/reminders
DELETE      /api/reminders/{id}
POST        /api/voice
GET         /api/status
GET         /api/features
GET         /api/search
POST        /api/search/index
GET         /api/tasks
POST        /api/tasks/done
POST        /api/tasks/undo
POST        /api/tasks/move
POST        /api/tasks/move_and_undo
POST        /api/tasks/clear_done
GET         /api/tasks/week
GET         /api/calendar/month
GET         /api/calendar/day
GET/POST    /api/calendar/events
PUT/DELETE  /api/calendar/events/{id}
GET         /api/calendar/upcoming
POST        /api/calendar/day_notes
GET         /api/vault/note
DELETE      /api/vault/cleanup
DELETE      /api/vault/file
```

---

## База данных (SQLite)

| Таблица | Содержимое |
|---|---|
| `history` | История переписки |
| `user_memory` | Долгосрочные факты |
| `structured_memory` | Структурированная память по категориям |
| `reminders` | Напоминания |
| `diary` | Записи дневника (через БД) |
| `tasks` | Задачи (зеркало Obsidian, для напоминаний) |
| `mood_log` | Трекинг настроения |
| `interaction_memory` | Топ-темы, паттерны общения |
| `conversation_context` | Текущая тема, цель разговора |
| `events` | События календаря |

---

## Railway Variables

```env
# Обязательные
TELEGRAM_TOKEN=
GROQ_API_KEY=
ALLOWED_USER_IDS=
TELEGRAM_USER_ID=

# Vault
OBSIDIAN_VAULT_PATH=/data/obsidian_vault
OBSIDIAN_VAULT_SUBDIR=RaYa-Vault
WEBDAV_USER=raya
WEBDAV_PASSWORD=

# Опциональные
TAVILY_API_KEY=
HF_TOKEN=
WEB_TOKEN=sokrat
MODEL_NAME=llama-3.3-70b-versatile
ROUTER_MODEL=llama-3.1-8b-instant
TEMPERATURE=0.7
MAX_HISTORY=20
DB_PATH=/data/database.db
```

---

## Структура проекта

```
RaYa-Assist/
├── main.py
├── persona.txt                    ← личность RaYa
├── audit.py                       ← проверка перед деплоем
├── README.md                      ← этот файл
├── requirements.txt
├── Procfile
├── runtime.txt
├── nixpacks.toml
└── app/
    ├── config.py
    ├── core.py                    ← инициализация, startup
    ├── database.py                ← SQLite, все таблицы
    ├── handlers.py                ← Telegram обработчики
    ├── llm_service.py             ← точка входа LLM
    ├── llm_pipeline.py            ← tone control, memory extraction
    ├── search_service.py          ← Tavily
    ├── semantic_search.py         ← Groq embeddings, vector index
    ├── calendar_service.py        ← события, daily notes
    ├── vault_tool.py              ← 26 vault операций (tool use)
    ├── feature_flags.py           ← включение/отключение функций
    ├── personality_service.py     ← mood, state, personality
    ├── proactive_service.py       ← фоновые триггеры
    ├── voice_service.py
    ├── vision_service.py
    ├── document_service.py
    ├── web_server.py              ← FastAPI + WebDAV
    ├── webdav_server.py
    ├── middleware.py
    ├── utils.py
    ├── agents/
    │   ├── registry.py
    │   ├── router.py
    │   ├── orchestrator.py
    │   ├── base_agent.py
    │   ├── raya_agent.py
    │   ├── code_agent.py
    │   ├── image_agent.py
    │   ├── research_agent.py
    │   ├── todo_agent.py
    │   ├── obsidian_agent.py
    │   ├── text_agent.py
    │   ├── ideas_agent.py
    │   ├── explain_agent.py
    │   ├── morning_agent.py
    │   └── critic_agent.py
    └── integrations/
        ├── base.py
        └── obsidian.py            ← все vault операции
```

---

## Деплой

```bash
# Проверка перед push
python3 audit.py

# Деплой
git add -A
git commit -m "feat: ..."
git push
```

После первого деплоя с новым vault — пересобрать semantic index:
```
POST https://raya-assist-production.up.railway.app/api/search/index?token=sokrat
```

---

## Changelog

| Дата | Изменение |
|---|---|
| 2026-03 | Общедоступный режим — убрана проверка Telegram user_id и пароли (WEBDAV_PASSWORD пустой = открытый доступ) |
| 2026-03 | Новости в дайджесте — русский язык, развернутая информация (800 символов на тему) |
| 2026-03 | Семантический поиск (Groq embeddings) |
| 2026-03 | Календарь — месячный вид, дневное расписание, Obsidian daily notes |
| 2026-03 | Матрица Эйзенхауэра — drag&drop, undo, дедлайны в UI |
| 2026-03 | Vault tool use — RaYa сама управляет vault через Anthropic tool use |
| 2026-03 | WebDAV auth hardened (пустой пароль → 401, hmac.compare_digest) |
| 2026-03 | SQLite: retry при SQLITE_BUSY, file lock в obsidian.py |
| 2026-03 | Morning digest: новости через Tavily (4 темы параллельно), 30 цитат, 26 вопросов |
| 2026-03 | Proactive state persistence (переживает рестарты) |
| 2026-03 | Persona переработана — RaYa как менеджер который не даёт слиться |
| 2026-03 | Feature flags — 10 флагов через Railway Variables |
