"""Memory tools — save_fact, search_memory, get_facts, think, write_scratch, read_scratch, ingest_document."""

import logging
from pathlib import Path

from core.tools import tool

logger = logging.getLogger(__name__)


@tool(
    name="think",
    description=(
        "Record your reasoning before taking an action. "
        "Use before any irreversible action, ambiguous task, or when uncertain. "
        "Returns 'ok'. Your reasoning is logged and auditable via /journal."
    ),
    schema={
        "type": "object",
        "properties": {
            "reasoning": {"type": "string", "description": "Your step-by-step reasoning"},
        },
        "required": ["reasoning"],
    },
)
async def think(reasoning: str, task_id: str | None = None, **kwargs) -> str:
    from system.journal import log as journal_log
    await journal_log("thought", reasoning, task_id=task_id)
    return "ok"


@tool(
    name="save_fact",
    description="Save a persistent fact to memory. Facts survive restarts and inform future tasks.",
    schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Fact key (snake_case, unique per concept)"},
            "value": {"type": "string", "description": "Fact value"},
            "importance": {
                "type": "string",
                "enum": ["high", "normal"],
                "description": "high = always injected into every prompt; normal = injected when relevant",
            },
        },
        "required": ["key", "value"],
    },
)
async def save_fact(key: str, value: str, importance: str = "normal", **kwargs) -> str:
    from core.database import memory_db

    # FTS5 safe UPSERT: delete stale FTS row, insert new row, sync FTS
    await memory_db.execute(
        "DELETE FROM facts_fts WHERE rowid = (SELECT rowid FROM facts WHERE key = ?)",
        (key,),
    )
    await memory_db.execute(
        "INSERT OR REPLACE INTO facts(key, value, importance, source, last_seen_at) "
        "VALUES (?, ?, ?, 'aria', datetime('now'))",
        (key, value, importance),
    )
    await memory_db.execute(
        "INSERT INTO facts_fts(rowid, key, value) VALUES "
        "((SELECT rowid FROM facts WHERE key = ?), ?, ?)",
        (key, key, value),
    )
    await memory_db.commit()
    return f"Saved fact: {key} = {value[:80]}"


@tool(
    name="search_memory",
    description="Full-text search across facts, history, and skills.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
)
async def search_memory(query: str, **kwargs) -> str:
    from core.database import memory_db

    facts = await memory_db.fetchall(
        "SELECT f.key, f.value FROM facts_fts ft JOIN facts f ON f.rowid = ft.rowid "
        "WHERE facts_fts MATCH ? LIMIT 5",
        (query,),
    )
    history = await memory_db.fetchall(
        "SELECT h.title, h.summary FROM history_fts hft JOIN history h ON h.id = hft.rowid "
        "WHERE history_fts MATCH ? LIMIT 3",
        (query,),
    )

    lines = []
    if facts:
        lines.append("Facts:")
        for r in facts:
            lines.append(f"  {r['key']}: {r['value'][:100]}")
    if history:
        lines.append("History:")
        for r in history:
            lines.append(f"  {r['title']}: {(r['summary'] or '')[:100]}")
    if not lines:
        return f"Nothing found for: {query}"
    return "\n".join(lines)


@tool(
    name="get_facts",
    description="Retrieve all high-importance facts or facts matching a key prefix.",
    schema={
        "type": "object",
        "properties": {
            "prefix": {"type": "string", "description": "Optional key prefix filter"},
            "importance": {"type": "string", "description": "Filter by importance: 'high' or 'normal'"},
        },
    },
)
async def get_facts(prefix: str | None = None, importance: str | None = None, **kwargs) -> str:
    from core.database import memory_db

    sql = "SELECT key, value, importance FROM facts WHERE 1=1"
    params: list = []
    if prefix:
        sql += " AND key LIKE ?"
        params.append(f"{prefix}%")
    if importance:
        sql += " AND importance = ?"
        params.append(importance)
    sql += " ORDER BY importance DESC, last_seen_at DESC LIMIT 50"

    rows = await memory_db.fetchall(sql, params)
    if not rows:
        return "No facts found."
    return "\n".join(f"[{r['importance']}] {r['key']}: {r['value'][:100]}" for r in rows)


@tool(
    name="write_scratch",
    description=(
        "Write a value to the task's ephemeral scratchpad. "
        "Use for intermediate results within a task — deleted when task completes. "
        "Not for cross-task information; use save_fact for that."
    ),
    schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Scratch key"},
            "value": {"type": "string", "description": "Value to store"},
        },
        "required": ["key", "value"],
    },
)
async def write_scratch(key: str, value: str, task_id: str | None = None, **kwargs) -> str:
    if not task_id:
        return "write_scratch requires a task_id context"
    from core.tasks import TaskRegistry
    await TaskRegistry.write_scratch(task_id, key, value)
    return f"Scratch written: {key}"


@tool(
    name="read_scratch",
    description="Read one or all values from the task's ephemeral scratchpad.",
    schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key to read (omit to read all)"},
        },
    },
)
async def read_scratch(key: str | None = None, task_id: str | None = None, **kwargs) -> str:
    if not task_id:
        return "read_scratch requires a task_id context"
    from core.tasks import TaskRegistry
    data = await TaskRegistry.read_scratch(task_id, key)
    if not data:
        return "(empty scratch)"
    import json
    return json.dumps(data, indent=2)


@tool(
    name="ingest_document",
    description=(
        "Ingest a document (PDF, TXT, MD, CSV) into searchable memory chunks. "
        "Chunks are 400 words with 50-word overlap."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the document"},
            "name": {"type": "string", "description": "Friendly name for the document"},
        },
        "required": ["path", "name"],
    },
)
async def ingest_document(path: str, name: str, **kwargs) -> str:
    from core.database import memory_db

    p = Path(path)
    if not p.exists():
        return f"File not found: {path}"

    suffix = p.suffix.lower()
    text = ""

    if suffix == ".pdf":
        try:
            from pdfminer.high_level import extract_text
            text = extract_text(path)
        except ImportError:
            return "pdfminer.six not installed. Run: pip install pdfminer.six"
        except Exception as exc:
            return f"PDF extraction failed: {exc}"
    elif suffix in (".txt", ".md", ".csv", ".rst"):
        text = p.read_text(encoding="utf-8", errors="replace")
    else:
        return f"Unsupported file type: {suffix}"

    if not text.strip():
        return "Document is empty or could not be extracted."

    # Chunk into 400-word windows with 50-word overlap
    words = text.split()
    chunk_size, overlap = 400, 50
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap

    # Delete existing chunks for this doc
    await memory_db.execute("DELETE FROM doc_chunks_fts WHERE doc_name = ?", (name,))
    await memory_db.execute("DELETE FROM doc_chunks WHERE doc_name = ?", (name,))

    for idx, chunk in enumerate(chunks):
        cursor = await memory_db.execute(
            "INSERT INTO doc_chunks(doc_name, chunk_index, content) VALUES (?, ?, ?)",
            (name, idx, chunk),
        )
        rowid = cursor.lastrowid
        await memory_db.execute(
            "INSERT INTO doc_chunks_fts(rowid, doc_name, content) VALUES (?, ?, ?)",
            (rowid, name, chunk),
        )

    await memory_db.commit()
    return f"Ingested '{name}': {len(chunks)} chunks from {len(words)} words."
