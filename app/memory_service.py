"""
memory_service.py — сервис структурированной персональной памяти RaYa.

Что делает:
- Анализирует каждое сообщение пользователя через LLM
- Извлекает факты, интересы, проекты, цели, предпочтения, контекст
- Сохраняет в structured_memory с категориями
- Мигрирует старые данные из user_memory при первом запуске
- Формирует богатый контекст для агентов

Категории памяти:
  facts       — имя, город, возраст, профессия
  interests   — увлечения, любимые темы
  projects    — проекты над которыми работает
  skills      — навыки и компетенции
  preferences — стиль общения, привычки
  goals       — цели и планы
  context     — что происходит прямо сейчас
"""
import json
import logging

logger = logging.getLogger(__name__)

# Извлекаем память каждые N сообщений
_EXTRACT_EVERY_N = 3

_EXTRACTION_PROMPT = """\
Проанализируй сообщение пользователя и извлеки персональную информацию о нём.

Сообщение: {message}

Верни ТОЛЬКО JSON объект со следующими полями (только те что нашёл, пустые — пропусти):
{{
  "facts":       {{"ключ": "значение"}},
  "interests":   {{"ключ": "значение"}},
  "projects":    {{"ключ": "значение"}},
  "skills":      {{"ключ": "значение"}},
  "preferences": {{"ключ": "значение"}},
  "goals":       {{"ключ": "значение"}},
  "context":     {{"ключ": "значение"}}
}}

Примеры:
- "Я живу в Самаре" → {{"facts": {{"город": "Самара"}}}}
- "Работаю над ботом на Python" → {{"projects": {{"raya_bot": "Telegram бот на Python"}}, "skills": {{"python": "Python разработка"}}}}
- "Хочу выучить Rust" → {{"goals": {{"изучить_rust": "Выучить язык Rust"}}}}
- "Предпочитаю короткие ответы" → {{"preferences": {{"стиль_ответов": "короткие и по делу"}}}}
- "Сейчас разбираюсь с Railway деплоем" → {{"context": {{"текущая_задача": "деплой на Railway"}}}}

Правила:
- Ключи — короткие, на русском, без пробелов (используй _)
- Значения — краткие, информативные
- Только реальные факты из сообщения, не придумывай
- Если ничего не нашёл — верни {{}}
- Только JSON, без пояснений"""


class MemoryService:
    """Сервис управления структурированной памятью."""

    def __init__(self, llm) -> None:
        self._llm   = llm
        self._counter: dict[int, int] = {}

    def should_extract(self, user_id: int) -> bool:
        """Возвращает True каждые N сообщений."""
        count = self._counter.get(user_id, 0) + 1
        self._counter[user_id] = count
        return count % _EXTRACT_EVERY_N == 1

    async def extract_and_save(self, user_id: int, message: str) -> None:
        """
        Анализирует сообщение и сохраняет найденные факты.
        Вызывается фоново — не блокирует ответ.
        """
        if len(message.strip()) < 10:
            return

        try:
            from langchain_core.messages import HumanMessage
            from app.database import upsert_memory

            prompt   = _EXTRACTION_PROMPT.format(message=message[:500])
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw      = (
                str(response.content)
                .strip()
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )

            if not raw or raw == "{}":
                return

            data: dict = json.loads(raw)
            if not isinstance(data, dict):
                return

            saved = 0
            for category, entries in data.items():
                if not isinstance(entries, dict):
                    continue
                for key, value in entries.items():
                    if key and value:
                        upsert_memory(user_id, category, str(key), str(value))
                        saved += 1

            if saved:
                logger.info("🧠 Память: сохранено %d фактов | user_id=%s", saved, user_id)

        except (json.JSONDecodeError, ValueError):
            logger.debug("memory: ничего не извлечено из сообщения")
        except Exception:
            logger.exception("memory: ошибка извлечения")

    async def migrate_old_memory(self, user_id: int) -> None:
        """
        Мигрирует старые факты из user_memory в structured_memory.
        Запускается один раз при первом обращении.
        """
        try:
            from app.database import load_memory, upsert_memory, get_structured_memory

            # Если структурированная память уже есть — не мигрируем
            existing = get_structured_memory(user_id)
            if existing:
                return

            old_facts = load_memory(user_id)
            if not old_facts:
                return

            logger.info("🔄 Миграция %d фактов из user_memory | user_id=%s",
                        len(old_facts), user_id)

            for i, fact in enumerate(old_facts):
                upsert_memory(user_id, "facts", f"факт_{i+1}", fact)

            logger.info("✅ Миграция завершена | user_id=%s", user_id)

        except Exception:
            logger.exception("memory: ошибка миграции")

    @staticmethod
    def build_context(user_id: int) -> str:
        """
        Формирует текстовый контекст из структурированной памяти.
        Используется в системном промпте агентов.
        """
        from app.database import format_memory_for_prompt
        return format_memory_for_prompt(user_id)

    @staticmethod
    def get_all(user_id: int) -> dict:
        """Возвращает всю структурированную память."""
        from app.database import get_structured_memory
        return get_structured_memory(user_id)


# ── Interaction Memory extractor ──────────────────────────────────────────────

_INTERACTION_PROMPT = """\
Проанализируй сообщение и определи тему разговора.

Сообщение: {message}

Верни ТОЛЬКО JSON (без markdown):
{{
  "topic": "короткое название темы (3-5 слов, на русском)",
  "summary": "одно предложение — о чём именно говорили или спрашивали"
}}

Примеры:
- "Как настроить Railway Volume?" → {{"topic": "Railway деплой", "summary": "Настройка персистентного хранилища на Railway"}}
- "Помоги написать async функцию" → {{"topic": "Python async код", "summary": "Написание асинхронных функций на Python"}}
- Если тема слишком общая или мелкая → верни {{}}"""


async def extract_interaction(user_id: int, message: str, llm) -> None:
    """Фоново извлекает тему и сохраняет в interaction_memory."""
    if len(message.strip()) < 15:
        return
    try:
        import json as _json
        from langchain_core.messages import HumanMessage
        from app.database import upsert_interaction

        prompt   = _INTERACTION_PROMPT.format(message=message[:400])
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = (
            str(response.content).strip()
            .replace("```json", "").replace("```", "").strip()
        )
        if not raw or raw == "{}":
            return

        data = _json.loads(raw)
        topic   = str(data.get("topic", "")).strip()
        summary = str(data.get("summary", "")).strip()

        if topic and summary:
            upsert_interaction(user_id, topic, summary)
            import logging
            logging.getLogger(__name__).info(
                "🔁 Interaction: '%s' | user_id=%s", topic, user_id
            )
    except Exception:
        pass  # фоновая задача — падение не критично
