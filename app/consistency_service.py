"""
consistency_service.py — проверка согласованности ответов RaYa.

Задача: убедиться что ответ не противоречит:
  - принятым решениям (decisions в structured_memory)
  - фактам о пользователе (facts)
  - предыдущим позициям RaYa в этом разговоре

Работает на двух уровнях:
  1. Быстрая проверка — детерминированная, без LLM (ключевые слова)
  2. Глубокая проверка — LLM анализ (только при явных сигналах противоречия)

Принцип: не блокировать ответ, а исправлять или добавлять контекст.
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Слова-сигналы возможного противоречия
_CONTRADICTION_SIGNALS = re.compile(
    r"\b(лучше использовать|рекомендую|советую|стоит выбрать|"
    r"лучший вариант|оптимально|предлагаю использовать|"
    r"лучше взять|попробуй другой|замени на|переключись на)\b",
    re.IGNORECASE,
)

# Технические термины где важна последовательность
_TECH_TERMS = re.compile(
    r"\b(postgresql|sqlite|mysql|mongodb|redis|"
    r"railway|heroku|vps|docker|kubernetes|"
    r"fastapi|django|flask|"
    r"groq|openai|anthropic|ollama|"
    r"python|rust|go|typescript|javascript|"
    r"llama|gpt|claude|mistral)\b",
    re.IGNORECASE,
)


class ConsistencyService:
    """Проверяет согласованность ответа с историей решений."""

    def __init__(self, llm) -> None:
        self._llm = llm
        # Кэш позиций RaYa в текущей сессии: user_id → {тема: позиция}
        self._session_positions: dict[int, dict[str, str]] = {}

    async def check_and_fix(
        self,
        user_id: int,
        reply: str,
        message: str,
    ) -> str:
        """
        Проверяет ответ на противоречия.
        Возвращает исправленный ответ или оригинал если всё ок.
        """
        try:
            decisions = self._get_decisions(user_id)
            if not decisions and not self._session_positions.get(user_id):
                return reply  # нечего проверять

            # Быстрая проверка — есть ли сигналы рекомендации?
            has_recommendation = bool(_CONTRADICTION_SIGNALS.search(reply))
            has_tech_term      = bool(_TECH_TERMS.search(reply))

            if not (has_recommendation and has_tech_term):
                # Нет явного сигнала — только обновляем позиции
                await self._update_positions(user_id, reply)
                return reply

            # Есть сигнал — проверяем через LLM
            contradiction = await self._llm_check(
                user_id, reply, message, decisions
            )

            if contradiction:
                fixed = await self._fix_reply(
                    reply, contradiction, decisions
                )
                logger.info(
                    "🔄 Consistency: исправлено противоречие | user_id=%s | %s",
                    user_id, contradiction[:60],
                )
                await self._update_positions(user_id, fixed)
                return fixed

            await self._update_positions(user_id, reply)
            return reply

        except Exception:
            logger.exception("consistency: ошибка проверки")
            return reply  # никогда не блокируем ответ

    def record_decision(self, user_id: int, topic: str, decision: str) -> None:
        """Явно записывает решение в сессионный кэш."""
        positions = self._session_positions.setdefault(user_id, {})
        positions[topic] = decision

    def get_decisions_block(self, user_id: int) -> str:
        """
        Возвращает блок принятых решений для системного промпта.
        Объединяет БД + сессионный кэш.
        """
        parts = []

        # Из structured_memory (decisions)
        db_decisions = self._get_decisions(user_id)
        if db_decisions:
            items = "\n".join(f"  - {k}: {v}" for k, v in db_decisions.items())
            parts.append(f"Принятые решения (из памяти):\n{items}")

        # Из сессионного кэша
        session = self._session_positions.get(user_id, {})
        if session:
            items = "\n".join(f"  - {k}: {v}" for k, v in session.items())
            parts.append(f"Позиции в этом разговоре:\n{items}")

        if not parts:
            return ""

        return (
            "\n\n⚠️ ВАЖНО — СОГЛАСОВАННОСТЬ:\n"
            + "\n".join(parts)
            + "\nНе противоречь этим решениям. Если нужно изменить позицию — "
            "явно скажи что меняешь мнение и объясни почему."
        )

    # ── Приватные методы ──────────────────────────────────────────────────────

    def _get_decisions(self, user_id: int) -> dict[str, str]:
        """Загружает decisions из structured_memory."""
        try:
            from app.database import get_memory_by_category
            rows = get_memory_by_category(user_id, "decisions")
            return {k: v for k, v in rows} if rows else {}
        except Exception:
            return {}

    async def _update_positions(self, user_id: int, reply: str) -> None:
        """
        Обновляет сессионный кэш позиций на основе ответа.
        Быстро — только регексп, без LLM.
        """
        try:
            positions = self._session_positions.setdefault(user_id, {})
            terms = _TECH_TERMS.findall(reply)
            for term in set(terms):
                term_lower = term.lower()
                # Ищем контекст вокруг термина (50 символов)
                m = re.search(
                    rf'.{{0,40}}{re.escape(term)}.{{0,40}}',
                    reply, re.IGNORECASE,
                )
                if m:
                    positions[term_lower] = m.group().strip()[:100]
        except Exception:
            pass

    async def _llm_check(
        self,
        user_id: int,
        reply: str,
        message: str,
        decisions: dict[str, str],
    ) -> str | None:
        """
        LLM проверяет есть ли противоречие.
        Возвращает описание противоречия или None.
        """
        if not decisions:
            return None

        decisions_str = "\n".join(f"- {k}: {v}" for k, v in decisions.items())
        session_str   = "\n".join(
            f"- {k}: {v}"
            for k, v in self._session_positions.get(user_id, {}).items()
        ) or "нет"

        prompt = (
            f"Проверь: противоречит ли НОВЫЙ ОТВЕТ принятым решениям?\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Позиции в разговоре:\n{session_str}\n\n"
            f"Вопрос пользователя: {message[:200]}\n\n"
            f"Новый ответ: {reply[:400]}\n\n"
            f"Если есть противоречие — опиши его ОДНИМ предложением.\n"
            f"Если противоречий нет — ответь: НЕТ\n"
            f"Только одна строка."
        )

        from langchain_core.messages import HumanMessage
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
    ) -> str:
        """
        Просит LLM исправить ответ с учётом противоречия.
        """
        decisions_str = "\n".join(f"- {k}: {v}" for k, v in decisions.items())

        prompt = (
            f"Исправь ответ так, чтобы он не противоречил принятым решениям.\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Противоречие: {contradiction}\n\n"
            f"Оригинальный ответ:\n{reply}\n\n"
            f"Правила исправления:\n"
            f"- Сохрани смысл и тон оригинала\n"
            f"- Если нужно сменить рекомендацию — добавь 'хотя мы уже решили использовать X'\n"
            f"- Не добавляй лишних объяснений\n"
            f"- Верни только исправленный текст"
        )

        response = await self._llm.ainvoke([HumanMessage(content=prompt)])
        fixed    = str(response.content).strip()
        return fixed if fixed else reply
