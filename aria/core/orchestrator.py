"""Main orchestrator — 5-second poll loop, message dispatch, streaming responses.

Message queue pattern (critical):
  Incoming Telegram messages go into an asyncio.Queue.
  _loop_tick() drains the queue each tick (non-blocking get_nowait()).
  Agent tasks are spawned as asyncio.create_task() — they run in the background
  and do NOT block the queue. ARIA can receive /status or new messages while
  a shell or browser task runs concurrently.

System prompt order (non-negotiable):
  1. Security block (trust level instructions)
  2. persona.md contents
  3. Available tools
  4. Available models
  5. Active task summary
  6. Memory context (injected LAST — LLMs attend more to content near end of context)
"""

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any

from core.config import settings
from core.context_builder import SECURITY_BLOCK, TrustLevel, tag, check_for_injection
from core.tasks import TaskRegistry

logger = logging.getLogger(__name__)

# Module-level singleton so /retry command can access it
_orchestrator_instance: "Orchestrator | None" = None


def get_orchestrator() -> "Orchestrator | None":
    return _orchestrator_instance


class Orchestrator:
    TICK_INTERVAL = 5.0       # seconds between loop ticks
    HEALTH_TICK_INTERVAL = 60  # health snapshot every 60 ticks (~5 min)

    def __init__(self, bot: Any) -> None:
        global _orchestrator_instance
        _orchestrator_instance = self

        self._bot = bot
        self._message_queue: asyncio.Queue = bot.message_queue
        self._running = True
        self._shutting_down = False
        self._budget_exceeded = False
        self._last_tick_time: float = time.time()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._tick_count = 0

    def stop(self) -> None:
        self._shutting_down = True
        self._running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        from core.database import state_db
        from tg.notify import send_text
        from core.approval import check_expired

        # Notify about interrupted tasks from last run
        count, titles = await TaskRegistry.count_interrupted()
        if count > 0:
            title_list = ", ".join(f"'{t}'" for t in titles[:5])
            await send_text(
                settings.telegram_chat_id,
                f"ARIA restarted. {count} task(s) were interrupted: {title_list}. "
                f"Say 'retry [title]' to requeue any of them.",
            )

        # First-run: ask about sub-agent models if not yet configured
        await self._maybe_ask_about_models()


        while self._running:
            try:
                self._last_tick_time = time.time()
                await self._loop_tick()
                await check_expired()
                self._tick_count += 1

                # Health snapshot every 60 ticks
                if self._tick_count % self.HEALTH_TICK_INTERVAL == 0:
                    asyncio.create_task(self._health_snapshot())

                # Budget reset at midnight
                self._maybe_reset_budget()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Orchestrator tick error")

            await asyncio.sleep(self.TICK_INTERVAL)

    async def _loop_tick(self) -> None:
        """Drain message queue + APScheduler job queue each tick."""
        from core.database import state_db

        # Check paused state
        row = await state_db.fetchone("SELECT value FROM paused WHERE key='paused'")
        paused = row and row["value"]

        # Drain Telegram message queue (non-blocking)
        if not paused:
            while True:
                try:
                    msg = self._message_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                asyncio.create_task(self._handle_message(msg))

        # Drain APScheduler job queue (always, even when paused — system jobs run regardless)
        try:
            from scheduler.cron import _job_queue
            if _job_queue:
                while True:
                    try:
                        job_desc = _job_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    asyncio.create_task(self._handle_scheduled_job(job_desc))
        except ImportError:
            pass

    async def _handle_scheduled_job(self, description: str) -> None:
        """Handle a fired APScheduler job by description."""
        from system.journal import log as journal_log

        # System maintenance jobs
        if description == "__system:health_snapshot":
            await self._health_snapshot()
        elif description == "__system:journal_compact":
            try:
                from system.journal import compact_old_entries
                await compact_old_entries()
            except Exception:
                logger.exception("Journal compact failed")
        elif description == "__system:browser_health":
            try:
                from system.health import HealthMonitor
                ok = await HealthMonitor.check_browser_health()
                await journal_log("system", f"Browser health check: {'ok' if ok else 'failed'}")
            except Exception:
                logger.exception("Browser health check failed")
        elif description == "__system:chromium_cleanup":
            try:
                from system.health import _cleanup_chromium_processes
                await _cleanup_chromium_processes()
            except Exception:
                logger.debug("Chromium cleanup skipped")
        elif description == "__system:dream_cycle":
            try:
                from system.dreamer import run_dream_cycle
                asyncio.create_task(run_dream_cycle(), name="dream_cycle")
            except Exception:
                logger.exception("Dream cycle launch failed")
        elif description == "__system:morning_briefing":
            asyncio.create_task(self._morning_briefing(), name="morning_briefing")
        else:
            # User-defined scheduled task — enqueue as a regular message
            from tg.bot import TextMessage
            await journal_log("system", f"Scheduled job fired: {description[:100]}")
            msg = TextMessage(text=description, update=None)
            await self._message_queue.put(msg)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: Any) -> None:
        from tg.bot import TextMessage, FileMessage
        from tg.notify import send_text

        if isinstance(msg, TextMessage):
            await self._handle_text_message(msg)
        elif isinstance(msg, FileMessage):
            await self._handle_file_message(msg)

    async def _handle_text_message(self, msg: Any) -> None:
        from tg.notify import send_text
        from core.router import classify, decompose_task, is_continuation_request
        from system.journal import log as journal_log

        text = msg.text
        await journal_log("message_in", text)

        # Check if this is a reply to the first-run model onboarding question
        if await self._handle_models_onboarding_reply(text):
            return  # consumed — don't route as a normal message

        # Detect "continue from last time" — inject active session context
        if is_continuation_request(text):
            try:
                from memory.sessions import SessionStore
                session_ctx = await SessionStore.get_active_context(text)
                if session_ctx:
                    text = f"{text}\n\n[Active session context: {session_ctx}]"
            except Exception:
                pass

        classification = await classify(text)
        kind = classification.get("kind", "conversation")
        mode = classification.get("mode", "conversation")
        title = classification.get("title", text[:60])
        complexity = classification.get("complexity", "simple")

        if kind == "conversation" or kind == "meta":
            # Direct LLM response (streamed)
            await self._stream_conversational_response(text, msg)
            return

        if self._budget_exceeded:
            await send_text(
                settings.telegram_chat_id,
                "⛔ Daily LLM budget exhausted — no new tasks until tomorrow.",
            )
            return

        # Create task and dispatch
        task_id = await TaskRegistry.create(title=title, kind=kind, mode=mode)

        if kind == "mixed" or complexity == "complex":
            # Decompose into subtasks
            subtasks = await decompose_task(text)
            await send_text(
                settings.telegram_chat_id,
                f"Task [{task_id}] decomposed into {len(subtasks)} subtask(s). Starting…",
            )
            asyncio.create_task(
                self._run_sequential_subtasks(task_id, title, subtasks, text),
                name=f"task_{task_id}",
            )
        else:
            await send_text(
                settings.telegram_chat_id,
                f"On it. [{task_id}] {title}",
            )
            asyncio.create_task(
                self._dispatch_single_task(task_id, text, mode),
                name=f"task_{task_id}",
            )

    async def _handle_file_message(self, msg: Any) -> None:
        from tg.notify import send_text
        from system.journal import log as journal_log

        await journal_log("message_in", f"File received: {msg.mime_type}, prompt={msg.prompt!r}")
        prompt = msg.prompt or "Analyze this file."
        await send_text(
            settings.telegram_chat_id,
            f"File received ({msg.mime_type}). Processing: {prompt}",
        )
        # TODO Phase 3: ingest_document for documents, vision for images
        await send_text(
            settings.telegram_chat_id,
            "File ingestion not yet implemented — coming in Phase 3.",
        )

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def _dispatch_single_task(self, task_id: str, objective: str, mode: str) -> None:
        from agents.specs import spec_for_mode
        from agents.worker import WorkerAgent
        from tg.notify import send_text
        from core.skill_extractor import SkillExtractor
        from core.tasks import TaskRegistry
        from system.journal import log as journal_log

        # Get task metadata for session/history tagging
        task_row = await TaskRegistry.get(task_id)
        kind = task_row["kind"] if task_row else mode
        title = task_row["title"] if task_row else objective[:60]

        try:
            spec = spec_for_mode(mode, objective)
            worker = WorkerAgent(spec, task_id)
            result = await worker.run()

            await send_text(settings.telegram_chat_id, f"✅ [{task_id}] {result[:300]}")
            await journal_log("message_out", result, task_id=task_id)

            # Post-task self-reflection → sets verdict on task row
            await self._reflect_on_task(task_id, objective)

            # Write to history store
            task_row2 = await TaskRegistry.get(task_id)
            verdict = task_row2["verdict"] if task_row2 else None
            asyncio.create_task(
                self._record_to_memory(task_id, title, result, verdict, kind),
                name=f"memory_{task_id}",
            )

            # Auto-retry on PARTIAL/FAILURE (catches silent failures)
            if verdict in ("PARTIAL", "FAILURE"):
                asyncio.create_task(
                    self._enqueue_retry(task_id, title, objective, mode, verdict),
                    name=f"retry_{task_id}",
                )

            # Update skill success/failure counts for any skills that were active
            asyncio.create_task(
                self._update_skill_counts(objective, verdict),
                name=f"skill_counts_{task_id}",
            )

            # Skill extraction (async, non-blocking)
            asyncio.create_task(
                SkillExtractor.maybe_extract(task_id, objective),
                name=f"skill_extract_{task_id}",
            )

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            await send_text(
                settings.telegram_chat_id,
                f"❌ [{task_id}] Task failed: {exc}",
            )

    async def _run_sequential_subtasks(
        self,
        parent_task_id: str,
        parent_title: str,
        subtasks: list[dict],
        original_message: str,
    ) -> None:
        from agents.specs import spec_for_mode
        from agents.worker import WorkerAgent, WORKER_SEMAPHORE
        from tg.notify import send_text
        from core.tasks import TaskRegistry

        results = []
        for i, st in enumerate(subtasks):
            st_objective = st.get("description", original_message)
            if results:
                st_objective += f"\n\nPrevious subtask result: {results[-1][:300]}"

            st_kind = st.get("kind", "shell")
            st_id = await TaskRegistry.create(
                title=f"{parent_title} [{i+1}/{len(subtasks)}]",
                kind=st_kind,
                description=st_objective,
            )

            async with WORKER_SEMAPHORE:
                spec = spec_for_mode(f"{st_kind}_mode", st_objective)
                worker = WorkerAgent(spec, st_id)
                try:
                    result = await worker.run()
                    results.append(result)
                except Exception as exc:
                    await send_text(
                        settings.telegram_chat_id,
                        f"❌ Subtask {i+1}/{len(subtasks)} failed: {exc}",
                    )
                    results.append(f"[failed: {exc}]")

        await send_text(
            settings.telegram_chat_id,
            f"✅ [{parent_task_id}] All {len(subtasks)} subtasks complete.",
        )
        await TaskRegistry.update(parent_task_id, status="done")

    # ------------------------------------------------------------------
    # Streaming conversational response
    # ------------------------------------------------------------------

    async def _build_conversational_system_prompt(self, message: str) -> str:
        """Stripped-down system prompt for conversational replies.

        Omits tool descriptions on purpose — the conversational streaming call
        does not pass tools= to the API, so the model would output tool-call
        reasoning as plain text if it sees tool descriptions. Memory and persona
        are still included.
        """
        persona_path = settings.aria_root / "config" / "persona.md"
        persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

        memory_context = ""
        try:
            from memory.retriever import MemoryRetriever
            memory_context = await MemoryRetriever.build_context(message)
        except ImportError:
            pass

        parts = [SECURITY_BLOCK, persona, memory_context]
        return "\n\n".join(p for p in parts if p)

    async def _stream_conversational_response(self, text: str, msg: Any) -> None:
        from tg.notify import send_text
        from system.journal import log as journal_log
        from core.llm_client import get_client, _record_usage

        # Use conversational prompt — no tool descriptions to avoid reasoning bleed
        system_prompt = await self._build_conversational_system_prompt(text)
        client = get_client()

        # Send initial placeholder
        try:
            sent_msg = await self._bot.app.bot.send_message(
                settings.telegram_chat_id, "⏳"
            )
        except Exception:
            sent_msg = None

        buffer = ""
        last_edit = 0.0

        try:
            from core.cost_guard import check as budget_check
            await budget_check()

            async with await client.chat.completions.create(
                model=settings.get_model("heavy"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": tag(text, TrustLevel.OWNER)},
                ],
                max_tokens=1000,
                stream=True,
            ) as stream:
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content or "" if chunk.choices else ""
                    buffer += delta
                    now = asyncio.get_event_loop().time()
                    if sent_msg and now - last_edit > 1.0:
                        try:
                            await self._bot.app.bot.edit_message_text(
                                (buffer + "▌")[:4096],
                                settings.telegram_chat_id,
                                sent_msg.message_id,
                            )
                            last_edit = now
                        except Exception:
                            pass

            # Final edit — remove cursor, truncate to Telegram limit
            if sent_msg and buffer:
                try:
                    await self._bot.app.bot.edit_message_text(
                        buffer[:4096],
                        settings.telegram_chat_id,
                        sent_msg.message_id,
                    )
                    # If response was longer than 4096, send the rest as follow-up
                    if len(buffer) > 4096:
                        for i in range(4096, len(buffer), 4096):
                            await send_text(settings.telegram_chat_id, buffer[i:i+4096])
                except Exception:
                    await send_text(settings.telegram_chat_id, buffer[:4096])
            elif buffer:
                await send_text(settings.telegram_chat_id, buffer[:4096])

            await journal_log("message_out", buffer[:500])

        except Exception:
            logger.exception("Streaming response failed")
            await send_text(
                settings.telegram_chat_id,
                "Sorry, I encountered an error generating a response.",
            )

    # ------------------------------------------------------------------
    # System prompt builder
    # ------------------------------------------------------------------

    async def _build_system_prompt(self, message: str) -> str:
        from core.tools import get_all_tools

        persona_path = settings.aria_root / "config" / "persona.md"
        persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

        tool_descriptions = "\n".join(
            f"- {t['name']}: {t['description']}"
            for t in get_all_tools().values()
        )

        model_descriptions = "\n".join(
            f"- {m['id']} ({m.get('tier', '?')}): {m.get('good_for', '')}"
            for m in settings._models_yaml.get("models", [])
        )

        running = await TaskRegistry.list_running()
        active_summary = ""
        if running:
            active_summary = "Active tasks:\n" + "\n".join(
                f"- [{t['id']}] {t['title']}" for t in running
            )

        # Memory context — injected LAST (closest to user message)
        memory_context = ""
        try:
            from memory.retriever import MemoryRetriever
            memory_context = await MemoryRetriever.build_context(message)
        except ImportError:
            pass  # Phase 3+ — not yet built

        parts = [
            SECURITY_BLOCK,
            persona,
            f"Available tools:\n{tool_descriptions}" if tool_descriptions else "",
            f"Available models:\n{model_descriptions}" if model_descriptions else "",
            active_summary,
            memory_context,  # LAST
        ]
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Post-task memory recording
    # ------------------------------------------------------------------

    async def _record_to_memory(
        self,
        task_id: str,
        title: str,
        result: str,
        verdict: str | None,
        kind: str | None,
    ) -> None:
        """Write completed task to HistoryStore and log it to the active Session."""
        try:
            from memory.history import HistoryStore
            from memory.sessions import SessionStore

            await HistoryStore.add(
                task_id=task_id,
                title=title,
                summary=result[:300] if result else None,
                outcome=verdict or "SUCCESS",
                tags=kind or "",
            )
            session_id = await SessionStore.get_or_create(title)
            await SessionStore.log_task(session_id, task_id)
        except Exception:
            logger.debug("Failed to record task %s to memory", task_id, exc_info=True)

    # ------------------------------------------------------------------
    # Post-task self-reflection
    # ------------------------------------------------------------------

    async def _reflect_on_task(self, task_id: str, objective: str) -> None:
        from core.llm_client import llm_call
        from core.tasks import TaskRegistry
        from system.journal import log as journal_log

        try:
            steps = await TaskRegistry.get_steps(task_id)
            if not steps:
                return

            recent = steps[-5:]
            steps_text = "\n".join(
                f"[{s['step']}] {s['role']}: {(s['content'] or s['tool_name'] or '')[:150]}"
                for s in recent
            )

            response = await llm_call(
                model=settings.get_model("light"),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Task goal: {objective}\n\nLast steps:\n{steps_text}\n\n"
                            "Reply with exactly one of: SUCCESS / PARTIAL / FAILURE\n"
                            "Then one sentence explaining why."
                        ),
                    }
                ],
                max_tokens=80,
                task_id=task_id,
            )
            verdict_text = (response.choices[0].message.content or "").strip()
            verdict = verdict_text.split("\n")[0].split()[0].upper()
            if verdict not in ("SUCCESS", "PARTIAL", "FAILURE"):
                verdict = "SUCCESS"

            await TaskRegistry.update(task_id, verdict=verdict)
            await journal_log("system", f"Verdict: {verdict_text[:200]}", task_id=task_id)

        except Exception:
            logger.debug("Self-reflection failed for task %s", task_id, exc_info=True)

    # ------------------------------------------------------------------
    # Skill count updates
    # ------------------------------------------------------------------

    async def _update_skill_counts(self, objective: str, verdict: str | None) -> None:
        """Update success/failure counts for skills that were relevant to this task.

        Uses SkillsMetaStore.format_for_prompt() to find which skills were active
        (same query the retriever uses), then records success or failure on each.
        Partial/Failure verdicts increment failure_count; success increments success_count.
        """
        try:
            from memory.skills_meta import SkillsMetaStore
            from core.database import memory_db

            # Find skill names that were injected for this objective
            skills_text = await SkillsMetaStore.format_for_prompt(objective)
            if not skills_text:
                return

            # Parse skill names from the formatted text — each skill line starts with "# "
            skill_names = [
                line[2:].strip()
                for line in skills_text.splitlines()
                if line.startswith("# ")
            ]

            is_failure = verdict in ("PARTIAL", "FAILURE")
            for name in skill_names:
                try:
                    if is_failure:
                        await SkillsMetaStore.record_failure(name, f"verdict={verdict}")
                    else:
                        await SkillsMetaStore.record_success(name)
                except Exception:
                    pass
        except Exception:
            logger.debug("_update_skill_counts failed", exc_info=True)

    # ------------------------------------------------------------------
    # Auto-retry on verdict PARTIAL / FAILURE
    # ------------------------------------------------------------------

    async def _enqueue_retry(
        self,
        original_task_id: str,
        title: str,
        objective: str,
        mode: str,
        verdict: str,
    ) -> None:
        """Notify the user and re-queue a task that self-reflected as PARTIAL or FAILURE.

        Creates a new TaskRegistry entry so the retry has its own task_id, steps,
        and agent_log. The original task row is kept intact with its verdict.
        Only one auto-retry is issued per original task — if the retry also fails,
        ARIA surfaces it but does not create an infinite retry loop.
        """
        from core.config import settings
        from core.tasks import TaskRegistry
        from tg.notify import send_text
        from system.journal import log as journal_log

        try:
            # Guard: don't retry a task that is itself already a retry
            orig_row = await TaskRegistry.get(original_task_id)
            if orig_row and "auto-retry" in (orig_row.get("title") or ""):
                logger.info("Skipping retry of retry task %s", original_task_id)
                return

            retry_title = f"[auto-retry] {title}"[:120]
            retry_task_id = await TaskRegistry.create(
                title=retry_title,
                kind=mode,
                mode=mode,
            )

            await journal_log(
                "system",
                f"Auto-retry queued for {original_task_id} (verdict={verdict}): {retry_task_id}",
                task_id=original_task_id,
            )

            emoji = "🔁" if verdict == "PARTIAL" else "⚠️"
            await send_text(
                settings.telegram_chat_id,
                f"{emoji} Task '{title}' returned {verdict}. Auto-retrying as [{retry_task_id}].",
            )

            # Re-dispatch via the normal path (non-blocking)
            asyncio.create_task(
                self._dispatch_single_task(
                    task_id=retry_task_id,
                    objective=f"[Retry after {verdict}] {objective}",
                    mode=mode,
                ),
                name=f"dispatch_{retry_task_id}",
            )
        except Exception:
            logger.exception("_enqueue_retry failed for task %s", original_task_id)

    # ------------------------------------------------------------------
    # Morning briefing
    # ------------------------------------------------------------------

    async def _morning_briefing(self) -> None:
        """Send the morning briefing to the user.

        Only fires if fact 'morning_briefing_enabled' == '1'.
        Sections (in order):
          1. Pending approvals count + titles (direct SQLite)
          2. Dream cycle flags from last night (journal read)
          3. Today's scheduled tasks (schedule table)
          4. One-line health snapshot (last health row)
          5. Yesterday's task summary (one fast LLM call)
        """
        try:
            from memory.facts import FactsStore
            enabled = await FactsStore.get("morning_briefing_enabled")
            if enabled != "1":
                return  # Off by default — skip silently
        except Exception:
            return

        try:
            from core.config import settings
            from core.database import state_db, journal_db
            from tg.notify import send_text
            import datetime

            lines: list[str] = ["🌅 *Good morning! Here's your briefing:*\n"]

            # 1. Pending approvals
            try:
                rows = await state_db.fetchall(
                    "SELECT description FROM approvals WHERE status='pending' ORDER BY requested_at"
                )
                if rows:
                    titles = ", ".join(r["description"][:40] for r in rows[:5])
                    lines.append(f"📋 *Pending approvals ({len(rows)}):* {titles}")
                else:
                    lines.append("📋 No pending approvals")
            except Exception:
                pass

            # 2. Dream cycle flags (journal entries tagged 'dream' from last night)
            try:
                cutoff = (datetime.datetime.now() - datetime.timedelta(hours=10)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                dream_rows = await journal_db.fetchall(
                    "SELECT content FROM journal WHERE type='system' AND tag='dream' "
                    "AND timestamp > ? ORDER BY timestamp DESC LIMIT 5",
                    (cutoff,),
                )
                if dream_rows:
                    dream_summary = "; ".join(r["content"][:60] for r in dream_rows)
                    lines.append(f"🌙 *Last night's dream cycle:* {dream_summary}")
            except Exception:
                pass

            # 3. Today's scheduled tasks
            try:
                sched_rows = await state_db.fetchall(
                    "SELECT description, trigger FROM schedule ORDER BY description LIMIT 5"
                )
                if sched_rows:
                    tasks_str = ", ".join(
                        f"{r['description'][:30]} ({r['trigger']})" for r in sched_rows
                    )
                    lines.append(f"📅 *Scheduled today:* {tasks_str}")
            except Exception:
                pass

            # 4. Health snapshot (most recent row)
            try:
                h = await state_db.fetchone(
                    "SELECT cpu_pct, ram_pct, disk_pct, temp_c FROM health ORDER BY timestamp DESC LIMIT 1"
                )
                if h:
                    temp_str = f" | 🌡️{h['temp_c']:.0f}°C" if h["temp_c"] else ""
                    lines.append(
                        f"💻 *Health:* CPU {h['cpu_pct']:.0f}% | "
                        f"RAM {h['ram_pct']:.0f}% | Disk {h['disk_pct']:.0f}%{temp_str}"
                    )
            except Exception:
                pass

            # 5. Yesterday summary — one fast LLM call
            try:
                yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
                task_rows = await state_db.fetchall(
                    "SELECT title FROM tasks WHERE status='done' AND date(updated_at)=? LIMIT 10",
                    (yesterday,),
                )
                if task_rows:
                    titles_str = ", ".join(r["title"][:40] for r in task_rows)
                    from core.llm_client import llm_call
                    from core.config import settings as s

                    resp = await llm_call(
                        model=s.get_model("light"),
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    f"Summarize in one sentence: yesterday ARIA completed these tasks: {titles_str}"
                                ),
                            }
                        ],
                        max_tokens=80,
                    )
                    summary = resp.choices[0].message.content.strip()
                    lines.append(f"✅ *Yesterday:* {summary}")
                else:
                    lines.append("✅ *Yesterday:* No tasks completed")
            except Exception:
                logger.debug("Morning briefing LLM summary failed", exc_info=True)

            briefing = "\n".join(lines)
            await send_text(settings.telegram_chat_id, briefing, parse_mode="Markdown")

        except Exception:
            logger.exception("Morning briefing failed")

    # ------------------------------------------------------------------
    # Health snapshot
    # ------------------------------------------------------------------

    async def _health_snapshot(self) -> None:
        try:
            from system.health import HealthMonitor
            await HealthMonitor.snapshot()
        except Exception:
            logger.debug("Health snapshot failed", exc_info=True)

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def _maybe_reset_budget(self) -> None:
        import datetime
        from core.cost_guard import reset_for_new_day
        # Reset at each new day (checked every tick, idempotent)
        today = datetime.date.today().isoformat()
        if getattr(self, "_last_budget_date", None) != today:
            self._last_budget_date = today
            reset_for_new_day()
            self._budget_exceeded = False

    # ------------------------------------------------------------------
    # First-run model onboarding
    # ------------------------------------------------------------------

    async def _maybe_ask_about_models(self) -> None:
        """On first startup, ask user if they want to add sub-agent models.

        Fires once — after the user replies, the response is parsed with the LLM
        and models.yaml is updated. The fact 'models_onboarding_done' is set so
        this never fires again.
        """
        try:
            from memory.facts import FactsStore
            done = await FactsStore.get("models_onboarding_done")
            if done:
                return
        except Exception:
            return

        try:
            from tg.notify import send_text
            model_id = settings.get_model("heavy")
            msg = (
                f"👋 ARIA is ready!\n\n"
                f"I'm currently using **{model_id}** for everything — all reasoning, "
                f"planning, browsing, and shell tasks.\n\n"
                f"Would you like to add more models for specific tasks? "
                f"For example, a faster/cheaper model for simple routing and summaries, "
                f"or a specialised model for coding.\n\n"
                f"Just describe each one — tell me the model ID and what it's good and bad at. "
                f"I'll configure them automatically.\n\n"
                f"Or reply **skip** to use {model_id} for everything."
            )
            await send_text(settings.telegram_chat_id, msg, parse_mode="Markdown")

            # Flag that we've sent the question — the reply will be handled by
            # the normal message handler which checks this pending flag
            try:
                from memory.facts import FactsStore
                await FactsStore.upsert("models_onboarding_pending", "1", source="aria")
            except Exception:
                pass
        except Exception:
            logger.debug("_maybe_ask_about_models failed", exc_info=True)

    async def _handle_models_onboarding_reply(self, text: str) -> bool:
        """Called from _handle_message when models_onboarding_pending==1,
        OR when the user sends a JSON block containing a 'models' array.

        Returns True if the message was consumed as a model-config reply.
        """
        import re as _re
        try:
            from memory.facts import FactsStore
            pending = await FactsStore.get("models_onboarding_pending")
        except Exception:
            return False

        # Also catch direct JSON model configs sent at any time
        is_model_json = False
        try:
            json_match = _re.search(r'\{[\s\S]*?"models"[\s\S]*?\}', text)
            if json_match:
                _data = json.loads(json_match.group())
                if isinstance(_data.get("models"), list) and _data["models"]:
                    is_model_json = True
        except Exception:
            pass

        if pending != "1" and not is_model_json:
            return False

        try:
            from tg.notify import send_text
            from core.llm_client import llm_call
            import yaml
            from pathlib import Path

            # Clear the pending flag first
            await FactsStore.upsert("models_onboarding_pending", "0", source="aria")
            await FactsStore.upsert("models_onboarding_done", "1", source="aria")

            if text.strip().lower() in ("skip", "no", "nope", "n", "no thanks"):
                await send_text(
                    settings.telegram_chat_id,
                    f"Got it — using {settings.get_model('heavy')} for everything. "
                    f"You can add more models any time by saying "
                    f"\"add a model\" or editing config/models.yaml.",
                )
                return True

            # Ask the LLM to parse the user's model descriptions into structured yaml
            existing_model = settings.get_model("heavy")

            parse_prompt = f"""The user wants to configure additional AI models for ARIA.
Current orchestrator model: {existing_model} (tier: heavy)

User's description:
{text}

Parse this into a YAML models list. Each model needs:
  - id: (exact model ID the user gave)
  - tier: light (for fast/cheap models) or heavy (for capable models)
  - good_for: short description of strengths (quoted string)
  - compress_at: 100000
  - context_limit: 128000

Rules:
- If the user describes a model as fast, cheap, small, or for simple tasks → tier: light
- If the user describes it as smart, capable, for complex tasks → tier: heavy
- The existing model {existing_model} is already configured — do NOT include it again
- Output ONLY valid YAML for the models list (just the list items, no outer key)
- If you cannot parse any valid models, output exactly: NONE

Example output:
- id: meta-llama/llama-3.1-8b-instruct
  tier: light
  good_for: "fast routing, summaries, simple Q&A"
  compress_at: 100000
  context_limit: 128000"""

            response = await llm_call(
                model=existing_model,
                messages=[{"role": "user", "content": parse_prompt}],
                max_tokens=500,
            )
            parsed = (response.choices[0].message.content or "").strip()

            if parsed.upper() == "NONE" or not parsed:
                await send_text(
                    settings.telegram_chat_id,
                    "I couldn't parse any model details from that. "
                    "You can add models later by saying \"add model [id] — good for [description]\".",
                )
                return True

            # Load existing models.yaml and append new models
            models_path = Path(settings.aria_root) / "config" / "models.yaml"
            existing = yaml.safe_load(models_path.read_text(encoding="utf-8")) or {}
            existing_models = existing.get("models", [])

            try:
                new_models = yaml.safe_load(parsed)
                if isinstance(new_models, list):
                    existing_models.extend(new_models)
                    existing["models"] = existing_models
                    models_path.write_text(
                        yaml.dump(existing, default_flow_style=False, allow_unicode=True),
                        encoding="utf-8",
                    )
                    settings.reload_models()

                    names = ", ".join(m.get("id", "?") for m in new_models)
                    await send_text(
                        settings.telegram_chat_id,
                        f"✅ Added {len(new_models)} model(s): {names}\n\n"
                        f"I'll use lighter models for fast tasks and the orchestrator "
                        f"for anything complex. You can see the config in config/models.yaml.",
                    )
                else:
                    raise ValueError("not a list")
            except Exception as e:
                logger.debug("Model yaml parse failed: %s\nRaw: %s", e, parsed)
                await send_text(
                    settings.telegram_chat_id,
                    "I had trouble parsing the model config. "
                    "You can edit config/models.yaml directly or try again.",
                )

            return True

        except Exception:
            logger.exception("_handle_models_onboarding_reply failed")
            return False

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    async def watchdog(self) -> None:
        """Auto-restart if the event loop stalls for 3+ minutes."""
        while True:
            try:
                await asyncio.sleep(120)
                if time.time() - self._last_tick_time > 180:
                    from tg.notify import send_text
                    await send_text(
                        settings.telegram_chat_id,
                        "⚠️ Orchestrator hung for 3+ minutes — restarting",
                    )
                    os.kill(os.getpid(), signal.SIGTERM)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Watchdog error")

    # ------------------------------------------------------------------
    # /retry support
    # ------------------------------------------------------------------

    async def retry_task(self, task_id: str, step: int | None = None) -> None:
        """Re-dispatch a task, optionally from a specific step checkpoint."""
        from core.tasks import TaskRegistry
        from agents.base import WorkerSpec
        from agents.worker import WorkerAgent

        task = await TaskRegistry.get(task_id)
        if not task:
            return

        objective = task["description"] or task["title"]
        mode = task["mode"] or "shell"

        if step is not None:
            snapshot = await TaskRegistry.get_step_snapshot(task_id, step)
            if snapshot:
                # Restore from checkpoint
                from agents.specs import spec_for_mode
                spec = spec_for_mode(f"{mode}_mode", objective)
                worker = WorkerAgent(spec, task_id)
                worker._step = step
                # Restore scratch
                for k, v in (snapshot.get("scratch") or {}).items():
                    await TaskRegistry.write_scratch(task_id, k, v)
                logger.info("Retrying task %s from step %d", task_id, step)
                asyncio.create_task(worker.run(), name=f"retry_{task_id}")
                return

        # Full restart
        await TaskRegistry.update(task_id, status="queued")
        asyncio.create_task(
            self._dispatch_single_task(task_id, objective, mode),
            name=f"retry_{task_id}",
        )
