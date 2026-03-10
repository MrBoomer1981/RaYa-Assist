"""
tone_controller.py — Tone Controller.

Перед отправкой ответа анализирует его и корректирует если нужно:
  - слишком формальный → дружелюбнее
  - слишком холодный → добавить живости
  - противоречит характеру RaYa

Использует быструю малую модель (8b) — не блокирует основной ответ.
Корректирует только если отклонение значительное.
"""
import logging

logger = logging.getLogger(__name__)

# Используем быструю модель — нет смысла тратить 70b на проверку
_CONTROLLER_MODEL = "llama-3.1-8b-instant"
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
        from langchain_core.messages import HumanMessage

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
        from langchain_core.messages import HumanMessage

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
