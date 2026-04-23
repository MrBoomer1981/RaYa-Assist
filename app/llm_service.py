"""
llm_service.py — сервис обработки сообщений.
Делегирует оркестратору, сохраняет историю, извлекает факты в фоне.

Поиск:
  - LLM-классификатор (router_model, T=0) решает нужен ли поиск
    и формулирует оптимальный поисковый запрос
  - Классификатор запускается параллельно с подготовкой контекста
  - Результат кэшируется в SearchService (TTL 10 мин)
"""
import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.config import settings
from app.database import load_memory, save_messages

logger = logging.getLogger(__name__)

# Семафор — ограничение параллельных LLM запросов.
# Настраивается через env LLM_CONCURRENCY (default 10).
# Groq free tier: ~30 RPM, 10 concurrent оптимально для 25+ users.
_LLM_CONCURRENCY = int(__import__("os").getenv("LLM_CONCURRENCY", "10"))
_LLM_SEMAPHORE = asyncio.Semaphore(_LLM_CONCURRENCY)

# Быстрый keyword pre-filter — отсекает явно неподходящие запросы
# до вызова LLM-классификатора. Снижает расход токенов на ~70%.
_SEARCH_NEVER: frozenset[str] = frozenset({
    "/start", "/help", "/clear", "/memory", "/forget", "/reminders",
})

_SEARCH_CLASSIFIER_PROMPT = """\
Ты решаешь нужен ли поиск в интернете для ответа на сообщение пользователя.

{context_block}ПОИСК НУЖЕН если:
- вопрос про текущие события, новости, запуски, миссии, релизы, анонсы
- спрашивают про дату, статус, результат конкретного события
- вопрос про актуальные данные: цены, курсы, погода, расписания
- любое "когда", "когда запуск", "что произошло", "последние новости о X"
- упоминаются конкретные программы, проекты, миссии (Артемида, SpaceX, ChatGPT, etc.)
- вопрос про человека, компанию, продукт в настоящем времени
- просят найти, проверить, исследовать

ПОИСК НЕ НУЖЕН если:
- общий разговор, мнение, «как дела», эмоции
- задача по программированию, математике, алгоритмам
- просьба написать текст, перевести, объяснить абстрактную концепцию
- личные задачи, напоминания, планирование

ПРАВИЛО: при сомнении — ИСКАТЬ. Лучше лишний поиск чем устаревший ответ.

Сообщение: {message}

Верни ТОЛЬКО JSON без markdown:
{{"needs_search": true/false, "query": "оптимальный поисковый запрос на русском или английском"}}

Примеры:
- "когда полетит Артемида 2" → {{"needs_search": true, "query": "Artemis 2 launch date 2025 2026"}}
- "что такое Артемида" → {{"needs_search": true, "query": "NASA Artemis program status 2026"}}
- "курс доллара сейчас" → {{"needs_search": true, "query": "курс доллара рубль сегодня"}}
- "напиши функцию на python" → {{"needs_search": false, "query": ""}}
- "новости SpaceX" → {{"needs_search": true, "query": "SpaceX news 2026"}}
- "что такое блокчейн" → {{"needs_search": false, "query": ""}}
- "последние новости ChatGPT" → {{"needs_search": true, "query": "ChatGPT OpenAI новости 2026"}}
- "как дела" → {{"needs_search": false, "query": ""}}
- "А температура?" (после вопроса про погоду в Самаре) → {{"needs_search": true, "query": "погода Самара завтра температура"}}
- "а как насчёт акций Apple?" (после разговора про инвестиции) → {{"needs_search": true, "query": "акции Apple сейчас"}}\
"""

_MEMORY_EXTRACTION_PROMPT = """\
Проанализируй сообщение пользователя и извлеки важные факты о нём.
Интересуют: имя, возраст, профессия, город, интересы, цели, важные детали жизни.

Сообщение: {message}

Верни ТОЛЬКО JSON-массив строк. Если фактов нет — верни [].
Пример: ["Зовут Алексей", "Работает программистом", "Живёт в Москве"]
Только JSON, без пояснений и markdown."""


@dataclass
class ChatResult:
    """Результат одного обращения к модели."""
    reply: str
    reminder: Optional[dict]  = field(default=None)
    agent_name: str           = "raya"
    metadata: dict            = field(default_factory=dict)


import re as _re

# ── Follow-up детектор и обогатитель запроса ─────────────────────────────────

# Паттерны коротких уточнений — "А температура?", "и как насчёт...", "что с X?"
_FOLLOWUP_RE = _re.compile(
    r"^(а|и|но|ещё|кстати|а что|а как|что с|а если|и что|а там|а у|скажи|"
    r"дай|покажи|уточни|расскажи подробнее|подробнее|а про|и про|"
    r"а насчёт|как насчёт)\b",
    _re.IGNORECASE | _re.UNICODE,
)

# Темы из которых извлекаем контекст для обогащения запроса
_SEARCH_TOPIC_RE = _re.compile(
    r"\b(погода|температура|курс|цена|стоимость|новости|событи|запуск|"
    r"матч|игра|счёт|акци|биткоин|доллар|евро|прогноз|расписани|"
    r"выход|релиз|анонс|обновлени)\b",
    _re.IGNORECASE | _re.UNICODE,
)


def _is_followup(message: str) -> bool:
    """Определяет является ли сообщение уточняющим follow-up."""
    return bool(_FOLLOWUP_RE.match(message.strip()))


def _enrich_followup_query(message: str, history: list | None) -> str:
    """
    Строит поисковый запрос для follow-up сообщения используя контекст истории.
    Извлекает тему из последнего ответа Раи и добавляет к текущему вопросу.
    Возвращает пустую строку если тема не найдена.
    """
    if not history:
        return ""

    # Ищем тему в последних сообщениях (предпочитаем ответ Раи)
    for msg in reversed(history):
        text = str(msg.content)[:300]
        match = _SEARCH_TOPIC_RE.search(text)
        if match:
            # Берём фрагмент вокруг найденной темы (до 60 символов)
            start = max(0, match.start() - 20)
            snippet = text[start:start + 60].strip()
            # Очищаем и склеиваем с текущим вопросом
            topic = _re.sub(r"[^\w\s,]", " ", snippet).strip()
            combined = f"{topic} {message}".strip()
            return combined[:120]

    return ""



class LLMService:
    """
    Сервис для обработки сообщений.
    Делегирует оркестратору — не содержит бизнес-логики агентов.
    """

    def __init__(self) -> None:
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.model_name,
            temperature=settings.temperature,
        )
        # Лёгкая модель для классификатора поиска (быстро и дёшево)
        self._router_llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.router_model,
            temperature=0.0,
        )
        self._search: Optional[Any] = None
        if settings.search_enabled:
            from app.search_service import SearchService
            self._search = SearchService()
            logger.info("🔍 Поиск в интернете включён")

        self._msg_counter: dict[int, int] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._orchestrator: Optional[Any] = None

        from app.llm_pipeline import (
            MemoryService, ContextService, ConsistencyService,
              RouterCalibration,
        )
        self._memory      = MemoryService(self._llm)
        self._context     = ContextService(self._llm)
          # ToneController убран — логика в системном промпте
        self._consistency = ConsistencyService(self._llm)
        self._calibration = RouterCalibration()

    # ── Вспомогательные ───────────────────────────────────────────────────────

    def _run_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Запускает корутину в фоне, защищая задачу от GC."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _decide_search(
        self, message: str, recent_history: list | None = None
    ) -> tuple[bool, str]:
        """
        LLM-классификатор: нужен ли поиск и какой запрос отправить.
        Использует лёгкую router_model (T=0) — ~0.2–0.3с.
        Запускается параллельно с подготовкой контекста.

        recent_history — последние 2 пары сообщений для контекста follow-up вопросов.
        Возвращает (needs_search, optimized_query).
        При любой ошибке — безопасный fallback (False, "").
        """
        if not self._search:
            return False, ""

        # Быстрый pre-filter — команды точно не требуют поиска
        if message.strip() in _SEARCH_NEVER or message.startswith("/"):
            return False, ""

        # Строим блок контекста — критично для follow-up вопросов ("А температура?")
        context_block = ""
        if recent_history:
            # Берём последние 4 сообщения (2 пары) — этого достаточно для контекста
            tail = recent_history[-4:]
            lines = []
            for msg in tail:
                role = "Пользователь" if msg.__class__.__name__ == "HumanMessage" else "Рая"
                lines.append(f"{role}: {str(msg.content)[:120]}")
            if lines:
                context_block = "Контекст последних сообщений:\n" + "\n".join(lines) + "\n\n"

        # Быстрый детектор follow-up без LLM: "А температура?" + context → enriched query
        # Работает для коротких уточнений (≤30 символов) — не тратим токены на классификацию
        msg_stripped = message.strip()
        if len(msg_stripped) <= 30 and context_block and _is_followup(msg_stripped):
            enriched_query = _enrich_followup_query(msg_stripped, recent_history)
            if enriched_query:
                logger.debug("search: follow-up enrich '%s' → '%s'", msg_stripped, enriched_query)
                return True, enriched_query

        try:
            prompt = _SEARCH_CLASSIFIER_PROMPT.format(
                context_block=context_block,
                message=message[:400],
            )
            response = await self._router_llm.ainvoke([HumanMessage(content=prompt)])
            raw = str(response.content).strip()
            # Убираем markdown если модель добавила
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            needs = bool(data.get("needs_search", False))
            query = str(data.get("query", "")).strip() or message
            logger.debug("search_classifier: needs=%s query='%s'", needs, query[:60])
            return needs, query
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.debug("search_classifier fallback (err: %s)", e)
            # Безопасный fallback — не ломаем ответ из-за ошибки классификатора
            return False, ""

    def _get_orchestrator(self):
        """Ленивая инициализация оркестратора."""
        if self._orchestrator is None:
            from app.agents.orchestrator import Orchestrator
            self._orchestrator = Orchestrator()
        return self._orchestrator

    def invalidate_user_cache(self, user_id: int) -> None:
        """
        Сбрасывает все in-memory кэши для пользователя.
        Вызывать при смене имени, настроек, или по явному запросу.
        """
        from app.database import invalidate_user_name_cache
        invalidate_user_name_cache(user_id)
        orch = self._orchestrator
        if orch and "raya" in orch._agents:
            raya = orch._agents["raya"]
            if hasattr(raya, "invalidate_prompt_cache"):
                raya.invalidate_prompt_cache(user_id)
        logger.debug("♻️ User cache invalidated | user_id=%s", user_id)

    # ── Фоновые задачи ────────────────────────────────────────────────────────

    # ── Основной метод ────────────────────────────────────────────────────────

    async def chat(
        self,
        user_id: int,
        user_message: str,
        is_voice: bool = False,
        resume_bridge: str | None = None,
    ) -> ChatResult:
        """Точка входа с rate-limiting — не более 10 одновременных LLM-запросов."""
        async with _LLM_SEMAPHORE:
            return await self._chat_inner(user_id, user_message, is_voice, resume_bridge)

    async def _chat_inner(
        self,
        user_id: int,
        user_message: str,
        is_voice: bool = False,
        resume_bridge: str | None = None,
    ) -> ChatResult:
        """
        Точка входа — делегирует оркестратору.
        Сохраняет в историю, извлекает факты в фоне.
        """
        # ── Обработка команды «забудь / не запоминай» ──────────────────────────
        import re as _re
        _FORGET_RE = _re.compile(
            r"\b(не надо|не нужно|не запоминай|забудь|удали|убери|сотри|не спрашивай)"
            r".{0,30}(это|об этом|про это|данные|информацию|тему)?\b",
            _re.IGNORECASE,
        )
        if _FORGET_RE.search(user_message):
            from app.database import clear_open_threads, clear_structured_memory
            clear_open_threads(user_id)
            # Если просит забыть вообще всё — чистим и structured_memory
            _FORGET_ALL_RE = _re.compile(
                r"\b(забудь всё|удали всё|очисти память|сотри всё|забудь обо мне)\b",
                _re.IGNORECASE,
            )
            if _FORGET_ALL_RE.search(user_message):
                clear_structured_memory(user_id)

        # Миграция старых фактов → structured_memory (один раз, фоново)
        self._run_background(self._memory.migrate_old_memory(user_id))

        # LLM-классификатор запускаем сразу как задачу —
        # пока он думает, мы синхронно готовим decisions_block и calibration_hint
        # Загружаем последние сообщения для контекста follow-up вопросов.
        # Используем небольшой лимит (4) — только для классификатора поиска.
        # Основная история грузится позже в оркестраторе с полным лимитом.
        from app.database import load_history as _load_hist
        _recent = _load_hist(user_id, limit=4) if self._search else []

        search_task: asyncio.Task | None = None
        if self._search:
            search_task = asyncio.create_task(
                self._decide_search(user_message, recent_history=_recent)
            )

        # Блок принятых решений — для системного промпта агента
        decisions_block = self._consistency.get_decisions_block(user_id)

        # Калибровка: проверяем не жалуется ли пользователь на предыдущий ответ
        self._calibration.check_mismatch(user_id, user_message)

        # Подсказка роутеру на основе накопленных ошибок
        calibration_hint = self._calibration.get_hint(user_message)

        # Ждём решения классификатора и запускаем поиск если нужен
        # (к этому моменту классификатор уже отработал пока мы готовили контекст)
        search_results = ""
        if search_task is not None:
            try:
                needs_search, optimized_query = await search_task
                if needs_search and self._search:
                    raw = await self._search.search(optimized_query)
                    if raw:
                        search_results = raw
                        logger.info(
                            "user_id=%s | поиск добавлен в контекст (query='%s')",
                            user_id, optimized_query[:60],
                        )
            except Exception:
                logger.exception("user_id=%s | ошибка поиска", user_id)

        agent_result = await self._get_orchestrator().run(
            user_id=user_id,
            message=user_message,
            search_results=search_results,
            is_voice=is_voice,
            extra={
                "decisions_block":  decisions_block,
                "resume_bridge":    resume_bridge,
                "calibration_hint": calibration_hint,
            },
        )

        # Запоминаем маршрут для следующей проверки
        self._calibration.record_route(user_id, user_message, agent_result.agent_name)

        reply    = agent_result.content
        reminder = (agent_result.metadata or {}).get("reminder")

        save_messages(user_id, user_message, reply)

        # Структурированная память — извлекаем каждые N сообщений
        if self._memory.should_extract(user_id):
            self._run_background(
                self._memory.extract_and_save(user_id, user_message)
            )

        # Контекст разговора — обновляем каждые N сообщений
        if self._context.should_update(user_id):
            self._run_background(self._context.update(user_id))

        # Personality feedback — каждые 6 сообщений
        if (self._msg_counter.get(user_id, 0)) % 6 == 0:
            from app.personality_service import update_feedback, update_emotional_patterns
            self._run_background(update_feedback(user_id, self._llm))
            self._run_background(update_emotional_patterns(user_id))

        # Interaction memory — каждое сообщение (фоново, лёгкая операция)
        from app.llm_pipeline import extract_interaction
        self._run_background(extract_interaction(user_id, user_message, self._llm))

        logger.debug(
            "user_id=%s | агент=%s | reminder=%s",
            user_id, agent_result.agent_name, reminder is not None,
        )


        # Consistency — проверяем согласованность с принятыми решениями
        reply = await self._consistency.check_and_fix(user_id, reply, user_message)

        return ChatResult(
            reply=reply,
            reminder=reminder,
            agent_name=agent_result.agent_name,
            metadata=agent_result.metadata or {},
        )

    # ── Вспомогательные для других обработчиков ───────────────────────────────

    async def get_resume_phrase(self, user_id: int) -> str | None:
        """
        Возвращает фразу-мостик если пользователь вернулся после паузы.
        None если пауза короткая или контекста нет.
        """
        return await self._context.build_resume_phrase(user_id)

    def save_photo_exchange(
        self, user_id: int, user_note: str, vision_result: str
    ) -> None:
        """Сохраняет фото-обмен в историю. Синхронный — без await."""
        save_messages(user_id, user_note, vision_result)

    async def chat_with_document(
        self,
        user_id: int,
        doc_text: str,
        user_question: str,
        doc_name: str = "документ",
    ) -> str:
        """Отвечает на вопрос по содержимому документа."""

        memory_facts = load_memory(user_id)
        system = settings.system_prompt
        if memory_facts:
            facts = "\n".join(f"- {f}" for f in memory_facts)
            system = f"{system}\n\nЧто известно о пользователе:\n{facts}"

        question = user_question.strip() or "Кратко изложи содержание документа."
        combined = (
            f"Вот содержимое документа «{doc_name}»:\n\n"
            f"{doc_text}\n\n"
            f"Вопрос: {question}"
        )

        messages: list[BaseMessage] = [
            SystemMessage(content=system),
            HumanMessage(content=combined),
        ]

        response = await self._llm.ainvoke(messages)
        reply = str(response.content)

        save_messages(user_id, f"[Документ: {doc_name}] {question}", reply)
        logger.debug("user_id=%s | документ: %s", user_id, doc_name)
        return reply
