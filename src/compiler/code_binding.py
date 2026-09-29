from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


CODE_BINDING_SCHEMA_VERSION = 2
CODE_BINDING_READY = "CODE_BINDING_READY"


@dataclass(slots=True)
class CodeBindingResult:
    registry: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class CodeBindingLowerer:
    """Join frozen IR and lowering manifests to their materialized source surfaces."""

    def lower(
        self,
        *,
        output_root: Path,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any],
        design_ir: dict[str, Any],
        frontend_ir: dict[str, Any],
        backend_symbol_registry: dict[str, Any],
        backend_type_manifest: dict[str, Any],
        backend_module_manifests: dict[str, dict[str, Any]],
        backend_route_registry: dict[str, Any],
        frontend_symbol_registry: dict[str, Any],
        frontend_file_registry: dict[str, Any],
        frontend_route_registry: dict[str, Any],
        frontend_lowering_report: dict[str, Any] | None = None,
    ) -> CodeBindingResult:
        errors: list[str] = []
        root = output_root.expanduser().resolve()

        _index_rows(
            backend_symbol_registry.get("symbols"), "id", "backend symbols", errors
        )
        type_definitions = _index_rows(
            backend_type_manifest.get("definitions"),
            "symbol_id",
            "backend type definitions",
            errors,
        )
        backend_modules = _index_rows(
            design_ir.get("modules"), "id", "Backend Design modules", errors
        )
        backend_routes = _index_rows(
            backend_route_registry.get("routes"),
            "module_id",
            "backend routes",
            errors,
        )
        lowered_backend: dict[str, dict[str, Any]] = {}
        for kind in ("DB", "FUNC", "API"):
            manifest = backend_module_manifests.get(kind)
            if not isinstance(manifest, dict):
                errors.append(f"ARC4301 CODE_BINDING_INPUT_INVALID: missing {kind} module manifest.")
                continue
            for module_id, row in _index_rows(
                manifest.get("modules"), "module_id", f"{kind} module manifest", errors
            ).items():
                if module_id in lowered_backend:
                    errors.append(
                        f"ARC4301 CODE_BINDING_INPUT_INVALID: duplicate lowered module {module_id}."
                    )
                lowered_backend[module_id] = row

        frontend_symbols = _index_rows(
            frontend_symbol_registry.get("symbols"),
            "id",
            "frontend symbols",
            errors,
        )
        frontend_ui_locations = _index_rows(
            frontend_file_registry.get("ui_locations"),
            "ui_id",
            "frontend UI locations",
            errors,
        )
        frontend_store_locations = _index_rows(
            frontend_file_registry.get("store_locations"),
            "store_id",
            "frontend Store locations",
            errors,
        )
        frontend_api_locations = _index_rows(
            frontend_file_registry.get("api_client_locations"),
            "api_id",
            "frontend API client locations",
            errors,
        )
        frontend_routes = _index_rows(
            frontend_route_registry.get("routes"),
            "page_id",
            "frontend routes",
            errors,
        )

        owner_map = _frontend_owner_map(frontend_ir)
        type_bindings = _backend_type_bindings(type_definitions)
        type_by_id = {str(row["type_id"]): row for row in type_bindings}

        bindings: list[dict[str, Any]] = []
        for module_id, module in sorted(backend_modules.items()):
            lowered = lowered_backend.get(module_id)
            if lowered is None:
                errors.append(
                    f"ARC4302 CODE_BINDING_COVERAGE_INVALID: no lowered source for {module_id}."
                )
                continue
            kind = str(module.get("kind", "")).upper()
            owner = str(module.get("owner_requirement", "")).strip()
            input_type = _type_reference(lowered.get("input_contract_id"), type_by_id)
            output_type = _type_reference(lowered.get("output_contract_id"), type_by_id)
            symbol = str(lowered.get("function_symbol", "")).strip()
            route = copy.deepcopy(backend_routes.get(module_id)) if kind == "API" else None
            binding = {
                "module_id": module_id,
                "owner_requirements": [owner] if owner else [],
                "kind": kind,
                "file": str(lowered.get("path", "")),
                "symbol": symbol,
                "input_type": input_type,
                "output_type": output_type,
                "props_type": None,
                "route": _compact_backend_route(route),
                "callees": _strings(module.get("callees")),
            }
            bindings.append(binding)

        ui_tables = {
            "LAYOUT": "layouts",
            "PAGE": "pages",
            "COMPONENT": "components",
        }
        if "root_component_id" in frontend_ir:
            ui_tables = {}
        frontend_items: dict[str, dict[str, Any]] = {}
        if "root_component_id" in frontend_ir:
            locations = {row["entity_id"]: row for row in (frontend_lowering_report or {}).get("bindings", [])}
            from .frontend_context import component_requirement_owners
            component_owners = component_requirement_owners(frontend_ir)
            for component in frontend_ir.get("components", []):
                cid = component["id"]
                frontend_items[cid] = component
                location = locations.get(cid)
                if location is None:
                    errors.append(f"ARC4302 CODE_BINDING_COVERAGE_INVALID: no lowered source for {cid}.")
                    continue
                owners = component_owners.get(cid, set())
                callees = {row["component_ref"] for row in frontend_ir.get("ui", [])
                           if row.get("component_id") == cid and row.get("kind") == "COMPONENT"}
                callees.update(row["target"] for row in frontend_ir.get("effects", [])
                               if row.get("component_id") == cid and row.get("kind") == "REQUEST"
                               and row.get("target") in backend_modules)
                bindings.append({"module_id": cid, "owner_requirements": sorted(owners),
                                 "kind": "COMPONENT", "file": location["file"], "symbol": location["symbol"],
                                 "input_type": None, "output_type": None, "props_type": None,
                                 "route": None, "callees": sorted(callees)})
        for kind, table in ui_tables.items():
            rows = _index_rows(frontend_ir.get(table), "id", f"Frontend {table}", errors)
            for ui_id, item in sorted(rows.items()):
                frontend_items[ui_id] = item
                location = frontend_ui_locations.get(ui_id)
                if location is None:
                    errors.append(
                        f"ARC4302 CODE_BINDING_COVERAGE_INVALID: no lowered source for {ui_id}."
                    )
                    continue
                props_type = _frontend_type_reference(
                    location.get("props_symbol_id"), frontend_symbols, location.get("path")
                )
                symbol = str(location.get("function_symbol", "")).strip()
                owners = sorted(owner_map.get(ui_id, set()))
                relationships = _frontend_relationships(item)
                bindings.append(
                    {
                        "module_id": ui_id,
                        "owner_requirements": owners,
                        "kind": kind,
                        "file": str(location.get("path", "")),
                        "symbol": symbol,
                        "input_type": None,
                        "output_type": None,
                        "props_type": props_type,
                        "route": _compact_frontend_route(frontend_routes.get(ui_id)),
                        "callees": relationships,
                    }
                )

        store_rows = _index_rows(frontend_ir.get("stores", []), "id", "Frontend stores", errors)
        for store_id in sorted(store_rows):
            location = frontend_store_locations.get(store_id)
            if location is None:
                errors.append(
                    f"ARC4302 CODE_BINDING_COVERAGE_INVALID: no lowered source for {store_id}."
                )
                continue
            owners = sorted(owner_map.get(store_id, set()))
            store_types = [
                _frontend_type_reference(location.get(key), frontend_symbols, location.get("path"))
                for key in ("state_symbol_id", "actions_symbol_id", "value_symbol_id")
            ]
            store_types = [row for row in store_types if row is not None]
            type_bindings.extend(
                _frontend_declared_types(store_types)
            )
            bindings.append(
                {
                    "module_id": store_id,
                    "owner_requirements": owners,
                    "kind": "STORE",
                    "file": str(location.get("path", "")),
                    "symbol": str(location.get("runtime_symbol", "")),
                    "input_type": None,
                    "output_type": None,
                    "props_type": None,
                    "route": None,
                    "callees": [],
                    "store_types": store_types,
                }
            )

        for api_id, location in sorted(frontend_api_locations.items()):
            backend_module = backend_modules.get(api_id, {})
            owner = str(backend_module.get("owner_requirement", "")).strip()
            input_type = _frontend_contract_reference(
                location.get("input_contract_id"),
                location.get("input_type_symbol"),
                type_by_id,
            )
            output_type = _frontend_contract_reference(
                location.get("output_contract_id"),
                location.get("output_type_symbol"),
                type_by_id,
            )
            client_id = f"API_CLIENT::{api_id}"
            symbol = str(location.get("client_symbol", "")).strip()
            bindings.append(
                {
                    "module_id": client_id,
                    "source_ir_id": api_id,
                    "owner_requirements": [owner] if owner else [],
                    "kind": "API_CLIENT",
                    "file": str(location.get("path", "")),
                    "symbol": symbol,
                    "input_type": input_type,
                    "output_type": output_type,
                    "props_type": None,
                    "route": _compact_backend_route(backend_routes.get(api_id)),
                    "callees": [api_id],
                }
            )

        ui_type_rows: list[dict[str, Any]] = []
        for ui_id, location in sorted(frontend_ui_locations.items()):
            reference = _frontend_type_reference(
                location.get("props_symbol_id"), frontend_symbols, location.get("path")
            )
            if reference is not None:
                ui_type_rows.extend(
                    _frontend_declared_types([reference])
                )
        type_bindings.extend(ui_type_rows)
        type_bindings = _deduplicate_type_bindings(type_bindings, errors)

        bindings.sort(key=lambda row: str(row.get("module_id", "")))
        binding_by_id = _unique_bindings(bindings, errors)
        _validate_coverage(backend_modules, frontend_items, store_rows, binding_by_id, errors)
        _validate_sources(root, bindings, type_bindings, errors)

        requirement_targets = _requirement_targets(
            requirement_ir,
            bindings,
            frontend_ir,
            dependency_graph,
        )
        _validate_requirement_permissions(
            requirement_targets,
            binding_by_id,
            errors,
        )
        registry = {
            "schema_version": CODE_BINDING_SCHEMA_VERSION,
            "status": CODE_BINDING_READY if not errors else "CODE_BINDING_INVALID",
            "code_bindings": bindings,
            "type_bindings": type_bindings,
            "requirement_targets": requirement_targets,
        }
        return CodeBindingResult(registry=registry, errors=list(dict.fromkeys(errors)))


class CodeTargetResolver:
    """Read-only query interface used by future test generation and TDD stages."""

    def __init__(self, registry: dict[str, Any]) -> None:
        if not isinstance(registry, dict) or registry.get("status") != CODE_BINDING_READY:
            raise ValueError("Code Binding Registry is not ready.")
        self._bindings = {
            str(row["module_id"]): row
            for row in registry.get("code_bindings", [])
            if isinstance(row, dict) and row.get("module_id")
        }
        self._types = {
            str(row["type_id"]): row
            for row in registry.get("type_bindings", [])
            if isinstance(row, dict) and row.get("type_id")
        }
        self._requirements = {
            str(row["requirement_id"]): row
            for row in registry.get("requirement_targets", [])
            if isinstance(row, dict) and row.get("requirement_id")
        }

    def resolve(self, module_id: str) -> dict[str, Any] | None:
        row = self._bindings.get(str(module_id))
        return copy.deepcopy(row) if row is not None else None

    def resolve_type(self, type_id: str) -> dict[str, Any] | None:
        row = self._types.get(str(type_id))
        return copy.deepcopy(row) if row is not None else None

    def resolve_requirement_targets(self, requirement_id: str) -> dict[str, Any]:
        row = self._requirements.get(str(requirement_id))
        if row is None:
            raise KeyError(f"Unknown requirement: {requirement_id}")
        owned_targets = [
            copy.deepcopy(self._bindings[module_id])
            for module_id in row.get("writable", [])
            if module_id in self._bindings
        ]
        dependency_targets = [
            copy.deepcopy(self._bindings[module_id])
            for module_id in row.get("read_only", [])
            if module_id in self._bindings
        ]
        type_ids = _referenced_type_ids([*owned_targets, *dependency_targets])
        return {
            **copy.deepcopy(row),
            "owned_targets": owned_targets,
            "dependency_targets": dependency_targets,
            "type_targets": [
                copy.deepcopy(self._types[type_id])
                for type_id in type_ids
                if type_id in self._types
            ],
        }

def resolve_requirement_targets(
    registry: dict[str, Any], requirement_id: str
) -> dict[str, Any]:
    return CodeTargetResolver(registry).resolve_requirement_targets(requirement_id)




def _referenced_type_ids(targets: list[dict[str, Any]]) -> list[str]:
    type_ids: set[str] = set()
    for target in targets:
        references = [
            target.get("input_type"),
            target.get("output_type"),
            target.get("props_type"),
            *target.get("store_types", []),
        ]
        for reference in references:
            if not isinstance(reference, dict):
                continue
            type_id = str(reference.get("type_id", "")).strip()
            if type_id:
                type_ids.add(type_id)
    return sorted(type_ids)


def _backend_type_bindings(
    definitions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "type_id": type_id,
            "file": str(definition.get("path", "")),
            "symbol": str(definition.get("symbol", "")),
        }
        for type_id, definition in sorted(definitions.items())
    ]


def _frontend_declared_types(
    references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "type_id": str(reference.get("type_id", "")),
            "file": str(reference.get("file", "")),
            "symbol": str(reference.get("symbol", "")),
        }
        for reference in references
    ]


def _type_reference(type_id: Any, types: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    normalized = str(type_id or "").strip()
    if not normalized:
        return None
    row = types.get(normalized)
    if row is None:
        return {"type_id": normalized, "symbol": None, "file": None}
    return {
        "type_id": normalized,
        "symbol": row.get("symbol"),
        "file": row.get("file"),
    }


def _frontend_type_reference(
    type_id: Any,
    symbols: dict[str, dict[str, Any]],
    file: Any,
) -> dict[str, Any] | None:
    normalized = str(type_id or "").strip()
    if not normalized:
        return None
    symbol = symbols.get(normalized)
    if symbol is None:
        return None
    return {
        "type_id": normalized,
        "symbol": str(symbol.get("symbol", "")),
        "file": str(file or ""),
    }


def _frontend_contract_reference(
    type_id: Any,
    type_symbol: Any,
    types: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    reference = _type_reference(type_id, types)
    if reference is not None and not reference.get("symbol") and type_symbol:
        reference["symbol"] = str(type_symbol)
    return reference


def _compact_backend_route(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(route, dict):
        return None
    return {
        key: copy.deepcopy(route.get(key))
        for key in ("route_id", "method", "path", "input_source")
    }


def _compact_frontend_route(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(route, dict):
        return None
    return {
        key: copy.deepcopy(route.get(key))
        for key in ("route_id", "path", "layout_id", "navigation")
    }


def _frontend_owner_map(frontend_ir: dict[str, Any]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for table in ("layouts", "pages", "components", "stores"):
        for item in frontend_ir.get(table, []):
            if not isinstance(item, dict):
                continue
            symbol_id = str(item.get("id", "")).strip()
            if symbol_id:
                owners.setdefault(symbol_id, set()).update(
                    _strings(item.get("requirement_ids"))
                )
    for link in frontend_ir.get("requirement_links", []):
        if not isinstance(link, dict):
            continue
        requirement_id = str(link.get("requirement_id", "")).strip()
        if not requirement_id:
            continue
        for symbol_id in _strings(link.get("symbol_ids")):
            owners.setdefault(symbol_id, set()).add(requirement_id)
    return owners


def _frontend_relationships(item: dict[str, Any]) -> list[str]:
    result = _strings(item.get("component_ids"))
    layout_id = str(item.get("layout_id", "")).strip()
    if layout_id:
        result.append(layout_id)
    result.extend(_strings(item.get("store_dependencies")))
    result.extend(_strings(item.get("api_dependencies")))
    return sorted(set(result))


def _requirement_targets(
    requirement_ir: dict[str, Any],
    bindings: list[dict[str, Any]],
    frontend_ir: dict[str, Any],
    dependency_graph: dict[str, Any],
) -> list[dict[str, Any]]:
    requirement_ids = [
        str(value)
        for value in requirement_ir.get("node_order", [])
        if str(value).strip()
    ]
    nodes = requirement_ir.get("nodes", {})
    nodes = nodes if isinstance(nodes, dict) else {}
    by_id = {str(row["module_id"]): row for row in bindings}
    graph: dict[str, set[str]] = {
        module_id: {value for value in _strings(row.get("callees")) if value in by_id}
        for module_id, row in by_id.items()
    }
    api_client_by_api = {
        str(row.get("source_ir_id")): str(row["module_id"])
        for row in bindings
        if row.get("kind") == "API_CLIENT"
    }
    consumer_api_ids: dict[str, set[str]] = {}
    for dependency in frontend_ir.get("api_dependencies", []):
        if not isinstance(dependency, dict):
            continue
        consumer = str(dependency.get("consumer_id", ""))
        api_id = str(dependency.get("api_id", ""))
        if consumer and api_id:
            consumer_api_ids.setdefault(consumer, set()).update(
                value for value in (api_id, api_client_by_api.get(api_id)) if value
            )
    for consumer, api_ids in consumer_api_ids.items():
        graph.setdefault(consumer, set()).update(value for value in api_ids if value in by_id)

    semantic_owners_by_requirement = {
        requirement_id: {
            str(row["module_id"])
            for row in bindings
            if requirement_id in _strings(row.get("owner_requirements"))
        }
        for requirement_id in requirement_ids
    }
    write_owner_by_module = _module_write_owners(
        requirement_ids=requirement_ids,
        nodes=nodes,
        bindings=bindings,
        dependency_graph=dependency_graph,
    )
    owned_by_requirement = {
        requirement_id: {
            module_id
            for module_id, owner_id in write_owner_by_module.items()
            if owner_id == requirement_id
        }
        for requirement_id in requirement_ids
    }
    for binding in bindings:
        if binding.get("kind") not in {"PAGE", "COMPONENT", "STORE", "LAYOUT"}:
            continue
        module_id = str(binding.get("module_id", ""))
        atomic_owners = {
            owner for owner in _strings(binding.get("owner_requirements"))
            if isinstance(nodes.get(owner), dict)
            and str(nodes[owner].get("type", "")).upper() == "ATOMIC"
        }
        if atomic_owners:
            if binding.get("kind") in {"PAGE", "COMPONENT", "STORE", "LAYOUT"}:
                for owner in _strings(binding.get("owner_requirements")):
                    if owner in owned_by_requirement:
                        owned_by_requirement[owner].add(module_id)
            else:
                for requirement_id in requirement_ids:
                    owned_by_requirement[requirement_id].discard(module_id)
                for owner in atomic_owners:
                    owned_by_requirement[owner].add(module_id)
        else:
            # Seven-entity frontend records are intentionally editable from
            # every requirement that semantically references the component or
            # one of its UI/behavior descendants.  Backend modules retain the
            # single-writer policy below; this branch only expands the
            # frontend composition scope.
            for owner in _strings(binding.get("owner_requirements")):
                if owner in owned_by_requirement:
                    owned_by_requirement[owner].add(module_id)
    requirement_dependencies = dependency_graph.get("requirement_dependencies", {})
    atomic_dependencies = dependency_graph.get("atomic_dependencies", {})

    def dependency_closure(requirement_id: str) -> set[str]:
        source = (
            atomic_dependencies
            if requirement_id in atomic_dependencies
            else requirement_dependencies
        )
        result: set[str] = set()
        pending = [str(value) for value in source.get(requirement_id, [])]
        while pending:
            dependency_id = pending.pop()
            if dependency_id in result or dependency_id == requirement_id:
                continue
            result.add(dependency_id)
            nested_source = (
                atomic_dependencies
                if dependency_id in atomic_dependencies
                else requirement_dependencies
            )
            pending.extend(str(value) for value in nested_source.get(dependency_id, []))
        return result

    rows: list[dict[str, Any]] = []
    for requirement_id in requirement_ids:
        owned = owned_by_requirement.get(requirement_id, set())
        # A module may be semantically relevant to several requirements, but
        # only its selected write owner may edit it. Other semantic owners see
        # the same module as a read-only dependency.
        reachable: set[str] = set(
            semantic_owners_by_requirement.get(requirement_id, set()) - owned
        )
        reachable.update({
            module_id
            for dependency_id in dependency_closure(requirement_id)
            for module_id in owned_by_requirement.get(dependency_id, set())
            if module_id not in owned
        })
        pending = [*owned, *reachable]
        while pending:
            current = pending.pop()
            for dependency in graph.get(current, set()):
                if dependency not in reachable and dependency not in owned:
                    reachable.add(dependency)
                    pending.append(dependency)
        rows.append(
            {
                "requirement_id": requirement_id,
                "writable": sorted(owned),
                "read_only": sorted(reachable),
            }
        )
    return rows


def _module_write_owners(
    *,
    requirement_ids: list[str],
    nodes: dict[str, Any],
    bindings: list[dict[str, Any]],
    dependency_graph: dict[str, Any],
) -> dict[str, str]:
    """Choose one deterministic writer for every semantically shared module."""

    order_index = {
        requirement_id: index for index, requirement_id in enumerate(requirement_ids)
    }
    atomic_order = [
        str(requirement_id)
        for wave in dependency_graph.get("atomic_implementation_waves", [])
        if isinstance(wave, list)
        for requirement_id in wave
        if str(requirement_id)
    ]
    atomic_rank = {
        requirement_id: index for index, requirement_id in enumerate(atomic_order)
    }

    def depth(requirement_id: str) -> int:
        result = 0
        current = requirement_id
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            node = nodes.get(current, {})
            if not isinstance(node, dict):
                break
            parent = str(node.get("parent_id", "")).strip()
            if not parent:
                break
            result += 1
            current = parent
        return result

    result: dict[str, str] = {}
    valid_requirements = set(requirement_ids)
    for binding in bindings:
        module_id = str(binding.get("module_id", "")).strip()
        owners = sorted(
            {
                value
                for value in _strings(binding.get("owner_requirements"))
                if value in valid_requirements
            }
        )
        if not module_id or not owners:
            continue
        atomic_owners = [
            value
            for value in owners
            if isinstance(nodes.get(value), dict)
            and str(nodes[value].get("type", "")).upper() == "ATOMIC"
        ]
        if atomic_owners:
            result[module_id] = min(
                atomic_owners,
                key=lambda value: (
                    atomic_rank.get(value, len(atomic_rank) + order_index.get(value, 0)),
                    order_index.get(value, len(order_index)),
                    value,
                ),
            )
            continue
        result[module_id] = min(
            owners,
            key=lambda value: (
                -depth(value),
                order_index.get(value, len(order_index)),
                value,
            ),
        )
    return result


def _validate_requirement_permissions(
    requirement_targets: list[dict[str, Any]],
    binding_by_id: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    """Allow shared frontend surfaces, but keep backend modules single-writer."""

    writable_owners: dict[str, list[str]] = {}
    permissions_by_requirement: dict[str, tuple[set[str], set[str]]] = {}
    known_modules = set(binding_by_id)
    for row in requirement_targets:
        requirement_id = str(row.get("requirement_id", "")).strip()
        writable = set(_strings(row.get("writable")))
        read_only = set(_strings(row.get("read_only")))
        overlap = sorted(writable & read_only)
        if overlap:
            errors.append(
                "ARC4306 CODE_BINDING_OWNERSHIP_INVALID: "
                f"{requirement_id} marks modules as both writable and read-only "
                f"{overlap}."
            )
        referenced = writable | read_only
        unknown = sorted(referenced - known_modules)
        if unknown:
            errors.append(
                "ARC4306 CODE_BINDING_OWNERSHIP_INVALID: "
                f"{requirement_id} references unknown modules {unknown}."
            )
        for module_id in writable:
            writable_owners.setdefault(module_id, []).append(requirement_id)
        permissions_by_requirement[requirement_id] = (writable, read_only)

    known_requirements = set(permissions_by_requirement)
    for module_id, binding in sorted(binding_by_id.items()):
        semantic_owners = {
            value
            for value in _strings(binding.get("owner_requirements"))
            if value in known_requirements
        }
        owners = sorted(set(writable_owners.get(module_id, [])))
        if binding.get("kind") in {"PAGE", "COMPONENT", "STORE", "LAYOUT"}:
            if semantic_owners and not owners:
                errors.append(
                    "ARC4306 CODE_BINDING_OWNERSHIP_INVALID: "
                    f"{module_id} has no writable requirement."
                )
        elif semantic_owners and len(owners) != 1:
            errors.append(
                "ARC4306 CODE_BINDING_OWNERSHIP_INVALID: "
                f"{module_id} must have exactly one writable requirement; found {owners}."
            )
        invalid_owners = sorted(set(owners) - semantic_owners)
        if invalid_owners:
            errors.append(
                "ARC4306 CODE_BINDING_OWNERSHIP_INVALID: "
                f"{module_id} is writable by non-semantic owners {invalid_owners}."
            )
        for requirement_id in sorted(semantic_owners):
            writable, read_only = permissions_by_requirement[requirement_id]
            if module_id not in writable and module_id not in read_only:
                errors.append(
                    "ARC4306 CODE_BINDING_OWNERSHIP_INVALID: "
                    f"{module_id} is missing from semantic owner {requirement_id}."
                )


def _validate_coverage(
    backend_modules: dict[str, dict[str, Any]],
    frontend_items: dict[str, dict[str, Any]],
    stores: dict[str, dict[str, Any]],
    bindings: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    expected = set(backend_modules) | set(frontend_items) | set(stores)
    missing = sorted(expected - set(bindings))
    if missing:
        errors.append(
            f"ARC4302 CODE_BINDING_COVERAGE_INVALID: missing bindings for {missing}."
        )


def _validate_sources(
    output_root: Path,
    bindings: list[dict[str, Any]],
    type_bindings: list[dict[str, Any]],
    errors: list[str],
) -> None:
    cache: dict[str, str] = {}

    def source(relative: str) -> str | None:
        normalized = _safe_relative_path(relative)
        if normalized is None:
            errors.append(f"ARC4304 CODE_BINDING_FILE_INVALID: invalid path {relative!r}.")
            return None
        if normalized in cache:
            return cache[normalized]
        path = (output_root / Path(normalized)).resolve()
        if output_root != path and output_root not in path.parents:
            errors.append(f"ARC4304 CODE_BINDING_FILE_INVALID: path escapes workspace {relative!r}.")
            return None
        if not path.is_file():
            errors.append(f"ARC4304 CODE_BINDING_FILE_INVALID: source does not exist {relative!r}.")
            return None
        try:
            cache[normalized] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"ARC4304 CODE_BINDING_FILE_INVALID: cannot read {relative!r}: {exc}")
            return None
        return cache[normalized]

    for binding in bindings:
        module_id = str(binding.get("module_id", ""))
        content = source(str(binding.get("file", "")))
        if content is None:
            continue
        symbol = str(binding.get("symbol", ""))
        if not _has_named_export(content, symbol):
            errors.append(
                f"ARC4305 CODE_BINDING_SYMBOL_INVALID: {module_id} export {symbol!r} is missing."
            )
    for binding in type_bindings:
        type_id = str(binding.get("type_id", ""))
        content = source(str(binding.get("file", "")))
        if content is None:
            continue
        symbol = str(binding.get("symbol", ""))
        if not _has_named_export(content, symbol):
            errors.append(
                f"ARC4307 TYPE_BINDING_SYMBOL_INVALID: {type_id} export {symbol!r} is missing."
            )


def _has_named_export(content: str, symbol: str) -> bool:
    if not symbol:
        return False
    pattern = re.compile(
        rf"\bexport\s+(?:default\s+)?(?:async\s+)?"
        rf"(?:function|interface|type|const|class)\s+{re.escape(symbol)}\b"
    )
    return pattern.search(content) is not None


def _safe_relative_path(value: str) -> str | None:
    normalized = str(value).replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or "." in path.parts or ".." in path.parts:
        return None
    if not normalized.endswith((".ts", ".tsx")):
        return None
    return normalized


def _deduplicate_type_bindings(
    rows: list[dict[str, Any]], errors: list[str]
) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        type_id = str(row.get("type_id", ""))
        if not type_id:
            errors.append("ARC4303 TYPE_BINDING_COVERAGE_INVALID: empty type id.")
            continue
        existing = result.get(type_id)
        if existing is not None and (
            existing.get("file") != row.get("file")
            or existing.get("symbol") != row.get("symbol")
        ):
            errors.append(
                f"ARC4303 TYPE_BINDING_COVERAGE_INVALID: conflicting binding for {type_id}."
            )
            continue
        if existing is None:
            result[type_id] = row
    return [result[key] for key in sorted(result)]


def _unique_bindings(
    rows: list[dict[str, Any]], errors: list[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        module_id = str(row.get("module_id", ""))
        if not module_id or module_id in result:
            errors.append(
                f"ARC4302 CODE_BINDING_COVERAGE_INVALID: invalid or duplicate binding {module_id!r}."
            )
            continue
        result[module_id] = row
    return result


def _index_rows(
    value: Any,
    key: str,
    label: str,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        errors.append(f"ARC4301 CODE_BINDING_INPUT_INVALID: {label} must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in value:
        row_id = str(row.get(key, "")).strip() if isinstance(row, dict) else ""
        if not row_id or row_id in result:
            errors.append(
                f"ARC4301 CODE_BINDING_INPUT_INVALID: invalid or duplicate {label} id {row_id!r}."
            )
            continue
        result[row_id] = row
    return result


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


__all__ = [
    "CODE_BINDING_READY",
    "CodeBindingLowerer",
    "CodeBindingResult",
    "CodeTargetResolver",
    "resolve_requirement_targets",
]
