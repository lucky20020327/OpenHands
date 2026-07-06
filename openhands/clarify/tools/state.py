from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()


@dataclass
class ClarifyState:
    state_path: str
    workspace: str | None = None
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    next_variant_id: int = 1
    variants: dict[str, str] = field(default_factory=dict)
    task_done_status: str | None = None
    task_done_summary: str | None = None
    task_done_report: str | None = None
    n_variants: int = 4


def state_path_for(*, persistence_dir: str | None, working_dir: str) -> Path:
    root = Path(persistence_dir) if persistence_dir else Path(working_dir) / ".openhands"
    return root / "clarify" / "state.json"


def load_state(path: Path) -> ClarifyState:
    with _LOCK:
        if not path.is_file():
            return ClarifyState(state_path=str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("state_path", str(path))
        data.setdefault("variants", {})
        data.setdefault("metadata", {})
        allowed = {item.name for item in fields(ClarifyState)}
        data = {key: value for key, value in data.items() if key in allowed}
        return ClarifyState(**data)


def save_state(state: ClarifyState) -> None:
    with _LOCK:
        path = Path(state.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def state_as_public_dict(state: ClarifyState) -> dict[str, Any]:
    data = asdict(state)
    data.pop("state_path", None)
    return data

