from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from openhands.clarify.tools import CLARIFY_TOOL_NAMES, CLARIFY_AGENT_NAMES, get_clarify_tool_specs
from openhands.clarify.klee.config import get_klee_config, reset_klee_config
import openhands.clarify.tools.definitions as clarify_definitions
from openhands.clarify.artifact_schema import (
    ClarifyReportPayload,
    ClusterEntry,
    dump_report_payload,
    report_is_complete,
    write_report_html,
    write_report_json,
    write_disambiguated_request,
)
from openhands.clarify.tools.definitions import (
    ClarifyClaimVariantAction,
    ClarifyClaimVariantTool,
    ClarifyCrossValidationAction,
    ClarifyCrossValidationTool,
    ClarifyKleeSolveAction,
    ClarifyKleeSolveTool,
    ClarifyPrepareKleeAction,
    ClarifyPrepareKleeTool,
    ClarifyTaskDoneAction,
    ClarifyTaskDoneTool,
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


def test_clarify_agents_are_registered():
    """register_clarify_agents() registers all three sub-agent definitions."""
    from openhands.clarify.tools import register_clarify_agents
    from openhands.sdk.subagent import get_registered_agent_definitions

    register_clarify_agents()

    registered_names = {defn.name for defn in get_registered_agent_definitions()}
    for agent_name in CLARIFY_AGENT_NAMES:
        assert agent_name in registered_names, f"{agent_name!r} not registered"


def test_clarify_subagent_factories_include_requested_tools(monkeypatch):
    """Clarify sub-agent factories must materialize their declared tools."""
    from openhands.clarify.tools import register_clarify_all
    from openhands.sdk.llm import LLM
    from openhands.sdk.subagent.registry import get_agent_factory
    from openhands.tools.preset.default import register_default_tools

    register_default_tools(enable_browser=False)
    register_clarify_all()
    monkeypatch.setenv("ALLOW_SHORT_CONTEXT_WINDOWS", "1")

    llm = LLM(model="gpt-4", api_key="test-key")
    harness_agent = get_agent_factory("clarify_harness_writer").factory_func(llm)
    simulation_agent = get_agent_factory("clarify_simulation_writer").factory_func(llm)

    assert {tool.name for tool in harness_agent.tools} >= {
        "terminal",
        "file_editor",
    }
    assert {tool.name for tool in simulation_agent.tools} >= {
        "terminal",
        "file_editor",
        "clarify_claim_variant",
        "clarify_klee_solve",
    }


def test_prepare_klee_and_claim_variant(tmp_path):
    conv_state = _conv_state(tmp_path)
    prepare_tool = _tool(ClarifyPrepareKleeTool, conv_state)
    claim_tool = _tool(ClarifyClaimVariantTool, conv_state)

    prepare_obs = prepare_tool(
        ClarifyPrepareKleeAction(task_id="case-1", metadata={"source": "unit"})
    )
    scaffold = tmp_path / "repo" / ".openhands" / "clarify" / "klee"
    (scaffold / "harness_0.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    claim_obs = claim_tool(ClarifyClaimVariantAction(variant_name="strict"))

    assert prepare_obs.is_error is False
    assert (scaffold / "klee_helpers.hpp").is_file()
    assert (scaffold / "KLEE_IMPLEMENTATION_RULES.md").is_file()
    assert claim_obs.data["variant_id"] == 1
    assert (tmp_path / "repo" / ".openhands" / "clarify" / "klee_1").is_dir()


def test_klee_solve_reports_missing_klee_sh(tmp_path, monkeypatch):
    conv_state = _conv_state(tmp_path)
    _tool(ClarifyPrepareKleeTool, conv_state)(ClarifyPrepareKleeAction())
    scaffold = tmp_path / "repo" / ".openhands" / "clarify" / "klee"
    (scaffold / "harness_0.cpp").write_text(
        "int main(){return 0;}\n",
        encoding="utf-8",
    )
    claim_obs = _tool(ClarifyClaimVariantTool, conv_state)(
        ClarifyClaimVariantAction()
    )
    variant_dir = claim_obs.data["variant_dir"]
    monkeypatch.delenv("KLEE_SH", raising=False)
    monkeypatch.delenv("OPENHANDS_CLARIFY_KLEE_SH", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(clarify_definitions, "_discover_klee_sh", lambda: None)

    obs = _tool(ClarifyKleeSolveTool, conv_state)(
        ClarifyKleeSolveAction(variant_id=claim_obs.data["variant_id"])
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

    obs = _tool(ClarifyTaskDoneTool, conv_state)(
        ClarifyTaskDoneAction(
            status="complete",
            summary="Done",
            report="status: complete\n\n1. Ambiguity",
        )
    )

    workspace = tmp_path / "repo"
    report_path = workspace / "clarify_report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert obs.is_error is False
    assert payload["status"] == "complete"
    assert payload["report"].startswith("status: complete")
    # New: HTML and disambiguated request should also be written
    assert (workspace / "clarify_report.html").is_file()
    assert (workspace / "disambiguated_request.md").is_file()
    # obs data should include html_path
    assert obs.data.get("html_path") is not None


def test_task_done_writes_trace_events(tmp_path):
    """Each tool call appends a trace event to trace.jsonl."""
    conv_state = _conv_state(tmp_path)
    _tool(ClarifyPrepareKleeTool, conv_state)(ClarifyPrepareKleeAction())
    scaffold = tmp_path / "repo" / ".openhands" / "clarify" / "klee"
    _tool(ClarifyClaimVariantTool, conv_state)(
        ClarifyClaimVariantAction()
    )
    _tool(ClarifyTaskDoneTool, conv_state)(
        ClarifyTaskDoneAction(status="complete", summary="Done")
    )

    workspace = tmp_path / "repo"
    trace_path = workspace / "trace.jsonl"
    assert trace_path.is_file()
    events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    # claim_variant, task_done → at least 2 events
    assert len(events) >= 2
    tools_seen = {e["tool"] for e in events}
    assert "clarify_claim_variant" in tools_seen
    assert "clarify_task_done" in tools_seen
    # Every event should have a timestamp and elapsed_ms
    for ev in events:
        assert "timestamp" in ev
        assert isinstance(ev["elapsed_ms"], float | int)


def test_cross_validation_builds_replay_matrix(tmp_path, monkeypatch):
    conv_state = _conv_state(tmp_path)
    workspace = tmp_path / "repo"
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
        ClarifyCrossValidationAction(
            variant_dirs=[
                str(workspace / "klee_1"),
                str(workspace / "klee_2"),
            ]
        )
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


# ---------------------------------------------------------------------------
# artifact_schema tests
# ---------------------------------------------------------------------------


def test_artifact_schema_round_trip(tmp_path):
    """dump + write_report_json preserves all fields; HTML and md files are created."""
    payload = ClarifyReportPayload(
        task_id="owner__repo.abc.feat.deadbeef.lv1",
        status="complete",
        summary="Found 2 ambiguities.",
        metadata={"source": "featurebench"},
        report="## Ambiguities\n\n1. Strict vs fuzzy\n2. Error handling",
        clusters=[ClusterEntry(cluster_id=1, size=3, ktests=["t1", "t2", "t3"])],
    )
    json_path = tmp_path / "clarify_report.json"
    html_path = tmp_path / "clarify_report.html"
    md_path = tmp_path / "disambiguated_request.md"

    write_report_json(payload, json_path)
    write_report_html(payload, html_path)
    write_disambiguated_request(payload, md_path)

    assert json_path.is_file()
    assert html_path.is_file()
    assert md_path.is_file()

    data = json.loads(json_path.read_text())
    assert data["task_id"] == "owner__repo.abc.feat.deadbeef.lv1"
    assert data["status"] == "complete"
    assert data["clusters"][0]["cluster_id"] == 1
    assert report_is_complete(json_path)

    html = html_path.read_text()
    assert "Clarify Final Report" in html
    assert "complete" in html

    md = md_path.read_text()
    assert "## Ambiguities" in md


def test_artifact_schema_report_is_complete_false(tmp_path):
    json_path = tmp_path / "report.json"
    json_path.write_text(json.dumps({"status": "incomplete"}))
    assert report_is_complete(json_path) is False


def test_artifact_schema_report_is_complete_missing(tmp_path):
    assert report_is_complete(tmp_path / "nonexistent.json") is False


def test_disambiguated_request_uses_ambiguity_section(tmp_path):
    """If report has an '## Ambiguit' section it is embedded verbatim."""
    payload = ClarifyReportPayload(
        task_id="case-1",
        status="complete",
        summary="Done",
        report="Some intro\n\n## Ambiguities\n\n1. Foo\n2. Bar",
    )
    md_path = tmp_path / "dr.md"
    write_disambiguated_request(payload, md_path)
    text = md_path.read_text()
    assert "## Ambiguities" in text
    assert "1. Foo" in text
