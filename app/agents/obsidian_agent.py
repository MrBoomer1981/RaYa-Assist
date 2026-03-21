"""
obsidian_agent.py — агент Obsidian vault.

Правила:
- Поиск → автоматически в Zettelkasten (если уверена)
- Похожие zettel → дополняет существующую, не создаёт дубль
- Дневник → отдельный файл на каждый день, НЕ в графе знаний
- Планы → Краткосрочные / Долгосрочные (пользователь говорит куда)
- Задачи → матрица Эйзенхауэра (Q1-Q4)
"""
import json
import logging

from langchain_core.messages import HumanMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.integrations.obsidian import (
    QUADRANTS, add_tasks, add_zettel, cleanup_vault, create_note,
    create_plan, delete_task_obsidian, format_all_tasks,
    list_files, list_zettel_titles, mark_task_done_obsidian,
    read_note, search_vault, update_zettel,
    vault_available, vault_stats, write_diary,
)
from app.utils import strip_json

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — ведёшь Obsidian vault Сократа.

Правила:
- Дневник — личные переживания, события дня. Один файл на день. НЕ в базе знаний.
- Zettelkasten — одна атомарная идея/концепция/факт. С тегами и [[ссылками]].
  Если похожая карточка уже есть — дополни её, не создавай дубль.
- Заметки — структурированная информация которую не нужно атомизировать.
- Планы — краткосрочные (≤2 нед) или долгосрочные. Сократ говорит куда.
- Задачи — матрица Эйзенхауэра (Q1-Q4).

При поиске через интернет — сохраняй найденное в Zettelkasten автоматически.
Обращайся только "Сократ".\
"""

# ── Классификатор ─────────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """\
Определи действие для Obsidian vault.

Сообщение: «{message}»
История: {history}

Действия:
- diary    — личная запись, день, настроение, события
- zettel   — идея, концепция, факт для базы знаний
- note     — структурированная заметка
- plan     — план (краткосрочный ≤2 нед / долгосрочный)
- tasks    — список дел
- search   — найти в vault
- read     — открыть конкретный файл
- list     — перечислить файлы
- stats    — статистика
- cleanup  — почистить лишние файлы
- none     — просто разговор, ничего не сохранять

JSON (только JSON):
{{"action":"...","content":"очищенный текст","confidence":0.0-1.0,"plan_horizon":"short|long|unknown","reason":"..."}}"""

# ── Zettel — определение нового vs дополнение ────────────────────────────────

_ZETTEL_DEDUP_PROMPT = """\
Есть новая идея: «{new_idea}»

Существующие карточки в базе знаний:
{existing}

Это новая самостоятельная идея или дополнение к одной из существующих?

JSON (только JSON):
{{"decision":"new|update","existing_id":"ID карточки или пусто","reason":"одна строка"}}"""

_ZETTEL_CREATE_PROMPT = """\
Создай атомарную Zettelkasten карточку. Одна идея — чётко и ёмко.

Текст: {text}

JSON (только JSON):
{{"title":"название 5-8 слов","content":"суть в 2-4 предложениях","tags":["тег1","тег2","тег3"],"links":[]}}"""

_TASKS_PROMPT = """\
Извлеки задачи и определи квадрант Эйзенхауэра.

Текст: {text}

q1: срочно+важно | q2: важно,не срочно | q3: срочно,не важно | q4: остальное

JSON (только JSON):
{{"groups":[{{"quadrant":"q1","tasks":["..."]}}]}}"""

_PLAN_PROMPT = """\
Создай структурированный план в markdown.

Тема: {text}
Горизонт: {horizon}

JSON (только JSON):
{{"title":"название плана","content":"план в markdown со шагами и дедлайнами"}}"""


class ObsidianAgent(BaseAgent):
    agent_name = "obsidian"
    timeout    = 50

    def _system_prompt(self) -> str:
        return _SYSTEM

    # ── Классификация ──────────────────────────────────────────────────────────

    async def _classify(self, ctx: AgentContext) -> dict:
        history_lines = []
        for msg in (ctx.history or [])[-6:]:
            role = "Сократ" if msg.__class__.__name__ == "HumanMessage" else "RaYa"
            history_lines.append(f"{role}: {msg.content[:80]}")

        prompt = _CLASSIFY_PROMPT.format(
            message=ctx.message[:800],
            history="\n".join(history_lines) or "нет",
        )
        resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
        raw  = strip_json(str(resp.content))
        try:
            data = json.loads(raw)
            logger.info("📓 classify: action='%s' conf=%.2f — %s",
                        data.get("action"), data.get("confidence", 0),
                        data.get("reason", "")[:60])
            return data
        except Exception:
            logger.warning("classify parse fail: %s", raw[:80])
            return {"action": "none", "content": ctx.message, "confidence": 0.0}

    # ── Главный execute ────────────────────────────────────────────────────────

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        if not vault_available():
            return AgentResult(success=False, agent_name=self.agent_name,
                content="Сократ, Obsidian vault недоступен. Проверь OBSIDIAN_VAULT_PATH.")

        classified = await self._classify(ctx)
        action     = classified.get("action", "none")
        content    = classified.get("content", ctx.message).strip()
        confidence = classified.get("confidence", 0.0)

        if action == "none" or confidence < 0.4:
            return await self._handle_none(ctx)

        try:
            if action == "diary":   return await self._diary(content)
            if action == "zettel":  return await self._zettel(content)
            if action == "note":    return await self._note(content)
            if action == "plan":
                horizon = classified.get("plan_horizon", "unknown")
                return await self._plan(content, horizon, ctx)
            if action == "tasks":   return await self._tasks(content, ctx)
            if action == "search":  return await self._search(content)
            if action == "read":    return await self._read(content)
            if action == "list":    return await self._list(ctx)
            if action == "stats":   return await self._stats()
            if action == "cleanup": return await self._cleanup()
            return await self._handle_none(ctx)
        except Exception as e:
            logger.exception("ObsidianAgent error action=%s", action)
            return AgentResult(success=False, agent_name=self.agent_name,
                content=f"Сократ, ошибка vault: {e}")

    # ── Дневник ────────────────────────────────────────────────────────────────

    async def _diary(self, content: str) -> AgentResult:
        path = write_diary(content)
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Записала в дневник. 📔\n`{path}`",
            metadata={"action": "diary", "path": path})

    # ── Zettelkasten — умный dedup ─────────────────────────────────────────────

    async def _zettel(self, content: str) -> AgentResult:
        # Шаг 1: проверяем есть ли похожая карточка
        existing = list_zettel_titles()
        decision = "new"
        existing_id = ""

        if existing:
            # Берём последние 20 для контекста
            existing_str = "\n".join(
                f"- {e['id']}: {e['title']} [{', '.join(e['tags'][:3])}]"
                for e in existing[-20:]
            )
            dedup_prompt = _ZETTEL_DEDUP_PROMPT.format(
                new_idea=content[:500],
                existing=existing_str,
            )
            resp = await self._llm.ainvoke([HumanMessage(content=dedup_prompt)])
            raw  = strip_json(str(resp.content))
            try:
                dedup = json.loads(raw)
                decision    = dedup.get("decision", "new")
                existing_id = dedup.get("existing_id", "").strip()
                logger.info("🧠 Zettel dedup: %s (id=%s)", decision, existing_id)
            except Exception:
                pass

        # Шаг 2: создаём или дополняем
        if decision == "update" and existing_id:
            path = update_zettel(existing_id, content)
            if path:
                return AgentResult(success=True, agent_name=self.agent_name,
                    needs_critic=False,
                    content=f"Дополнила существующую карточку. 🔄\n`{path}`",
                    metadata={"action": "zettel_update", "path": path})

        # Новая карточка
        create_prompt = _ZETTEL_CREATE_PROMPT.format(text=content[:1500])
        resp  = await self._llm.ainvoke([HumanMessage(content=create_prompt)])
        raw   = strip_json(str(resp.content))
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
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Добавила в базу знаний. 🧠\n**{title}**\n`{path}`\n_{tags_str}_",
            metadata={"action": "zettel", "path": path, "tags": tags})

    # ── Заметка ────────────────────────────────────────────────────────────────

    async def _note(self, content: str) -> AgentResult:
        resp  = await self._llm.ainvoke([HumanMessage(content=
            f"Создай структурированную заметку в markdown.\n\nТекст: {content[:2000]}\n\n"
            f'JSON: {{"title":"название","content":"markdown","tags":["тег1","тег2"]}}'
        )])
        raw = strip_json(str(resp.content))
        try:
            data  = json.loads(raw)
            title = data.get("title", content[:50])
            body  = data.get("content", content)
            tags  = data.get("tags", [])
        except Exception:
            title, body, tags = content[:50], content, []
        path = create_note(title, body, tags)
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Заметка создана. 📝\n**{title}**\n`{path}`",
            metadata={"action": "note", "path": path})

    # ── Планы ──────────────────────────────────────────────────────────────────

    async def _plan(self, content: str, horizon: str, ctx: AgentContext) -> AgentResult:
        # Если горизонт не определён — спрашиваем
        if horizon == "unknown":
            return AgentResult(success=True, agent_name=self.agent_name,
                content="Сократ, куда сохранить план — в краткосрочные (≤2 недели) или долгосрочные?")

        prompt = _PLAN_PROMPT.format(text=content[:2000], horizon=horizon)
        resp   = await self._llm.ainvoke([HumanMessage(content=prompt)])
        raw    = strip_json(str(resp.content))
        try:
            data  = json.loads(raw)
            title = data.get("title", content[:50])
            body  = data.get("content", content)
        except Exception:
            title, body = content[:50], content

        path       = create_plan(title, body, horizon)
        folder_rus = "Краткосрочные" if horizon == "short" else "Долгосрочные"
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"План сохранён в {folder_rus}. 📅\n**{title}**\n`{path}`",
            metadata={"action": "plan", "path": path, "horizon": horizon})

    # ── Задачи ─────────────────────────────────────────────────────────────────

    async def _tasks(self, content: str, ctx: AgentContext) -> AgentResult:
        resp   = await self._llm.ainvoke([HumanMessage(content=
            _TASKS_PROMPT.format(text=content[:1000]))])
        raw    = strip_json(str(resp.content))
        try:
            groups = json.loads(raw).get("groups", [])
        except Exception:
            tasks  = [t.lstrip("- •").strip() for t in content.splitlines() if t.strip()]
            groups = [{"quadrant": "q2", "tasks": tasks}] if tasks else []

        if not groups or not any(g.get("tasks") for g in groups):
            return AgentResult(success=False, agent_name=self.agent_name,
                content="Сократ, не смогла разобрать задачи.")

        reply_lines = ["Задачи добавлены по матрице Эйзенхауэра:\n"]
        for group in groups:
            q     = group.get("quadrant", "q2")
            tasks = group.get("tasks", [])
            if not tasks:
                continue
            q_info = QUADRANTS.get(q, QUADRANTS["q2"])
            add_tasks(tasks, quadrant=q)
            reply_lines.append(f"{q_info['emoji']} **{q_info['title']}**")
            for t in tasks:
                reply_lines.append(f"  • {t}")
            reply_lines.append("")

        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(reply_lines))

    # ── Поиск ──────────────────────────────────────────────────────────────────

    async def _search(self, content: str) -> AgentResult:
        # Сначала семантический поиск, fallback на fulltext
        sem_results = []
        try:
            from app.semantic_search import semantic_search
            sem_results = await semantic_search(content, top_k=5)
        except Exception:
            logger.debug("semantic search unavailable, fallback to fulltext")

        results = sem_results if sem_results else search_vault(content)

        if not results:
            return AgentResult(success=True, agent_name=self.agent_name,
                content=f"Сократ, по запросу «{content}» ничего не нашла в vault.")

        search_type = "семантически" if sem_results else "по тексту"
        lines = [f"Нашла {len(results)} совпадений {search_type} по «{content}»:\n"]
        for r in results[:5]:
            score_str = f" [{r['score']:.0%}]" if "score" in r else ""
            lines.append(f"📄 `{r['path']}`{score_str}\n_{r.get('snippet','')[:120]}_\n")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(lines))

    # ── Читать ─────────────────────────────────────────────────────────────────

    async def _read(self, content: str) -> AgentResult:
        text = read_note(content)
        if not text:
            return AgentResult(success=True, agent_name=self.agent_name,
                content=f"Сократ, «{content}» не нашла.")
        preview = text[:2000] + ("\n\n_... (обрезано)_" if len(text) > 2000 else "")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content=preview)

    # ── Список ─────────────────────────────────────────────────────────────────

    async def _list(self, ctx: AgentContext) -> AgentResult:
        m = ctx.message.lower()
        if "задач" in m:        folder, label = "Задачи",       "задачи"
        elif "zettel" in m:     folder, label = "Zettelkasten",  "Zettelkasten"
        elif "дневник" in m:    folder, label = "Дневник",       "дневник"
        elif "план" in m:       folder, label = "Планы",         "планы"
        else:                   folder, label = "Заметки",       "заметки"
        files = list_files(folder)
        if not files:
            return AgentResult(success=True, agent_name=self.agent_name,
                content=f"Сократ, в «{label}» пока пусто.")
        lines = [f"📁 {label} ({len(files)}):\n"]
        for f in files[:20]:
            lines.append(f"• {f.split('/')[-1].replace('.md','')}")
        if len(files) > 20:
            lines.append(f"_...и ещё {len(files)-20}_")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(lines))

    # ── Статистика ─────────────────────────────────────────────────────────────

    async def _stats(self) -> AgentResult:
        stats = vault_stats()
        total = sum(stats.values())
        icons = {"Дневник":"📔","Заметки":"📝","Zettelkasten":"🧠","Планы":"📅"}
        lines = ["📊 Obsidian vault:\n"]
        for folder, count in stats.items():
            icon = icons.get(folder, "")
            lines.append(f"{icon} {folder}: {count}".strip())
        lines.append(f"\nВсего: {total}")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content="\n".join(lines))

    # ── Очистка ────────────────────────────────────────────────────────────────

    async def _cleanup(self) -> AgentResult:
        result  = cleanup_vault()
        deleted = result.get("deleted", [])
        if not deleted:
            return AgentResult(success=True, agent_name=self.agent_name,
                content="Сократ, vault чист — лишних файлов нет. 👍")
        return AgentResult(success=True, agent_name=self.agent_name, needs_critic=False,
            content=f"Почистила vault. 🗑️ Удалено {len(deleted)}:\n" +
                    "\n".join(f"• {n}" for n in deleted))

    # ── Fallback ───────────────────────────────────────────────────────────────

    async def _handle_none(self, ctx: AgentContext) -> AgentResult:
        messages = self._build_messages(ctx)
        resp     = await self._llm.ainvoke(messages)
        return AgentResult(success=True, agent_name=self.agent_name,
            content=str(resp.content))
