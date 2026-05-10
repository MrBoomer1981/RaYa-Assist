"""
obsidian_agent.py — прямое управление Obsidian vault из чата.

Умеет:
  - Создать произвольную заметку
  - Найти заметки по запросу (полнотекстовый поиск)
  - Показать содержимое заметки
  - Удалить заметку
  - Добавить текст в существующую заметку
  - Показать список файлов в папке

Если Obsidian не настроен — сообщает об этом вместо ошибки.
"""
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings as _cfg

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ассистент. Управляешь Obsidian vault пользователя.

Операции — возвращай XML-теги в ответе:

1. Создать заметку:
<obs_create folder="📝 Заметки" title="название">текст заметки</obs_create>

2. Найти заметки:
<obs_search>поисковый запрос</obs_search>

3. Прочитать заметку:
<obs_read>путь/к/файлу.md</obs_read>

4. Добавить в заметку:
<obs_append path="путь/к/файлу.md">текст для добавления</obs_append>

5. Удалить заметку:
<obs_delete>путь/к/файлу.md</obs_delete>

6. Список папки:
<obs_list>имя папки</obs_list>

Стандартные папки:
  📝 Заметки      — произвольные заметки
  📓 Дневник      — дневниковые записи (по датам)
  📅 Расписание   — события календаря (по датам)
  🔬 Исследования — отчёты DEEper

Отвечай коротко и конкретно. Обращайся по имени.
Если пользователь просит что-то найти или прочитать — делай это, не переспрашивай.
"""


class ObsidianAgent(BaseAgent):
    agent_name = "obsidian"
    timeout    = 20

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        if not _cfg.obsidian_enabled:
            return AgentResult(
                success=True,
                content=(
                    "⚠️ Obsidian не подключён.\n\n"
                    "Добавь в `.env` (или Railway Variables):\n"
                    "```\n"
                    "OBSIDIAN_API_URL=https://127.0.0.1:27124\n"
                    "OBSIDIAN_API_KEY=твой_ключ\n"
                    "```\n"
                    "Плагин: [obsidian-local-rest-api](https://github.com/coddingtonbear/obsidian-local-rest-api)"
                ),
                agent_name=self.agent_name,
                needs_critic=False,
            )

        # Быстрые операции без LLM — прямые команды
        msg = ctx.message.strip()
        msg_lower = msg.lower()

        if any(kw in msg_lower for kw in ("список", "что в папке", "покажи папку", "ls ")):
            folder = re.sub(r".*(папк[еу]|ls)\s*", "", msg, flags=re.IGNORECASE).strip()
            return await self._list(folder or "")

        if any(kw in msg_lower for kw in ("найди в vault", "поиск в obsidian", "найди заметку")):
            query = re.sub(r".*(найди|поиск[^:]*)\s*", "", msg, flags=re.IGNORECASE).strip()
            return await self._search(query or msg)

        # LLM разбирает намерение
        messages = [
            SystemMessage(content=_SYSTEM),
            *ctx.history[-4:],
            HumanMessage(content=msg),
        ]
        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        return await self._process_tags(raw, ctx)

    # ── Обработка тегов ───────────────────────────────────────────────────────

    async def _process_tags(self, raw: str, ctx: AgentContext) -> AgentResult:
        from app.services import obsidian as obs

        reply = raw
        extra_parts: list[str] = []

        # obs_create
        m = re.search(r'<obs_create(?:\s+folder="([^"]*)")?\s+title="([^"]*)">(.*?)</obs_create>',
                      raw, re.DOTALL)
        if m:
            folder, title, content = m.group(1) or "📝 Заметки", m.group(2), m.group(3).strip()
            try:
                path = await obs.save_note(title, content, folder)
                extra_parts.append(f"✅ Заметка создана: `{path}`")
            except Exception as e:
                extra_parts.append(f"⚠️ Не удалось создать заметку: {e}")

        # obs_search
        m = re.search(r"<obs_search>(.*?)</obs_search>", raw, re.DOTALL)
        if m:
            query = m.group(1).strip()
            return await self._search(query)

        # obs_read
        m = re.search(r"<obs_read>(.*?)</obs_read>", raw, re.DOTALL)
        if m:
            path = m.group(1).strip()
            try:
                content = await obs.read(path)
                if content:
                    extra_parts.append(f"📄 `{path}`:\n\n{content[:2000]}")
                else:
                    extra_parts.append(f"⚠️ Файл не найден: `{path}`")
            except Exception as e:
                extra_parts.append(f"⚠️ {e}")

        # obs_append
        m = re.search(r'<obs_append\s+path="([^"]*)">(.*?)</obs_append>', raw, re.DOTALL)
        if m:
            path, text = m.group(1), m.group(2).strip()
            try:
                await obs.append(path, f"\n\n{text}")
                extra_parts.append(f"✅ Добавлено в `{path}`")
            except Exception as e:
                extra_parts.append(f"⚠️ Не удалось добавить: {e}")

        # obs_delete
        m = re.search(r"<obs_delete>(.*?)</obs_delete>", raw, re.DOTALL)
        if m:
            path = m.group(1).strip()
            try:
                deleted = await obs.delete(path)
                extra_parts.append(f"{'🗑️ Удалено' if deleted else '⚠️ Файл не найден'}: `{path}`")
            except Exception as e:
                extra_parts.append(f"⚠️ {e}")

        # obs_list
        m = re.search(r"<obs_list>(.*?)</obs_list>", raw, re.DOTALL)
        if m:
            folder = m.group(1).strip()
            return await self._list(folder)

        # Чистим теги из ответа
        reply = re.sub(
            r"<obs_(create|search|read|append|delete|list)[^>]*>.*?</obs_(create|search|read|append|delete|list)>",
            "", reply, flags=re.DOTALL,
        ).strip()

        if extra_parts:
            reply = (reply + "\n\n" + "\n".join(extra_parts)).strip()

        return AgentResult(
            success=True,
            content=reply or "Готово.",
            agent_name=self.agent_name,
            needs_critic=False,
        )

    # ── Быстрые операции ──────────────────────────────────────────────────────

    async def _search(self, query: str) -> AgentResult:
        from app.services import obsidian as obs
        try:
            results = await obs.search(query, limit=10)
            if not results:
                return AgentResult(
                    success=True,
                    content=f"🔍 По запросу «{query}» ничего не найдено.",
                    agent_name=self.agent_name, needs_critic=False,
                )
            lines = [f"🔍 Найдено {len(results)} заметок по «{query}»:\n"]
            for r in results:
                filename = r.get("filename", r.get("path", "?"))
                score    = r.get("score", "")
                score_s  = f" ({score:.2f})" if isinstance(score, float) else ""
                lines.append(f"• `{filename}`{score_s}")
            lines.append("\nНапиши «прочитай [путь]» чтобы открыть.")
            return AgentResult(
                success=True,
                content="\n".join(lines),
                agent_name=self.agent_name, needs_critic=False,
            )
        except Exception as e:
            return AgentResult(
                success=False,
                content=f"⚠️ Ошибка поиска: {e}",
                agent_name=self.agent_name, needs_critic=False,
            )

    async def _list(self, folder: str) -> AgentResult:
        from app.services import obsidian as obs
        try:
            files = await obs.list_folder(folder)
            if not files:
                name = folder or "vault"
                return AgentResult(
                    success=True,
                    content=f"📂 `{name}` пуста или не существует.",
                    agent_name=self.agent_name, needs_critic=False,
                )
            name = folder or "vault"
            lines = [f"📂 `{name}` ({len(files)} файлов):\n"]
            for f in files[:30]:
                lines.append(f"• `{f}`")
            if len(files) > 30:
                lines.append(f"_...и ещё {len(files)-30}_")
            return AgentResult(
                success=True,
                content="\n".join(lines),
                agent_name=self.agent_name, needs_critic=False,
            )
        except Exception as e:
            return AgentResult(
                success=False,
                content=f"⚠️ Ошибка: {e}",
                agent_name=self.agent_name, needs_critic=False,
            )
