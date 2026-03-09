"""
context_service.py — сервис контекста разговора.

Анализирует диалог и поддерживает живое состояние:
  topic        — текущая тема
  user_goal    — что пользователь хочет достичь
  open_threads — незавершённые обсуждения
  last_summary — краткое резюме последних сообщений

Обновляется каждые N сообщений фоново.
При возобновлении разговора после паузы — строит bridge-фразу.
"""
import json
import logging
from datetime import datetime, timedelta

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
            from app.database import load_history, save_conversation_context
            from langchain_core.messages import HumanMessage

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
            raw = (
                str(response.content)
                .strip()
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )

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
            from app.database import get_conversation_context, load_history
            from langchain_core.messages import HumanMessage

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
            pause_hours = (datetime.utcnow() - updated).total_seconds() / 3600

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

    @staticmethod
    def get_prompt_block(user_id: int) -> str:
        """Возвращает блок контекста для системного промпта."""
        from app.database import format_context_for_prompt
        return format_context_for_prompt(user_id)
