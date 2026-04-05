"""
obsidian.py — работа с Obsidian vault.

Структура:
  Zettelkasten/YYYYMMDDHHMMSS.md  ← граф знаний
  Дневник/YYYY-MM/YYYY-MM-DD.md   ← один файл на день, НЕ в графе
  Заметки/YYYY-MM-DD HH-MM.md     ← структурированные заметки
  Планы/Краткосрочные/            ← ≤ 2 недели
  Планы/Долгосрочные/             ← > 2 недели
  Задачи/Q1.md Q2.md Q3.md Q4.md  ← матрица Эйзенхауэра
"""
import logging
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

# Per-file locks — защита от race condition при параллельных запросах
_file_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()

def _get_file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_mutex:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]

logger = logging.getLogger(__name__)

QUADRANTS = {
    "q1": {"file": "Задачи/Q1.md", "title": "🔴 Q1 — Срочно и важно",     "emoji": "🔴"},
    "q2": {"file": "Задачи/Q2.md", "title": "🟡 Q2 — Важно, не срочно",   "emoji": "🟡"},
    "q3": {"file": "Задачи/Q3.md", "title": "🟠 Q3 — Срочно, не важно",   "emoji": "🟠"},
    "q4": {"file": "Задачи/Q4.md", "title": "⚪ Q4 — Не срочно, не важно", "emoji": "⚪"},
}

PLAN_FOLDERS = {
    "short": "Планы/Краткосрочные",
    "long":  "Планы/Долгосрочные",
}


def VAULT_PATH() -> Path:
    base   = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
    subdir = os.getenv("OBSIDIAN_VAULT_SUBDIR", "RaYa-Vault")
    return base / subdir if subdir else base


def vault_available() -> bool:
    return VAULT_PATH().exists()


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _slug(text: str, max_len: int = 50) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "", text).strip()
    return re.sub(r"\s+", " ", text)[:max_len].strip()


def _zettel_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S")


def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


def _frontmatter(tags: list, extra: dict | None = None) -> str:
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    lines = ["---", f"created: {_now_str()} UTC", "tags:", tag_lines]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _write(rel_path: Path, content: str) -> Path:
    """Атомарная запись через tmp файл + per-file lock."""
    full = VAULT_PATH() / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp  = full.with_suffix(full.suffix + ".tmp")
    lock = _get_file_lock(full)
    with lock:
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(full)   # атомарная замена на POSIX
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
    return full


def _read(rel_path: Path) -> str | None:
    full = VAULT_PATH() / rel_path
    return full.read_text(encoding="utf-8") if full.exists() else None


# ── Поиск — улучшенный ────────────────────────────────────────────────────────

def search_vault(query: str, folder: str = "") -> list:
    """
    Полнотекстовый поиск с релевантностью.
    Приоритет: совпадение в заголовке > тегах > тексте.
    Поддерживает поиск по тегам через #тег.
    """
    root     = VAULT_PATH() / folder if folder else VAULT_PATH()
    q        = query.lower().strip()
    is_tag   = q.startswith("#")
    tag_q    = q.lstrip("#") if is_tag else ""
    found    = []

    for f in sorted(root.rglob("*.md")):
        # Пропускаем файлы задач при общем поиске (они имеют особый формат)
        if "Задачи/" in str(f) and not folder:
            continue
        try:
            text  = f.read_text(encoding="utf-8")
            tl    = text.lower()
            score = 0
            snippet = ""
            title   = f.stem

            # Заголовок
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break

            if is_tag:
                # Поиск по тегу
                tags = re.findall(r"^  - (.+)$", text, re.MULTILINE)
                if tag_q not in [t.lower() for t in tags]:
                    continue
                score = 10
                snippet = f"теги: {', '.join(tags[:5])}"
            else:
                # Обычный поиск
                if q not in tl:
                    continue
                # Релевантность
                if q in title.lower():
                    score += 5
                tags = re.findall(r"^  - (.+)$", text, re.MULTILINE)
                if any(q in t.lower() for t in tags):
                    score += 3
                score += tl.count(q)  # частота

                # Сниппет — первая строка с совпадением не из frontmatter
                in_fm = False
                for line in text.splitlines():
                    if line == "---":
                        in_fm = not in_fm
                        continue
                    if not in_fm and q in line.lower() and line.strip():
                        snippet = line.strip()[:200]
                        break

            found.append({
                "path":    str(f.relative_to(VAULT_PATH())),
                "title":   title,
                "snippet": snippet,
                "score":   score,
            })
        except Exception:
            pass

    # Сортируем по релевантности
    found.sort(key=lambda x: -x["score"])
    return found[:10]


def search_tasks(status: str = "active") -> list:
    """
    Поиск задач по статусу.
    status: 'active' | 'done' | 'all'
    """
    result = []
    for q_key, q in QUADRANTS.items():
        content = _read(Path(q["file"])) or ""
        for line in content.splitlines():
            m = re.match(r"^- \[([ xX])\] (.+)$", line)
            if not m:
                continue
            done = m.group(1).lower() == "x"
            if status == "active" and done:
                continue
            if status == "done" and not done:
                continue
            result.append({
                "quadrant": q_key,
                "emoji":    q["emoji"],
                "title":    q["title"],
                "text":     m.group(2).strip(),
                "done":     done,
            })
    return result


def find_task_fuzzy(query: str) -> str | None:
    """
    Нечёткий поиск задачи по тексту.
    Возвращает точный текст задачи из файла или None.
    """
    q = query.lower().strip()
    best_match = None
    best_score = 0

    for task in search_tasks("active"):
        text  = task["text"].lower()
        # Точное совпадение
        if q == text:
            return task["text"]
        # Вхождение
        if q in text or text in q:
            score = len(q) / max(len(text), 1)
            if score > best_score:
                best_score = score
                best_match = task["text"]
        # Пересечение слов
        q_words = set(q.split())
        t_words = set(text.split())
        overlap = len(q_words & t_words) / max(len(q_words), 1)
        if overlap > 0.6 and overlap > best_score:
            best_score = overlap
            best_match = task["text"]

    return best_match if best_score > 0.4 else None


def read_note(query: str) -> str | None:
    for folder in ("Заметки", "Zettelkasten", "Дневник", "Планы", "Задачи", ""):
        p = Path(folder) / query if folder else Path(query)
        if not str(p).endswith(".md"):
            p = Path(str(p) + ".md")
        content = _read(p)
        if content:
            return content
    results = search_vault(query)
    return _read(Path(results[0]["path"])) if results else None


def list_files(folder: str = "Заметки") -> list:
    root = VAULT_PATH() / folder
    if not root.exists():
        return []
    return [str(f.relative_to(VAULT_PATH())) for f in sorted(root.rglob("*.md"))]


# ── Zettelkasten ──────────────────────────────────────────────────────────────

def list_zettel_titles() -> list[dict]:
    """Возвращает [{id, title, tags, keywords}] всех карточек."""
    root   = VAULT_PATH() / "Zettelkasten"
    result = []
    if not root.exists():
        return result
    for f in sorted(root.rglob("*.md")):
        try:
            text  = f.read_text(encoding="utf-8")
            title = f.stem
            tags  = re.findall(r"^  - (.+)$", text, re.MULTILINE)
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            # Ключевые слова из текста (первый абзац после заголовка)
            body_lines = [l for l in text.splitlines()
                          if l.strip() and not l.startswith("#")
                          and not l.startswith("-") and not l.startswith("---")]
            keywords = " ".join(body_lines[:3])[:200]
            result.append({
                "id":       f.stem,
                "title":    title,
                "tags":     tags,
                "keywords": keywords,
                "path":     str(f.relative_to(VAULT_PATH())),
            })
        except Exception:
            pass
    return result


def zettel_similarity(text: str, entry: dict) -> float:
    """
    Быстрая оценка схожести без LLM.
    Возвращает score 0.0–1.0.
    """
    words = set(w for w in text.lower().split() if len(w) > 3)
    if not words:
        return 0.0

    title_words = set(w for w in entry["title"].lower().split() if len(w) > 3)
    tag_words   = set(w for t in entry["tags"] for w in t.lower().split())
    kw_words    = set(w for w in entry["keywords"].lower().split() if len(w) > 3)
    all_entry   = title_words | tag_words | kw_words

    overlap = len(words & all_entry)
    return overlap / max(len(words), 1)


def update_zettel(zid: str, extra_content: str,
                  new_links: list | None = None) -> str:
    """Дополняет карточку. Обновляет updated: в frontmatter."""
    rel_path = Path(f"Zettelkasten/{zid}.md")
    existing = _read(rel_path)
    if not existing:
        return ""
    # Обновляем updated в frontmatter
    now = _now_str()
    if "updated:" in existing:
        updated = re.sub(r"updated: .+", f"updated: {now} UTC", existing)
    else:
        updated = existing.replace("---\n", f"---\nupdated: {now} UTC\n", 1)

    updated = updated.rstrip() + f"\n\n---\n\n{extra_content}"
    if new_links:
        if "## Связи" in updated:
            for link in new_links:
                if f"[[{link}]]" not in updated:
                    updated = updated.rstrip() + f"\n- [[{link}]]"
        else:
            updated += "\n\n## Связи\n" + "\n".join(f"- [[{l}]]" for l in new_links)
    _write(rel_path, updated + "\n")
    logger.info("🔄 Zettel обновлён: %s", zid)
    return str(rel_path)


def add_zettel(title: str, content: str, tags: list | None = None,
               links: list | None = None) -> str:
    """Создаёт новую Zettelkasten карточку."""
    zid      = _zettel_id()
    tags     = tags or []
    links    = links or []
    rel_path = Path(f"Zettelkasten/{zid}.md")
    fm       = _frontmatter(["zettel"] + tags, {"id": zid, "title": f'"{title}"'})
    links_str = ("\n\n## Связи\n" + "\n".join(f"- [[{l}]]" for l in links)) if links else ""
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}{links_str}\n")
    logger.info("🧠 Zettel создан: %s — '%s'", zid, title[:40])
    return str(rel_path)


# ── Дневник ───────────────────────────────────────────────────────────────────

def write_diary(text: str, dt: datetime | None = None) -> str:
    """Один файл на день. НЕ в графе знаний."""
    dt       = dt or datetime.utcnow()
    rel_path = Path(f"Дневник/{dt.strftime('%Y-%m')}/{dt.strftime('%Y-%m-%d')}.md")
    existing = _read(rel_path)
    time_str = dt.strftime("%H:%M")
    if not existing:
        fm      = _frontmatter(["дневник", dt.strftime("%Y-%m")],
                                {"date": dt.strftime("%Y-%m-%d")})
        content = f"{fm}\n\n# {dt.strftime('%d %B %Y')}\n\n**{time_str} UTC**\n\n{text}\n"
    else:
        content = existing.rstrip() + f"\n\n**{time_str} UTC**\n\n{text}\n"
    _write(rel_path, content)
    logger.info("📔 Дневник: %s", rel_path)
    return str(rel_path)


# ── Заметки ───────────────────────────────────────────────────────────────────

def create_note(title: str, content: str, tags: list | None = None) -> str:
    dt       = datetime.utcnow()
    tags     = tags or ["заметка"]
    rel_path = Path(f"Заметки/{dt.strftime('%Y-%m-%d %H-%M')} {_slug(title)}.md")
    fm       = _frontmatter(["заметка"] + tags)
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}\n")
    logger.info("📝 Заметка: %s", rel_path)
    return str(rel_path)


# ── Планы ─────────────────────────────────────────────────────────────────────

def create_plan(title: str, content: str, horizon: str = "short") -> str:
    dt       = datetime.utcnow()
    folder   = PLAN_FOLDERS.get(horizon, PLAN_FOLDERS["short"])
    rel_path = Path(f"{folder}/{dt.strftime('%Y-%m-%d')} {_slug(title)}.md")
    fm       = _frontmatter(["план", f"план-{horizon}"], {"horizon": horizon})
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}\n")
    logger.info("📅 План [%s]: %s", horizon, rel_path)
    return str(rel_path)


# ── Задачи — матрица Эйзенхауэра ─────────────────────────────────────────────

def _ensure_quadrant_file(q_key: str) -> None:
    q    = QUADRANTS[q_key]
    path = Path(q["file"])
    if not _read(path):
        _write(path, f"# {q['title']}\n\n")


def add_tasks(tasks: list, quadrant: str = "q2") -> str:
    """Добавляет задачи с защитой от дублей."""
    try:
        if quadrant not in QUADRANTS:
            quadrant = "q2"
        _ensure_quadrant_file(quadrant)
        q        = QUADRANTS[quadrant]
        rel_path = Path(q["file"])
        existing = _read(rel_path) or f"# {q['title']}\n\n"

        existing_texts = set()
        for line in existing.splitlines():
            m = re.match(r"^- \[[ xX]\] (.+)$", line)
            if m:
                existing_texts.add(m.group(1).strip().lower())

        new_tasks = [t for t in tasks if t.strip().lower() not in existing_texts]
        if not new_tasks:
            return q["file"]

        new_lines = "\n".join(f"- [ ] {t}" for t in new_tasks)
        _write(rel_path, existing.rstrip() + "\n" + new_lines + "\n")
        logger.info("✅ [%s] +%d задач", quadrant.upper(), len(new_tasks))
        return q["file"]
    except Exception as e:
        logger.exception("add_tasks: ошибка quadrant=%s", quadrant)
        return ""


def get_all_tasks() -> dict:
    result = {}
    for q_key, q in QUADRANTS.items():
        content = _read(Path(q["file"])) or ""
        tasks   = []
        for line in content.splitlines():
            m = re.match(r"^- \[([ xX])\] (.+)$", line)
            if m:
                tasks.append({"text": m.group(2).strip(), "done": m.group(1).lower() == "x"})
        result[q_key] = {"title": q["title"], "emoji": q["emoji"], "tasks": tasks}
    return result


def mark_task_done_obsidian(task_text: str) -> bool:
    """Отмечает задачу выполненной. Использует fuzzy match если точное не найдено."""
    # Сначала точное совпадение
    found = _mark_exact(task_text)
    if found:
        return True
    # Fuzzy match
    actual = find_task_fuzzy(task_text)
    if actual:
        logger.info("vault: fuzzy match '%s' → '%s'", task_text[:40], actual[:40])
        return _mark_exact(actual)
    return False


def _mark_exact(task_text: str) -> bool:
    found = False
    for q in QUADRANTS.values():
        rel     = Path(q["file"])
        content = _read(rel)
        if not content:
            continue
        new = re.sub(
            r"^(- \[) \] (" + re.escape(task_text) + r".*)$",
            r"\1x] \2", content, flags=re.MULTILINE
        )
        if new != content:
            _write(rel, new)
            found = True
    return found


def delete_task_obsidian(task_text: str) -> bool:
    """Удаляет задачу. Fuzzy match если точное не найдено."""
    found = _delete_exact(task_text)
    if found:
        return True
    actual = find_task_fuzzy(task_text)
    if actual:
        return _delete_exact(actual)
    return False


def _delete_exact(task_text: str) -> bool:
    found = False
    for q in QUADRANTS.values():
        rel     = Path(q["file"])
        content = _read(rel)
        if not content:
            continue
        new = re.sub(
            r"^- \[[ xX]\] " + re.escape(task_text) + r".*\n?",
            "", content, flags=re.MULTILINE
        )
        if new != content:
            _write(rel, new)
            found = True
    return found


def clear_done_tasks() -> int:
    """Удаляет все выполненные задачи из всех квадрантов."""
    count = 0
    for q in QUADRANTS.values():
        rel     = Path(q["file"])
        content = _read(rel)
        if not content:
            continue
        lines   = content.splitlines(keepends=True)
        cleaned = [l for l in lines if not re.match(r"^- \[[xX]\] ", l)]
        removed = len(lines) - len(cleaned)
        if removed:
            _write(rel, "".join(cleaned))
            count += removed
    logger.info("🗑️ Удалено %d выполненных задач", count)
    return count


def format_all_tasks() -> str:
    all_tasks = get_all_tasks()
    lines, total = [], 0
    for q_key in ("q1", "q2", "q3", "q4"):
        data   = all_tasks[q_key]
        active = [t for t in data["tasks"] if not t["done"]]
        if not active:
            continue
        lines.append(f"{data['emoji']} **{data['title']}**")
        for t in active:
            lines.append(f"  • {t['text']}")
        lines.append("")
        total += len(active)
    if not lines:
        return "Задач нет. Всё чисто 👍"
    return f"📋 Задачи ({total} активных):\n\n" + "\n".join(lines).strip()


# ── Статистика и утилиты ──────────────────────────────────────────────────────

def vault_stats() -> dict:
    stats = {}
    for folder in ("Дневник", "Заметки", "Zettelkasten", "Планы"):
        root = VAULT_PATH() / folder
        stats[folder] = len(list(root.rglob("*.md"))) if root.exists() else 0
    all_tasks = get_all_tasks()
    for q_key, data in all_tasks.items():
        active = sum(1 for t in data["tasks"] if not t["done"])
        stats[f"{data['emoji']} {data['title']}"] = active
    return stats


def cleanup_vault() -> dict:
    vault = VAULT_PATH()
    if not vault.exists():
        return {"ok": False, "deleted": [], "error": "vault не найден"}
    keep    = {"Дневник", "Заметки", "Задачи", "Zettelkasten", "Планы", ".obsidian"}
    deleted = []
    for item in sorted(vault.iterdir()):
        if item.name not in keep:
            try:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
                deleted.append(item.name)
            except Exception as e:
                logger.warning("cleanup: '%s': %s", item.name, e)
    return {"ok": True, "deleted": deleted}




def move_task(task_text: str, target_quadrant: str) -> bool:
    """Перемещает задачу из одного квадранта в другой."""
    if target_quadrant not in QUADRANTS:
        return False
    # Находим задачу
    actual = find_task_fuzzy(task_text) or task_text
    # Удаляем из текущего квадранта
    found = _delete_exact(actual)
    if not found:
        return False
    # Добавляем в новый квадрант
    add_tasks([actual], quadrant=target_quadrant)
    logger.info("↔️  Задача перемещена в [%s]: '%s'", target_quadrant.upper(), actual[:50])
    return True


def add_task_with_deadline(text: str, quadrant: str = "q1", deadline: str = "") -> str:
    """Добавляет задачу с дедлайном. Дедлайн вписывается прямо в текст."""
    if quadrant not in QUADRANTS:
        quadrant = "q1"
    task_text = f"{text} (до {deadline})" if deadline else text
    add_tasks([task_text], quadrant=quadrant)
    logger.info("✅ [%s] +задача с дедлайном: '%s'", quadrant.upper(), task_text[:50])
    return task_text


def get_tasks_summary() -> str:
    """Возвращает краткую сводку задач для промпта."""
    all_tasks = get_all_tasks()
    parts = []
    for q_key in ("q1", "q2", "q3", "q4"):
        data   = all_tasks[q_key]
        active = [t["text"] for t in data["tasks"] if not t["done"]]
        if active:
            parts.append(f"{data['emoji']} {data['title']}: {len(active)} задач")
    return ", ".join(parts) if parts else "задач нет"


def get_overdue_tasks(days_threshold: int = 0) -> list:
    """
    Возвращает задачи с дедлайном в формате '(до ДАТА)'.
    days_threshold=0 — только просроченные.
    days_threshold=1 — просроченные + на сегодня.
    """
    from datetime import date, timedelta
    import re as _re

    today    = date.today()
    deadline_pattern = _re.compile(r"до (\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)")
    result   = []

    for task in search_tasks("active"):
        m = deadline_pattern.search(task["text"])
        if not m:
            continue
        raw = m.group(1).replace("/", ".").replace("-", ".")
        parts = raw.split(".")
        try:
            if len(parts) == 2:
                d, mo = int(parts[0]), int(parts[1])
                yr    = today.year
            else:
                d, mo, yr = int(parts[0]), int(parts[1]), int(parts[2])
                if yr < 100:
                    yr += 2000
            deadline = date(yr, mo, d)
            if deadline <= today + timedelta(days=days_threshold):
                result.append({**task, "deadline": deadline, "overdue": deadline < today})
        except (ValueError, IndexError):
            pass

    result.sort(key=lambda x: x["deadline"])
    return result


def format_tasks_by_quadrant(quadrant: str) -> str:
    """Форматирует задачи одного квадранта."""
    if quadrant not in QUADRANTS:
        return f"неизвестный квадрант: {quadrant}"
    q       = QUADRANTS[quadrant]
    content = _read(Path(q["file"])) or ""
    tasks   = []
    for line in content.splitlines():
        m = re.match(r"^- \[([ xX])\] (.+)$", line)
        if m:
            tasks.append({"text": m.group(2).strip(), "done": m.group(1).lower() == "x"})
    active = [t for t in tasks if not t["done"]]
    done   = [t for t in tasks if t["done"]]
    if not tasks:
        return f"{q['emoji']} {q['title']} — пусто"
    lines = [f"{q['emoji']} **{q['title']}**"]
    for t in active:
        lines.append(f"  • {t['text']}")
    if done:
        lines.append(f"  ✅ выполнено: {len(done)}")
    return "\n".join(lines)

def batch_add_tasks(task_groups: list[dict]) -> dict:
    """
    Добавляет несколько групп задач за один вызов.
    task_groups: [{"quadrant": "q1", "tasks": ["задача1", "задача2"]}, ...]
    Возвращает {"added": int, "skipped": int}
    """
    added = skipped = 0
    for group in task_groups:
        q     = group.get("quadrant", "q2")
        tasks = [t.strip() for t in group.get("tasks", []) if t.strip()]
        if not tasks:
            continue
        q_data   = QUADRANTS.get(q, QUADRANTS["q2"])
        rel_path = Path(q_data["file"])
        _ensure_quadrant_file(q)
        existing_content = _read(rel_path) or f"# {q_data['title']}\n\n"
        existing_texts   = set()
        for line in existing_content.splitlines():
            m = re.match(r"^- \[[ xX]\] (.+)$", line)
            if m:
                existing_texts.add(m.group(1).strip().lower())
        new_tasks = [t for t in tasks if t.lower() not in existing_texts]
        skipped  += len(tasks) - len(new_tasks)
        if new_tasks:
            lines = "\n".join(f"- [ ] {t}" for t in new_tasks)
            _write(rel_path, existing_content.rstrip() + "\n" + lines + "\n")
            added += len(new_tasks)
    logger.info("batch_add_tasks: +%d added, %d skipped", added, skipped)
    return {"added": added, "skipped": skipped}


def get_week_plan() -> str:
    """
    Возвращает задачи сгруппированные для планирования недели:
    - Q1 + Q2 как приоритеты недели
    - задачи с дедлайном
    """
    all_tasks  = get_all_tasks()
    overdue    = get_overdue_tasks(days_threshold=6)  # вся неделя
    lines      = ["📅 Задачи на неделю:\n"]

    # Критичные — Q1
    q1_tasks = [t["text"] for t in all_tasks["q1"]["tasks"] if not t["done"]]
    if q1_tasks:
        lines.append("🔴 Срочно и важно:")
        for t in q1_tasks:
            lines.append(f"  • {t}")
        lines.append("")

    # С дедлайном на неделю
    if overdue:
        lines.append("⏰ Дедлайны:")
        for t in overdue:
            flag = " 🔴" if t["overdue"] else ""
            lines.append(f"  • {t['text']}{flag}")
        lines.append("")

    # Важные без срока — Q2
    q2_tasks = [t["text"] for t in all_tasks["q2"]["tasks"] if not t["done"]]
    if q2_tasks:
        lines.append("🟡 Важно (нет срока):")
        for t in q2_tasks[:5]:  # топ-5
            lines.append(f"  • {t}")
        if len(q2_tasks) > 5:
            lines.append(f"  ...и ещё {len(q2_tasks)-5}")

    return "\n".join(lines) if len(lines) > 1 else "Задач на неделю нет."


def get_diary_context(days: int = 3) -> str:
    """
    Читает последние N дней дневника для контекста.
    Возвращает краткую выжимку (не весь текст).
    """
    from datetime import timedelta
    now    = datetime.utcnow()
    result = []
    for i in range(days):
        dt       = now - timedelta(days=i)
        rel_path = Path(f"Дневник/{dt.strftime('%Y-%m')}/{dt.strftime('%Y-%m-%d')}.md")
        content  = _read(rel_path)
        if not content:
            continue
        # Убираем frontmatter
        content = re.sub(r"^---.*?---\n+", "", content, flags=re.DOTALL)
        # Берём первые 300 символов как контекст
        preview = content.strip()[:300]
        if preview:
            result.append(f"[{dt.strftime('%d.%m')}] {preview}")
    return "\n".join(result) if result else ""


def mark_multiple_done(texts: list[str]) -> dict:
    """
    Отмечает несколько задач выполненными за один вызов.
    Возвращает {"done": [...], "not_found": [...]}
    """
    done       = []
    not_found  = []
    for text in texts:
        ok = mark_task_done_obsidian(text)
        (done if ok else not_found).append(text)
    logger.info("mark_multiple_done: %d done, %d not found", len(done), len(not_found))
    return {"done": done, "not_found": not_found}


def get_task_count() -> dict:
    """Быстрый подсчёт задач по квадрантам без форматирования."""
    all_tasks = get_all_tasks()
    return {
        q_key: sum(1 for t in data["tasks"] if not t["done"])
        for q_key, data in all_tasks.items()
    }


def undo_task(task_text: str) -> bool:
    """
    Возвращает выполненную задачу в активные — [x] → [ ].
    Fuzzy match если точного нет.
    """
    found = _undo_exact(task_text)
    if found:
        return True
    actual = find_task_fuzzy_done(task_text)
    if actual:
        return _undo_exact(actual)
    return False


def _undo_exact(task_text: str) -> bool:
    """Меняет [x] → [ ] для точного текста задачи."""
    found = False
    for q in QUADRANTS.values():
        rel     = Path(q["file"])
        content = _read(rel)
        if not content:
            continue
        new = re.sub(
            r"^(- \[)[xX](\] " + re.escape(task_text) + r".*)$",
            r"\g<1> \2",
            content, flags=re.MULTILINE
        )
        if new != content:
            _write(rel, new)
            found = True
            logger.info("↩️  Задача возвращена: '%s'", task_text[:50])
    return found


def find_task_fuzzy_done(query: str) -> str | None:
    """Нечёткий поиск среди ВЫПОЛНЕННЫХ задач."""
    q = query.lower().strip()
    best_match = None
    best_score = 0

    for task in search_tasks("done"):
        text    = task["text"].lower()
        if q == text:
            return task["text"]
        if q in text or text in q:
            score = len(q) / max(len(text), 1)
            if score > best_score:
                best_score  = score
                best_match  = task["text"]
        q_words = set(q.split())
        t_words = set(text.split())
        overlap = len(q_words & t_words) / max(len(q_words), 1)
        if overlap > 0.6 and overlap > best_score:
            best_score = overlap
            best_match = task["text"]

    return best_match if best_score > 0.4 else None


def sync_tasks_to_db(user_id: int) -> dict:
    if not vault_available():
        return {"ok": False, "reason": "vault недоступен"}
    try:
        from app.database import delete_task, get_active_tasks, save_task
        all_tasks  = get_all_tasks()
        obs_texts  = set()
        _Q_TO_PRIO = {"q1": 1, "q2": 2, "q3": 3, "q4": 3}
        for q_key, data in all_tasks.items():
            for t in data["tasks"]:
                if not t["done"]:
                    obs_texts.add(t["text"].lower())
        db_tasks = get_active_tasks(user_id)
        db_texts = {t[1].lower(): t[0] for t in db_tasks}
        deleted = added = 0
        for text_lower, task_id in db_texts.items():
            if text_lower not in obs_texts:
                delete_task(task_id, user_id)
                deleted += 1
        for q_key, data in all_tasks.items():
            prio = _Q_TO_PRIO.get(q_key, 2)
            for t in data["tasks"]:
                if not t["done"] and t["text"].lower() not in db_texts:
                    save_task(user_id, t["text"], prio, "")
                    added += 1
        logger.info("🔄 Sync tasks: -%d +%d", deleted, added)
        return {"ok": True, "deleted": deleted, "added": added}
    except Exception:
        logger.exception("sync_tasks_to_db")
        return {"ok": False}
