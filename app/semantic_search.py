"""
semantic_search.py — семантический поиск по Obsidian vault.

Бекенд: Groq embeddings (nomic-embed-text-v1_5), 768 измерений.
Хранилище: JSON файл с векторами на Railway Volume.
Стратегия: lazy build — индексируем при первом поиске, обновляем инкрементально.

Использование:
    results = await semantic_search("прокрастинация и мотивация", top_k=5)
    # → [{"path": "Zettelkasten/...", "title": "...", "score": 0.87, "snippet": "..."}]
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
_MODEL        = "nomic-embed-text-v1_5"
_GROQ_URL     = "https://api.groq.com/openai/v1/embeddings"
_DIM          = 768
_INDEX_FILE   = Path(os.getenv("DB_PATH", "/data/database.db")).parent / "vault_index.json"
_INDEX_TTL    = 3600        # пересобираем индекс каждый час
_BATCH_SIZE   = 20          # файлов за один API запрос
_SEARCH_DIRS  = ["Zettelkasten", "Заметки", "Планы"]  # что индексируем


# ── Embedding API ──────────────────────────────────────────────────────────────

async def _embed(texts: list[str]) -> list[list[float]]:
    """Получает embeddings через Groq API. Возвращает список векторов."""
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY не задан")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": _MODEL, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        # Сортируем по index на случай если API вернул не по порядку
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]


async def _embed_one(text: str) -> list[float]:
    vecs = await _embed([text[:2000]])
    return vecs[0]


# ── Индекс ────────────────────────────────────────────────────────────────────

def _load_index() -> dict:
    """Загружает индекс с диска. Возвращает {path: {title, snippet, vector, mtime}}."""
    if _INDEX_FILE.exists():
        try:
            return json.loads(_INDEX_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_index(index: dict) -> None:
    _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    _INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False))


def _extract_text(content: str) -> tuple[str, str, str]:
    """Возвращает (title, snippet, index_text) из markdown файла."""
    import re
    # Убираем frontmatter
    clean = re.sub(r"^---.*?---\n+", "", content, flags=re.DOTALL).strip()
    lines = clean.splitlines()

    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    if not title:
        title = lines[0][:60] if lines else "без названия"

    # Сниппет — первые 200 символов без заголовка и тегов
    body_lines = [l for l in lines if l and not l.startswith("#")
                  and not l.startswith("- ") and l != "---"]
    snippet = " ".join(body_lines[:3])[:200]

    # Текст для индексации — заголовок + тело (до 1500 символов)
    index_text = f"{title}. {' '.join(body_lines[:15])}"[:1500]

    return title, snippet, index_text


async def build_index(force: bool = False) -> dict:
    """
    Строит или обновляет индекс векторов.
    Инкрементальный — индексирует только изменённые файлы.
    """
    from app.integrations.obsidian import VAULT_PATH, vault_available
    if not vault_available():
        return {}

    index = {} if force else _load_index()
    vault = VAULT_PATH()
    new_entries: list[tuple[str, str, str, str]] = []  # (rel_path, title, snippet, text)

    # Собираем файлы для индексации
    for folder in _SEARCH_DIRS:
        dir_path = vault / folder
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.rglob("*.md")):
            rel  = str(f.relative_to(vault))
            mtime = str(f.stat().st_mtime)
            # Пропускаем если не изменился
            if rel in index and index[rel].get("mtime") == mtime:
                continue
            try:
                content = f.read_text(encoding="utf-8")
                title, snippet, idx_text = _extract_text(content)
                new_entries.append((rel, title, snippet, idx_text, mtime))
            except Exception:
                pass

    if not new_entries:
        logger.debug("semantic_search: индекс актуален (%d записей)", len(index))
        return index

    logger.info("semantic_search: индексируем %d файлов...", len(new_entries))

    # Батчевое получение embeddings
    for i in range(0, len(new_entries), _BATCH_SIZE):
        batch = new_entries[i:i + _BATCH_SIZE]
        texts = [e[3] for e in batch]
        try:
            vectors = await _embed(texts)
            for entry, vec in zip(batch, vectors):
                rel, title, snippet, _, mtime = entry
                index[rel] = {
                    "title":   title,
                    "snippet": snippet,
                    "vector":  vec,
                    "mtime":   mtime,
                }
        except Exception as e:
            logger.warning("semantic_search: ошибка батча %d: %s", i, e)
            await asyncio.sleep(2)  # rate limit back-off

    _save_index(index)
    logger.info("semantic_search: индекс обновлён (%d записей)", len(index))
    return index


# ── Кэш индекса в памяти ──────────────────────────────────────────────────────
_cache: dict     = {}
_cache_ts: float = 0.0
_build_lock      = asyncio.Lock()


async def _get_index() -> dict:
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _INDEX_TTL:
        return _cache
    async with _build_lock:
        # Double-check после захвата лока
        if _cache and (time.monotonic() - _cache_ts) < _INDEX_TTL:
            return _cache
        _cache    = await build_index()
        _cache_ts = time.monotonic()
    return _cache


# ── Поиск ─────────────────────────────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


async def semantic_search(query: str, top_k: int = 5,
                           folder: Optional[str] = None) -> list[dict]:
    """
    Семантический поиск по vault.
    Возвращает [{path, title, score, snippet}] отсортированный по релевантности.
    """
    if not query.strip():
        return []

    try:
        index = await _get_index()
        if not index:
            return []

        query_vec = await _embed_one(query)

        results = []
        for rel_path, entry in index.items():
            # Фильтр по папке если задан
            if folder and not rel_path.startswith(folder):
                continue
            score = _cosine_similarity(query_vec, entry["vector"])
            if score > 0.3:  # порог релевантности
                results.append({
                    "path":    rel_path,
                    "title":   entry["title"],
                    "score":   round(score, 3),
                    "snippet": entry["snippet"],
                })

        results.sort(key=lambda x: -x["score"])
        return results[:top_k]

    except Exception:
        logger.exception("semantic_search: ошибка поиска '%s'", query[:60])
        return []


async def find_related(path: str, top_k: int = 3) -> list[dict]:
    """Находит заметки похожие на данную (для графа знаний)."""
    try:
        index = await _get_index()
        entry = index.get(path)
        if not entry:
            return []
        results = []
        for rel, e in index.items():
            if rel == path:
                continue
            score = _cosine_similarity(entry["vector"], e["vector"])
            if score > 0.4:
                results.append({"path": rel, "title": e["title"], "score": round(score, 3)})
        results.sort(key=lambda x: -x["score"])
        return results[:top_k]
    except Exception:
        logger.exception("find_related: ошибка")
        return []


async def invalidate_cache() -> None:
    """Сбрасывает кэш — вызывать после добавления новых заметок."""
    global _cache, _cache_ts
    _cache    = {}
    _cache_ts = 0.0
