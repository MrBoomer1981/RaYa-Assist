"""
deep_research.py — движок глубокого исследования в стиле Perplexity Deep Research.

Архитектура:
  QueryDecomposer     — разбивает запрос на 3-6 под-вопросов
  ResearchGraph       — граф знаний: что искали → что нашли → какие пробелы
  IterativeCollector  — параллельный сбор по под-вопросам + gap-filling
  ReportSynthesizer   — длинный структурированный отчёт с секциями и источниками

Использование:
  engine = DeepResearchEngine()
  async for update in engine.research(query, user_id, progress_cb):
      ...  # каждый update — строка прогресса
  report = engine.get_report()
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator, Callable

from typing import Any
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

_MAX_SUBQUESTIONS  = 5   # максимум под-вопросов
_MAX_SOURCES       = 20  # максимум источников в итоговом отчёте
_MAX_GAP_ROUNDS    = 2   # максимум итераций gap-filling
_SEARCH_PER_Q      = 4   # результатов на под-вопрос
_SNIPPET_LEN       = 600 # символов на сниппет


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SubQuestion:
    text:      str
    angle:     str        # угол: "определение" / "история" / "механизм" / "критика" / "будущее"
    findings:  list[dict] = field(default_factory=list)  # [{title, content, url, score}]
    answered:  bool       = False
    confidence: float     = 0.0


@dataclass
class ResearchGraph:
    """Граф знаний — что знаем, что ищем, что пропустили."""
    query:          str
    sub_questions:  list[SubQuestion] = field(default_factory=list)
    all_sources:    list[dict]        = field(default_factory=list)
    gaps:           list[str]         = field(default_factory=list)
    synthesis:      str               = ""
    started_at:     float             = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> str:
        return f"{time.monotonic() - self.started_at:.1f}с"

    @property
    def total_sources(self) -> int:
        return len(self.all_sources)

    @property
    def answered_count(self) -> int:
        return sum(1 for q in self.sub_questions if q.answered)


# ═══════════════════════════════════════════════════════════════════════════════
# QUERY DECOMPOSER
# ═══════════════════════════════════════════════════════════════════════════════

_DECOMPOSE_PROMPT = """\
Ты исследовательский ассистент. Твоя задача — разбить сложный вопрос на {n} под-вопросов,
которые вместе дадут полное понимание темы.

Исходный вопрос: {query}

Верни JSON массив объектов:
[
  {{"text": "конкретный под-вопрос", "angle": "определение|история|механизм|критика|будущее|факты|сравнение"}},
  ...
]

Правила:
- Каждый под-вопрос должен быть самодостаточным поисковым запросом
- Углы должны быть разными — не дублируй
- Под-вопросы должны быть конкретными, не абстрактными
- Только JSON, без пояснений\
"""

async def decompose_query(query: str, llm: Any) -> list[SubQuestion]:
    """Разбивает запрос на под-вопросы с разными углами исследования."""
    n = min(_MAX_SUBQUESTIONS, max(3, len(query.split()) // 3 + 2))

    try:
        resp = await llm.ainvoke([
            SystemMessage(content="Ты эксперт по структурированию исследовательских запросов."),
            HumanMessage(content=_DECOMPOSE_PROMPT.format(query=query, n=n)),
        ])
        raw = re.sub(r"```(?:json)?|```", "", str(resp.content)).strip()
        data = json.loads(raw)

        questions = []
        for item in data[:_MAX_SUBQUESTIONS]:
            if isinstance(item, dict) and item.get("text"):
                questions.append(SubQuestion(
                    text=item["text"].strip(),
                    angle=item.get("angle", "факты"),
                ))

        if questions:
            logger.info("🔬 DeepResearch: декомпозиция → %d под-вопросов", len(questions))
            return questions

    except Exception as e:
        logger.warning("decompose_query error: %s", e)

    # Fallback: базовые углы
    angles = ["определение и суть", "ключевые факты и данные", "практическое применение"]
    return [SubQuestion(text=f"{query} — {a}", angle=a) for a in angles]


# ═══════════════════════════════════════════════════════════════════════════════
# ITERATIVE COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════════

async def collect_for_question(
    q: SubQuestion,
    svc,  # SearchService
) -> SubQuestion:
    """Ищет ответ на один под-вопрос, оценивает полноту."""
    try:
        results = await svc.deep_search(q.text, max_results=_SEARCH_PER_Q)

        if not results:
            q.confidence = 0.0
            return q

        q.findings   = results[:_SEARCH_PER_Q]
        # Оцениваем уверенность по среднему score результатов
        scores       = [r.get("score", 0.5) for r in q.findings]
        q.confidence = min(1.0, sum(scores) / len(scores))
        q.answered   = q.confidence >= 0.45

        logger.debug("  ✓ '%s' → %d источников, confidence=%.2f",
                     q.text[:50], len(q.findings), q.confidence)
    except Exception as e:
        logger.warning("collect_for_question error: %s → %s", q.text[:40], e)
        q.confidence = 0.0

    return q


async def find_gaps(graph: ResearchGraph, llm: Any) -> list[str]:
    """Определяет что ещё нужно найти на основе собранных данных."""
    answered = [q for q in graph.sub_questions if q.answered]
    if not answered:
        return []

    findings_summary = "\n".join(
        f"- {q.text}: {'; '.join(r.get('title','') for r in q.findings[:2])}"
        for q in answered
    )

    try:
        resp = await llm.ainvoke([
            HumanMessage(content=(
                f"Исходный вопрос: {graph.query}\n\n"
                f"Что уже найдено:\n{findings_summary}\n\n"
                "Какие важные аспекты темы ещё не раскрыты? "
                "Верни JSON массив строк — конкретные поисковые запросы для пробелов. "
                "Максимум 2. Если пробелов нет — верни []."
            )),
        ])
        raw = re.sub(r"```(?:json)?|```", "", str(resp.content)).strip()
        gaps = json.loads(raw)
        if isinstance(gaps, list):
            return [str(g) for g in gaps[:2] if g]
    except Exception:
        pass

    return []


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT SYNTHESIZER
# ═══════════════════════════════════════════════════════════════════════════════

_SYNTHESIS_PROMPT = """\
Ты аналитик-исследователь. На основе собранных данных напиши структурированный отчёт.

Исходный вопрос: {query}

Собранные данные по под-вопросам:
{findings}

Требования к отчёту:
1. Начни с краткого резюме (2-3 предложения) — главный вывод
2. Разбей на тематические секции с заголовками **Секция**
3. В каждой секции: факты, цифры, конкретика — не общие слова
4. Укажи противоречия или спорные моменты если они есть
5. Заключение: что это означает на практике
6. Длина: 400-800 слов
7. Без URL. Используй [Источник N] для ссылок на источники.
8. Тон: аналитический, но живой. Не академический сухой язык.\
"""

async def synthesize_report(graph: ResearchGraph, llm: Any) -> str:
    """Синтезирует финальный отчёт из всех собранных данных."""

    # Собираем все findings
    findings_parts = []
    source_index   = 1
    sources_map    = {}

    for q in graph.sub_questions:
        if not q.findings:
            continue
        findings_parts.append(f"\n**{q.angle.upper()}: {q.text}**")
        for r in q.findings[:3]:
            title   = r.get("title", "")
            content = r.get("content", "")[:_SNIPPET_LEN]
            url     = r.get("url", "")
            if not content:
                continue
            findings_parts.append(f"[Источник {source_index}] {title}\n{content}")
            if url:
                sources_map[source_index] = {"title": title, "url": url}
            source_index += 1

    # Gap findings
    for src in graph.all_sources:
        title   = src.get("title", "")
        content = src.get("content", "")[:_SNIPPET_LEN]
        if not content:
            continue
        findings_parts.append(f"[Источник {source_index}] {title}\n{content}")
        source_index += 1

    findings_text = "\n".join(findings_parts)

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=(
                "Ты пишешь аналитический исследовательский отчёт. "
                "Факты важнее красоты. Конкретика важнее общих слов."
            )),
            HumanMessage(content=_SYNTHESIS_PROMPT.format(
                query=graph.query,
                findings=findings_text[:6000],
            )),
        ])
        report = str(resp.content).strip()
    except Exception as e:
        logger.error("synthesize_report error: %s", e)
        report = "Не удалось сгенерировать отчёт."

    # Добавляем список источников в конец
    if sources_map:
        source_lines = ["\n\n---\n**Источники:**"]
        for idx, src in list(sources_map.items())[:_MAX_SOURCES]:
            title = src.get("title", f"Источник {idx}")
            source_lines.append(f"[{idx}] {title}")
        report += "\n".join(source_lines)

    return report


# ═══════════════════════════════════════════════════════════════════════════════
# DEEP RESEARCH ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class DeepResearchEngine:
    """
    Оркестратор глубокого исследования.

    Алгоритм:
      1. Декомпозиция запроса → N под-вопросов
      2. Параллельный сбор данных по всем под-вопросам
      3. Анализ пробелов → дополнительный поиск (до 2 итераций)
      4. Синтез → структурированный отчёт

    Прогресс отдаётся через async generator — каждый yield это строка статуса.
    """

    def __init__(self) -> None:
        # Используем shared LLM cache из base_agent — не создаём лишних объектов
        from app.agents.base_agent import _get_llm
        self._llm      = _get_llm(settings.model_name)
        self._fast_llm = _get_llm(settings.router_model)
        self._graph: ResearchGraph | None = None

    async def research(
        self,
        query:       str,
        progress_cb: Callable[[str], None] | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Запускает полный цикл исследования.
        Yields строки прогресса по мере работы.
        По завершении — отчёт доступен через get_report().
        """
        from app.search_service import SearchService
        svc   = SearchService()
        graph = ResearchGraph(query=query)
        self._graph = graph

        def _progress(msg: str):
            logger.info("📚 DeepResearch: %s", msg)
            if progress_cb:
                progress_cb(msg)

        # ── Шаг 1: Декомпозиция ───────────────────────────────────────────────
        yield "🔍 Анализирую вопрос и строю план исследования..."
        _progress(f"Декомпозиция: '{query[:60]}'")

        graph.sub_questions = await decompose_query(query, self._fast_llm)
        n = len(graph.sub_questions)
        yield f"📋 Разбил на {n} направлений: {', '.join(q.angle for q in graph.sub_questions)}"

        # ── Шаг 2: Параллельный сбор ──────────────────────────────────────────
        yield f"⚡ Ищу параллельно по {n} направлениям..."

        tasks   = [collect_for_question(q, svc) for q in graph.sub_questions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("collect error for q[%d]: %s", i, result)
            else:
                graph.sub_questions[i] = result

        answered = graph.answered_count
        sources  = sum(len(q.findings) for q in graph.sub_questions)
        yield f"✅ Нашёл {sources} источников, ответил на {answered}/{n} под-вопросов ({graph.elapsed})"

        # ── Шаг 3: Gap filling (до 2 раундов) ────────────────────────────────
        for gap_round in range(_MAX_GAP_ROUNDS):
            unanswered = [q for q in graph.sub_questions if not q.answered]
            if not unanswered:
                break

            yield f"🔎 Заполняю пробелы (раунд {gap_round + 1})..."

            gaps = await find_gaps(graph, self._fast_llm)
            if not gaps:
                break

            gap_tasks   = [svc.deep_search(gap_q, max_results=3) for gap_q in gaps]
            gap_results = await asyncio.gather(*gap_tasks, return_exceptions=True)

            new_sources = 0
            for gap_res in gap_results:
                if isinstance(gap_res, list):
                    seen_urls = {s.get("url") for s in graph.all_sources}
                    for r in gap_res:
                        if r.get("url") not in seen_urls:
                            graph.all_sources.append(r)
                            new_sources += 1

            if new_sources:
                yield f"  + {new_sources} новых источников из пробелов"

        # ── Шаг 4: Синтез ─────────────────────────────────────────────────────
        total_src = sum(len(q.findings) for q in graph.sub_questions) + len(graph.all_sources)
        yield f"✍️ Синтезирую отчёт из {total_src} источников..."

        graph.synthesis = await synthesize_report(graph, self._llm)

        elapsed = graph.elapsed
        yield f"✅ Исследование завершено за {elapsed} | {total_src} источников"

    def get_report(self) -> str:
        """Возвращает финальный отчёт после завершения research()."""
        if self._graph and self._graph.synthesis:
            now = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
            header = f"📚 **Глубокое исследование**\n_Запрос: {self._graph.query}_\n_Дата: {now}_\n\n"
            return header + self._graph.synthesis
        return "Исследование не завершено."

    def get_stats(self) -> dict:
        """Статистика последнего исследования."""
        if not self._graph:
            return {}
        return {
            "query":          self._graph.query,
            "sub_questions":  len(self._graph.sub_questions),
            "answered":       self._graph.answered_count,
            "total_sources":  self._graph.total_sources,
            "elapsed":        self._graph.elapsed,
        }
