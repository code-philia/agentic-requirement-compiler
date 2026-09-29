"""Stage snapshots committed with source history; restart on an isolated worktree."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from arcbench_agent_runtime.jsonio import write_json_atomic
from .git_history import GitStageError, ProjectGitHistory
from .process_utils import resolve_executable, run_command


START_FROM = ("zero", "initialized", "database", "backend-ir", "frontend-ir", "lowered")
SNAPSHOT = ".arc/checkpoints/current.json"
REQUIRED = {
    "initialized": set(),
    "database": {"preprocessing", "database"},
    "backend-ir": {"preprocessing", "database", "fixture_ir", "database_manifest", "design"},
    "frontend-ir": {"preprocessing", "database", "fixture_ir", "database_manifest", "design", "backend_routes", "frontend"},
    "lowered": {"preprocessing", "database", "fixture_ir", "database_manifest", "design", "backend_routes", "frontend"},
}


class CheckpointStore:
    def __init__(self, root: Path, requirement_path: Path, port: int) -> None:
        self.root = root
        self.requirement_path = requirement_path
        self.port = port
        self.payload: dict[str, Any] = {}

    def save(self, stage: str, **values: Any) -> None:
        self.payload.update(values)
        write_json_atomic(self.root / SNAPSHOT, {
            "stage": stage,
            "requirement_sha256": hashlib.sha256(self.requirement_path.read_bytes()).hexdigest(),
            "web_port": self.port,
            "payload": self.payload,
        })
        ProjectGitHistory(self.root).commit(f"checkpoint {stage}", [SNAPSHOT])

    def load(self, stage: str, *, resume: bool = False) -> dict[str, Any]:
        try:
            snapshot = json.loads((self.root / SNAPSHOT).read_text(encoding="utf-8"))
            validate_snapshot(snapshot, stage)
            if stage != "initialized" and snapshot["requirement_sha256"] != hashlib.sha256(self.requirement_path.read_bytes()).hexdigest():
                raise ValueError("Requirement document differs from checkpoint; start from zero/initialized with the intended source.")
            if snapshot["web_port"] != self.port:
                raise ValueError(f"Checkpoint uses port {snapshot['web_port']}; use the same --port.")
            history = ProjectGitHistory(self.root)
            committed = json.loads(history._run(["show", f"HEAD:{SNAPSHOT}"]))
            if committed != snapshot:
                raise ValueError("Checkpoint snapshot differs from HEAD; select a committed checkpoint.")
            protected = ([".arc/design", ".arc/preprocessing", ".arc/database", ".arc/fixtures"] if resume else
                         ["backend", "frontend", "shared", "package.json", "package-lock.json", ".arc/design",
                          ".arc/lowering", ".arc/database", ".arc/fixtures", ".arc/code", ".arc/preprocessing", ".arc/project"])
            changed = history._run(["diff", "--name-only", "HEAD", "--", *protected])
            if changed:
                raise ValueError("Checkpoint source has uncommitted changes: " + changed)
            self.payload = snapshot["payload"]
            return self.payload
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GitStageError(f"Cannot start from {stage}: {exc}") from exc


def validate_snapshot(snapshot: Any, stage: str) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("stage") != stage:
        raise ValueError(f"Selected Git commit is not a {stage} checkpoint")
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or REQUIRED[stage] - payload.keys():
        raise ValueError(f"Incomplete {stage} checkpoint payload")
    if any(not isinstance(payload[name], dict) for name in REQUIRED[stage]):
        raise ValueError("Checkpoint payload sections must be objects")
    if stage != "initialized":
        preprocessing = payload["preprocessing"]
        if not all(isinstance(preprocessing.get(k), dict) for k in ("requirement_ir", "dependency_graph")):
            raise ValueError("Checkpoint has no complete preprocessing artifacts")
        if not preprocessing["requirement_ir"].get("root_id"):
            raise ValueError("Checkpoint Requirement IR has no root")
    if stage in {"frontend-ir", "lowered"} and payload["frontend"]["report"]["status"] not in {"GENERATED", "GENERATED_WITH_WARNINGS"}:
        raise ValueError("Partial frontend IR cannot be used as a checkpoint")
    if stage in {"frontend-ir", "lowered"}:
        frontend = payload["frontend"].get("frontend_ir")
        if not isinstance(frontend, dict) or not frontend.get("root_component_id"):
            raise ValueError("Checkpoint requires the new seven-entity frontend IR")


def prepare_restart(source: Path, stage: str, revision: str | None = None,
                    destination: Path | None = None, requirement_path: Path | None = None,
                    requested_port: int | None = None) -> tuple[Path, int, str]:
    """Use only committed state. Dirty source and its active branch are never touched."""
    if stage not in REQUIRED:
        raise GitStageError(f"Unknown restart stage {stage}")
    source = source.resolve()
    if not (source / ".git").exists():
        raise GitStageError(f"Not a generated Git project: {source}")
    history = ProjectGitHistory(source)
    if revision:
        commit = history._run(["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"])
    else:
        commit = history._run(["log", "-1", "--format=%H", "--fixed-strings",
                               "--grep", f"ARC: checkpoint {stage}", "HEAD"])
        if not commit:
            old_stages = {"initialized": "0 project initialization", "database": "2.1 database schema design",
                          "backend-ir": "3.1 backend design", "frontend-ir": "4.1 frontend IR generation",
                          "lowered": "4.3 frontend build accepted"}
            commit = history._run(["log", "-1", "--format=%H", "--fixed-strings",
                                   "--grep", f"ARC: {old_stages[stage]}", "HEAD"])
    if not commit:
        raise GitStageError(f"No completed {stage} checkpoint found in HEAD history")
    legacy = not history._run(["ls-tree", "--name-only", commit, "--", SNAPSHOT])
    try:
        snapshot = legacy_snapshot(history, commit, stage) if legacy else json.loads(history._run(["show", f"{commit}:{SNAPSHOT}"]))
        if not legacy:
            validate_snapshot(snapshot, stage)
        if requested_port is not None and requested_port != int(snapshot["web_port"]):
            raise ValueError(f"Checkpoint requires --port {snapshot['web_port']}")
        if requirement_path is not None and stage != "initialized":
            if snapshot["requirement_sha256"] != hashlib.sha256(requirement_path.read_bytes()).hexdigest():
                raise ValueError("Requirement document differs from the selected checkpoint")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GitStageError(f"Invalid checkpoint at {commit}: {exc}") from exc
    suffix = uuid.uuid4().hex[:8]
    target = destination.resolve() if destination else source.with_name(f"{source.name}-from-{stage}-{suffix}")
    if target.exists() or target == source or source in target.parents:
        raise GitStageError("Restart output must be a new directory outside the source project")
    history._run(["worktree", "add", "-b", f"arc/restart/{stage}/{suffix}", str(target), commit])
    if legacy:
        try:
            complete_legacy_manifests(target, snapshot)
            if stage == "initialized" and requirement_path is not None:
                snapshot["requirement_sha256"] = hashlib.sha256(requirement_path.read_bytes()).hexdigest()
            validate_snapshot(snapshot, stage)
            write_json_atomic(target / SNAPSHOT, snapshot)
            ProjectGitHistory(target).commit(f"checkpoint {stage}", [SNAPSHOT])
        except Exception as exc:
            raise GitStageError(f"Cannot import historical checkpoint; worktree retained at {target}: {exc}") from exc
    return target, int(snapshot["web_port"]), commit


def legacy_snapshot(history: ProjectGitHistory, commit: str, stage: str) -> dict[str, Any]:
    """Read actual historical artifacts, never today's copies or a guessed successful stage."""
    expected = {"initialized": "0 project initialization", "database": "2.1 database schema design",
                "backend-ir": "3.1 backend design", "frontend-ir": "4.1 frontend IR generation",
                "lowered": "4.3 frontend build accepted"}
    subject = history._run(["show", "-s", "--format=%s", commit])
    if subject != f"ARC: {expected[stage]}":
        raise ValueError(f"Expected historical stage commit 'ARC: {expected[stage]}', got {subject!r}")
    def read(path: str) -> dict[str, Any]:
        return json.loads(history._run(["show", f"{commit}:{path}"]))

    def read_first(*paths: str) -> dict[str, Any]:
        last: Exception | None = None
        for path in paths:
            try:
                return read(path)
            except Exception as exc:
                last = exc
        raise last or ValueError("No historical artifact path supplied")
    manifest = read(".arc/project/project-manifest.json")
    port = urlparse(manifest["deployment"]["e2eBaseUrl"]).port
    if port is None:
        raise ValueError("Historical project manifest has no deployment port")
    payload: dict[str, Any] = {}
    digest = ""
    if stage != "initialized":
        requirements = read(".arc/preprocessing/requirement_ir.json")
        payload["preprocessing"] = {"requirement_ir": requirements,
                                    "dependency_graph": read(".arc/preprocessing/dependency_graph.json")}
        digest = requirements["source"]["sha256"]
        payload["database"] = read_first(".arc/design/database/schema.json", ".arc/database/schema.json")
    if stage in {"backend-ir", "frontend-ir", "lowered"}:
        payload["fixture_ir"] = read_first(".arc/design/database/fixture_ir.json", ".arc/fixtures/fixture_ir.json")
        requirements = read_first(".arc/design/backend/requirements.json", ".arc/design/design.json")
        modules = read_first(".arc/design/backend/modules.json", ".arc/design/design.json")
        payload["design"] = {"requirements": requirements.get("requirements", []),
                              "modules": modules.get("modules", [])}
        if "modules" not in modules and "requirements" in modules:
            payload["design"] = modules
    if stage in {"frontend-ir", "lowered"}:
        frontend_tables = {}
        for table in ("components", "data", "ui", "properties", "events", "handlers", "effects", "api_dependencies"):
            frontend_tables[table] = read_first(
                f".arc/design/frontend/{table}.json",
                ".arc/design/frontend/frontend.json",
            ).get(table, [])
        metadata = read_first(".arc/design/frontend/metadata.json", ".arc/design/frontend/frontend.json")
        frontend_ir = {"root_component_id": metadata.get("root_component_id") or next(
            (row["id"] for row in frontend_tables["components"] if row.get("name") == "App"), None
        ), **frontend_tables}
        visual = read_first(".arc/design/frontend/visual_references.json", ".arc/design/frontend/visual-references.json")
        frontend_ir["visual_references"] = visual.get("visual_references", [])
        payload["frontend"] = {"frontend_ir": frontend_ir,
                               "report": read_first(".arc/design/frontend/report.json")}
        if payload["frontend"]["report"]["status"] not in {"GENERATED", "GENERATED_WITH_WARNINGS"}:
            raise ValueError("Historical frontend IR is partial; select backend-ir instead")
        if "root_component_id" not in payload["frontend"]["frontend_ir"]:
            raise ValueError("Historical Thin Frontend IR is not supported")
    if stage == "lowered" and read_first(".arc/lowering/frontend/lowering.json", ".arc/code/frontend/lowering.json").get("build_status") != "PASSED":
        raise ValueError("Historical lowering has no successful build record")
    return {"stage": stage, "web_port": port, "requirement_sha256": digest, "payload": payload}


def complete_legacy_manifests(root: Path, snapshot: dict[str, Any]) -> None:
    """Rebuild missing planning metadata deterministically, without model calls or source writes."""
    if snapshot["stage"] in {"initialized", "database"}:
        return
    from .symbol_planning import GlobalSymbolPlanner
    from .file_planning import GlobalFilePlanner
    from .skeleton_lowering import DatabaseSchemaLowerer, TypeLowerer
    from .module_lowering import ModuleSkeletonLowerer
    from .backend_lowering import BackendGlueLowerer
    from .fixture_lowering import FixtureLowerer, fixture_source_paths

    def checked(result: Any) -> Any:
        if not result.ok:
            raise ValueError("; ".join(result.errors))
        return result
    payload = snapshot["payload"]
    manifest = json.loads((root / ".arc/project/project-manifest.json").read_text(encoding="utf-8"))
    empty = {"requirements": [], "modules": []}
    symbols = checked(GlobalSymbolPlanner().plan(empty, payload["database"], manifest))
    files = checked(GlobalFilePlanner(root).plan(empty, symbols.registry, manifest,
                    fixture_paths=fixture_source_paths(payload["fixture_ir"])))
    database = checked(DatabaseSchemaLowerer().lower(payload["database"], symbols.registry, files.registry))
    fixture = checked(FixtureLowerer().lower(payload["fixture_ir"], database.manifest))
    database.manifest["generated_files"] = sorted(
        path for path in database.sources | fixture.sources if path.startswith("backend/src/"))
    payload["database_manifest"] = database.manifest
    if snapshot["stage"] == "backend-ir":
        return
    design = payload["design"]
    symbols = checked(GlobalSymbolPlanner().plan(design, payload["database"], manifest))
    files = checked(GlobalFilePlanner(root).plan(design, symbols.registry, manifest,
                    fixture_paths=fixture_source_paths(payload["fixture_ir"])))
    types = checked(TypeLowerer().lower(symbols.registry, files.registry))
    manifests = {"type": types.manifest, "database": database.manifest}
    for kind in ("DB", "FUNC", "API"):
        manifests[kind] = checked(ModuleSkeletonLowerer().lower(kind, design, symbols.registry, files.registry)).manifest
    glue = checked(BackendGlueLowerer().lower(design, symbols.registry, files.registry, manifests,
                                             default_port=snapshot["web_port"]))
    payload["backend_routes"] = glue.route_registry


def install_restart_dependencies(root: Path) -> None:
    """Ignored node_modules are not in Git; recreate links in the new worktree."""
    executable = resolve_executable("npm", os.environ)
    if executable is None:
        raise GitStageError("npm is required in the restart worktree")
    try:
        result = run_command([executable, "ci"], cwd=root, environment=os.environ, timeout=900)
    except Exception as exc:
        raise GitStageError(f"Restart dependency installation failed: {exc}") from exc
    if result.returncode:
        raise GitStageError(f"Restart npm ci failed: {result.stderr or result.stdout}")
    if (root / "backend/init-db.mjs").is_file():
        node = resolve_executable("node", os.environ)
        if node is None:
            raise GitStageError("node is required to rebuild the ignored database in the restart worktree")
        try:
            seeded = run_command([node, "init-db.mjs"], cwd=root / "backend", environment=os.environ, timeout=120)
        except Exception as exc:
            raise GitStageError(f"Restart database initialization failed: {exc}") from exc
        if seeded.returncode:
            raise GitStageError(f"Restart database initialization failed: {seeded.stderr or seeded.stdout}")
