"""Import smoke test — verifies all core modules load without errors.

Run with: python test_imports.py
Does NOT require .env or a running Telegram connection.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

MODULES = [
    "core.config",
    "core.secrets",
    "core.database",
    "core.tools",
    "core.context_builder",
    "core.llm_client",
    "core.cost_guard",
    "core.router",
    "core.tasks",
    "core.approval",
    "core.skill_extractor",
    "core.orchestrator",
    "agents.base",
    "agents.worker",
    "agents.specs",
    "memory.store",
    "memory.facts",
    "memory.history",
    "memory.sessions",
    "memory.skills_meta",
    "memory.retriever",
    "system.journal",
    "system.health",
    "system.handover",
    "system.dreamer",
    "system.webhook",
    "scheduler.cron",
    "tools.shell",
    "tools.python_executor",
    "tools.search",
    "tools.memory_tools",
    "tools.credential_tools",
    "tools.approval_tools",
    "tools.computer_use",
    "tools.browser",
    "tools.schedule_tools",
    "tools.self_modify_tools",
    "tools.challenge_detector",
]


def run_smoke_test():
    import importlib
    print("=== Import Smoke Test ===\n")
    passed = 0
    failed = []

    for mod in MODULES:
        try:
            importlib.import_module(mod)
            print(f"  PASS {mod}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL {mod}: {exc}")
            failed.append((mod, str(exc)))

    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{len(MODULES)} passed")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for mod, err in failed:
            print(f"  {mod}: {err}")
        sys.exit(1)
    else:
        print("ALL IMPORTS PASSED")


if __name__ == "__main__":
    run_smoke_test()
