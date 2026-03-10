"""
proactive_service.py — проактивные сообщения от RaYa.

Что делает:
- 08:00 МСК (05:00 UTC) — утренний дайджест
- Каждые 30 мин — проверяет не пора ли написать первой (4ч тишины)
"""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from app.config import settings

logger = logging.getLogger(__name__)

_MOSCOW_UTC_OFFSET  = 3
_DIGEST_HOUR_MSK    = 6      # 06:45 МСК
_DIGEST_MINUTE_MSK  = 45
_SILENCE_HOURS      = 4      # через сколько часов писать первой
_CHECK_INTERVAL_SEC = 60     # проверка каждую минуту


class ProactiveService:

    def __init__(self, bot: Bot, llm_service) -> None:
        self._bot  = bot
        self._llm  = llm_service
        self._task: asyncio.Task | None = None

        self._digest_sent_date: str  = ""
        self._last_initiative:  datetime | None = None  # когда последний раз писали первой

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info(
            "🌅 Проактивный сервис запущен | дайджест %02d:%02d МСК | тишина %dч",
            _DIGEST_HOUR_MSK, _DIGEST_MINUTE_MSK, _SILENCE_HOURS,
        )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в proactive_service")
            await asyncio.sleep(_CHECK_INTERVAL_SEC)

    async def _tick(self) -> None:
        now_utc = datetime.utcnow()

        # Всё время считаем в МСК
        msk_total_minutes = now_utc.hour * 60 + now_utc.minute + _MOSCOW_UTC_OFFSET * 60
        msk_hour   = (msk_total_minutes // 60) % 24
        msk_minute = msk_total_minutes % 60

        # today по МСК — иначе в 21:00-23:59 МСК дата UTC уже другая
        msk_date = (now_utc + timedelta(hours=_MOSCOW_UTC_OFFSET)).strftime("%Y-%m-%d")

        # ── Утренний дайджест — строго 6:45 МСК, 1 раз в день ──────────────
        # Окно ±2 минуты — защита от пропущенного тика под нагрузкой
        digest_target = _DIGEST_HOUR_MSK * 60 + _DIGEST_MINUTE_MSK
        current_msk   = msk_hour * 60 + msk_minute
        in_window     = abs(current_msk - digest_target) <= 2

        if in_window and self._digest_sent_date != msk_date:
            self._digest_sent_date = msk_date
            await self._send_morning_digest()
            return  # не проверяем тишину в момент дайджеста

        # Переопределяем now_msk_hour для остальной логики
        now_msk_hour = msk_hour

        # ── Инициативное сообщение при тишине ────────────────────────────────
        # Не пишем ночью (23:00 - 08:00 МСК)
        if not (8 <= now_msk_hour < 23):
            return

        # Не пишем чаще чем раз в _SILENCE_HOURS
        if self._last_initiative:
            since_initiative = (now_utc - self._last_initiative).total_seconds() / 3600
            if since_initiative < _SILENCE_HOURS:
                return

        await self._check_silence(now_utc)

    async def _check_silence(self, now_utc: datetime) -> None:
        """Проверяет тишину и пишет первой если надо."""
        try:
            from app.emotional_service import get_last_message_time, generate_initiative_message

            user_id   = settings.telegram_user_id
            last_msg  = get_last_message_time(user_id)

            if last_msg is None:
                return  # нет сообщений вообще — не пишем

            silence_hours = (now_utc - last_msg).total_seconds() / 3600

            if silence_hours >= _SILENCE_HOURS:
                logger.info("🤫 Тишина %.1f ч — RaYa пишет первой", silence_hours)

                # Берём лёгкую модель для инициативы
                llm = self._llm._llm

                text = await generate_initiative_message(user_id, llm)

                if text:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                    )
                    self._last_initiative = now_utc
                    logger.info("✅ Инициативное сообщение отправлено")

        except Exception:
            logger.exception("Ошибка проверки тишины")

    async def _send_morning_digest(self) -> None:
        """Генерирует и отправляет утренний дайджест."""
        try:
            logger.info("🌅 Генерируем утренний дайджест...")
            user_id = settings.telegram_user_id

            from app.agents.morning_agent import MorningAgent
            from app.agents.base_agent import AgentContext
            from app.database import load_history, load_memory

            agent = MorningAgent()
            ctx   = AgentContext(
                user_id=user_id,
                message="утренний дайджест",
                history=load_history(user_id, limit=5),
                memory_facts=load_memory(user_id),
                search_results="",
            )

            result = await agent.run(ctx)

            if result.success and result.content:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"🌅 *Доброе утро, Сократ*\n\n{result.content}",
                    parse_mode="Markdown",
                )
                from app.database import save_messages
                save_messages(user_id, "[утренний дайджест]", result.content)
                logger.info("✅ Утренний дайджест отправлен")

        except Exception:
            logger.exception("Ошибка отправки дайджеста")
