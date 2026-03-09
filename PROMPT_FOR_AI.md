# Контекст проекта RaYa — инструкция для AI-ассистента

## О проекте
Персональный Telegram-бот + веб-интерфейс для одного пользователя.
Пользователь: Сократ (Sokrat). Бот обращается к нему ТОЛЬКО "Сократ".
Репозиторий: https://github.com/MrBoomer1981/RaYa-Assist (приватный)
Хостинг: Railway.app, проект называется RaYa-Assist
Веб-интерфейс: https://raya-assist.up.railway.app/?token=sokrat

## Стек
| Компонент       | Технология |
|---|---|
| Python          | 3.11 |
| Telegram        | aiogram 3.17.0 |
| LLM основная    | Groq — llama-3.3-70b-versatile |
| LLM роутер      | Groq — llama-3.1-8b-instant (температура 0) |
| Vision          | Groq — meta-llama/llama-4-scout-17b-16e-instruct |
| STT             | Groq Whisper — whisper-large-v3-turbo |
| Image gen       | Hugging Face — FLUX.1-schnell |
| БД              | SQLite WAL-режим |
| Поиск           | Tavily API |
| Веб-фреймворк   | FastAPI + uvicorn |
| LLM клиент      | langchain-groq + langchain-core |
| Настройки       | pydantic-settings + python-dotenv |

## Структура проекта
```
RaYa-Assist/
├── main.py                   # точка входа: Telegram бот + веб-сервер параллельно
├── persona.txt               # личность бота (редактируется без кода)
├── requirements.txt
├── Procfile                  # worker: python main.py
├── runtime.txt               # python-3.11.x
├── nixpacks.toml
├── static/
│   └── index.html            # веб-интерфейс (одна страница, все разделы)
└── app/
    ├── config.py             # Settings (pydantic), загрузка persona.txt
    ├── database.py           # SQLite: история, память, напоминания, дневник
    ├── utils.py              # общие утилиты: parse_reminder, clean_reminder_tag, build_reminder_prompt_block
    ├── llm_service.py        # LLMService: chat(), chat_with_document(), save_photo_exchange()
    ├── web_server.py         # FastAPI: create_app(llm_service) → все /api/* endpoints
    ├── scheduler_service.py  # планировщик напоминаний (каждые 60с)
    ├── search_service.py     # Tavily поиск
    ├── voice_service.py      # Groq Whisper transcribe()
    ├── vision_service.py     # Llama 4 Vision analyze()
    ├── document_service.py   # PDF/DOCX/TXT парсинг
    └── agents/
        ├── registry.py       # AgentInfo dataclass, AGENTS dict, quick_match()
        ├── router.py         # RouterAgent: двухуровневый роутинг (keywords → LLM)
        ├── base_agent.py     # BaseAgent, AgentContext, AgentResult
        ├── orchestrator.py   # Orchestrator: роутинг→агент→критик
        ├── raya_agent.py     # главный агент, fallback, напоминания
        ├── code_agent.py     # код и отладка → needs_critic=True
        ├── image_agent.py    # FLUX через HF API
        ├── diary_agent.py    # личный дневник в SQLite
        ├── science_agent.py  # верификация фактов + поиск → needs_critic=True
        └── critic_agent.py   # финальная проверка, температура 0
```

## Ключевые контракты

### ChatResult (llm_service.py)
```python
@dataclass
class ChatResult:
    reply: str
    reminder: Optional[dict] = None  # {"text": str, "remind_at": "YYYY-MM-DD HH:MM:SS"}
    agent_name: str = "raya"
    metadata: dict = field(default_factory=dict)
```

### AgentContext / AgentResult (base_agent.py)
```python
@dataclass
class AgentContext:
    user_id: int
    message: str
    history: list[BaseMessage]
    memory_facts: list[str]
    search_results: str = ""
    extra: dict = field(default_factory=dict)

@dataclass
class AgentResult:
    success: bool
    content: str
    agent_name: str
    elapsed_ms: int = 0
    needs_critic: bool = False
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None
```

### BaseAgent (base_agent.py)
```python
class BaseAgent:
    agent_name: str = "base"
    timeout: int = 30

    def _system_prompt(self) -> str: ...        # переопределить
    async def _execute(self, ctx) -> AgentResult: ...  # переопределить
    async def run(self, ctx) -> AgentResult: ... # публичный, не трогать
    def _build_messages(self, ctx, user_content=None) -> list: ...
```

## Поток данных
```
Telegram сообщение
  → main.py (AccessMiddleware)
  → llm_service.chat(user_id, message)
      → поиск параллельно (если нужен)
      → orchestrator.run(user_id, message, search_results)
          → router.route(message)           # keywords → llama-3.1-8b
          → agent.run(AgentContext)         # нужный агент
          → critic.review() если needs_critic
      → save_messages() в историю
      → _extract_facts_background() каждые 5 сообщений
  → ChatResult(reply, reminder, agent_name, metadata)
  → main._handle_chat_result() → отправка в Telegram

Веб-запрос
  → FastAPI web_server.py (проверка WEB_TOKEN)
  → /api/chat → llm_service.chat() → тот же поток
  → /api/memory|diary|reminders|status → напрямую из database.py
```

## Таблицы БД (database.py)
```sql
history     (id, user_id, role, content, created_at)
user_memory (id, user_id, fact, created_at)
reminders   (id, user_id, text, remind_at, done, created_at)
diary       (id, user_id, entry, mood, created_at)
```
database.py использует контекстный менеджер `_conn()` — автоматический commit/rollback.

## Переменные окружения (Railway Variables)
```
TELEGRAM_TOKEN      # обязательно
GROQ_API_KEY        # обязательно
TAVILY_API_KEY      # опционально — поиск
ALLOWED_USER_IDS    # опционально — список Telegram ID через запятую
HF_TOKEN            # опционально — генерация изображений FLUX
WEB_TOKEN=sokrat    # защита веб-интерфейса токеном в URL
```

## Правила — НЕЛЬЗЯ НАРУШАТЬ
1. `datetime.utcnow()` везде — никакого timezone.utc
2. Формат времени везде `"%Y-%m-%d %H:%M:%S"`
3. `save_photo_exchange()` — синхронный метод, вызывать БЕЗ await
4. Напоминания через тег: `<reminder>{"text":"...","remind_at":"..."}</reminder>`
5. Новый агент = файл + запись в registry.py + ветка в orchestrator._create_agent()
6. `_conn()` в database.py — всегда через контекстный менеджер, никогда напрямую sqlite3.connect
7. utils.py — общие утилиты, не дублировать parse_reminder и clean_reminder_tag

## Веб-интерфейс (web_server.py + static/index.html)
FastAPI приложение создаётся через `create_app(llm_service)`.
Тот же экземпляр LLMService что использует Telegram бот.
Запускается параллельно с ботом через `asyncio.gather()` в main.py.

API endpoints:
- POST /api/chat          → llm_service.chat()
- GET  /api/history       → load_history()
- DELETE /api/history     → clear_history()
- GET  /api/memory        → load_memory()
- DELETE /api/memory      → clear_memory()
- GET  /api/reminders     → get_active_reminders()
- POST /api/reminders     → save_reminder()
- DELETE /api/reminders/{id} → delete_reminder()
- GET  /api/diary         → load_diary_entries()
- GET  /api/status        → модель, агенты, UTC время

Все endpoints защищены: `?token=WEB_TOKEN`
index.html — один файл, тёмный элегантный дизайн, шрифты Syne + DM Mono + DM Sans.
Разделы: Чат, Память, Дневник, Напоминания, Статус.

## Как добавить нового агента
```python
# 1. app/agents/my_agent.py
from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

class MyAgent(BaseAgent):
    agent_name = "my_agent"
    timeout = 30

    def _system_prompt(self) -> str:
        return "Ты специалист по X. Обращайся только 'Сократ'."

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        return AgentResult(
            success=True,
            content=str(response.content),
            agent_name=self.agent_name,
        )

# 2. В registry.py — добавить AgentInfo с keywords
# 3. В orchestrator._create_agent() — добавить ветку case "my_agent":
```

## Деплой
```bash
git add .
git commit -m "описание изменений"
git push
# Railway деплоит автоматически
```

## Текущий статус
Всё задеплоено и работает:
- ✅ Telegram бот
- ✅ Голос, фото, документы
- ✅ Напоминания (scheduler)
- ✅ Мультиагентная система (6 агентов)
- ✅ Генерация изображений (FLUX)
- ✅ Веб-интерфейс (FastAPI + index.html)

---

## ТЕКУЩАЯ ЗАДАЧА
[ОПИШИ ЗДЕСЬ ЧТО НУЖНО СДЕЛАТЬ]
