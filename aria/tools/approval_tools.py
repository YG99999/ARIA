"""Approval tool — request_approval blocks agent until user approves/denies."""

from core.tools import tool


@tool(
    name="request_approval",
    description=(
        "Request user approval before taking a sensitive action. "
        "Blocks until approved or denied. Categories: spend_money, send_message, "
        "delete_file, publish, modify_core_code."
    ),
    schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["spend_money", "send_message", "delete_file", "publish", "modify_core_code"],
                "description": "Action category (determines timeout)",
            },
            "description": {"type": "string", "description": "What you are about to do"},
            "context": {"type": "string", "description": "Additional context for the user"},
        },
        "required": ["category", "description"],
    },
)
async def request_approval(
    category: str,
    description: str,
    context: str | None = None,
    task_id: str | None = None,
    **kwargs,
) -> str:
    from core.approval import ApprovalQueue

    result = await ApprovalQueue.request(
        category=category,
        description=description,
        context_text=context,
        task_id=task_id,
    )
    return result  # "approved" or raises RuntimeError
