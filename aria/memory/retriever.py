"""MemoryRetriever — unified context builder injected into every LLM prompt.

Memory is injected LAST in the system prompt — LLMs attend more strongly to
content near the end of the context. This is non-negotiable.

Hard 4,000-token ceiling enforced via tiktoken cl100k_base.
Priority when budget is tight: facts → skills → history (truncated) → session (truncated).

All content is tagged with the appropriate trust level before injection.
"""

import logging

logger = logging.getLogger(__name__)

MAX_MEMORY_TOKENS = 4_000


def _count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Rough fallback: 1 token ≈ 4 chars
        return len(text) // 4


class MemoryRetriever:

    @staticmethod
    async def build_context(query: str) -> str:
        """Build memory context string for LLM prompt injection.

        Returns XML-tagged content, total tokens ≤ MAX_MEMORY_TOKENS.
        """
        from core.context_builder import TrustLevel, tag
        from memory.facts import FactsStore
        from memory.history import HistoryStore
        from memory.skills_meta import SkillsMetaStore
        from memory.sessions import SessionStore

        budget = MAX_MEMORY_TOKENS
        parts: list[str] = []

        # 1. High-importance facts (always included in full)
        facts_text = await FactsStore.format_for_prompt()
        if facts_text:
            tagged = tag(facts_text, TrustLevel.ARIA)
            cost = _count_tokens(tagged)
            if cost <= budget:
                parts.append(tagged)
                budget -= cost

        # 2. Skills (if any relevant)
        if budget > 200:
            skills_text = await SkillsMetaStore.format_for_prompt(query)
            if skills_text:
                tagged = tag(skills_text, TrustLevel.ARIA)
                cost = _count_tokens(tagged)
                if cost <= budget:
                    parts.append(tagged)
                    budget -= cost

        # 3. Relevant history (truncated if needed)
        if budget > 300:
            history_text = await HistoryStore.format_for_prompt(query)
            if history_text:
                tagged = tag(history_text, TrustLevel.ARIA)
                cost = _count_tokens(tagged)
                if cost > budget:
                    # Truncate to fit
                    ratio = budget / cost
                    history_text = history_text[: int(len(history_text) * ratio)]
                    tagged = tag(history_text, TrustLevel.ARIA)
                parts.append(tagged)
                budget -= _count_tokens(tagged)

        # 4. Relevant document chunks (truncated to fit)
        if budget > 300:
            try:
                from core.database import memory_db
                doc_rows = await memory_db.fetchall(
                    "SELECT dc.doc_name, dc.content "
                    "FROM doc_chunks_fts ft JOIN doc_chunks dc ON dc.id = ft.rowid "
                    "WHERE doc_chunks_fts MATCH ? LIMIT 3",
                    (query,),
                )
                if doc_rows:
                    doc_lines = ["Relevant document extracts:"]
                    for r in doc_rows:
                        doc_lines.append(f"[{r['doc_name']}] {r['content'][:300]}")
                    doc_text = "\n".join(doc_lines)
                    # Tag as EXTERNAL — ingested from outside sources
                    from core.context_builder import tag as ctag
                    tagged = ctag(doc_text, TrustLevel.EXTERNAL, source="ingested_doc")
                    cost = _count_tokens(tagged)
                    if cost <= budget:
                        parts.append(tagged)
                        budget -= cost
            except Exception:
                pass

        # 5. Active session (truncated last)
        if budget > 200:
            session_text = await SessionStore.get_active_context(query)
            if session_text:
                tagged = tag(session_text, TrustLevel.ARIA)
                cost = _count_tokens(tagged)
                if cost > budget:
                    session_text = session_text[: int(len(session_text) * (budget / cost))]
                    tagged = tag(session_text, TrustLevel.ARIA)
                parts.append(tagged)

        return "\n\n".join(parts)
