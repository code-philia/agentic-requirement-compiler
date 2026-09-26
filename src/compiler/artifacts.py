from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from arcbench_agent_runtime.jsonio import write_json_atomic

from .frontend_thin_design import validate_thin_frontend_design

class CompilerArtifactStore:
    """Persist compact, stage-owned JSON symbol tables."""

    def __init__(self, output_dir: Path) -> None:
        self.root = output_dir.expanduser().resolve() / ".arc"
        # Preprocessing owns requirement normalization; product frontend design
        # artifacts may use .arc/frontend independently.
        self.preprocessing_root = self.root / "preprocessing"
        self.database_root = self.root / "database"
        self.fixtures_root = self.root / "fixtures"
        self.design_root = self.root / "design"
        self.backend_design_root = self.design_root / "backend"
        self.frontend_design_root = self.design_root / "frontend"
        self.backend_root = self.root / "backend"
        self.frontend_root = self.root / "frontend"
        self.code_root = self.root / "code"
        self.tests_root = self.root / "tests"

    def write_preprocessing(
        self,
        *,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any],
    ) -> dict[str, str]:
        shutil.rmtree(self.preprocessing_root, ignore_errors=True)
        shutil.rmtree(self.design_root, ignore_errors=True)
        paths = {
            "requirement_ir": self.preprocessing_root / "requirement_ir.json",
            "dependency_graph": self.preprocessing_root / "dependency_graph.json",
        }
        nodes = requirement_ir.get("nodes", {})
        requirements = [copy.deepcopy(nodes[key]) for key in requirement_ir.get("node_order", []) if key in nodes]
        if not requirements:
            requirements = [copy.deepcopy(value) for _, value in sorted(nodes.items()) if isinstance(value, dict)]
        atomic_dependencies = dependency_graph.get("atomic_dependencies", {})
        requirement_dependencies = dependency_graph.get("requirement_dependencies", {})
        wave_by_requirement = {
            str(requirement_id): wave_index
            for wave_index, wave in enumerate(dependency_graph.get("implementation_waves", []), start=1)
            for requirement_id in wave
        }
        dependencies = []
        for requirement_id, values in sorted(dependency_graph.get("requirements", {}).items()):
            item = {
                "requirement_id": requirement_id,
                "dependencies": list(values) if isinstance(values, list) else [],
            }
            if requirement_id in atomic_dependencies:
                item["effective_atomic_dependencies"] = list(atomic_dependencies[requirement_id])
            if requirement_id in requirement_dependencies:
                item["effective_requirement_dependencies"] = list(
                    requirement_dependencies[requirement_id]
                )
            if requirement_id in wave_by_requirement:
                item["implementation_wave"] = wave_by_requirement[requirement_id]
            dependencies.append(item)
        write_json_atomic(paths["requirement_ir"], requirements)
        write_json_atomic(paths["dependency_graph"], dependencies)
        return {name: str(path) for name, path in paths.items()}

    def write_fixture_ir(self, fixture_ir: dict[str, Any]) -> str:
        path = self.fixtures_root / "fixture_ir.json"
        write_json_atomic(path, fixture_ir)
        return str(path)

    def read_fixture_ir(self) -> tuple[dict[str, Any] | None, str | None]:
        path = self.fixtures_root / "fixture_ir.json"
        if not path.is_file():
            return None, f"Fixture IR does not exist: {path}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read Fixture IR: {exc}"
        if not isinstance(payload, dict):
            return None, f"Fixture IR must contain an object: {path}"
        return payload, None

    def write_database(
        self, *, entities: dict[str, dict[str, Any]], relationships: list[dict[str, Any]],
    ) -> dict[str, str]:
        path = self.database_root / "database.json"
        write_json_atomic(path, {"entities": entities, "relationships": relationships})
        return {"database": str(path)}
    def read_database(self) -> tuple[dict[str, Any] | None, str | None]:
        """Read the persisted JSON ER graph into the internal structure."""

        schema_path = self.database_root / "database.json"
        relationships_path = schema_path
        if not schema_path.is_file():
            return None, f"Database artifact does not exist: {schema_path}"
        try:
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read Database artifact: {exc}"
        if not isinstance(payload, dict):
            return None, f"Database artifact must be an object: {schema_path}"
        entities = payload.get("entities")
        relationships = payload.get("relationships")
        if not isinstance(entities, dict) or any(not isinstance(value, dict) for value in entities.values()):
            return None, f"Database schema artifact must be keyed by entity id: {schema_path}"
        if not isinstance(relationships, list) or any(
            not isinstance(item, dict) or item.get("kind") != "RELATIONSHIP" for item in relationships
        ):
            return None, f"Relationships artifact must contain only RELATIONSHIP symbols: {relationships_path}"

        entity_rows = []
        constraints: list[dict[str, Any]] = []
        for entity_id, value in sorted(entities.items()):
            entity = copy.deepcopy(value)
            entity_constraints = entity.pop("constraints", [])
            if not isinstance(entity_constraints, list):
                return None, f"Entity constraints must be a list: {schema_path}#{entity_id}"
            for constraint in entity_constraints:
                if not isinstance(constraint, dict) or constraint.get("kind") != "CONSTRAINT":
                    return None, f"Entity constraints must contain only CONSTRAINT symbols: {schema_path}#{entity_id}"
                restored = copy.deepcopy(constraint)
                restored.pop("kind", None)
                constraints.append(restored)
            fields = entity.get("fields", [])
            if not isinstance(fields, list):
                return None, f"Entity fields must be a list: {schema_path}#{entity_id}"
            for field in fields:
                if not isinstance(field, dict):
                    return None, f"Entity fields must contain objects: {schema_path}#{entity_id}"
                field_constraints = field.pop("constraints", [])
                if not isinstance(field_constraints, list):
                    return None, f"Field constraints must be a list: {schema_path}#{entity_id}.{field.get('name', '')}"
                for constraint in field_constraints:
                    if not isinstance(constraint, dict) or constraint.get("kind") != "CONSTRAINT":
                        return None, f"Field constraints must contain only CONSTRAINT symbols: {schema_path}#{entity_id}.{field.get('name', '')}"
                    restored = copy.deepcopy(constraint)
                    restored.pop("kind", None)
                    restored["fields"] = [f"{entity_id}.{field.get('name', '')}"]
                    constraints.append(restored)
            entity_rows.append({**entity, "key": entity_id})

        def without_kind(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = []
            for item in items:
                row = copy.deepcopy(item)
                row.pop("kind", None)
                rows.append(row)
            return rows

        return {
            "schema_version": 2,
            "status": "RESOLVED",
            "entities": entity_rows,
            "relationships": without_kind(relationships),
            "constraints": constraints,
        }, None

    def write_design(self, *, design_ir: dict[str, Any]) -> dict[str, str]:
        path = self.design_root / "design.json"
        write_json_atomic(path, design_ir)
        return {"design_ir": str(path)}

    def read_design(
        self, *, expected_requirement_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        path = self.design_root / "design.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read Design artifact {path}: {exc}"
        if not isinstance(value, dict) or not isinstance(value.get("modules"), list) or not isinstance(value.get("requirements"), list):
            return None, f"Design artifact is malformed: {path}"
        identifiers = {str(row.get("id")) for row in value["requirements"] if isinstance(row, dict)}
        if expected_requirement_ids is not None and identifiers != expected_requirement_ids:
            return None, f"Design requirements do not match current requirements: {path}"
        return value, None
    def write_frontend_design(self, *, frontend_ir: dict[str, Any]) -> dict[str, str]:
        issues = validate_thin_frontend_design(frontend_ir)
        if issues:
            raise ValueError(f"Cannot persist invalid Frontend Design IR: {issues[0].format()}")
        path = self.frontend_design_root / "frontend.json"
        write_json_atomic(path, frontend_ir)
        return {"frontend_design_ir": str(path)}

    def read_frontend_design(
        self, *, requirement_links: list[dict[str, Any]],
        expected_requirement_ids: set[str] | None = None,
        backend_api_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        path = self.frontend_design_root / "frontend.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read Frontend Design artifact {path}: {exc}"
        if not isinstance(value, dict):
            return None, f"Frontend Design artifact must be an object: {path}"
        issues = validate_thin_frontend_design(
            value, expected_requirement_ids=expected_requirement_ids,
            backend_api_ids=backend_api_ids,
        )
        if issues:
            return None, f"Frontend Design artifact is invalid: {issues[0].format()}"
        return value, None
    def read_project_manifest(self) -> tuple[dict[str, Any] | None, str | None]:
        """Validate the project-initialization boundary before Skeleton starts."""

        path = self.root / "project" / "project-manifest.json"
        if not path.is_file():
            return None, f"Project manifest does not exist: {path}"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read project manifest {path}: {exc}"
        if not isinstance(manifest, dict) or manifest.get("status") != "PROJECT_INITIALIZED":
            return None, f"Project manifest is not initialized: {path}"
        allowed = manifest.get("allowedOutputRoots", {}).get("skeleton", [])
        required_roots = {"backend/src", "shared/src/contracts", "shared/src/index.ts"}
        if not isinstance(allowed, list) or not required_roots <= {str(value) for value in allowed}:
            return None, f"Project manifest has incomplete Skeleton output roots: {path}"
        workspaces = manifest.get("workspaces")
        if not isinstance(workspaces, dict):
            return None, f"Project manifest has no workspace map: {path}"
        output_root = self.root.parent
        for name in ("frontend", "backend", "shared"):
            item = workspaces.get(name)
            relative_root = str(item.get("root", "")).strip() if isinstance(item, dict) else ""
            if not relative_root or not (output_root / relative_root).is_dir():
                return None, f"Initialized project workspace is missing: {name}"
        shared_link = output_root / "node_modules" / "@arc" / "shared"
        expected_shared = (output_root / "shared").resolve()
        actual_shared = shared_link.resolve()
        if not shared_link.exists() or actual_shared != expected_shared:
            return None, (
                "Initialized @arc/shared workspace link is invalid: "
                f"expected {expected_shared}, resolved {actual_shared}"
            )
        return manifest, None

    def write_symbol_registry(self, registry: dict[str, Any]) -> str:
        path = self.backend_root / "symbol_registry.json"
        write_json_atomic(path, registry)
        return str(path)

    def write_file_registry(self, registry: dict[str, Any]) -> str:
        path = self.backend_root / "file_registry.json"
        write_json_atomic(path, registry)
        return str(path)

    def write_type_manifest(self, manifest: dict[str, Any]) -> str:
        path = self.backend_root / "type_manifest.json"
        write_json_atomic(path, manifest)
        return str(path)

    def write_database_schema_manifest(self, manifest: dict[str, Any]) -> str:
        path = self.backend_root / "database_schema_manifest.json"
        write_json_atomic(path, manifest)
        return str(path)

    def write_module_manifest(
        self,
        module_kind: str,
        manifest: dict[str, Any],
    ) -> str:
        filenames = {
            "DB": "db_modules_manifest.json",
            "FUNC": "func_modules_manifest.json",
            "API": "api_modules_manifest.json",
        }
        kind = str(module_kind).upper()
        if kind not in filenames:
            raise ValueError(f"Unsupported module manifest kind: {module_kind!r}")
        path = self.backend_root / filenames[kind]
        write_json_atomic(path, manifest)
        return str(path)

    def write_backend_lowering(
        self,
        *,
        manifest: dict[str, Any],
    ) -> dict[str, str]:
        path = self.backend_root / "manifest.json"
        write_json_atomic(path, manifest)
        return {"backend_manifest": str(path)}

    def write_frontend_symbol_registry(self, registry: dict[str, Any]) -> str:
        path = self.frontend_root / "symbol_registry.json"
        write_json_atomic(path, registry)
        return str(path)

    def write_frontend_file_registry(self, registry: dict[str, Any]) -> str:
        path = self.frontend_root / "file_registry.json"
        write_json_atomic(path, registry)
        return str(path)

    def write_frontend_lowering(
        self,
        *,
        manifest: dict[str, Any],
    ) -> dict[str, str]:
        path = self.frontend_root / "manifest.json"
        write_json_atomic(path, manifest)
        return {"frontend_manifest": str(path)}

    def write_code_bindings(self, registry: dict[str, Any]) -> str:
        path = self.code_root / "code_bindings.json"
        write_json_atomic(path, registry)
        return str(path)

    def read_code_bindings(self) -> tuple[dict[str, Any] | None, str | None]:
        path = self.code_root / "code_bindings.json"
        if not path.is_file():
            return None, f"Code Binding Registry does not exist: {path}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read Code Binding Registry: {exc}"
        if not isinstance(payload, dict):
            return None, f"Code Binding Registry must contain an object: {path}"
        return payload, None

    def write_test_environment_manifest(self, manifest: dict[str, Any]) -> str:
        path = self.tests_root / "environment_manifest.json"
        write_json_atomic(path, manifest)
        return str(path)

    def write_test_manifest(self, manifest: dict[str, Any]) -> str:
        path = self.tests_root / "test_manifest.json"
        write_json_atomic(path, manifest)
        return str(path)

    def read_test_manifest(self) -> tuple[dict[str, Any] | None, str | None]:
        path = self.tests_root / "test_manifest.json"
        if not path.is_file():
            return None, None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"Cannot read Test Manifest: {exc}"
        if not isinstance(payload, dict):
            return None, f"Test Manifest must contain an object: {path}"
        return payload, None

    def write_generated_tests(self, sources: dict[str, str]) -> dict[str, str]:
        output_root = self.root.parent
        artifacts: dict[str, str] = {}
        for relative, content in sorted(sources.items()):
            normalized = str(relative).replace("\\", "/").strip().strip("/")
            path = PurePosixPath(normalized)
            if (
                not normalized
                or path.is_absolute()
                or "." in path.parts
                or ".." in path.parts
                or not normalized.startswith(
                    ("tests/unit/", "tests/integration/", "tests/e2e/")
                )
                or not normalized.endswith(".spec.ts")
            ):
                raise ValueError(f"Invalid generated test path: {relative!r}")
            target = (output_root / Path(normalized)).resolve()
            if output_root not in target.parents:
                raise ValueError(f"Generated test escapes output workspace: {relative!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f"{target.suffix}.tmp")
            # Test Manifest hashes the canonical UTF-8 source bytes. Writing
            # text with newline=None translates LF to CRLF on Windows and
            # immediately invalidates the newly frozen SHA-256.
            temporary.write_bytes(content.encode("utf-8"))
            temporary.replace(target)
            artifacts[f"generated_test:{normalized}"] = str(target)
        return artifacts

    def write_generated_sources(self, sources: dict[str, str]) -> dict[str, str]:
        """Atomically materialize compiler-planned source files inside the output workspace."""

        output_root = self.root.parent
        artifacts: dict[str, str] = {}
        for relative, content in sorted(sources.items()):
            normalized = str(relative).replace("\\", "/").strip().strip("/")
            path = PurePosixPath(normalized)
            if (
                not normalized
                or path.is_absolute()
                or "." in path.parts
                or ".." in path.parts
                or not (normalized.endswith((".ts", ".tsx")) or normalized == "backend/init-db.mjs")
            ):
                raise ValueError(f"Invalid generated source path: {relative!r}")
            target = (output_root / Path(normalized)).resolve()
            if output_root != target and output_root not in target.parents:
                raise ValueError(f"Generated source escapes output workspace: {relative!r}")
            if not (
                normalized.startswith("backend/src/")
                or normalized == "backend/init-db.mjs"
                or normalized.startswith("shared/src/")
                or normalized.startswith("frontend/src/")
                or normalized == "frontend/vite.config.ts"
            ):
                raise ValueError(f"Generated source is outside Stage 3 output roots: {relative!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f"{target.suffix}.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
            artifacts[f"generated_source:{normalized}"] = str(target)
        return artifacts
