"""sunday_digest.py — еженедельный обзор по воскресеньям."""
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_MOOD_EMOJI = {
    "радость": "😊", "вдохновение": "🔥", "спокойствие": "😌",
    "гордость": "💪", "грусть": "😔", "усталость": "😴",
    "тревога": "😰", "злость": "😤", "скука": "😑", "нейтрально": "😐",
}


async def build_sunday_digest(user_id: int, name: str) -> str:
    """Собирает текст еженедельного обзора из БД."""
    from app.database import (
        get_active_tasks, get_top_interactions,
        load_diary_entries, _conn,
    )

    now      = datetime.utcnow()
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    with _conn() as con:
        msgs_count = con.execute(
            "SELECT COUNT(*) FROM history WHERE user_id=? AND created_at>=?",
            (user_id, week_ago),
        ).fetchone()[0]

        done_tasks = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id=? AND done=1 AND created_at>=?",
            (user_id, week_ago),
        ).fetchone()[0]

        diary_count = con.execute(
            "SELECT COUNT(*) FROM diary WHERE user_id=? AND created_at>=?",
            (user_id, week_ago),
        ).fetchone()[0]

        mood_rows = con.execute(
            "SELECT mood FROM mood_log WHERE user_id=? AND created_at>=? ORDER BY created_at",
            (user_id, week_ago),
        ).fetchall()

    active_tasks  = len(get_active_tasks(user_id))
    topics        = get_top_interactions(user_id, limit=3)
    diary_entries = load_diary_entries(user_id, limit=3)

    lines = [f"📅 *Итоги недели, {name}*\n"]

    # Активность
    lines.append("**📊 Активность:**")
    lines.append(f"  💬 Сообщений: {msgs_count}")
    lines.append(f"  ✅ Задач закрыто: {done_tasks} | В работе: {active_tasks}")
    if diary_count:
        lines.append(f"  📓 Записей в дневнике: {diary_count}")

    # Настроение недели
    if mood_rows:
        moods    = [r[0] for r in mood_rows]
        top_mood = max(set(moods), key=moods.count)
        lines.append(f"\n**🧠 Настроение недели:** {_MOOD_EMOJI.get(top_mood, '🙂')} {top_mood}")

        # Динамика: первая половина vs вторая
        mid    = len(moods) // 2
        first  = moods[:mid] or moods
        second = moods[mid:] or moods
        first_top  = max(set(first),  key=first.count)
        second_top = max(set(second), key=second.count)
        if first_top != second_top:
            lines.append(
                f"  _К концу недели: {_MOOD_EMOJI.get(second_top, '🙂')} {second_top}_"
            )

    # Главные темы
    if topics:
        topic_str = ", ".join(t[0] for t in topics)
        lines.append(f"\n**🗣️ Главные темы:** {topic_str}")

    # Отрывок из дневника
    if diary_entries:
        lines.append("\n**📓 Из дневника:**")
        _, entry = diary_entries[0]
        clean = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*", "", entry)
        snippet = clean[:150] + ("..." if len(clean) > 150 else "")
        lines.append(f"  _{snippet}_")

    lines.append("\n_Хорошей недели! 🌱_")
    return "\n".join(lines)
