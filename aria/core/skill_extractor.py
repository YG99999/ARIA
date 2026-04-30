"""Post-task skill extraction — promotes successful task patterns to permanent skills.

Triggered after every completed task. Uses the fast model.
Skips extraction if the task had fewer than 4 steps (too simple to generalize).
"""

import logging
import re
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_TEMPLATE = '''"""
ARIA Skill: {name}
Tags: {tags}
Assumptions: {assumptions}
Known issues: {known_issues}
"""

# STEPS:
# {steps}

from core.tools import tool


@tool(
    name="{tool_name}",
    description="{description}",
)
async def {fn_name}(**kwargs) -> str:
    """Auto-extracted skill. Edit as needed."""
    raise NotImplementedError("Skill stub — implement based on the steps above.")
'''


class SkillExtractor:

    @staticmethod
    async def should_extract(task_id: str) -> bool:
        """Fast check: return True if this task has enough steps to be worth extracting."""
        from core.tasks import TaskRegistry

        steps = await TaskRegistry.get_steps(task_id)
        return len(steps) >= 4

    @staticmethod
    async def maybe_extract(task_id: str, objective: str) -> None:
        """Check if extraction is warranted and run it if so."""
        from core.tasks import TaskRegistry

        steps = await TaskRegistry.get_steps(task_id)
        if len(steps) < 4:
            return  # Too simple — skip LLM call

        # Check verdict
        task = await TaskRegistry.get(task_id)
        if task and task.get("verdict") in ("FAILURE",):
            return  # Don't extract from failures

        try:
            await SkillExtractor.extract(task_id, objective, steps)
        except Exception:
            logger.debug("Skill extraction failed for task %s", task_id, exc_info=True)

    @staticmethod
    async def extract(task_id: str, objective: str, steps: list) -> None:
        from core.llm_client import llm_call
        from core.config import settings
        from core.database import memory_db
        from system.journal import log as journal_log

        steps_text = "\n".join(
            f"[{s['step']}] {s['role']}: {(s['content'] or s['tool_name'] or '')[:200]}"
            for s in steps
        )

        response = await llm_call(
            model=settings.get_model("light"),
            messages=[
                {
                    "role": "user",
                    "content": textwrap.dedent(f"""
                        A task was completed successfully. Extract a reusable skill from it.

                        Task: {objective}

                        Agent steps:
                        {steps_text}

                        Return a JSON object with these fields:
                        {{
                            "name": "snake_case_skill_name",
                            "description": "One sentence: what this skill does",
                            "tags": "comma,separated,tags",
                            "assumptions": "What conditions must be true for this to work",
                            "steps": "Numbered list of high-level steps",
                            "known_issues": "Any caveats or failure modes"
                        }}

                        Only extract if this is genuinely reusable for similar future tasks.
                        If it's too one-off, return {{"skip": true}}.
                        Reply with ONLY valid JSON.
                    """).strip(),
                }
            ],
            max_tokens=500,
            task_id=task_id,
        )

        raw = (response.choices[0].message.content or "{}").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        import json
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Skill extractor returned invalid JSON for task %s", task_id)
            return

        if data.get("skip"):
            return

        name = data.get("name", "").strip()
        if not name or not re.match(r"^[a-z][a-z0-9_]*$", name):
            return

        # Write skill file
        skill_path = settings.skills_dir / f"{name}.py"
        if not skill_path.exists():
            fn_name = name
            tool_name = name
            skill_code = _SKILL_TEMPLATE.format(
                name=name,
                tool_name=tool_name,
                fn_name=fn_name,
                description=data.get("description", ""),
                tags=data.get("tags", ""),
                assumptions=data.get("assumptions", ""),
                steps=data.get("steps", "").replace("\n", "\n# "),
                known_issues=data.get("known_issues", "none"),
            )
            skill_path.write_text(skill_code, encoding="utf-8")
            logger.info("Wrote skill file: %s", skill_path)

        # Register in skills_meta
        await memory_db.execute(
            """INSERT OR IGNORE INTO skills_meta(name, description, tags, created_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (name, data.get("description", ""), data.get("tags", "")),
        )
        await memory_db.commit()

        await journal_log(
            "skill_extracted",
            f"Extracted skill '{name}' from task {task_id}: {data.get('description', '')}",
            task_id=task_id,
        )

        logger.info("Skill extracted: %s", name)
