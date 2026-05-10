"""
Groq API key rotator.
Automatically switches to the next key on 429 rate limit errors.
Supports unlimited number of keys via GROQ_API_KEY_1, GROQ_API_KEY_2, ... env vars.
"""
import os
import asyncio
from typing import List, Optional

from groq import Groq
from deeper.utils.logger import get_logger

logger = get_logger("groq_rotator")


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
        Tries all available keys before giving up.
        """
        if retries is None:
            retries = len(self.clients)

        loop = asyncio.get_event_loop()
        last_error = None

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
                    logger.warning(
                        "Rate limit on key {} (attempt {}/{}), rotating...",
                        self._index + 1, attempt + 1, retries
                    )
                    await self._rotate()
                    # Small yield to let event loop breathe
                    await asyncio.sleep(0.1)
                else:
                    # Non-rate-limit error — don't rotate, just raise
                    raise

        raise RuntimeError(
            f"All {len(self.clients)} Groq keys exhausted. Last error: {last_error}"
        )
