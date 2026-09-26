from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any


MODULE_MANIFEST_SCHEMA_VERSION = 1
MODULE_KINDS = {"DB", "FUNC", "API"}
MODULE_STATUSES = {
    "DB": "DB_MODULES_GENERATED",
    "FUNC": "FUNC_MODULES_GENERATED",
    "API": "API_MODULES_GENERATED",
}
MODULE_ROOTS = {
    "DB": "backend/src/db/repositories/",
    "FUNC": "backend/src/functions/",
    "API": "backend/src/api/",
}
BARREL_PATHS = {
    "DB": "backend/src/db/repositories/index.ts",
    "FUNC": "backend/src/functions/index.ts",
    "API": "backend/src/api/index.ts",
}
ERROR_CODES = {"DB": "ARC350", "FUNC": "ARC360", "API": "ARC370"}

RUNTIME_ERRORS_PATH = "backend/src/runtime/errors.ts"
RUNTIME_IDS_PATH = "backend/src/runtime/ids.ts"
RUNTIME_CLOCK_PATH = "backend/src/runtime/clock.ts"
RUNTIME_STUBS_PATH = "backend/src/runtime/stubs.ts"
RUNTIME_FILES = (
    RUNTIME_ERRORS_PATH,
    RUNTIME_IDS_PATH,
    RUNTIME_CLOCK_PATH,
    RUNTIME_STUBS_PATH,
)

# Zero value per design type.  A stub answers with the emptiest well-typed value
# its contract allows, so a caller that only needs the shape keeps working while a
# caller that needs real data still fails its assertion.
ZERO_VALUES: dict[str, str] = {
    "string": '""',
    "date": '""',
    "datetime": '""',
    "uuid": '""',
    "integer": "0",
    "number": "0",
    "boolean": "false",
    "json": "[]",
}

# Every backend module receives the same cross-cutting runtime. Owning these here
# keeps identity, time and failure reporting out of the implementation agent's
# hands: it can use them, it cannot reinvent them.
RUNTIME_IMPORTS: tuple[tuple[str, str], ...] = (
    ("NotImplementedError", RUNTIME_ERRORS_PATH),
    ("HttpError", RUNTIME_ERRORS_PATH),
    ("newId", RUNTIME_IDS_PATH),
    ("now", RUNTIME_CLOCK_PATH),
    ("nowIso", RUNTIME_CLOCK_PATH),
    ("recordStubHit", RUNTIME_STUBS_PATH),
)

# Drizzle predicate/ordering helpers a repository realistically needs. Importing
# them unconditionally removes the single most common "I cannot express this
# query inside my region" failure.
DRIZZLE_OPERATORS: tuple[str, ...] = (
    "and",
    "asc",
    "desc",
    "eq",
    "gt",
    "gte",
    "inArray",
    "isNotNull",
    "isNull",
    "like",
    "lt",
    "lte",
    "ne",
    "or",
    "sql",
)


@dataclass(slots=True)
class ModuleSkeletonResult:
    manifest: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ModuleSkeletonLowerer:
    """Generate frozen module surfaces and editable source files."""

    def lower(
        self,
        module_kind: str,
        design_ir: dict[str, Any],
        symbol_registry: dict[str, Any],
        file_registry: dict[str, Any],
    ) -> ModuleSkeletonResult:
        kind = str(module_kind).upper()
        if kind not in MODULE_KINDS:
            return ModuleSkeletonResult(
                manifest={
                    "schema_version": MODULE_MANIFEST_SCHEMA_VERSION,
                    "status": "MODULE_SKELETON_GENERATION_FAILED",
                    "module_kind": kind,
                    "modules": [],
                    "generated_files": [],
                },
                errors=[f"ARC3501 MODULE_KIND_INVALID: unsupported module kind {kind!r}."],
            )

        prefix = ERROR_CODES[kind]
        errors: list[str] = []
        modules = _index_modules(design_ir, errors, prefix)
        symbols = _index_symbols(symbol_registry, errors, prefix)
        bindings = _index_bindings(symbol_registry, errors, prefix)
        locations = _index_locations(file_registry, errors, prefix)
        planned_files = _planned_files(file_registry, errors, prefix)
        _validate_registry_statuses(symbol_registry, file_registry, errors, prefix)

        selected = {
            module_id: module
            for module_id, module in modules.items()
            if module.get("kind") == kind
        }
        sources: dict[str, str] = {}
        manifest_modules: list[dict[str, Any]] = []

        required_barrel = BARREL_PATHS[kind]
        if required_barrel not in planned_files:
            errors.append(
                f"{prefix}2 FILE_REGISTRY_INVALID: missing planned barrel {required_barrel}."
            )
        missing_runtime = sorted(set(RUNTIME_FILES) - planned_files)
        if missing_runtime:
            errors.append(
                f"{prefix}2 FILE_REGISTRY_INVALID: missing planned runtime files {missing_runtime}."
            )

        for module_id, module in sorted(selected.items()):
            symbol = symbols.get(module_id)
            binding = bindings.get(module_id)
            location = locations.get(module_id)
            if symbol is None or symbol.get("kind") != "MODULE":
                errors.append(f"{prefix}3 MODULE_SYMBOL_MISSING: {module_id}.")
                continue
            if binding is None or location is None:
                errors.append(f"{prefix}4 MODULE_PLAN_MISSING: binding or file missing for {module_id}.")
                continue
            path = str(location.get("path", ""))
            if not path.startswith(MODULE_ROOTS[kind]):
                errors.append(
                    f"{prefix}5 MODULE_PATH_INVALID: {module_id} is outside {MODULE_ROOTS[kind]}."
                )
                continue
            if path not in planned_files:
                errors.append(
                    f"{prefix}5 MODULE_PATH_INVALID: {module_id} path is not in File Registry."
                )
                continue
            if binding.get("function_symbol") != symbol.get("symbol"):
                errors.append(f"{prefix}6 MODULE_BINDING_INVALID: function mismatch for {module_id}.")
                continue

            input_contract = _contract_symbol(
                binding.get("input_contract_id"),
                symbols,
                locations,
                errors,
                prefix,
                module_id,
            )
            output_contract = _contract_symbol(
                binding.get("output_contract_id"),
                symbols,
                locations,
                errors,
                prefix,
                module_id,
            )
            dependency_rows = _dependency_rows(
                kind,
                module,
                modules,
                symbols,
                locations,
                path,
                errors,
                prefix,
            )
            database_rows = _database_rows(
                kind,
                module,
                symbols,
                locations,
                path,
                errors,
            )
            source, exports, import_plan = _render_module(
                kind,
                module,
                symbol,
                input_contract,
                output_contract,
                dependency_rows,
                database_rows,
                path,
            )
            sources[path] = source
            manifest_modules.append(
                {
                    "module_id": module_id,
                    "path": path,
                    "function_symbol": str(symbol["symbol"]),
                    "input_contract_id": binding.get("input_contract_id"),
                    "output_contract_id": binding.get("output_contract_id"),
                    "callees": list(module.get("callees", [])),
                    "database_entities": [row["entity"] for row in database_rows],
                    "exports": exports,
                    "imports": import_plan,
                }
            )

        expected = set(selected)
        generated = {row["module_id"] for row in manifest_modules}
        if generated != expected:
            errors.append(
                f"{prefix}7 MODULE_COVERAGE_INVALID: missing={sorted(expected - generated)}, "
                f"unknown={sorted(generated - expected)}."
            )

        if not errors:
            sources[required_barrel] = _render_module_barrel(
                required_barrel,
                manifest_modules,
            )
            if kind == "DB":
                sources["backend/src/runtime/errors.ts"] = _render_runtime_errors()

        status = MODULE_STATUSES[kind] if not errors else f"{kind}_MODULE_GENERATION_FAILED"
        manifest = {
            "schema_version": MODULE_MANIFEST_SCHEMA_VERSION,
            "status": status,
            "module_kind": kind,
            "modules": manifest_modules,
            "planned_files": sorted(sources),
            "generated_files": [] if errors else sorted(sources),
        }
        return ModuleSkeletonResult(
            manifest=manifest,
            sources={} if errors else sources,
            errors=list(dict.fromkeys(errors)),
        )


def _index_modules(
    design_ir: Any,
    errors: list[str],
    prefix: str,
) -> dict[str, dict[str, Any]]:
    rows = design_ir.get("modules", []) if isinstance(design_ir, dict) else []
    if not isinstance(rows, list):
        errors.append(f"{prefix}1 DESIGN_IR_INVALID: modules must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        module_id = str(row.get("id", "")).strip() if isinstance(row, dict) else ""
        kind = str(row.get("kind", "")).strip() if isinstance(row, dict) else ""
        if not module_id or module_id in result or kind not in MODULE_KINDS:
            errors.append(f"{prefix}1 DESIGN_IR_INVALID: invalid or duplicate module {module_id!r}.")
            continue
        result[module_id] = row
    return result


def _index_symbols(
    registry: Any,
    errors: list[str],
    prefix: str,
) -> dict[str, dict[str, Any]]:
    rows = registry.get("symbols", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append(f"{prefix}2 SYMBOL_REGISTRY_INVALID: symbols must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol_id = str(row.get("id", "")).strip() if isinstance(row, dict) else ""
        symbol_name = str(row.get("symbol", "")).strip() if isinstance(row, dict) else ""
        if not symbol_id or not symbol_name or symbol_id in result:
            errors.append(f"{prefix}2 SYMBOL_REGISTRY_INVALID: invalid symbol {symbol_id!r}.")
            continue
        result[symbol_id] = row
    return result


def _index_bindings(
    registry: Any,
    errors: list[str],
    prefix: str,
) -> dict[str, dict[str, Any]]:
    rows = registry.get("module_bindings", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append(f"{prefix}2 SYMBOL_REGISTRY_INVALID: bindings must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        module_id = str(row.get("module_id", "")).strip() if isinstance(row, dict) else ""
        if not module_id or module_id in result:
            errors.append(f"{prefix}2 SYMBOL_REGISTRY_INVALID: invalid binding {module_id!r}.")
            continue
        result[module_id] = row
    return result


def _index_locations(
    registry: Any,
    errors: list[str],
    prefix: str,
) -> dict[str, dict[str, Any]]:
    rows = registry.get("symbol_locations", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append(f"{prefix}2 FILE_REGISTRY_INVALID: symbol_locations must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol_id = str(row.get("symbol_id", "")).strip() if isinstance(row, dict) else ""
        path = str(row.get("path", "")).strip() if isinstance(row, dict) else ""
        if not symbol_id or not path or symbol_id in result:
            errors.append(f"{prefix}2 FILE_REGISTRY_INVALID: invalid location {symbol_id!r}.")
            continue
        result[symbol_id] = row
    return result


def _planned_files(registry: Any, errors: list[str], prefix: str) -> set[str]:
    rows = registry.get("files", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append(f"{prefix}2 FILE_REGISTRY_INVALID: files must be a list.")
        return set()
    return {
        str(row.get("path", ""))
        for row in rows
        if isinstance(row, dict) and row.get("path")
    }


def _validate_registry_statuses(
    symbol_registry: Any,
    file_registry: Any,
    errors: list[str],
    prefix: str,
) -> None:
    if not isinstance(symbol_registry, dict) or symbol_registry.get("status") != "SYMBOLS_PLANNED":
        errors.append(f"{prefix}2 SYMBOL_REGISTRY_INVALID: symbols are not planned.")
    if not isinstance(file_registry, dict) or file_registry.get("status") != "FILES_PLANNED":
        errors.append(f"{prefix}2 FILE_REGISTRY_INVALID: files are not planned.")


def _contract_symbol(
    contract_id: Any,
    symbols: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    errors: list[str],
    prefix: str,
    module_id: str,
) -> dict[str, Any] | None:
    if contract_id is None:
        return None
    symbol = symbols.get(str(contract_id))
    if symbol is None or symbol.get("kind") != "DATA_CONTRACT":
        errors.append(
            f"{prefix}8 CONTRACT_MISSING: {module_id} references {contract_id!r}."
        )
        return None
    location = locations.get(str(contract_id))
    if location is None:
        errors.append(
            f"{prefix}8 CONTRACT_MISSING: {module_id} contract {contract_id!r} has no file."
        )
        return None
    result = dict(symbol)
    result["source_path"] = str(location["path"])
    return result


def _dependency_rows(
    kind: str,
    module: dict[str, Any],
    modules: dict[str, dict[str, Any]],
    symbols: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    current_path: str,
    errors: list[str],
    prefix: str,
) -> list[dict[str, str]]:
    callees = module.get("callees", [])
    if not isinstance(callees, list):
        errors.append(f"{prefix}9 MODULE_EDGE_INVALID: callees must be a list for {module.get('id')}.")
        return []
    allowed = {"DB": set(), "FUNC": {"FUNC", "DB"}, "API": {"FUNC"}}[kind]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for callee_id in callees:
        callee_key = str(callee_id)
        callee = modules.get(callee_key)
        symbol = symbols.get(callee_key)
        location = locations.get(callee_key)
        if callee_key in seen:
            errors.append(f"{prefix}9 MODULE_EDGE_INVALID: duplicate callee {callee_key}.")
            continue
        seen.add(callee_key)
        if (
            callee is None
            or symbol is None
            or symbol.get("kind") != "MODULE"
            or location is None
        ):
            errors.append(f"{prefix}9 MODULE_EDGE_INVALID: unknown callee {callee_key}.")
            continue
        callee_kind = str(callee.get("kind", ""))
        if callee_kind not in allowed:
            errors.append(
                f"{prefix}9 MODULE_EDGE_INVALID: {kind} cannot call {callee_kind} ({callee_key})."
            )
            continue
        target_path = str(location["path"])
        rows.append(
            {
                "module_id": callee_key,
                "symbol": str(symbol["symbol"]),
                "path": target_path,
                "specifier": _relative_specifier(current_path, target_path),
            }
        )
    return rows


def _database_rows(
    kind: str,
    module: dict[str, Any],
    symbols: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    current_path: str,
    errors: list[str],
) -> list[dict[str, str]]:
    if kind != "DB":
        return []
    effects = module.get("effects", [])
    entity_tables = {
        str(symbol.get("entity", "")): symbol
        for symbol in symbols.values()
        if symbol.get("kind") == "ENTITY_TABLE"
    }
    targets: list[str] = []
    for effect in effects:
        target = str(effect.get("target", "")) if isinstance(effect, dict) else ""
        if target not in targets:
            targets.append(target)
    rows: list[dict[str, str]] = []
    for target in sorted(targets):
        symbol = entity_tables.get(target)
        if symbol is None:
            errors.append(
                f"ARC3510 DB_EFFECT_INVALID: {module.get('id')} targets unknown entity {target!r}."
            )
            continue
        location = locations.get(str(symbol["id"]))
        if location is None:
            errors.append(f"ARC3510 DB_EFFECT_INVALID: no table file for {target}.")
            continue
        target_path = str(location["path"])
        rows.append(
            {
                "entity": target,
                "symbol": str(symbol["symbol"]),
                "path": target_path,
                "specifier": _relative_specifier(current_path, target_path),
            }
        )
    return rows


def _render_module(
    kind: str,
    module: dict[str, Any],
    function_symbol: dict[str, Any],
    input_contract: dict[str, Any] | None,
    output_contract: dict[str, Any] | None,
    dependencies: list[dict[str, str]],
    database_rows: list[dict[str, str]],
    current_path: str,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    function_name = str(function_symbol["symbol"])
    input_name = str(input_contract["symbol"]) if input_contract else None
    output_name = str(output_contract["symbol"]) if output_contract else None
    dependency_name = f"{function_name}Dependencies"
    exports = [function_name]
    if kind in {"FUNC", "API"}:
        exports.append(dependency_name)

    imports: list[dict[str, Any]] = []
    for contract, symbol in (
        (input_contract, input_name),
        (output_contract, output_name),
    ):
        if contract is not None and symbol is not None:
            imports.append(
                {
                    "symbol": symbol,
                    "from": str(contract["source_path"]),
                    "specifier": "@arc/shared",
                    "type_only": True,
                }
            )
    if kind == "API":
        imports.extend(
            {
                "symbol": symbol,
                "from": "express",
                "specifier": "express",
                "type_only": True,
            }
            for symbol in ("Request", "Response")
        )
        imports.append(
            {
                "symbol": "ApiErrorBody",
                "from": "shared/src/contracts/runtime.ts",
                "specifier": "@arc/shared",
                "type_only": True,
            }
        )
        imports.append(
            {
                "symbol": "sendError",
                "from": RUNTIME_ERRORS_PATH,
                "specifier": _relative_specifier(current_path, RUNTIME_ERRORS_PATH),
                "type_only": False,
            }
        )
    runtime_symbols = [
        {
            "symbol": symbol,
            "from": source_path,
            "specifier": _relative_specifier(current_path, source_path),
            "type_only": False,
        }
        for symbol, source_path in RUNTIME_IMPORTS
    ]
    imports.extend(runtime_symbols)
    # DB modules always use the single compiler-owned connection.  The table
    # imports below are schema descriptions only; keeping the connection as a
    # real import prevents the generated skeleton from accidentally treating a
    # Drizzle table as an in-memory repository.
    if kind == "DB" and database_rows:
        database_client_path = "backend/src/db/client.ts"
        imports.append(
            {
                "symbol": "database",
                "from": database_client_path,
                "specifier": _relative_specifier(current_path, database_client_path),
                "type_only": False,
            }
        )
        for operator in DRIZZLE_OPERATORS:
            imports.append(
                {
                    "symbol": operator,
                    "from": "drizzle-orm",
                    "specifier": "drizzle-orm",
                    "type_only": False,
                }
            )
    for row in [*dependencies, *database_rows]:
        imports.append(
            {
                "symbol": row["symbol"],
                "from": row["path"],
                "specifier": row["specifier"],
                "type_only": False,
            }
        )

    lines = ["// Generated by ARC. Edit only requirement-owned source bodies."]
    for type_only, specifier, names in _group_imports(imports):
        keyword = "import type" if type_only else "import"
        lines.append(f'{keyword} {{ {", ".join(names)} }} from "{specifier}";')
    lines.extend(
        [
            "",
            "/**",
            f" * @arc-owner {function_symbol.get('owner_requirement', '')}",
            f" * @arc-requirement {function_symbol.get('owner_requirement', '')}",
            f" * @arc-kind {kind}",
            " * @arc-generated skeleton",
            " */",
        ]
    )

    if kind in {"FUNC", "API"}:
        dependency_symbols = ", ".join(
            [
                *(row["symbol"] for row in dependencies),
                "HttpError",
                "newId",
                "now",
                "nowIso",
            ]
        )
        dependency_object = f"{{ {dependency_symbols} }}" if dependency_symbols else "{}"
        lines.extend(
            [
                f"export const {dependency_name} = {dependency_object} as const;",
                "",
            ]
        )
    elif database_rows:
        lines.extend(
            [
                "// The imported database is the compiler-owned Drizzle client.",
                "// Use database.select/insert/update/delete with the schema table imports.",
                "",
            ]
        )

    module_id = str(module.get("id", ""))
    if kind == "API":
        request_body = input_name or "unknown"
        response_body = f"{output_name} | ApiErrorBody" if output_name else "ApiErrorBody"
        # The envelope is compiler-owned: whatever the region throws leaves the
        # process as one ApiErrorBody, so the frontend never has to guess.
        #
        # An unimplemented route answers with the zero value of its own contract
        # rather than a 501.  A node whose screens merely traverse a not-yet-built
        # route then still renders, and the recorded stub hit lets the compiler tell
        # "my code is wrong" apart from "my dependency does not exist yet".
        lines.extend(
            [
                f"export async function {function_name}(",
                f"  req: Request<Record<string, string>, {response_body}, {request_body}>,",
                f"  res: Response<{response_body}>,",
                "): Promise<void> {",
                "  void req;",
                "  void res;",
                "  try {",
                f'    recordStubHit("{module_id}", res);',
                *_stub_response_lines(output_contract, output_name),
                "  } catch (error) {",
                "    sendError(res, error);",
                "  }",
                "}",
                "",
            ]
        )
        return "\n".join(lines), exports, imports

    parameter = f"input: {input_name}" if input_name else ""
    return_type = output_name or "void"
    lines.append(
        f"export async function {function_name}({parameter}): Promise<{return_type}> {{"
    )
    if input_name:
        lines.append("  void input;")
    lines.extend(
        [
            f'  recordStubHit("{module_id}");',
            f'  throw new NotImplementedError("{module_id}");',
            "}",
            "",
        ]
    )
    return "\n".join(lines), exports, imports


def _stub_response_lines(
    output_contract: dict[str, Any] | None,
    output_name: str | None,
) -> list[str]:
    """Render the skeleton reply for a route nobody has implemented yet."""

    fields = (output_contract or {}).get("fields")
    if output_name is None or not isinstance(fields, list) or not fields:
        return ["    res.status(204).end();", "    return;"]
    seen: set[str] = set()
    properties: list[str] = []
    for field_item in fields:
        if not isinstance(field_item, dict):
            continue
        name = str(field_item.get("name", "")).strip()
        # An optional property stays absent: exactOptionalPropertyTypes rejects an
        # explicit undefined, and a zero value would be indistinguishable from data.
        if not name or name in seen or not bool(field_item.get("required", True)):
            continue
        seen.add(name)
        zero = ZERO_VALUES.get(str(field_item.get("type", "")))
        if zero is None:
            return ["    res.status(204).end();", "    return;"]
        properties.append(f"      {json.dumps(name)}: {zero},")
    if not properties:
        return ["    res.status(204).end();", "    return;"]
    return [
        f"    const stub: {output_name} = {{",
        *properties,
        "    };",
        "    res.status(200).json(stub);",
        "    return;",
    ]


def _group_imports(
    imports: list[dict[str, Any]],
) -> list[tuple[bool, str, list[str]]]:
    grouped: dict[tuple[bool, str], set[str]] = {}
    for row in imports:
        key = (bool(row["type_only"]), str(row["specifier"]))
        grouped.setdefault(key, set()).add(str(row["symbol"]))
    return [
        (type_only, specifier, sorted(names))
        for (type_only, specifier), names in sorted(
            grouped.items(),
            key=lambda item: (item[0][1], item[0][0]),
        )
    ]


def _render_module_barrel(
    barrel_path: str,
    modules: list[dict[str, Any]],
) -> str:
    lines = ["// Generated by ARC. Do not edit."]
    for module in sorted(modules, key=lambda item: item["module_id"]):
        specifier = _relative_specifier(barrel_path, str(module["path"]))
        exports = ", ".join(module["exports"])
        lines.append(f'export {{ {exports} }} from "{specifier}";')
    return "\n".join(lines) + "\n"


def _render_runtime_errors() -> str:
    return "\n".join(
        [
            "// Generated by ARC. Do not edit.",
            'import type { ApiErrorBody, JsonValue } from "@arc/shared";',
            "",
            "export class NotImplementedError extends Error {",
            "  constructor(moduleId: string) {",
            "    super(`Module ${moduleId} is not implemented`);",
            '    this.name = "NotImplementedError";',
            "  }",
            "}",
            "",
            "/**",
            " * The one way a module reports a domain failure. Throw it from any DB, FUNC or",
            " * API implementation region; the generated API wrapper turns it into the shared",
            " * ApiErrorBody envelope with the right status code.",
            " */",
            "export class HttpError extends Error {",
            "  readonly status: number;",
            "  readonly code: string;",
            "  readonly details: JsonValue | undefined;",
            "",
            "  constructor(",
            "    status: number,",
            "    code: string,",
            "    message: string,",
            "    details?: JsonValue,",
            "  ) {",
            "    super(message);",
            '    this.name = "HttpError";',
            "    this.status = status;",
            "    this.code = code;",
            "    this.details = details;",
            "  }",
            "",
            "  static badRequest(message: string, details?: JsonValue): HttpError {",
            '    return new HttpError(400, "BAD_REQUEST", message, details);',
            "  }",
            "",
            "  static unauthorized(message: string, details?: JsonValue): HttpError {",
            '    return new HttpError(401, "UNAUTHORIZED", message, details);',
            "  }",
            "",
            "  static forbidden(message: string, details?: JsonValue): HttpError {",
            '    return new HttpError(403, "FORBIDDEN", message, details);',
            "  }",
            "",
            "  static notFound(message: string, details?: JsonValue): HttpError {",
            '    return new HttpError(404, "NOT_FOUND", message, details);',
            "  }",
            "",
            "  static conflict(message: string, details?: JsonValue): HttpError {",
            '    return new HttpError(409, "CONFLICT", message, details);',
            "  }",
            "",
            "  static unprocessable(message: string, details?: JsonValue): HttpError {",
            '    return new HttpError(422, "UNPROCESSABLE", message, details);',
            "  }",
            "}",
            "",
            "export function isHttpError(value: unknown): value is HttpError {",
            "  return value instanceof HttpError;",
            "}",
            "",
            "/** Normalize any thrown value into the shared failure envelope. */",
            "export function toErrorBody(error: unknown): {",
            "  status: number;",
            "  body: ApiErrorBody;",
            "} {",
            "  if (isHttpError(error)) {",
            "    return {",
            "      status: error.status,",
            "      body: {",
            "        error: {",
            "          code: error.code,",
            "          message: error.message,",
            "          ...(error.details === undefined ? {} : { details: error.details }),",
            "        },",
            "      },",
            "    };",
            "  }",
            "  if (error instanceof NotImplementedError) {",
            "    return {",
            "      status: 501,",
            '      body: { error: { code: "NOT_IMPLEMENTED", message: error.message } },',
            "    };",
            "  }",
            "  const message = error instanceof Error ? error.message : String(error);",
            "  return {",
            "    status: 500,",
            '    body: { error: { code: "INTERNAL_ERROR", message } },',
            "  };",
            "}",
            "",
            "/**",
            " * Structural view of the response object: express Response satisfies it without",
            " * this file importing express.",
            " */",
            "export interface ErrorResponseTarget {",
            "  headersSent: boolean;",
            "  status(code: number): { json(body: ApiErrorBody): unknown };",
            "}",
            "",
            "/** Write one failure envelope. Safe to call with any thrown value. */",
            "export function sendError(response: ErrorResponseTarget, error: unknown): void {",
            "  if (response.headersSent) return;",
            "  const { status, body } = toErrorBody(error);",
            "  response.status(status).json(body);",
            "}",
            "",
        ]
    )


def _relative_specifier(current_path: str, target_path: str) -> str:
    relative = posixpath.relpath(target_path, posixpath.dirname(current_path))
    if not relative.startswith("."):
        relative = f"./{relative}"
    return re.sub(r"\.ts$", ".js", relative)
