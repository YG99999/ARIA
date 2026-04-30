"""All Telegram slash command handlers.

CRITICAL: Every handler here reads SQLite directly — never calls the LLM.
This means commands work even if the orchestrator crashes or is busy.

Each handler registered in COMMAND_HANDLERS at the bottom of this file.
"""

import logging
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------

async def _reply(update: Update, text: str, parse_mode: str | None = None) -> None:
    if update.message:
        await update.message.reply_text(text[:4096], parse_mode=parse_mode)


# ------------------------------------------------------------------
# /start /help
# ------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "👋 ARIA online.\n\n"
        "Send me any task in plain English. Examples:\n"
        "  • Search for the latest news on X\n"
        "  • Install Python package Y\n"
        "  • Every morning at 8, send me the weather\n\n"
        "Type /help for all commands.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    row = await memory_db.fetchone("SELECT value FROM facts WHERE key = 'ux_mode'")
    ux_mode = row["value"] if row else "beginner"

    beginner = (
        "**ARIA Commands**\n\n"
        "**Basics**\n"
        "/status — What am I doing right now?\n"
        "/tasks — List recent tasks\n"
        "/stop [id] — Stop a running task\n"
        "/approve [id] — Approve a pending action\n"
        "/memory — What do I remember?\n\n"
        "Type any request in plain English to give me a task."
    )

    expert_extra = (
        "\n\n**Advanced**\n"
        "/task [id] — Full task detail + step log\n"
        "/agents — Running agent details\n"
        "/journal [n|term] — Recent journal entries or search\n"
        "/health — System vitals + LLM reachability\n"
        "/schedule — Scheduled jobs\n"
        "/skills — Loaded skill files\n"
        "/skill [name] — Skill detail\n"
        "/budget — Today's LLM spend\n"
        "/approvals — Pending approvals\n"
        "/recall [term] — Search memory\n"
        "/accounts — Saved credentials (no secrets shown)\n"
        "/backup — Download data backup\n"
        "/mode [beginner|expert] — Switch UX mode\n"
        "/trust — Recent injection detection log\n"
        "/rollback [n] — Git revert last N commits\n"
        "/pause — Pause task dispatch\n"
        "/resume — Resume dispatch\n"
        "/dream — Last dream cycle summary\n"
        "/why [id] — Plain-English explanation of a task\n"
        "/retry [id] [step] — Restart task from a checkpoint\n"
        "/docs — Ingested documents\n"
        "/session — Active session info\n"
        "/logs [n] — Raw log file tail\n"
    )

    text = beginner + (expert_extra if ux_mode == "expert" else "")
    await _reply(update, text, parse_mode="Markdown")


# ------------------------------------------------------------------
# /status
# ------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db, memory_db

    row = await memory_db.fetchone("SELECT value FROM facts WHERE key = 'ux_mode'")
    ux_mode = row["value"] if row else "beginner"

    running = await state_db.fetchall(
        "SELECT id, title, kind FROM tasks WHERE status = 'running' ORDER BY updated_at DESC LIMIT 5"
    )

    if ux_mode == "beginner":
        if not running:
            paused_row = await state_db.fetchone("SELECT value FROM paused WHERE key='paused'")
            msg = "⏸️ Paused." if (paused_row and paused_row["value"]) else "I'm idle and ready."
            await _reply(update, msg)
        else:
            lines = ["I'm working on:"]
            for t in running:
                lines.append(f"  • {t['title']}")
            await _reply(update, "\n".join(lines))
    else:
        if not running:
            await _reply(update, "Status: IDLE\nNo active tasks.")
        else:
            lines = ["Status: RUNNING\n"]
            for t in running:
                lines.append(f"[{t['id']}] {t['kind'] or '?'}: {t['title']}")

            # Pause state
            paused_row = await state_db.fetchone(
                "SELECT value FROM paused WHERE key = 'paused'"
            )
            if paused_row and paused_row["value"]:
                lines.append("\n⏸️ PAUSED — new task dispatch is suspended")

            # Last dream cycle info (from journal)
            try:
                from core.database import journal_db
                dream_row = await journal_db.fetchone(
                    "SELECT content, timestamp FROM journal "
                    "WHERE type='system' AND tag='dream' ORDER BY timestamp DESC LIMIT 1"
                )
                if dream_row:
                    ts = dream_row["timestamp"][:16] if dream_row["timestamp"] else "?"
                    # Count promoted facts, flagged skills, contradictions from last night
                    import datetime
                    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=10)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    dream_rows = await journal_db.fetchall(
                        "SELECT content FROM journal WHERE type='system' AND tag='dream' "
                        "AND timestamp > ? ORDER BY timestamp DESC LIMIT 20",
                        (cutoff,),
                    )
                    n_promoted = sum(1 for r in dream_rows if "promoted" in r["content"].lower())
                    n_flagged = sum(1 for r in dream_rows if "flagged" in r["content"].lower() or "degraded" in r["content"].lower())
                    n_conflicts = sum(1 for r in dream_rows if "conflict" in r["content"].lower() or "contradicting" in r["content"].lower())
                    lines.append(
                        f"\nLast dream: {ts} — "
                        f"{n_promoted} facts promoted, {n_flagged} skills flagged, "
                        f"{n_conflicts} contradictions found"
                    )
            except Exception:
                pass

            await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /tasks [filter]
# ------------------------------------------------------------------

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    args = context.args or []
    status_filter = args[0] if args else None

    if status_filter:
        rows = await state_db.fetchall(
            "SELECT id, title, status, kind, created_at FROM tasks "
            "WHERE status = ? ORDER BY created_at DESC LIMIT 20",
            (status_filter,),
        )
    else:
        rows = await state_db.fetchall(
            "SELECT id, title, status, kind, created_at FROM tasks "
            "ORDER BY created_at DESC LIMIT 20"
        )

    if not rows:
        await _reply(update, "No tasks found.")
        return

    lines = [f"Recent tasks ({len(rows)}):"]
    for t in rows:
        ts = t["created_at"][:16] if t["created_at"] else "?"
        lines.append(f"[{t['id']}] {t['status'].upper():12} {t['title']} ({ts})")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /task [id]
# ------------------------------------------------------------------

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    args = context.args or []
    if not args:
        await _reply(update, "Usage: /task [id]")
        return

    task_id = args[0]
    task = await state_db.fetchone(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    )
    if not task:
        await _reply(update, f"Task {task_id!r} not found.")
        return

    steps = await state_db.fetchall(
        "SELECT step, role, content, tool_name, tool_result, created_at "
        "FROM agent_log WHERE task_id = ? ORDER BY step ASC",
        (task_id,),
    )

    lines = [
        f"Task: {task['title']}",
        f"ID: {task['id']}",
        f"Status: {task['status']}",
        f"Kind: {task['kind'] or '?'}",
        f"Verdict: {task['verdict'] or '—'}",
        f"Created: {task['created_at']}",
        "",
        f"Steps ({len(steps)}):",
    ]
    for s in steps[-15:]:  # last 15 steps to fit in message
        role = s["role"].upper()
        content = (s["content"] or s["tool_name"] or "")[:120]
        lines.append(f"  [{s['step']}] {role}: {content}")

    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /stop [id]
# ------------------------------------------------------------------

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    args = context.args or []
    if not args:
        await _reply(update, "Usage: /stop [task_id]")
        return

    task_id = args[0]
    cursor = await state_db.execute(
        "UPDATE tasks SET status='cancelled', updated_at=datetime('now') WHERE id=? AND status IN ('running','queued')",
        (task_id,),
    )
    await state_db.commit()

    if cursor.rowcount:
        await _reply(update, f"Task {task_id} cancelled.")
    else:
        await _reply(update, f"Task {task_id} not found or not cancellable.")


# ------------------------------------------------------------------
# /agents
# ------------------------------------------------------------------

async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    running = await state_db.fetchall(
        "SELECT id, title, kind, mode, updated_at FROM tasks WHERE status='running' ORDER BY updated_at"
    )
    queued = await state_db.fetchall(
        "SELECT id, title, kind FROM tasks WHERE status='queued' ORDER BY created_at"
    )

    lines = []
    if running:
        lines.append(f"Running ({len(running)}):")
        for t in running:
            lines.append(f"  [{t['id']}] {t['kind'] or '?'}/{t['mode'] or '?'}: {t['title']}")
    if queued:
        lines.append(f"Queued ({len(queued)}):")
        for i, t in enumerate(queued):
            lines.append(f"  [pos {i+1}] [{t['id']}] {t['title']}")
    if not running and not queued:
        lines.append("No active agents.")

    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /journal [n|term]
# ------------------------------------------------------------------

async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import journal_db

    args = context.args or []

    if args and args[0].isdigit():
        n = min(int(args[0]), 50)
        rows = await journal_db.fetchall(
            "SELECT type, content, task_id, created_at FROM journal ORDER BY id DESC LIMIT ?",
            (n,),
        )
        rows = list(reversed(rows))
    elif args:
        from core.database import fts5_escape
        term = " ".join(args)
        try:
            rows = await journal_db.fetchall(
                "SELECT j.type, j.content, j.task_id, j.created_at "
                "FROM journal_fts f JOIN journal j ON j.id = f.rowid "
                "WHERE journal_fts MATCH ? ORDER BY j.id DESC LIMIT 20",
                (fts5_escape(term),),
            )
        except Exception:
            rows = []
    else:
        rows = await journal_db.fetchall(
            "SELECT type, content, task_id, created_at FROM journal ORDER BY id DESC LIMIT 10"
        )
        rows = list(reversed(rows))

    if not rows:
        await _reply(update, "No journal entries found.")
        return

    lines = [f"Journal ({len(rows)} entries):"]
    for r in rows:
        ts = r["created_at"][:16] if r["created_at"] else "?"
        task_part = f" [{r['task_id']}]" if r["task_id"] else ""
        content = (r["content"] or "")[:120].replace("\n", " ")
        lines.append(f"{ts}{task_part} {r['type'].upper()}: {content}")

    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /health
# ------------------------------------------------------------------

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from system.health import HealthMonitor

    snapshot = await HealthMonitor.snapshot()
    llm_ok = await HealthMonitor.check_llm()

    lines = [
        "System health:",
        f"  CPU:   {snapshot.get('cpu_pct', 0):.1f}%",
        f"  RAM:   {snapshot.get('ram_pct', 0):.1f}%",
        f"  Disk:  {snapshot.get('disk_pct', 0):.1f}%",
    ]
    temp = snapshot.get("temp_c", 0)
    if temp > 0:
        lines.append(f"  Temp:  {temp:.1f}°C")
    lines.append(f"  LLM:   {'✅ reachable' if llm_ok else '❌ unreachable'}")

    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /memory
# ------------------------------------------------------------------

async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    rows = await memory_db.fetchall(
        "SELECT key, value, importance, source, last_seen_at FROM facts ORDER BY importance DESC, last_seen_at DESC LIMIT 30"
    )
    if not rows:
        await _reply(update, "No facts in memory.")
        return

    lines = [f"Memory ({len(rows)} facts):"]
    for r in rows:
        imp = "★" if r["importance"] == "high" else " "
        lines.append(f"{imp} {r['key']}: {r['value'][:80]}")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /recall [term]
# ------------------------------------------------------------------

async def cmd_recall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search facts, history, and sessions. Labels each result [fact], [history], or [session]."""
    from core.database import memory_db, state_db

    args = context.args or []
    if not args:
        await _reply(update, "Usage: /recall [search term]")
        return

    term = " ".join(args)
    results: list[str] = []

    from core.database import fts5_escape
    safe_term = fts5_escape(term)

    # 1. Facts — FTS5
    try:
        rows = await memory_db.fetchall(
            "SELECT f.key, f.value FROM facts_fts ft "
            "JOIN facts f ON f.rowid = ft.rowid "
            "WHERE facts_fts MATCH ? LIMIT 5",
            (safe_term,),
        )
        for r in rows:
            results.append(f"  [fact] {r['key']}: {r['value'][:100]}")
    except Exception:
        pass

    # 2. History — FTS5
    try:
        rows = await memory_db.fetchall(
            "SELECT h.title, h.summary FROM history_fts ft "
            "JOIN history h ON h.rowid = ft.rowid "
            "WHERE history_fts MATCH ? LIMIT 5",
            (safe_term,),
        )
        for r in rows:
            results.append(f"  [history] {r['title']}: {r['summary'][:100]}")
    except Exception:
        pass

    # 3. Sessions — direct search on intent field
    try:
        rows = await state_db.fetchall(
            "SELECT intent, summary FROM sessions "
            "WHERE intent LIKE ? OR summary LIKE ? LIMIT 3",
            (f"%{term}%", f"%{term}%"),
        )
        for r in rows:
            label = r["summary"][:80] if r["summary"] else r["intent"][:80]
            results.append(f"  [session] {r['intent'][:50]}: {label}")
    except Exception:
        pass

    if not results:
        await _reply(update, f"Nothing found for '{term}'.")
        return

    lines = [f"Results for '{term}' ({len(results)} found):"] + results
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /approvals
# ------------------------------------------------------------------

async def cmd_approvals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    rows = await state_db.fetchall(
        "SELECT id, category, description, timeout_at, created_at FROM approvals "
        "WHERE status='pending' ORDER BY created_at"
    )
    if not rows:
        await _reply(update, "No pending approvals.")
        return

    lines = [f"Pending approvals ({len(rows)}):"]
    for r in rows:
        ts = r["created_at"][:16] if r["created_at"] else "?"
        lines.append(f"  [{r['id']}] {r['category']}: {r['description'][:60]} ({ts})")
    lines.append("\nUse /approve [id] or /deny [id]")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /approve [id] /deny [id]
# ------------------------------------------------------------------

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /approve [approval_id]")
        return
    from core.approval import approve
    approve(args[0])
    await _reply(update, f"✅ Approved [{args[0]}]")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /deny [approval_id]")
        return
    from core.approval import deny
    deny(args[0])
    await _reply(update, f"❌ Denied [{args[0]}]")


# ------------------------------------------------------------------
# /schedule
# ------------------------------------------------------------------

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    rows = await state_db.fetchall(
        "SELECT id, description, trigger, enabled, last_run_at FROM schedule ORDER BY created_at"
    )
    if not rows:
        await _reply(update, "No scheduled jobs.")
        return

    lines = [f"Scheduled jobs ({len(rows)}):"]
    for r in rows:
        status = "✅" if r["enabled"] else "⏸️"
        last = r["last_run_at"][:16] if r["last_run_at"] else "never"
        lines.append(f"{status} [{r['id']}] {r['description']} — {r['trigger']} (last: {last})")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /pause /resume
# ------------------------------------------------------------------

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    await state_db.execute("UPDATE paused SET value=1 WHERE key='paused'")
    await state_db.commit()
    await _reply(update, "⏸️ ARIA paused. New task dispatch suspended.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    await state_db.execute("UPDATE paused SET value=0 WHERE key='paused'")
    await state_db.commit()
    await _reply(update, "▶️ ARIA resumed.")


# ------------------------------------------------------------------
# /budget
# ------------------------------------------------------------------

async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.cost_guard import get_spend_summary
    from core.config import settings

    spend = await get_spend_summary()
    budget = settings.budget

    lines = [
        "LLM Budget:",
        f"  Today:  ${spend['today_usd']:.4f} / ${budget.get('daily_usd_hard_stop', 10):.2f}",
        f"  Month:  ${spend['month_usd']:.4f} / ${budget.get('monthly_usd_hard_stop', 50):.2f}",
        f"  Alert threshold: ${budget.get('daily_usd_alert', 2):.2f}/day",
    ]
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /skills
# ------------------------------------------------------------------

async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    rows = await memory_db.fetchall(
        "SELECT name, description, success_count, failure_count, works, last_used_at "
        "FROM skills_meta ORDER BY use_count DESC LIMIT 30"
    )
    if not rows:
        await _reply(update, "No skills loaded yet.")
        return

    lines = [f"Skills ({len(rows)}):"]
    for r in rows:
        status = "✅" if r["works"] else "⚠️"
        lines.append(
            f"{status} {r['name']} — ✓{r['success_count']} ✗{r['failure_count']}"
        )
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /skill [name] [disable|enable]
# ------------------------------------------------------------------

async def cmd_skill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    args = context.args or []
    if not args:
        await _reply(update, "Usage: /skill [name] [disable|enable]")
        return

    name = args[0]
    if len(args) >= 2 and args[1] in ("disable", "enable"):
        works = 1 if args[1] == "enable" else 0
        await memory_db.execute(
            "UPDATE skills_meta SET works=? WHERE name=?", (works, name)
        )
        await memory_db.commit()
        await _reply(update, f"Skill '{name}' {'enabled' if works else 'disabled'}.")
        return

    row = await memory_db.fetchone("SELECT * FROM skills_meta WHERE name=?", (name,))
    if not row:
        await _reply(update, f"Skill '{name}' not found.")
        return

    lines = [
        f"Skill: {row['name']}",
        f"Status: {'Active' if row['works'] else 'Disabled'}",
        f"Description: {row['description'] or '—'}",
        f"Tags: {row['tags'] or '—'}",
        f"Successes: {row['success_count']}  Failures: {row['failure_count']}  Used: {row['use_count']}",
        f"Last used: {row['last_used_at'] or 'never'}",
    ]
    if row["last_error"]:
        lines.append(f"Last error: {row['last_error'][:100]}")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /mode [beginner|expert]
# ------------------------------------------------------------------

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    args = context.args or []
    if not args or args[0] not in ("beginner", "expert"):
        await _reply(update, "Usage: /mode [beginner|expert]")
        return

    mode = args[0]
    await memory_db.execute(
        "DELETE FROM facts_fts WHERE rowid=(SELECT rowid FROM facts WHERE key='ux_mode')"
    )
    await memory_db.execute(
        "INSERT OR REPLACE INTO facts(key, value, source) VALUES ('ux_mode', ?, 'user')",
        (mode,),
    )
    await memory_db.execute(
        "INSERT INTO facts_fts(rowid, key, value) VALUES "
        "((SELECT rowid FROM facts WHERE key='ux_mode'), 'ux_mode', ?)",
        (mode,),
    )
    await memory_db.commit()
    await _reply(update, f"Switched to {mode} mode.")


# ------------------------------------------------------------------
# /accounts
# ------------------------------------------------------------------

async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    rows = await state_db.fetchall(
        "SELECT service, account_label, username, kind, source, last_used_at, session_file "
        "FROM credentials ORDER BY service, account_label"
    )
    if not rows:
        await _reply(update, "No saved accounts.")
        return

    lines = [f"Saved accounts ({len(rows)}):"]
    for r in rows:
        session = "📁 session" if r["session_file"] else "🔑 credential"
        last = r["last_used_at"][:10] if r["last_used_at"] else "never"
        lines.append(
            f"  {r['service']} / {r['account_label']} ({r['username'] or '?'}) "
            f"[{r['kind']}] {session} last:{last}"
        )
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /trust — recent injection detection log
# ------------------------------------------------------------------

async def cmd_trust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import journal_db

    rows = await journal_db.fetchall(
        "SELECT content, created_at FROM journal "
        "WHERE type='error' AND content LIKE '%injection%' "
        "ORDER BY id DESC LIMIT 10"
    )
    if not rows:
        await _reply(update, "No injection events logged.")
        return

    lines = [f"Recent injection flags ({len(rows)}):"]
    for r in rows:
        ts = r["created_at"][:16] if r["created_at"] else "?"
        lines.append(f"  {ts}: {r['content'][:120]}")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /rollback [n]
# ------------------------------------------------------------------

async def cmd_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    import os
    import signal
    import sys
    from core.config import settings

    args = context.args or []
    if not args or not args[0].isdigit():
        await _reply(update, "Usage: /rollback [n]  (reverts last N git commits)")
        return

    n = int(args[0])
    if n < 1:
        await _reply(update, "n must be a positive integer.")
        return

    aria_root = str(settings.aria_root)
    proc = await asyncio.create_subprocess_shell(
        f'git -C "{aria_root}" revert --no-edit HEAD~{n}..HEAD',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        await _reply(update, f"git revert failed:\n{stderr.decode()[:300]}")
        return

    await _reply(update, f"Reverted {n} commit(s). Restarting…")
    os.kill(os.getpid(), signal.SIGTERM)


# ------------------------------------------------------------------
# /why [task_id]
# ------------------------------------------------------------------

async def cmd_why(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db
    from core.llm_client import llm_call
    from core.config import settings

    args = context.args or []
    if not args:
        await _reply(update, "Usage: /why [task_id]")
        return

    task_id = args[0]
    steps = await state_db.fetchall(
        "SELECT step, role, content, tool_name, tool_result FROM agent_log "
        "WHERE task_id=? ORDER BY step ASC",
        (task_id,),
    )
    if not steps:
        await _reply(update, f"No log for task {task_id!r}.")
        return

    history_text = "\n".join(
        f"[{s['step']}] {s['role']}: {(s['content'] or s['tool_name'] or '')[:150]}"
        for s in steps
    )

    task_row = await state_db.fetchone("SELECT title FROM tasks WHERE id=?", (task_id,))
    title = task_row["title"] if task_row else task_id

    response = await llm_call(
        model=settings.get_model("light"),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Task goal: {title}\n\nAgent history:\n{history_text}\n\n"
                    "Narrate this agent task history as one clear plain-English paragraph. "
                    "What did the agent do, what worked, what didn't?"
                ),
            }
        ],
        max_tokens=300,
        task_id=task_id,
    )
    narration = response.choices[0].message.content or "Could not generate narration."
    await _reply(update, f"/why {task_id}:\n\n{narration}")


# ------------------------------------------------------------------
# /retry [task_id] [step]
# ------------------------------------------------------------------

async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import state_db

    args = context.args or []
    if len(args) < 1:
        await _reply(update, "Usage: /retry [task_id] [step]")
        return

    task_id = args[0]
    step = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    task_row = await state_db.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task_row:
        await _reply(update, f"Task {task_id!r} not found.")
        return

    # Re-enqueue from orchestrator
    from core.orchestrator import get_orchestrator
    orch = get_orchestrator()
    if orch:
        await orch.retry_task(task_id, step)
        await _reply(update, f"Task {task_id} re-queued from step {step or 'beginning'}.")
    else:
        await _reply(update, "Orchestrator not available.")


# ------------------------------------------------------------------
# /docs /doc forget [name]
# ------------------------------------------------------------------

async def cmd_docs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    rows = await memory_db.fetchall(
        "SELECT doc_name, COUNT(*) as chunks, MAX(ingested_at) as last_ingested "
        "FROM doc_chunks GROUP BY doc_name ORDER BY last_ingested DESC"
    )
    if not rows:
        await _reply(update, "No documents ingested. Use ingest_document tool.")
        return

    lines = [f"Ingested documents ({len(rows)}):"]
    for r in rows:
        ts = r["last_ingested"][:10] if r["last_ingested"] else "?"
        lines.append(f"  {r['doc_name']} ({r['chunks']} chunks, {ts})")
    lines.append("\nTo remove: /doc forget [name]")
    await _reply(update, "\n".join(lines))


async def cmd_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    args = context.args or []
    if len(args) < 2 or args[0] != "forget":
        await _reply(update, "Usage: /doc forget [document_name]")
        return

    doc_name = " ".join(args[1:])
    cursor = await memory_db.execute(
        "DELETE FROM doc_chunks WHERE doc_name=?", (doc_name,)
    )
    await memory_db.execute(
        "DELETE FROM doc_chunks_fts WHERE doc_name=?", (doc_name,)
    )
    await memory_db.commit()
    if cursor.rowcount:
        await _reply(update, f"Deleted {cursor.rowcount} chunks for '{doc_name}'.")
    else:
        await _reply(update, f"No document named '{doc_name}' found.")


# ------------------------------------------------------------------
# /session
# ------------------------------------------------------------------

async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    args = context.args or []
    if args:
        session_id = args[0]
        row = await memory_db.fetchone("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not row:
            await _reply(update, f"Session {session_id!r} not found.")
            return
        lines = [
            f"Session: {row['id']}",
            f"Started: {row['started_at']}",
            f"Ended: {row['ended_at'] or 'active'}",
            f"Intent: {row['intent_words'] or '—'}",
            f"Summary: {(row['summary'] or '—')[:200]}",
        ]
        await _reply(update, "\n".join(lines))
    else:
        rows = await memory_db.fetchall(
            "SELECT id, intent_words, started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT 10"
        )
        if not rows:
            await _reply(update, "No sessions found.")
            return
        lines = [f"Sessions ({len(rows)}):"]
        for r in rows:
            status = "active" if not r["ended_at"] else r["ended_at"][:10]
            lines.append(f"  [{r['id'][:8]}] {r['intent_words'] or '?'} ({status})")
        await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /logs [n]
# ------------------------------------------------------------------

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.config import settings

    log_file = settings.data_dir / "aria.log"
    if not log_file.exists():
        await _reply(update, "Log file not found.")
        return

    args = context.args or []
    n = int(args[0]) if args and args[0].isdigit() else 30

    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = "\n".join(lines[-n:])
    await _reply(update, f"Last {n} log lines:\n\n{tail[:3900]}")


# ------------------------------------------------------------------
# /dream
# ------------------------------------------------------------------

async def cmd_dream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import journal_db

    rows = await journal_db.fetchall(
        "SELECT content, created_at FROM journal "
        "WHERE type='system' AND tag='dream' ORDER BY id DESC LIMIT 10"
    )
    if not rows:
        await _reply(update, "No dream cycle entries found.")
        return

    last_ts = rows[0]["created_at"][:16] if rows else "never"
    lines = [f"Last dream cycle: {last_ts}\n"]
    for r in rows:
        ts = r["created_at"][:16] if r["created_at"] else "?"
        lines.append(f"  {ts}: {r['content'][:120]}")
    await _reply(update, "\n".join(lines))


# ------------------------------------------------------------------
# /backup
# ------------------------------------------------------------------

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import io
    import zipfile
    from core.config import settings

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for db_path in [settings.state_db, settings.memory_db, settings.journal_db]:
            if db_path.exists():
                zf.write(db_path, db_path.name)
        for skill_path in settings.skills_dir.glob("*.py"):
            zf.write(skill_path, f"skills/{skill_path.name}")

    buf.seek(0)
    from tg.notify import send_document

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    await send_document(
        settings.telegram_chat_id,
        document=buf,
        caption=f"ARIA backup {ts}",
        filename=f"aria_backup_{ts}.zip",
    )
    await _reply(update, f"Backup sent ({buf.tell() // 1024} KB).")


# ------------------------------------------------------------------
# /handover /captcha /webhooks
# ------------------------------------------------------------------

async def cmd_handover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import sys
    if sys.platform == "win32":
        await _reply(update, "VNC handover not available on Windows.")
        return
    from system.handover import HumanHandover
    await HumanHandover.send_handover_link()
    await _reply(update, "Handover link sent.")


async def cmd_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.database import memory_db

    args = context.args or []
    if not args or args[0] not in ("on", "off"):
        await _reply(update, "Usage: /captcha [on|off]")
        return
    enabled = "1" if args[0] == "on" else "0"
    await memory_db.execute(
        "INSERT OR REPLACE INTO facts(key, value, source) VALUES ('captcha_solver', ?, 'user')",
        (enabled,),
    )
    await memory_db.commit()
    await _reply(update, f"CAPTCHA solver {'enabled' if enabled == '1' else 'disabled'}.")


async def cmd_webhooks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from core.config import settings

    sources = list(settings.webhook_secrets.keys())
    ip = settings.tailscale_ip or "<not configured>"
    lines = [
        "Webhook receiver: port 8765",
        f"Tailscale URL: https://{ip}:8765/webhook/{{source}}",
        f"Registered sources: {', '.join(sources) or 'none'}",
    ]
    await _reply(update, "\n".join(lines))


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/briefing [on|off] [hour] — enable or disable the morning briefing.

    Examples:
      /briefing on        → enable at default 8am
      /briefing on 7      → enable at 7am
      /briefing off       → disable
      /briefing           → show current status
    """
    from core.database import memory_db

    args = context.args or []

    if not args:
        from memory.facts import FactsStore
        enabled = await FactsStore.get("morning_briefing_enabled")
        hour = await FactsStore.get("morning_briefing_hour") or "8"
        status = "enabled" if enabled == "1" else "disabled"
        await _reply(update, f"Morning briefing: {status} (at {hour}:00). Use /briefing on|off [hour].")
        return

    if args[0] == "off":
        await memory_db.execute(
            "INSERT OR REPLACE INTO facts(key, value, source) VALUES ('morning_briefing_enabled', '0', 'user')"
        )
        await memory_db.commit()
        await _reply(update, "Morning briefing disabled.")
        return

    if args[0] == "on":
        hour = "8"
        if len(args) > 1 and args[1].isdigit():
            hour = str(int(args[1]) % 24)

        await memory_db.execute(
            "INSERT OR REPLACE INTO facts(key, value, source) VALUES ('morning_briefing_enabled', '1', 'user')"
        )
        await memory_db.execute(
            "INSERT OR REPLACE INTO facts(key, value, source) VALUES ('morning_briefing_hour', ?, 'user')",
            (hour,),
        )
        await memory_db.commit()

        # Reschedule APScheduler job at the new hour
        try:
            from scheduler.cron import _scheduler
            from apscheduler.triggers.cron import CronTrigger

            if _scheduler:
                _scheduler.reschedule_job(
                    "sys_morning_briefing",
                    trigger=CronTrigger(hour=int(hour), minute=0),
                )
        except Exception:
            pass

        await _reply(update, f"Morning briefing enabled at {hour}:00 daily.")
        return

    await _reply(update, "Usage: /briefing [on|off] [hour]")


# ------------------------------------------------------------------
# Command handler registry — imported by telegram/bot.py
# ------------------------------------------------------------------

COMMAND_HANDLERS: dict[str, Any] = {
    "start": cmd_start,
    "help": cmd_help,
    "status": cmd_status,
    "tasks": cmd_tasks,
    "task": cmd_task,
    "stop": cmd_stop,
    "agents": cmd_agents,
    "journal": cmd_journal,
    "health": cmd_health,
    "memory": cmd_memory,
    "recall": cmd_recall,
    "approvals": cmd_approvals,
    "approve": cmd_approve,
    "deny": cmd_deny,
    "schedule": cmd_schedule,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "budget": cmd_budget,
    "skills": cmd_skills,
    "skill": cmd_skill,
    "mode": cmd_mode,
    "accounts": cmd_accounts,
    "trust": cmd_trust,
    "rollback": cmd_rollback,
    "why": cmd_why,
    "retry": cmd_retry,
    "docs": cmd_docs,
    "doc": cmd_doc,
    "session": cmd_session,
    "logs": cmd_logs,
    "dream": cmd_dream,
    "backup": cmd_backup,
    "handover": cmd_handover,
    "captcha": cmd_captcha,
    "webhooks": cmd_webhooks,
    "briefing": cmd_briefing,
}
