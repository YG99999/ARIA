"""Centralized LLM call wrapper with exponential backoff and cost tracking.

All LLM calls in ARIA go through llm_call() — no bare client.chat.completions.create()
anywhere else in the codebase. This ensures retry behavior and budget enforcement
are applied consistently everywhere.
"""

import asyncio
import logging
import random
import time
from typing import Any

from openai import AsyncOpenAI, RateLimitError, APIStatusError

from core.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
    return _client


async def llm_call(
    max_retries: int = 3,
    record_usage: bool = True,
    task_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Call the LLM with exponential backoff on rate-limit and 5xx errors.

    Raises BudgetExceededError before the call if the daily budget is exhausted.
    Records token usage to llm_usage table after a successful call.

    kwargs are passed directly to client.chat.completions.create().
    """
    from core.cost_guard import check as budget_check  # late import — avoids circular

    await budget_check()

    client = get_client()
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(**kwargs)

            if record_usage:
                await _record_usage(response, task_id)

            return response

        except RateLimitError as exc:
            last_exc = exc
            delay = 2 ** attempt + random.uniform(0, 1)
            logger.warning("Rate limited (attempt %d/%d) — retrying in %.1fs", attempt + 1, max_retries, delay)
            await asyncio.sleep(delay)

        except APIStatusError as exc:
            last_exc = exc
            if exc.status_code >= 500:
                delay = 2 ** attempt + random.uniform(0, 0.5)
                logger.warning("LLM server error %d (attempt %d/%d) — retrying in %.1fs",
                               exc.status_code, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
            else:
                raise

    raise RuntimeError(f"LLM call failed after {max_retries} retries") from last_exc


async def _record_usage(response: Any, task_id: str | None) -> None:
    """Write token usage + estimated cost to llm_usage table."""
    try:
        from core.database import state_db  # late import

        usage = getattr(response, "usage", None)
        if usage is None:
            return

        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        model = getattr(response, "model", "unknown") or "unknown"

        cost_usd = _estimate_cost(model, tokens_in, tokens_out)

        await state_db.execute(
            """INSERT INTO llm_usage(model, tokens_in, tokens_out, cost_usd, task_id)
               VALUES (?, ?, ?, ?, ?)""",
            (model, tokens_in, tokens_out, cost_usd, task_id),
        )
        await state_db.commit()
    except Exception:
        logger.debug("Failed to record LLM usage", exc_info=True)


# Very rough per-token pricing table (USD per 1M tokens, [input, output])
# Update when actual pricing changes. Falls back to zeros if model not found.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku": (0.25, 1.25),
    "claude-sonnet": (3.0, 15.0),
    "claude-opus": (15.0, 75.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (5.0, 15.0),
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    model_lower = model.lower()
    for key, (price_in, price_out) in _PRICING.items():
        if key in model_lower:
            return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return 0.0
