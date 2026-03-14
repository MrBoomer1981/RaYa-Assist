"""
obsidian_agent.py — агент для работы с Obsidian vault.

RaYa сама определяет что куда записывать — через LLM классификатор.
Никаких команд запоминать не нужно. Просто говори естественно:

  "сегодня был отличный день, встретился со старым другом"
  → дневник

  "прокрастинация — это страх неудачи, а не лень"
  → zettelkasten

  "нужно купить молоко, позвонить врачу и сдать отчёт"
  → задачи

  "расскажи мне о стоицизме" / "что я писал о продуктивности"
  → поиск/чтение
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.integrations.obsidian import (
    add_tasks, add_zettel, cleanup_vault, create_note, list_files,
    read_note, search_vault, vault_available, vault_stats, write_diary,
)
from app.utils import strip_json

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ИИ-ассистент Сократа, отвечающая за его Obsidian vault.

Ты сама решаешь куда что сохранять — Сократу не нужно запоминать команды.
Просто читай его сообщение и действуй правильно.

Дневник — личные переживания, события дня, настроение, рефлексия.
Заметка — информация, идеи, планы которые нужно сохранить структурно.
Zettelkasten — одна чёткая мысль, концепция, факт который можно переиспользовать.
Задачи — список дел, что нужно сделать.

При записи в дневник — сохраняй голос Сократа, не перефразируй.
При создании Zettel — одна идея, чётко, с тегами.
Всегда подтверждай что именно сохранено. Обращайся только "Сократ".\
"""

# ══════════════════════════════════════════════════════════
# LLM КЛАССИФИКАТОР
# ══════════════════════════════════════════════════════════

_CLASSIFY_PROMPT = """\
Определи что Сократ хочет сделать с Obsidian vault.

Сообщение: «{message}»

Контекст разговора (последние сообщения):
{history}

Варианты действий:
- diary     — личная запись, событие дня, переживание, настроение, рефлексия
- note      — заметка, информация, идея которую нужно структурировать
- zettel    — одна чёткая концепция/мысль для базы знаний
- tasks     — список задач, дел, что нужно сделать
- search    — найти что-то в vault, вспомнить что писал
- read      — открыть конкретную заметку или дневник
- list      — список файлов, задач, заметок
- stats     — статистика vault
- cleanup   — удалить лишние/ненужные файлы из vault, почистить хранилище
- none      — это просто разговор, в vault сохранять не нужно

Правила:
- Если человек рассказывает о своём дне, чувствах, событиях → diary
- Если человек формулирует идею или концепцию → zettel
- Если перечисляет что нужно сделать → tasks
- Если просто разговаривает без намерения сохранить → none
- Если явно просит найти/показать → search или read

Ответь ТОЛЬКО JSON (без markdown):
{{"action":"diary|note|zettel|tasks|search|read|list|stats|none","content":"очищенный текст для сохранения (без служебных фраз вроде запомни/сохрани)","confidence":0.0-1.0,"reason":"одна строка почему"}}"""

_ZETTEL_PROMPT = """\
Создай атомарную Zettelkasten карточку. Одна идея — максимально чётко.

Текст: {text}

JSON (только JSON):
{{"title":"название 5-8 слов","content":"суть в 2-4 предложениях","tags":["тег1","тег2","тег3"],"links":[]}}"""

_NOTE_PROMPT = """\
Создай структурированную заметку в markdown.

Текст: {text}

JSON (только JSON):
{{"title":"название","content":"содержимое в markdown","tags":["тег1","тег2"]}}"""

_TASKS_PROMPT = """\
Извлеки список задач. Каждая задача — отдельный пункт, чётко и кратко.

Текст: {text}

JSON (только JSON):
{{"tasks":["задача 1","задача 2","задача 3"]}}"""


class ObsidianAgent(BaseAgent):
    agent_name = "obsidian"
    timeout    = 45

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _classify(self, ctx: AgentContext) -> dict:
        """LLM определяет action + очищенный content одним вызовом."""
        # Берём последние 3 сообщения как контекст
        history_lines = []
        for msg in (ctx.history or [])[-6:]:
            role = "Сократ" if msg.__class__.__name__ == "HumanMessage" else "RaYa"
            history_lines.append(f"{role}: {msg.content[:100]}")
        history_str = "\n".join(history_lines) if history_lines else "нет"

        prompt = _CLASSIFY_PROMPT.format(
            message=ctx.message[:800],
            history=history_str,
        )
        resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
        raw  = strip_json(str(resp.content))

        try:
            data = json.loads(raw)
            logger.info(
                "📓 Obsidian classify: action='%s' conf=%.2f reason='%s'",
                data.get("action"), data.get("confidence", 0), data.get("reason", "")[:60],
            )
            return data
        except Exception:
            logger.warning("Obsidian: не смог распарсить classify JSON: %s", raw[:100])
            return {"action": "none", "content": ctx.message, "confidence": 0.0}

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        if not vault_available():
            return AgentResult(
                success=False, agent_name=self.agent_name,
                content=(
                    "Сократ, Obsidian vault недоступен. "
                    "Нужно задать OBSIDIAN_VAULT_PATH в Railway Variables."
                ),
            )

        classified = await self._classify(ctx)
        action     = classified.get("action", "none")
        content    = classified.get("content", ctx.message).strip()
        confidence = classified.get("confidence", 0.0)

        # Если уверенность низкая — переспрашиваем только если совсем непонятно
        if action == "none" or confidence < 0.4:
            return await self._handle_none(ctx)

        try:
            if action == "diary":   return await self._diary(content, ctx)
            if action == "zettel":  return await self._zettel(content, ctx)
            if action == "note":    return await self._note(content, ctx)
            if action == "tasks":   return await self._tasks(content, ctx)
            if action == "search":  return await self._search(content, ctx)
            if action == "read":    return await self._read(content, ctx)
            if action == "list":    return await self._list(ctx)
            if action == "stats":   return await self._stats(ctx)
            if action == "cleanup": return await self._cleanup(ctx)
            return await self._handle_none(ctx)
        except Exception as e:
            logger.exception("ObsidianAgent: ошибка action=%s", action)
            return AgentResult(
                success=False, agent_name=self.agent_name,
                content=f"Сократ, ошибка при работе с vault: {e}",
            )

    # ── Diary ──────────────────────────────────────────────────────────────────

    async def _diary(self, content: str, ctx: AgentContext) -> AgentResult:
        path = write_diary(content)
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Записала в дневник. 📔\n`{path}`",
            metadata={"action": "diary", "path": path},
        )

    # ── Zettelkasten ───────────────────────────────────────────────────────────

    async def _zettel(self, content: str, ctx: AgentContext) -> AgentResult:
        prompt = _ZETTEL_PROMPT.format(text=content[:1500])
        resp   = await self._llm.ainvoke([HumanMessage(content=prompt)])
        raw    = strip_json(str(resp.content))
        try:
            data  = json.loads(raw)
            title = data.get("title", content[:60])
            body  = data.get("content", content)
            tags  = data.get("tags", [])
            links = data.get("links", [])
        except Exception:
            title, body, tags, links = content[:60], content, [], []

        path     = add_zettel(title, body, tags, links)
        tags_str = " ".join(f"#{t}" for t in tags)
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Добавила в базу знаний. 🧠\n**{title}**\n`{path}`\n_{tags_str}_",
            metadata={"action": "zettel", "path": path, "tags": tags},
        )

    # ── Note ───────────────────────────────────────────────────────────────────

    async def _note(self, content: str, ctx: AgentContext) -> AgentResult:
        prompt = _NOTE_PROMPT.format(text=content[:2000])
        resp   = await self._llm.ainvoke([HumanMessage(content=prompt)])
        raw    = strip_json(str(resp.content))
        try:
            data  = json.loads(raw)
            title = data.get("title", content[:50])
            body  = data.get("content", content)
            tags  = data.get("tags", [])
        except Exception:
            title, body, tags = content[:50], content, []

        path = create_note(title, body, tags)
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Заметка создана. 📝\n**{title}**\n`{path}`",
            metadata={"action": "note", "path": path},
        )

    # ── Tasks ──────────────────────────────────────────────────────────────────

    async def _tasks(self, content: str, ctx: AgentContext) -> AgentResult:
        prompt = _TASKS_PROMPT.format(text=content[:1000])
        resp   = await self._llm.ainvoke([HumanMessage(content=prompt)])
        raw    = strip_json(str(resp.content))
        try:
            tasks = json.loads(raw).get("tasks", [])
        except Exception:
            tasks = [t.lstrip("- •").strip() for t in content.splitlines() if t.strip()]

        if not tasks:
            return AgentResult(
                success=False, agent_name=self.agent_name,
                content="Сократ, не смогла разобрать задачи. Перечисли их подробнее.",
            )

        path      = add_tasks(tasks)
        tasks_str = "\n".join(f"• {t}" for t in tasks)
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Задачи добавлены в Obsidian. ✅\n\n{tasks_str}\n\n`{path}`",
            metadata={"action": "tasks", "path": path, "count": len(tasks)},
        )

    # ── Search ─────────────────────────────────────────────────────────────────

    async def _search(self, content: str, ctx: AgentContext) -> AgentResult:
        results = search_vault(content)
        if not results:
            return AgentResult(
                success=True, agent_name=self.agent_name,
                content=f"Сократ, по запросу «{content}» ничего не нашла в vault.",
            )
        lines = [f"Нашла {len(results)} совпадений по «{content}»:\n"]
        for r in results[:5]:
            lines.append(f"📄 `{r['path']}`\n_{r['snippet'][:120]}_\n")
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(lines),
            metadata={"action": "search", "count": len(results)},
        )

    # ── Read ───────────────────────────────────────────────────────────────────

    async def _read(self, content: str, ctx: AgentContext) -> AgentResult:
        text = read_note(content)
        if not text:
            return AgentResult(
                success=True, agent_name=self.agent_name,
                content=f"Сократ, заметку «{content}» не нашла.",
            )
        preview = text[:2000] + ("\n\n_... (обрезано)_" if len(text) > 2000 else "")
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content=preview,
        )

    # ── List ───────────────────────────────────────────────────────────────────

    async def _list(self, ctx: AgentContext) -> AgentResult:
        m = ctx.message.lower()
        if "задач" in m:       folder, label = "Задачи", "задачи"
        elif "zettel" in m:    folder, label = "Zettelkasten", "Zettelkasten"
        elif "дневник" in m:   folder, label = "Дневник", "дневник"
        else:                  folder, label = "Заметки", "заметки"

        files = list_files(folder)
        if not files:
            return AgentResult(success=True, agent_name=self.agent_name,
                content=f"Сократ, в разделе «{label}» пока пусто.")

        lines = [f"📁 {label} ({len(files)} шт.):\n"]
        for f in files[:20]:
            lines.append(f"• {f.split('/')[-1].replace('.md','')}")
        if len(files) > 20:
            lines.append(f"_...и ещё {len(files)-20}_")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(lines))

    # ── Stats ──────────────────────────────────────────────────────────────────

    async def _stats(self, ctx: AgentContext) -> AgentResult:
        stats = vault_stats()
        total = sum(stats.values())
        icons = {"Дневник": "📔", "Заметки": "📝", "Задачи": "✅", "Zettelkasten": "🧠"}
        lines = ["📊 Obsidian vault:\n"]
        for folder, count in stats.items():
            lines.append(f"{icons.get(folder,'📄')} {folder}: {count} файлов")
        lines.append(f"\nВсего: {total} заметок")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(lines))

    # ── None / Help ────────────────────────────────────────────────────────────

    async def _cleanup(self, ctx: AgentContext) -> AgentResult:
        """Удаляет лишние файлы из vault."""
        result = cleanup_vault()
        if not result["ok"]:
            return AgentResult(
                success=False, agent_name=self.agent_name,
                content=f"Сократ, не удалось почистить vault: {result.get('error')}",
            )
        deleted = result["deleted"]
        if not deleted:
            return AgentResult(
                success=True, agent_name=self.agent_name, needs_critic=False,
                content="Сократ, в vault всё чисто — лишних файлов нет. 👍",
            )
        deleted_str = "\n".join(f"• {name}" for name in deleted)
        return AgentResult(
            success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Почистила vault, Сократ. 🗑️ Удалено {len(deleted)} файлов:\n\n{deleted_str}",
            metadata={"action": "cleanup", "deleted": deleted},
        )

    async def _handle_none(self, ctx: AgentContext) -> AgentResult:
        """Просто отвечаем как обычный ассистент — vault не трогаем."""
        messages = self._build_messages(ctx)
        resp     = await self._llm.ainvoke(messages)
        return AgentResult(
            success=True, agent_name=self.agent_name,
            content=str(resp.content),
        )
