"""HistoryStore — task history with FTS5 search."""

import logging

logger = logging.getLogger(__name__)


class HistoryStore:

    @staticmethod
    async def add(task_id: str, title: str, summary: str | None, outcome: str | None, tags: str | None) -> None:
        from core.database import memory_db

        cursor = await memory_db.execute(
            "INSERT INTO history(task_id, title, summary, outcome, tags) VALUES (?, ?, ?, ?, ?)",
            (task_id, title, summary, outcome, tags),
        )
        rowid = cursor.lastrowid
        await memory_db.execute(
            "INSERT INTO history_fts(rowid, title, summary, tags) VALUES (?, ?, ?, ?)",
            (rowid, title, summary or "", tags or ""),
        )
        await memory_db.commit()

    @staticmethod
    async def search(query: str, limit: int = 3) -> list[dict]:
        from core.database import memory_db

        # FTS5 MATCH interprets colons, quotes, *, etc. as syntax.
        # Wrap in double-quotes to force a literal phrase search.
        safe_query = '"' + query[:200].replace('"', '""') + '"'
        try:
            rows = await memory_db.fetchall(
                "SELECT h.title, h.summary, h.outcome, h.created_at "
                "FROM history_fts hft JOIN history h ON h.id = hft.rowid "
                "WHERE history_fts MATCH ? ORDER BY h.id DESC LIMIT ?",
                (safe_query, limit),
            )
        except Exception:
            rows = []  # FTS5 syntax error — return empty rather than crashing
        return [dict(r) for r in rows]

    @staticmethod
    async def format_for_prompt(query: str) -> str:
        results = await HistoryStore.search(query)
        if not results:
            return ""
        lines = ["Relevant past tasks:"]
        for r in results:
            ts = r["created_at"][:10] if r["created_at"] else "?"
            lines.append(f"- {r['title']} ({ts}): {(r['summary'] or '')[:120]}")
        return "\n".join(lines)
