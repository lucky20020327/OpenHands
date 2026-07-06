from __future__ import annotations

from openhands.sdk.tool.spec import Tool


CLARIFY_TOOL_NAMES = (
    "clarify_workspace_generate",
    "clarify_claim_variant",
    "clarify_klee_solve",
    "clarify_cross_validation",
    "clarify_task_done",
)

CLARIFY_AGENT_NAMES = (
    "clarify_harness_writer",
    "clarify_simulation_writer",
    "clarify_disambiguation_analyst",
)


def register_clarify_tools() -> None:
    """Import tool definitions so their module-level registration runs."""
    from openhands.clarify.tools import definitions as _definitions  # noqa: F401


def register_clarify_agents() -> None:
    """Register the three clarify sub-agent definitions idempotently."""
    from openhands.clarify.agents import register_clarify_agents as _reg
    _reg()


def register_clarify_all() -> None:
    """Register both tools and agents in one call."""
    register_clarify_tools()
    register_clarify_agents()


def get_clarify_tool_specs() -> list[Tool]:
    register_clarify_tools()
    return [Tool(name=name) for name in CLARIFY_TOOL_NAMES]


__all__ = [
    "CLARIFY_AGENT_NAMES",
    "CLARIFY_TOOL_NAMES",
    "get_clarify_tool_specs",
    "register_clarify_agents",
    "register_clarify_all",
    "register_clarify_tools",
]

