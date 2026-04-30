"""Task classification — routes messages to the right agent mode and spec.

Router uses the fast (cheap) model to classify incoming messages.
All mode decisions go through classify_mode().
"""

import json
import logging
from typing import Literal

from core.config import settings
from core.llm_client import llm_call

logger = logging.getLogger(__name__)

TaskKind = Literal["browser", "shell", "mixed", "meta", "conversation"]
AgentMode = Literal["research_mode", "dev_mode", "browser_mode", "analysis_mode", "conversation"]

# Explicit model tier mapping (from plan)
# Routing / classification → fast
ROUTER_MODEL_TIER = "light"


_CLASSIFY_SYSTEM = """You are a task router for an AI agent. Classify the user's message.

Return a JSON object with these fields:
- kind: one of "browser", "shell", "mixed", "meta", "conversation"
- mode: one of "research_mode", "dev_mode", "browser_mode", "analysis_mode", "conversation"
- title: a short (≤60 char) task title
- complexity: one of "simple", "complex"

Rules:
- "browser": needs to navigate/interact with a website (fill form, login, click, scrape)
- "shell": file ops, running commands, installing packages, coding, system tasks
- "mixed": needs both browser and shell steps
- "meta": asking about ARIA itself (/why, /status type questions in natural language)
- "conversation": chitchat, questions ARIA can answer from memory without tools

- "research_mode": information retrieval, fact-finding — prefer search_web first
- "dev_mode": write/fix/test code, CI, debugging — always use edit_file + test loop
- "browser_mode": web interaction (login, forms, purchases, data extraction)
- "analysis_mode": analyze data or documents ARIA already has

IMPORTANT: For information retrieval (research, fact-finding, price comparison, checking status),
always classify as research_mode and prefer search_web first. Only escalate to browser_mode
when the user needs to *interact* with a page (fill a form, click a button, log in).
search_web is 10× faster than browser.

Reply with ONLY valid JSON. No explanation."""


async def classify(message: str) -> dict:
    """Classify a message and return routing info."""
    try:
        response = await llm_call(
            model=settings.get_model(ROUTER_MODEL_TIER),
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": message},
            ],
            max_tokens=200,
            temperature=0.0,
            record_usage=False,  # Don't count router calls toward usage tracking
        )
        raw = response.choices[0].message.content or "{}"
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        # Validate required fields
        result.setdefault("kind", "conversation")
        result.setdefault("mode", "conversation")
        result.setdefault("title", message[:60])
        result.setdefault("complexity", "simple")
        return result
    except Exception:
        logger.warning("Router classification failed — defaulting to conversation", exc_info=True)
        return {
            "kind": "conversation",
            "mode": "conversation",
            "title": message[:60],
            "complexity": "simple",
        }


_DECOMPOSE_SYSTEM = """You are a task planner for an AI agent system.

The user has a mixed or complex task. Decompose it into 2-6 ordered subtasks.
Each subtask should be achievable by one specialized agent.

Return a JSON array of subtask objects:
[
  {"kind": "shell", "description": "..."},
  {"kind": "browser", "description": "..."}
]

kind options: "browser", "shell", "analysis"
Keep descriptions concise but specific. Order matters — later tasks may use results from earlier ones.
Reply with ONLY valid JSON array."""


async def decompose_task(message: str) -> list[dict]:
    """Break a complex/mixed task into ordered subtasks."""
    try:
        response = await llm_call(
            model=settings.get_model(ROUTER_MODEL_TIER),
            messages=[
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": message},
            ],
            max_tokens=500,
            temperature=0.1,
            record_usage=False,
        )
        raw = response.choices[0].message.content or "[]"
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        subtasks = json.loads(raw)
        if not isinstance(subtasks, list):
            return [{"kind": "shell", "description": message}]
        return subtasks
    except Exception:
        logger.warning("Task decomposition failed — treating as single task", exc_info=True)
        return [{"kind": "shell", "description": message}]


def is_continuation_request(text: str) -> bool:
    """Detect 'continue from last time' style messages."""
    signals = [
        "continue", "resume", "pick up where", "from last time",
        "where we left off", "keep going", "carry on",
    ]
    lower = text.lower()
    return any(s in lower for s in signals)
