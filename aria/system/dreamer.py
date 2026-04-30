"""Dream cycle — nightly cognitive maintenance at 3:30am.

Six steps, each in its own try/except so one failure never aborts the rest.
max_instances=1 in APScheduler prevents overlap if a previous run is still going.
Total LLM calls: ~3-4 per night using the fast model.
"""

import logging

logger = logging.getLogger(__name__)


async def run_dream_cycle() -> None:
    """Entry point called by the scheduler."""
    from system.journal import log as journal_log

    logger.info("Dream cycle starting")
    await journal_log("system", "Dream cycle started", tag="dream")

    for step_fn in [
        _step1_memory_consolidation,
        _step2_skill_health,
        _step3_contradiction_detection,
        _step4_session_summarization,
        _step5_self_assessment,
        _step6_clutter_pruning,
    ]:
        try:
            await step_fn()
        except Exception:
            logger.exception("Dream cycle step failed: %s", step_fn.__name__)

    await journal_log("system", "Dream cycle complete", tag="dream")
    logger.info("Dream cycle complete")


async def _step1_memory_consolidation() -> None:
    """Identify patterns in recent history, promote to facts."""
    from core.database import memory_db
    from core.llm_client import llm_call
    from core.config import settings
    from system.journal import log as journal_log
    from memory.facts import FactsStore

    rows = await memory_db.fetchall(
        "SELECT title, summary, outcome FROM history "
        "WHERE created_at > date('now', '-7 days') ORDER BY created_at DESC LIMIT 30"
    )
    if len(rows) < 3:
        return

    history_text = "\n".join(
        f"- {r['title']}: {r['outcome'] or '?'}" for r in rows
    )

    response = await llm_call(
        model=settings.get_model("light"),
        messages=[{
            "role": "user",
            "content": (
                f"Analyze these recent tasks and identify stable patterns worth remembering:\n{history_text}\n\n"
                "Return a JSON array of facts to save: [{\"key\": \"...\", \"value\": \"...\"}]\n"
                "Only include patterns that appear 3+ times. Max 5 facts. Reply with ONLY JSON."
            ),
        }],
        max_tokens=300,
    )

    import json
    raw = (response.choices[0].message.content or "[]").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        facts = json.loads(raw)
    except json.JSONDecodeError:
        return

    promoted = 0
    for f in facts[:5]:
        if isinstance(f, dict) and "key" in f and "value" in f:
            # Don't overwrite user-stated facts
            existing = await FactsStore.get(f["key"])
            if not existing:
                await FactsStore.upsert(f["key"], f["value"], source="aria_inferred")
                promoted += 1

    if promoted:
        await journal_log("system", f"Dream: promoted {promoted} facts from history patterns", tag="dream")


async def _step2_skill_health() -> None:
    """Flag degraded skills in the journal."""
    from core.database import memory_db
    from system.journal import log as journal_log

    rows = await memory_db.fetchall(
        "SELECT name, failure_count, success_count FROM skills_meta "
        "WHERE failure_count >= 2 AND failure_count > success_count"
    )
    for r in rows:
        await journal_log(
            "system",
            f"Dream: skill '{r['name']}' may be degraded — "
            f"{r['failure_count']} failures vs {r['success_count']} successes. "
            f"Will attempt repair on next use.",
            tag="dream",
        )


async def _step3_contradiction_detection() -> None:
    """Scan facts for semantic conflicts and flag them."""
    from core.database import memory_db
    from core.llm_client import llm_call
    from core.config import settings
    from system.journal import log as journal_log
    from tg.notify import send_text

    rows = await memory_db.fetchall(
        "SELECT key, value FROM facts ORDER BY importance DESC, last_seen_at DESC LIMIT 50"
    )
    if len(rows) < 5:
        return

    facts_text = "\n".join(f"{r['key']}: {r['value']}" for r in rows)

    response = await llm_call(
        model=settings.get_model("light"),
        messages=[{
            "role": "user",
            "content": (
                f"Scan these facts for semantic conflicts or contradictions:\n{facts_text}\n\n"
                "Return a JSON array of conflicts: [{\"a\": \"fact1\", \"b\": \"fact2\", \"reason\": \"...\"}]\n"
                "Return [] if no conflicts. Reply with ONLY JSON."
            ),
        }],
        max_tokens=200,
    )

    import json
    raw = (response.choices[0].message.content or "[]").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        conflicts = json.loads(raw)
    except json.JSONDecodeError:
        return

    for c in conflicts[:3]:
        if isinstance(c, dict):
            msg = f"Dream: conflicting memory — '{c.get('a', '?')}' vs '{c.get('b', '?')}': {c.get('reason', '')}"
            await journal_log("system", msg, tag="dream")
            # Queue morning notification
            try:
                from core.config import settings as s
                await send_text(
                    s.telegram_chat_id,
                    f"🤔 I noticed conflicting preferences in my memory — "
                    f"can you clarify: {c.get('a', '?')} vs {c.get('b', '?')}?",
                )
            except Exception:
                pass


async def _step4_session_summarization() -> None:
    """Summarize old sessions that have no summary yet."""
    from core.database import memory_db
    from core.llm_client import llm_call
    from core.config import settings

    rows = await memory_db.fetchall(
        "SELECT id, task_ids FROM sessions "
        "WHERE summary IS NULL AND ended_at < date('now', '-7 days') LIMIT 10"
    )
    for row in rows:
        try:
            from memory.sessions import _summarize_session
            summary = await _summarize_session(row["id"], row["task_ids"])
            if summary:
                await memory_db.execute(
                    "UPDATE sessions SET summary=? WHERE id=?", (summary, row["id"])
                )
                await memory_db.commit()
        except Exception:
            logger.debug("Failed to summarize session %s", row["id"])


async def _step5_self_assessment() -> None:
    """Write a self-assessment covering successes and failures from the last 7 days."""
    from core.database import state_db
    from core.llm_client import llm_call
    from core.config import settings
    from system.journal import log as journal_log

    rows = await state_db.fetchall(
        "SELECT title, kind, verdict FROM tasks "
        "WHERE created_at > date('now', '-7 days') AND status='done' LIMIT 30"
    )
    if not rows:
        return

    tasks_text = "\n".join(
        f"- [{r['kind'] or '?'}] {r['title']}: {r['verdict'] or '?'}" for r in rows
    )
    successes = sum(1 for r in rows if r["verdict"] == "SUCCESS")
    failures = sum(1 for r in rows if r["verdict"] == "FAILURE")

    response = await llm_call(
        model=settings.get_model("light"),
        messages=[{
            "role": "user",
            "content": (
                f"Write a 3-5 sentence self-assessment for ARIA based on the last 7 days:\n"
                f"{tasks_text}\n\n"
                f"Successes: {successes}, Failures: {failures}\n"
                "Cover: what went well, what failed, what to watch. Write in first person."
            ),
        }],
        max_tokens=200,
    )
    assessment = response.choices[0].message.content or ""
    if assessment:
        await journal_log("system", f"Dream self-assessment: {assessment}", tag="dream")


async def _step6_clutter_pruning() -> None:
    """Delete stale inferred facts and unused skill metadata."""
    from core.database import memory_db
    from system.journal import log as journal_log

    # Delete inferred facts not accessed in 90+ days
    cursor = await memory_db.execute(
        "DELETE FROM facts WHERE importance='normal' AND source='aria_inferred' "
        "AND last_seen_at < date('now', '-90 days')"
    )
    pruned_facts = cursor.rowcount

    # Delete skill metadata for skills unused in 30+ days with 0 use_count
    cursor = await memory_db.execute(
        "DELETE FROM skills_meta WHERE use_count=0 AND created_at < date('now', '-30 days')"
    )
    pruned_skills = cursor.rowcount

    await memory_db.commit()

    if pruned_facts or pruned_skills:
        await journal_log(
            "system",
            f"Dream: pruned {pruned_facts} stale facts, {pruned_skills} unused skill metadata rows",
            tag="dream",
        )
