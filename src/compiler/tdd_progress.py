"""Durable node admission records for in-place TDD continuation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arcbench_agent_runtime.jsonio import write_json_atomic

from .git_history import ProjectGitHistory


class TDDProgress:
    def __init__(self, root: Path, requirement_ir: dict[str, Any], order: list[str], *, resume: bool) -> None:
        self.path = root / ".arc/tdd/progress.json"
        fingerprint = hashlib.sha256(json.dumps(requirement_ir, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.data: dict[str, Any] = {"requirement_fingerprint": fingerprint, "order": order, "nodes": {}}
        if resume and self.path.is_file():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("TDD progress must be an object")
            if loaded.get("requirement_fingerprint") != fingerprint or loaded.get("order") != order:
                raise ValueError("TDD progress does not match the requirement IR or traversal order")
            nodes = loaded.get("nodes")
            if not isinstance(nodes, dict) or set(nodes) - set(order):
                raise ValueError("Invalid TDD progress nodes")
            if any(not isinstance(row, dict) or row.get("status") not in
                   {"STARTED", "FAILED", "TESTS_PASSED", "AGGREGATE_NO_UI"} for row in nodes.values()):
                raise ValueError("Invalid TDD progress status")
            self.data = loaded
        elif resume:
            # Older runs have no admission journal. Use positive evidence only;
            # a generated test proves admission, never successful completion.
            manifest = root / ".arc/tests/test_manifest.json"
            if manifest.is_file():
                for row in (load_tdd_manifest(root) or {}).get("files", []):
                    rid = row.get("requirement_id")
                    if rid in order:
                        self.data["nodes"][rid] = {"status": "STARTED", "stage": "legacy test manifest"}
            subjects = ProjectGitHistory(root)._run(["log", "--format=%s"]).splitlines()
            for rid in order:
                if f"ARC: 5 test generation {rid}" in subjects:
                    self.data["nodes"].setdefault(rid, {"status": "STARTED", "stage": "legacy Git history"})
            log_path = root / ".arc/debug.log"
            if log_path.is_file():
                with log_path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        started = re.search(r"Generating and freezing tests for (\S+)\.", line)
                        completed = re.search(r"TDD completed for (\S+)\.", line)
                        match = completed or started
                        if match and match[1] in order:
                            self.data["nodes"][match[1]] = {
                                "status": "TESTS_PASSED" if completed else "STARTED",
                                "stage": "legacy compiler log",
                            }
        self.save()

    @property
    def nodes(self) -> dict[str, Any]:
        return self.data["nodes"]

    def mark(self, rid: str, status: str, stage: str) -> None:
        self.nodes[rid] = {"status": status, "stage": stage,
                           "updated_at": datetime.now(timezone.utc).isoformat()}
        self.save()

    def save(self) -> None:
        write_json_atomic(self.path, self.data)


def load_tdd_manifest(root: Path) -> dict[str, Any] | None:
    path = root / ".arc/tests/test_manifest.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or not isinstance(value.get("files"), list)
            or any(not isinstance(row, dict) for row in value["files"])):
        raise ValueError("Invalid existing test manifest")
    return value
