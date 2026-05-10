"""
Research planner — two-stage search strategy.
Stage 1: broad queries to explore the topic.
Stage 2: refined queries based on Stage 1 results.
All output is always in Russian.
"""
import json
from typing import List

from deeper.services.groq_rotator import GroqKeyRotator
from deeper.utils.logger import get_logger
from deeper.utils.text_utils import truncate_text

logger = get_logger("planner")

PLAN_SYSTEM = """Ты — опытный исследователь и аналитик. Всегда отвечай ТОЛЬКО на русском языке.

Получив тему для исследования, создай структурированный JSON-план:
- "title": краткое название исследования на русском
- "overview": 2-3 предложения о том, что будет исследоваться
- "angles": список из 5-7 различных углов исследования
- "queries": список из ровно {n_queries} конкретных поисковых запросов на русском и английском языках

Отвечай ТОЛЬКО валидным JSON. Без markdown, без пояснений."""

PLAN_USER = """Тема исследования: {topic}

Создай план с {n_queries} широкими поисковыми запросами для первичного изучения темы.
Запросы должны быть на русском И английском языках для максимального охвата."""

REFINE_SYSTEM = """Ты — эксперт-аналитик. Отвечай ТОЛЬКО на русском языке.

На основе первичных результатов поиска сгенерируй уточняющие поисковые запросы.
Запросы должны углублять найденные темы, закрывать пробелы и исследовать неожиданные аспекты.

Отвечай ТОЛЬКО валидным JSON: {"queries": ["запрос1", "запрос2", ...]}
Без markdown, без пояснений."""

REFINE_USER = """Тема исследования: {topic}

Первичные результаты поиска:
{snippets}

Сгенерируй {n_queries} уточняющих поисковых запросов, которые:
1. Углубляют наиболее интересные найденные аспекты
2. Закрывают очевидные пробелы в информации
3. Исследуют спорные или неожиданные моменты
4. Ищут экспертные мнения и научные данные

Запросы на русском И английском языках."""


class ResearchPlanner:
    def __init__(self, groq_api_key: str, primary_model: str, n_queries: int = 15) -> None:
        self.rotator = GroqKeyRotator()
        self.primary_model = primary_model
        self.n_queries = n_queries
        # Stage 1: broad — 1/3 of total queries
        self.stage1_count = max(3, n_queries // 3)
        # Stage 2: refined — remaining 2/3
        self.stage2_count = n_queries - self.stage1_count

    async def generate_plan(self, topic: str) -> dict:
        """Generate initial broad research plan (Stage 1 queries)."""
        system = PLAN_SYSTEM.format(n_queries=self.stage1_count)
        user = PLAN_USER.format(topic=topic, n_queries=self.stage1_count)

        try:
            raw = await self.rotator.chat(
                model=self.primary_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=2048,
                temperature=0.7,
            )
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            plan = json.loads(clean)
            plan["queries"] = plan.get("queries", [])[:self.stage1_count]
            plan["stage"] = 1
            logger.info("Этап 1: план '{}' | {} запросов", plan.get("title", topic), len(plan["queries"]))
            return plan
        except Exception as e:
            logger.warning("Ошибка генерации плана: {}. Fallback.", e)
            return self._fallback_plan(topic)

    async def refine_queries(self, topic: str, snippets: List[str]) -> List[str]:
        """
        Stage 2: generate refined queries based on Stage 1 search results.
        Returns list of additional search queries.
        """
        snippets_text = truncate_text("\n".join(snippets[:40]), max_tokens=2000)
        user = REFINE_USER.format(
            topic=topic,
            snippets=snippets_text,
            n_queries=self.stage2_count,
        )
        try:
            raw = await self.rotator.chat(
                model=self.primary_model,
                messages=[
                    {"role": "system", "content": REFINE_SYSTEM},
                    {"role": "user", "content": user},
                ],
                max_tokens=1024,
                temperature=0.8,
            )
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data = json.loads(clean)
            queries = data.get("queries", [])[:self.stage2_count]
            logger.info("Этап 2: {} уточняющих запросов сгенерировано", len(queries))
            return queries
        except Exception as e:
            logger.warning("Ошибка генерации уточняющих запросов: {}", e)
            return self._fallback_refine_queries(topic)

    def _fallback_plan(self, topic: str) -> dict:
        queries = [
            f"{topic} обзор",
            f"{topic} overview",
            f"{topic} что это такое",
            f"{topic} последние исследования",
            f"{topic} применение",
        ]
        return {
            "title": topic,
            "overview": f"Комплексное исследование по теме: {topic}",
            "angles": ["обзор", "технологии", "применение", "риски", "тенденции"],
            "queries": queries[:self.stage1_count],
            "stage": 1,
        }

    def _fallback_refine_queries(self, topic: str) -> List[str]:
        queries = [
            f"{topic} технические детали",
            f"{topic} статистика 2024",
            f"{topic} мнение экспертов",
            f"{topic} риски и проблемы",
            f"{topic} future trends",
            f"{topic} научные исследования",
            f"{topic} история развития",
            f"{topic} сравнение",
            f"{topic} best practices",
            f"{topic} innovations",
        ]
        return queries[:self.stage2_count]
