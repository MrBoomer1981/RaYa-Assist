"""
obsidian.py — работа с Obsidian vault через файловую систему.

Vault живёт на Railway Volume (/data/obsidian_vault/).
Remotely Save в Obsidian синхронизирует его через WebDAV.

Структура:
  /Дневник/YYYY-MM/YYYY-MM-DD.md
  /Заметки/YYYY-MM-DD HH-MM Заголовок.md
  /Задачи/YYYY-MM-DD.md
  /Zettelkasten/YYYYMMDDHHMMSS.md
"""
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

def VAULT_PATH() -> Path:
    base = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
    # Remotely Save синхронизирует в подпапку RaYa-Vault
    vault_subdir = os.getenv("OBSIDIAN_VAULT_SUBDIR", "RaYa-Vault")
    return base / vault_subdir if vault_subdir else base


def _slug(text: str, max_len: int = 50) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "", text).strip()
    return re.sub(r"\s+", " ", text)[:max_len].strip()


def _zettel_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S")


def _frontmatter(tags: list, extra: dict | None = None) -> str:
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    lines = [
        "---",
        f"created: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        "tags:",
        tag_lines,
    ]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _write(rel_path: Path, content: str, append: bool = False) -> Path:
    full = VAULT_PATH() / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    if append and full.exists():
        with open(full, "a", encoding="utf-8") as f:
            f.write("\n\n---\n\n" + content)
    else:
        full.write_text(content, encoding="utf-8")
    return full


def _read(rel_path: Path) -> str | None:
    full = VAULT_PATH() / rel_path
    return full.read_text(encoding="utf-8") if full.exists() else None


def _search(query: str, folder: str = "") -> list:
    root  = VAULT_PATH() / folder if folder else VAULT_PATH()
    q     = query.lower()
    found = []
    for f in sorted(root.rglob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            if q in text.lower():
                snippet = ""
                for line in text.splitlines():
                    if q in line.lower() and line.strip() and not line.startswith("---"):
                        snippet = line.strip()[:200]
                        break
                title = f.stem
                for line in text.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                found.append({"path": str(f.relative_to(VAULT_PATH())), "snippet": snippet, "title": title})
        except Exception:
            pass
    return found[:10]


def vault_available() -> bool:
    return VAULT_PATH().exists()


def write_diary(text: str, dt: datetime | None = None) -> str:
    dt       = dt or datetime.utcnow()
    rel_path = Path(f"Дневник/{dt.strftime('%Y-%m')}/{dt.strftime('%Y-%m-%d')}.md")
    existing = _read(rel_path)
    time_str = dt.strftime("%H:%M")
    if not existing:
        fm      = _frontmatter(
            tags=["дневник", dt.strftime("%Y"), dt.strftime("%Y-%m")],
            extra={"date": dt.strftime("%Y-%m-%d")},
        )
        content = f"{fm}\n\n# {dt.strftime('%d %B %Y')}\n\n**{time_str} UTC**\n\n{text}"
        _write(rel_path, content)
    else:
        _write(rel_path, f"**{time_str} UTC**\n\n{text}", append=True)
    logger.info("📔 Дневник: %s", rel_path)
    return str(rel_path)


def create_note(title: str, content: str, tags: list | None = None) -> str:
    dt       = datetime.utcnow()
    tags     = tags or ["заметка"]
    rel_path = Path(f"Заметки/{dt.strftime('%Y-%m-%d %H-%M')} {_slug(title)}.md")
    fm       = _frontmatter(tags=["заметка"] + tags)
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}")
    logger.info("📝 Заметка: %s", rel_path)
    return str(rel_path)


def add_tasks(tasks: list, group: str = "", dt: datetime | None = None) -> str:
    """
    Все задачи хранятся в одном файле Задачи/Все задачи.md.
    Группируются по разделам которые RaYa определяет сама.
    Каждая задача — отдельная строка чеклиста.
    """
    dt       = dt or datetime.utcnow()
    rel_path = Path("Задачи/Все задачи.md")
    existing = _read(rel_path)
    time_str = dt.strftime("%d.%m.%Y %H:%M")

    # Формируем новые строки задач
    new_items = "\n".join(f"- [ ] {t}" for t in tasks)

    if not existing:
        # Создаём файл с базовой структурой
        fm = _frontmatter(tags=["задачи"])
        content = (
            f"{fm}\n\n"
            f"# Задачи\n\n"
            f"> Обновлено: {time_str}\n\n"
        )
        if group:
            content += f"## {group}\n\n{new_items}\n"
        else:
            content += f"## Входящие\n\n{new_items}\n"
        _write(rel_path, content)
    else:
        # Добавляем в существующий файл
        if group and f"## {group}" in existing:
            # Группа уже есть — добавляем задачи в конец группы
            # Находим позицию после заголовка группы
            lines = existing.split("\n")
            result = []
            in_group = False
            inserted = False
            for i, line in enumerate(lines):
                result.append(line)
                if line.strip() == f"## {group}":
                    in_group = True
                elif in_group and not inserted:
                    # Ищем конец группы (следующий ## или конец файла)
                    next_lines = lines[i+1:]
                    at_end = all(not l.startswith("## ") for l in next_lines if l.strip())
                    if line.startswith("## ") and line.strip() != f"## {group}":
                        # Вставляем перед следующей группой
                        result = result[:-1]
                        result.extend(new_items.split("\n"))
                        result.append("")
                        result.append(line)
                        inserted = True
                        in_group = False
            if not inserted:
                result.extend(new_items.split("\n"))
            new_content = "\n".join(result)
            # Обновляем timestamp
            import re
            new_content = re.sub(r'> Обновлено:.*', f'> Обновлено: {time_str}', new_content)
            _write(rel_path, new_content)
        elif group:
            # Новая группа — добавляем секцию
            addition = f"\n## {group}\n\n{new_items}\n"
            _write(rel_path, addition, append=True)
            # Обновляем timestamp
            import re
            content = _read(rel_path) or ""
            content = re.sub(r'> Обновлено:.*', f'> Обновлено: {time_str}', content)
            _write(rel_path, content)
        else:
            # Без группы — добавляем в "Входящие" или создаём
            if "## Входящие" in existing:
                addition = new_items
                lines = existing.split("\n")
                result = []
                after_incoming = False
                inserted = False
                for line in lines:
                    result.append(line)
                    if line.strip() == "## Входящие":
                        after_incoming = True
                    elif after_incoming and not inserted and (line.startswith("## ") or not line.strip()):
                        if line.startswith("## ") and line.strip() != "## Входящие":
                            result = result[:-1]
                            result.extend(new_items.split("\n"))
                            result.append("")
                            result.append(line)
                            inserted = True
                            after_incoming = False
                if not inserted:
                    result.extend(new_items.split("\n"))
                import re
                new_content = "\n".join(result)
                new_content = re.sub(r'> Обновлено:.*', f'> Обновлено: {time_str}', new_content)
                _write(rel_path, new_content)
            else:
                _write(rel_path, f"\n## Входящие\n\n{new_items}\n", append=True)

    logger.info("✅ Задачи: %s (%d шт.) группа='%s'", rel_path, len(tasks), group)
    return str(rel_path)


def add_zettel(title: str, content: str, tags: list | None = None, links: list | None = None) -> str:
    zid      = _zettel_id()
    tags     = tags or []
    links    = links or []
    rel_path = Path(f"Zettelkasten/{zid}.md")
    fm       = _frontmatter(tags=["zettel"] + tags, extra={"id": zid, "title": f'"{title}"'})
    links_str = ("\n\n## Связи\n" + "\n".join(f"- [[{l}]]" for l in links)) if links else ""
    _write(rel_path, f"{fm}\n\n# {title}\n\n{content}{links_str}")
    logger.info("🧠 Zettel: %s — '%s'", zid, title[:40])
    return str(rel_path)


def read_note(query: str) -> str | None:
    for folder in ("Заметки", "Zettelkasten", "Дневник", "Задачи", ""):
        p = Path(folder) / query if folder else Path(query)
        if not str(p).endswith(".md"):
            p = Path(str(p) + ".md")
        content = _read(p)
        if content:
            return content
    results = _search(query)
    return _read(Path(results[0]["path"])) if results else None


def search_vault(query: str, folder: str = "") -> list:
    return _search(query, folder)


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
    # Квадранты Эйзенхауэра
    for qkey, qdata in QUADRANTS.items():
        root = VAULT_PATH() / qdata["folder"]
        stats[f"{qdata['emoji']} {qdata['name']}"] = (
            len(list(root.rglob("*.md"))) if root.exists() else 0
        )
    return stats

def cleanup_vault() -> dict:
    """Удаляет все файлы из vault кроме папок RaYa."""
    import shutil
    vault = VAULT_PATH()
    if not vault.exists():
        return {"ok": False, "deleted": [], "error": "vault не найден"}

    raya_folders = {"Дневник", "Заметки", "Задачи", "Zettelkasten", ".obsidian"}
    deleted = []

    for item in sorted(vault.iterdir()):
        if item.name not in raya_folders:
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                deleted.append(item.name)
                logger.info("🗑️ Vault cleanup: удалён '%s'", item.name)
            except Exception as e:
                logger.warning("Vault cleanup: не удалось удалить '%s': %s", item.name, e)

    return {"ok": True, "deleted": deleted}
