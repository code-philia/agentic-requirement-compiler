from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CompilationRequest:
    """Everything callers must provide to run the compiler."""

    requirement_path: Path
    output_dir: Path
    web_port: int = 3000
    start_from: str = "zero"


@dataclass(slots=True)
class CompilationResult:
    """Compiler outcome independent of CLI rendering."""

    ok: bool
    root_id: str | None = None
    states: dict[str, str] = field(default_factory=dict)
    failed_nodes: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "root_id": self.root_id,
            "states": dict(sorted(self.states.items())),
            "failed_nodes": sorted(self.failed_nodes),
            "artifacts": dict(sorted(self.artifacts.items())),
        }
