"""WorkerAgent — the single runtime that executes any WorkerSpec.

No separate BrowserAgent or ShellAgent classes exist. Those are convenience
specs in agents/specs.py. This class is the only agent runtime.

Architecture:
- Filters the tool registry to spec.allowed_tools — unknown tool calls return an error
- Runs a ReAct loop using OpenAI function calling (not free-form JSON parsing)
- At the top of each iteration: compresses context + loads fresh scratch
- Saves step snapshots after each step for /retry
- Acquires SHELL_SEMAPHORE for shell-type workers
"""

import asyncio
import json
import logging
from typing import Any

from agents.base import BaseAgent, WorkerSpec
from core.config import settings
from core.tasks import TaskRegistry
from core.tools import get_tools_for_worker, build_openai_tools_spec

logger = logging.getLogger(__name__)

# Shell concurrency cap — max 3 shell tasks running simultaneously
SHELL_SEMAPHORE = asyncio.Semaphore(3)

# Worker concurrency cap — max 4 workers total
WORKER_SEMAPHORE = asyncio.Semaphore(4)

# Vision: if a tool result looks like a base64 PNG, use the smart model for that step
_PNG_PREFIX = "data:image/png;base64,"


class WorkerAgent(BaseAgent):
    """Executes a WorkerSpec using a ReAct loop with structured function calling."""

    def __init__(self, spec: WorkerSpec, task_id: str) -> None:
        super().__init__(spec, task_id)
        self._tools = get_tools_for_worker(spec.allowed_tools)
        self._tools_spec = build_openai_tools_spec(self._tools)
        self._is_shell = any(t in spec.allowed_tools for t in ("run_shell_command", "execute_python"))
        self._last_tool: str | None = None
        self._last_result: str | None = None

    async def run(self) -> str:
        """Execute the spec. Returns result string. Updates task status throughout."""
        from core.llm_client import llm_call
        from system.journal import log as journal_log

        await TaskRegistry.update(self.task_id, status="running")

        sem = SHELL_SEMAPHORE if self._is_shell else None
        if sem:
            if sem.locked() and sem._value == 0:
                # Report queue position before waiting
                queued = [t for t in asyncio.all_tasks() if hasattr(t, "_aria_shell")]
                logger.info("Shell semaphore full — task %s queued", self.task_id)

        async def _inner():
            model_id = settings.get_model("heavy")
            messages = self._build_initial_messages()

            await journal_log("action", f"Starting: {self.spec.objective}", task_id=self.task_id)

            for step in range(self.spec.step_budget):
                self._step = step

                # Compress if approaching context limit
                messages = await self._compress_if_needed(messages)

                # Reload scratch fresh each step
                scratch = await TaskRegistry.read_scratch(self.task_id)
                if scratch:
                    scratch_block = "Current scratch:\n" + json.dumps(scratch, indent=2)
                    # Inject as last system message before the current exchange
                    # Replace any existing scratch injection to avoid duplication
                    messages = [m for m in messages if not m.get("_scratch_injection")]
                    scratch_msg = {
                        "role": "system",
                        "content": scratch_block,
                        "_scratch_injection": True,
                    }
                    # Insert just before the last user message
                    messages.append(scratch_msg)

                # Check for cancellation
                if await TaskRegistry.is_cancelled(self.task_id):
                    logger.info("Task %s cancelled — stopping", self.task_id)
                    return "Task cancelled."

                # Choose model: upgrade to smart if last result was an image
                step_model = model_id
                if self._last_result and self._last_result.startswith(_PNG_PREFIX):
                    step_model = settings.get_model("heavy")

                # Remove internal flags before sending to LLM
                clean_messages = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]

                try:
                    response = await llm_call(
                        model=step_model,
                        messages=clean_messages,
                        tools=self._tools_spec if self._tools_spec else None,
                        tool_choice="auto" if self._tools_spec else None,
                        max_tokens=2000,
                        task_id=self.task_id,
                    )
                except Exception as exc:
                    logger.exception("LLM call failed in step %d", step)
                    await TaskRegistry.update(self.task_id, status="failed")
                    raise

                choice = response.choices[0]
                msg = choice.message

                # Log assistant message
                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])],
                })

                await TaskRegistry.log_step(
                    task_id=self.task_id,
                    step=step,
                    role="assistant",
                    content=msg.content,
                    tool_name=msg.tool_calls[0].function.name if msg.tool_calls else None,
                    tool_args=json.loads(msg.tool_calls[0].function.arguments) if msg.tool_calls else None,
                )

                # Finished?
                if choice.finish_reason == "stop" and not msg.tool_calls:
                    result = msg.content or "Done."
                    await journal_log("action", f"Completed: {result[:200]}", task_id=self.task_id)
                    await TaskRegistry.update(self.task_id, status="done")
                    await TaskRegistry.clear_scratch(self.task_id)
                    return result

                # Tool calls
                if msg.tool_calls:
                    tool_results = await self._dispatch_tools(msg.tool_calls)

                    # Add tool results to messages
                    for tc_id, tool_name, result_str in tool_results:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": tool_name,
                            "content": result_str,
                        })
                        self._last_tool = tool_name
                        self._last_result = result_str

                    # Save snapshot after each tool execution
                    scratch_now = await TaskRegistry.read_scratch(self.task_id)
                    await self._save_snapshot(
                        messages=clean_messages,
                        scratch=scratch_now,
                        last_tool=self._last_tool,
                        last_result=self._last_result,
                    )

            # Step budget exhausted
            await journal_log("error", f"Step budget exhausted after {self.spec.step_budget} steps", task_id=self.task_id)
            await TaskRegistry.update(self.task_id, status="failed", verdict="PARTIAL")
            return f"Step budget exhausted ({self.spec.step_budget} steps)."

        try:
            if sem:
                async with sem:
                    return await _inner()
            else:
                return await _inner()
        except asyncio.CancelledError:
            await TaskRegistry.update(self.task_id, status="cancelled")
            raise
        except Exception as exc:
            logger.exception("WorkerAgent failed for task %s", self.task_id)
            await TaskRegistry.update(self.task_id, status="failed")
            raise

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_initial_messages(self) -> list[dict]:
        from core.context_builder import SECURITY_BLOCK, TrustLevel, tag

        persona_path = settings.aria_root / "config" / "persona.md"
        persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

        tool_list = "\n".join(
            f"- {t['name']}: {t['description']}"
            for t in self._tools.values()
        )

        mode_config = settings.agent_modes.get(
            next((m for m in settings.agent_modes if m != "conversation"), ""), {}
        )
        mode_addendum = mode_config.get("system_prompt_addendum", "") if mode_config else ""

        constraints_text = ""
        if self.spec.constraints:
            constraints_text = "\n\nConstraints:\n" + "\n".join(f"- {c}" for c in self.spec.constraints)

        agent_rules = """## Agent Execution Rules

You are in autonomous agent mode. These rules are absolute:

1. USE TOOLS IMMEDIATELY. Do not explain what you are about to do. Call the tool.
2. NEVER output reasoning as text. Your thoughts go in `think` calls, not in messages.
3. NEVER say "I will now...", "Let me...", "First I'll...", "I need to...". Just act.
4. NEVER ask the user for confirmation unless the task is physically impossible to attempt without it.
5. When done, send ONE concise result message: what you did + outcome. Nothing else.
6. If a tool fails, try an alternative approach before giving up. Report failure only when truly stuck.
7. `think` is for logging irreversible-action reasoning to the audit trail — not for planning monologues."""

        system_content = "\n\n".join(filter(None, [
            SECURITY_BLOCK,
            persona,
            agent_rules,
            f"Role: {self.spec.role}",
            f"Objective: {self.spec.objective}",
            f"Available tools:\n{tool_list}" if tool_list else "",
            mode_addendum,
            constraints_text,
        ]))

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": tag(self.spec.objective, TrustLevel.OWNER)},
        ]

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch_tools(
        self, tool_calls: list[Any]
    ) -> list[tuple[str, str, str]]:
        """Execute tool calls, in parallel if multiple. Returns list of (id, name, result)."""
        tasks = [self._run_single_tool(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = []
        for tc, result in zip(tool_calls, results):
            name = tc.function.name
            if isinstance(result, Exception):
                result_str = f"Tool error: {result}"
            else:
                result_str = str(result) if not isinstance(result, str) else result
            out.append((tc.id, name, result_str))
        return out

    async def _run_single_tool(self, tool_call: Any) -> str:
        name = tool_call.function.name
        args_raw = tool_call.function.arguments or "{}"

        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            return f"Invalid tool arguments (not JSON): {args_raw[:100]}"

        tool_entry = self._tools.get(name)
        if tool_entry is None:
            return f"Tool '{name}' not available to this worker."

        try:
            # Inject task_id for tools that need it (scratch, approval, etc.)
            fn = tool_entry["fn"]
            import inspect
            sig = inspect.signature(fn)
            if "task_id" in sig.parameters and "task_id" not in args:
                args["task_id"] = self.task_id

            result = await fn(**args)
            return str(result) if result is not None else "ok"
        except Exception as exc:
            logger.exception("Tool '%s' raised an exception", name)
            from system.journal import log as journal_log
            await journal_log(
                "error",
                f"Tool '{name}' failed: {exc}",
                task_id=self.task_id,
            )
            return f"Tool '{name}' error: {exc}"
