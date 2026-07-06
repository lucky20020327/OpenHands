from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from openhands.sdk.tool.tool import DeclaredResources
from openhands.clarify.klee.config import get_klee_config
from openhands.clarify.tools.state import (
    ClarifyState,
    load_state,
    save_state,
    state_as_public_dict,
    state_path_for,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState


CORE_ABI_PLACEHOLDER = """#pragma once
// Placeholder shared ABI. The harness-writing step should replace this with
// CoreInput/CoreOutput definitions and the core function declaration.
"""

KLEE_HELPERS_HPP = """#pragma once
#include <klee/klee.h>

template <typename T>
inline T clarify_symbolic(const char* name) {
  T value{};
  klee_make_symbolic(&value, sizeof(value), name);
  return value;
}
"""

KLEE_RULES = """# KLEE Implementation Rules

- Keep harness and mock code deterministic.
- Do not read hidden tests, reference patches, or oracle artifacts.
- Put shared ABI declarations in `core_abi.hpp`.
- Use paired `harness_<i>.cpp` and `mock_<i>.cpp` files for each entry point.
- Run `clarify_klee_solve` after writing or editing a variant.
"""


def _text_observation(
    text: str,
    *,
    is_error: bool = False,
    command: str,
    data: dict[str, Any] | None = None,
) -> "ClarifyObservation":
    return ClarifyObservation.from_text(
        text=text,
        is_error=is_error,
        command=command,
        data=data or {},
    )


def _repo_root_from_working_dir(working_dir: str) -> Path:
    return Path(working_dir).resolve()


def _safe_name(value: str | None, fallback: str) -> str:
    text = "".join(c if c.isalnum() or c in "._-" else "_" for c in (value or ""))
    return text.strip("._-") or fallback


def _truncate_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"[..{len(text) - max_chars} chars truncated..]\n{text[-max_chars:]}"


def _discover_klee_sh() -> Path | None:
    candidates: list[Path] = []
    if os.getenv("KLEE_SH"):
        candidates.append(Path(os.environ["KLEE_SH"]))
    if os.getenv("OPENHANDS_CLARIFY_KLEE_SH"):
        candidates.append(Path(os.environ["OPENHANDS_CLARIFY_KLEE_SH"]))
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "klee" / "runners" / "klee_sh" / "klee.sh")
    path_value = shutil.which("klee.sh")
    if path_value:
        candidates.append(Path(path_value))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _count_ktests(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for item in path.glob("test*.ktest") if item.is_file())


def _build_workspace_tree(root: Path, max_depth: int = 3) -> str:
    """Build a compact directory tree string limited to *max_depth* levels."""

    def _tree_lines(dir_path: Path, prefix: str = "", depth: int = 0) -> list[str]:
        if depth >= max_depth:
            return [f"{prefix}..."]
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except PermissionError:
            return [f"{prefix}<permission denied>"]
        entries = [e for e in entries if not e.name.startswith((".", "_"))]
        lines: list[str] = []
        for idx, entry in enumerate(entries):
            is_last = idx == len(entries) - 1
            connector = "└── " if is_last else "├── "
            extension = "    " if is_last else "│   "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                child_max = depth + 1 if entry.name in {"repo", "testbed"} else max_depth
                lines.extend(_tree_lines(entry, prefix + extension, child_max))
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
        return lines

    result = ["<workspace>"]
    result.extend(_tree_lines(root))
    result.append("</workspace>")
    return "\n".join(result)



def _read_info_summary(info_path: Path) -> dict[str, Any]:
    if not info_path.is_file():
        return {}
    summary: dict[str, Any] = {}
    for line in info_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key and value:
            summary[key] = value
    return summary


def _parse_replay_index(index_path: Path) -> dict[str, Any]:
    if not index_path.is_file():
        return {"total": 0, "ok": 0, "failed": 0, "items": []}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"total": 0, "ok": 0, "failed": 0, "items": []}
    return data if isinstance(data, dict) else {"total": 0, "ok": 0, "failed": 0, "items": []}


def _load_replay_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _behavior_signature(replay_json: dict[str, Any]) -> str:
    """Compute a stable behavior signature from one replay JSON result."""
    if not replay_json:
        return "missing"
    normalized = {
        key: value
        for key, value in replay_json.items()
        if key not in {"ktest", "duration_ms", "elapsed_ms", "timestamp"}
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)


def _cluster_divergent_ktests(
    divergent: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in divergent:
        signatures = item.get("signatures")
        if not isinstance(signatures, dict):
            continue
        key = json.dumps(signatures, ensure_ascii=False, sort_keys=True, default=str)
        grouped.setdefault(key, []).append(item)

    clusters: list[dict[str, Any]] = []
    for cluster_id, (_signature_key, items) in enumerate(
        sorted(
            grouped.items(),
            key=lambda pair: (-len(pair[1]), str(pair[1][0].get("ktest", ""))),
        ),
        start=1,
    ):
        representative = items[0]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "size": len(items),
                "ktests": [str(item.get("ktest", "")) for item in items],
                "statuses_by_ktest": {
                    str(item.get("ktest", "")): item.get("statuses", {})
                    for item in items
                },
                "signatures_by_variant": representative.get("signatures", {}),
            }
        )
    return clusters


def _markdown_cell(value: Any, *, max_chars: int = 500) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > max_chars:
        text = text[: max_chars - 18] + "...[truncated]"
    return text.replace("|", "\\|").replace("\n", " ")


def _write_cluster_markdown_files(
    *,
    cross_val_dir: Path,
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_id = int(cluster["cluster_id"])
        path = cross_val_dir / f"cluster_{cluster_id:03d}.md"
        ktests = [str(name) for name in cluster.get("ktests", [])]
        statuses = cluster.get("statuses_by_ktest", {})
        signatures = cluster.get("signatures_by_variant", {})

        lines = [
            f"# Clarify Cross-Validation Cluster {cluster_id}",
            "",
            f"- divergent_ktest_count: {cluster.get('size', len(ktests))}",
            "",
            "## Ktests",
            "",
            "| ktest | statuses |",
            "| --- | --- |",
        ]
        for ktest in ktests:
            status_payload = statuses.get(ktest, {}) if isinstance(statuses, dict) else {}
            lines.append(f"| `{ktest}` | `{_markdown_cell(status_payload)}` |")

        lines.extend(
            [
                "",
                "## Representative Signatures",
                "",
                "| variant | signature |",
                "| --- | --- |",
            ]
        )
        if isinstance(signatures, dict):
            for variant_id in sorted(signatures, key=str):
                lines.append(
                    f"| `{variant_id}` | `{_markdown_cell(signatures[variant_id], max_chars=2000)}` |"
                )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        summaries.append(
            {
                "cluster_id": cluster_id,
                "size": cluster.get("size", len(ktests)),
                "path": str(path),
                "ktests": ktests[:50],
                "signatures_by_variant": signatures,
            }
        )
    return summaries


def _sample_ktest_paths(paths: list[Path], *, max_count: int) -> list[Path]:
    if len(paths) <= max_count:
        return paths
    if max_count == 1:
        return [paths[0]]
    selected: list[Path] = []
    last_index = len(paths) - 1
    for offset in range(max_count):
        index = round(offset * last_index / (max_count - 1))
        selected.append(paths[index])
    return selected


def _merge_variant_ktests(
    *,
    variants: list[tuple[int, Path]],
    merged_dir: Path,
    max_ktests_per_variant: int,
) -> list[str]:
    if merged_dir.exists():
        shutil.rmtree(merged_dir)
    merged_dir.mkdir(parents=True, exist_ok=True)
    merged_names: list[str] = []
    for variant_id, variant_dir in variants:
        sources = sorted((variant_dir / "out" / "klee-out").glob("test*.ktest"))
        for source in _sample_ktest_paths(
            sources,
            max_count=max_ktests_per_variant,
        ):
            target_name = f"test_v{variant_id}_{source.stem}.ktest"
            shutil.copy2(source, merged_dir / target_name)
            merged_names.append(target_name.removesuffix(".ktest"))
    return sorted(merged_names)


def _discover_solved_variants(workspace: Path) -> list[tuple[int, Path]]:
    variants: list[tuple[int, Path]] = []
    for path in sorted(workspace.glob("klee_*")):
        if not path.is_dir():
            continue
        try:
            variant_id = int(path.name.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if _count_ktests(path / "out" / "klee-out") > 0:
            variants.append((variant_id, path))
    return sorted(variants)


def _write_failure_diagnostics(
    *,
    variant_dir: Path,
    command: list[str],
    stdout: str,
    stderr: str,
    returncode: int,
) -> list[str]:
    diagnostics_dir = variant_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    payload = {
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    json_path = diagnostics_dir / f"klee_solve_failure_{stamp}.json"
    log_path = diagnostics_dir / f"klee_solve_failure_{stamp}.log"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log_path.write_text(
        "COMMAND\n"
        + " ".join(command)
        + "\n\nSTDOUT\n"
        + stdout
        + "\n\nSTDERR\n"
        + stderr,
        encoding="utf-8",
    )
    return [str(json_path), str(log_path)]


def _write_replay_failure_diagnostics(
    *,
    cross_val_dir: Path,
    variant_id: int,
    command: list[str],
    stdout: str,
    stderr: str,
    returncode: int,
) -> list[str]:
    diagnostics_dir = cross_val_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    payload = {
        "variant_id": variant_id,
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    json_path = diagnostics_dir / f"klee_replay_failure_v{variant_id}_{stamp}.json"
    log_path = diagnostics_dir / f"klee_replay_failure_v{variant_id}_{stamp}.log"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log_path.write_text(
        "COMMAND\n"
        + " ".join(command)
        + "\n\nSTDOUT\n"
        + stdout
        + "\n\nSTDERR\n"
        + stderr,
        encoding="utf-8",
    )
    return [str(json_path), str(log_path)]


_TRACE_LOCK = __import__("threading").Lock()


def _append_trace_event(workspace: Path | None, event: dict[str, Any]) -> None:
    """Append one JSON line to ``{workspace}/trace.jsonl`` (best-effort)."""
    if workspace is None:
        return
    try:
        trace_path = workspace / "trace.jsonl"
        workspace.mkdir(parents=True, exist_ok=True)
        with _TRACE_LOCK:
            with trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


class ClarifyObservation(Observation):
    command: str = Field(description="Clarify command that was executed.")
    data: dict[str, Any] = Field(default_factory=dict)


class ClarifyWorkspaceGenerateAction(Action):
    instance_id: str = Field(description="Benchmark instance id.")
    feature_request: str = Field(description="Public feature request text.")
    dataset: str = Field(default="featurebench")
    base_commit: str | None = None
    repo: str | None = Field(
        default=None,
        description="Repository slug (e.g. owner/repo), if known.",
    )
    language: str | None = Field(
        default=None,
        description="Primary programming language of the benchmark (e.g. 'python', 'c', 'java').",
    )
    core_func: str | None = Field(
        default=None,
        description="Name of the core function being specified (for C/C++ KLEE harness).",
    )
    entry_points: list[str] = Field(
        default_factory=list,
        description="Entry-point function signatures, if available from dataset metadata.",
    )
    n_variants: int = Field(
        default=2,
        ge=1,
        le=8,
        description="Number of KLEE simulation variants to generate (default 2).",
    )
    workspace_name: str | None = Field(
        default=None,
        description="Optional workspace directory name under .openhands/clarify.",
    )


class ClarifyClaimVariantAction(Action):
    variant_name: str | None = Field(
        default=None,
        description="Optional human-readable variant name.",
    )


class ClarifyKleeSolveAction(Action):
    variant_id: int | None = Field(
        default=None,
        description="Variant id returned by clarify_claim_variant. Defaults to 1.",
    )
    max_seconds: int | None = Field(default=None, ge=1, le=1800)
    max_forks: int | None = Field(default=None, ge=1, le=1000000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=7200)


class ClarifyCrossValidationAction(Action):
    command: Literal["summarize"] = "summarize"


class ClarifyTaskDoneAction(Action):
    status: Literal["complete", "incomplete"]
    summary: str
    report: str = ""


class ClarifyExecutor(ToolExecutor[Action, ClarifyObservation]):
    def __init__(self, *, working_dir: str, persistence_dir: str | None):
        self.working_dir = str(Path(working_dir).resolve())
        self.state_path = state_path_for(
            persistence_dir=persistence_dir,
            working_dir=self.working_dir,
        )

    def _load(self) -> ClarifyState:
        return load_state(self.state_path)

    def _save(self, state: ClarifyState) -> None:
        save_state(state)

    def __call__(
        self,
        action: Action,
        conversation: "LocalConversation | None" = None,  # noqa: ARG002
    ) -> ClarifyObservation:
        t0 = time.monotonic()
        if isinstance(action, ClarifyWorkspaceGenerateAction):
            obs = self.workspace_generate(action)
        elif isinstance(action, ClarifyClaimVariantAction):
            obs = self.claim_variant(action)
        elif isinstance(action, ClarifyKleeSolveAction):
            obs = self.klee_solve(action)
        elif isinstance(action, ClarifyCrossValidationAction):
            obs = self.cross_validation(action)
        elif isinstance(action, ClarifyTaskDoneAction):
            obs = self.task_done(action)
        else:
            obs = _text_observation(
                f"Unsupported Clarify action: {action.__class__.__name__}",
                is_error=True,
                command="unknown",
            )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        workspace = self._workspace_path()
        trace_event: dict[str, Any] = {
            "tool": obs.command,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": round(elapsed_ms, 1),
            "is_error": obs.is_error,
            "data": {
                k: v
                for k, v in obs.data.items()
                if k not in ("stdout_tail", "stderr_tail")
            },
        }
        if "returncode" in obs.data:
            trace_event["returncode"] = obs.data["returncode"]
        _append_trace_event(workspace, trace_event)
        return obs

    def _workspace_path(self) -> Path | None:
        """Return the current clarify workspace path, or None if not yet set."""
        try:
            state = self._load()
            if state.workspace:
                return Path(state.workspace)
        except Exception:  # noqa: BLE001
            pass
        return None

    def workspace_generate(
        self, action: ClarifyWorkspaceGenerateAction
    ) -> ClarifyObservation:
        repo_root = _repo_root_from_working_dir(self.working_dir)
        state = self._load()
        workspace_name = action.workspace_name or _safe_name(
            action.instance_id, "task"
        )
        workspace = repo_root / ".openhands" / "clarify" / workspace_name
        klee_dir = workspace / "klee"
        klee_dir.mkdir(parents=True, exist_ok=True)

        (workspace / "feature_request.md").write_text(
            action.feature_request.strip() + "\n",
            encoding="utf-8",
        )
        metadata: dict[str, Any] = {
            "instance_id": action.instance_id,
            "dataset": action.dataset,
            "base_commit": action.base_commit,
            "repo": action.repo,
            "language": action.language,
            "core_func": action.core_func,
            "entry_points": action.entry_points,
            "n_variants": action.n_variants,
            "repo_path": str(repo_root),
        }
        # Strip None values for cleaner metadata.json
        metadata = {k: v for k, v in metadata.items() if v is not None}
        (workspace / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (klee_dir / "core_abi.hpp").write_text(CORE_ABI_PLACEHOLDER, encoding="utf-8")
        (klee_dir / "klee_helpers.hpp").write_text(KLEE_HELPERS_HPP, encoding="utf-8")
        (klee_dir / "KLEE_IMPLEMENTATION_RULES.md").write_text(
            KLEE_RULES,
            encoding="utf-8",
        )

        # Write a task brief that summarises important context for sub-agents.
        brief_lines = [
            f"# Task Brief: {action.instance_id}",
            "",
            f"**Dataset:** {action.dataset}",
        ]
        if action.repo:
            brief_lines.append(f"**Repo:** {action.repo}")
        if action.base_commit:
            brief_lines.append(f"**Base commit:** {action.base_commit}")
        if action.language:
            brief_lines.append(f"**Language:** {action.language}")
        if action.core_func:
            brief_lines.append(f"**Core function:** `{action.core_func}`")
        if action.entry_points:
            brief_lines.append(
                f"**Entry points:** {', '.join(f'`{e}`' for e in action.entry_points)}"
            )
        brief_lines.extend([
            f"**Variants to generate:** {action.n_variants}",
            "",
            "## Feature Request",
            "",
            action.feature_request.strip(),
        ])
        (workspace / "task_brief.md").write_text(
            "\n".join(brief_lines) + "\n", encoding="utf-8"
        )

        state.workspace = str(workspace)
        state.repo_path = str(repo_root)
        state.instance_id = action.instance_id
        state.dataset = action.dataset
        state.base_commit = action.base_commit
        state.n_variants = action.n_variants
        self._save(state)

        workspace_tree = _build_workspace_tree(workspace)
        text = (
            "Clarify workspace prepared.\n"
            f"workspace: {workspace}\n"
            f"klee scaffold: {klee_dir}\n\n"
            f"Workspace tree:\n{workspace_tree}\n\n"
            "Next steps:\n"
            "1. Read feature_request.md (or task_brief.md) for full task context.\n"
            "2. Write the shared KLEE harness: edit klee/core_abi.hpp with CoreInput/"
            "CoreOutput types, create klee/harness_0.cpp + klee/mock_0.cpp.\n"
            "3. Call clarify_claim_variant to create each isolated simulation directory.\n"
            "4. Write implementation variants and call clarify_klee_solve for each.\n"
            "5. Call clarify_cross_validation after all variants are solved.\n"
            "6. Call clarify_task_done with the final ambiguity analysis."
        )
        return _text_observation(
            text,
            command="clarify_workspace_generate",
            data={"workspace": str(workspace), "klee_dir": str(klee_dir), **metadata},
        )

    def claim_variant(self, action: ClarifyClaimVariantAction) -> ClarifyObservation:
        state = self._load()
        if not state.workspace:
            return _text_observation(
                "workspace is not set. Run clarify_workspace_generate first.",
                is_error=True,
                command="clarify_claim_variant",
            )
        workspace = Path(state.workspace)
        variant_id = state.next_variant_id
        state.next_variant_id += 1
        variant_dir = workspace / f"klee_{variant_id}"
        if variant_dir.exists():
            shutil.rmtree(variant_dir)
        shutil.copytree(workspace / "klee", variant_dir)
        state.variants[str(variant_id)] = str(variant_dir)
        self._save(state)
        return _text_observation(
            f"Claimed Clarify KLEE variant {variant_id}: {variant_dir}",
            command="clarify_claim_variant",
            data={
                "variant_id": variant_id,
                "variant_dir": str(variant_dir),
                "variant_name": action.variant_name,
            },
        )

    def klee_solve(self, action: ClarifyKleeSolveAction) -> ClarifyObservation:
        state = self._load()
        if not state.workspace:
            return _text_observation(
                "workspace is not set. Run clarify_workspace_generate first.",
                is_error=True,
                command="clarify_klee_solve",
            )
        cfg = get_klee_config()
        max_seconds = action.max_seconds or cfg.solve.max_seconds
        max_forks = action.max_forks or cfg.solve.max_forks
        timeout_seconds = (
            action.timeout_seconds or max_seconds + cfg.solve.timeout_buffer_sec
        )
        variant_id = action.variant_id or 1
        variant_dir = Path(
            state.variants.get(str(variant_id))
            or Path(state.workspace) / f"klee_{variant_id}"
        )
        if not variant_dir.is_dir():
            return _text_observation(
                f"variant directory not found: {variant_dir}",
                is_error=True,
                command="clarify_klee_solve",
                data={"variant_id": variant_id, "variant_dir": str(variant_dir)},
            )
        cpp_files = sorted(p.name for p in variant_dir.glob("*.cpp"))
        if not cpp_files:
            return _text_observation(
                f"No .cpp files found under {variant_dir}. Write harness/mock files first.",
                is_error=True,
                command="clarify_klee_solve",
                data={"variant_id": variant_id, "variant_dir": str(variant_dir)},
            )
        klee_sh = _discover_klee_sh()
        if klee_sh is None:
            return _text_observation(
                "Cannot locate klee.sh. Set KLEE_SH or OPENHANDS_CLARIFY_KLEE_SH.",
                is_error=True,
                command="clarify_klee_solve",
                data={"variant_id": variant_id, "variant_dir": str(variant_dir)},
            )
        out_dir = variant_dir / "out" / "klee-out"
        command = [
            str(klee_sh),
            "solve",
            str(variant_dir),
            str(out_dir),
            "--max-seconds",
            str(max_seconds),
            "--max-forks",
            str(max_forks),
        ]
        if cfg.solve.only_output_states_covering_new:
            command.append("--only-output-states-covering-new")
        for search in cfg.solve.search:
            command.extend(["--search", search])
        if cfg.solve.use_batching_search:
            command.append("--use-batching-search")
        command.extend(["--batch-instructions", str(cfg.solve.batch_instructions)])
        if cfg.solve.max_memory_mb > 0:
            command.extend(["--max-memory", str(cfg.solve.max_memory_mb)])
        proc = subprocess.run(
            command,
            cwd=str(variant_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        info_path = out_dir / "info"
        ktest_count = _count_ktests(out_dir)
        info_summary = _read_info_summary(info_path)
        if proc.returncode != 0 or not info_path.is_file():
            diag_files = _write_failure_diagnostics(
                variant_dir=variant_dir,
                command=command,
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
            )
            text = (
                "KLEE solve failed or did not produce klee-out/info.\n"
                f"returncode: {proc.returncode}\n"
                f"diagnostic_files: {diag_files}\n\n"
                "stdout tail:\n"
                f"{_truncate_tail(proc.stdout, cfg.runner.stderr_tail_bytes)}\n\n"
                "stderr tail:\n"
                f"{_truncate_tail(proc.stderr, cfg.runner.stderr_tail_bytes)}"
            )
            return _text_observation(
                text,
                is_error=True,
                command="clarify_klee_solve",
                data={
                    "variant_id": variant_id,
                    "variant_dir": str(variant_dir),
                    "out_dir": str(out_dir),
                    "returncode": proc.returncode,
                    "max_seconds": max_seconds,
                    "max_forks": max_forks,
                    "timeout_seconds": timeout_seconds,
                    "diagnostic_files": diag_files,
                    "truncated": True,
                    "next_actions": [
                        "Read the diagnostic files if the tail is insufficient.",
                        "Fix harness/mock compile errors before raising KLEE limits.",
                    ],
                },
            )
        text = (
            "KLEE solve completed.\n"
            f"variant_id: {variant_id}\n"
            f"out_dir: {out_dir}\n"
            f"ktest_count: {ktest_count}\n"
            f"info_summary: {json.dumps(info_summary, ensure_ascii=False)}"
        )
        return _text_observation(
            text,
            command="clarify_klee_solve",
            data={
                "variant_id": variant_id,
                "variant_dir": str(variant_dir),
                "out_dir": str(out_dir),
                "ktest_count": ktest_count,
                "info": info_summary,
            },
        )

    def cross_validation(
        self, action: ClarifyCrossValidationAction  # noqa: ARG002
    ) -> ClarifyObservation:
        state = self._load()
        if not state.workspace:
            return _text_observation(
                "workspace is not set. Run clarify_workspace_generate first.",
                is_error=True,
                command="clarify_cross_validation",
            )
        workspace = Path(state.workspace)
        cfg = get_klee_config()
        variants = _discover_solved_variants(workspace)
        if len(variants) < 2:
            return _text_observation(
                "Need at least two solved variants with test*.ktest files before "
                f"cross-validation. Found {len(variants)}.",
                is_error=True,
                command="clarify_cross_validation",
                data={
                    "workspace": str(workspace),
                    "solved_variant_count": len(variants),
                },
            )
        klee_sh = _discover_klee_sh()
        if klee_sh is None:
            return _text_observation(
                "Cannot locate klee.sh. Set KLEE_SH or OPENHANDS_CLARIFY_KLEE_SH.",
                is_error=True,
                command="clarify_cross_validation",
                data={"workspace": str(workspace)},
            )
        cross_val_dir = workspace / "cross_val"
        if cross_val_dir.exists():
            shutil.rmtree(cross_val_dir)
        cross_val_dir.mkdir(parents=True, exist_ok=True)
        merged_dir = cross_val_dir / "merged_klee_out"
        merged_names = _merge_variant_ktests(
            variants=variants,
            merged_dir=merged_dir,
            max_ktests_per_variant=cfg.cross_validation.max_ktests_per_variant,
        )
        replay_timeout = min(
            cfg.cross_validation.replay_timeout_max_sec,
            max(
                cfg.cross_validation.replay_timeout_min_sec,
                len(merged_names) * cfg.cross_validation.replay_sec_per_ktest,
            ),
        )

        replay_statuses: dict[int, dict[str, Any]] = {}
        for variant_id, variant_dir in variants:
            replay_dir = cross_val_dir / f"replay_v{variant_id}"
            command = [
                str(klee_sh),
                "replay",
                str(variant_dir),
                str(merged_dir),
                str(replay_dir),
                "--replay-parallel",
                str(cfg.cross_validation.replay_parallel),
            ]
            proc = subprocess.run(
                command,
                cwd=str(variant_dir),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=replay_timeout,
                check=False,
            )
            index_path = replay_dir / "index.json"
            ok = proc.returncode == 0 and index_path.is_file()
            diagnostics: list[str] = []
            if not ok:
                diagnostics = _write_replay_failure_diagnostics(
                    cross_val_dir=cross_val_dir,
                    variant_id=variant_id,
                    command=command,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    returncode=proc.returncode,
                )
            replay_statuses[variant_id] = {
                "ok": ok,
                "returncode": proc.returncode,
                "index_path": str(index_path),
                "diagnostic_files": diagnostics,
                "stdout_tail": _truncate_tail(proc.stdout, cfg.runner.stderr_tail_bytes),
                "stderr_tail": _truncate_tail(proc.stderr, cfg.runner.stderr_tail_bytes),
            }

        matrix: dict[str, dict[str, str]] = {}
        signatures: dict[str, dict[str, str]] = {}
        for ktest_name in merged_names:
            matrix[ktest_name] = {}
            signatures[ktest_name] = {}
            for variant_id, _variant_dir in variants:
                replay_dir = cross_val_dir / f"replay_v{variant_id}"
                index = _parse_replay_index(replay_dir / "index.json")
                items = index.get("items") if isinstance(index.get("items"), list) else []
                status_by_ktest = {
                    str(item.get("ktest")): str(item.get("status") or "failed")
                    for item in items
                    if isinstance(item, dict) and item.get("ktest")
                }
                status = status_by_ktest.get(ktest_name, "missing")
                matrix[ktest_name][str(variant_id)] = status
                replay_json = _load_replay_json(replay_dir / f"{ktest_name}.json")
                signatures[ktest_name][str(variant_id)] = _behavior_signature(replay_json)

        divergent: list[dict[str, Any]] = []
        for ktest_name, by_variant in signatures.items():
            unique = set(by_variant.values())
            if len(unique) > 1:
                divergent.append(
                    {
                        "ktest": ktest_name,
                        "statuses": matrix.get(ktest_name, {}),
                        "signatures": by_variant,
                    }
                )

        clusters = _cluster_divergent_ktests(divergent)
        cluster_summaries = _write_cluster_markdown_files(
            cross_val_dir=cross_val_dir,
            clusters=clusters,
        )
        matrix_path = cross_val_dir / "matrix.json"
        matrix_path.write_text(
            json.dumps(
                {
                    "variants": [
                        {"variant_id": variant_id, "path": str(path)}
                        for variant_id, path in variants
                    ],
                    "merged_ktests": merged_names,
                    "status_matrix": matrix,
                    "signature_matrix": signatures,
                    "divergent": divergent,
                    "replay_statuses": replay_statuses,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        summary_path = cross_val_dir / "cluster_summary.json"
        summary = {
            "workspace": str(workspace),
            "variant_count": len(variants),
            "merged_ktest_count": len(merged_names),
            "divergent_ktest_count": len(divergent),
            "cluster_count": len(clusters),
            "variants": [
                {
                    "variant_id": variant_id,
                    "path": str(path),
                    "source_ktest_count": _count_ktests(path / "out" / "klee-out"),
                    "replay": replay_statuses.get(variant_id, {}),
                }
                for variant_id, path in variants
            ],
            "matrix_path": str(matrix_path),
            "clusters": cluster_summaries,
            "divergent": divergent[:50],
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        failed_replays = [
            variant_id
            for variant_id, status in replay_statuses.items()
            if not status.get("ok")
        ]
        return _text_observation(
            "Cross-validation replay matrix written. "
            f"variants={len(variants)} merged_ktests={len(merged_names)} "
            f"divergent={len(divergent)} summary={summary_path}",
            is_error=bool(failed_replays),
            command="clarify_cross_validation",
            data=summary,
        )

    def task_done(self, action: ClarifyTaskDoneAction) -> ClarifyObservation:
        from openhands.clarify.artifact_schema import (
            ClusterEntry,
            ClarifyReportPayload,
            ToolRunEvent,
            write_report_html,
            write_report_json,
            write_disambiguated_request,
        )

        state = self._load()
        state.task_done_status = action.status
        state.task_done_summary = action.summary
        state.task_done_report = action.report
        self._save(state)

        report_path = None
        html_path = None
        disambig_path = None
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if state.workspace:
            workspace = Path(state.workspace)

            # Build cluster list from cross-validation output if available.
            clusters: list[ClusterEntry] = []
            cluster_summary_path = workspace / "cross_val" / "cluster_summary.json"
            if cluster_summary_path.is_file():
                try:
                    cs = json.loads(cluster_summary_path.read_text(encoding="utf-8"))
                    for c in cs.get("clusters", []):
                        clusters.append(
                            ClusterEntry(
                                cluster_id=c.get("cluster_id", 0),
                                size=c.get("size", 0),
                                ktests=c.get("ktests", [])[:50],
                                path=str(c.get("path", "")),
                                signatures_by_variant=c.get("signatures_by_variant", {}),
                            )
                        )
                except Exception:  # noqa: BLE001
                    pass

            # Read trace events already written by prior tool calls.
            tool_runs: list[ToolRunEvent] = []
            trace_path = workspace / "trace.jsonl"
            if trace_path.is_file():
                for line in trace_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        tool_runs.append(
                            ToolRunEvent(
                                tool=ev.get("tool", ""),
                                timestamp=ev.get("timestamp", ""),
                                elapsed_ms=float(ev.get("elapsed_ms", 0)),
                                is_error=bool(ev.get("is_error", False)),
                                returncode=ev.get("returncode"),
                                data={
                                    k: v
                                    for k, v in ev.get("data", {}).items()
                                    if k in ("variant_id", "variant_dir", "ktest_count", "workspace")
                                },
                            )
                        )
                    except Exception:  # noqa: BLE001
                        pass

            # Collect metadata.
            metadata: dict[str, Any] = {}
            metadata_path = workspace / "metadata.json"
            if metadata_path.is_file():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    pass

            payload = ClarifyReportPayload(
                instance_id=state.instance_id or metadata.get("instance_id", ""),
                dataset=state.dataset or metadata.get("dataset", "featurebench"),
                mode="hybrid",
                status=action.status,
                summary=action.summary,
                report=action.report,
                clusters=clusters,
                tool_runs=tool_runs,
                workspace=str(workspace),
                finished_at=now_iso,
                state={
                    k: v
                    for k, v in (state.__dict__ if hasattr(state, "__dict__") else {}).items()
                    if not k.startswith("_") and k != "state_path"
                },
            )

            report_path = write_report_json(payload, workspace / "clarify_report.json")
            html_path = write_report_html(payload, workspace / "clarify_report.html")
            disambig_path = write_disambiguated_request(
                payload, workspace / "disambiguated_request.md"
            )

        return _text_observation(
            f"Clarify task marked {action.status}: {action.summary}",
            is_error=action.status != "complete",
            command="clarify_task_done",
            data={
                "status": action.status,
                "summary": action.summary,
                "report_path": str(report_path) if report_path else None,
                "html_path": str(html_path) if html_path else None,
                "disambiguated_request_path": str(disambig_path) if disambig_path else None,
            },
        )


class _ClarifyBaseTool(ToolDefinition[Action, ClarifyObservation]):
    @classmethod
    def _create_with_action(
        cls,
        conv_state: "ConversationState",
        action_type: type[Action],
        description: str,
        *,
        read_only: bool = False,
        destructive: bool = False,
    ) -> Sequence["_ClarifyBaseTool"]:
        executor = ClarifyExecutor(
            working_dir=conv_state.workspace.working_dir,
            persistence_dir=conv_state.persistence_dir,
        )
        return [
            cls(
                action_type=action_type,
                observation_type=ClarifyObservation,
                description=description,
                annotations=ToolAnnotations(
                    title=cls.name,
                    readOnlyHint=read_only,
                    destructiveHint=destructive,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]


class ClarifyWorkspaceGenerateTool(_ClarifyBaseTool):
    name = "clarify_workspace_generate"

    @classmethod
    def create(cls, conv_state: "ConversationState") -> Sequence["ClarifyWorkspaceGenerateTool"]:
        return cls._create_with_action(
            conv_state,
            ClarifyWorkspaceGenerateAction,
            "Create a standalone Clarify workspace and KLEE scaffold for this task.",
            destructive=False,
        )


class ClarifyClaimVariantTool(_ClarifyBaseTool):
    name = "clarify_claim_variant"

    @classmethod
    def create(cls, conv_state: "ConversationState") -> Sequence["ClarifyClaimVariantTool"]:
        return cls._create_with_action(
            conv_state,
            ClarifyClaimVariantAction,
            "Create an isolated KLEE variant directory copied from the scaffold.",
            destructive=False,
        )


class ClarifyKleeSolveTool(_ClarifyBaseTool):
    name = "clarify_klee_solve"

    def declared_resources(self, action: Action) -> DeclaredResources:
        return DeclaredResources(keys=("clarify:klee",), declared=True)

    @classmethod
    def create(cls, conv_state: "ConversationState") -> Sequence["ClarifyKleeSolveTool"]:
        return cls._create_with_action(
            conv_state,
            ClarifyKleeSolveAction,
            "Run KLEE solve for a claimed Clarify variant and persist long failures.",
            destructive=True,
        )


class ClarifyCrossValidationTool(_ClarifyBaseTool):
    name = "clarify_cross_validation"

    @classmethod
    def create(cls, conv_state: "ConversationState") -> Sequence["ClarifyCrossValidationTool"]:
        return cls._create_with_action(
            conv_state,
            ClarifyCrossValidationAction,
            "Summarize KLEE outputs across Clarify variants.",
            destructive=False,
        )


class ClarifyTaskDoneTool(_ClarifyBaseTool):
    name = "clarify_task_done"

    @classmethod
    def create(cls, conv_state: "ConversationState") -> Sequence["ClarifyTaskDoneTool"]:
        return cls._create_with_action(
            conv_state,
            ClarifyTaskDoneAction,
            "Finish the Clarify run with status, summary, and optional full report.",
            destructive=False,
        )


for _tool_cls in (
    ClarifyWorkspaceGenerateTool,
    ClarifyClaimVariantTool,
    ClarifyKleeSolveTool,
    ClarifyCrossValidationTool,
    ClarifyTaskDoneTool,
):
    register_tool(_tool_cls.name, _tool_cls)
