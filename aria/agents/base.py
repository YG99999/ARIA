"""WorkerSpec dataclass and BaseAgent abstract class.

WorkerSpec defines what a worker is allowed to do.
BaseAgent provides context compression and the abstract run() interface.

Context compression:
  _compress_if_needed(messages) is called at the top of every ReAct iteration.
  When token count exceeds compress_at threshold, the middle messages are summarized
  with the fast model and replaced with a single compressed history entry.
  System prompt (messages[0]) and last 6 messages are always preserved.
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_tokenizer = None  # lazy-initialized tiktoken encoder


def _get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        import tiktoken
        _tokenizer = tiktoken.get_encoding("cl100k_base")
    return _tokenizer


def _count_tokens(messages: list[dict]) -> int:
    enc = _get_tokenizer()
    total = 0
    for msg in messages:
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            # Multi-part content (vision)
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(enc.encode(part["text"]))
        else:
            total += len(enc.encode(content))
    return total


# ------------------------------------------------------------------
# WorkerSpec
# ------------------------------------------------------------------


@dataclass
class WorkerSpec:
    """Specification for a WorkerAgent instance.

    name: human-readable name for logging
    role: system prompt role description
    objective: the task the worker must complete
    allowed_tools: tool names this worker can call (others return "not available")
    constraints: additional instructions appended to system prompt
    step_budget: max ReAct iterations before the worker declares STEP_LIMIT
    output_schema: optional JSON schema for structured output validation
    depends_on: task IDs that must complete before this worker starts
    """

    name: str
    role: str
    objective: str
    allowed_tools: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    step_budget: int = 25
    output_schema: dict | None = None
    depends_on: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# BaseAgent
# ------------------------------------------------------------------


class BaseAgent(ABC):
    """Abstract base for all ARIA agents."""

    def __init__(self, spec: WorkerSpec, task_id: str) -> None:
        self.spec = spec
        self.task_id = task_id
        self._step = 0

    @abstractmethod
    async def run(self) -> str:
        """Execute the agent. Returns a result string."""
        ...

    # ------------------------------------------------------------------
    # Context compression
    # ------------------------------------------------------------------

    async def _compress_if_needed(self, messages: list[dict]) -> list[dict]:
        """Summarize middle messages when approaching the context limit.

        Preserves messages[0] (system prompt) and the last 6 messages.
        Uses the fast model for summarization. Logs to journal.
        """
        from core.config import settings

        model_id = settings.get_model("heavy")
        model_config = settings.get_model_config(model_id)
        compress_at = model_config.get("compress_at", 150_000)

        token_count = _count_tokens(messages)
        if token_count <= compress_at:
            return messages

        if len(messages) <= 8:
            # Not enough messages to compress meaningfully
            return messages

        keep_head = [messages[0]]  # system prompt — never compress
        keep_tail = messages[-6:]   # last 6 messages — always keep
        middle = messages[1:-6]

        if not middle:
            return messages

        middle_text = "\n".join(
            f"[{m['role'].upper()}] {(m.get('content') or '')[:300]}"
            for m in middle
        )

        try:
            from core.llm_client import llm_call
            response = await llm_call(
                model=settings.get_model("light"),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Summarize this agent task history in 3-5 sentences, "
                            f"preserving key findings and decisions:\n\n{middle_text}"
                        ),
                    }
                ],
                max_tokens=300,
                task_id=self.task_id,
            )
            summary = response.choices[0].message.content or "[history compressed]"
        except Exception:
            logger.warning("Context compression LLM call failed — skipping", exc_info=True)
            return messages

        compressed = {
            "role": "assistant",
            "content": f"[Task history compressed — {len(middle)} messages summarized]: {summary}",
        }

        new_messages = keep_head + [compressed] + keep_tail

        from system.journal import log as journal_log
        await journal_log(
            "system",
            f"Context compressed: {len(middle)} messages → 1 summary ({token_count} tokens)",
            task_id=self.task_id,
        )

        logger.info(
            "Compressed context for task %s: %d msg → %d msg (%d tokens)",
            self.task_id, len(messages), len(new_messages), token_count,
        )
        return new_messages

    # ------------------------------------------------------------------
    # Step snapshot for /retry
    # ------------------------------------------------------------------

    async def _save_snapshot(
        self,
        messages: list[dict],
        scratch: dict,
        last_tool: str | None,
        last_result: str | None,
    ) -> None:
        from core.tasks import TaskRegistry

        snapshot = {
            "messages": messages,
            "scratch": scratch,
            "last_tool": last_tool,
            "last_result": last_result,
        }
        await TaskRegistry.log_step(
            task_id=self.task_id,
            step=self._step,
            role="snapshot",
            state_snapshot=snapshot,
        )
