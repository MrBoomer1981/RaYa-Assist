"""
Stub module — kept for import compatibility.
Search is handled by SQLite FTS5 in knowledge_base.py.
"""

class EmbeddingService:
    """No-op stub. FTS5 handles all search."""

    def __init__(self, groq_api_key: str, index_path: str) -> None:
        pass

    async def add_research(self, research_id: int, text: str) -> None:
        pass

    def remove_research(self, research_id: int) -> None:
        pass

    def save(self) -> None:
        pass
