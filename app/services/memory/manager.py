"""
MemoryManager — оркестратор трёх слоёв памяти.

    mm = MemoryManager(fast_llm)
    ctx = await mm.build_context(user_id, message)   # ДО ответа
    # ctx.to_prompt() → вставляется в системный промпт

    asyncio.create_task(mm.after_turn(user_id, human, ai))  # ПОСЛЕ ответа

Recall + Archival ищутся параллельно (asyncio.gather).
Экстракция фактов — каждые _EXTRACT_EVERY сообщений (не каждое).
Эпизод создаётся каждые _EPISODE_TURNS ходов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Триггеры ──────────────────────────────────────────────────────────────────

_RECALL_TRIGGERS = re.compile(
    r"(помнишь|помнит|мы говорили|мы обсуждали|ранее|раньше|в прошлый раз|"
    r"уже упоминал|ты знаешь|из того что|напомни|вспомни|как я говорил|"
    r"я рассказывал|я упоминал|мой проект|моя цель|я решил)",
    re.IGNORECASE,
)

_ARCHIVAL_TRIGGERS = re.compile(
    r"(что я изучал|что в vault|из исследований|из базы знаний|"
    r"deeper|поиск в памяти|архив|моё исследование про)",
    re.IGNORECASE,
)

# ── Промпты ───────────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """\
Извлеки персональные факты из сообщения пользователя.
Текущая Core Memory:
{core}

Сообщение: {message}

Верни JSON (только новое/изменившееся — пропусти уже известное):
{{
  "facts":       {{"ключ": "значение"}},
  "interests":   {{"ключ": "значение"}},
  "projects":    {{"ключ": "значение"}},
  "preferences": {{"ключ": "значение"}},
  "goals":       {{"ключ": "значение"}},
  "context":     {{"ключ": "значение"}},
  "decisions":   {{"ключ": "значение"}}
}}
Если факт критичен — добавь "!!" в конец значения (importance=4.5).
Если факт устарел — значение "[удалить]".
Если ничего нового — верни {{}}.
Только JSON."""

_EPISODE_PROMPT = """\
Создай краткую запись в долгосрочную память по диалогу.

Диалог:
{dialogue}

Верни ТОЛЬКО JSON:
{{
  "summary": "2-3 предложения: о чём говорили и к чему пришли",
  "key_facts": ["факт 1", "факт 2"],
  "topics": ["тема1", "тема2"],
  "importance": 3
}}
importance: 1=незначительно, 3=обычно, 5=критично.
Только JSON."""


# ── Контекст ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryContext:
    core_block:     str  = ""
    recall_block:   str  = ""
    archival_block: str  = ""
    recall_used:    bool = False
    archival_used:  bool = False
    episodes_found: int  = 0

    def to_prompt(self) -> str:
        parts = [b for b in (self.core_block, self.recall_block, self.archival_block) if b]
        return "\n\n".join(parts)

    def is_empty(self) -> bool:
        return not (self.core_block or self.recall_block or self.archival_block)


# ── MemoryManager ─────────────────────────────────────────────────────────────

class MemoryManager:
    # Экстракция фактов — каждые N сообщений (не каждое, экономим токены)
    _EXTRACT_EVERY = 2

    def __init__(self, fast_llm) -> None:
        """fast_llm — router_model (8b), не тратим большую модель на memory."""
        self._llm         = fast_llm
        self._turn_count: dict[int, int]               = {}
        self._episode_buf: dict[int, list[tuple[str, str]]] = {}
        self._bg_tasks: set = set()  # удерживаем ссылки на фоновые задачи

    # ── Публичные ─────────────────────────────────────────────────────────────

    async def build_context(self, user_id: int, message: str) -> MemoryContext:
        """
        Строит контекст ДО генерации ответа.
        Recall + Archival запрашиваются параллельно.
        """
        import app.feature_flags as ff
        if not ff.memory_enabled():
            return MemoryContext()

        from app.services.memory import core as Core

        ctx = MemoryContext()
        ctx.core_block = Core.format_for_prompt(user_id)

        need_recall   = bool(_RECALL_TRIGGERS.search(message))
        need_archival = bool(_ARCHIVAL_TRIGGERS.search(message))

        if need_recall or need_archival:
            # Параллельный поиск
            recall_task   = self._fetch_recall(user_id, message)   if need_recall   else _noop()
            archival_task = self._fetch_archival(message)           if need_archival else _noop()

            recall_result, archival_result = await asyncio.gather(
                recall_task, archival_task, return_exceptions=True
            )

            if isinstance(recall_result, list) and recall_result:
                ctx.recall_used    = True
                ctx.episodes_found = len(recall_result)
                ctx.recall_block   = _format_recall(recall_result)

            if isinstance(archival_result, list) and archival_result:
                from app.services.memory import archival as Archival
                ctx.archival_used  = True
                ctx.archival_block = Archival.format_for_prompt(archival_result)

        if ctx.recall_used or ctx.archival_used:
            logger.info(
                "🧠 Memory: recall=%s(%d) archival=%s | user_id=%s",
                ctx.recall_used, ctx.episodes_found, ctx.archival_used, user_id,
            )
        return ctx

    async def after_turn(self, user_id: int, human: str, ai: str) -> None:
        """
        Вызывается ПОСЛЕ ответа (фоново).
        1. Каждые _EXTRACT_EVERY сообщений → экстракция фактов в Core
        2. Накопление в буфер
        3. Каждые _EPISODE_TURNS ходов → создание эпизода в Recall
        4. Вытеснение переполненного Core в Recall
        """
        import app.feature_flags as ff
        if not ff.memory_enabled():
            return

        from app.services.memory.recall import _EPISODE_TURNS

        n = self._turn_count.get(user_id, 0) + 1
        self._turn_count[user_id] = n

        # 1. Экстракция — каждые _EXTRACT_EVERY сообщений
        if len(human.strip()) >= 15 and n % self._EXTRACT_EVERY == 1:
            await self._extract_and_save(user_id, human)

        # 2. Буфер
        buf = self._episode_buf.setdefault(user_id, [])
        buf.append((human[:400], ai[:400]))

        # 3. Эпизод
        if n % _EPISODE_TURNS == 0 and len(buf) >= 3:
            await self._create_episode(user_id, buf)
            self._episode_buf[user_id] = []

        # 4. Eviction
        from app.services.memory import core as Core
        evicted = Core.evict_to_recall(user_id)
        if evicted:
            from app.services.memory.recall import save_episode
            save_episode(
                user_id,
                summary   = "Факты вытеснены из Core Memory: "
                            + "; ".join(f"{e['key']}: {e['value']}" for e in evicted),
                key_facts = [f"{e['key']}: {e['value']}" for e in evicted],
                topics    = ["core_eviction"],
                importance = 2.0,
            )

    def clear_session(self, user_id: int) -> None:
        """Сбрасывает состояние сессии (/clear)."""
        self._turn_count.pop(user_id, None)
        # Сохраняем частичный буфер в эпизод если достаточно ходов
        buf = self._episode_buf.pop(user_id, [])
        if len(buf) >= 3:
            _run_bg(self._bg_tasks, self._create_episode(user_id, buf))

    # ── Приватные ─────────────────────────────────────────────────────────────

    async def _fetch_recall(self, user_id: int, query: str) -> list[dict]:
        from app.services.memory import recall as Recall
        return await Recall.search(user_id, query, self._llm)

    async def _fetch_archival(self, query: str) -> list[dict]:
        from app.services.memory import archival as Archival
        return await Archival.search(query, limit=3)

    async def _extract_and_save(self, user_id: int, message: str) -> None:
        """Извлекает факты LLM → Core Memory."""
        from app.services.memory import core as Core
        from app.utils import strip_json
        from langchain_core.messages import HumanMessage

        try:
            core_text = Core.format_for_prompt(user_id) or "пусто"
            prompt    = _EXTRACT_PROMPT.format(
                core=core_text[:600], message=message[:500]
            )
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw      = strip_json(str(response.content))

            if not raw or raw.strip() in ("{}", ""):
                return

            data = json.loads(raw)
            if not isinstance(data, dict):
                return

            saved = deleted = 0
            for category, entries in data.items():
                if not isinstance(entries, dict):
                    continue
                for key, value in entries.items():
                    val = str(value).strip()
                    if not val:
                        continue
                    importance = 4.5 if val.endswith("!!") else 3.0
                    val        = val.removesuffix("!!").strip()
                    if val == "[удалить]":
                        from app.database import delete_memory_entry
                        delete_memory_entry(user_id, category, str(key))
                        deleted += 1
                    elif val:
                        Core.upsert_core_fact(user_id, category, str(key), val, importance)
                        saved += 1

            if saved or deleted:
                logger.debug(
                    "🧠 Core: +%d / -%d | user_id=%s", saved, deleted, user_id
                )

        except (json.JSONDecodeError, ValueError):
            pass
        except Exception:
            logger.exception("memory: extract_and_save failed")

    async def _create_episode(self, user_id: int, turns: list[tuple[str, str]]) -> None:
        """Создаёт эпизод из буфера диалога."""
        from app.services.memory.recall import save_episode
        from app.utils import strip_json
        from langchain_core.messages import HumanMessage

        try:
            dialogue = "\n".join(f"Пользователь: {h}\nRaYa: {a}" for h, a in turns)
            prompt   = _EPISODE_PROMPT.format(dialogue=dialogue[:1500])
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw      = strip_json(str(response.content))
            data     = json.loads(raw)

            save_episode(
                user_id,
                summary    = data.get("summary", dialogue[:200]),
                key_facts  = data.get("key_facts", [])[:5],
                topics     = data.get("topics",    [])[:5],
                importance = float(data.get("importance", 3.0)),
            )
        except Exception:
            logger.exception("memory: create_episode failed")


# ── Хелперы ───────────────────────────────────────────────────────────────────

def _run_bg(tasks_set: set, coro) -> None:
    """Запускает корутину фоново, удерживая ссылку на задачу от GC."""
    task = asyncio.create_task(coro)
    tasks_set.add(task)
    task.add_done_callback(tasks_set.discard)


async def _noop() -> list:
    return []


def _format_recall(episodes: list[dict]) -> str:
    """Форматирует эпизоды. Score нормализован в 0..1 в recall.bm25_search."""
    if not episodes:
        return ""
    lines = ["<recall_memory>"]
    for ep in episodes:
        date    = (ep.get("created_at") or "")[:10]
        score   = ep.get("rerank_score", ep.get("bm25_score"))
        score_s = f" ({score:.0%})" if isinstance(score, float) else ""
        lines.append(f"  [{date}]{score_s} {ep['summary']}")
        for fact in ep.get("key_facts", [])[:2]:
            lines.append(f"    • {fact}")
    lines.append("</recall_memory>")
    return "\n".join(lines)
