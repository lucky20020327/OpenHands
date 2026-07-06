"""Clarify orchestrator prompt builder.

Converts the ADK ``user_query.j2`` template to a Python function that builds
the full clarify orchestrator prompt for the OpenHands runtime.

Tool-name mapping vs. the ADK version:
  cross_validation            → clarify_cross_validation
  user_request_completed      → clarify_task_done
  klee_solve                  → clarify_klee_solve
  delegate_task(harness_writer)      → start sub-agent clarify_harness_writer
  delegate_task(simulation_writer)   → start sub-agent clarify_simulation_writer
  delegate_task(disambiguation_analyst) → start sub-agent clarify_disambiguation_analyst
"""

from __future__ import annotations

import json

__all__ = ["build_orchestrator_prompt"]

_AMBIGUITY_CLASSES = """\
## Divergence Classification: Four Classes

Every divergence root cause belongs to exactly one of these four classes.
The **judgment criterion is the feature request text only** — never test
patches, gold patches, or f2p lists.

### 1. `design_ambiguity` (primary target)
The feature request leaves the behavior undefined. Multiple valid interpretations
exist; the request text does NOT uniquely determine the correct behavior.

**Detect**: Locate the divergence point in the user-provided feature request or
specification material. If the request is silent or genuinely ambiguous about that behavior, and multiple
implementations could each claim faithfulness → `design_ambiguity`.

**Example**: "return appropriate error on NULL" without specifying which error
code.

### 2. `benign_choice`
The request is silent about the diverging behavior, but the divergence does
**not** affect the externally observable contract. Different implementations
converge on the same external behavior despite internal differences.

**Example**: Different internal variable names or loop structures with identical
outputs.

### 3. `implementation_error`
A variant **violates something the request explicitly states**. The request is
clear; the variant simply did not follow it.

**Detect**: Find the relevant request text. If request says X and variant
B does not-X → `implementation_error` for variant B only.

**Example**: Request says "must not return dstSize_tooSmall when dst is NULL and
srcSize is 0", but variant A returns that error.

### 4. `simulation_artifact`
The divergence is caused by KLEE abstraction choices (POD approximation of a
complex type, stub modeling noise) unrelated to the spec. The divergence would
disappear with a more faithful simulation.

**Example**: Different stub buffer size choices for string fields producing path
divergences with no semantic meaning.
"""

_REPORT_FORMAT = """\
## Final Report Format

{ambiguity_classes}

Compile and submit via the final `clarify_task_done` tool call. The report is
for feature-request authors and designers, not for KLEE developers.

When calling `clarify_task_done`:
- Set `status` to `complete` if analysis finished.
- Set `summary` to a one- or two-sentence overview only.
- Set `report` to the complete markdown report below, including every section
  and ambiguity entry. Do not put only a short summary in `report`.

### Reporting Principles

- Lead with the ambiguity in the request, not with the detection mechanics.
- Do not expose low-level internal details in the main ambiguity entries: avoid
  `klee_<N>`, `.ktest`, cluster file paths, raw replay statuses, or long
  mock.cpp snippets.
- It is acceptable to mention evidence type in plain language: "interface
  modeling", "implementation self-report", or "behavioral divergence".
- For each ambiguity, clearly show: request text or silence → ambiguity cause
  → reasonable interpretations → observable impact → clarification needed.
- `clarification_needed` is not a code fix suggestion. It is the exact question
  or spec decision the request author should answer.
- If you cannot explain why the request text permits multiple readings, do not
  list it as an ambiguity; classify it as implementation error or simulation
  artifact instead.

---

### Feature Request Ambiguity Report

**Feature**: [task_id + one-line description]
**Entry point(s)**: [names of the entry points modeled]
**Analysis scope**: [briefly describe modeled entry points and any important
uncovered areas]

#### Summary
- Design ambiguities requiring request clarification: [N]
- Benign choices observed: [N]
- Implementation errors/noise excluded from ambiguity list: [N]
- Most important clarification needed: [one sentence, or "none"]

#### Ambiguity Points

For each `design_ambiguity` or `benign_choice` root cause:

**[A1]** [One-sentence summary]
- **Class**: design_ambiguity | benign_choice
- **Confidence**: [1-10]
- **Request text**: [Quote the relevant request sentence/paragraph,
  or "The request is silent about <specific decision>"]
- **Ambiguity cause**: [Explain the exact missing condition, undefined term,
  vague verb, competing references, unspecified error/output, or unspecified
  dependency boundary. This must answer: why can reasonable readers disagree?]
- **Possible interpretations**:
  1. [Business-language interpretation and resulting behavior]
  2. [Business-language interpretation and resulting behavior]
  [Add more if needed]
- **Observed impact**: [What user-visible behavior, returned value, error
  handling, side effect, or external dependency choice changes]
- **Clarification needed**: [Concrete question or decision to add to the
  request, e.g. "Specify whether duplicate metadata entries should count once
  or per occurrence."]
- **Evidence**: interface_model | self_report | behavioral | static_analysis |
  (any combination)

For each `implementation_error`, keep it separate from ambiguities:

**[E1]** [One-sentence summary]
- **Class**: implementation_error
- **Confidence**: [1-10]
- **Request text**: [Quote the specific text being violated]
- **Why not an ambiguity**: [Explain why the request is already clear]
- **What went wrong**: [Specific faithfulness error, without overstating it as
  a spec gap]

For each important `simulation_artifact`, summarize only if it affects trust in
the results:

**[S1]** [One-sentence summary]
- **Class**: simulation_artifact
- **Cause**: [KLEE/modeling limitation]
- **Effect on report**: [Why it was excluded or how it limits confidence]

#### Uncovered Areas
[List any feature areas that were hard to model, with the reason and whether
this may hide additional ambiguity.]

---
"""

_PERSONA_BLOCK = """\
#### Persona mechanism (amplify divergence detection)

Divide {n_variants} variants into two groups with different decision
tiebreakers. Append to each variant's prompt:

**Persona A (minimal, variants 1 ~ {half_variants})**:
```
<decision_tiebreaker>
When the feature request is silent about a behavior and no other context resolves it:
- Unresolved condition: treat as "not applicable", return early with no-op
- Multiple possible real external interfaces to call: call only the most essential repo-evidenced one
- Conditionally-described operation with no stated condition: skip the operation
</decision_tiebreaker>
```

**Persona B (expansive, variants {half_plus_one} ~ {n_variants})**:
```
<decision_tiebreaker>
When the feature request is silent about a behavior and no other context resolves it:
- Unresolved condition: choose the most conservative branch and continue
- Multiple possible real external interfaces to call: call all plausible repo-evidenced ones
- Conditionally-described operation with no stated condition: infer the condition from context and execute
</decision_tiebreaker>
```
"""


def build_orchestrator_prompt(
    task: dict,
    mode: str = "hybrid",
    n_variants: int = 4,
) -> str:
    """Build the full Clarify orchestrator prompt for OpenHands.

    Parameters
    ----------
    task:
        Task dict with at minimum ``task_id`` and ``feature_request``.
        May also contain opaque ``metadata`` supplied by the caller.
    mode:
        One of ``"hybrid"`` (default), ``"bold"``, ``"self_check_only"``,
        ``"difference_only"``.
    n_variants:
        Number of simulation_writer variants to spawn (used in hybrid /
        self_check_only modes).
    """
    half = max(1, n_variants // 2)
    half_plus_one = half + 1

    header = (
        "You are a **Spec Ambiguity Analyst** for software feature requests. "
        "Your mission: generate N independent C simulations of a feature "
        "request, cross-validate their behavior using KLEE symbolic execution, "
        "and produce an ambiguity report that helps the feature-request author "
        "understand what is unclear and what decision would disambiguate it. "
        "Do not suggest code fixes; do include clarification questions or "
        "specification decisions."
    )

    if mode == "bold":
        return _build_bold_prompt(task, header)
    if mode == "self_check_only":
        return _build_self_check_only_prompt(task, header, n_variants, half, half_plus_one)
    # hybrid (default) and difference_only both use the full KLEE pipeline
    return _build_hybrid_prompt(task, header, mode, n_variants, half, half_plus_one)


def _task_id(task: dict) -> str:
    return str(task.get("task_id") or "unknown")


def _feature_request(task: dict) -> str:
    return str(task.get("feature_request") or "")


def _task_context_lines(task: dict) -> list[str]:
    lines = [f"Task ID: {_task_id(task)}"]
    metadata = task.get("metadata")
    if isinstance(metadata, dict) and metadata:
        lines.extend([
            "Metadata:",
            "```json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
            "```",
        ])
    return lines


# ---------------------------------------------------------------------------
# Mode builders
# ---------------------------------------------------------------------------

def _build_bold_prompt(task: dict, header: str) -> str:
    feature_request = _feature_request(task)

    lines = [
        header,
        "",
        "## Mode: bold (spec-only, no KLEE)",
        "",
        "Running in **bold mode**. No KLEE variants, no cross-validation, "
        "no sub-agents.",
        "Use the user request and current workspace contents directly to "
        "perform static ambiguity analysis.",
        "",
        "## Mission",
        "",
        "Produce a \"Feature Request Ambiguity Report\" via static analysis of "
        "the user-provided feature request/specification.",
        "",
        "## Execution Steps",
        "",
        "### Step 1: Inspect the prepared workspace",
        "",
        "The business workspace has already been prepared by the caller. Do not "
        "call a business workspace preparation tool and do not assume fixed "
        "filenames. Use the user request plus whatever files/paths the user "
        "provided or that are present in the current workspace.",
        "",
        "### Step 2: Static analysis",
        "",
        "Identify points where the feature request is silent, ambiguous, or "
        "leaves behavior undefined. Classify each as `design_ambiguity`, "
        "`benign_choice`, `implementation_error`, or `simulation_artifact`.",
        "- All findings marked as source: \"static analysis\".",
        "- Output the final report via `clarify_task_done`, with the complete "
        "report in the `report` field.",
        "",
        _REPORT_FORMAT.format(ambiguity_classes=_AMBIGUITY_CLASSES),
        "## Important Rules",
        "",
        "1. **Test-free**: NEVER look at `test_patch`, `gold_patch`, `f2p`, or "
        "test files. These are invisible to the method.",
        "2. **No web tools**: Do not use browser, web search, Tavily, or "
        "internet-fetch tools. Use only the Clarify workspace and public "
        "reference files provided there.",
        "3. **Actionable ambiguity reporting**: Do not suggest code fixes. Do "
        "state the spec clarification question or decision needed to remove "
        "each ambiguity.",
        "4. **Full report handoff**: The final `clarify_task_done.report` field "
        "must contain the full Feature Request Ambiguity Report, not just the "
        "summary section.",
        "",
        *_task_context_lines(task),
        "",
        "Feature request:",
        feature_request,
    ]
    return "\n".join(lines)


def _build_self_check_only_prompt(
    task: dict,
    header: str,
    n_variants: int,
    half: int,
    half_plus_one: int,
) -> str:
    feature_request = _feature_request(task)
    persona = _PERSONA_BLOCK.format(
        n_variants=n_variants,
        half_variants=half,
        half_plus_one=half_plus_one,
    )

    lines = [
        header,
        "",
        "## Mode: self_check_only (simulation variants only, no cross-validation)",
        "",
        "Running in **self_check_only mode**. Spawn N simulation_writer variants "
        "but do NOT run `clarify_cross_validation`.",
        "Final report covers only self-reported ambiguities from "
        "`fuzzy_design_report`.",
        "",
        "## Mission",
        "",
        "Produce a \"Feature Request Ambiguity Report\" from simulation_writer "
        "self-reports only.",
        "",
        "## Execution Steps (strictly in order)",
        "",
        "### Step 1: Inspect the prepared workspace",
        "",
        "The business workspace has already been prepared by the caller. Do not "
        "call a business workspace preparation tool and do not assume fixed "
        "filenames or directories. Use the user request and current workspace "
        "contents as the source of truth.",
        "",
        "### Step 2: Prepare KLEE scaffold",
        "",
        "Call `clarify_prepare_klee`. This creates only OpenHands-clarify KLEE "
        "support files under an internal scaffold directory; it does not create "
        "or modify business/specification workspace content. Pass the returned "
        "`scaffold_dir` to harness_writer.",
        "",
        "### Step 3: Serial — harness_writer",
        "",
        "Start the `clarify_harness_writer` sub-agent **once**:",
        "- This step is **serial** — wait for it to complete before proceeding",
        "- Give only the high-level goal in the task prompt. Do not restate a "
        "file-by-file workflow; harness_writer's own instruction decides how "
        "to gather evidence from the workspace.",
        "- harness_writer reads the user request plus relevant workspace "
        "materials, infers a request-coverage-preserving symbolic input ABI "
        "for one or more entry points, and writes the shared KLEE scaffold at "
        "the `scaffold_dir` returned by `clarify_prepare_klee`.",
        "- harness_writer returns a `fuzzy_design_report` capturing "
        "interface-level ambiguities",
        "",
        "### Step 4: Parallel — simulation_writer variants",
        "",
        f"Start **{n_variants} clarify_simulation_writer sub-agents in a "
        "single response** (parallel):",
        "- All N calls in the **same assistant turn** (parallel execution)",
        "- Keep each task prompt focused on the variant goal/persona.",
        "- Wait for all variants to complete",
        "",
        persona,
        "",
        "### Step 5 (self_check_only mode): Compile report",
        "",
        "Collect:",
        "- harness_writer's `fuzzy_design_report` (returned in Step 3)",
        "- all simulation_writers' `fuzzy_design_report` fields (returned in "
        "Step 4)",
        "",
        "Deduplicate and merge. Submit the final report via `clarify_task_done`, "
        "with the complete report in the `report` field.",
        "Mark harness_writer findings source: \"interface_model\"; "
        "simulation_writer findings source: \"self_report\".",
        "",
        _REPORT_FORMAT.format(ambiguity_classes=_AMBIGUITY_CLASSES),
        _IMPORTANT_RULES,
        "",
        *_task_context_lines(task),
        "",
        "Feature request:",
        feature_request,
    ]
    return "\n".join(lines)


def _build_hybrid_prompt(
    task: dict,
    header: str,
    mode: str,
    n_variants: int,
    half: int,
    half_plus_one: int,
) -> str:
    feature_request = _feature_request(task)
    persona = _PERSONA_BLOCK.format(
        n_variants=n_variants,
        half_variants=half,
        half_plus_one=half_plus_one,
    )
    difference_only_note = (
        "\n> **difference_only mode**: simulation_writer skips self-check; "
        "`fuzzy_design_report` will be `[]`. This is expected. Attribution is "
        "fully handled by disambiguation_analyst.\n"
        if mode == "difference_only"
        else ""
    )

    lines = [
        header,
        "",
        "## Mission",
        "",
        "Produce a \"Feature Request Ambiguity Report\" combining three "
        "independent evidence sources:",
        "1. **Interface model**: harness_writer's `fuzzy_design_report` (spec "
        "gaps found while modeling request-relevant inputs/state and ABI)",
        "2. **Self-reports**: each simulation_writer's `fuzzy_design_report` "
        "(spec uncertainty noticed during implementation)",
        "3. **Behavioral divergences**: `clarify_cross_validation` cluster "
        "analysis (what actually diverged between variants)",
        "",
        "## Execution Steps (strictly in order)",
        "",
        "### Step 1: Inspect the prepared workspace",
        "",
        "The business workspace has already been prepared by the caller. Do not "
        "call a business workspace preparation tool and do not assume fixed "
        "filenames or directories. Use the user request and current workspace "
        "contents as the source of truth.",
        "",
        "### Step 2: Prepare KLEE scaffold",
        "",
        "Call `clarify_prepare_klee`. This creates only OpenHands-clarify KLEE "
        "support files under an internal scaffold directory; it does not create "
        "or modify business/specification workspace content. Pass the returned "
        "`scaffold_dir` to harness_writer.",
        "",
        "### Step 3: Serial — harness_writer",
        "",
        "Start the `clarify_harness_writer` sub-agent **once**:",
        "- This step is **serial** — wait for it to complete before proceeding",
        "- Give only the high-level goal in the task prompt. Do not restate a "
        "file-by-file workflow; harness_writer's own instruction decides how to "
        "use `ls`, `grep`, and `cat` to gather evidence from whatever reference "
        "material the workspace provides.",
        "- harness_writer reads the user request plus relevant workspace "
        "materials, infers a request-coverage-preserving symbolic input ABI "
        "for one or more entry points, and writes the shared KLEE scaffold at "
        "the `scaffold_dir` returned by `clarify_prepare_klee`.",
        "- harness_writer returns a `fuzzy_design_report` capturing "
        "interface-level ambiguities (including when the request does not "
        "clearly delimit which interfaces are in scope)",
        "",
        "**Why**: A shared harness guarantees all variants use **identical "
        "symbolic inputs** — enabling direct KLEE cross-replay without post-hoc "
        "ABI clustering. harness_writer fixes the request-relevant input/state "
        "ABI and the entry-point set, without hiding request-relevant "
        "object/global/environment state behind opaque IDs. Which real external "
        "dependencies get mocked and how they behave remains open for each "
        "simulation_writer to decide independently.",
        "",
        "### Step 4: Parallel — simulation_writer variants",
        "",
        f"Start **{n_variants} clarify_simulation_writer sub-agents in a "
        "single response** (parallel):",
        "- All N calls in the **same assistant turn** (parallel execution)",
        "- Keep each task prompt focused on the variant goal/persona. Do not "
        "provide a fixed exploration recipe; simulation_writer's own instruction "
        "decides how to gather repo evidence.",
        "- Do NOT read spec files yourself before delegation — simulation_writer "
        "reads them",
        "- Wait for all variants to complete",
        "",
        "**Key design**: Each simulation_writer **independently decides** which "
        "real repository/external interfaces to call and how to stub them. "
        "`call_stub` is for mocking real repo symbols or real "
        "external/system boundaries that should not be executed inside KLEE, "
        "not for inventing arbitrary internal helpers. When the feature request "
        "is ambiguous about which real dependencies to invoke, different variants "
        "will make different choices, producing L2 (external call) divergences "
        "that directly surface the ambiguity.",
        "",
        difference_only_note,
        persona,
        "",
        "Each simulation_writer will independently:",
        "1. Read the user request/specification material plus the harness "
        "scaffold from `variant_dir`",
        "2. Decide which real repository/external interfaces to call and "
        "implement only those as `call_stub` stubs in `mock.cpp`",
        "3. Implement every tagged entry point's business logic in `mock.cpp`",
        "4. Run `clarify_klee_solve` once as a self-check; it produces the "
        "ktests that `clarify_cross_validation` will consume",
        "5. Return `fuzzy_design_report` and KLEE results",
        "",
        "### Step 5: Cross-validation",
        "",
        "After all variants complete, call `clarify_cross_validation` (no "
        "arguments). This tool:",
        "1. Uses the variants claimed in Clarify state (or discovers internal "
        "`klee_<i>/` directories)",
        "2. Merges all ktest files into a unified pool",
        "3. Replays each ktest against each variant",
        "4. Returns: statistics + divergence cluster analysis with decoded "
        "execution paths",
        "",
        "Two types of divergence to watch for:",
        "- **L2 (external call divergence)**: variants call different external "
        "functions, or in different order/parameters — directly reflects spec "
        "ambiguity about which dependencies to invoke",
        "- **L3 (behavior divergence)**: variants call the same functions but "
        "produce different outputs — reflects spec ambiguity about the core logic",
        "",
        "**KLEE replay semantics**:",
        "- In `KLEE_REPLAY` mode, `klee_assume` and `assume_str_len_in` do not "
        "impose constraints — replay uses concrete bytes from the ktest.",
        "- `KLEE_RUN_TEST_ERROR: out of inputs` means a replayed variant "
        "requested more symbolic input objects than the ktest contains, usually "
        "because it took a longer control-flow path or made an extra stub call. "
        "`.early` / `.err` truncated ktests are filtered before "
        "cross-validation, so treat this as behavior divergence evidence and "
        "trace it to `mock.cpp` + the request/specification material.",
        "",
        "The `clarify_cross_validation` return value will indicate whether to:",
        "- Do inline analysis (few clusters), or",
        "- Delegate to `clarify_disambiguation_analyst` sub-agents (many clusters)",
        "",
        "Follow its instructions.",
        "",
        "### Step 6: Attribution (disambiguation_analyst if directed)",
        "",
        "If `clarify_cross_validation` directs you to use "
        "`clarify_disambiguation_analyst`:",
        "- Delegate in batches (batch_size=10 clusters per sub-agent)",
        "- Wait for all disambiguation_analyst agents to complete",
        "- Collect their 4-class JSON results",
        "",
        "If doing inline analysis:",
        "1. **Read all mock.cpp files** in parallel (one tool call for all "
        "`klee_<i>/mock.cpp`)",
        "2. For each cluster: identify what differs (L2: which calls; L3: what "
        "outputs), trace to the feature request text",
        "3. Classify: `design_ambiguity` / `benign_choice` / "
        "`implementation_error` / `simulation_artifact`",
        "",
        "**Iron rule**: Do NOT read test_patch, gold_patch, f2p, or any test "
        "files. Attribution is based solely on the user-provided request/"
        "specification, mock.cpp code, and divergence data.",
        "",
        _REPORT_FORMAT.format(ambiguity_classes=_AMBIGUITY_CLASSES),
        _IMPORTANT_RULES,
        "",
        f"Clarify mode: {mode}",
        *_task_context_lines(task),
        "",
        "Feature request:",
        feature_request,
    ]
    return "\n".join(lines)


_IMPORTANT_RULES = """\
## Important Rules

1. **Test-free**: NEVER look at `test_patch`, `gold_patch`, `f2p`, or test
   files. These are invisible to the method.
2. **Serial before parallel**: harness_writer must complete before spawning
   simulation_writers.
3. **No web tools**: Do not use browser, web search, Tavily, or internet-fetch
   tools. Use only the Clarify workspace and public reference files provided
   there.
4. **Read before delegating**: Do not pre-read repo code yourself before
   delegating to harness_writer or simulation_writer — they handle that.
5. **Actionable ambiguity reporting**: Do not suggest code fixes. Do state the
   spec clarification question or decision needed to remove each ambiguity.
6. **Persona diversity**: Use different decision tiebreakers across variant
   groups to amplify spec ambiguity signal.
7. **L2 is a first-class signal**: External call divergences (which functions
   variants chose to call) are as meaningful as behavioral divergences — do not
   dismiss them as noise.
8. **Full report handoff**: The final `clarify_task_done.report` field must
   contain the full Feature Request Ambiguity Report, not just the summary
   section."""
