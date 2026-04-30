"""Convenience WorkerSpec factories.

browser_spec(), shell_spec(), dev_spec() create pre-built specs for common task types.
The orchestrator chooses which spec to use based on router classification.
"""

from agents.base import WorkerSpec


def shell_spec(objective: str, step_budget: int = 25) -> WorkerSpec:
    """Spec for shell/filesystem/Python execution tasks."""
    return WorkerSpec(
        name="shell_worker",
        role="You are a shell and Python execution agent. You run commands, write files, and execute code to complete the user's task.",
        objective=objective,
        allowed_tools=[
            "run_shell_command",
            "write_file",
            "edit_file",
            "read_file",
            "list_directory",
            "execute_python",
            "think",
            "write_scratch",
            "read_scratch",
            "search_web",
        ],
        constraints=[
            "Use edit_file (str_replace) for modifying existing files; use write_file only for new files.",
            "Run tests after every code write. Fix and retry until exit 0 or step budget exhausted.",
            "Use search_web before launching a browser for information retrieval.",
        ],
        step_budget=step_budget,
    )


def browser_spec(objective: str, step_budget: int = 25) -> WorkerSpec:
    """Spec for browser interaction tasks (login, forms, scraping, navigation)."""
    return WorkerSpec(
        name="browser_worker",
        role="You are a browser automation agent. You navigate websites, fill forms, log in, and extract information.",
        objective=objective,
        allowed_tools=[
            "browse",
            "take_screenshot",
            "mouse_click",
            "keyboard_type",
            "key_press",
            "think",
            "write_scratch",
            "read_scratch",
            "save_session_state",
            "get_credential",
            "save_credential",
            "request_approval",
            "escalate",
            "search_web",
        ],
        constraints=[
            "Always try loading a stored session before using get_credential for login.",
            "Save the session after a successful login with save_session_state.",
            "Use think before any form submission, purchase, or irreversible browser action.",
            "Request approval before spending money, sending messages, or publishing content.",
            "Take a screenshot when uncertain about the current page state.",
        ],
        step_budget=step_budget,
    )


def dev_spec(
    objective: str,
    test_command: str | None = None,
    step_budget: int = 30,
) -> WorkerSpec:
    """Spec for software development tasks with a write→test→fix loop.

    test_command: shell command to run after each code write (e.g. 'pytest tests/')
    """
    constraints = [
        "Use edit_file (str_replace) for modifications; write_file only for new files.",
        "Think before each architectural decision.",
        "Run tests after every write. Fix failures and retry until exit 0 or max steps.",
    ]
    if test_command:
        constraints.append(f"Test command: {test_command} — run this after every code change.")

    return WorkerSpec(
        name="dev_worker",
        role="You are a software development agent. You write, edit, and test code iteratively until the task is complete.",
        objective=objective,
        allowed_tools=[
            "run_shell_command",
            "write_file",
            "edit_file",
            "read_file",
            "list_directory",
            "execute_python",
            "think",
            "write_scratch",
            "read_scratch",
            "search_web",
        ],
        constraints=constraints,
        step_budget=step_budget,
    )


def research_spec(objective: str, step_budget: int = 20) -> WorkerSpec:
    """Spec for information retrieval and research tasks."""
    return WorkerSpec(
        name="research_worker",
        role="You are a research agent. You find, synthesize, and summarize information using web search and memory.",
        objective=objective,
        allowed_tools=[
            "search_web",
            "think",
            "save_fact",
            "search_memory",
            "get_facts",
            "write_scratch",
            "read_scratch",
            "ingest_document",
        ],
        constraints=[
            "Always prefer search_web for information retrieval.",
            "Only escalate to browser if you need to interact with a page.",
            "Save important findings with save_fact so they persist across tasks.",
        ],
        step_budget=step_budget,
    )


def analysis_spec(objective: str, step_budget: int = 15) -> WorkerSpec:
    """Spec for data analysis tasks using existing memory/documents."""
    return WorkerSpec(
        name="analysis_worker",
        role="You are a data analysis agent. You analyze data, documents, and memory to answer questions and derive insights.",
        objective=objective,
        allowed_tools=[
            "search_memory",
            "get_facts",
            "think",
            "execute_python",
            "write_scratch",
            "read_scratch",
            "ingest_document",
        ],
        constraints=[
            "Use existing memory and documents first before searching the web.",
            "Execute Python for calculations, data processing, or statistical analysis.",
            "Save key insights as facts.",
        ],
        step_budget=step_budget,
    )


def spec_for_mode(mode: str, objective: str) -> WorkerSpec:
    """Map a router mode string to the appropriate convenience spec."""
    mapping = {
        "dev_mode": dev_spec,
        "browser_mode": browser_spec,
        "research_mode": research_spec,
        "analysis_mode": analysis_spec,
    }
    fn = mapping.get(mode, shell_spec)
    return fn(objective)
