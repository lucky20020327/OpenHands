"""Unified artifact schema for OpenHands Clarify reports.

All clarify runs should produce a ``clarify_report.json`` that conforms to
``ClarifyReportPayload``.  The schema is intentionally kept as plain
dataclasses / TypedDicts so it can be serialised to JSON without any
framework dependency.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AmbiguityEntry",
    "ClusterEntry",
    "SubagentEvent",
    "ToolRunEvent",
    "ClarifyReportPayload",
    "dump_report_payload",
    "load_report_payload",
    "report_is_complete",
    "write_report_json",
    "write_report_html",
    "write_disambiguated_request",
]


# ---------------------------------------------------------------------------
# Sub-structures
# ---------------------------------------------------------------------------


@dataclass
class AmbiguityEntry:
    """A single ambiguity detected by the clarify agent."""

    id: int
    title: str = ""
    description: str = ""
    classification: str = ""
    variant_ids: list[int] = field(default_factory=list)
    evidence: str = ""


@dataclass
class ClusterEntry:
    """A cross-validation behavior cluster."""

    cluster_id: int
    size: int = 0
    ktests: list[str] = field(default_factory=list)
    path: str = ""
    signatures_by_variant: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubagentEvent:
    """Summary of a sub-agent invocation."""

    name: str
    status: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: float | None = None
    summary: str = ""
    variant_id: int | None = None


@dataclass
class ToolRunEvent:
    """Record of a single clarify tool execution (trace entry)."""

    tool: str
    timestamp: str
    elapsed_ms: float
    is_error: bool = False
    returncode: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level report payload
# ---------------------------------------------------------------------------


@dataclass
class ClarifyReportPayload:
    """Full clarify report written as ``clarify_report.json``."""

    task_id: str
    status: str
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)
    mode: str = "hybrid"
    report: str = ""
    ambiguities: list[AmbiguityEntry] = field(default_factory=list)
    clusters: list[ClusterEntry] = field(default_factory=list)
    subagents: list[SubagentEvent] = field(default_factory=list)
    tool_runs: list[ToolRunEvent] = field(default_factory=list)
    workspace: str | None = None
    cost: float | None = None
    tokens: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    # Raw state dict captured at task_done time; kept for debugging.
    state: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _strip_none(obj: Any) -> Any:
    """Recursively remove None-valued keys from nested dicts/lists."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(item) for item in obj]
    return obj


def dump_report_payload(payload: ClarifyReportPayload) -> dict[str, Any]:
    return _strip_none(asdict(payload))


def load_report_payload(data: dict[str, Any]) -> ClarifyReportPayload:
    """Best-effort deserialisation; unknown fields are silently ignored."""
    ambiguities = [
        AmbiguityEntry(**{k: v for k, v in item.items() if k in AmbiguityEntry.__dataclass_fields__})
        for item in data.get("ambiguities", [])
        if isinstance(item, dict)
    ]
    clusters = [
        ClusterEntry(**{k: v for k, v in item.items() if k in ClusterEntry.__dataclass_fields__})
        for item in data.get("clusters", [])
        if isinstance(item, dict)
    ]
    subagents = [
        SubagentEvent(**{k: v for k, v in item.items() if k in SubagentEvent.__dataclass_fields__})
        for item in data.get("subagents", [])
        if isinstance(item, dict)
    ]
    tool_runs = [
        ToolRunEvent(**{k: v for k, v in item.items() if k in ToolRunEvent.__dataclass_fields__})
        for item in data.get("tool_runs", [])
        if isinstance(item, dict)
    ]
    known = {f for f in ClarifyReportPayload.__dataclass_fields__}
    extra = {k: v for k, v in data.items() if k in known and k not in
             ("ambiguities", "clusters", "subagents", "tool_runs")}
    return ClarifyReportPayload(
        ambiguities=ambiguities,
        clusters=clusters,
        subagents=subagents,
        tool_runs=tool_runs,
        **extra,
    )


def report_is_complete(path: Path | str) -> bool:
    """Return True iff the JSON report at *path* has status=complete."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return str(data.get("status", "")).lower() == "complete"
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_report_json(payload: ClarifyReportPayload, path: Path | str) -> Path:
    """Write *payload* as indented JSON to *path*."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(dump_report_payload(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest


def write_report_html(payload: ClarifyReportPayload, path: Path | str) -> Path:
    """Write a human-readable HTML report to *path*."""
    from html import escape

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    status_color = "#16a34a" if payload.status == "complete" else "#dc2626"
    status_badge = (
        f'<span style="background:{status_color};color:#fff;padding:2px 10px;'
        f'border-radius:4px;font-size:0.9em;">{escape(payload.status)}</span>'
    )
    meta_rows = "\n".join(
        f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>"
        for k, v in [
            ("task_id", payload.task_id),
            ("mode", payload.mode),
            ("status", payload.status),
            ("workspace", payload.workspace or ""),
            ("started_at", payload.started_at or ""),
            ("finished_at", payload.finished_at or ""),
        ]
    )
    cluster_rows = ""
    if payload.clusters:
        rows = "\n".join(
            f"<tr><td>{c.cluster_id}</td><td>{c.size}</td>"
            f"<td>{escape(c.path)}</td></tr>"
            for c in payload.clusters
        )
        cluster_rows = (
            "<h2>Divergence Clusters</h2>"
            "<table><thead><tr><th>#</th><th>ktests</th><th>report path</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Clarify Report - {escape(payload.task_id)}</title>
  <style>
    body {{
      color: #1f2937;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
      margin: 40px auto;
      max-width: 980px;
      padding: 0 24px;
    }}
    h1 {{ margin-bottom: 0.25rem; }}
    h2 {{ margin-top: 2rem; }}
    table {{ border-collapse: collapse; margin: 16px 0; width: 100%; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f4f6; width: 160px; }}
    .report {{
      background: #f9fafb;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      padding: 20px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
  </style>
</head>
<body>
  <h1>Clarify Final Report {status_badge}</h1>
  <table><tbody>
{meta_rows}
  </tbody></table>
  {cluster_rows}
  <h2>Report</h2>
  <main class="report">{escape(payload.report or payload.summary or "")}</main>
</body>
</html>
"""
    dest.write_text(html, encoding="utf-8")
    return dest


def write_disambiguated_request(
    payload: ClarifyReportPayload,
    path: Path | str,
) -> Path:
    """Write a downstream-facing ``disambiguated_request.md`` from *payload*.

    The file contains the original feature request followed by any ambiguity
    entries that the agent identified.  If the agent's full ``report`` field
    contains a section starting with ``## Ambiguit`` we embed that section
    verbatim; otherwise we fall back to the ``ambiguities`` list.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        f"# Disambiguated Feature Request",
        f"",
        f"**Task:** {payload.task_id}",
        f"**Status:** {payload.status}",
        f"",
        f"---",
        f"",
    ]

    report_text = payload.report or ""
    ambiguity_section = ""
    for marker in ("## Ambiguit", "## Identified Ambiguit", "## Clarification"):
        idx = report_text.find(marker)
        if idx != -1:
            ambiguity_section = report_text[idx:]
            break

    if ambiguity_section:
        lines.append(ambiguity_section)
    elif payload.ambiguities:
        lines.append("## Ambiguities\n")
        for entry in payload.ambiguities:
            lines.append(f"### {entry.id}. {entry.title}")
            if entry.description:
                lines.append(f"\n{entry.description}\n")
            if entry.classification:
                lines.append(f"**Classification:** {entry.classification}\n")
    else:
        lines.append(
            "_No structured ambiguities extracted. See the full report above._\n"
        )

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest
