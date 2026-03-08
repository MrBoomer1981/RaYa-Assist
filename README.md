# RaYa — Personal AI Assistant Bot

Персональный Telegram-бот на базе мультиагентной архитектуры.  
RaYa — главный оркестратор, который распределяет задачи между специализированными агентами.

---

## Стек технологий

| Компонент | Технология |
|---|---|
| LLM (основная) | Groq — `llama-3.3-70b-versatile` |
| LLM (роутер) | Groq — `llama-3.1-8b-instant` |
| Vision | Groq — `meta-llama/llama-4-scout-17b-16e-instruct` |
| Speech-to-Text | Groq Whisper — `whisper-large-v3-turbo` |
| Image Generation | Hugging Face — `FLUX.1-schnell` |
| Telegram | aiogram 3.17.0 |
| База данных | SQLite (WAL-режим) |
| Поиск | Tavily API |
| Хостинг | Railway.app |
| Python | 3.11 |

---

## Возможности

### Основное общение
- Диалог на любом языке
- История разговора сохраняется между сессиями (SQLite)
- Долгосрочная память: бот извлекает факты о пользователе и использует их в будущих разговорах
- Поиск актуальной информации в интернете через Tavily (опционально)

### Голосовые сообщения 🎤
- Распознавание голоса через Groq Whisper
- Транскрибирует и отвечает на голосовые сообщения
- Поддержка файлов до 20 МБ

### Анализ изображений 🖼️
- Анализ фотографий через Llama 4 Vision
- Поддерживает JPEG, PNG, WebP
- Принимает подпись как вопрос к изображению
- Fallback между моделями при недоступности

### Анализ документов 📄
- Поддерживаемые форматы: PDF, DOCX, DOC, TXT
- Извлечение текста из таблиц Word
- Лимит: 24 000 символов (обрезание с предупреждением)
- Файлы до 20 МБ

### Напоминания ⏰
- Установка напоминаний на естественном языке ("через 2 часа", "завтра в 10")
- Время вычисляется относительно UTC автоматически
- Планировщик проверяет и отправляет напоминания каждые 60 секунд
- Управление: `/reminders`, отмена по номеру

### Генерация изображений 🎨 *(требует HF_TOKEN)*
- Генерация через FLUX.1-schnell (Hugging Face)
- Автоматическое улучшение промпта через LLM
- Перевод на английский для лучшего результата

---

## Система агентов

RaYa использует мультиагентную архитектуру.  
Каждое сообщение проходит через роутер — он определяет нужного агента.

```
Сообщение пользователя
        │
   RouterAgent          ← быстрый матч по ключевым словам
        │                 если неоднозначно → llama-3.1-8b-instant
        ▼
   Orchestrator
        │
   ┌────┴─────────────────────┐
   ▼    ▼    ▼    ▼    ▼      ▼
 RaYa Code Image Diary Science Critic
        │                      ▲
        └──── needs_critic ─────┘
```

### Агенты

| Агент | Триггеры | Особенности |
|---|---|---|
| **RaYa** | Всё остальное (fallback) | Напоминания, общий диалог |
| **Code Agent** | код, python, баг, функция, алгоритм | Автоматически проверяется Критиком |
| **Image Agent** | нарисуй, сгенерируй картинку | FLUX через HF API |
| **Diary Agent** | дневник, запиши, сегодня я, чувствую | Приватное хранилище в SQLite |
| **Science Agent** | факт, исследование, научно, проверь | Поиск + проверка Критиком |
| **Critic Agent** | *(только программно)* | Температура 0, финальная проверка |

### Двухуровневая маршрутизация

1. **Быстрый матч** — ключевые слова без LLM, мгновенно
2. **LLM роутер** — `llama-3.1-8b-instant` при неоднозначности, температура 0

---

## Безопасность

- `AccessMiddleware` — белый список пользователей через `ALLOWED_USER_IDS`
- Чужие сообщения игнорируются без ответа
- Если список пуст — бот открыт (режим разработки)

---

## Команды

| Команда | Описание |
|---|---|
| `/start` | Приветствие |
| `/help` | Список возможностей |
| `/memory` | Показать что бот знает о пользователе |
| `/forget` | Удалить долгосрочную память |
| `/clear` | Очистить историю разговора |
| `/reminders` | Показать активные напоминания |
| `/debug_time` | Диагностика UTC времени и напоминаний в БД |

---

## Переменные окружения

### Обязательные

```env
TELEGRAM_TOKEN=        # токен бота от @BotFather
GROQ_API_KEY=          # ключ Groq API
```

### Опциональные

```env
TAVILY_API_KEY=        # поиск в интернете
ALLOWED_USER_IDS=      # список Telegram ID через запятую (безопасность)
HF_TOKEN=              # генерация изображений через Hugging Face
MODEL_NAME=llama-3.3-70b-versatile
TEMPERATURE=0.7
MAX_HISTORY=20
SYSTEM_PROMPT=         # системный промпт (перегружает persona.txt)
```

---

## Файл личности

Редактируется без изменения кода:

```
persona.txt            # личность, тон, правила общения
```

---

## Структура проекта

```
RaYa-Assist/
├── main.py                        # точка входа, Telegram обработчики
├── persona.txt                    # личность бота
├── requirements.txt
├── Procfile                       # Railway: worker: python main.py
├── runtime.txt                    # python-3.11.x
├── nixpacks.toml                  # Railway build config
└── app/
    ├── config.py                  # настройки, загрузка persona.txt
    ├── database.py                # SQLite: история, память, напоминания, дневник
    ├── llm_service.py             # LLM сервис, делегирует оркестратору
    ├── scheduler_service.py       # планировщик напоминаний
    ├── search_service.py          # Tavily поиск
    ├── voice_service.py           # Groq Whisper
    ├── vision_service.py          # Llama 4 Vision
    ├── document_service.py        # PDF/DOCX/TXT парсинг
    └── agents/
        ├── registry.py            # реестр агентов с метаданными
        ├── router.py              # двухуровневый роутер
        ├── base_agent.py          # базовый класс: таймаут, логирование
        ├── orchestrator.py        # координатор агентов
        ├── raya_agent.py          # главный агент (fallback)
        ├── code_agent.py          # код и отладка
        ├── image_agent.py         # генерация изображений
        ├── diary_agent.py         # личный дневник
        ├── science_agent.py       # верификация фактов
        └── critic_agent.py        # финальная проверка результатов
```

---

## База данных

SQLite, WAL-режим. Таблицы:

| Таблица | Содержимое |
|---|---|
| `history` | История переписки (роль, текст, время) |
| `user_memory` | Долгосрочные факты о пользователе |
| `reminders` | Напоминания (текст, время, статус) |
| `diary` | Записи личного дневника |

---

## Деплой на Railway

```bash
# Первый деплой
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR/REPO.git
git push -u origin main
# Подключить репозиторий в Railway → Variables → добавить ключи

# Обновление
git add .
git commit -m "Update"
git push
```

### Railway Variables

```
TELEGRAM_TOKEN
GROQ_API_KEY
TAVILY_API_KEY        (опционально)
ALLOWED_USER_IDS      (опционально)
HF_TOKEN              (опционально)
```

---

## Локальный запуск

```bash
cd RaYa-Assist
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить ключи
python main.py
```

---

## Добавление нового агента

1. Создать `app/agents/my_agent.py` — наследовать `BaseAgent`, реализовать `_execute()`
2. Добавить запись в `AGENTS` в `app/agents/registry.py` с ключевыми словами
3. Добавить в фабрику `_create_agent()` в `app/agents/orchestrator.py`
4. Готово — роутер начнёт направлять подходящие сообщения к новому агенту

```python
# Минимальный агент
class MyAgent(BaseAgent):
    agent_name = "my_agent"

    def _system_prompt(self) -> str:
        return "Ты специалист по..."

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        return AgentResult(
            success=True,
            content=str(response.content),
            agent_name=self.agent_name,
        )
```
