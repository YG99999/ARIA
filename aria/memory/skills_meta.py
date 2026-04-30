"""SkillsMetaStore — skill registry with success/failure tracking."""

import logging

logger = logging.getLogger(__name__)


class SkillsMetaStore:

    @staticmethod
    async def get_relevant(query: str, limit: int = 5) -> list[dict]:
        from core.database import memory_db

        rows = await memory_db.fetchall(
            "SELECT name, description, tags, success_count, failure_count "
            "FROM skills_meta WHERE works=1 ORDER BY use_count DESC LIMIT ?",
            (limit,),
        )
        # Simple tag/description match — FTS5 upgrade possible later
        results = []
        query_words = set(query.lower().split())
        for r in rows:
            tags = set((r["tags"] or "").lower().split(","))
            desc_words = set((r["description"] or "").lower().split())
            if query_words & (tags | desc_words):
                results.append(dict(r))
        return results

    @staticmethod
    async def record_success(name: str) -> None:
        from core.database import memory_db

        await memory_db.execute(
            "UPDATE skills_meta SET success_count=success_count+1, use_count=use_count+1, "
            "last_used_at=datetime('now') WHERE name=?",
            (name,),
        )
        await memory_db.commit()

    @staticmethod
    async def record_failure(name: str, error: str) -> None:
        from core.database import memory_db

        await memory_db.execute(
            "UPDATE skills_meta SET failure_count=failure_count+1, use_count=use_count+1, "
            "last_error=?, last_used_at=datetime('now') WHERE name=?",
            (error[:200], name),
        )
        # Auto-disable at 2 consecutive failures
        row = await memory_db.fetchone(
            "SELECT failure_count, success_count FROM skills_meta WHERE name=?", (name,)
        )
        if row and row["failure_count"] >= 2 and row["failure_count"] > row["success_count"]:
            await memory_db.execute(
                "UPDATE skills_meta SET works=0 WHERE name=?", (name,)
            )
            logger.warning("Skill '%s' auto-disabled after repeated failures", name)
        await memory_db.commit()

    @staticmethod
    async def format_for_prompt(query: str) -> str:
        skills = await SkillsMetaStore.get_relevant(query)
        if not skills:
            return ""
        lines = ["Available skills:"]
        for s in skills:
            lines.append(f"- {s['name']}: {s['description']}")
        return "\n".join(lines)
