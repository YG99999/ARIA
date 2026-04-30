"""Task classification — routes messages to the right agent mode and spec.

Router uses the LLM to classify every incoming message. No keyword fallbacks —
ARIA is an intelligent agent and the LLM decides what to do regardless of how
the user phrases things.

If the first JSON parse fails (e.g. model wrapped in prose), we extract the
first JSON object from anywhere in the response. If that also fails, we retry
with a simpler yes/no prompt to at least decide task vs conversation. Only if
all LLM calls fail do we default to conversation, and that failure is logged.
"""

import json
import logging
import re
from typing import Literal

from core.config import settings
from core.llm_client import llm_call

logger = logging.getLogger(__name__)

TaskKind = Literal["browser", "shell", "mixed", "meta", "conversation"]
AgentMode = Literal["research_mode", "dev_mode", "browser_mode", "analysis_mode", "conversation"]

ROUTER_MODEL_TIER = "light"


_CLASSIFY_SYSTEM = """You are a task router for an AI agent. Classify the user's message.

Return a JSON object with these fields:
- kind: one of "browser", "shell", "mixed", "meta", "conversation"
- mode: one of "research_mode", "dev_mode", "browser_mode", "analysis_mode", "conversation"
- title: a short (≤60 char) task title
- complexity: one of "simple", "complex"

Definitions:
- "browser": needs to navigate/interact with a website (fill form, login, click, scrape, check live page)
- "shell": file operations, running commands, installing packages, writing/editing code, system tasks
- "mixed": needs both browser and shell steps
- "meta": asking about ARIA itself (status, tasks, memory, settings)
- "conversation": questions ARIA can answer from knowledge/memory without using any tools

- "research_mode": information retrieval — search first, browser only if interaction needed
- "dev_mode": write, fix, test, debug code — use edit_file + test loop
- "browser_mode": web interaction (login, forms, purchases, data extraction)
- "analysis_mode": analyze data or documents ARIA already has

Output ONLY the JSON object. No explanation, no markdown, no preamble."""


_RETRY_SYSTEM = """You are a task classifier. Answer with a single JSON object only.

The user sent: {message}

Is this something that needs tools (shell commands, web browsing, file editing) or just a question/conversation?

Reply with exactly this JSON:
{{"kind": "shell", "mode": "dev_mode", "title": "{title}", "complexity": "simple"}}

OR:
{{"kind": "conversation", "mode": "conversation", "title": "{title}", "complexity": "simple"}}

OR if it needs a website:
{{"kind": "browser", "mode": "browser_mode", "title": "{title}", "complexity": "simple"}}

Output ONLY the JSON. Nothing else."""


def _extract_json(text: str) -> dict | None:
    """Extract the first valid JSON object from anywhere in the text."""
    # Try raw parse first
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Strip markdown code fences
    cleaned = text
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except Exception:
                pass

    # Find first { ... } block in the text
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass

    return None


async def classify(message: str) -> dict:
    """Classify a message using the LLM. Retries with a simpler prompt if needed.

    The router only sees the first 500 chars — enough to classify intent without
    being confused by large payloads (JSON blobs, code, etc.).

    If both LLM attempts fail, default to 'shell' (dispatch an agent with tools)
    rather than 'conversation' (no tools). An agent can always answer a question;
    the conversational path cannot run a task.
    """
    model = settings.get_model(ROUTER_MODEL_TIER)
    # Truncate for routing — we classify intent, not content
    snippet = message[:500]
    title = message[:60]

    # Attempt 1: full classification prompt
    try:
        response = await llm_call(
            model=model,
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": snippet},
            ],
            max_tokens=200,
            temperature=0.0,
            record_usage=False,
        )
        raw = (response.choices[0].message.content or "").strip()
        result = _extract_json(raw)
        if result and "kind" in result:
            result.setdefault("mode", "conversation")
            result.setdefault("title", title)
            result.setdefault("complexity", "simple")
            return result
    except Exception:
        logger.debug("Router attempt 1 failed", exc_info=True)

    # Attempt 2: simpler binary prompt — model may not follow strict JSON format
    try:
        retry_prompt = _RETRY_SYSTEM.format(message=snippet, title=title)
        response = await llm_call(
            model=model,
            messages=[{"role": "user", "content": retry_prompt}],
            max_tokens=100,
            temperature=0.0,
            record_usage=False,
        )
        raw = (response.choices[0].message.content or "").strip()
        result = _extract_json(raw)
        if result and "kind" in result:
            result.setdefault("mode", "conversation")
            result.setdefault("title", title)
            result.setdefault("complexity", "simple")
            logger.info("Router used retry prompt for: %s", title)
            return result
    except Exception:
        logger.debug("Router attempt 2 failed", exc_info=True)

    # Both LLM attempts failed. Default to shell so an agent with tools handles it.
    # An agent can answer questions; the conversational path cannot run tasks.
    # This only happens when the API is down or returning completely garbled output.
    logger.error(
        "Router LLM failed on both attempts for: %r — dispatching as shell task. "
        "Check API connectivity.", message[:100]
    )
    return {
        "kind": "shell",
        "mode": "dev_mode",
        "title": title,
        "complexity": "simple",
    }


async def decompose_task(message: str) -> list[dict]:
    """Break a complex/mixed task into ordered subtasks."""
    system = """You are a task planner for an AI agent system.

The user has a mixed or complex task. Decompose it into 2-6 ordered subtasks.
Each subtask should be achievable by one specialized agent.

Return a JSON array of subtask objects:
[
  {"kind": "shell", "description": "..."},
  {"kind": "browser", "description": "..."}
]

kind options: "browser", "shell", "analysis"
Keep descriptions concise but specific. Order matters.
Output ONLY the JSON array. Nothing else."""

    try:
        response = await llm_call(
            model=settings.get_model(ROUTER_MODEL_TIER),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            max_tokens=500,
            temperature=0.1,
            record_usage=False,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Extract JSON array
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    result = json.loads(part)
                    if isinstance(result, list):
                        return result
                except Exception:
                    pass
        subtasks = json.loads(raw)
        if isinstance(subtasks, list):
            return subtasks
    except Exception:
        logger.warning("Task decomposition failed — treating as single shell task", exc_info=True)

    return [{"kind": "shell", "description": message}]


def is_continuation_request(text: str) -> bool:
    """Detect 'continue from last time' style messages."""
    signals = [
        "continue", "resume", "pick up where", "from last time",
        "where we left off", "keep going", "carry on",
    ]
    lower = text.lower()
    return any(s in lower for s in signals)
