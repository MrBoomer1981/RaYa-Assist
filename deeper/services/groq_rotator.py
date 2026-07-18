"""
Groq API key rotator.
Automatically switches to the next key on 429 rate limit errors.
Supports unlimited number of keys via GROQ_API_KEY_1, GROQ_API_KEY_2, ... env vars.

ВАЖНО: лимиты Groq действуют на уровень ОРГАНИЗАЦИИ (в т.ч. для одной и той
же модели), а не на отдельный API-ключ — несколько ключей одной организации
делят один и тот же TPM/RPM бюджет (см. https://console.groq.com/docs/rate-limits).
Поэтому одна только ротация ключей не спасает от 429 — нужно реально ждать
подсказанное сервером время (Retry-After / "Please try again in Xs"),
иначе все ключи "исчерпываются" почти мгновенно и запрос просто падает.
"""
import os
import re
import asyncio
from typing import List, Optional

from groq import Groq
from deeper.utils.logger import get_logger

logger = get_logger("groq_rotator")

# "Please try again in 10.08s" — Groq всегда указывает точное время ожидания
# в тексте 429-ошибки. Парсим его вместо угадывания.
_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)s", re.IGNORECASE)
# TPM-лимиты у Groq сбрасываются в пределах минуты — было 20с, но под
# сильной нагрузкой (несколько параллельных исследований + основной бот
# на том же fast_model) подсказанное время ожидания может быть близко
# к полной минуте. Раньше при wait=45с мы всё равно ждали только 20с и
# retry'или заведомо рано, впустую тратя попытку. 60с — полное окно.
_MAX_WAIT_SEC = 60.0  # не ждём дольше этого за одну попытку


class GroqKeyRotator:
    """
    Round-robin key rotator with 429-aware failover.
    
    Loads keys from environment:
      GROQ_API_KEY      — primary key (always required)
      GROQ_API_KEY_2    — second key (optional)
      GROQ_API_KEY_3    — third key (optional)
      GROQ_API_KEY_N    — any number of additional keys
    """

    def __init__(self) -> None:
        self.keys = self._load_keys()
        self.clients = [Groq(api_key=k) for k in self.keys]
        self._index = 0
        self._lock = asyncio.Lock()
        logger.info("GroqKeyRotator initialized with {} key(s)", len(self.keys))

    @staticmethod
    def _load_keys() -> List[str]:
        keys = []
        # Primary key
        primary = os.getenv("GROQ_API_KEY")
        if primary:
            keys.append(primary)
        # Additional keys: GROQ_API_KEY_2, GROQ_API_KEY_3, ...
        i = 2
        while True:
            key = os.getenv(f"GROQ_API_KEY_{i}")
            if not key:
                break
            keys.append(key)
            i += 1
        if not keys:
            raise ValueError("No GROQ_API_KEY found in environment")
        return keys

    def current_client(self) -> Groq:
        return self.clients[self._index]

    async def _rotate(self) -> None:
        async with self._lock:
            prev = self._index
            self._index = (self._index + 1) % len(self.clients)
            logger.info("Rotated Groq key: {} → {}", prev + 1, self._index + 1)

    @staticmethod
    def _parse_wait_seconds(error_text: str, attempt: int) -> float:
        """
        Groq сообщает точное время ожидания в тексте 429-ошибки
        ("Please try again in 10.08s"). Используем его напрямую —
        это надёжнее любого угадывания. Если почему-то не нашли —
        экспоненциальный backoff как запасной вариант.
        """
        match = _RETRY_AFTER_RE.search(error_text)
        if match:
            try:
                return min(float(match.group(1)) + 0.5, _MAX_WAIT_SEC)  # +буфер
            except ValueError:
                pass
        return min(2.0 ** attempt, _MAX_WAIT_SEC)

    async def chat(
        self,
        model: str,
        messages: list,
        max_tokens: int = 1000,
        temperature: float = 0.4,
        retries: Optional[int] = None,
    ) -> str:
        """
        Call Groq chat completion with automatic key rotation on 429.

        Лимиты Groq — на уровень организации, так что ротация ключей сама
        по себе от 429 не спасает (см. модуль-докстринг). Поэтому при
        рейт-лимите ждём подсказанное сервером время и только потом
        пробуем следующий ключ — это медленнее, чем было, но реально
        доходит до успешного ответа вместо мгновенного провала.
        """
        if retries is None:
            # Раньше было len(self.clients) (обычно 3) — этого хватало на
            # рывок по всем ключам за ~0.3с без единой реальной паузы,
            # после чего запрос просто падал. Теперь попытки дороже
            # (реальное ожидание), поэтому и бюджет должен быть больше.
            retries = max(len(self.clients), 5)

        loop = asyncio.get_event_loop()
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            client = self.current_client()
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda c=client: c.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    ),
                )
                return response.choices[0].message.content.strip()

            except Exception as e:
                last_error = e
                if "429" in str(e):
                    wait = self._parse_wait_seconds(str(e), attempt)
                    logger.warning(
                        "Rate limit on key {} (attempt {}/{}), waiting {:.1f}s...",
                        self._index + 1, attempt + 1, retries, wait,
                    )
                    await asyncio.sleep(wait)
                    # Ротируем и после ожидания — вдруг ключи всё же из
                    # разных организаций/тарифов, тогда это реально поможет.
                    await self._rotate()
                else:
                    # Non-rate-limit error — don't rotate, just raise
                    raise

        raise RuntimeError(
            f"All {len(self.clients)} Groq keys exhausted after {retries} attempts. "
            f"Last error: {last_error}"
        )
