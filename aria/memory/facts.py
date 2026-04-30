"""FactsStore — read/write to the facts table with FTS5 sync."""

import logging

logger = logging.getLogger(__name__)


class FactsStore:

    @staticmethod
    async def upsert(key: str, value: str, importance: str = "normal", source: str = "user") -> None:
        """FTS5-safe UPSERT: delete stale FTS row → replace row → insert FTS row."""
        from core.database import memory_db

        await memory_db.execute(
            "DELETE FROM facts_fts WHERE rowid = (SELECT rowid FROM facts WHERE key = ?)",
            (key,),
        )
        await memory_db.execute(
            "INSERT OR REPLACE INTO facts(key, value, importance, source, last_seen_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (key, value, importance, source),
        )
        await memory_db.execute(
            "INSERT INTO facts_fts(rowid, key, value) VALUES "
            "((SELECT rowid FROM facts WHERE key = ?), ?, ?)",
            (key, key, value),
        )
        await memory_db.commit()

    @staticmethod
    async def get(key: str) -> str | None:
        from core.database import memory_db

        row = await memory_db.fetchone(
            "SELECT value FROM facts WHERE key=?", (key,)
        )
        if row:
            await memory_db.execute(
                "UPDATE facts SET last_seen_at=datetime('now') WHERE key=?", (key,)
            )
            await memory_db.commit()
        return row["value"] if row else None

    @staticmethod
    async def get_all_high() -> list[dict]:
        from core.database import memory_db

        rows = await memory_db.fetchall(
            "SELECT key, value FROM facts WHERE importance='high' ORDER BY last_seen_at DESC"
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def search(query: str, limit: int = 10) -> list[dict]:
        from core.database import memory_db

        rows = await memory_db.fetchall(
            "SELECT f.key, f.value, f.importance FROM facts_fts ft "
            "JOIN facts f ON f.rowid = ft.rowid "
            "WHERE facts_fts MATCH ? LIMIT ?",
            (query, limit),
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def format_for_prompt() -> str:
        """Return high-importance facts formatted for prompt injection."""
        from core.database import memory_db

        rows = await memory_db.fetchall(
            "SELECT key, value FROM facts WHERE importance='high' ORDER BY last_seen_at DESC LIMIT 20"
        )
        if not rows:
            return ""
        return "Key facts:\n" + "\n".join(f"- {r['key']}: {r['value']}" for r in rows)
