"""Database layer — three aiosqlite databases with WAL mode, FTS5, and schema versioning.

All SQLite access must go through this module. Never use stdlib sqlite3 from the
async event loop — synchronous DB calls stall the entire event loop including
Telegram polling. Every db.execute() is await db.execute().
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


def fts5_escape(query: str) -> str:
    """Wrap a user-supplied string in FTS5 phrase-quote syntax.

    FTS5 MATCH treats bare words like 'models' as potential column names,
    colons as column filters, *, -, AND/OR/NOT as operators, etc.
    Wrapping in double-quotes forces a literal phrase search and avoids
    'no such column: X' errors when the query contains any special tokens.
    """
    return '"' + query[:200].replace('"', '""') + '"'


# ------------------------------------------------------------------
# Database wrapper
# ------------------------------------------------------------------


class Database:
    """Async SQLite connection wrapper with WAL mode and convenience methods."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Any = ()) -> aiosqlite.Cursor:
        assert self._conn is not None, "Database not opened"
        return await self._conn.execute(sql, params)

    async def executemany(self, sql: str, params_seq: Any) -> aiosqlite.Cursor:
        assert self._conn is not None, "Database not opened"
        return await self._conn.executemany(sql, params_seq)

    async def fetchall(self, sql: str, params: Any = ()) -> list[aiosqlite.Row]:
        cursor = await self.execute(sql, params)
        return await cursor.fetchall()

    async def fetchone(self, sql: str, params: Any = ()) -> aiosqlite.Row | None:
        cursor = await self.execute(sql, params)
        return await cursor.fetchone()

    async def commit(self) -> None:
        assert self._conn is not None, "Database not opened"
        await self._conn.commit()

    async def migrate(self, migrations: list[tuple[int, str, list[str]]]) -> None:
        """Apply unapplied migrations in order.

        migrations: list of (version, description, [sql_statement, ...]) tuples.
        Each migration is a list of individual SQL statements — sqlite3 does not
        support executing multiple statements at once.
        Never reruns a migration — safe to call on an existing DB.
        """
        await self.execute(
            """CREATE TABLE IF NOT EXISTS schema_versions (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT DEFAULT (datetime('now')),
                description TEXT
            )"""
        )
        await self.commit()

        row = await self.fetchone("SELECT MAX(version) as v FROM schema_versions")
        current_version = row["v"] if row and row["v"] is not None else 0

        for version, description, statements in sorted(migrations, key=lambda x: x[0]):
            if version <= current_version:
                continue
            logger.info("Applying migration v%d: %s", version, description)
            for stmt in statements:
                stmt = stmt.strip()
                if stmt:
                    await self.execute(stmt)
            await self.execute(
                "INSERT INTO schema_versions(version, description) VALUES (?, ?)",
                (version, description),
            )
            await self.commit()
            logger.info("Migration v%d applied", version)


# ------------------------------------------------------------------
# Module-level DB instances
# ------------------------------------------------------------------

state_db = Database.__new__(Database)
memory_db = Database.__new__(Database)
journal_db = Database.__new__(Database)


# ------------------------------------------------------------------
# Schema definitions
# ------------------------------------------------------------------

_STATE_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Initial state schema",
        [
            """CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                description TEXT,
                status      TEXT NOT NULL DEFAULT 'queued',
                kind        TEXT,
                mode        TEXT,
                verdict     TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS agent_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        TEXT NOT NULL,
                step           INTEGER NOT NULL,
                role           TEXT NOT NULL,
                content        TEXT,
                tool_name      TEXT,
                tool_args      TEXT,
                tool_result    TEXT,
                state_snapshot TEXT,
                created_at     TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_agent_log_task ON agent_log(task_id, step)",
            """CREATE TABLE IF NOT EXISTS scratch (
                task_id    TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, key)
            )""",
            """CREATE TABLE IF NOT EXISTS credentials (
                id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
                service       TEXT NOT NULL,
                account_label TEXT NOT NULL,
                username      TEXT,
                kind          TEXT NOT NULL,
                secret_enc    BLOB NOT NULL,
                source        TEXT DEFAULT 'user',
                session_file  TEXT,
                last_used_at  TEXT,
                rotation_hint TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_credentials_service ON credentials(service)",
            """CREATE TABLE IF NOT EXISTS schedule (
                id           TEXT PRIMARY KEY,
                description  TEXT NOT NULL,
                trigger      TEXT NOT NULL,
                trigger_args TEXT,
                enabled      INTEGER DEFAULT 1,
                last_run_at  TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS health (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cpu_pct     REAL,
                ram_pct     REAL,
                disk_pct    REAL,
                temp_c      REAL,
                recorded_at TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS llm_usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT NOT NULL,
                tokens_in   INTEGER DEFAULT 0,
                tokens_out  INTEGER DEFAULT 0,
                cost_usd    REAL DEFAULT 0.0,
                task_id     TEXT,
                date        TEXT DEFAULT (date('now')),
                recorded_at TEXT DEFAULT (datetime('now'))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_date ON llm_usage(date)",
            """CREATE TABLE IF NOT EXISTS approvals (
                id          TEXT PRIMARY KEY,
                task_id     TEXT,
                category    TEXT NOT NULL,
                description TEXT NOT NULL,
                context     TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                timeout_at  TEXT,
                decided_at  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS paused (
                key   TEXT PRIMARY KEY DEFAULT 'paused',
                value INTEGER DEFAULT 0
            )""",
            "INSERT OR IGNORE INTO paused(key, value) VALUES ('paused', 0)",
            """CREATE TABLE IF NOT EXISTS chromium_procs (
                pid        INTEGER PRIMARY KEY,
                start_time REAL NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )""",
        ],
    ),
]

_MEMORY_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Initial memory schema",
        [
            """CREATE TABLE IF NOT EXISTS facts (
                key          TEXT PRIMARY KEY,
                value        TEXT NOT NULL,
                importance   TEXT DEFAULT 'normal',
                source       TEXT DEFAULT 'user',
                last_seen_at TEXT DEFAULT (datetime('now')),
                created_at   TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                key, value,
                content='facts',
                content_rowid='rowid'
            )""",
            """CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    TEXT NOT NULL,
                title      TEXT NOT NULL,
                summary    TEXT,
                outcome    TEXT,
                tags       TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
                title, summary, tags,
                content='history',
                content_rowid='id'
            )""",
            """CREATE TABLE IF NOT EXISTS sessions (
                id           TEXT PRIMARY KEY,
                intent_words TEXT,
                summary      TEXT,
                started_at   TEXT DEFAULT (datetime('now')),
                ended_at     TEXT,
                task_ids     TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS skills_meta (
                name          TEXT PRIMARY KEY,
                description   TEXT,
                tags          TEXT,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                use_count     INTEGER DEFAULT 0,
                works         INTEGER DEFAULT 1,
                last_error    TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                last_used_at  TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS doc_chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_name    TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content     TEXT NOT NULL,
                embedding   BLOB,
                ingested_at TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(
                doc_name, content,
                content='doc_chunks',
                content_rowid='id'
            )""",
        ],
    ),
]

_JOURNAL_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Initial journal schema",
        [
            """CREATE TABLE IF NOT EXISTS journal (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT NOT NULL,
                content    TEXT NOT NULL,
                task_id    TEXT,
                tag        TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
                type, content, tag,
                content='journal',
                content_rowid='id'
            )""",
        ],
    ),
]


# ------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------


async def init_all_databases(
    state_path: Path, memory_path: Path, journal_path: Path
) -> None:
    """Open all three databases, apply pending migrations, recover interrupted tasks.

    Safe to call on an existing DB — migrations are idempotent.
    """
    global state_db, memory_db, journal_db

    state_db.__init__(state_path)
    memory_db.__init__(memory_path)
    journal_db.__init__(journal_path)

    await state_db.open()
    await memory_db.open()
    await journal_db.open()

    await state_db.migrate(_STATE_MIGRATIONS)
    await memory_db.migrate(_MEMORY_MIGRATIONS)
    await journal_db.migrate(_JOURNAL_MIGRATIONS)

    # Recover interrupted tasks from a previous crash/restart
    cursor = await state_db.execute(
        "UPDATE tasks SET status='interrupted', updated_at=datetime('now') "
        "WHERE status='running' OR status='queued'"
    )
    interrupted = cursor.rowcount
    await state_db.commit()

    if interrupted > 0:
        logger.info(
            "%d task(s) were interrupted by the previous shutdown and marked 'interrupted'",
            interrupted,
        )

    logger.info("All databases initialized (state=%s, memory=%s, journal=%s)",
                state_path, memory_path, journal_path)
