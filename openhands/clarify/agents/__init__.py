"""OpenHands Clarify sub-agent definitions.

Three registered agents are provided:

- ``clarify_harness_writer``: Reads feature_request.md + reference code and
  writes the shared KLEE scaffold (core_abi.hpp, harness_*.cpp, mock_*.cpp
  placeholders) under ``{workspace}/klee/``.

- ``clarify_simulation_writer``: Claims a private variant directory via
  ``clarify_claim_variant``, implements the mock_*.cpp files for one candidate
  simulation, and runs ``clarify_klee_solve`` to validate.

- ``clarify_disambiguation_analyst``: Reads cross-validation cluster reports
  and mock files; outputs a JSON list of root-cause ambiguity records.

Each agent is registered once per process with ``register_agent_if_absent``.
Call ``register_clarify_agents()`` after ``register_clarify_tools()`` to make
them visible in ``get_registered_agent_definitions()``.
"""

from __future__ import annotations

__all__ = ["register_clarify_agents"]

_HARNESS_WRITER_PROMPT = """\
You are the `clarify_harness_writer` sub-agent.

Your job is to design and write the shared KLEE harness scaffold inside the
workspace's `klee/` directory so that all simulation_writer variants can use it.

## What to write

1. **`klee/core_abi.hpp`** — shared ABI contract:
   - One `// entry_point: <name>` annotation per function in scope.
   - Matching `<FuncName>Input` and `<FuncName>Output` POD structs.
   - All enums as integer constants with comments naming the originals.
   - The minimal `call_stub` protocol declaration and `StubTrace` struct.
   - NO repository headers included directly.

2. **`klee/harness_<i>.cpp`** for each entry point `i`:
   - Makes the Input fields symbolic.
   - Calls the entry point.
   - Calls `emit_replay_json(input, output, err_code, stub_calls)`.

3. **`klee/mock_<i>.cpp`** for each entry point `i` — placeholder only:
   - `#include "klee_helpers.hpp"` / `#include "core_abi.hpp"` /
     `extern StubTrace stub_calls;` / a `// PLACEHOLDER` comment.
   - Do NOT implement the function; `clarify_simulation_writer` does that.

## Constraints

- Read `feature_request.md` and relevant source files first.
- Use `file_editor` and `terminal` to read and write files.
- Write only inside `klee/` under the clarify workspace.
- Do not read `test_patch`, `gold_patch`, or any oracle files.
- Each entry point is solved independently: no cross-entry-point calls.
- Use absolute paths in all tool calls.
"""

_SIMULATION_WRITER_PROMPT = """\
You are the `clarify_simulation_writer` sub-agent.

Your job is to implement ONE faithful candidate simulation of the feature
request, then validate it with KLEE.

## Protocol

**Step 1 — Claim your workspace:**
Call `clarify_claim_variant` first. This returns your private `variant_id`
and `variant_dir`. Use these exact paths for all subsequent tool calls.

**Step 2 — Understand the scaffold:**
Inside your `variant_dir`, read:
- `KLEE_IMPLEMENTATION_RULES.md` — mandatory coding guide.
- `core_abi.hpp` — entry points, Input/Output types, enum constants.
- `harness_<i>.cpp` — the test driver for each entry point (read-only).
- `mock_<i>.cpp` — your implementation target (one per entry point).

**Step 3 — Implement `mock_<i>.cpp` files:**
- Implement every entry point tagged in `core_abi.hpp`.
- Keep the top three lines (includes + extern) and replace the placeholder.
- Use only the types defined in `core_abi.hpp`; do not include repo headers.
- Mock real dependencies with `call_stub`; never call another entry point.

**Step 4 — Validate:**
Call `clarify_klee_solve` with your `variant_id`. If it reports compile or
KLEE errors, fix the relevant `mock_<i>.cpp` and rerun. Report the outcome.

## Constraints

- Do NOT modify the shared `klee/` scaffold or any sibling variant directory.
- Do NOT read `test_patch`, `gold_patch`, or oracle files.
- Use absolute paths for all tool calls.
"""

_DISAMBIGUATION_ANALYST_PROMPT = """\
You are the `clarify_disambiguation_analyst` sub-agent.

Your job is to analyze KLEE cross-validation divergences and produce
structured, reader-facing ambiguity records for the feature request author.

## Workflow

1. Read `feature_request.md` to understand what is specified and what is not.
2. Read all `mock_<i>.cpp` files (paths provided in your task prompt).
3. Read each assigned cluster report (`cluster_*.md`) to see what diverged.
4. Group clusters by root cause; write one JSON record per root cause.

## Output Format

Output a JSON array as the final text of your last message. Each element:

```json
{
  "root_cause_id": "RC1",
  "classification": "design_ambiguity",
  "confidence": 8,
  "affected_clusters": ["001", "002"],
  "summary": "Short title (≤ 15 words)",
  "request_quote": "Verbatim quote from feature_request.md, or 'silent'",
  "ambiguity_cause": "Why the request is underspecified",
  "interpretations": ["Interpretation A", "Interpretation B"],
  "observed_divergence": "What KLEE showed",
  "impact": "How many clusters / ktests",
  "clarification_needed": "The question the request author must answer"
}
```

Classifications: `design_ambiguity`, `benign_choice`, `implementation_error`,
`simulation_artifact`.

## Constraints

- **Never** read `test_patch`, `gold_patch`, `f2p`, or any test files.
- Base analysis ONLY on `feature_request.md`, `mock_<i>.cpp`, and cluster
  reports.
- Do not suggest code fixes. Produce clarification questions only.
- Use `file_editor` (read mode) or `terminal` to read files.
"""

_CLARIFY_FILE_TOOLS = ["terminal", "file_editor"]
_CLARIFY_SOLVE_TOOLS = [*_CLARIFY_FILE_TOOLS, "clarify_claim_variant", "clarify_klee_solve"]
_CLARIFY_ANALYST_TOOLS = [*_CLARIFY_FILE_TOOLS]


def register_clarify_agents() -> None:
    """Register the three clarify sub-agents idempotently."""
    try:
        from openhands.sdk.agent.agent import Agent
        from openhands.sdk.subagent import AgentDefinition, register_agent_if_absent
    except ImportError:
        return

    def _factory(system_prompt: str):
        def _make(llm):
            return Agent(llm=llm, system_prompt=system_prompt)  # type: ignore[arg-type]
        return _make

    register_agent_if_absent(
        "clarify_harness_writer",
        _factory(_HARNESS_WRITER_PROMPT),
        AgentDefinition(
            name="clarify_harness_writer",
            description=(
                "Writes the shared KLEE harness scaffold (core_abi.hpp, "
                "harness_*.cpp, mock_*.cpp placeholders) for a clarify task."
            ),
            tools=_CLARIFY_FILE_TOOLS,
            system_prompt=_HARNESS_WRITER_PROMPT,
        ),
    )

    register_agent_if_absent(
        "clarify_simulation_writer",
        _factory(_SIMULATION_WRITER_PROMPT),
        AgentDefinition(
            name="clarify_simulation_writer",
            description=(
                "Claims a private KLEE variant directory, implements one "
                "candidate mock_*.cpp simulation, and validates with klee_solve."
            ),
            tools=_CLARIFY_SOLVE_TOOLS,
            system_prompt=_SIMULATION_WRITER_PROMPT,
        ),
    )

    register_agent_if_absent(
        "clarify_disambiguation_analyst",
        _factory(_DISAMBIGUATION_ANALYST_PROMPT),
        AgentDefinition(
            name="clarify_disambiguation_analyst",
            description=(
                "Reads cross-validation cluster reports and mock files; "
                "outputs a JSON list of root-cause ambiguity records."
            ),
            tools=_CLARIFY_ANALYST_TOOLS,
            system_prompt=_DISAMBIGUATION_ANALYST_PROMPT,
        ),
    )
