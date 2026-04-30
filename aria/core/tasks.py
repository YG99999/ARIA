"""TaskRegistry — CRUD operations on tasks and agent_log tables.

All operations are async and go through aiosqlite.
cancel() sets status so WorkerAgent._should_stop() can check it.
"""

import json
import logging
import uuid
from typing import Any

from core.database import state_db

logger = logging.getLogger(__name__)


class TaskRegistry:

    @staticmethod
    async def create(
        title: str,
        description: str | None = None,
        kind: str | None = None,
        mode: str | None = None,
    ) -> str:
        """Create a new task and return its ID."""
        task_id = str(uuid.uuid4())[:8]
        await state_db.execute(
            """INSERT INTO tasks(id, title, description, status, kind, mode)
               VALUES (?, ?, ?, 'queued', ?, ?)""",
            (task_id, title, description, kind, mode),
        )
        await state_db.commit()
        logger.info("Created task %s: %r", task_id, title)
        return task_id

    @staticmethod
    async def update(
        task_id: str,
        status: str | None = None,
        verdict: str | None = None,
        **kwargs: Any,
    ) -> None:
        fields: list[str] = ["updated_at=datetime('now')"]
        params: list[Any] = []

        if status:
            fields.append("status=?")
            params.append(status)
        if verdict:
            fields.append("verdict=?")
            params.append(verdict)

        params.append(task_id)
        await state_db.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id=?",
            params,
        )
        await state_db.commit()

    @staticmethod
    async def get(task_id: str) -> Any | None:
        return await state_db.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))

    @staticmethod
    async def is_cancelled(task_id: str) -> bool:
        row = await state_db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
        return row is not None and row["status"] == "cancelled"

    @staticmethod
    async def cancel(task_id: str) -> None:
        await state_db.execute(
            "UPDATE tasks SET status='cancelled', updated_at=datetime('now') "
            "WHERE id=? AND status IN ('running', 'queued')",
            (task_id,),
        )
        await state_db.commit()

    # ------------------------------------------------------------------
    # Agent log
    # ------------------------------------------------------------------

    @staticmethod
    async def log_step(
        task_id: str,
        step: int,
        role: str,
        content: str | None = None,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        tool_result: str | None = None,
        state_snapshot: dict | None = None,
    ) -> None:
        await state_db.execute(
            """INSERT INTO agent_log(task_id, step, role, content, tool_name, tool_args, tool_result, state_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                step,
                role,
                content,
                tool_name,
                json.dumps(tool_args) if tool_args else None,
                tool_result,
                json.dumps(state_snapshot) if state_snapshot else None,
            ),
        )
        await state_db.commit()

    @staticmethod
    async def get_steps(task_id: str) -> list[Any]:
        return await state_db.fetchall(
            "SELECT * FROM agent_log WHERE task_id=? ORDER BY step ASC",
            (task_id,),
        )

    @staticmethod
    async def get_step_snapshot(task_id: str, step: int) -> dict | None:
        row = await state_db.fetchone(
            "SELECT state_snapshot FROM agent_log WHERE task_id=? AND step=?",
            (task_id, step),
        )
        if row and row["state_snapshot"]:
            return json.loads(row["state_snapshot"])
        return None

    # ------------------------------------------------------------------
    # Scratch (per-task ephemeral whiteboard)
    # ------------------------------------------------------------------

    @staticmethod
    async def write_scratch(task_id: str, key: str, value: str) -> None:
        await state_db.execute(
            "INSERT OR REPLACE INTO scratch(task_id, key, value, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (task_id, key, value),
        )
        await state_db.commit()

    @staticmethod
    async def read_scratch(task_id: str, key: str | None = None) -> dict[str, str]:
        if key:
            row = await state_db.fetchone(
                "SELECT key, value FROM scratch WHERE task_id=? AND key=?", (task_id, key)
            )
            return {row["key"]: row["value"]} if row else {}
        rows = await state_db.fetchall(
            "SELECT key, value FROM scratch WHERE task_id=?", (task_id,)
        )
        return {r["key"]: r["value"] for r in rows}

    @staticmethod
    async def clear_scratch(task_id: str) -> None:
        await state_db.execute("DELETE FROM scratch WHERE task_id=?", (task_id,))
        await state_db.commit()

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def list_running() -> list[Any]:
        return await state_db.fetchall(
            "SELECT id, title, kind, mode FROM tasks WHERE status='running'"
        )

    @staticmethod
    async def count_interrupted() -> tuple[int, list[str]]:
        rows = await state_db.fetchall(
            "SELECT id, title FROM tasks WHERE status='interrupted' LIMIT 10"
        )
        return len(rows), [r["title"] for r in rows]
