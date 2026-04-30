"""Journal — structured log of ARIA's actions, thoughts, and system events.

Types: thought | action | message_in | message_out | error | system | skill_extracted
Tags: optional facet for filtering (e.g. tag='dream' for dream cycle entries)
"""

import logging

logger = logging.getLogger(__name__)


async def log(
    type_: str,
    content: str,
    task_id: str | None = None,
    tag: str | None = None,
) -> None:
    """Write an entry to the journal and its FTS shadow table."""
    try:
        from core.database import journal_db

        cursor = await journal_db.execute(
            "INSERT INTO journal(type, content, task_id, tag) VALUES (?, ?, ?, ?)",
            (type_, content, task_id, tag),
        )
        rowid = cursor.lastrowid
        await journal_db.execute(
            "INSERT INTO journal_fts(rowid, type, content, tag) VALUES (?, ?, ?, ?)",
            (rowid, type_, content, tag or ""),
        )
        await journal_db.commit()
    except Exception:
        logger.exception("Failed to write journal entry type=%s", type_)


async def tail(n: int = 20) -> list[dict]:
    """Return the last n journal entries, oldest-first."""
    from core.database import journal_db

    rows = await journal_db.fetchall(
        "SELECT id, type, content, task_id, tag, created_at FROM journal ORDER BY id DESC LIMIT ?",
        (n,),
    )
    return [dict(r) for r in reversed(rows)]


async def search(query: str, limit: int = 20) -> list[dict]:
    """FTS5 full-text search over journal content."""
    from core.database import journal_db

    rows = await journal_db.fetchall(
        "SELECT j.id, j.type, j.content, j.task_id, j.tag, j.created_at "
        "FROM journal_fts f JOIN journal j ON j.id = f.rowid "
        "WHERE journal_fts MATCH ? ORDER BY j.id DESC LIMIT ?",
        (query, limit),
    )
    return [dict(r) for r in rows]


async def compact_old_entries() -> None:
    """Summarize journal entries older than 90 days, replacing them with one summary per day.

    Uses the fast LLM model. Idempotent — entries already summarized are skipped.
    """
    from core.database import journal_db
    from core.config import settings
    from core.llm_client import llm_call

    rows = await journal_db.fetchall(
        "SELECT date(created_at) as day, GROUP_CONCAT(type || ': ' || content, '\n') as text "
        "FROM journal WHERE created_at < date('now', '-90 days') AND type != 'compacted' "
        "GROUP BY date(created_at) ORDER BY day"
    )

    for row in rows:
        day = row["day"]
        day_text = row["text"] or ""
        if not day_text.strip():
            continue

        try:
            response = await llm_call(
                model=settings.get_model("light"),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Summarize these journal entries from {day} in 2-3 sentences:\n\n{day_text[:3000]}"
                        ),
                    }
                ],
                max_tokens=150,
            )
            summary = response.choices[0].message.content or day_text[:200]
        except Exception:
            logger.warning("Failed to compact journal for day=%s", day)
            continue

        # Delete old entries for this day, insert one summary
        await journal_db.execute(
            "DELETE FROM journal WHERE date(created_at)=? AND type != 'compacted'", (day,)
        )
        cursor = await journal_db.execute(
            "INSERT INTO journal(type, content, tag, created_at) VALUES (?, ?, ?, ?)",
            ("compacted", summary, "compacted", f"{day}T23:59:59"),
        )
        rowid = cursor.lastrowid
        await journal_db.execute(
            "INSERT INTO journal_fts(rowid, type, content, tag) VALUES (?, ?, ?, ?)",
            (rowid, "compacted", summary, "compacted"),
        )
        await journal_db.commit()
        logger.info("Compacted journal for %s", day)
