"""SessionStore — 4-hour gap rule, Jaccard similarity session matching.

Jaccard on raw word tokens is fast and correct for exact word overlap.
It misses semantic siblings ("deploy my app" vs "vercel hosting").
# TODO: upgrade to semantic similarity if Jaccard proves inaccurate in practice.
"""

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SESSION_GAP_HOURS = 4


def _jaccard(a: str, b: str) -> float:
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


class SessionStore:

    @staticmethod
    async def get_or_create(task_title: str) -> str:
        """Return an existing active session ID or create a new one."""
        from core.database import memory_db

        # Find most recent session
        row = await memory_db.fetchone(
            "SELECT id, intent_words, ended_at, started_at FROM sessions "
            "WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )

        if row:
            # Check 4-hour gap
            started = datetime.fromisoformat(row["started_at"])
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            gap_hours = (now - started).total_seconds() / 3600

            if gap_hours < SESSION_GAP_HOURS:
                # Check similarity
                intent = row["intent_words"] or ""
                similarity = _jaccard(task_title, intent)
                if similarity > 0.1:  # low threshold — same broad topic
                    return row["id"]

            # Close stale session
            await memory_db.execute(
                "UPDATE sessions SET ended_at=datetime('now') WHERE id=?",
                (row["id"],),
            )
            await memory_db.commit()

        # Create new session
        session_id = str(uuid.uuid4())[:8]
        await memory_db.execute(
            "INSERT INTO sessions(id, intent_words) VALUES (?, ?)",
            (session_id, task_title),
        )
        await memory_db.commit()
        return session_id

    @staticmethod
    async def log_task(session_id: str, task_id: str) -> None:
        from core.database import memory_db

        row = await memory_db.fetchone(
            "SELECT task_ids FROM sessions WHERE id=?", (session_id,)
        )
        existing = (row["task_ids"] or "") if row else ""
        task_ids = (existing + "," + task_id).strip(",")
        await memory_db.execute(
            "UPDATE sessions SET task_ids=? WHERE id=?", (task_ids, session_id)
        )
        await memory_db.commit()

    @staticmethod
    async def close_all_sessions() -> None:
        """Called on SIGTERM — close all active sessions with a brief LLM summary."""
        from core.database import memory_db

        rows = await memory_db.fetchall(
            "SELECT id, task_ids FROM sessions WHERE ended_at IS NULL"
        )
        for row in rows:
            # Try to summarize
            summary = await _summarize_session(row["id"], row["task_ids"])
            await memory_db.execute(
                "UPDATE sessions SET ended_at=datetime('now'), summary=? WHERE id=?",
                (summary, row["id"]),
            )
        await memory_db.commit()

    @staticmethod
    async def get_active_context(task_title: str) -> str:
        """Return active session context for prompt injection."""
        from core.database import memory_db

        row = await memory_db.fetchone(
            "SELECT id, intent_words, summary, task_ids FROM sessions "
            "WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )
        if not row:
            return ""
        lines = [f"Active session intent: {row['intent_words']}"]
        if row["summary"]:
            lines.append(f"Session summary: {row['summary'][:200]}")
        return "\n".join(lines)


async def _summarize_session(session_id: str, task_ids: str | None) -> str | None:
    try:
        from core.database import state_db
        from core.llm_client import llm_call
        from core.config import settings

        if not task_ids:
            return None

        ids = [t.strip() for t in task_ids.split(",") if t.strip()]
        tasks = []
        for tid in ids[:10]:
            row = await state_db.fetchone("SELECT title, verdict FROM tasks WHERE id=?", (tid,))
            if row:
                tasks.append(f"{row['title']} ({row['verdict'] or '?'})")

        if not tasks:
            return None

        response = await llm_call(
            model=settings.get_model("light"),
            messages=[{
                "role": "user",
                "content": f"Summarize this session in one sentence:\n" + "\n".join(tasks),
            }],
            max_tokens=60,
        )
        return response.choices[0].message.content
    except Exception:
        return None
