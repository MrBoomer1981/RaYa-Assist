"""
obsidian.py — работа с Obsidian vault.

Структура vault:
  Задачи/Q1.md  — 🔴 Срочно и важно
  Задачи/Q2.md  — 🟡 Важно, не срочно
  Задачи/Q3.md  — 🟠 Срочно, не важно
  Задачи/Q4.md  — ⚪ Не срочно, не важно
  Дневник/YYYY-MM/YYYY-MM-DD.md
  Заметки/YYYY-MM-DD HH-MM Название.md
  Zettelkasten/YYYYMMDDHHMMSS.md
"""
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Квадранты Эйзенхауэра ─────────────────────────────────────────────────────
QUADRANTS = {
    "q1": {"file": "Задачи/Q1.md", "title": "🔴 Q1 — Срочно и важно",    "emoji": "🔴"},
    "q2": {"file": "Задачи/Q2.md", "title": "🟡 Q2 — Важно, не срочно",  "emoji": "🟡"},
    "q3": {"file": "Задачи/Q3.md", "title": "🟠 Q3 — Срочно, не важно",  "emoji": "🟠"},
    "q4": {"file": "Задачи/Q4.md", "title": "⚪ Q4 — Не срочно, не важно","emoji": "⚪"},
}


def VAULT_PATH() -> Path:
    base    = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
    subdir  = os.getenv("OBSIDIAN_VAULT_SUBDIR", "RaYa-Vault")
    return base / subdir if subdir else base


def vault_available() -> bool:
    return VAULT_PATH().exists()


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _slug(text: str, max_len: int = 50) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "", text).strip()
    return re.sub(r"\s+", " ", text)[:max_len].strip()


def _zettel_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S")


def _frontmatter(tags: list, extra: dict | None = None) -> str:
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    lines = ["---", f"created: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC", "tags:", tag_lines]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _write(rel_path: Path, content: str) -> Path:
    full = VAULT_PATH() / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _read(rel_path: Path) -> str | None:
    full = VAULT_PATH() / rel_path
    return full.read_text(encoding="utf-8") if full.exists() else None


# ══════════════════════════════════════════════════════════
# ЗАДАЧИ — матрица Эйзенхауэра
# Каждый квадрант = отдельный .md файл
# Формат строки: - [ ] текст задачи
# ══════════════════════════════════════════════════════════

def _ensure_quadrant_file(q_key: str) -> None:
    """Создаёт файл квадранта если не существует."""
    q     = QUADRANTS[q_key]
    path  = Path(q["file"])
    if not _read(path):
        content = f"# {q['title']}\n\n"
        _write(path, content)
        logger.info("📁 Создан файл квадранта: %s", q["file"])


def add_tasks(tasks: list, quadrant: str = "q2") -> str:
    """Добавляет задачи в файл квадранта. Пропускает дубли."""
    if quadrant not in QUADRANTS:
        quadrant = "q2"
    _ensure_quadrant_file(quadrant)

    q        = QUADRANTS[quadrant]
    rel_path = Path(q["file"])
    existing = _read(rel_path) or f"# {q['title']}\n\n"

    # Собираем существующие тексты задач для проверки дублей
    existing_texts = set()
    for line in existing.splitlines():
        m = re.match(r"^- \[[ xX]\] (.+)$", line)
        if m:
            existing_texts.add(m.group(1).strip().lower())

    # Добавляем только новые задачи
    new_tasks = [t for t in tasks if t.strip().lower() not in existing_texts]
    if not new_tasks:
        logger.info("⏭️ [%s] все задачи уже есть, пропускаем", quadrant.upper())
        return q["file"]

    new_lines = "\n".join(f"- [ ] {t}" for t in new_tasks)
    updated   = existing.rstrip() + "\n" + new_lines + "\n"
    _write(rel_path, updated)

    logger.info("✅ [%s] добавлено %d задач (пропущено дублей: %d)",
                quadrant.upper(), len(new_tasks), len(tasks) - len(new_tasks))
    return q["file"]


def get_all_tasks() -> dict:
    """
    Читает все задачи из всех квадрантов.
    Возвращает {q_key: {"title": ..., "tasks": [{"text": ..., "done": bool}]}}
    """
    result = {}
    for q_key, q in QUADRANTS.items():
        content = _read(Path(q["file"])) or ""
        tasks   = []
        for line in content.splitlines():
            m = re.match(r"^- \[([ xX])\] (.+)$", line)
            if m:
                tasks.append({
                    "text": m.group(2).strip(),
                    "done": m.group(1).lower() == "x",
                })
        result[q_key] = {"title": q["title"], "emoji": q["emoji"], "tasks": tasks}
    return result


def mark_task_done_obsidian(task_text: str) -> bool:
    """Отмечает задачу выполненной во всех квадрантах по тексту."""
    found = False
    for q_key, q in QUADRANTS.items():
        rel   = Path(q["file"])
        content = _read(rel)
        if not content:
            continue
        new_content = re.sub(
            r"^(- \[) \] (" + re.escape(task_text) + r".*)$",
            r"\1x] \2",
            content, flags=re.MULTILINE
        )
        if new_content != content:
            _write(rel, new_content)
            found = True
            logger.info("✅ Задача выполнена в %s: '%s'", q["file"], task_text[:40])
    return found


def delete_task_obsidian(task_text: str) -> bool:
    """Удаляет задачу из всех квадрантов по тексту."""
    found = False
    for q_key, q in QUADRANTS.items():
        rel     = Path(q["file"])
        content = _read(rel)
        if not content:
            continue
        new_content = re.sub(
            r"^- \[[ xX]\] " + re.escape(task_text) + r".*\n?",
            "", content, flags=re.MULTILINE
        )
        if new_content != content:
            _write(rel, new_content)
            found = True
            logger.info("🗑️ Задача удалена из %s: '%s'", q["file"], task_text[:40])
    return found


def format_all_tasks() -> str:
    """Форматирует все задачи для отправки в Telegram."""
    all_tasks = get_all_tasks()
    lines     = []
    total     = 0

    for q_key in ("q1", "q2", "q3", "q4"):
        data      = all_tasks[q_key]
        active    = [t for t in data["tasks"] if not t["done"]]
        if not active:
            continue
        lines.append(f"{data['emoji']} **{data['title']}**")
        for t in active:
            lines.append(f"  • {t['text']}")
        lines.append("")
        total += len(active)

    if not lines:
        return "Сократ, задач нет. Всё чисто 👍"

    return f"📋 Задачи ({total} активных):\n\n" + "\n".join(lines).strip()


# ══════════════════════════════════════════════════════════
# ДНЕВНИК
# ══════════════════════════════════════════════════════════

def write_diary(text: str, dt: datetime | None = None) -> str:
    dt       = dt or datetime.utcnow()
    rel_path = Path(f"Дневник/{dt.strftime('%Y-%m')}/{dt.strftime('%Y-%m-%d')}.md")
    existing = _read(rel_path)
    time_str = dt.strftime("%H:%M")

    if not existing:
        fm      = _frontmatter(["дневник", dt.strftime("%Y-%m")], {"date": dt.strftime("%Y-%m-%d")})
        content = f"{fm}\n\n# {dt.strftime('%d %B %Y')}\n\n**{time_str} UTC**\n\n{text}\n"
    else:
        content = existing.rstrip() + f"\n\n**{time_str} UTC**\n\n{text}\n"

    _write(rel_path, content)
    logger.info("📔 Дневник: %s", rel_path)
    return str(rel_path)


# ══════════════════════════════════════════════════════════
# ЗАМЕТКИ
# ══════════════════════════════════════════════════════════

def create_note(title: str, content: str, tags: list | None = None) -> str:
    dt       = datetime.utcnow()
    tags     = tags or ["заметка"]
    rel_path = Path(f"Заметки/{dt.strftime('%Y-%m-%d %H-%M')} {_slug(title)}.md")
    fm       = _frontmatter(["заметка"] + tags)
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}\n")
    logger.info("📝 Заметка: %s", rel_path)
    return str(rel_path)


# ══════════════════════════════════════════════════════════
# ZETTELKASTEN
# ══════════════════════════════════════════════════════════

def add_zettel(title: str, content: str, tags: list | None = None, links: list | None = None) -> str:
    zid      = _zettel_id()
    tags     = tags or []
    links    = links or []
    rel_path = Path(f"Zettelkasten/{zid}.md")
    fm       = _frontmatter(["zettel"] + tags, {"id": zid, "title": f'"{title}"'})
    links_str = ("\n\n## Связи\n" + "\n".join(f"- [[{l}]]" for l in links)) if links else ""
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}{links_str}\n")
    logger.info("🧠 Zettel: %s — '%s'", zid, title[:40])
    return str(rel_path)


# ══════════════════════════════════════════════════════════
# ПОИСК И СТАТИСТИКА
# ══════════════════════════════════════════════════════════

def search_vault(query: str, folder: str = "") -> list:
    root  = VAULT_PATH() / folder if folder else VAULT_PATH()
    q     = query.lower()
    found = []
    for f in sorted(root.rglob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            if q in text.lower():
                snippet, title = "", f.stem
                for line in text.splitlines():
                    if q in line.lower() and line.strip() and not line.startswith("---"):
                        snippet = line.strip()[:200]
                        break
                for line in text.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                found.append({"path": str(f.relative_to(VAULT_PATH())), "snippet": snippet, "title": title})
        except Exception:
            pass
    return found[:10]


def read_note(query: str) -> str | None:
    for folder in ("Заметки", "Zettelkasten", "Дневник", "Задачи", ""):
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


def vault_stats() -> dict:
    stats = {}
    for folder in ("Дневник", "Заметки", "Zettelkasten"):
        root = VAULT_PATH() / folder
        stats[folder] = len(list(root.rglob("*.md"))) if root.exists() else 0
    # Задачи — считаем активные строки по квадрантам
    all_tasks = get_all_tasks()
    for q_key, data in all_tasks.items():
        active = sum(1 for t in data["tasks"] if not t["done"])
        stats[f"{data['emoji']} {data['title']}"] = active
    return stats


def cleanup_vault() -> dict:
    """Удаляет все файлы из vault кроме папок RaYa."""
    vault = VAULT_PATH()
    if not vault.exists():
        return {"ok": False, "deleted": [], "error": "vault не найден"}
    raya_folders = {"Дневник", "Заметки", "Задачи", "Zettelkasten", ".obsidian"}
    deleted = []
    for item in sorted(vault.iterdir()):
        if item.name not in raya_folders:
            try:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
                deleted.append(item.name)
                logger.info("🗑️ Vault cleanup: '%s'", item.name)
            except Exception as e:
                logger.warning("Vault cleanup: не удалось удалить '%s': %s", item.name, e)
    return {"ok": True, "deleted": deleted}

def sync_tasks_to_db(user_id: int) -> dict:
    """
    Синхронизирует задачи из Obsidian в БД.
    Удаляет из БД задачи которых нет в Obsidian.
    Добавляет в БД новые задачи из Obsidian.
    """
    if not vault_available():
        return {"ok": False, "reason": "vault недоступен"}

    try:
        from app.database import get_active_tasks, save_task, delete_task

        # Получаем все активные задачи из Obsidian
        all_tasks   = get_all_tasks()
        obs_texts   = set()
        _Q_TO_PRIO  = {"q1": 1, "q2": 2, "q3": 3, "q4": 3}

        for q_key, data in all_tasks.items():
            for t in data["tasks"]:
                if not t["done"]:
                    obs_texts.add(t["text"].lower())

        # Получаем задачи из БД
        db_tasks  = get_active_tasks(user_id)
        db_texts  = {t[1].lower(): t[0] for t in db_tasks}

        deleted = 0
        added   = 0

        # Удаляем из БД то чего нет в Obsidian
        for text_lower, task_id in db_texts.items():
            if text_lower not in obs_texts:
                delete_task(task_id, user_id)
                deleted += 1

        # Добавляем в БД то что есть в Obsidian но нет в БД
        for q_key, data in all_tasks.items():
            priority = _Q_TO_PRIO.get(q_key, 2)
            for t in data["tasks"]:
                if not t["done"] and t["text"].lower() not in db_texts:
                    save_task(user_id, t["text"], priority, "")
                    added += 1

        logger.info("🔄 Sync tasks: удалено %d, добавлено %d из БД", deleted, added)
        return {"ok": True, "deleted": deleted, "added": added}

    except Exception as e:
        logger.exception("sync_tasks_to_db: ошибка")
        return {"ok": False, "reason": str(e)}
