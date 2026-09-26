"""Deterministic Frontend Design IR to React/TypeScript skeleton lowering."""

from __future__ import annotations

import copy
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

FRONTEND_SYMBOL_REGISTRY_SCHEMA_VERSION = 2
FRONTEND_FILE_REGISTRY_SCHEMA_VERSION = 2
FRONTEND_MANIFEST_SCHEMA_VERSION = 2
FRONTEND_ROUTE_REGISTRY_SCHEMA_VERSION = 2
FRONTEND_IMPORT_PLAN_SCHEMA_VERSION = 2

FRONTEND_SYMBOLS_PLANNED = "FRONTEND_SYMBOLS_PLANNED"
FRONTEND_FILES_PLANNED = "FRONTEND_FILES_PLANNED"
FRONTEND_MANIFEST_GENERATED = "FRONTEND_MANIFEST_GENERATED"

_UI_TABLES = {
    "LAYOUT": "layouts",
    "PAGE": "pages",
    "COMPONENT": "components",
}

_SYSTEM_FILES = (
    ("frontend/vite.config.ts", "VITE_RUNTIME_CONFIG"),
    ("frontend/src/App.tsx", "APP_SHELL"),
    ("frontend/src/app/router.tsx", "ROUTER"),
    ("frontend/src/app/index.ts", "APP_BARREL"),
    ("frontend/src/app/stores/index.ts", "STORE_BARREL"),
    ("frontend/src/api/http.ts", "HTTP_RUNTIME"),
    ("frontend/src/runtime/store.ts", "STORE_RUNTIME"),
    ("frontend/src/api/index.ts", "API_BARREL"),
    ("frontend/src/components/index.ts", "COMPONENT_BARREL"),
    ("frontend/src/pages/index.ts", "PAGE_BARREL"),
    ("frontend/src/pages/system-main-page.tsx", "SYSTEM_MAIN_PAGE"),
)


@dataclass(slots=True)
class FrontendSymbolPlanningResult:
    registry: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class FrontendFilePlanningResult:
    registry: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class FrontendLoweringResult:
    route_registry: dict[str, Any]
    import_plan: dict[str, Any]
    manifest: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class FrontendGlobalSymbolPlanner:
    """Allocate every Frontend TypeScript symbol through one planning interface."""

    def __init__(self) -> None:
        self._symbols: dict[str, dict[str, Any]] = {}
        self._used_names: dict[str, str] = {}
        self._ui_bindings: dict[str, dict[str, Any]] = {}
        self._store_bindings: dict[str, dict[str, Any]] = {}
        self._api_client_bindings: dict[str, dict[str, Any]] = {}
        self._errors: list[str] = []

    def plan(
        self,
        frontend_ir: dict[str, Any],
        api_contracts: list[dict[str, Any]],
        backend_symbol_registry: dict[str, Any],
        project_manifest: dict[str, Any],
    ) -> FrontendSymbolPlanningResult:
        self._reset()
        self._validate_project_manifest(project_manifest)
        api_contract_index = _index_api_contracts(api_contracts, self._errors)
        backend_symbols = _index_backend_symbols(backend_symbol_registry, self._errors)
        backend_bindings = _index_backend_bindings(backend_symbol_registry, self._errors)

        for kind, table_name in _UI_TABLES.items():
            for item in _object_rows(frontend_ir.get(table_name), table_name, self._errors):
                self._plan_ui_symbol(kind, item)

        for store in _object_rows(frontend_ir.get("stores"), "stores", self._errors):
            self._plan_store(store)

        for api_id in _frontend_api_ids(frontend_ir):
            api = api_contract_index.get(api_id)
            binding = backend_bindings.get(api_id)
            if api is None or binding is None:
                self._errors.append(
                    f"ARC4205 FRONTEND_API_UNKNOWN: Frontend references unavailable API {api_id}."
                )
                continue
            self._plan_api_client(api, binding, backend_symbols)

        return self._result()

    def _reset(self) -> None:
        self._symbols.clear()
        self._used_names.clear()
        self._ui_bindings.clear()
        self._store_bindings.clear()
        self._api_client_bindings.clear()
        self._errors.clear()

    def _validate_project_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict) or manifest.get("status") != "PROJECT_INITIALIZED":
            self._errors.append(
                "ARC4201 PROJECT_NOT_INITIALIZED: Frontend lowering requires project initialization."
            )
            return
        allowed = manifest.get("allowedOutputRoots", {}).get("frontendSkeleton", [])
        required = {
            "frontend/src/App.tsx",
            "frontend/src/app",
            "frontend/src/api",
            "frontend/src/components",
            "frontend/src/pages",
            "frontend/src/runtime",
        }
        if not isinstance(allowed, list) or not required <= {str(value) for value in allowed}:
            self._errors.append(
                "ARC4202 PROJECT_MANIFEST_INVALID: frontendSkeleton roots are incomplete."
            )

    def _plan_ui_symbol(self, kind: str, item: dict[str, Any]) -> None:
        symbol_id = str(item.get("id", "")).strip()
        expected_prefix = f"{kind}."
        if not symbol_id.startswith(expected_prefix) or symbol_id in self._ui_bindings:
            self._errors.append(
                f"ARC4203 FRONTEND_SYMBOL_INVALID: invalid or duplicate {kind} id {symbol_id!r}."
            )
            return
        base = _pascal_case(symbol_id.split(".", 1)[1]) or kind.title()
        function_symbol = self._register(
            symbol_id,
            kind,
            "function",
            base,
            owner_id=symbol_id,
        )
        props_id = f"PROPS::{symbol_id}"
        fields = copy.deepcopy(item.get("route_inputs", [])) if kind == "PAGE" else (
            copy.deepcopy(item.get("inputs", [])) if kind == "COMPONENT" else []
        )
        events = copy.deepcopy(item.get("events", [])) if kind == "COMPONENT" else []
        props_symbol = self._register(
            props_id,
            "PROPS",
            "interface",
            f"{function_symbol}Props",
            owner_id=symbol_id,
            fields=fields,
            events=events,
            includes_children=kind == "LAYOUT",
        )
        self._ui_bindings[symbol_id] = {
            "ui_id": symbol_id,
            "ui_kind": kind,
            "function_symbol": function_symbol,
            "props_symbol_id": props_id,
            "props_symbol": props_symbol,
        }

    def _plan_store(self, store: dict[str, Any]) -> None:
        store_id = str(store.get("id", "")).strip()
        if not store_id.startswith("STORE.") or store_id in self._store_bindings:
            self._errors.append(
                f"ARC4203 FRONTEND_SYMBOL_INVALID: invalid or duplicate Store id {store_id!r}."
            )
            return
        base = _pascal_case(store_id.split(".", 1)[1]) or "Store"
        state_id = f"STORE_STATE::{store_id}"
        actions_id = f"STORE_ACTIONS::{store_id}"
        value_id = f"STORE_VALUE::{store_id}"
        initial_id = f"STORE_INITIAL::{store_id}"
        runtime_id = f"STORE_RUNTIME::{store_id}"
        state_symbol = self._register(
            state_id,
            "STORE_STATE",
            "interface",
            f"{base}State",
            owner_id=store_id,
            fields=copy.deepcopy(store.get("state", [])),
        )
        actions_symbol = self._register(
            actions_id,
            "STORE_ACTIONS",
            "interface",
            f"{base}Actions",
            owner_id=store_id,
            actions=copy.deepcopy(store.get("actions", [])),
        )
        value_symbol = self._register(
            value_id,
            "STORE_VALUE",
            "interface",
            base,
            owner_id=store_id,
        )
        initial_symbol = self._register(
            initial_id,
            "STORE_INITIAL",
            "const",
            f"initial{base}State",
            owner_id=store_id,
        )
        runtime_symbol = self._register(
            runtime_id,
            "STORE_RUNTIME",
            "const",
            f"{base[:1].lower() + base[1:]}Store",
            owner_id=store_id,
        )
        self._store_bindings[store_id] = {
            "store_id": store_id,
            "state_symbol_id": state_id,
            "state_symbol": state_symbol,
            "actions_symbol_id": actions_id,
            "actions_symbol": actions_symbol,
            "value_symbol_id": value_id,
            "value_symbol": value_symbol,
            "initial_symbol_id": initial_id,
            "initial_symbol": initial_symbol,
            "runtime_symbol_id": runtime_id,
            "runtime_symbol": runtime_symbol,
        }

    def _plan_api_client(
        self,
        api: dict[str, Any],
        binding: dict[str, Any],
        backend_symbols: dict[str, dict[str, Any]],
    ) -> None:
        api_id = str(api["id"])
        client_id = f"API_CLIENT::{api_id}"
        local_name = api_id.split("::", 1)[-1].split(".", 1)[-1]
        client_symbol = self._register(
            client_id,
            "API_CLIENT",
            "function",
            _camel_case(local_name),
            owner_id=api_id,
        )
        input_id = binding.get("input_contract_id")
        output_id = binding.get("output_contract_id")
        input_symbol = backend_symbols.get(str(input_id), {}).get("symbol") if input_id else None
        output_symbol = backend_symbols.get(str(output_id), {}).get("symbol") if output_id else None
        if input_id and not input_symbol:
            self._errors.append(
                f"ARC4206 FRONTEND_API_CONTRACT_UNKNOWN: {api_id} input contract is unavailable."
            )
        if output_id and not output_symbol:
            self._errors.append(
                f"ARC4206 FRONTEND_API_CONTRACT_UNKNOWN: {api_id} output contract is unavailable."
            )
        self._api_client_bindings[api_id] = {
            "api_id": api_id,
            "client_symbol_id": client_id,
            "client_symbol": client_symbol,
            "input_contract_id": input_id,
            "input_type_symbol": input_symbol,
            "output_contract_id": output_id,
            "output_type_symbol": output_symbol,
        }

    def _register(
        self,
        symbol_id: str,
        kind: str,
        typescript_kind: str,
        base_name: str,
        **metadata: Any,
    ) -> str:
        if symbol_id in self._symbols:
            self._errors.append(
                f"ARC4203 FRONTEND_SYMBOL_INVALID: duplicate symbol id {symbol_id}."
            )
            return str(self._symbols[symbol_id].get("symbol", ""))
        base = _safe_identifier(base_name)
        candidate = base
        index = 2
        while candidate in self._used_names and self._used_names[candidate] != symbol_id:
            candidate = f"{base}{index}"
            index += 1
        self._used_names[candidate] = symbol_id
        self._symbols[symbol_id] = {
            "id": symbol_id,
            "kind": kind,
            "typescript_kind": typescript_kind,
            "symbol": candidate,
            **metadata,
        }
        return candidate

    def _result(self) -> FrontendSymbolPlanningResult:
        registry = {
            "schema_version": FRONTEND_SYMBOL_REGISTRY_SCHEMA_VERSION,
            "status": FRONTEND_SYMBOLS_PLANNED if not self._errors else "FRONTEND_SYMBOL_PLANNING_FAILED",
            "symbols": [copy.deepcopy(self._symbols[key]) for key in sorted(self._symbols)],
            "ui_bindings": [copy.deepcopy(self._ui_bindings[key]) for key in sorted(self._ui_bindings)],
            "store_bindings": [
                copy.deepcopy(self._store_bindings[key]) for key in sorted(self._store_bindings)
            ],
            "api_client_bindings": [
                copy.deepcopy(self._api_client_bindings[key])
                for key in sorted(self._api_client_bindings)
            ],
        }
        return FrontendSymbolPlanningResult(
            registry=registry,
            errors=list(dict.fromkeys(self._errors)),
        )


class FrontendFilePlanner:
    """Assign all Frontend symbols to compiler-owned source files."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root.expanduser().resolve()
        self._allowed_roots: tuple[str, ...] = ()
        self._files: dict[str, dict[str, Any]] = {}
        self._symbol_locations: dict[str, dict[str, Any]] = {}
        self._ui_locations: dict[str, dict[str, Any]] = {}
        self._store_locations: dict[str, dict[str, Any]] = {}
        self._api_locations: dict[str, dict[str, Any]] = {}
        self._allocated_paths: set[str] = set()
        self._errors: list[str] = []

    def plan(
        self,
        frontend_ir: dict[str, Any],
        symbol_registry: dict[str, Any],
        project_manifest: dict[str, Any],
    ) -> FrontendFilePlanningResult:
        self._reset()
        self._load_allowed_roots(project_manifest)
        symbols = _index_rows(symbol_registry.get("symbols"), "id", "symbols", self._errors)
        ui_bindings = _index_rows(
            symbol_registry.get("ui_bindings"), "ui_id", "ui_bindings", self._errors
        )
        store_bindings = _index_rows(
            symbol_registry.get("store_bindings"), "store_id", "store_bindings", self._errors
        )
        api_bindings = _index_rows(
            symbol_registry.get("api_client_bindings"),
            "api_id",
            "api_client_bindings",
            self._errors,
        )
        if symbol_registry.get("status") != FRONTEND_SYMBOLS_PLANNED:
            self._errors.append(
                "ARC4211 FRONTEND_SYMBOL_REGISTRY_INVALID: symbol planning has not completed."
            )
        if self._errors:
            return self._result()

        for path, role in _SYSTEM_FILES:
            self._add_file(path, role, "FROZEN", "SYSTEM")

        ui_items = {
            str(item["id"]): item
            for table_name in _UI_TABLES.values()
            for item in _object_rows(frontend_ir.get(table_name), table_name, self._errors)
        }
        for ui_id, binding in sorted(ui_bindings.items()):
            item = ui_items.get(ui_id)
            if item is None:
                self._errors.append(f"ARC4212 FRONTEND_SYMBOL_COVERAGE_INVALID: missing {ui_id}.")
                continue
            kind = str(binding.get("ui_kind", ""))
            root = "frontend/src/pages" if kind == "PAGE" else "frontend/src/components"
            stem = _kebab_case(ui_id.split(".", 1)[-1])
            path = self._allocate_path(root, stem, ui_id, ".tsx")
            self._place_symbol(str(binding["ui_id"]), symbols, path, f"{kind}_SKELETON", "BODY_ONLY")
            self._place_symbol(str(binding["props_symbol_id"]), symbols, path, f"{kind}_SKELETON", "BODY_ONLY")
            self._ui_locations[ui_id] = {**copy.deepcopy(binding), "path": path}

        for store_id, binding in sorted(store_bindings.items()):
            path = self._allocate_path(
                "frontend/src/app/stores",
                _kebab_case(store_id.split(".", 1)[-1]),
                store_id,
                ".ts",
            )
            for key in (
                "state_symbol_id",
                "actions_symbol_id",
                "value_symbol_id",
                "initial_symbol_id",
                "runtime_symbol_id",
            ):
                self._place_symbol(str(binding[key]), symbols, path, "STORE_SKELETON", "BODY_ONLY")
            self._store_locations[store_id] = {**copy.deepcopy(binding), "path": path}

        for api_id, binding in sorted(api_bindings.items()):
            path = self._allocate_path(
                "frontend/src/api",
                _kebab_case(api_id.split("::", 1)[-1].split(".", 1)[-1]),
                api_id,
                ".ts",
            )
            self._place_symbol(
                str(binding["client_symbol_id"]),
                symbols,
                path,
                "API_CLIENT",
                "FROZEN",
            )
            self._api_locations[api_id] = {**copy.deepcopy(binding), "path": path}

        if set(self._symbol_locations) != set(symbols):
            self._errors.append(
                "ARC4212 FRONTEND_SYMBOL_COVERAGE_INVALID: not every symbol has one file: "
                f"missing={sorted(set(symbols) - set(self._symbol_locations))}."
            )
        return self._result()

    def _reset(self) -> None:
        self._allowed_roots = ()
        self._files.clear()
        self._symbol_locations.clear()
        self._ui_locations.clear()
        self._store_locations.clear()
        self._api_locations.clear()
        self._allocated_paths.clear()
        self._errors.clear()

    def _load_allowed_roots(self, manifest: Any) -> None:
        if not isinstance(manifest, dict) or manifest.get("status") != "PROJECT_INITIALIZED":
            self._errors.append("ARC4210 PROJECT_NOT_INITIALIZED: Frontend file planning is unavailable.")
            return
        roots = manifest.get("allowedOutputRoots", {}).get("frontendSkeleton", [])
        if not isinstance(roots, list):
            self._errors.append("ARC4210 PROJECT_MANIFEST_INVALID: frontendSkeleton roots must be a list.")
            return
        normalized: list[str] = []
        for value in roots:
            path = _normalize_relative_path(value)
            if path is None:
                self._errors.append(f"ARC4210 PROJECT_MANIFEST_INVALID: invalid root {value!r}.")
                continue
            target = (self.output_root / Path(path)).resolve()
            if not target.exists() or self.output_root not in target.parents:
                self._errors.append(f"ARC4210 PROJECT_MANIFEST_INVALID: unavailable root {path}.")
                continue
            normalized.append(path)
        self._allowed_roots = tuple(sorted(set(normalized)))

    def _allocate_path(
        self,
        root: str,
        stem: str,
        qualifier: str,
        extension: str,
    ) -> str:
        candidate = f"{root}/{stem or 'generated'}{extension}"
        if candidate not in self._allocated_paths:
            return candidate
        suffix = _kebab_case(qualifier) or "generated"
        candidate = f"{root}/{stem}-{suffix}{extension}"
        index = 2
        while candidate in self._allocated_paths:
            candidate = f"{root}/{stem}-{suffix}-{index}{extension}"
            index += 1
        return candidate

    def _place_symbol(
        self,
        symbol_id: str,
        symbols: dict[str, dict[str, Any]],
        path: str,
        role: str,
        mutability: str,
    ) -> None:
        symbol = symbols.get(symbol_id)
        if symbol is None or symbol_id in self._symbol_locations:
            self._errors.append(
                f"ARC4212 FRONTEND_SYMBOL_COVERAGE_INVALID: cannot place {symbol_id}."
            )
            return
        file_row = self._add_file(path, role, mutability, "FRONTEND_DESIGN_IR")
        if file_row is None:
            return
        file_row["symbol_ids"].append(symbol_id)
        file_row["exports"].append(str(symbol["symbol"]))
        self._symbol_locations[symbol_id] = {
            "symbol_id": symbol_id,
            "symbol": str(symbol["symbol"]),
            "typescript_kind": str(symbol.get("typescript_kind", "")),
            "path": path,
        }

    def _add_file(
        self,
        path: str,
        role: str,
        mutability: str,
        origin: str,
    ) -> dict[str, Any] | None:
        normalized = _normalize_relative_path(path)
        if normalized is None or not self._is_allowed(normalized):
            self._errors.append(
                f"ARC4214 FRONTEND_FILE_PATH_INVALID: path is outside frontend roots: {path!r}."
            )
            return None
        existing = self._files.get(normalized)
        if existing is not None:
            return existing
        row = {
            "path": normalized,
            "role": role,
            "owner": "FRONTEND_SKELETON_COMPILER",
            "mutability": mutability,
            "origin": origin,
            "symbol_ids": [],
            "exports": [],
        }
        self._files[normalized] = row
        self._allocated_paths.add(normalized)
        return row

    def _is_allowed(self, path: str) -> bool:
        return any(path == root or path.startswith(f"{root}/") for root in self._allowed_roots)

    def _result(self) -> FrontendFilePlanningResult:
        files: list[dict[str, Any]] = []
        for path in sorted(self._files):
            row = copy.deepcopy(self._files[path])
            row["symbol_ids"] = sorted(set(row["symbol_ids"]))
            row["exports"] = sorted(set(row["exports"]))
            files.append(row)
        registry = {
            "schema_version": FRONTEND_FILE_REGISTRY_SCHEMA_VERSION,
            "status": FRONTEND_FILES_PLANNED if not self._errors else "FRONTEND_FILE_PLANNING_FAILED",
            "allowed_output_roots": list(self._allowed_roots),
            "directories": sorted({str(PurePosixPath(path).parent) for path in self._files}),
            "files": files,
            "symbol_locations": [
                copy.deepcopy(self._symbol_locations[key]) for key in sorted(self._symbol_locations)
            ],
            "ui_locations": [copy.deepcopy(self._ui_locations[key]) for key in sorted(self._ui_locations)],
            "store_locations": [
                copy.deepcopy(self._store_locations[key]) for key in sorted(self._store_locations)
            ],
            "api_client_locations": [
                copy.deepcopy(self._api_locations[key]) for key in sorted(self._api_locations)
            ],
        }
        return FrontendFilePlanningResult(registry=registry, errors=list(dict.fromkeys(self._errors)))


class FrontendSkeletonLowerer:
    """Lower all planned Frontend interfaces and integration into skeleton sources."""

    def lower(
        self,
        frontend_ir: dict[str, Any],
        api_contracts: list[dict[str, Any]],
        backend_route_registry: dict[str, Any],
        symbol_registry: dict[str, Any],
        file_registry: dict[str, Any],
        *,
        backend_port: int,
    ) -> FrontendLoweringResult:
        errors: list[str] = []
        if symbol_registry.get("status") != FRONTEND_SYMBOLS_PLANNED:
            errors.append("ARC4221 FRONTEND_SYMBOL_REGISTRY_INVALID: symbols are not planned.")
        if file_registry.get("status") != FRONTEND_FILES_PLANNED:
            errors.append("ARC4222 FRONTEND_FILE_REGISTRY_INVALID: files are not planned.")

        symbols = _index_rows(symbol_registry.get("symbols"), "id", "symbols", errors)
        ui_locations = _index_rows(file_registry.get("ui_locations"), "ui_id", "ui_locations", errors)
        store_locations = _index_rows(
            file_registry.get("store_locations"), "store_id", "store_locations", errors
        )
        api_locations = _index_rows(
            file_registry.get("api_client_locations"),
            "api_id",
            "api_client_locations",
            errors,
        )
        files = _index_rows(file_registry.get("files"), "path", "files", errors)
        api_contract_index = _index_api_contracts(api_contracts, errors)
        backend_routes = _index_backend_routes(backend_route_registry, errors)
        resolved_backend_port = _validated_port(backend_port, errors)

        layouts = _table_by_id(frontend_ir, "layouts")
        pages = _table_by_id(frontend_ir, "pages")
        components = _table_by_id(frontend_ir, "components")
        stores = _table_by_id(frontend_ir, "stores")
        owner_requirements = _frontend_owner_requirements(frontend_ir)

        sources: dict[str, str] = {}
        imports_by_path: dict[str, list[dict[str, Any]]] = {path: [] for path in files}
        exports_by_path: dict[str, list[str]] = {
            path: list(row.get("exports", [])) for path, row in files.items()
        }

        if not errors:
            self._lower_vite_runtime_config(
                resolved_backend_port,
                sources,
                imports_by_path,
                exports_by_path,
            )
            self._lower_http_runtime(sources, imports_by_path, exports_by_path)
            self._lower_store_runtime(sources, imports_by_path, exports_by_path)
            self._lower_api_clients(
                api_locations,
                api_contract_index,
                backend_routes,
                owner_requirements,
                sources,
                imports_by_path,
                exports_by_path,
                errors,
            )
            self._lower_stores(
                stores,
                store_locations,
                owner_requirements,
                sources,
                imports_by_path,
                exports_by_path,
            )
            self._lower_ui_modules(
                layouts,
                pages,
                components,
                ui_locations,
                store_locations,
                api_locations,
                owner_requirements,
                sources,
                imports_by_path,
                exports_by_path,
                errors,
            )
            self._lower_system_main_page(
                stores,
                store_locations,
                sources,
                imports_by_path,
                exports_by_path,
            )

        frontend_routes = self._lower_router(
            pages,
            layouts,
            ui_locations,
            sources,
            imports_by_path,
            exports_by_path,
            errors,
        ) if not errors else []
        if not errors:
            self._lower_barrels(
                ui_locations,
                store_locations,
                api_locations,
                sources,
                exports_by_path,
            )

        planned = set(files)
        generated = set(sources)
        if not errors and planned != generated:
            errors.append(
                "ARC4223 FRONTEND_FILE_COVERAGE_INVALID: planned and generated files differ: "
                f"missing={sorted(planned - generated)}, unknown={sorted(generated - planned)}."
            )

        import_plan = {
            "schema_version": FRONTEND_IMPORT_PLAN_SCHEMA_VERSION,
            "status": "FRONTEND_IMPORTS_PLANNED" if not errors else "FRONTEND_IMPORT_PLANNING_FAILED",
            "files": [
                {
                    "path": path,
                    "imports": _deduplicate_imports(imports_by_path.get(path, [])),
                }
                for path in sorted(files)
            ],
        }
        route_registry = {
            "schema_version": FRONTEND_ROUTE_REGISTRY_SCHEMA_VERSION,
            "status": "FRONTEND_ROUTES_PLANNED" if not errors else "FRONTEND_ROUTE_PLANNING_FAILED",
            "routes": frontend_routes,
        }
        manifest = {
            "schema_version": FRONTEND_MANIFEST_SCHEMA_VERSION,
            "status": FRONTEND_MANIFEST_GENERATED if not errors else "FRONTEND_MANIFEST_FAILED",
            "files": [
                {
                    **copy.deepcopy(files[path]),
                    "imports": _deduplicate_imports(imports_by_path.get(path, [])),
                    "exports": sorted(set(exports_by_path.get(path, []))),
                }
                for path in sorted(files)
            ],
            "routes": copy.deepcopy(frontend_routes),
            "runtime": {
                "backend_port": resolved_backend_port,
                "backend_target": f"http://127.0.0.1:{resolved_backend_port}",
                "proxy_prefix": "/api",
                "vite_config": "frontend/vite.config.ts",
            },
        }
        return FrontendLoweringResult(
            route_registry=route_registry,
            import_plan=import_plan,
            manifest=manifest,
            sources={} if errors else sources,
            errors=list(dict.fromkeys(errors)),
        )

    @staticmethod
    def _lower_vite_runtime_config(
        backend_port: int,
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
    ) -> None:
        path = "frontend/vite.config.ts"
        rows = [
            _import("defineConfig", "vite"),
            _import("react", "@vitejs/plugin-react", import_style="default"),
            _import("tailwindcss", "@tailwindcss/vite", import_style="default"),
        ]
        backend_target = f"http://127.0.0.1:{backend_port}"
        sources[path] = (
            "\n".join(_render_imports(rows))
            + "\n\n"
            + f"const backendTarget = {json.dumps(backend_target)};\n"
            + "const apiProxy = { target: backendTarget, changeOrigin: true };\n\n"
            + "export default defineConfig({\n"
            + "  plugins: [react(), tailwindcss()],\n"
            + "  server: { proxy: { \"/api\": apiProxy } },\n"
            + "  preview: { proxy: { \"/api\": apiProxy } },\n"
            + "});\n"
        )
        imports[path] = rows
        exports[path] = ["default"]

    @staticmethod
    def _lower_http_runtime(
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
    ) -> None:
        path = "frontend/src/api/http.ts"
        rows = [_import("ApiErrorBody", "@arc/shared", type_only=True)]
        sources[path] = (
            "\n".join(_render_imports(rows))
            + "\n\n"
            + "export class ApiClientError extends Error {\n"
            "  readonly status: number;\n"
            "  readonly code: string;\n"
            "  readonly details: unknown;\n\n"
            "  constructor(status: number, code: string, message: string, details?: unknown) {\n"
            "    super(message);\n"
            "    this.status = status;\n"
            "    this.code = code;\n"
            "    this.details = details;\n"
            "    this.name = \"ApiClientError\";\n"
            "  }\n"
            "}\n\n"
            "function isApiErrorBody(value: unknown): value is ApiErrorBody {\n"
            '  if (typeof value !== "object" || value === null) return false;\n'
            "  const error = (value as { error?: unknown }).error;\n"
            '  if (typeof error !== "object" || error === null) return false;\n'
            "  const detail = error as { code?: unknown; message?: unknown };\n"
            '  return typeof detail.code === "string" && typeof detail.message === "string";\n'
            "}\n\n"
            "/** Turn any failed response into one ApiClientError carrying the server code. */\n"
            "function toClientError(status: number, body: string): ApiClientError {\n"
            "  try {\n"
            "    const parsed: unknown = body ? JSON.parse(body) : null;\n"
            "    if (isApiErrorBody(parsed)) {\n"
            "      return new ApiClientError(\n"
            "        status,\n"
            "        parsed.error.code,\n"
            "        parsed.error.message,\n"
            "        parsed.error.details,\n"
            "      );\n"
            "    }\n"
            "  } catch {\n"
            "    // Not JSON: fall through to the raw body below.\n"
            "  }\n"
            '  return new ApiClientError(status, "HTTP_ERROR", body || `Request failed with ${status}`);\n'
            "}\n\n"
            "export function toQueryString(value: unknown): string {\n"
            "  const params = new URLSearchParams();\n"
            "  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {\n"
            "    if (item !== undefined && item !== null) params.set(key, String(item));\n"
            "  }\n"
            "  const query = params.toString();\n"
            "  return query ? `?${query}` : \"\";\n"
            "}\n\n"
            "export async function requestJson<T>(path: string, init: RequestInit): Promise<T> {\n"
            "  const response = await fetch(path, init);\n"
            "  if (!response.ok) throw toClientError(response.status, await response.text());\n"
            "  const text = await response.text();\n"
            "  if (!text) return undefined as T;\n"
            "  return JSON.parse(text) as T;\n"
            "}\n"
        )
        imports[path] = rows
        exports[path] = ["ApiClientError", "requestJson", "toQueryString"]

    @staticmethod
    def _lower_store_runtime(
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
    ) -> None:
        """Own reactivity in the compiler so stores are not hand-rolled per app.

        Every generated store is created through ``createStore`` and read through
        ``useStore``/``useStoreState``; the implementation region only has to
        describe how actions change state.
        """

        path = "frontend/src/runtime/store.ts"
        rows = [
            _import("useCallback", "react"),
            _import("useSyncExternalStore", "react"),
        ]
        sources[path] = (
            "\n".join(_render_imports(rows))
            + "\n\n"
            + "export type StoreUpdate<TState> = TState | ((current: TState) => TState);\n\n"
            "export interface Store<TState, TActions> {\n"
            "  readonly actions: TActions;\n"
            "  getState(): TState;\n"
            "  setState(update: StoreUpdate<TState>): void;\n"
            "  subscribe(listener: () => void): () => void;\n"
            "}\n\n"
            "export interface StoreHelpers<TState> {\n"
            "  getState(): TState;\n"
            "  setState(update: StoreUpdate<TState>): void;\n"
            "}\n\n"
            "/** Build one observable store. Listeners run whenever state identity changes. */\n"
            "export function createStore<TState, TActions>(\n"
            "  initialState: TState,\n"
            "  createActions: (helpers: StoreHelpers<TState>) => TActions,\n"
            "): Store<TState, TActions> {\n"
            "  let currentState = initialState;\n"
            "  const listeners = new Set<() => void>();\n"
            "  const getState = (): TState => currentState;\n"
            "  const setState = (update: StoreUpdate<TState>): void => {\n"
            "    const next =\n"
            '      typeof update === "function"\n'
            "        ? (update as (current: TState) => TState)(currentState)\n"
            "        : update;\n"
            "    if (Object.is(next, currentState)) return;\n"
            "    currentState = next;\n"
            "    for (const listener of [...listeners]) listener();\n"
            "  };\n"
            "  const subscribe = (listener: () => void): (() => void) => {\n"
            "    listeners.add(listener);\n"
            "    return () => {\n"
            "      listeners.delete(listener);\n"
            "    };\n"
            "  };\n"
            "  return {\n"
            "    actions: createActions({ getState, setState }),\n"
            "    getState,\n"
            "    setState,\n"
            "    subscribe,\n"
            "  };\n"
            "}\n\n"
            "/** Read the whole store state and re-render when it changes. */\n"
            "export function useStoreState<TState, TActions>(\n"
            "  store: Store<TState, TActions>,\n"
            "): TState {\n"
            "  return useSyncExternalStore(store.subscribe, store.getState, store.getState);\n"
            "}\n\n"
            "/**\n"
            " * Read one slice of a store. The selector must return a primitive or a value\n"
            " * that keeps its identity between renders, otherwise React re-renders forever.\n"
            " */\n"
            "export function useStore<TState, TActions, TSelected>(\n"
            "  store: Store<TState, TActions>,\n"
            "  select: (state: TState) => TSelected,\n"
            "): TSelected {\n"
            "  const snapshot = useCallback(\n"
            "    () => select(store.getState()),\n"
            "    [store, select],\n"
            "  );\n"
            "  return useSyncExternalStore(store.subscribe, snapshot, snapshot);\n"
            "}\n\n"
            "/** Persist a store to localStorage under one key. Called by generated stores. */\n"
            "export function persistStore<TState, TActions>(\n"
            "  store: Store<TState, TActions>,\n"
            "  storageKey: string | null,\n"
            "): void {\n"
            '  if (!storageKey || typeof window === "undefined") return;\n'
            "  store.subscribe(() => {\n"
            "    try {\n"
            "      window.localStorage.setItem(storageKey, JSON.stringify(store.getState()));\n"
            "    } catch {\n"
            "      // Storage quota or private browsing: state stays in memory.\n"
            "    }\n"
            "  });\n"
            "}\n"
        )
        imports[path] = rows
        exports[path] = [
            "Store",
            "StoreHelpers",
            "StoreUpdate",
            "createStore",
            "persistStore",
            "useStore",
            "useStoreState",
        ]

    @staticmethod
    def _lower_api_clients(
        locations: dict[str, dict[str, Any]],
        apis: dict[str, dict[str, Any]],
        routes: dict[str, dict[str, Any]],
        owner_requirements: dict[str, list[str]],
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
        errors: list[str],
    ) -> None:
        for api_id, location in sorted(locations.items()):
            api = apis.get(api_id)
            route = routes.get(api_id)
            if api is None or route is None:
                errors.append(
                    f"ARC4224 FRONTEND_API_ROUTE_UNKNOWN: cannot lower API client {api_id}."
                )
                continue
            path = str(location["path"])
            client_symbol = str(location["client_symbol"])
            input_symbol = location.get("input_type_symbol")
            output_symbol = location.get("output_type_symbol")
            method = str(route.get("method", "GET")).upper()
            route_path = str(route.get("path", ""))
            input_source = str(route.get("input_source", "query"))
            rows = [
                _import("requestJson", _relative_specifier(path, "frontend/src/api/http.ts"), source="frontend/src/api/http.ts")
            ]
            if input_symbol and input_source == "query":
                rows.append(
                    _import("toQueryString", _relative_specifier(path, "frontend/src/api/http.ts"), source="frontend/src/api/http.ts")
                )
            for type_symbol in (input_symbol, output_symbol):
                if type_symbol:
                    rows.append(_import(str(type_symbol), "@arc/shared", type_only=True))
            imports[path] = rows
            result_type = str(output_symbol) if output_symbol else "void"
            parameter = f"request: {input_symbol}" if input_symbol else ""
            if input_symbol and input_source == "query":
                target = f"`{route_path}${{toQueryString(request)}}`"
                init = f'{{ method: "{method}" }}'
            elif input_symbol:
                target = json.dumps(route_path)
                init = (
                    f'{{ method: "{method}", headers: {{ "content-type": "application/json" }}, '
                    "body: JSON.stringify(request) }"
                )
            else:
                target = json.dumps(route_path)
                init = f'{{ method: "{method}" }}'
            sources[path] = (
                "/**\n"
                + f" * @arc-requirements {','.join(owner_requirements.get(api_id, []))}\n"
                + " */\n"
                + "\n".join(_render_imports(rows))
                + "\n\n"
                + f"export async function {client_symbol}({parameter}): Promise<{result_type}> {{\n"
                + f"  return requestJson<{result_type}>({target}, {init});\n"
                + "}\n"
            )
            exports[path] = [client_symbol]

    @staticmethod
    def _lower_stores(
        stores: dict[str, dict[str, Any]],
        locations: dict[str, dict[str, Any]],
        owner_requirements: dict[str, list[str]],
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
    ) -> None:
        for store_id, store in sorted(stores.items()):
            location = locations[store_id]
            path = str(location["path"])
            state_symbol = str(location["state_symbol"])
            actions_symbol = str(location["actions_symbol"])
            value_symbol = str(location["value_symbol"])
            initial_symbol = str(location["initial_symbol"])
            runtime_symbol = str(location["runtime_symbol"])
            state_fields = _render_fields(store.get("state", []))
            action_lines = []
            for action in store.get("actions", []):
                if not isinstance(action, dict):
                    continue
                name = _safe_property(str(action.get("name", "action")))
                input_type = action.get("input_type")
                parameter = f"input: {_typescript_type(str(input_type))}" if input_type else ""
                action_lines.append(f"  {name}({parameter}): void;")
            initial_lines = []
            for field_item in store.get("state", []):
                if not isinstance(field_item, dict):
                    continue
                name = json.dumps(str(field_item.get("name", "value")))
                initial_lines.append(
                    f"  {name}: {_default_value(str(field_item.get('type', 'unknown')))},"
                )
            persistence = store.get("persistence", {})
            persistence_kind = str(persistence.get("kind", "MEMORY"))
            storage_key = (
                str(persistence.get("storage_key"))
                if persistence_kind == "LOCAL_STORAGE" and persistence.get("storage_key")
                else None
            )
            runtime_path = "frontend/src/runtime/store.ts"
            specifier = _relative_specifier(path, runtime_path)
            rows = [
                _import("createStore", specifier, source=runtime_path),
                _import("persistStore", specifier, source=runtime_path),
                _import("Store", specifier, type_only=True, source=runtime_path),
            ]
            # Default action bodies are plain state transitions expressed through
            # setState, so the store stays reactive no matter what the agent writes.
            action_lines_body: list[str] = []
            for action in store.get("actions", []):
                if not isinstance(action, dict):
                    continue
                name = _safe_property(str(action.get("name", "action")))
                has_input = bool(action.get("input_type"))
                if name.lower().startswith(("clear", "reset", "signout", "logout")):
                    action_lines_body.extend([
                        f"      {name}: () => {{",
                        f"        setState({{ ...{initial_symbol} }});",
                        "      },",
                    ])
                elif has_input:
                    action_lines_body.extend([
                        f"      {name}: (input) => {{",
                        '        const patch = typeof input === "object" && input !== null ? input : {};',
                        "        setState((current) => "
                        f"({{ ...current, ...patch }}) as {state_symbol});",
                        "      },",
                    ])
                else:
                    action_lines_body.append(f"      {name}: () => undefined,")
            runtime_lines = [
                "    void getState;",
                "    void setState;",
                "    return {",
                *(
                    action_lines_body
                    if action_lines_body
                    else ["      // No global actions were designed."]
                ),
                "    };",
            ]
            sources[path] = (
                "/**\n"
                + f" * @arc-requirements {','.join(owner_requirements.get(store_id, []))}\n"
                + " */\n"
                + "\n".join(_render_imports(rows))
                + "\n\n"
                + f"export interface {state_symbol} {{\n{state_fields}\n}}\n\n"
                f"export interface {actions_symbol} {{\n"
                + ("\n".join(action_lines) if action_lines else "  // No global actions were designed.")
                + "\n}\n\n"
                f"export interface {value_symbol} extends Store<{state_symbol}, {actions_symbol}> {{}}\n\n"
                f"export const {initial_symbol}: {state_symbol} = {{\n"
                + ("\n".join(initial_lines) if initial_lines else "  // No global state was designed.")
                + "\n};\n\n"
                + f"const storageKey: string | null = {json.dumps(storage_key)};\n"
                + f"function load{state_symbol}(): {state_symbol} {{\n"
                + "  if (!storageKey || typeof window === \"undefined\") "
                + f"return {{ ...{initial_symbol} }};\n"
                + "  try {\n"
                + "    const value = window.localStorage.getItem(storageKey);\n"
                + f"    return value ? {{ ...{initial_symbol}, ...JSON.parse(value) }} : {{ ...{initial_symbol} }};\n"
                + "  } catch {\n"
                + f"    return {{ ...{initial_symbol} }};\n"
                + "  }\n}\n\n"
                + f"export const {runtime_symbol}: {value_symbol} = "
                + f"createStore<{state_symbol}, {actions_symbol}>(\n"
                + f"  load{state_symbol}(),\n"
                + "  ({ getState, setState }) => {\n"
                + "\n".join(runtime_lines)
                + "  },\n"
                + ");\n\n"
                + f"persistStore({runtime_symbol}, storageKey);\n"
            )
            imports[path] = rows
            exports[path] = [
                state_symbol,
                actions_symbol,
                value_symbol,
                initial_symbol,
                runtime_symbol,
            ]

    @staticmethod
    def _lower_ui_modules(
        layouts: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
        components: dict[str, dict[str, Any]],
        locations: dict[str, dict[str, Any]],
        store_locations: dict[str, dict[str, Any]],
        api_locations: dict[str, dict[str, Any]],
        owner_requirements: dict[str, list[str]],
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
        errors: list[str],
    ) -> None:
        all_items = {**layouts, **pages, **components}
        for ui_id, item in sorted(all_items.items()):
            location = locations.get(ui_id)
            if location is None:
                errors.append(f"ARC4225 FRONTEND_UI_LOCATION_UNKNOWN: no file for {ui_id}.")
                continue
            path = str(location["path"])
            function_symbol = str(location["function_symbol"])
            props_symbol = str(location["props_symbol"])
            kind = str(location["ui_kind"])
            rows: list[dict[str, Any]] = []
            implementation_dependencies: list[str] = []
            props_lines: list[str] = []
            if kind == "LAYOUT":
                rows.append(_import("ReactNode", "react", type_only=True))
                props_lines.append("  children?: ReactNode;")
            else:
                # The hooks a screen realistically needs. Importing them here means
                # the implementation region never has to add a file-level import,
                # which it is not allowed to do.
                react_hooks = (
                    "useCallback",
                    "useEffect",
                    "useMemo",
                    "useRef",
                    "useState",
                )
                rows.extend(_import(hook, "react") for hook in react_hooks)
                implementation_dependencies.extend(react_hooks)
                rows.append(_import("useNavigate", "react-router-dom"))
                implementation_dependencies.append("useNavigate")
            fields = item.get("route_inputs", []) if kind == "PAGE" else item.get("inputs", [])
            props_lines.extend(_render_fields(fields).splitlines() if fields else [])
            if kind == "COMPONENT":
                for event in item.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    event_name = f"on{_pascal_case(str(event.get('name', 'event')))}"
                    payload = event.get("payload_type")
                    result_type = "Promise<void>" if event.get("async") else "void"
                    callback = (
                        f"(payload: {_typescript_type(str(payload))}) => {result_type}"
                        if payload else f"() => {result_type}"
                    )
                    props_lines.append(f"  {event_name}?: {callback};")

            if kind in ("PAGE", "COMPONENT"):
                for api_id in item.get("api_dependencies", []):
                    api_location = api_locations.get(str(api_id))
                    if api_location is not None:
                        client_symbol = str(api_location["client_symbol"])
                        rows.append(_import(
                            client_symbol,
                            _relative_specifier(path, str(api_location["path"])),
                            source=str(api_location["path"]),
                        ))
                        implementation_dependencies.append(client_symbol)
                store_runtime_path = "frontend/src/runtime/store.ts"
                uses_stores = False
                for store_id in item.get("store_dependencies", []):
                    store_location = store_locations.get(str(store_id))
                    if store_location is not None:
                        uses_stores = True
                        runtime_symbol = str(store_location["runtime_symbol"])
                        rows.append(_import(
                            runtime_symbol,
                            _relative_specifier(path, str(store_location["path"])),
                            source=str(store_location["path"]),
                        ))
                        implementation_dependencies.append(runtime_symbol)
                if uses_stores:
                    # Reading a store reactively is the compiler's job, not the agent's.
                    for hook in ("useStore", "useStoreState"):
                        rows.append(_import(
                            hook,
                            _relative_specifier(path, store_runtime_path),
                            source=store_runtime_path,
                        ))
                        implementation_dependencies.append(hook)

            child_ids = [
                str(value) for value in item.get("component_ids", [])
                if str(value) in components
            ]
            child_lines: list[str] = []
            declarations: list[str] = []
            for index, child_id in enumerate(child_ids):
                child_location = locations.get(child_id)
                if child_location is None:
                    errors.append(
                        f"ARC4225 FRONTEND_UI_LOCATION_UNKNOWN: {ui_id} child {child_id} has no file."
                    )
                    continue
                child_symbol = str(child_location["function_symbol"])
                child_props = str(child_location["props_symbol"])
                specifier = _relative_specifier(path, str(child_location["path"]))
                rows.extend([
                    _import(child_symbol, specifier, source=str(child_location["path"])),
                    _import(child_props, specifier, type_only=True, source=str(child_location["path"])),
                ])
                variable = f"childProps{index + 1}"
                available = {
                    str(row.get("semantic_id", "")): str(row.get("name", ""))
                    for row in fields
                    if isinstance(row, dict)
                }
                forwarded = [
                    f"{str(row.get('name', ''))}: _props.{available[str(row.get('semantic_id', ''))]}"
                    for row in components.get(child_id, {}).get("inputs", [])
                    if isinstance(row, dict) and str(row.get("semantic_id", "")) in available
                ]
                literal = "{ " + ", ".join(forwarded) + " }" if forwarded else "{}"
                declarations.append(f"  const {variable} = {literal} as {child_props};")
                child_lines.append(f"      <{child_symbol} {{...{variable}}} />")

            obligations = [
                str(value.get("label", "")).strip()
                for value in item.get("render_obligations", [])
                if isinstance(value, dict) and str(value.get("label", "")).strip()
            ]
            obligation_lines = [
                "      <span "
                f"className=\"rounded-lg border border-slate-200 bg-white/80 px-4 py-3 text-sm "
                f"font-medium text-slate-700 shadow-sm\" data-arc-obligation={{{json.dumps(label)}}} "
                'aria-hidden="true">Implementation pending</span>'
                for label in obligations
            ]
            body_lines = [*obligation_lines, *child_lines]
            if kind == "LAYOUT":
                body_lines.append("      {_props.children}")
            if not body_lines:
                body_lines.append("      <span>Implementation pending</span>")
            tag = "main" if kind == "PAGE" else "section"
            shell_class = (
                "min-h-screen bg-slate-950 px-4 py-8 text-slate-100 sm:px-8"
                if kind == "LAYOUT"
                else "mx-auto flex min-h-[60vh] w-full max-w-6xl flex-col gap-6 rounded-3xl "
                "bg-slate-50 p-6 text-slate-900 shadow-xl ring-1 ring-slate-200 sm:p-10"
            )
            default_body = (
                ("\n".join(declarations) + "\n" if declarations else "")
                + "  return (\n"
                + f"    <{tag} className={json.dumps(shell_class)} data-arc-{kind.lower()}={{{json.dumps(ui_id)}}}>\n"
                + "\n".join(body_lines)
                + f"\n    </{tag}>\n"
                + "  );"
            )
            module_prefix = (
                ("\n".join(_render_imports(rows)) + "\n\n" if rows else "")
                + "/**\n"
                + f" * @arc-requirements {','.join(owner_requirements.get(ui_id, []))}\n"
                + " */\n"
                + f"export interface {props_symbol} {{\n"
                + ("\n".join(props_lines) if props_lines else "  // No external props were designed.")
                + "\n}\n"
            )
            dependency_symbols = list(dict.fromkeys(implementation_dependencies))
            if dependency_symbols:
                dependency_symbol = (
                    f"{function_symbol[:1].lower() + function_symbol[1:]}Dependencies"
                )
                implementation_symbol = f"{function_symbol}Implementation"
                dependency_body = "\n".join(
                    f"  {symbol}," for symbol in dependency_symbols
                )
                sources[path] = (
                    module_prefix
                    + f"\nconst {dependency_symbol} = {{\n{dependency_body}\n}} as const;\n"
                    + f"\nexport function {function_symbol}(_props: {props_symbol}) {{\n"
                    + f"  return <{implementation_symbol} props={{_props}} "
                    + f"dependencies={{{dependency_symbol}}} />;\n"
                    + "}\n"
                    + f"\nfunction {implementation_symbol}({{\n"
                    + "  props: _props,\n"
                    + "  dependencies: _dependencies,\n"
                    + "}: {\n"
                    + f"  props: {props_symbol};\n"
                    + f"  dependencies: typeof {dependency_symbol};\n"
                    + "}) {\n"
                    + default_body
                    + "}\n"
                )
            else:
                sources[path] = (
                    module_prefix
                    + f"\nexport function {function_symbol}(_props: {props_symbol}) {{\n"
                    + default_body
                    + "}\n"
                )
            imports[path] = rows
            exports[path] = [function_symbol, props_symbol]

    @staticmethod
    def _lower_system_main_page(
        stores: dict[str, dict[str, Any]],
        store_locations: dict[str, dict[str, Any]],
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
    ) -> None:
        """Materialize the compiler-owned landing page used by Home navigation."""

        path = "frontend/src/pages/system-main-page.tsx"
        auth_store_id: str | None = None
        username_field: str | None = None
        clear_action: str | None = None
        for store_id, store in sorted(stores.items()):
            state_names = {
                str(row.get("name", ""))
                for row in store.get("state", [])
                if isinstance(row, dict)
            }
            candidate_username = next(
                (
                    value
                    for value in ("current_username", "username", "display_name")
                    if value in state_names
                ),
                None,
            )
            looks_like_session = "session_id" in state_names or any(
                token in store_id.lower() for token in ("auth", "session")
            )
            if candidate_username and looks_like_session:
                auth_store_id = store_id
                username_field = candidate_username
                actions = [
                    str(row.get("name", ""))
                    for row in store.get("actions", [])
                    if isinstance(row, dict) and not row.get("input_type")
                ]
                clear_action = next(
                    (
                        value
                        for value in actions
                        if value.lower().startswith(("clear", "reset", "signout", "logout"))
                    ),
                    None,
                )
                break

        rows: list[dict[str, Any]] = []
        runtime_symbol: str | None = None
        if auth_store_id is not None:
            location = store_locations.get(auth_store_id)
            if location is not None:
                runtime_symbol = str(location["runtime_symbol"])
                store_runtime_path = "frontend/src/runtime/store.ts"
                rows.extend([
                    _import(
                        runtime_symbol,
                        _relative_specifier(path, str(location["path"])),
                        source=str(location["path"]),
                    ),
                    _import(
                        "useStoreState",
                        _relative_specifier(path, store_runtime_path),
                        source=store_runtime_path,
                    ),
                ])

        state_lines: list[str] = []
        if runtime_symbol and username_field:
            state_lines.append(f"  const sessionState = useStoreState({runtime_symbol});")
            username_expression = f"sessionState[{json.dumps(username_field)}]"
        else:
            username_expression = "null"
        sign_out = ""
        if runtime_symbol and clear_action:
            rows.append(_import("useNavigate", "react-router-dom"))
            state_lines.append("  const navigate = useNavigate();")
            sign_out = (
                "\n        <button\n"
                "          type=\"button\"\n"
                "          className=\"rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-semibold "
                "text-slate-700 shadow-sm transition hover:border-slate-400 hover:bg-slate-50 "
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500\"\n"
                f"          onClick={{() => {{ {runtime_symbol}.actions.{_safe_property(clear_action)}(); "
                "navigate(\"/login\"); }}\n"
                "        >\n          Sign out\n        </button>"
            )
        sources[path] = (
            ("\n".join(_render_imports(rows)) + "\n\n" if rows else "")
            + "export function SystemMainPage() {\n"
            + ("\n".join(state_lines) + "\n" if state_lines else "")
            + f"  const username = {username_expression};\n"
            + "  return (\n"
            + "    <main className=\"min-h-screen bg-slate-950 px-6 py-16 text-slate-100\">\n"
            + "      <section className=\"mx-auto flex w-full max-w-5xl flex-col gap-8 rounded-3xl "
            + "border border-white/10 bg-white/5 p-8 shadow-2xl backdrop-blur sm:p-12\">\n"
            + "        <p className=\"text-sm font-semibold uppercase tracking-[0.24em] text-indigo-300\">Workspace</p>\n"
            + "        <div className=\"space-y-3\">\n"
            + "          <h1 className=\"text-4xl font-semibold tracking-tight sm:text-5xl\">System main interface</h1>\n"
            + "          <p className=\"max-w-2xl text-base leading-7 text-slate-300\">"
            + "Your session is ready. Continue with the available product workflows.</p>\n"
            + "          {username ? <p className=\"text-sm text-slate-400\">Signed in as {String(username)}</p> : null}\n"
            + "        </div>"
            + sign_out
            + "\n      </section>\n"
            + "    </main>\n"
            + "  );\n"
            + "}\n"
        )
        imports[path] = rows
        exports[path] = ["SystemMainPage"]

    @staticmethod
    def _lower_router(
        pages: dict[str, dict[str, Any]],
        layouts: dict[str, dict[str, Any]],
        locations: dict[str, dict[str, Any]],
        sources: dict[str, str],
        imports: dict[str, list[dict[str, Any]]],
        exports: dict[str, list[str]],
        errors: list[str],
    ) -> list[dict[str, Any]]:
        router_path = "frontend/src/app/router.tsx"
        app_path = "frontend/src/App.tsx"
        rows = [
            _import("createBrowserRouter", "react-router-dom"),
            _import(
                "SystemMainPage",
                _relative_specifier(router_path, "frontend/src/pages/system-main-page.tsx"),
                source="frontend/src/pages/system-main-page.tsx",
            ),
        ]
        route_rows: list[dict[str, Any]] = []
        route_source: list[str] = []
        for page_id, page in sorted(pages.items(), key=lambda value: str(value[1].get("route", ""))):
            location = locations.get(page_id)
            if location is None:
                errors.append(f"ARC4226 FRONTEND_ROUTE_INVALID: page {page_id} has no location.")
                continue
            page_symbol = str(location["function_symbol"])
            page_props = str(location["props_symbol"])
            rows.extend([
                _import(page_symbol, _relative_specifier(router_path, str(location["path"])), source=str(location["path"])),
                _import(page_props, _relative_specifier(router_path, str(location["path"])), type_only=True, source=str(location["path"])),
            ])
            element = f"<{page_symbol} {{...({{}} as {page_props})}} />"
            layout_id = page.get("layout_id")
            layout_symbol = None
            if layout_id:
                layout_location = locations.get(str(layout_id))
                if layout_location is None or str(layout_id) not in layouts:
                    errors.append(
                        f"ARC4226 FRONTEND_ROUTE_INVALID: page {page_id} layout {layout_id} is unavailable."
                    )
                    continue
                layout_symbol = str(layout_location["function_symbol"])
                layout_props = str(layout_location["props_symbol"])
                rows.extend([
                    _import(layout_symbol, _relative_specifier(router_path, str(layout_location["path"])), source=str(layout_location["path"])),
                    _import(layout_props, _relative_specifier(router_path, str(layout_location["path"])), type_only=True, source=str(layout_location["path"])),
                ])
                element = (
                    f"<{layout_symbol} {{...({{}} as {layout_props})}}>"
                    f"{element}</{layout_symbol}>"
                )
            route = str(page.get("route", ""))
            route_source.append(f"  {{ path: {json.dumps(route)}, element: {element} }},")
            route_rows.append({
                "route_id": f"FRONTEND_ROUTE::{page_id}",
                "page_id": page_id,
                "path": route,
                "page_symbol": page_symbol,
                "page_source": str(location["path"]),
                "layout_id": layout_id,
                "layout_symbol": layout_symbol,
                "navigation": copy.deepcopy(page.get("navigation", [])),
            })
        if not any(str(page.get("route", "")) == "/" for page in pages.values()):
            route_source.append('  { path: "/", element: <SystemMainPage /> },')
            route_rows.append({
                "route_id": "FRONTEND_ROUTE::SYSTEM_MAIN",
                "page_id": "SYSTEM_MAIN",
                "path": "/",
                "page_symbol": "SystemMainPage",
                "page_source": "frontend/src/pages/system-main-page.tsx",
                "layout_id": None,
                "layout_symbol": None,
                "navigation": [],
            })
        route_source.append('  { path: "*", element: <SystemMainPage /> },')
        sources[router_path] = (
            "\n".join(_render_imports(rows))
            + "\n\nexport const router = createBrowserRouter([\n"
            + "\n".join(route_source)
            + "\n]);\n"
        )
        imports[router_path] = rows
        exports[router_path] = ["router"]

        app_imports = [
            _import("RouterProvider", "react-router-dom"),
            _import("router", "./app/router", source=router_path),
        ]
        sources[app_path] = (
            "\n".join(_render_imports(app_imports))
            + "\n\nexport default function App() {\n"
            + "  return <RouterProvider router={router} />;\n"
            + "}\n"
        )
        imports[app_path] = app_imports
        exports[app_path] = ["default"]
        return route_rows

    @staticmethod
    def _lower_barrels(
        ui_locations: dict[str, dict[str, Any]],
        store_locations: dict[str, dict[str, Any]],
        api_locations: dict[str, dict[str, Any]],
        sources: dict[str, str],
        exports: dict[str, list[str]],
    ) -> None:
        page_paths = sorted(
            {str(row["path"]) for row in ui_locations.values() if row.get("ui_kind") == "PAGE"}
            | {"frontend/src/pages/system-main-page.tsx"}
        )
        component_paths = sorted(
            {str(row["path"]) for row in ui_locations.values() if row.get("ui_kind") != "PAGE"}
        )
        store_paths = sorted({str(row["path"]) for row in store_locations.values()})
        api_paths = sorted({str(row["path"]) for row in api_locations.values()})
        barrel_sets = {
            "frontend/src/pages/index.ts": page_paths,
            "frontend/src/components/index.ts": component_paths,
            "frontend/src/app/stores/index.ts": store_paths,
            "frontend/src/api/index.ts": ["frontend/src/api/http.ts", *api_paths],
            "frontend/src/app/index.ts": [
                "frontend/src/app/router.tsx",
                "frontend/src/app/stores/index.ts",
            ],
        }
        for barrel, targets in barrel_sets.items():
            sources[barrel] = "".join(
                f'export * from "{_relative_specifier(barrel, target)}";\n'
                for target in targets
                if target != barrel
            ) or "export {};\n"
            exports[barrel] = ["*"]


def _object_rows(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors.append(f"ARC4200 FRONTEND_IR_INVALID: {label} must be a list.")
        return []
    rows = [item for item in value if isinstance(item, dict)]
    if len(rows) != len(value):
        errors.append(f"ARC4200 FRONTEND_IR_INVALID: {label} must contain only objects.")
    return rows


def _index_rows(
    value: Any,
    key: str,
    label: str,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    rows = _object_rows(value, label, errors)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_key = str(row.get(key, "")).strip()
        if not row_key or row_key in result:
            errors.append(f"ARC4200 FRONTEND_REGISTRY_INVALID: invalid {label} key {row_key!r}.")
            continue
        result[row_key] = row
    return result


def _table_by_id(frontend_ir: dict[str, Any], table: str) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for item in frontend_ir.get(table, [])
        if isinstance(item, dict) and item.get("id")
    }


def _frontend_owner_requirements(frontend_ir: dict[str, Any]) -> dict[str, list[str]]:
    owners: dict[str, set[str]] = {}
    for table in ("layouts", "pages", "components", "stores"):
        for item in frontend_ir.get(table, []):
            if not isinstance(item, dict):
                continue
            symbol_id = str(item.get("id", "")).strip()
            if not symbol_id:
                continue
            owners.setdefault(symbol_id, set()).update(
                str(value).strip()
                for value in item.get("requirement_ids", [])
                if str(value).strip()
            )
    for link in frontend_ir.get("requirement_links", []):
        if not isinstance(link, dict):
            continue
        requirement_id = str(link.get("requirement_id", "")).strip()
        if not requirement_id:
            continue
        for symbol_id in link.get("symbol_ids", []):
            normalized = str(symbol_id).strip()
            if normalized:
                owners.setdefault(normalized, set()).add(requirement_id)
    return {key: sorted(values) for key, values in sorted(owners.items())}


def _index_api_contracts(
    contracts: Any,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in _object_rows(contracts, "api_contracts", errors):
        api_id = str(item.get("id", "")).strip()
        if not api_id or api_id in result:
            errors.append(f"ARC4205 FRONTEND_API_UNKNOWN: invalid API {api_id!r}.")
            continue
        result[api_id] = item
    return result


def _index_backend_symbols(
    registry: dict[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    if registry.get("status") != "SYMBOLS_PLANNED":
        errors.append("ARC4206 BACKEND_SYMBOL_REGISTRY_INVALID: Backend symbols are not planned.")
    return _index_rows(registry.get("symbols"), "id", "backend symbols", errors)


def _index_backend_bindings(
    registry: dict[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    return _index_rows(
        registry.get("module_bindings"),
        "module_id",
        "backend module bindings",
        errors,
    )


def _index_backend_routes(
    registry: dict[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    if registry.get("status") != "ROUTES_PLANNED":
        errors.append("ARC4224 FRONTEND_API_ROUTE_UNKNOWN: Backend routes are not planned.")
    return _index_rows(registry.get("routes"), "module_id", "backend routes", errors)


def _frontend_api_ids(frontend_ir: dict[str, Any]) -> list[str]:
    result = {
        str(api_id)
        for table in ("pages", "components")
        for item in frontend_ir.get(table, [])
        if isinstance(item, dict)
        for api_id in item.get("api_dependencies", [])
        if str(api_id)
    }
    result.update(
        str(item.get("api_id"))
        for item in frontend_ir.get("api_dependencies", [])
        if isinstance(item, dict) and str(item.get("api_id", "")).strip()
    )
    return sorted(result)


def _render_fields(fields: Any) -> str:
    lines: list[str] = []
    for field_item in fields if isinstance(fields, list) else []:
        if not isinstance(field_item, dict):
            continue
        name = json.dumps(str(field_item.get("name", "value")))
        optional = "" if bool(field_item.get("required", True)) else "?"
        lines.append(
            f"  {name}{optional}: {_typescript_type(str(field_item.get('type', 'unknown')))};"
        )
    return "\n".join(lines) if lines else "  // No fields were designed."


def _typescript_type(value: str) -> str:
    raw = value.strip()
    lower = raw.lower()
    primitives = {
        "string": "string",
        "text": "string",
        "uuid": "string",
        "date": "string",
        "datetime": "string",
        "email": "string",
        "integer": "number",
        "int": "number",
        "number": "number",
        "float": "number",
        "boolean": "boolean",
        "bool": "boolean",
        "unknown": "unknown",
        "any": "unknown",
        "json": "unknown",
        "object": "Record<string, unknown>",
        "void": "void",
    }
    if lower in primitives:
        return primitives[lower]
    if lower.endswith("[]"):
        return f"Array<{_typescript_type(raw[:-2])}>"
    array_match = re.fullmatch(r"(?:array|list)<(.+)>", raw, flags=re.IGNORECASE)
    if array_match:
        return f"Array<{_typescript_type(array_match.group(1))}>"
    nullable_match = re.fullmatch(r"(?:optional|nullable)<(.+)>", raw, flags=re.IGNORECASE)
    if nullable_match:
        return f"{_typescript_type(nullable_match.group(1))} | null"
    # UI Design currently stores free-form type strings without a canonical
    # cross-file type reference. Preserve buildability until such a reference
    # exists instead of inventing an importable TypeScript symbol.
    return "unknown"


def _default_value(value: str) -> str:
    resolved = _typescript_type(value)
    if resolved == "string":
        return '""'
    if resolved == "number":
        return "0"
    if resolved == "boolean":
        return "false"
    if resolved.startswith("Array<"):
        return "[]"
    return f"undefined as unknown as {resolved}"


def _import(
    symbol: str,
    specifier: str,
    *,
    type_only: bool = False,
    source: str | None = None,
    import_style: str = "named",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "specifier": specifier,
        "import_style": import_style,
        "type_only": type_only,
    }
    if source:
        row["from"] = source
    return row


def _deduplicate_imports(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, bool, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("specifier", "")),
            str(row.get("symbol", "")),
            bool(row.get("type_only")),
            str(row.get("import_style", "named")),
        )
        if key[0] and key[1]:
            result[key] = copy.deepcopy(row)
    return [result[key] for key in sorted(result)]


def _render_imports(rows: Iterable[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in _deduplicate_imports(rows):
        keyword = "import type" if row.get("type_only") else "import"
        if row.get("import_style") == "default":
            lines.append(f'{keyword} {row["symbol"]} from "{row["specifier"]}";')
        else:
            lines.append(f'{keyword} {{ {row["symbol"]} }} from "{row["specifier"]}";')
    return lines


def _validated_port(value: Any, errors: list[str]) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        errors.append(
            f"ARC4227 FRONTEND_RUNTIME_CONFIG_INVALID: invalid Backend port {value!r}."
        )
        return 0
    return port


def _relative_specifier(current_path: str, target_path: str) -> str:
    relative = posixpath.relpath(target_path, posixpath.dirname(current_path))
    if not relative.startswith("."):
        relative = f"./{relative}"
    return re.sub(r"\.(?:ts|tsx)$", "", relative)


def _normalize_relative_path(value: Any) -> str | None:
    normalized = str(value).replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or "." in path.parts or ".." in path.parts:
        return None
    return normalized


def _safe_property(value: str) -> str:
    return _safe_identifier(_camel_case(value))


def _safe_identifier(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_$]", "", value)
    if not candidate:
        candidate = "generated"
    if not re.match(r"[A-Za-z_$]", candidate):
        candidate = f"generated{candidate}"
    return candidate


def _word_parts(value: str) -> list[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return [part for part in re.split(r"[^A-Za-z0-9]+", normalized) if part]


def _pascal_case(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in _word_parts(value))


def _camel_case(value: str) -> str:
    pascal = _pascal_case(value)
    return pascal[:1].lower() + pascal[1:] if pascal else "generated"


def _kebab_case(value: str) -> str:
    return "-".join(part.lower() for part in _word_parts(value)) or "generated"
