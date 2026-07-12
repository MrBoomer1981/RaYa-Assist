"""
app/services/memory — трёхслойная память (Hermes 3 / MemGPT архитектура).

  core     — Core Memory:    ключевые факты, всегда в контексте (~400 токенов)
  recall   — Recall Memory:  эпизоды разговоров, BM25 + LLM rerank
  archival — Archival Memory: DEEper KB, безлимитный архив
  manager  — MemoryManager:  оркестратор всех трёх слоёв

Точка входа: manager.MemoryManager
"""
from app.services.memory.manager import MemoryManager

__all__ = ["MemoryManager"]
