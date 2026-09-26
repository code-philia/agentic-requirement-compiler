from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

FILE_REGISTRY_SCHEMA_VERSION = 1
FILES_PLANNED = "FILES_PLANNED"
MODULE_KINDS = {"API", "FUNC", "DB"}

SYSTEM_FILES = (
    ("shared/src/index.ts", "SHARED_ROOT_BARREL", "COMPILER"),
    ("shared/src/contracts/index.ts", "CONTRACT_BARREL", "SKELETON_COMPILER"),
    ("shared/src/contracts/runtime.ts", "RUNTIME_TYPES", "SKELETON_COMPILER"),
    ("backend/src/api/index.ts", "API_BARREL", "SKELETON_COMPILER"),
    ("backend/src/functions/index.ts", "FUNCTION_BARREL", "SKELETON_COMPILER"),
    ("backend/src/db/schema/index.ts", "DATABASE_SCHEMA_BARREL", "SKELETON_COMPILER"),
    ("backend/src/db/repositories/index.ts", "REPOSITORY_BARREL", "SKELETON_COMPILER"),
    ("backend/src/db/client.ts", "DATABASE_CLIENT", "SKELETON_COMPILER"),
    ("backend/src/fixtures/index.ts", "FIXTURE_ENTRY", "SKELETON_COMPILER"),
    ("backend/src/generated/router.ts", "ROUTER", "SKELETON_COMPILER"),
    ("backend/src/generated/module-registry.ts", "MODULE_REGISTRY", "SKELETON_COMPILER"),
    ("backend/src/generated/dependency-registry.ts", "DEPENDENCY_REGISTRY", "SKELETON_COMPILER"),
    ("backend/src/runtime/errors.ts", "RUNTIME_ERRORS", "SKELETON_COMPILER"),
    ("backend/src/runtime/ids.ts", "RUNTIME_IDS", "SKELETON_COMPILER"),
    ("backend/src/runtime/clock.ts", "RUNTIME_CLOCK", "SKELETON_COMPILER"),
    ("backend/src/index.ts", "BACKEND_ROOT_BARREL", "SKELETON_COMPILER"),
    ("backend/src/app.ts", "APPLICATION", "SKELETON_COMPILER"),
    ("backend/src/server.ts", "SERVER", "SKELETON_COMPILER"),
)


@dataclass(slots=True)
class FilePlanningResult:
    registry: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class GlobalFilePlanner:
    """Assign every planned symbol and module to one deterministic source file."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root.expanduser().resolve()
        self._allowed_roots: tuple[str, ...] = ()
        self._files: dict[str, dict[str, Any]] = {}
        self._symbol_locations: dict[str, dict[str, Any]] = {}
        self._module_locations: dict[str, dict[str, Any]] = {}
        self._allocated_paths: set[str] = set()
        self._errors: list[str] = []

    def plan(
        self,
        design_ir: dict[str, Any],
        symbol_registry: dict[str, Any],
        project_manifest: dict[str, Any],
        fixture_paths: list[str] | None = None,
    ) -> FilePlanningResult:
        self._reset()
        self._validate_project_manifest(project_manifest)
        symbols = self._index_symbols(symbol_registry)
        modules = self._index_modules(design_ir)
        bindings = self._index_bindings(symbol_registry)
        self._validate_registry_status(symbol_registry)
        self._validate_module_coverage(modules, symbols, bindings)
        if self._errors:
            return self._result()

        self._reserve_system_files()
        for path in sorted(set(fixture_paths or [])):
            if path == "backend/src/fixtures/index.ts":
                continue
            self._add_file(
                path,
                role="FIXTURE_SET",
                owner="SKELETON_COMPILER",
                mutability="FROZEN",
                origin="FIXTURE_IR",
            )
        self._plan_entity_files(symbols)
        self._plan_contract_files(symbols, modules)
        self._plan_module_files(symbols, modules, bindings)
        self._validate_complete_placement(symbols)
        return self._result()

    def _reset(self) -> None:
        self._allowed_roots = ()
        self._files.clear()
        self._symbol_locations.clear()
        self._module_locations.clear()
        self._allocated_paths.clear()
        self._errors.clear()

    def _validate_project_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict) or manifest.get("status") != "PROJECT_INITIALIZED":
            self._errors.append(
                "ARC3201 PROJECT_NOT_INITIALIZED: project-manifest.json is missing or not initialized."
            )
            return
        allowed = manifest.get("allowedOutputRoots", {}).get("skeleton", [])
        required = {"backend/src", "shared/src/contracts", "shared/src/index.ts"}
        if not isinstance(allowed, list) or not required <= {str(value) for value in allowed}:
            self._errors.append(
                "ARC3202 PROJECT_MANIFEST_INVALID: skeleton output roots are incomplete."
            )
            return
        normalized: list[str] = []
        for value in allowed:
            path = _normalize_relative_path(value)
            if path is None:
                self._errors.append(
                    f"ARC3202 PROJECT_MANIFEST_INVALID: invalid skeleton output root {value!r}."
                )
                continue
            candidate = (self.output_root / Path(path)).resolve()
            if (
                not candidate.exists()
                or (candidate != self.output_root and self.output_root not in candidate.parents)
            ):
                self._errors.append(
                    f"ARC3202 PROJECT_MANIFEST_INVALID: skeleton output root is unavailable: {path}."
                )
                continue
            normalized.append(path)
        self._allowed_roots = tuple(sorted(set(normalized)))

    def _validate_registry_status(self, registry: Any) -> None:
        if not isinstance(registry, dict) or registry.get("status") != "SYMBOLS_PLANNED":
            self._errors.append(
                "ARC3203 SYMBOL_REGISTRY_INVALID: Global Symbol Planning has not completed."
            )

    def _index_symbols(self, registry: Any) -> dict[str, dict[str, Any]]:
        rows = registry.get("symbols", []) if isinstance(registry, dict) else []
        if not isinstance(rows, list):
            self._errors.append("ARC3203 SYMBOL_REGISTRY_INVALID: symbols must be a list.")
            return {}
        symbols: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                self._errors.append(
                    "ARC3203 SYMBOL_REGISTRY_INVALID: symbols must contain only objects."
                )
                continue
            symbol_id = str(row.get("id", "")).strip()
            symbol_name = str(row.get("symbol", "")).strip()
            if not symbol_id or not symbol_name or symbol_id in symbols:
                self._errors.append(
                    f"ARC3204 SYMBOL_INVALID: invalid or duplicate symbol {symbol_id!r}."
                )
                continue
            if row.get("kind") not in {"MODULE", "DATA_CONTRACT", "ENTITY_TYPE", "ENTITY_TABLE"}:
                self._errors.append(
                    f"ARC3204 SYMBOL_INVALID: unsupported symbol kind for {symbol_id}."
                )
                continue
            symbols[symbol_id] = row
        return symbols

    def _index_modules(self, design_ir: Any) -> dict[str, dict[str, Any]]:
        rows = design_ir.get("modules", []) if isinstance(design_ir, dict) else []
        if not isinstance(rows, list):
            self._errors.append("ARC3205 DESIGN_IR_INVALID: modules must be a list.")
            return {}
        modules: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                self._errors.append(
                    "ARC3205 DESIGN_IR_INVALID: modules must contain only objects."
                )
                continue
            module_id = str(row.get("id", "")).strip()
            parsed = _parse_module_id(module_id)
            module_kind = str(row.get("kind", "")).strip().upper()
            if (
                not module_id
                or module_id in modules
                or parsed is None
                or module_kind != parsed[1]
            ):
                self._errors.append(
                    f"ARC3205 DESIGN_IR_INVALID: invalid or duplicate module {module_id!r}."
                )
                continue
            modules[module_id] = row
        return modules

    def _index_bindings(self, registry: Any) -> dict[str, dict[str, Any]]:
        rows = registry.get("module_bindings", []) if isinstance(registry, dict) else []
        if not isinstance(rows, list):
            self._errors.append(
                "ARC3203 SYMBOL_REGISTRY_INVALID: module_bindings must be a list."
            )
            return {}
        bindings: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                self._errors.append(
                    "ARC3203 SYMBOL_REGISTRY_INVALID: module_bindings must contain only objects."
                )
                continue
            module_id = str(row.get("module_id", "")).strip()
            if not module_id or module_id in bindings:
                self._errors.append(
                    f"ARC3206 MODULE_BINDING_INVALID: invalid or duplicate binding {module_id!r}."
                )
                continue
            bindings[module_id] = row
        return bindings

    def _validate_module_coverage(
        self,
        modules: dict[str, dict[str, Any]],
        symbols: dict[str, dict[str, Any]],
        bindings: dict[str, dict[str, Any]],
    ) -> None:
        module_symbols = {
            symbol_id
            for symbol_id, symbol in symbols.items()
            if symbol.get("kind") == "MODULE"
        }
        expected = set(modules)
        if module_symbols != expected:
            self._errors.append(
                "ARC3207 MODULE_COVERAGE_INVALID: Design IR modules and module symbols differ: "
                f"missing_symbols={sorted(expected - module_symbols)}, "
                f"unknown_symbols={sorted(module_symbols - expected)}."
            )
        if set(bindings) != expected:
            self._errors.append(
                "ARC3207 MODULE_COVERAGE_INVALID: Design IR modules and module bindings differ: "
                f"missing_bindings={sorted(expected - set(bindings))}, "
                f"unknown_bindings={sorted(set(bindings) - expected)}."
            )
        for module_id, binding in sorted(bindings.items()):
            module_symbol = symbols.get(module_id)
            if module_symbol is not None and binding.get("function_symbol") != module_symbol.get("symbol"):
                self._errors.append(
                    f"ARC3206 MODULE_BINDING_INVALID: function symbol mismatch for {module_id}."
                )
            if module_symbol is not None and module_symbol.get("module_kind") != modules.get(
                module_id, {}
            ).get("kind"):
                self._errors.append(
                    f"ARC3206 MODULE_BINDING_INVALID: module kind mismatch for {module_id}."
                )
            for key in ("input_contract_id", "output_contract_id"):
                contract_id = binding.get(key)
                if contract_id is not None and (
                    contract_id not in symbols
                    or symbols[contract_id].get("kind") != "DATA_CONTRACT"
                ):
                    self._errors.append(
                        f"ARC3206 MODULE_BINDING_INVALID: {module_id} references unknown {key} "
                        f"{contract_id!r}."
                    )

    def _reserve_system_files(self) -> None:
        for path, role, owner in SYSTEM_FILES:
            self._add_file(
                path,
                role=role,
                owner=owner,
                mutability="FROZEN",
                origin="SYSTEM",
            )

    def _plan_entity_files(self, symbols: dict[str, dict[str, Any]]) -> None:
        entity_paths: dict[str, str] = {}
        rows = sorted(
            (
                symbol
                for symbol in symbols.values()
                if symbol.get("kind") in {"ENTITY_TYPE", "ENTITY_TABLE"}
            ),
            key=lambda value: str(value.get("id", "")),
        )
        for symbol in rows:
            entity = str(symbol.get("entity", "")).strip()
            if not entity:
                self._errors.append(
                    f"ARC3208 ENTITY_PLACEMENT_INVALID: {symbol.get('id')} has no entity key."
                )
                continue
            path = entity_paths.get(entity)
            if path is None:
                path = self._allocate_path(
                    "backend/src/db/schema",
                    _kebab_case(entity),
                    qualifier=entity,
                )
                entity_paths[entity] = path
            self._place_symbol(
                symbol,
                path,
                role="DATABASE_SCHEMA",
                owner="SKELETON_COMPILER",
                mutability="FROZEN",
                origin="DATABASE_SCHEMA",
            )

    def _plan_contract_files(
        self,
        symbols: dict[str, dict[str, Any]],
        modules: dict[str, dict[str, Any]],
    ) -> None:
        contracts_by_primary_consumer: dict[str, list[dict[str, Any]]] = {}
        rows = sorted(
            (symbol for symbol in symbols.values() if symbol.get("kind") == "DATA_CONTRACT"),
            key=lambda value: str(value.get("id", "")),
        )
        for symbol in rows:
            consumers = symbol.get("consumers", [])
            if not isinstance(consumers, list) or not consumers:
                self._errors.append(
                    f"ARC3209 CONTRACT_PLACEMENT_INVALID: {symbol.get('id')} has no consumers."
                )
                continue
            normalized_consumers = sorted({str(value) for value in consumers})
            missing = [value for value in normalized_consumers if value not in modules]
            if missing:
                self._errors.append(
                    f"ARC3209 CONTRACT_PLACEMENT_INVALID: {symbol.get('id')} references "
                    f"unknown consumers {missing}."
                )
                continue
            primary = normalized_consumers[0]
            contracts_by_primary_consumer.setdefault(primary, []).append(symbol)

        for primary in sorted(contracts_by_primary_consumer):
            owner, _, local_name = _parse_module_id(primary) or ("generated", "", "contract")
            path = self._allocate_path(
                "shared/src/contracts",
                _kebab_case(local_name),
                qualifier=owner,
            )
            for symbol in contracts_by_primary_consumer[primary]:
                self._place_symbol(
                    symbol,
                    path,
                    role="DATA_CONTRACT",
                    owner="SKELETON_COMPILER",
                    mutability="FROZEN",
                    origin="DESIGN_IR",
                )

    def _plan_module_files(
        self,
        symbols: dict[str, dict[str, Any]],
        modules: dict[str, dict[str, Any]],
        bindings: dict[str, dict[str, Any]],
    ) -> None:
        roots = {
            "API": "backend/src/api",
            "FUNC": "backend/src/functions",
            "DB": "backend/src/db/repositories",
        }
        roles = {
            "API": "API_MODULE",
            "FUNC": "FUNCTION_MODULE",
            "DB": "DATABASE_MODULE",
        }
        for module_id in sorted(modules):
            owner, module_kind, local_name = _parse_module_id(module_id) or (
                "generated",
                "FUNC",
                "module",
            )
            path = self._allocate_path(
                roots[module_kind],
                _kebab_case(local_name),
                qualifier=owner,
            )
            symbol = symbols[module_id]
            self._place_symbol(
                symbol,
                path,
                role=roles[module_kind],
                owner="IMPLEMENTATION",
                mutability="BODY_ONLY",
                origin="DESIGN_IR",
                module_id=module_id,
            )
            binding = bindings[module_id]
            self._module_locations[module_id] = {
                "module_id": module_id,
                "module_kind": module_kind,
                "path": path,
                "function_symbol": str(binding["function_symbol"]),
                "input_contract_id": binding.get("input_contract_id"),
                "output_contract_id": binding.get("output_contract_id"),
            }

    def _allocate_path(self, directory: str, stem: str, *, qualifier: str) -> str:
        normalized_stem = stem or "generated"
        candidate = f"{directory}/{normalized_stem}.ts"
        if candidate not in self._allocated_paths:
            self._allocated_paths.add(candidate)
            return candidate
        suffix = _kebab_case(qualifier) or "generated"
        candidate = f"{directory}/{normalized_stem}-{suffix}.ts"
        index = 2
        while candidate in self._allocated_paths:
            candidate = f"{directory}/{normalized_stem}-{suffix}-{index}.ts"
            index += 1
        self._allocated_paths.add(candidate)
        return candidate

    def _place_symbol(
        self,
        symbol: dict[str, Any],
        path: str,
        *,
        role: str,
        owner: str,
        mutability: str,
        origin: str,
        module_id: str | None = None,
    ) -> None:
        symbol_id = str(symbol["id"])
        if symbol_id in self._symbol_locations:
            self._errors.append(
                f"ARC3210 SYMBOL_PLACEMENT_CONFLICT: {symbol_id} has more than one file."
            )
            return
        file_record = self._add_file(
            path,
            role=role,
            owner=owner,
            mutability=mutability,
            origin=origin,
        )
        if file_record is None:
            return
        file_record["symbol_ids"].append(symbol_id)
        file_record["exports"].append(str(symbol["symbol"]))
        if module_id is not None:
            file_record["module_ids"].append(module_id)
        self._symbol_locations[symbol_id] = {
            "symbol_id": symbol_id,
            "symbol": str(symbol["symbol"]),
            "typescript_kind": str(symbol.get("typescript_kind", "")),
            "path": path,
        }

    def _add_file(
        self,
        path: str,
        *,
        role: str,
        owner: str,
        mutability: str,
        origin: str,
    ) -> dict[str, Any] | None:
        normalized = _normalize_relative_path(path)
        if normalized is None or not self._is_allowed_path(normalized):
            self._errors.append(
                f"ARC3211 FILE_PATH_INVALID: planned path is outside skeleton roots: {path!r}."
            )
            return None
        existing = self._files.get(normalized)
        if existing is not None:
            expected = (role, owner, mutability, origin)
            actual = (
                existing["role"],
                existing["owner"],
                existing["mutability"],
                existing["origin"],
            )
            if actual != expected:
                self._errors.append(
                    f"ARC3212 FILE_ROLE_CONFLICT: {normalized} has incompatible roles."
                )
                return None
            return existing
        record = {
            "path": normalized,
            "role": role,
            "owner": owner,
            "mutability": mutability,
            "origin": origin,
            "symbol_ids": [],
            "module_ids": [],
            "exports": [],
        }
        self._files[normalized] = record
        self._allocated_paths.add(normalized)
        return record

    def _is_allowed_path(self, path: str) -> bool:
        return any(path == root or path.startswith(f"{root}/") for root in self._allowed_roots)

    def _validate_complete_placement(self, symbols: dict[str, dict[str, Any]]) -> None:
        planned = set(self._symbol_locations)
        expected = set(symbols)
        if planned != expected:
            self._errors.append(
                "ARC3213 FILE_COVERAGE_INVALID: not every symbol has exactly one file: "
                f"missing={sorted(expected - planned)}, unknown={sorted(planned - expected)}."
            )

    def _result(self) -> FilePlanningResult:
        files = []
        for path in sorted(self._files):
            record = copy.deepcopy(self._files[path])
            record["symbol_ids"].sort()
            record["module_ids"].sort()
            record["exports"].sort()
            files.append(record)
        registry = {
            "schema_version": FILE_REGISTRY_SCHEMA_VERSION,
            "status": FILES_PLANNED if not self._errors else "FILE_PLANNING_FAILED",
            "allowed_output_roots": list(self._allowed_roots),
            "directories": sorted({str(PurePosixPath(path).parent) for path in self._files}),
            "files": files,
            "symbol_locations": [
                copy.deepcopy(self._symbol_locations[key])
                for key in sorted(self._symbol_locations)
            ],
            "module_locations": [
                copy.deepcopy(self._module_locations[key])
                for key in sorted(self._module_locations)
            ],
        }
        return FilePlanningResult(registry=registry, errors=list(dict.fromkeys(self._errors)))


def _parse_module_id(module_id: str) -> tuple[str, str, str] | None:
    owner, separator, tail = module_id.partition("::")
    kind, dot, name = tail.partition(".")
    if not separator or not dot or not owner or kind not in MODULE_KINDS or not name:
        return None
    return owner, kind, name


def _kebab_case(value: str) -> str:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    words = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", normalized) if word]
    return "-".join(words) or "generated"


def _normalize_relative_path(value: Any) -> str | None:
    text = str(value).replace("\\", "/").strip().strip("/")
    if not text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        return None
    return path.as_posix()
