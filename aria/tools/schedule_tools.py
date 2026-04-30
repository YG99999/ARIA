"""Schedule tools — add_job, list_jobs, remove_job."""

from core.tools import tool


@tool(
    name="add_schedule",
    description=(
        "Schedule a recurring task. Parses natural language triggers. "
        "Examples: 'daily at 8am', 'every 30 minutes', 'weekly on Monday at 9am'."
    ),
    schema={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What should run"},
            "trigger": {"type": "string", "description": "Natural language schedule, e.g. 'daily at 8am'"},
        },
        "required": ["description", "trigger"],
    },
)
async def add_schedule(description: str, trigger: str, **kwargs) -> str:
    from scheduler.cron import CronScheduler
    job_id = await CronScheduler.add_job(description, trigger)
    return f"Scheduled: '{description}' [{job_id}] — {trigger}"


@tool(
    name="remove_schedule",
    description="Remove a scheduled job by ID.",
    schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job ID from /schedule list"},
        },
        "required": ["job_id"],
    },
)
async def remove_schedule(job_id: str, **kwargs) -> str:
    from scheduler.cron import CronScheduler
    await CronScheduler.remove_job(job_id)
    return f"Removed job {job_id}"
