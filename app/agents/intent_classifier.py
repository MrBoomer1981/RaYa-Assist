from dataclasses import dataclass
from langchain_core.messages import SystemMessage, HumanMessage


@dataclass
class IntentResult:
    intent: str
    confidence: float


class IntentClassifier:

    def __init__(self, llm):
        self._llm = llm

    async def classify(self, message: str) -> IntentResult:

        prompt = """
Ты классификатор намерений пользователя.

Верни JSON:

{
 "intent": "...",
 "confidence": 0-1
}

Возможные intent:

conversation
programming
image_generation
science_question
diary_entry
reminder
general_question
"""

        response = await self._llm.ainvoke([
            SystemMessage(content=prompt),
            HumanMessage(content=message)
        ])

        text = str(response.content)

        import json
        data = json.loads(text)

        return IntentResult(
            intent=data["intent"],
            confidence=float(data["confidence"])
        )
