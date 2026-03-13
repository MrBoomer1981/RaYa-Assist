"""
llm_pipeline.py — всё что обрабатывает сообщение на пути через LLM.

Пять компонентов в одном файле (все используются только llm_service):
  MemoryService      — извлекает факты из сообщений, хранит в structured_memory
  ContextService     — отслеживает тему и цель разговора, строит bridge при паузе
  ConsistencyService — следит за когнитивной последовательностью ответов
  ToneController     — мягко корректирует тон финального ответа
  RouterCalibration  — учится на ошибках роутера, накапливает подсказки
"""
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import settings

# ══════════════════════════════════════════════════════════
# MEMORY SERVICE
# ══════════════════════════════════════════════════════════


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
  "context":     {{"ключ": "значение"}},
  "decisions":   {{"ключ": "значение"}}
}}

Примеры:
- "Я живу в Самаре" → {{"facts": {{"город": "Самара"}}}}
- "Работаю над ботом на Python" → {{"projects": {{"raya_bot": "Telegram бот на Python"}}, "skills": {{"python": "Python разработка"}}}}
- "Хочу выучить Rust" → {{"goals": {{"изучить_rust": "Выучить язык Rust"}}}}
- "Предпочитаю короткие ответы" → {{"preferences": {{"стиль_ответов": "короткие и по делу"}}}}
- "Сейчас разбираюсь с Railway деплоем" → {{"context": {{"текущая_задача": "деплой на Railway"}}}}
- "Решил использовать PostgreSQL" → {{"decisions": {{"бд_проекта": "PostgreSQL вместо SQLite"}}}}
- "Буду деплоить на Railway" → {{"decisions": {{"хостинг": "Railway.app"}}}}
- "Выбрал llama-3.3-70b для основной модели" → {{"decisions": {{"llm_модель": "llama-3.3-70b-versatile"}}}}

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

            prompt   = _EXTRACTION_PROMPT.format(message=message[:500])
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw = strip_json(str(response.content))

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

        prompt   = _INTERACTION_PROMPT.format(message=message[:400])
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = (
            str(response.content).strip()
            .replace("```json", "").replace("```", "").strip()
        )
        if not raw or raw == "{}":
            return

        data = json.loads(raw)
        topic   = str(data.get("topic", "")).strip()
        summary = str(data.get("summary", "")).strip()

        if topic and summary:
            upsert_interaction(user_id, topic, summary)
            logger.info("🔁 Interaction: '%s' | user_id=%s", topic, user_id)
    except Exception:
        logger.debug("extract_interaction: ошибка", exc_info=True)


# ══════════════════════════════════════════════════════════
# CONTEXT SERVICE
# ══════════════════════════════════════════════════════════


logger = logging.getLogger(__name__)

# Обновляем контекст каждые N сообщений
_UPDATE_EVERY_N = 4

# Пауза после которой считаем что разговор возобновился (часы)
_RESUME_PAUSE_HOURS = 2

_ANALYSIS_PROMPT = """\
Проанализируй последние сообщения диалога и определи контекст разговора.

Диалог:
{history}

Верни ТОЛЬКО JSON (без markdown, без пояснений):
{{
  "topic": "одна фраза — главная тема разговора",
  "user_goal": "что пользователь хочет достичь в этом разговоре",
  "open_threads": ["незавершённая тема 1", "незавершённая тема 2"],
  "last_summary": "2-3 предложения — о чём говорили, к чему пришли"
}}

Правила:
- topic: коротко, конкретно (например: "разработка Telegram бота", "выбор БД")
- user_goal: цель именно в этом разговоре, не глобальная
- open_threads: только реально незавершённые темы, максимум 3, пустой список если всё закрыто
- last_summary: нейтрально, от третьего лица
- Если диалог только начался — верни пустые строки и пустой список
- Только JSON"""

_RESUME_PROMPT = """\
Сократ вернулся после паузы {hours:.0f} ч. Вот что обсуждали:
Тема: {topic}
Цель: {user_goal}
Незавершённые темы: {threads}
Краткое резюме: {summary}

Напиши одну короткую фразу-мостик (1 предложение) — напомни Сократу о чём говорили,
чтобы легко продолжить. Тон живой, не формальный.
Обращайся "Сократ". Только фраза, без лишних слов."""


class ContextService:
    """Сервис анализа и хранения контекста разговора."""

    def __init__(self, llm) -> None:
        self._llm     = llm
        self._counter: dict[int, int] = {}

    def should_update(self, user_id: int) -> bool:
        """Возвращает True каждые N сообщений."""
        count = self._counter.get(user_id, 0) + 1
        self._counter[user_id] = count
        return count % _UPDATE_EVERY_N == 0

    async def update(self, user_id: int) -> None:
        """
        Анализирует последние сообщения и обновляет контекст.
        Вызывается фоново — не блокирует ответ.
        """
        try:

            messages = load_history(user_id, limit=12)
            if len(messages) < 2:
                return

            # Форматируем историю для анализа
            history_text = "\n".join(
                f"{'Сократ' if m.__class__.__name__ == 'HumanMessage' else 'RaYa'}: {m.content[:200]}"
                for m in messages
            )

            prompt   = _ANALYSIS_PROMPT.format(history=history_text)
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw = strip_json(str(response.content))

            data = json.loads(raw)
            if not isinstance(data, dict):
                return

            save_conversation_context(
                user_id=user_id,
                topic=str(data.get("topic", "")),
                user_goal=str(data.get("user_goal", "")),
                open_threads=data.get("open_threads", []),
                last_summary=str(data.get("last_summary", "")),
            )

            logger.info(
                "🗣️ Контекст обновлён | topic='%s' | user_id=%s",
                data.get("topic", "")[:50], user_id,
            )

        except (json.JSONDecodeError, ValueError):
            logger.debug("context: не удалось распарсить JSON")
        except Exception:
            logger.exception("context: ошибка обновления")

    async def build_resume_phrase(self, user_id: int) -> str | None:
        """
        Если пользователь вернулся после паузы — строит фразу-мостик.
        Возвращает строку или None если пауза короткая или контекста нет.
        """
        try:

            ctx = get_conversation_context(user_id)
            if not ctx["topic"] and not ctx["last_summary"]:
                return None

            # Проверяем паузу по времени последнего сообщения
            history = load_history(user_id, limit=1)
            if not history:
                return None

            # updated_at контекста — когда последний раз анализировали
            if not ctx["updated_at"]:
                return None

            updated = datetime.strptime(ctx["updated_at"], "%Y-%m-%d %H:%M:%S")
            pause_hours = (datetime.utcnow() - updated).total_seconds() / 3_600  # → часы

            if pause_hours < _RESUME_PAUSE_HOURS:
                return None  # пауза слишком короткая

            logger.info(
                "⏸️ Пауза %.1fч — строим фразу-мостик | user_id=%s",
                pause_hours, user_id,
            )

            threads_str = "; ".join(ctx["open_threads"]) if ctx["open_threads"] else "нет"

            prompt = _RESUME_PROMPT.format(
                hours=pause_hours,
                topic=ctx["topic"] or "общий разговор",
                user_goal=ctx["user_goal"] or "не определена",
                threads=threads_str,
                summary=ctx["last_summary"] or "нет данных",
            )

            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            phrase   = str(response.content).strip()

            # Убираем кавычки если модель их добавила
            phrase = phrase.strip('"\'')
            return phrase if phrase else None

        except Exception:
            logger.exception("context: ошибка построения bridge")
            return None


# ══════════════════════════════════════════════════════════
# CONSISTENCY SERVICE
# ══════════════════════════════════════════════════════════


logger = logging.getLogger(__name__)

# Сигналы рекомендации — проверяем только такие ответы
_RECOMMENDATION_RE = re.compile(
    r"\b(лучше использовать|рекомендую|советую|стоит выбрать|"
    r"лучший вариант|оптимально|предлагаю|лучше взять|"
    r"попробуй другой|замени на|переключись на|выбери|используй)\b",
    re.IGNORECASE,
)

# Термины где последовательность критична
_TECH_RE = re.compile(
    r"\b(postgresql|sqlite|mysql|mongodb|redis|"
    r"railway|heroku|vps|docker|kubernetes|"
    r"fastapi|django|flask|aiohttp|"
    r"groq|openai|anthropic|ollama|"
    r"python|rust|go|typescript|javascript|"
    r"llama|gpt|claude|mistral|"
    r"react|vue|svelte|nextjs)\b",
    re.IGNORECASE,
)

# Сигналы принятия решения (для автосохранения)
_DECISION_RE = re.compile(
    r"\b(решил|выбрал|буду использовать|остановился на|"
    r"договорились на|определились с|используем|"
    r"будем деплоить|будем хранить|будем писать)\b",
    re.IGNORECASE,
)


class ConsistencyService:
    """Следит за когнитивной последовательностью ответов RaYa."""

    def __init__(self, llm) -> None:
        self._llm = llm
        # Сессионный кэш: user_id → {тема → позиция}
        self._session: dict[int, dict[str, str]] = {}

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def check_and_fix(
        self,
        user_id: int,
        reply: str,
        message: str,
    ) -> str:
        """
        Главная точка входа. Проверяет ответ и при необходимости исправляет.
        Никогда не бросает исключений — возвращает оригинал при ошибке.
        """
        try:
            decisions = self._load_decisions(user_id)
            session   = self._session.get(user_id, {})

            # Фоново: автосохраняем новые решения из ответа
            await self._auto_save_decisions(user_id, reply, message)

            # Обновляем сессионные позиции
            self._update_session(user_id, reply)

            # Если нет данных для сравнения — возвращаем как есть
            if not decisions and not session:
                return reply

            # Быстрая проверка: есть ли сигналы рекомендации + технический термин?
            has_rec  = bool(_RECOMMENDATION_RE.search(reply))
            has_tech = bool(_TECH_RE.search(reply))

            if not (has_rec and has_tech):
                return reply

            # Глубокая проверка через LLM
            contradiction = await self._llm_check(
                user_id, reply, message, decisions, session
            )

            if contradiction:
                fixed = await self._fix_reply(reply, contradiction, decisions, session)
                logger.info(
                    "🔄 Consistency: исправлено | user_id=%s | %s",
                    user_id, contradiction[:60],
                )
                return fixed

            return reply

        except Exception:
            logger.exception("consistency: ошибка проверки")
            return reply

    def get_decisions_block(self, user_id: int) -> str:
        """
        Блок для системного промпта — что RaYa уже рекомендовала/решила.
        Пустая строка если нечего показать.
        """
        db_dec  = self._load_decisions(user_id)
        session = self._session.get(user_id, {})

        parts = []
        if db_dec:
            items = "\n".join(f"  • {k}: {v}" for k, v in list(db_dec.items())[:8])
            parts.append(f"Принятые решения (помни — не противоречь):\n{items}")

        if session:
            items = "\n".join(f"  • {k}: {v}" for k, v in list(session.items())[:5])
            parts.append(f"Твои позиции в этом разговоре:\n{items}")

        if not parts:
            return ""

        return (
            "\n\n--- КОГНИТИВНАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ ---\n"
            + "\n".join(parts)
            + "\n\nЕсли меняешь позицию — скажи об этом явно: "
            "'Я раньше рекомендовала X, но сейчас думаю что Y лучше потому что...'\n"
            "Никогда не противоречь молча.\n"
            "--- КОНЕЦ ---"
        )


    def clear_session(self, user_id: int) -> None:
        """Очищает сессионный кэш (например, при /reset)."""
        self._session.pop(user_id, None)

    # ── Приватные методы ──────────────────────────────────────────────────────

    def _load_decisions(self, user_id: int) -> dict[str, str]:
        """Загружает decisions из structured_memory в БД."""
        try:
            rows = get_memory_by_category(user_id, "decisions")
            return dict(rows) if rows else {}
        except Exception:
            return {}

    def _update_session(self, user_id: int, reply: str) -> None:
        """
        Обновляет сессионные позиции на основе ответа.
        Ищет паттерн 'термин + контекст рекомендации'.
        """
        try:
            session = self._session.setdefault(user_id, {})
            terms   = _TECH_RE.findall(reply)
            for term in set(t.lower() for t in terms):
                m = re.search(
                    rf'.{{0,50}}{re.escape(term)}.{{0,50}}',
                    reply, re.IGNORECASE,
                )
                if m:
                    ctx = m.group().strip()
                    # Сохраняем только если есть рекомендация рядом
                    if _RECOMMENDATION_RE.search(ctx):
                        session[term] = ctx[:120]
        except Exception:
            logger.debug("pipeline: ошибка", exc_info=True)

    async def _auto_save_decisions(
        self,
        user_id: int,
        reply: str,
        message: str,
    ) -> None:
        """
        Если в паре сообщение+ответ есть принятое решение — сохраняем в БД.
        Использует лёгкую эвристику + редкий LLM вызов.
        """
        try:
            combined = f"{message} {reply}"
            has_decision = bool(_DECISION_RE.search(combined))
            has_tech     = bool(_TECH_RE.search(combined))

            if not (has_decision and has_tech):
                return


            prompt = (
                f"Сообщение: {message[:200]}\n"
                f"Ответ: {reply[:300]}\n\n"
                f"Если в этом диалоге принято конкретное техническое или "
                f"личное решение — извлеки его.\n"
                f"Верни JSON: {{\"topic\": \"короткое название\", \"decision\": \"что решили\"}}\n"
                f"Если решения нет — верни: {{}}\n"
                f"Только JSON, без пояснений."
            )
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw = (
                str(response.content).strip()
                .replace("```json", "").replace("```", "").strip()
            )
            if not raw or raw == "{}":
                return

            data  = json.loads(raw)
            topic = str(data.get("topic", "")).strip()
            dec   = str(data.get("decision", "")).strip()

            if topic and dec and len(topic) > 2:
                upsert_memory(user_id, "decisions", topic, dec)
                self._session.setdefault(user_id, {})[topic] = dec
                logger.info(
                    "💾 Decision сохранён: '%s' = '%s' | user_id=%s",
                    topic, dec[:40], user_id,
                )
        except Exception:
            pass  # автосохранение — некритично

    async def _llm_check(
        self,
        user_id: int,
        reply: str,
        message: str,
        decisions: dict[str, str],
        session: dict[str, str],
    ) -> str | None:
        """LLM проверяет наличие противоречия. Возвращает описание или None."""
        all_decisions = {**decisions, **session}
        if not all_decisions:
            return None

        dec_str = "\n".join(f"- {k}: {v}" for k, v in list(all_decisions.items())[:10])

        prompt = (
            f"Есть ли ЯВНОЕ противоречие между новым ответом и принятыми решениями?\n\n"
            f"Принятые решения/позиции:\n{dec_str}\n\n"
            f"Вопрос: {message[:150]}\n"
            f"Новый ответ: {reply[:350]}\n\n"
            f"Если есть противоречие — одно предложение описывающее его.\n"
            f"Если противоречия нет или оно несущественное — ответь: НЕТ"
        )

        response = await self._llm.ainvoke([HumanMessage(content=prompt)])
        result   = str(response.content).strip()

        if result.upper().startswith("НЕТ") or len(result) < 5:
            return None
        return result

    async def _fix_reply(
        self,
        reply: str,
        contradiction: str,
        decisions: dict[str, str],
        session: dict[str, str],
    ) -> str:
        """Исправляет ответ — добавляет признание смены позиции если нужно."""
        all_dec = {**decisions, **session}
        dec_str = "\n".join(f"- {k}: {v}" for k, v in list(all_dec.items())[:8])

        prompt = (
            f"Исправь ответ чтобы он не противоречил принятым решениям.\n\n"
            f"Принятые решения:\n{dec_str}\n\n"
            f"Противоречие: {contradiction}\n\n"
            f"Оригинальный ответ:\n{reply}\n\n"
            f"Правила:\n"
            f"- Если меняешь рекомендацию — добавь: 'Хотя мы говорили о X, "
            f"сейчас думаю что Y лучше потому что...'\n"
            f"- Сохрани тон и стиль оригинала\n"
            f"- Не добавляй лишних объяснений\n"
            f"- Верни только исправленный текст"
        )

        response = await self._llm.ainvoke([HumanMessage(content=prompt)])
        fixed    = str(response.content).strip()
        return fixed if fixed else reply


# ══════════════════════════════════════════════════════════
# TONE CONTROLLER
# ══════════════════════════════════════════════════════════


logger = logging.getLogger(__name__)

# Используем быструю модель — нет смысла тратить 70b на проверку
_CONTROLLER_MODEL = settings.router_model  # из конфига
_TEMPERATURE      = 0.3   # детерминированно, но не 0 — нужна лёгкая переформулировка

_CHECK_PROMPT = """\
Ты проверяешь ответ ИИ-ассистента RaYa (девушка 23 года, умная, прямая, с юмором).

Сообщение пользователя: {user_message}

Ответ RaYa: {response}

Оцени ответ по трём критериям (только цифры, формат: X|X|X):
1. Соответствие характеру RaYa (1=полностью не она, 5=точно она)
2. Теплота/живость (1=сухо и формально, 5=живо и по-человечески)
3. Адекватность длины (1=слишком длинно, 3=в самый раз, 5=слишком коротко)

Ответ ТОЛЬКО в формате: X|X|X
Пример: 4|3|3"""

_REWRITE_PROMPT = """\
Ты редактируешь ответ ИИ-ассистента RaYa (девушка 23 года).

Характер RaYa:
- Прямая, но не грубая
- Живая речь, как умный друг
- Без пафоса и канцелярита
- Иногда краткая реакция перед ответом
- НЕ начинает с "Конечно!", "Отличный вопрос!" и т.п.
- Обращается только "Сократ"

Проблема с ответом: {problem}

Оригинальный ответ:
{response}

Перепиши ответ исправив только проблему. Сохрани весь смысл и информацию.
Верни ТОЛЬКО исправленный ответ, без пояснений."""


class ToneController:
    """Проверяет и при необходимости корректирует тон ответа."""

    def __init__(self, llm_factory) -> None:
        """
        llm_factory — функция () -> LLM, вызывается один раз при первом использовании.
        Ленивая инициализация чтобы не создавать лишний клиент если контроллер отключён.
        """
        self._llm_factory = llm_factory
        self._llm         = None

    def _get_llm(self):
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    async def process(self, user_message: str, response: str) -> str:
        """
        Основной метод. Возвращает (возможно скорректированный) ответ.
        При любой ошибке возвращает оригинал — не блокируем пользователя.
        """
        # Очень короткие ответы не трогаем
        if len(response.strip()) < 60:
            return response

        try:
            scores = await self._check(user_message, response)
            if scores is None:
                return response

            character, warmth, length = scores
            problems = []

            if character < 3:
                problems.append("ответ не звучит как RaYa — слишком безликий или формальный")
            if warmth < 3:
                problems.append("слишком сухо и холодно, нужно добавить живости")
            if length == 1:
                problems.append("слишком длинно — сократи без потери смысла")

            if not problems:
                return response  # всё хорошо — не трогаем

            problem_str = "; ".join(problems)
            logger.info("🎭 Tone Controller: корректирую (%s)", problem_str)

            corrected = await self._rewrite(response, problem_str)
            return corrected if corrected else response

        except Exception:
            logger.exception("ToneController: ошибка, возвращаю оригинал")
            return response

    async def _check(self, user_message: str, response: str) -> tuple[int, int, int] | None:
        """Возвращает (character, warmth, length) или None при ошибке парсинга."""

        prompt = _CHECK_PROMPT.format(
            user_message=user_message[:200],
            response=response[:600],
        )

        llm      = self._get_llm()
        raw      = await llm.ainvoke([HumanMessage(content=prompt)])
        text     = str(raw.content).strip()

        try:
            parts = text.split("|")
            if len(parts) != 3:
                return None
            return tuple(int(p.strip()) for p in parts)
        except (ValueError, TypeError):
            logger.debug("ToneController: не удалось распарсить '%s'", text)
            return None

    async def _rewrite(self, response: str, problem: str) -> str | None:
        """Переписывает ответ с учётом проблемы."""

        prompt = _REWRITE_PROMPT.format(
            problem=problem,
            response=response,
        )

        llm = self._get_llm()
        raw = await llm.ainvoke([HumanMessage(content=prompt)])
        return str(raw.content).strip() or None


def make_tone_controller_factory(groq_api_key: str) -> "ToneController":
    """Создаёт ToneController с ленивой инициализацией LLM."""
    def _factory():
        from langchain_groq import ChatGroq
        return ChatGroq(
            api_key=groq_api_key,
            model=_CONTROLLER_MODEL,
            temperature=_TEMPERATURE,
        )
    return ToneController(_factory)


# ══════════════════════════════════════════════════════════
# ROUTER CALIBRATION
# ══════════════════════════════════════════════════════════

from app.database import (
    DB_PATH, get_conversation_context,
    get_memory_by_category, get_structured_memory, load_history,
    load_memory, save_conversation_context, upsert_interaction, upsert_memory,
)

logger = logging.getLogger(__name__)

# Сигналы неверного роутинга
_MISMATCH_RE = re.compile(
    r"\b(не об этом|не то|не понял|другой вопрос|я спрашивал|"
    r"имел в виду|переформулирую|не так понял|снова спрошу|"
    r"другое имел|нет не то|совсем не то)\b",
    re.IGNORECASE,
)


def _db_path() -> Path:
    return DB_PATH


def init_calibration_table() -> None:
    """Создаёт таблицу если не существует."""
    try:
        with sqlite3.connect(str(_db_path())) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS router_feedback (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    message_hash TEXT    NOT NULL,
                    keywords     TEXT    NOT NULL,
                    wrong_agent  TEXT    NOT NULL,
                    right_agent  TEXT,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_rf_keywords
                ON router_feedback(keywords)
            """)
    except Exception:
        logger.exception("calibration: ошибка создания таблицы")


class RouterCalibration:
    """Отслеживает ошибки роутера и предоставляет подсказки."""

    def __init__(self) -> None:
        init_calibration_table()
        # Кэш последнего маршрута на сессию: user_id → (message, agent)
        self._last_route: dict[int, tuple[str, str]] = {}

    def record_route(self, user_id: int, message: str, agent: str) -> None:
        """Запоминаем что было отправлено какому агенту."""
        self._last_route[user_id] = (message, agent)

    def check_mismatch(self, user_id: int, next_message: str) -> bool:
        """
        Проверяем: следующее сообщение Сократа — сигнал недовольства?
        Если да — сохраняем фидбэк в БД.
        """
        if user_id not in self._last_route:
            return False

        is_mismatch = bool(_MISMATCH_RE.search(next_message))
        if not is_mismatch:
            return False

        prev_msg, wrong_agent = self._last_route[user_id]
        self._save_feedback(user_id, prev_msg, wrong_agent)
        logger.info(
            "📊 Router calibration: зафиксирован неверный роутинг '%s' → '%s'",
            prev_msg[:40], wrong_agent,
        )
        return True

    def get_hint(self, message: str) -> str | None:
        """
        Возвращает подсказку для роутера на основе накопленных ошибок.
        Формат: 'сообщения похожие на X обычно не для агента Y'
        """
        try:
            keywords = self._extract_keywords(message)
            if not keywords:
                return None

            with sqlite3.connect(str(_db_path())) as con:
                rows = con.execute("""
                    SELECT wrong_agent, COUNT(*) as cnt
                    FROM router_feedback
                    WHERE keywords LIKE ?
                    GROUP BY wrong_agent
                    ORDER BY cnt DESC
                    LIMIT 3
                """, (f"%{keywords[0]}%",)).fetchall()

            if not rows:
                return None

            hints = [
                f"избегай агента '{row[0]}' для сообщений о '{keywords[0]}' "
                f"(зафиксировано {row[1]} ошибок)"
                for row in rows if row[1] >= 2
            ]
            return "\n".join(hints) if hints else None

        except Exception:
            return None

    def _save_feedback(self, user_id: int, message: str, wrong_agent: str) -> None:
        try:
            keywords = " ".join(self._extract_keywords(message))
            msg_hash = str(hash(message.strip().lower()))[:12]

            with sqlite3.connect(str(_db_path())) as con:
                con.execute("""
                    INSERT INTO router_feedback
                        (user_id, message_hash, keywords, wrong_agent)
                    VALUES (?, ?, ?, ?)
                """, (user_id, msg_hash, keywords, wrong_agent))
        except Exception:
            logger.exception("calibration: ошибка сохранения фидбэка")

    @staticmethod
    def _extract_keywords(message: str) -> list[str]:
        """Извлекает ключевые слова из сообщения (простая версия)."""
        stop = {
            "и", "в", "на", "с", "по", "для", "что", "как", "это",
            "не", "но", "а", "я", "ты", "мне", "мой", "моя",
        }
        words = re.findall(r"\b[а-яёa-z]{4,}\b", message.lower())
        return [w for w in words if w not in stop][:5]