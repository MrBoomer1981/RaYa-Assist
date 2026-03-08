"""
image_agent.py — агент генерации изображений.
Использует Hugging Face Inference API (FLUX) — бесплатно.
HF_TOKEN добавляется в Railway Variables когда будем подключать.
"""
import logging
import os
from typing import Optional

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings

logger = logging.getLogger(__name__)

# Модель для генерации — FLUX.1-schnell быстрая и бесплатная
_HF_MODEL = "black-forest-labs/FLUX.1-schnell"
_HF_API_URL = f"https://router.huggingface.co/hf-inference/models/{_HF_MODEL}"

_SYSTEM = """\
Ты агент генерации изображений в команде RaYa.
Твоя задача — улучшить промпт пользователя для генерации изображения.

Правила:
- Расширяй описание деталями стиля, освещения, композиции
- Переводи на английский (FLUX лучше работает с английским)
- Добавляй технические теги качества: high quality, detailed, 8k
- Убирай неподходящий контент

Верни ТОЛЬКО улучшенный промпт на английском. Без пояснений."""


class ImageAgent(BaseAgent):
    agent_name = "image"
    timeout = 60  # генерация может занимать время

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        hf_token = os.getenv("HF_TOKEN", "")

        # Шаг 1: улучшаем промпт через LLM
        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        enhanced_prompt = str(response.content).strip()

        logger.info("🎨 Промпт для генерации: %s", enhanced_prompt[:100])

        # Шаг 2: генерируем изображение если есть токен
        if not hf_token:
            # Токена нет — возвращаем промпт и инструкцию
            return AgentResult(
                success=True,
                content=(
                    f"🎨 Подготовил промпт для генерации:\n\n"
                    f"{enhanced_prompt}\n\n"
                    f"_(Для автоматической генерации добавь HF_TOKEN в настройки)_"
                ),
                agent_name=self.agent_name,
                metadata={"prompt": enhanced_prompt, "generated": False},
            )

        # Шаг 3: запрос к Hugging Face API
        image_bytes = await _generate_image(enhanced_prompt, hf_token)
        if image_bytes:
            return AgentResult(
                success=True,
                content=enhanced_prompt,  # текст — main.py отправит картинку отдельно
                agent_name=self.agent_name,
                metadata={
                    "prompt": enhanced_prompt,
                    "generated": True,
                    "image_bytes": image_bytes,
                },
            )

        return AgentResult(
            success=False,
            content=(
                f"⚠️ Не удалось сгенерировать изображение.\n"
                f"Промпт был: {enhanced_prompt}"
            ),
            agent_name=self.agent_name,
            error="HF API вернул пустой результат",
        )


async def _generate_image(prompt: str, token: str) -> Optional[bytes]:
    """Запрос к Hugging Face Inference API."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=55.0) as client:
            response = await client.post(
                _HF_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"inputs": prompt},
            )
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "image" in content_type:
                    return response.content
            logger.warning(
                "HF API статус: %d | %s",
                response.status_code, response.text[:200],
            )
            return None
    except Exception:
        logger.exception("Ошибка запроса к HF API")
        return None
