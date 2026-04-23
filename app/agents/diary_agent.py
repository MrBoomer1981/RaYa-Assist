"""
diary_agent.py — личный дневник пользователя.

Умеет:
- Записать мысли/события/ощущения в дневник
- Показать последние записи
- Дать рефлексию по записям (паттерны, настроение, инсайты)
- Автоматически определяет настроение и сохраняет в mood_log

Запись: LLM помогает оформить мысль → save_diary_entry + save_mood
Чтение: load_diary_entries → LLM делает анализ или просто показывает
"""
import logging
import re
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import load_diary_entries, save_diary_entry, save_mood

logger = logging.getLogger(__name__)

_MOODS = ["радость", "грусть", "тревога", "злость", "спокойствие",
          "усталость", "вдохновение", "скука", "гордость", "нейтрально"]

_SYSTEM_WRITE = """\
Ты RaYa — личный ассистент и доверенный собеседник. Пользователь делится мыслями, \
переживаниями или событиями дня — ты помогаешь это зафиксировать в дневнике.

Твоя задача:
1. Ответить живо и по-человечески — не как шаблон, а как человек которому интересно
2. В конце ответа добавить XML-тег с записью для дневника и настроением:

<diary_entry>текст записи — оформленный, от первого лица, 2-5 предложений</diary_entry>
<diary_mood>одно слово из списка: радость|грусть|тревога|злость|спокойствие|усталость|вдохновение|скука|гордость|нейтрально</diary_mood>

Запись в дневнике — это то что пользователь сказал, оформленное чисто и по делу. \
Не добавляй от себя то чего не было сказано. Тон — нейтральный, от первого лица.

Не занудствуй. Не задавай больше одного вопроса. Обращайся по имени.\
"""

_SYSTEM_READ = """\
Ты RaYa — личный ассистент. Показываешь записи из дневника пользователя и при запросе \
делаешь рефлексию: паттерны, настроение, что повторяется, что изменилось.

Будь честной и конкретной. Не банальность, а наблюдение. Обращайся по имени.\
"""

# Фразы-триггеры для записи в дневник
_WRITE_TRIGGERS = re.compile(
    r"\b(запиши|запиши в дневник|хочу записать|добавь в дневник|"
    r"сохрани мысль|зафиксируй|в дневник|дневниковая запись|"
    r"сегодня я|сегодня было|сегодня думал|сегодня чувствую|"
    r"хочу поделиться|расскажу тебе|хочу сказать что)\b",
    re.IGNORECASE,
)

_READ_TRIGGERS = re.compile(
    r"\b(покажи дневник|мои записи|что я писал|что я записывал|"
    r"записи за|прочитай дневник|открой дневник|история записей|"
    r"рефлексия|проанализируй записи|что ты заметила)\b",
    re.IGNORECASE,
)


class DiaryAgent(BaseAgent):
    agent_name = "diary"
    timeout    = 35

    def _system_prompt(self) -> str:
        return _SYSTEM_WRITE

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        msg = ctx.message.lower()
        is_read = bool(_READ_TRIGGERS.search(msg))

        if is_read:
            return await self._handle_read(ctx)
        else:
            return await self._handle_write(ctx)

    # ── Запись ─────────────────────────────────────────────────────────────────

    async def _handle_write(self, ctx: AgentContext) -> AgentResult:
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

        messages = [
            SystemMessage(content=_SYSTEM_WRITE),
            *ctx.history[-4:],          # последние 4 сообщения для контекста
            HumanMessage(content=ctx.message),
        ]
        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        # Извлекаем запись и настроение из тегов
        entry_match = re.search(r"<diary_entry>(.*?)</diary_entry>", raw, re.DOTALL)
        mood_match  = re.search(r"<diary_mood>(.*?)</diary_mood>",   raw, re.DOTALL)

        entry_text = entry_match.group(1).strip() if entry_match else ctx.message.strip()
        mood       = mood_match.group(1).strip().lower() if mood_match else "нейтрально"

        if mood not in _MOODS:
            mood = "нейтрально"

        # Сохраняем в БД
        full_entry = f"[{now_str}] {entry_text}"
        try:
            entry_id = save_diary_entry(ctx.user_id, full_entry, mood)
            save_mood(ctx.user_id, mood, ctx.message[:100])
            logger.info("📓 DiaryAgent: запись #%d, настроение='%s' | user_id=%s",
                        entry_id, mood, ctx.user_id)
        except Exception as e:
            logger.warning("diary: ошибка сохранения: %s", e)

        # Убираем служебные теги из ответа пользователю
        reply = re.sub(r"<diary_(entry|mood)>.*?</diary_(entry|mood)>", "",
                       raw, flags=re.DOTALL).strip()
        reply = reply or "Записала."

        return AgentResult(
            success=True, content=reply,
            agent_name=self.agent_name, needs_critic=False,
            metadata={"mood": mood},
        )

    # ── Чтение и рефлексия ─────────────────────────────────────────────────────

    async def _handle_read(self, ctx: AgentContext) -> AgentResult:
        msg = ctx.message.lower()

        # Определяем сколько записей показывать
        limit = 10 if any(kw in msg for kw in ("рефлексия", "проанализируй", "паттерн", "что заметила")) else 5

        entries = load_diary_entries(ctx.user_id, limit=limit)

        if not entries:
            return AgentResult(
                success=True,
                content="Дневник пока пустой. Поделись чем-нибудь — запишу.",
                agent_name=self.agent_name, needs_critic=False,
            )

        # Быстрый показ без рефлексии
        wants_reflection = any(kw in msg for kw in
                               ("рефлексия", "проанализируй", "что заметила",
                                "паттерн", "анализ", "что изменилось"))

        if not wants_reflection:
            lines = ["**📓 Последние записи:**\n"]
            for created_at, entry in entries:
                # Убираем временную метку из начала если есть
                clean = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*", "", entry)
                date  = created_at[:10] if created_at else ""
                lines.append(f"_{date}_\n{clean}\n")
            return AgentResult(
                success=True, content="\n".join(lines),
                agent_name=self.agent_name, needs_critic=False,
            )

        # Рефлексия через LLM
        entries_text = "\n\n".join(
            f"[{created_at[:10]}] {entry}" for created_at, entry in entries
        )
        messages = [
            SystemMessage(content=_SYSTEM_READ),
            HumanMessage(content=(
                f"Вот записи из дневника пользователя за последнее время:\n\n"
                f"{entries_text}\n\n"
                f"Запрос пользователя: {ctx.message}\n\n"
                "Сделай честный анализ: что повторяется, как меняется настроение, "
                "что бросается в глаза. Конкретно, без банальностей."
            )),
        ]
        response = await self._llm.ainvoke(messages)
        reply = str(response.content).strip()

        return AgentResult(
            success=True, content=reply,
            agent_name=self.agent_name, needs_critic=False,
        )
