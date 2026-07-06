from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_KLEE_CONFIG_PATH = Path(__file__).resolve().parent / "common" / "klee_config.yaml"


@dataclass(frozen=True)
class SolveConfig:
    max_seconds: int = 180
    max_forks: int = 50000
    only_output_states_covering_new: bool = False
    timeout_buffer_sec: int = 180
    search: list[str] = field(default_factory=lambda: ["dfs", "nurs:covnew"])
    use_batching_search: bool = True
    batch_instructions: int = 1000
    max_memory_mb: int = 16000


@dataclass(frozen=True)
class CrossValidationConfig:
    replay_parallel: int = 4
    replay_timeout_min_sec: int = 300
    replay_sec_per_ktest: int = 2
    replay_timeout_max_sec: int = 3600
    max_ktests_per_variant: int = 300


@dataclass(frozen=True)
class RunnerConfig:
    stderr_tail_bytes: int = 4000


@dataclass(frozen=True)
class KleeConfig:
    solve: SolveConfig = field(default_factory=SolveConfig)
    cross_validation: CrossValidationConfig = field(
        default_factory=CrossValidationConfig
    )
    runner: RunnerConfig = field(default_factory=RunnerConfig)


def _require_mapping(value: Any, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"KLEE config section {name!r} must be a mapping.")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], *, section: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(
            f"Unknown KLEE config key(s) in {section}: {sorted(unknown)}"
        )


def _positive_int(value: Any, *, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"KLEE config {name} must be a positive integer.")
    return value


def _non_negative_int(value: Any, *, name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"KLEE config {name} must be a non-negative integer.")
    return value


def _bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"KLEE config {name} must be a boolean.")
    return value


def _string_list(value: Any, *, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"KLEE config {name} must be a list of strings.")
    return value


def _load_solve(data: dict[str, Any]) -> SolveConfig:
    defaults = SolveConfig()
    allowed = {
        "max_seconds",
        "max_forks",
        "only-output-states-covering-new",
        "timeout_buffer_sec",
        "search",
        "use-batching-search",
        "batch-instructions",
        "max-memory-mb",
    }
    _reject_unknown(data, allowed, section="solve")
    return SolveConfig(
        max_seconds=_positive_int(
            data.get("max_seconds", defaults.max_seconds),
            name="solve.max_seconds",
        ),
        max_forks=_positive_int(
            data.get("max_forks", defaults.max_forks),
            name="solve.max_forks",
        ),
        only_output_states_covering_new=_bool(
            data.get(
                "only-output-states-covering-new",
                defaults.only_output_states_covering_new,
            ),
            name="solve.only-output-states-covering-new",
        ),
        timeout_buffer_sec=_non_negative_int(
            data.get("timeout_buffer_sec", defaults.timeout_buffer_sec),
            name="solve.timeout_buffer_sec",
        ),
        search=_string_list(data.get("search", defaults.search), name="solve.search"),
        use_batching_search=_bool(
            data.get("use-batching-search", defaults.use_batching_search),
            name="solve.use-batching-search",
        ),
        batch_instructions=_positive_int(
            data.get("batch-instructions", defaults.batch_instructions),
            name="solve.batch-instructions",
        ),
        max_memory_mb=_non_negative_int(
            data.get("max-memory-mb", defaults.max_memory_mb),
            name="solve.max-memory-mb",
        ),
    )


def _load_cross_validation(data: dict[str, Any]) -> CrossValidationConfig:
    defaults = CrossValidationConfig()
    allowed = {
        "replay_parallel",
        "replay_timeout_min_sec",
        "replay_sec_per_ktest",
        "replay_timeout_max_sec",
        "max_ktests_per_variant",
    }
    _reject_unknown(data, allowed, section="cross_validation")
    return CrossValidationConfig(
        replay_parallel=_positive_int(
            data.get("replay_parallel", defaults.replay_parallel),
            name="cross_validation.replay_parallel",
        ),
        replay_timeout_min_sec=_positive_int(
            data.get("replay_timeout_min_sec", defaults.replay_timeout_min_sec),
            name="cross_validation.replay_timeout_min_sec",
        ),
        replay_sec_per_ktest=_positive_int(
            data.get("replay_sec_per_ktest", defaults.replay_sec_per_ktest),
            name="cross_validation.replay_sec_per_ktest",
        ),
        replay_timeout_max_sec=_positive_int(
            data.get("replay_timeout_max_sec", defaults.replay_timeout_max_sec),
            name="cross_validation.replay_timeout_max_sec",
        ),
        max_ktests_per_variant=_positive_int(
            data.get("max_ktests_per_variant", defaults.max_ktests_per_variant),
            name="cross_validation.max_ktests_per_variant",
        ),
    )


def _load_runner(data: dict[str, Any]) -> RunnerConfig:
    defaults = RunnerConfig()
    allowed = {"stderr_tail_bytes"}
    _reject_unknown(data, allowed, section="runner")
    return RunnerConfig(
        stderr_tail_bytes=_positive_int(
            data.get("stderr_tail_bytes", defaults.stderr_tail_bytes),
            name="runner.stderr_tail_bytes",
        )
    )


def load_klee_config(path: Path | None = None) -> KleeConfig:
    config_path = Path(
        os.environ.get("OPENHANDS_CLARIFY_KLEE_CONFIG")
        or path
        or DEFAULT_KLEE_CONFIG_PATH
    )
    if not config_path.is_file():
        return KleeConfig()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = _require_mapping(data, name="root")
    allowed = {"solve", "cross_validation", "runner"}
    _reject_unknown(root, allowed, section="root")
    return KleeConfig(
        solve=_load_solve(_require_mapping(root.get("solve"), name="solve")),
        cross_validation=_load_cross_validation(
            _require_mapping(root.get("cross_validation"), name="cross_validation")
        ),
        runner=_load_runner(_require_mapping(root.get("runner"), name="runner")),
    )


_KLEE_CONFIG: KleeConfig | None = None


def get_klee_config() -> KleeConfig:
    global _KLEE_CONFIG
    if _KLEE_CONFIG is None:
        _KLEE_CONFIG = load_klee_config()
    return _KLEE_CONFIG


def reset_klee_config() -> None:
    global _KLEE_CONFIG
    _KLEE_CONFIG = None

