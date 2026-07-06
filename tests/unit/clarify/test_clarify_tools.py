from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from openhands.clarify.tools import CLARIFY_TOOL_NAMES, get_clarify_tool_specs
from openhands.clarify.klee.config import get_klee_config, reset_klee_config
import openhands.clarify.tools.definitions as clarify_definitions
from openhands.clarify.tools.definitions import (
    ClarifyClaimVariantAction,
    ClarifyClaimVariantTool,
    ClarifyCrossValidationAction,
    ClarifyCrossValidationTool,
    ClarifyKleeSolveAction,
    ClarifyKleeSolveTool,
    ClarifyTaskDoneAction,
    ClarifyTaskDoneTool,
    ClarifyWorkspaceGenerateAction,
    ClarifyWorkspaceGenerateTool,
    _discover_klee_sh,
)
from openhands.sdk.tool.registry import list_registered_tools


def _conv_state(tmp_path):
    workspace = tmp_path / "repo"
    persistence = tmp_path / "persistence"
    workspace.mkdir()
    persistence.mkdir()
    return SimpleNamespace(
        workspace=SimpleNamespace(working_dir=str(workspace)),
        persistence_dir=str(persistence),
    )


def _tool(tool_cls, conv_state):
    return tool_cls.create(conv_state)[0]


def test_clarify_tools_are_registered():
    specs = get_clarify_tool_specs()

    assert [spec.name for spec in specs] == list(CLARIFY_TOOL_NAMES)
    registered = set(list_registered_tools())
    assert set(CLARIFY_TOOL_NAMES).issubset(registered)


def test_workspace_generate_and_claim_variant(tmp_path):
    conv_state = _conv_state(tmp_path)
    workspace_tool = _tool(ClarifyWorkspaceGenerateTool, conv_state)
    claim_tool = _tool(ClarifyClaimVariantTool, conv_state)

    workspace_obs = workspace_tool(
        ClarifyWorkspaceGenerateAction(
            instance_id="owner__repo.abc.test_feature.deadbeef.lv1",
            dataset="featurebench",
            base_commit="abc",
            feature_request="Add a fuzzy matching mode.",
        )
    )
    claim_obs = claim_tool(ClarifyClaimVariantAction(variant_name="strict"))

    workspace = tmp_path / "repo" / ".openhands" / "clarify"
    assert workspace_obs.is_error is False
    assert (workspace / "owner__repo.abc.test_feature.deadbeef.lv1").is_dir()
    assert claim_obs.data["variant_id"] == 1
    assert (workspace / "owner__repo.abc.test_feature.deadbeef.lv1" / "klee_1").is_dir()


def test_klee_solve_reports_missing_klee_sh(tmp_path, monkeypatch):
    conv_state = _conv_state(tmp_path)
    _tool(ClarifyWorkspaceGenerateTool, conv_state)(
        ClarifyWorkspaceGenerateAction(
            instance_id="case-1",
            feature_request="Request",
        )
    )
    claim_obs = _tool(ClarifyClaimVariantTool, conv_state)(
        ClarifyClaimVariantAction()
    )
    variant_dir = claim_obs.data["variant_dir"]
    (tmp_path / "repo" / ".openhands" / "clarify" / "case-1" / "klee_1" / "harness_0.cpp").write_text(
        "int main(){return 0;}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KLEE_SH", raising=False)
    monkeypatch.delenv("OPENHANDS_CLARIFY_KLEE_SH", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(clarify_definitions, "_discover_klee_sh", lambda: None)

    obs = _tool(ClarifyKleeSolveTool, conv_state)(
        ClarifyKleeSolveAction(variant_id=1)
    )

    assert obs.is_error is True
    assert "Cannot locate klee.sh" in obs.text
    assert obs.data["variant_dir"] == variant_dir


def test_vendored_klee_sh_is_discoverable(monkeypatch):
    monkeypatch.delenv("KLEE_SH", raising=False)
    monkeypatch.delenv("OPENHANDS_CLARIFY_KLEE_SH", raising=False)
    monkeypatch.setenv("PATH", "")

    klee_sh = _discover_klee_sh()

    assert klee_sh is not None
    assert klee_sh.name == "klee.sh"
    assert "openhands/clarify/klee/runners/klee_sh" in str(klee_sh)


def test_klee_config_loads_vendored_defaults():
    reset_klee_config()

    cfg = get_klee_config()

    assert cfg.solve.max_seconds == 180
    assert cfg.solve.search == ["dfs", "nurs:covnew"]
    assert cfg.cross_validation.replay_parallel == 4
    assert cfg.cross_validation.max_ktests_per_variant == 300


def test_task_done_writes_report(tmp_path):
    conv_state = _conv_state(tmp_path)
    _tool(ClarifyWorkspaceGenerateTool, conv_state)(
        ClarifyWorkspaceGenerateAction(
            instance_id="case-1",
            feature_request="Request",
        )
    )

    obs = _tool(ClarifyTaskDoneTool, conv_state)(
        ClarifyTaskDoneAction(
            status="complete",
            summary="Done",
            report="status: complete\n\n1. Ambiguity",
        )
    )

    report_path = tmp_path / "repo" / ".openhands" / "clarify" / "case-1" / "clarify_report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert obs.is_error is False
    assert payload["status"] == "complete"
    assert payload["report"].startswith("status: complete")


def test_cross_validation_builds_replay_matrix(tmp_path, monkeypatch):
    conv_state = _conv_state(tmp_path)
    _tool(ClarifyWorkspaceGenerateTool, conv_state)(
        ClarifyWorkspaceGenerateAction(
            instance_id="case-1",
            feature_request="Request",
        )
    )
    workspace = tmp_path / "repo" / ".openhands" / "clarify" / "case-1"
    for variant_id in (1, 2):
        variant = workspace / f"klee_{variant_id}"
        klee_out = variant / "out" / "klee-out"
        klee_out.mkdir(parents=True)
        (variant / "harness_0.cpp").write_text(
            "int main(){return 0;}\n",
            encoding="utf-8",
        )
        (klee_out / "test000001.ktest").write_text(
            f"ktest-{variant_id}",
            encoding="utf-8",
        )

    def fake_run(command, **kwargs):
        del kwargs
        assert "--replay-parallel" in command
        assert command[command.index("--replay-parallel") + 1] == "4"
        variant_id = int(str(command[2]).rsplit("klee_", 1)[1])
        merged_dir = workspace / "cross_val" / "merged_klee_out"
        replay_dir = workspace / "cross_val" / f"replay_v{variant_id}"
        replay_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for ktest in sorted(merged_dir.glob("*.ktest")):
            name = ktest.stem
            items.append({"ktest": name, "status": "ok", "exit": 0})
            (replay_dir / f"{name}.json").write_text(
                json.dumps(
                    {
                        "ktest": name,
                        "variant_behavior": f"v{variant_id}",
                    }
                ),
                encoding="utf-8",
            )
        (replay_dir / "index.json").write_text(
            json.dumps(
                {
                    "total": len(items),
                    "ok": len(items),
                    "failed": 0,
                    "items": items,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(clarify_definitions.subprocess, "run", fake_run)

    obs = _tool(ClarifyCrossValidationTool, conv_state)(
        ClarifyCrossValidationAction()
    )

    summary = json.loads(
        (workspace / "cross_val" / "cluster_summary.json").read_text()
    )
    matrix = json.loads((workspace / "cross_val" / "matrix.json").read_text())
    cluster_report = workspace / "cross_val" / "cluster_001.md"
    assert obs.is_error is False
    assert summary["variant_count"] == 2
    assert summary["merged_ktest_count"] == 2
    assert summary["divergent_ktest_count"] == 2
    assert summary["cluster_count"] == 1
    assert summary["clusters"][0]["size"] == 2
    assert summary["clusters"][0]["path"] == str(cluster_report)
    assert cluster_report.is_file()
    assert "Clarify Cross-Validation Cluster 1" in cluster_report.read_text()
    assert set(matrix["status_matrix"]) == {
        "test_v1_test000001",
        "test_v2_test000001",
    }
