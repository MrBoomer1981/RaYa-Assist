"""
deep_research_agent.py — мост между Раей и DEEper.

Этот файл — ЕДИНСТВЕННАЯ точка интеграции.
DEEper дорабатывается в deeper/ независимо, этот мост не трогается.

Что делает:
  1. Принимает запрос от оркестратора Раи
  2. Показывает выбор режима (simple/deep/study) через Telegram
  3. Запускает DEEper ResearchAgent с live-прогрессом
  4. Сохраняет результат в DEEper KnowledgeBase
  5. Возвращает отчёт + метаданные (id, sources, stats)

Интерфейс DEEper намеренно не трогается — только импортируем и вызываем.
"""
import asyncio
import logging
import time
from typing import Optional

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings as _cfg

logger = logging.getLogger(__name__)


# ── Ленивая инициализация DEEper (не грузим до первого запроса) ──────────────

_bridge: Optional["DEEperBridge"] = None


def _get_bridge() -> "DEEperBridge":
    global _bridge
    if _bridge is None:
        _bridge = DEEperBridge()
    return _bridge


class DEEperBridge:
    """
    Инициализирует DEEper один раз и держит в памяти.
    Переиспользуется между запросами — экономим память и время.
    """

    def __init__(self) -> None:
        from deeper.config import deeper_config
        from deeper.services.knowledge_base import KnowledgeBase
        from deeper.services.research_agent import ResearchAgent
        from deeper.services.embeddings import EmbeddingService

        deeper_config.ensure_dirs()

        self.config = deeper_config

        embedding_service = EmbeddingService(
            groq_api_key=deeper_config.groq_api_key,
            index_path=deeper_config.faiss_index_path,
        )
        self.kb = KnowledgeBase(
            db_path=deeper_config.db_path,
            embedding_service=embedding_service,
            max_researches=deeper_config.max_researches,
        )
        self.agent = ResearchAgent(
            config=deeper_config,
            knowledge_base=self.kb,
        )
        logger.info("🔬 DEEperBridge инициализирован | db=%s", deeper_config.db_path)

    async def research(
        self,
        topic: str,
        mode: str = "deep",
        progress_cb=None,
    ) -> dict:
        """Запускает исследование. Возвращает dict с report, id, sources, stats."""
        return await self.agent.research(
            topic=topic,
            mode_name=mode,
            progress_callback=progress_cb,
        )

    def search_kb(self, query: str, limit: int = 5):
        """Поиск по базе знаний DEEper."""
        return self.kb.search(query, limit=limit)

    def get_research(self, research_id: int):
        """Получить исследование по id."""
        return self.kb.get_by_id(research_id)

    def list_researches(self, limit: int = 10):
        """Список последних исследований."""
        return self.kb.get_recent(limit=limit)


# ── Агент ─────────────────────────────────────────────────────────────────────

_MODE_LABELS = {
    "simple": "🟢 Простой (~3 мин, 5 запросов)",
    "deep":   "🔵 Углублённый (~5 мин, 15 запросов)",
    "study":  "🟣 Изучение (~7 мин, 20 запросов)",
}

_DEFAULT_MODE = "deep"


class DeepResearchAgent(BaseAgent):
    """
    Агент глубокого исследования.
    Делегирует всю работу DEEperBridge, сам только маршрутизирует.
    """

    agent_name = "deep_research"
    timeout    = 480  # 8 минут — study mode может быть долгим

    def _system_prompt(self) -> str:
        return ""  # DEEper строит свой промпт внутри

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        query = ctx.message.strip()

        if len(query) < 10:
            return AgentResult(
                success=True,
                content="Уточни тему — нужен конкретный вопрос для исследования.",
                agent_name=self.agent_name,
            )

        # Определяем режим из extra (если передан через inline-кнопки)
        # или используем дефолтный
        mode = (ctx.extra or {}).get("deeper_mode", _DEFAULT_MODE)
        if mode not in ("simple", "deep", "study"):
            mode = _DEFAULT_MODE

        try:
            bridge = _get_bridge()
        except Exception as e:
            logger.exception("DEEperBridge init failed")
            return AgentResult(
                success=False,
                content=f"⚠️ DEEper не удалось запустить: {e}\nПроверь GROQ_API_KEY и TAVILY_API_KEY.",
                agent_name=self.agent_name,
            )

        progress: list[str] = []
        start = time.monotonic()

        async def collect_progress(msg: str):
            progress.append(msg)

        try:
            result = await bridge.research(
                topic=query,
                mode=mode,
                progress_cb=collect_progress,
            )
        except Exception as e:
            logger.exception("DEEper research failed | query=%s", query[:60])
            return AgentResult(
                success=False,
                content=f"⚠️ Исследование не удалось: {e}",
                agent_name=self.agent_name,
                metadata={"progress": progress},
            )

        elapsed   = round(time.monotonic() - start, 1)
        report    = result.get("report", "Отчёт не сформирован.")
        sources   = result.get("sources", [])
        res_id    = result.get("id")
        # Темы и факты — нужны для wiki-связей в Obsidian
        topics    = result.get("topics",    [])
        key_facts = result.get("key_facts", [])

        obs_note = f" | 🗂 [vault]" if obs_path else ""
        footer = f"\n\n---\n🔬 *{_MODE_LABELS[mode]}* | ⏱ {elapsed}с | 📚 {len(sources)} источников{obs_note}"
        if res_id:
            footer += f" | ID: {res_id}"

        logger.info(
            "✅ DEEper завершён | mode=%s | sources=%d | %.1fs | id=%s",
            mode, len(sources), elapsed, res_id,
        )

        # Сохраняем отчёт в Obsidian vault
        obs_path = None
        if _cfg.obsidian_enabled:
            try:
                from app.services.obsidian import save_research_report as obs_research
                obs_path = await obs_research(query, report, sources, mode)
                logger.info("🔬 Obsidian: отчёт → %s", obs_path)

                # Автосвязи с другими заметками vault (фоново)
                if obs_path and (topics or key_facts):
                    import asyncio as _aio
                    from app.services.obsidian_links import link_research_note
                    _link_task = _aio.create_task(
                        link_research_note(obs_path, query, topics, key_facts)
                    )
                    # Удерживаем от GC до завершения
                    _link_task.add_done_callback(
                        lambda t: logger.info(
                            "🔗 Links: %d связей создано",
                            len(t.result()) if not t.exception() else 0,
                        )
                    )
            except Exception as e:
                logger.warning("🔬 Obsidian research sync failed: %s", e)

        return AgentResult(
            success=True,
            content=report + footer,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={
                "deeper_id":     res_id,
                "deeper_mode":   mode,
                "sources":       sources,
                "progress":      progress,
                "elapsed":       elapsed,
                "deep_research": True,
                "obsidian_path": obs_path,
            },
        )
