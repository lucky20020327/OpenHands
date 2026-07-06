from __future__ import annotations

from openhands.sdk.tool.spec import Tool


CLARIFY_TOOL_NAMES = (
    "clarify_workspace_generate",
    "clarify_claim_variant",
    "clarify_klee_solve",
    "clarify_cross_validation",
    "clarify_task_done",
)


def register_clarify_tools() -> None:
    """Import tool definitions so their module-level registration runs."""
    from openhands.clarify.tools import definitions as _definitions  # noqa: F401


def get_clarify_tool_specs() -> list[Tool]:
    register_clarify_tools()
    return [Tool(name=name) for name in CLARIFY_TOOL_NAMES]


__all__ = [
    "CLARIFY_TOOL_NAMES",
    "get_clarify_tool_specs",
    "register_clarify_tools",
]

