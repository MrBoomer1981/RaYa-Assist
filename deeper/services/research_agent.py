"""
Core research agent — parallel scrape+analyze pipeline, source scoring,
two-stage search, report verification.
All reports in Russian.
"""
import asyncio
from typing import Callable, List, Optional, Tuple

from deeper.config import DeeperConfig as Config, ResearchMode, RESEARCH_MODES
from deeper.services.cache_manager import CacheManager
from deeper.services.groq_rotator import GroqKeyRotator
from deeper.services.knowledge_base import KnowledgeBase
from deeper.services.planner import ResearchPlanner
from deeper.services.web_scraper import WebScraper
from deeper.services.web_search import WebSearch
from deeper.utils.logger import get_logger
from deeper.utils.text_utils import chunk_text, truncate_text

logger = get_logger("research_agent")

CHUNK_ANALYSIS_SYSTEM = """Ты — аналитик-исследователь. Извлеки ключевые факты и данные 
из предоставленного текста веб-страницы. Отвечай ТОЛЬКО на русском языке. 
Будь конкретным, фокусируйся на фактах, цифрах и важных выводах."""

REPORT_SYSTEM = """Ты — эксперт-аналитик. Создай подробный структурированный отчёт на РУССКОМ языке.

Структура отчёта (строго соблюдай):

# {title}

## Краткое резюме
[2-3 предложения — главный вывод]

## Ключевые находки
[5-8 пунктов — самые важные открытия]

## Риски и ограничения
[Потенциальные риски, ограничения, оговорки]

## Научные и технические подробности
[Глубокий технический/научный анализ]

## Заключение
[Итоговый вывод и рекомендации]

## Источники
{sources_placeholder}

Отчёт должен быть объективным, основанным на фактах, написанным по-русски."""

VERIFY_SYSTEM = """Ты — критический редактор. Проверь исследовательский отчёт и улучши его.

Задачи:
1. Найди противоречия или неточности
2. Выяви пропущенные важные аспекты
3. Усиль слабые разделы
4. Убедись что все разделы полные и информативные

Верни УЛУЧШЕННУЮ версию отчёта на русском языке, сохраняя структуру."""

ASK_SYSTEM = """Ты — эксперт-аналитик. У тебя есть исследовательский отчёт.
Отвечай на вопросы пользователя ТОЛЬКО на основе данных из отчёта.
Если информации в отчёте недостаточно — честно скажи об этом.
Отвечай на русском языке, чётко и по существу."""


class ResearchAgent:
    def __init__(self, config: Config, knowledge_base: KnowledgeBase) -> None:
        self.config = config
        self.kb = knowledge_base
        self.rotator = GroqKeyRotator()
        self.cache = CacheManager(db_path=config.db_path)
        self.scraper = WebScraper(
            cache=self.cache,
            timeout=config.scrape_timeout,
            retries=config.scrape_retries,
        )

    # ------------------------------------------------------------------
    # Main research pipeline
    # ------------------------------------------------------------------

    async def research(
        self,
        topic: str,
        mode_name: str = "deep",
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        mode: ResearchMode = RESEARCH_MODES.get(mode_name, RESEARCH_MODES["deep"])

        async def notify(msg: str) -> None:
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass  # callback fail не должен ломать research

        planner = ResearchPlanner(
            groq_api_key=self.config.groq_api_key,
            primary_model=self.config.primary_model,
            n_queries=mode.search_queries,
        )
        searcher = WebSearch(
            api_key=self.config.tavily_api_key,
            pages_per_query=self.config.pages_per_query,
        )

        # ── Stage 1: broad search ─────────────────────────────────────
        await notify(f"🧠 Этап 1: составляю план ({mode.label})...")
        plan = await planner.generate_plan(topic)
        title = plan.get("title", topic)
        stage1_queries = plan.get("queries", [topic])
        logger.info("Этап 1 | {} | '{}' | {} запросов",
                    mode.name, title, len(stage1_queries))

        await notify(f"🔍 Этап 1: {len(stage1_queries)} широких запросов...")
        stage1_results = await searcher.search_many(stage1_queries)
        all_urls, all_snippets, all_answers = self._collect_results(stage1_results)

        # ── Stage 2: refined search ───────────────────────────────────
        await notify("🔍 Этап 2: генерирую уточняющие запросы...")
        stage2_queries = await planner.refine_queries(topic, all_snippets + all_answers)

        await notify(f"🔍 Этап 2: {len(stage2_queries)} уточняющих запросов...")
        stage2_results = await searcher.search_many(stage2_queries)
        s2_urls, s2_snippets, s2_answers = self._collect_results(stage2_results)

        seen = set(all_urls)
        for url in s2_urls:
            if url not in seen:
                seen.add(url)
                all_urls.append(url)
        all_snippets += s2_snippets
        all_answers += s2_answers
        all_urls = all_urls[:mode.max_pages]
        logger.info("Итого: {} URL, {} AI-ответов", len(all_urls), len(all_answers))

        # ── Parallel scrape + analyze ─────────────────────────────────
        await notify(f"⚡ Параллельный анализ {len(all_urls)} страниц...")
        findings = await self._parallel_scrape_and_analyze(
            all_urls, topic, mode.max_chunks_per_page, notify
        )

        # ── Generate report ───────────────────────────────────────────
        await notify("✍️ Формирую отчёт...")
        report = await self._generate_report(
            topic, title, plan, findings, all_snippets, all_answers, all_urls
        )

        # ── Verify (Deep + Study only) ────────────────────────────────
        if mode_name in ("deep", "study"):
            await notify("🔍 Проверяю и улучшаю отчёт...")
            report = await self._verify_report(report, topic)

        # ── Save ──────────────────────────────────────────────────────
        summary = await self._generate_summary(topic, report)
        topics, key_facts = await self._extract_topics(topic, summary)
        await notify("💾 Сохраняю в базу знаний...")
        research_id = await self.kb.save_research(
            title=title, summary=summary, report=report, sources=all_urls,
        )

        logger.info("Исследование #{} завершено: '{}'", research_id, title)
        return {
            "id":          research_id,   # alias для совместимости с RaYa
            "research_id": research_id,
            "title":       title,
            "report":      report,
            "sources":     all_urls,
            "summary":     summary,
            "topics":      topics,
            "key_facts":   key_facts,
            "mode":        mode.label,
        }

    # ------------------------------------------------------------------
    # Follow-up Q&A
    # ------------------------------------------------------------------

    async def ask(self, research_id: int, question: str) -> str:
        """Answer a question based on a saved research report."""
        research = self.kb.get_research(research_id)
        if not research:
            return f"❌ Исследование #{research_id} не найдено."

        report_ctx = truncate_text(research.report, max_tokens=5000)
        try:
            answer = await self.rotator.chat(
                model=self.config.primary_model,
                messages=[
                    {"role": "system", "content": ASK_SYSTEM},
                    {"role": "user", "content": (
                        f"Отчёт об исследовании '{research.title}':\n\n{report_ctx}"
                        f"\n\n---\nВопрос: {question}"
                    )},
                ],
                max_tokens=1500,
                temperature=0.3,
            )
            logger.info("Ask #{}: '{}'", research_id, question[:60])
            return answer
        except Exception as e:
            logger.error("Ask failed: {}", e)
            return "❌ Не удалось обработать вопрос. Попробуй ещё раз."

    # ------------------------------------------------------------------
    # Parallel scrape + analyze
    # ------------------------------------------------------------------

    async def _parallel_scrape_and_analyze(
        self,
        urls: List[str],
        topic: str,
        max_chunks: int,
        notify: Callable,
    ) -> List[str]:
        """
        Stream pages as they finish scraping and analyze immediately.
        Higher-scored sources are processed first.
        Findings from authoritative sources get a score-weighted prefix.
        """
        all_findings: List[str] = []
        analyze_semaphore = asyncio.Semaphore(3)
        pages_done = 0
        pages_total = len(urls)

        async def analyze_page(text: str, score: int) -> List[str]:
            chunks = chunk_text(text, self.config.chunk_size, self.config.chunk_overlap)
            findings = []
            # Higher-scored sources get more chunks analyzed
            adjusted_max = max_chunks + (2 if score >= 8 else 1 if score >= 6 else 0)
            async with analyze_semaphore:
                for chunk in chunks[:adjusted_max]:
                    f = await self._analyze_chunk(chunk, topic, score)
                    if f:
                        findings.append(f)
            return findings

        analyze_tasks = []

        async for text, score in self.scraper.scrape_stream(urls):
            pages_done += 1
            if pages_done % 5 == 0 or pages_done == pages_total:
                await notify(
                    f"⚡ Обработано {pages_done}/{pages_total} страниц, "
                    f"найдено {len(all_findings)} фактов..."
                )
            task = asyncio.create_task(analyze_page(text, score))
            analyze_tasks.append(task)

        # Wait for all pending analysis tasks
        results = await asyncio.gather(*analyze_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_findings.extend(r)

        logger.info("Параллельный анализ завершён: {} фактов", len(all_findings))
        return all_findings

    async def _analyze_chunk(self, chunk: str, topic: str, score: int = 5) -> Optional[str]:
        """Analyze a chunk. High-score sources get authority prefix."""
        authority_note = ""
        if score >= 8:
            authority_note = "[АВТОРИТЕТНЫЙ ИСТОЧНИК] "
        elif score >= 6:
            authority_note = "[КАЧЕСТВЕННЫЙ ИСТОЧНИК] "

        try:
            result = await self.rotator.chat(
                model=self.config.fast_model,
                messages=[
                    {"role": "system", "content": CHUNK_ANALYSIS_SYSTEM},
                    {"role": "user", "content": f"Тема: {topic}\n\nТекст:\n{chunk}"},
                ],
                max_tokens=400,
                temperature=0.2,
            )
            return f"{authority_note}{result}" if authority_note else result
        except Exception as e:
            logger.warning("Ошибка анализа чанка: {}", e)
            return None

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    async def _generate_report(self, topic, title, plan, findings, snippets, answers, sources) -> str:
        # Sort findings — authoritative ones first
        auth = [f for f in findings if f.startswith("[АВТОРИТЕТНЫЙ")]
        qual = [f for f in findings if f.startswith("[КАЧЕСТВЕННЫЙ")]
        rest = [f for f in findings if not f.startswith("[")]
        sorted_findings = auth + qual + rest

        findings_text = truncate_text("\n\n".join(sorted_findings), max_tokens=4000)
        sources_fmt = "\n".join(
            f"{i+1}. {u} (авторитетность: {self.scraper.scraper.score_url(u) if hasattr(self.scraper, 'scraper') else '—'})"
            for i, u in enumerate(sources)
        ) if False else "\n".join(f"{i+1}. {u}" for i, u in enumerate(sources))

        answers_text = truncate_text("\n".join(answers), max_tokens=1500) if answers else ""
        snippet_ctx = truncate_text("\n".join(snippets[:20]), 800)

        prompt = f"""Тема исследования: {topic}
Обзор плана: {plan.get("overview", "")}
Углы исследования: {", ".join(plan.get("angles", []))}

{'=== КЛЮЧЕВЫЕ AI-ОТВЕТЫ (высокая достоверность) ===' + chr(10) + answers_text + chr(10) if answers_text else ''}
=== ИЗВЛЕЧЁННЫЕ ФАКТЫ ({len(findings)} источников, отсортированы по авторитетности) ===
{findings_text}

=== ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ===
{snippet_ctx}

Приоритизируй факты из авторитетных источников [АВТОРИТЕТНЫЙ ИСТОЧНИК].
Напиши полный отчёт на русском языке."""

        try:
            return await self.rotator.chat(
                model=self.config.primary_model,
                messages=[
                    {"role": "system", "content": REPORT_SYSTEM.format(
                        title=title, sources_placeholder=sources_fmt)},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=4000,
                temperature=0.4,
            )
        except Exception as e:
            logger.error("Ошибка генерации отчёта: {}", e)
            return (
                f"# {title}\n\n## Краткое резюме\nИсследование по теме: {topic}\n\n"
                f"## Ключевые находки\n" + "\n".join(f"- {f[:200]}" for f in findings[:10])
                + f"\n\n## Источники\n{sources_fmt}"
            )

    async def _verify_report(self, report: str, topic: str) -> str:
        try:
            verified = await self.rotator.chat(
                model=self.config.primary_model,
                messages=[
                    {"role": "system", "content": VERIFY_SYSTEM},
                    {"role": "user", "content": (
                        f"Тема: {topic}\n\nОтчёт:\n\n"
                        + truncate_text(report, max_tokens=3500)
                    )},
                ],
                max_tokens=4000,
                temperature=0.3,
            )
            logger.info("Отчёт проверен и улучшен")
            return verified
        except Exception as e:
            logger.warning("Верификация пропущена: {}", e)
            return report

    async def _extract_topics(self, topic: str, summary: str) -> tuple[list[str], list[str]]:
        """Извлекает темы и ключевые факты из summary для wiki-связей в Obsidian."""
        import json
        try:
            from groq import AsyncGroq
            client = AsyncGroq(api_key=self.config.groq_api_key)
            prompt = (
                f"Тема исследования: {topic}\n"
                f"Краткое резюме: {summary[:600]}\n\n"
                "Верни ТОЛЬКО JSON без пояснений:\n"
                '{"topics": ["тема1", "тема2", "тема3"], '
                '"key_facts": ["факт1", "факт2", "факт3"]}\n'
                "topics — 3-5 ключевых тем (1-3 слова каждая).\n"
                "key_facts — 3-5 конкретных фактов (компании, технологии, имена).\n"
                "Только JSON."
            )
            resp = await client.chat.completions.create(
                model=self.config.fast_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.0,
            )
            raw = resp.choices[0].message.content.strip()
            # Убираем markdown-блоки если есть
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            return data.get("topics", [])[:5], data.get("key_facts", [])[:5]
        except Exception as e:
            logger.debug("_extract_topics failed: {}", e)
            # Fallback: используем первые слова темы
            words = topic.split()[:5]
            return [" ".join(words[:3]), " ".join(words[:2])], []

    async def _generate_summary(self, topic: str, report: str) -> str:
        try:
            return await self.rotator.chat(
                model=self.config.fast_model,
                messages=[
                    {"role": "system", "content": "Создай краткое резюме из 2-3 предложений на русском языке."},
                    {"role": "user", "content": truncate_text(report, 3000)},
                ],
                max_tokens=300,
                temperature=0.3,
            )
        except Exception:
            return f"Исследование по теме: {topic}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_results(search_results: dict):
        urls, snippets, answers = [], [], []
        seen = set()
        for result in search_results.values():
            for url in result["urls"]:
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
            snippets.extend(result["snippets"])
            answers.extend(result.get("answers", []))
        return urls, snippets, answers
