from __future__ import annotations

import copy
import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any


BACKEND_MANIFEST_SCHEMA_VERSION = 1
IMPORT_PLAN_SCHEMA_VERSION = 1
ROUTE_REGISTRY_SCHEMA_VERSION = 1
BACKEND_MANIFEST_GENERATED = "BACKEND_MANIFEST_GENERATED"

SYSTEM_GLUE_FILES = {
    "backend/src/db/client.ts",
    "backend/src/fixtures/index.ts",
    "backend/src/generated/router.ts",
    "backend/src/generated/module-registry.ts",
    "backend/src/generated/dependency-registry.ts",
    "backend/src/runtime/errors.ts",
    "backend/src/runtime/ids.ts",
    "backend/src/runtime/clock.ts",
    "backend/src/app.ts",
    "backend/src/server.ts",
    "backend/src/index.ts",
}

BARREL_FILES = {
    "shared/src/contracts/index.ts",
    "shared/src/index.ts",
    "backend/src/db/schema/index.ts",
    "backend/src/db/repositories/index.ts",
    "backend/src/functions/index.ts",
    "backend/src/api/index.ts",
    "backend/src/index.ts",
}

UPSTREAM_STATUSES = {
    "type": "TYPES_GENERATED",
    "database": "DATABASE_SCHEMA_LOWERED",
    "DB": "DB_MODULES_GENERATED",
    "FUNC": "FUNC_MODULES_GENERATED",
    "API": "API_MODULES_GENERATED",
}


@dataclass(slots=True)
class BackendGlueResult:
    manifest: dict[str, Any]
    import_plan: dict[str, Any]
    route_registry: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class BackendGlueLowerer:
    """Materialize all compiler-owned backend integration from frozen plans."""

    def lower(
        self,
        design_ir: dict[str, Any],
        symbol_registry: dict[str, Any],
        file_registry: dict[str, Any],
        upstream_manifests: dict[str, dict[str, Any]],
        *,
        default_port: int,
    ) -> BackendGlueResult:
        errors: list[str] = []
        modules = _index_modules(design_ir, errors)
        symbols = _index_symbols(symbol_registry, errors)
        locations = _index_locations(file_registry, errors)
        planned_files = _index_files(file_registry, errors)
        _validate_registry_statuses(symbol_registry, file_registry, errors)
        _validate_upstream_manifests(upstream_manifests, errors)
        _validate_required_files(planned_files, errors)

        module_rows = _index_module_manifests(upstream_manifests, modules, planned_files, errors)
        routes = _plan_routes(modules, module_rows.get("API", {}), errors)
        route_registry = {
            "schema_version": ROUTE_REGISTRY_SCHEMA_VERSION,
            "status": "ROUTES_PLANNED" if not errors else "ROUTE_PLANNING_FAILED",
            "routes": routes,
        }

        sources: dict[str, str] = {}
        glue_imports: dict[str, list[dict[str, Any]]] = {}
        glue_exports: dict[str, list[str]] = {}
        if not errors:
            initialization_statements = _database_initialization_statements(
                upstream_manifests.get("database", {}),
                errors,
            )
        if not errors:
            sources, glue_imports, glue_exports = _render_glue(
                routes,
                module_rows,
                default_port,
                initialization_statements,
            )

        import_plan = _build_import_plan(
            planned_files,
            symbols,
            locations,
            upstream_manifests,
            glue_imports,
            errors,
        )
        if errors:
            import_plan["status"] = "IMPORT_PLANNING_FAILED"

        manifest = _build_backend_manifest(
            planned_files,
            upstream_manifests,
            module_rows,
            routes,
            import_plan,
            glue_exports,
            sources,
            errors,
        )
        import_plan["status"] = "IMPORTS_PLANNED" if not errors else "IMPORT_PLANNING_FAILED"
        route_registry["status"] = "ROUTES_PLANNED" if not errors else "ROUTE_PLANNING_FAILED"
        return BackendGlueResult(
            manifest=manifest,
            import_plan=import_plan,
            route_registry=route_registry,
            sources={} if errors else sources,
            errors=list(dict.fromkeys(errors)),
        )


def _index_modules(
    design_ir: Any,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    rows = design_ir.get("modules", []) if isinstance(design_ir, dict) else []
    if not isinstance(rows, list):
        errors.append("ARC3801 DESIGN_IR_INVALID: modules must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        module_id = str(row.get("id", "")).strip() if isinstance(row, dict) else ""
        kind = str(row.get("kind", "")).strip() if isinstance(row, dict) else ""
        if not module_id or module_id in result or kind not in {"DB", "FUNC", "API"}:
            errors.append(f"ARC3801 DESIGN_IR_INVALID: invalid or duplicate module {module_id!r}.")
            continue
        result[module_id] = row
    return result


def _index_symbols(
    registry: Any,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    rows = registry.get("symbols", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append("ARC3802 SYMBOL_REGISTRY_INVALID: symbols must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol_id = str(row.get("id", "")).strip() if isinstance(row, dict) else ""
        symbol = str(row.get("symbol", "")).strip() if isinstance(row, dict) else ""
        if not symbol_id or not symbol or symbol_id in result:
            errors.append(f"ARC3802 SYMBOL_REGISTRY_INVALID: invalid symbol {symbol_id!r}.")
            continue
        result[symbol_id] = row
    return result


def _index_locations(
    registry: Any,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    rows = registry.get("symbol_locations", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append("ARC3803 FILE_REGISTRY_INVALID: symbol_locations must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol_id = str(row.get("symbol_id", "")).strip() if isinstance(row, dict) else ""
        path = str(row.get("path", "")).strip() if isinstance(row, dict) else ""
        if not symbol_id or not path or symbol_id in result:
            errors.append(f"ARC3803 FILE_REGISTRY_INVALID: invalid location for {symbol_id!r}.")
            continue
        result[symbol_id] = row
    return result


def _index_files(
    registry: Any,
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    rows = registry.get("files", []) if isinstance(registry, dict) else []
    if not isinstance(rows, list):
        errors.append("ARC3803 FILE_REGISTRY_INVALID: files must be a list.")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row.get("path", "")).strip() if isinstance(row, dict) else ""
        if not path or path in result:
            errors.append(f"ARC3803 FILE_REGISTRY_INVALID: invalid or duplicate file {path!r}.")
            continue
        result[path] = row
    return result


def _validate_registry_statuses(
    symbol_registry: Any,
    file_registry: Any,
    errors: list[str],
) -> None:
    if not isinstance(symbol_registry, dict) or symbol_registry.get("status") != "SYMBOLS_PLANNED":
        errors.append("ARC3802 SYMBOL_REGISTRY_INVALID: symbols are not planned.")
    if not isinstance(file_registry, dict) or file_registry.get("status") != "FILES_PLANNED":
        errors.append("ARC3803 FILE_REGISTRY_INVALID: files are not planned.")


def _validate_upstream_manifests(
    manifests: Any,
    errors: list[str],
) -> None:
    if not isinstance(manifests, dict):
        errors.append("ARC3804 UPSTREAM_MANIFEST_INVALID: manifest set must be an object.")
        return
    for name, expected_status in UPSTREAM_STATUSES.items():
        manifest = manifests.get(name)
        if not isinstance(manifest, dict) or manifest.get("status") != expected_status:
            errors.append(
                f"ARC3804 UPSTREAM_MANIFEST_INVALID: {name} must have status {expected_status}."
            )


def _database_initialization_statements(
    manifest: Any,
    errors: list[str],
) -> list[str]:
    initialization = manifest.get("initialization") if isinstance(manifest, dict) else None
    if not isinstance(initialization, dict):
        errors.append(
            "ARC3804 UPSTREAM_MANIFEST_INVALID: database initialization plan is missing."
        )
        return []
    if initialization.get("mode") != "create_if_not_exists":
        errors.append(
            "ARC3804 UPSTREAM_MANIFEST_INVALID: unsupported database initialization mode."
        )
        return []
    raw_statements = initialization.get("statements")
    if not isinstance(raw_statements, list):
        errors.append(
            "ARC3804 UPSTREAM_MANIFEST_INVALID: database initialization statements must be a list."
        )
        return []
    statements: list[str] = []
    for statement in raw_statements:
        if not isinstance(statement, str) or not statement.strip():
            errors.append(
                "ARC3804 UPSTREAM_MANIFEST_INVALID: database initialization contains an invalid statement."
            )
            continue
        statements.append(statement)
    return statements


def _validate_required_files(
    files: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    required = SYSTEM_GLUE_FILES | BARREL_FILES
    missing = sorted(required - set(files))
    if missing:
        errors.append(f"ARC3803 FILE_REGISTRY_INVALID: missing system files {missing}.")


def _index_module_manifests(
    manifests: dict[str, dict[str, Any]],
    modules: dict[str, dict[str, Any]],
    files: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {"DB": {}, "FUNC": {}, "API": {}}
    for kind in ("DB", "FUNC", "API"):
        manifest = manifests.get(kind, {})
        rows = manifest.get("modules", []) if isinstance(manifest, dict) else []
        if not isinstance(rows, list):
            errors.append(f"ARC3804 UPSTREAM_MANIFEST_INVALID: {kind} modules must be a list.")
            continue
        for row in rows:
            module_id = str(row.get("module_id", "")).strip() if isinstance(row, dict) else ""
            path = str(row.get("path", "")).strip() if isinstance(row, dict) else ""
            if (
                not module_id
                or module_id in result[kind]
                or module_id not in modules
                or modules[module_id].get("kind") != kind
                or path not in files
            ):
                errors.append(
                    f"ARC3804 UPSTREAM_MANIFEST_INVALID: invalid {kind} module row {module_id!r}."
                )
                continue
            result[kind][module_id] = row
        expected = {
            module_id
            for module_id, module in modules.items()
            if module.get("kind") == kind
        }
        if set(result[kind]) != expected:
            errors.append(
                f"ARC3804 UPSTREAM_MANIFEST_INVALID: {kind} coverage differs: "
                f"missing={sorted(expected - set(result[kind]))}, "
                f"unknown={sorted(set(result[kind]) - expected)}."
            )
    return result


def _plan_routes(
    modules: dict[str, dict[str, Any]],
    api_rows: dict[str, dict[str, Any]],
    errors: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    by_base_path: dict[str, list[str]] = {}
    for module_id, row in sorted(api_rows.items()):
        parsed = _parse_module_id(module_id)
        if parsed is None or parsed[1] != "API":
            errors.append(f"ARC3805 ROUTE_DERIVATION_FAILED: invalid API module id {module_id!r}.")
            continue
        owner, _, local_name = parsed
        base_path = f"/api/{_kebab_case(local_name)}"
        by_base_path.setdefault(base_path, []).append(module_id)
        module = modules[module_id]
        method, method_rule = _derive_http_method(module)
        candidates.append(
            {
                "route_id": f"ROUTE::{module_id}",
                "module_id": module_id,
                "method": method,
                "base_path": base_path,
                "owner_requirement": owner,
                "handler_symbol": str(row.get("function_symbol", "")),
                "handler_path": str(row.get("path", "")),
                "input_source": "query" if method in {"GET", "DELETE"} else "body",
                "derivation": {
                    "method": method_rule,
                    "path": "API_MODULE_NAME",
                },
            }
        )

    routes: list[dict[str, Any]] = []
    for candidate in candidates:
        base_path = str(candidate.pop("base_path"))
        collisions = by_base_path[base_path]
        if len(collisions) == 1:
            path = base_path
        else:
            owner_segment = _kebab_case(str(candidate["owner_requirement"]))
            path = f"/api/{owner_segment}/{base_path.removeprefix('/api/')}"
            candidate["derivation"]["path"] = "API_MODULE_NAME_WITH_REQUIREMENT_DISAMBIGUATION"
        candidate["path"] = path
        routes.append(candidate)

    seen: set[tuple[str, str]] = set()
    for route in routes:
        key = (str(route["method"]), str(route["path"]))
        if key in seen:
            errors.append(
                f"ARC3806 ROUTE_CONFLICT: duplicate route {key[0]} {key[1]}."
            )
        seen.add(key)
        if not route["handler_symbol"] or not route["handler_path"]:
            errors.append(
                f"ARC3805 ROUTE_DERIVATION_FAILED: handler binding missing for {route['module_id']}."
            )
    return sorted(routes, key=lambda item: (item["path"], item["method"], item["module_id"]))


def _derive_http_method(module: dict[str, Any]) -> tuple[str, str]:
    effects = module.get("effects", [])
    operations = {
        str(effect.get("operation", "")).upper()
        for effect in effects
        if isinstance(effect, dict) and effect.get("operation")
    }
    if operations and operations <= {"READ"}:
        return "GET", "READ_ONLY_EFFECTS"
    if "DELETE" in operations and operations <= {"READ", "DELETE"}:
        return "DELETE", "DELETE_EFFECT"
    if "UPDATE" in operations and operations <= {"READ", "UPDATE"}:
        return "PATCH", "UPDATE_EFFECT"
    inputs = module.get("inputs", [])
    if not operations and isinstance(inputs, list) and not inputs:
        return "GET", "PURE_NO_INPUT_OPERATION"
    return "POST", "COMMAND_OR_MIXED_EFFECTS"


def _render_glue(
    routes: list[dict[str, Any]],
    module_rows: dict[str, dict[str, dict[str, Any]]],
    default_port: int,
    initialization_statements: list[str],
) -> tuple[
    dict[str, str],
    dict[str, list[dict[str, Any]]],
    dict[str, list[str]],
]:
    sources: dict[str, str] = {}
    imports: dict[str, list[dict[str, Any]]] = {}
    exports: dict[str, list[str]] = {}

    # Cross-cutting runtime the compiler owns outright: identifier minting and the
    # clock.  Implementation regions import these instead of inventing their own,
    # so identity and time stay deterministic and testable across every module.
    ids_path = "backend/src/runtime/ids.ts"
    ids_imports = [_import("randomUUID", "node:crypto")]
    sources[ids_path] = _render_runtime_ids(ids_imports)
    imports[ids_path] = ids_imports
    exports[ids_path] = ["newId"]

    clock_path = "backend/src/runtime/clock.ts"
    sources[clock_path] = _render_runtime_clock()
    imports[clock_path] = []
    exports[clock_path] = ["now", "nowIso"]

    database_path = "backend/src/db/client.ts"
    database_imports = [
        _import("Database", "better-sqlite3", style="default"),
        _import("drizzle", "drizzle-orm/better-sqlite3"),
        _import("mkdirSync", "node:fs"),
        _import("dirname", "node:path"),
        _import("resolve", "node:path"),
        _import("schema", "./schema/index.js", style="namespace", source="backend/src/db/schema/index.ts"),
    ]
    sources[database_path] = _render_database_client(
        database_imports,
        initialization_statements,
    )
    imports[database_path] = database_imports
    exports[database_path] = ["database", "sqliteDatabase", "resetDatabase"]

    router_path = "backend/src/generated/router.ts"
    router_imports = [_import("Router", "express")]
    for route in routes:
        router_imports.append(
            _import(
                str(route["handler_symbol"]),
                _relative_specifier(router_path, str(route["handler_path"])),
                source=str(route["handler_path"]),
            )
        )
    sources[router_path] = _render_router(router_imports, routes)
    imports[router_path] = router_imports
    exports[router_path] = ["router"]

    all_modules = {
        module_id: row
        for kind in ("DB", "FUNC", "API")
        for module_id, row in module_rows[kind].items()
    }
    module_registry_path = "backend/src/generated/module-registry.ts"
    module_registry_imports = [
        _import(
            str(row["function_symbol"]),
            _relative_specifier(module_registry_path, str(row["path"])),
            source=str(row["path"]),
        )
        for _, row in sorted(all_modules.items())
    ]
    sources[module_registry_path] = _render_registry(
        "moduleRegistry",
        [(module_id, str(row["function_symbol"])) for module_id, row in sorted(all_modules.items())],
        module_registry_imports,
    )
    imports[module_registry_path] = module_registry_imports
    exports[module_registry_path] = ["moduleRegistry"]

    dependency_rows = {
        module_id: row
        for kind in ("FUNC", "API")
        for module_id, row in module_rows[kind].items()
    }
    dependency_registry_path = "backend/src/generated/dependency-registry.ts"
    dependency_registry_imports: list[dict[str, Any]] = []
    dependency_entries: list[tuple[str, str]] = []
    for module_id, row in sorted(dependency_rows.items()):
        row_exports = row.get("exports", [])
        dependency_symbol = str(row_exports[1]) if isinstance(row_exports, list) and len(row_exports) > 1 else ""
        if dependency_symbol:
            dependency_registry_imports.append(
                _import(
                    dependency_symbol,
                    _relative_specifier(dependency_registry_path, str(row["path"])),
                    source=str(row["path"]),
                )
            )
            dependency_entries.append((module_id, dependency_symbol))
    sources[dependency_registry_path] = _render_registry(
        "dependencyRegistry",
        dependency_entries,
        dependency_registry_imports,
    )
    imports[dependency_registry_path] = dependency_registry_imports
    exports[dependency_registry_path] = ["dependencyRegistry"]

    app_path = "backend/src/app.ts"
    app_imports = [
        _import("express", "express", style="default"),
        _import("ErrorRequestHandler", "express", type_only=True),
        _import("fileURLToPath", "node:url"),
        _import("sqliteDatabase", "./db/client.js", source=database_path),
        _import("resetDatabase", "./db/client.js", source=database_path),
        _import("seedFor", "./fixtures/index.js", source="backend/src/fixtures/index.ts"),
        _import("router", "./generated/router.js", source=router_path),
        _import("toErrorBody", "./runtime/errors.js", source="backend/src/runtime/errors.ts"),
    ]
    sources[app_path] = _render_app(app_imports)
    imports[app_path] = app_imports
    exports[app_path] = ["app"]

    server_path = "backend/src/server.ts"
    server_imports = [_import("app", "./app.js", source=app_path)]
    sources[server_path] = _render_server(server_imports, default_port)
    imports[server_path] = server_imports
    exports[server_path] = ["port", "server"]

    root_barrel_path = "backend/src/index.ts"
    sources[root_barrel_path] = _render_backend_barrel()
    imports[root_barrel_path] = []
    exports[root_barrel_path] = [
        "apiModules",
        "app",
        "database",
        "dbRepositories",
        "dbSchema",
        "dependencyRegistry",
        "functionModules",
        "moduleRegistry",
        "router",
        "sqliteDatabase",
    ]
    return sources, imports, exports


def _render_runtime_ids(imports: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "// Generated by ARC. Do not edit.",
            *_render_imports(imports),
            "",
            "/** Mint one opaque identifier. Every generated row id comes from here. */",
            "export function newId(prefix?: string): string {",
            "  const value = randomUUID();",
            "  return prefix ? `${prefix}_${value}` : value;",
            "}",
            "",
        ]
    )


def _render_runtime_clock() -> str:
    return "\n".join(
        [
            "// Generated by ARC. Do not edit.",
            "",
            "/** Current wall-clock time. Tests can freeze it through ARC_FIXED_NOW. */",
            "export function now(): Date {",
            '  const fixed = (process.env.ARC_FIXED_NOW ?? "").trim();',
            "  if (!fixed) return new Date();",
            "  const parsed = new Date(fixed);",
            "  return Number.isNaN(parsed.getTime()) ? new Date() : parsed;",
            "}",
            "",
            "/** Current time as an ISO-8601 string, the only timestamp format ARC persists. */",
            "export function nowIso(): string {",
            "  return now().toISOString();",
            "}",
            "",
        ]
    )


def _render_database_client(
    imports: list[dict[str, Any]],
    initialization_statements: list[str],
) -> str:
    lines = ["// Generated by ARC. Do not edit.", *_render_imports(imports), ""]
    lines.extend(
        [
            'const databaseUrl = (process.env.DATABASE_URL ?? "./data/app.db").trim();',
            'if (!databaseUrl) throw new Error("DATABASE_URL must not be empty.");',
            'if (databaseUrl !== ":memory:") {',
            "  mkdirSync(dirname(resolve(databaseUrl)), { recursive: true });",
            "}",
            "",
            "export const sqliteDatabase = new Database(databaseUrl);",
            'sqliteDatabase.pragma("foreign_keys = ON");',
            "",
            "const schemaInitializationStatements = [",
            *[
                f"  {json.dumps(statement, ensure_ascii=False)},"
                for statement in initialization_statements
            ],
            "] as const;",
            "",
            "const initializeSchema = sqliteDatabase.transaction(() => {",
            "  for (const statement of schemaInitializationStatements) {",
            "    sqliteDatabase.exec(statement);",
            "  }",
            "});",
            "try {",
            "  initializeSchema();",
            "} catch (error) {",
            "  sqliteDatabase.close();",
            '  const detail = error instanceof Error ? error.message : String(error);',
            '  throw new Error(`[ARC] database initialization failed: ${detail}`);',
            "}",
            "",
            "export const database = drizzle(sqliteDatabase, { schema });",
            "",
            "export function resetDatabase(): void {",
            "  sqliteDatabase.pragma(\"foreign_keys = OFF\");",
            "  try {",
            "    const tables = sqliteDatabase.prepare(\"SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'\").all() as Array<{ name: string }>;",
            "    const reset = sqliteDatabase.transaction(() => {",
            "      for (const table of tables) {",
            '        const escaped = table.name.replaceAll(\'"\', \'""\');',
            "        sqliteDatabase.prepare('DELETE FROM \"' + escaped + '\"').run();",
            "      }",
            "      const sequenceTable = sqliteDatabase.prepare(\"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'\").get();",
            "      if (sequenceTable) sqliteDatabase.prepare('DELETE FROM \"sqlite_sequence\"').run();",
            "    });",
            "    reset();",
            "  } finally {",
            "    sqliteDatabase.pragma(\"foreign_keys = ON\");",
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def render_database_client(initialization_statements: list[str]) -> str:
    """The same compiler-owned database client is used before and after backend lowering."""

    return _render_database_client([
        _import("Database", "better-sqlite3", style="default"),
        _import("drizzle", "drizzle-orm/better-sqlite3"),
        _import("mkdirSync", "node:fs"),
        _import("dirname", "node:path"),
        _import("resolve", "node:path"),
        _import("schema", "./schema/index.js", style="namespace", source="backend/src/db/schema/index.ts"),
    ], initialization_statements)


def _render_router(
    imports: list[dict[str, Any]],
    routes: list[dict[str, Any]],
) -> str:
    lines = ["// Generated by ARC. Do not edit.", *_render_imports(imports), "", "export const router = Router();"]
    for route in routes:
        lines.append(
            f'router.{str(route["method"]).lower()}("{route["path"]}", {route["handler_symbol"]});'
        )
    lines.append("")
    return "\n".join(lines)


def _render_registry(
    registry_name: str,
    entries: list[tuple[str, str]],
    imports: list[dict[str, Any]],
) -> str:
    lines = ["// Generated by ARC. Do not edit.", *_render_imports(imports)]
    if imports:
        lines.append("")
    if not entries:
        lines.extend([f"export const {registry_name} = {{}} as const;", ""])
        return "\n".join(lines)
    lines.append(f"export const {registry_name} = {{")
    for module_id, symbol in entries:
        lines.append(f"  {json.dumps(module_id, ensure_ascii=False)}: {symbol},")
    lines.extend(["} as const;", ""])
    return "\n".join(lines)


def _render_app(imports: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "// Generated by ARC. Do not edit.",
            *_render_imports(imports),
            "",
            'const frontendDist = fileURLToPath(new URL("../../frontend/dist/", import.meta.url));',
            'const frontendIndex = fileURLToPath(new URL("../../frontend/dist/index.html", import.meta.url));',
            "void sqliteDatabase; // Opening the app requires a ready, initialized database.",
            "",
            "export const app = express();",
            "app.use(express.json());",
            'app.get("/__arc/health", (_request, response) => {',
            '  response.status(200).json({ status: "ok", service: "arc-backend" });',
            "});",
            'app.post("/__arc/reset", (_request, response) => {',
            '  if (process.env.NODE_ENV !== "test") {',
            '    response.status(404).json({ error: { code: "NOT_FOUND", message: "Not found" } });',
            "    return;",
            "  }",
            "  resetDatabase();",
            "  response.status(204).end();",
            "});",
            'app.post("/__arc/seed", (request, response) => {',
            '  if (process.env.NODE_ENV !== "test") {',
            '    response.status(404).json({ error: { code: "NOT_FOUND", message: "Not found" } });',
            "    return;",
            "  }",
            '  const requirementId = typeof request.body?.requirement_id === "string" ? request.body.requirement_id : "";',
            '  if (!requirementId) { response.status(400).json({ error: { code: "BAD_REQUEST", message: "requirement_id is required" } }); return; }',
            "  seedFor(requirementId);",
            "  response.status(204).end();",
            "});",
            "app.use(router);",
            "app.use(express.static(frontendDist));",
            "app.use((request, response, next) => {",
            '  if (request.method !== "GET" || request.path === "/api" || request.path.startsWith("/api/")) {',
            "    next();",
            "    return;",
            "  }",
            "  response.sendFile(frontendIndex, (error) => {",
            '    if (error) next(new Error("Frontend build is unavailable. Run npm run build -w @arc/frontend or start Vite with npm run dev -w @arc/frontend."));',
            "  });",
            "});",
            "const errorHandler: ErrorRequestHandler = (error, _request, response, _next) => {",
            '  console.error("[ARC] request failed:", error);',
            "  const { status, body } = toErrorBody(error);",
            "  if (response.headersSent) return;",
            "  response.status(status === 500 ? 503 : status).json(body);",
            "};",
            "app.use(errorHandler);",
            "",
        ]
    )


def _render_server(imports: list[dict[str, Any]], default_port: int) -> str:
    port = max(1, min(65535, int(default_port)))
    return "\n".join(
        [
            "// Generated by ARC. Do not edit.",
            *_render_imports(imports),
            "",
            f'const configuredPort = Number.parseInt(process.env.PORT ?? "{port}", 10);',
            f"export const port = Number.isInteger(configuredPort) ? configuredPort : {port};",
            'const host = process.env.NODE_ENV === "test" ? "127.0.0.1" : "0.0.0.0";',
            "export const server = app.listen(port, host, () => {",
            '  console.log(`[ARC] backend listening on http://127.0.0.1:${port}`);',
            '  console.log(`[ARC] health check: http://127.0.0.1:${port}/__arc/health`);',
            "});",
            "server.on(\"error\", (error) => {",
            '  console.error(`[ARC] backend failed to listen on port ${port}:`, error);',
            "  process.exitCode = 1;",
            "});",
            "",
        ]
    )


def _render_backend_barrel() -> str:
    return (
        "// Generated by ARC. Do not edit.\n"
        'export * as apiModules from "./api/index.js";\n'
        'export * as functionModules from "./functions/index.js";\n'
        'export * as dbRepositories from "./db/repositories/index.js";\n'
        'export * as dbSchema from "./db/schema/index.js";\n'
        'export { database, resetDatabase, sqliteDatabase } from "./db/client.js";\n'
        'export { router } from "./generated/router.js";\n'
        'export { moduleRegistry } from "./generated/module-registry.js";\n'
        'export { dependencyRegistry } from "./generated/dependency-registry.js";\n'
        'export { app } from "./app.js";\n'
    )


def _build_import_plan(
    files: dict[str, dict[str, Any]],
    symbols: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    manifests: dict[str, dict[str, Any]],
    glue_imports: dict[str, list[dict[str, Any]]],
    errors: list[str],
) -> dict[str, Any]:
    imports_by_path: dict[str, list[dict[str, Any]]] = {path: [] for path in files}

    contract_files: dict[str, list[dict[str, Any]]] = {}
    for symbol_id, symbol in symbols.items():
        if symbol.get("kind") != "DATA_CONTRACT":
            continue
        location = locations.get(symbol_id)
        if location is None:
            errors.append(f"ARC3807 IMPORT_PLAN_INVALID: missing contract location for {symbol_id}.")
            continue
        contract_files.setdefault(str(location["path"]), []).append(symbol)
    runtime_path = "shared/src/contracts/runtime.ts"
    for path, rows in contract_files.items():
        uses_json = any(
            field_item.get("type") == "json"
            for symbol in rows
            for field_item in symbol.get("fields", [])
            if isinstance(field_item, dict)
        )
        if uses_json:
            imports_by_path.setdefault(path, []).append(
                _import(
                    "JsonValue",
                    _relative_specifier(path, runtime_path),
                    type_only=True,
                    source=runtime_path,
                )
            )

    database_manifest = manifests.get("database", {})
    for table in database_manifest.get("tables", []) if isinstance(database_manifest, dict) else []:
        if not isinstance(table, dict):
            continue
        path = str(table.get("path", ""))
        table_imports = table.get("imports", [])
        if isinstance(table_imports, list):
            imports_by_path.setdefault(path, []).extend(
                copy.deepcopy(row) for row in table_imports if isinstance(row, dict)
            )

    for kind in ("DB", "FUNC", "API"):
        manifest = manifests.get(kind, {})
        for module in manifest.get("modules", []) if isinstance(manifest, dict) else []:
            if not isinstance(module, dict):
                continue
            path = str(module.get("path", ""))
            rows = module.get("imports", [])
            if isinstance(rows, list):
                imports_by_path.setdefault(path, []).extend(
                    _normalize_module_import(row)
                    for row in rows
                    if isinstance(row, dict)
                )

    for path, rows in glue_imports.items():
        imports_by_path.setdefault(path, []).extend(copy.deepcopy(rows))

    file_rows = [
        {
            "path": path,
            "imports": _deduplicate_imports(imports_by_path.get(path, [])),
        }
        for path in sorted(files)
    ]
    for file_row in file_rows:
        for import_row in file_row["imports"]:
            source = str(import_row.get("from", ""))
            if source.startswith(("backend/src/", "shared/src/")) and source not in files:
                errors.append(
                    "ARC3807 IMPORT_PLAN_INVALID: "
                    f"{file_row['path']} imports unknown source {source}."
                )
    return {
        "schema_version": IMPORT_PLAN_SCHEMA_VERSION,
        "status": "IMPORTS_PLANNED" if not errors else "IMPORT_PLANNING_FAILED",
        "files": file_rows,
    }


def _build_backend_manifest(
    files: dict[str, dict[str, Any]],
    manifests: dict[str, dict[str, Any]],
    module_rows: dict[str, dict[str, dict[str, Any]]],
    routes: list[dict[str, Any]],
    import_plan: dict[str, Any],
    glue_exports: dict[str, list[str]],
    sources: dict[str, str],
    errors: list[str],
) -> dict[str, Any]:
    imports_by_path = {
        str(row.get("path", "")): copy.deepcopy(row.get("imports", []))
        for row in import_plan.get("files", [])
        if isinstance(row, dict)
    }
    exports_by_path: dict[str, set[str]] = {
        path: {str(value) for value in row.get("exports", [])}
        for path, row in files.items()
    }
    for kind in ("DB", "FUNC", "API"):
        for row in module_rows[kind].values():
            exports_by_path.setdefault(str(row["path"]), set()).update(
                str(value) for value in row.get("exports", [])
            )
    for path, values in glue_exports.items():
        exports_by_path.setdefault(path, set()).update(values)
    exports_by_path.setdefault("backend/src/runtime/errors.ts", set()).update(
        {"HttpError", "NotImplementedError", "isHttpError", "sendError", "toErrorBody"}
    )
    exports_by_path.setdefault("shared/src/contracts/runtime.ts", set()).update(
        {"ApiErrorBody", "ApiErrorDetail", "JsonPrimitive", "JsonValue"}
    )

    type_manifest = manifests.get("type", {})
    contract_exports = {
        str(row.get("symbol", ""))
        for row in type_manifest.get("definitions", [])
        if isinstance(row, dict)
        and str(row.get("path", "")).startswith("shared/src/contracts/")
        and row.get("symbol")
    }
    exports_by_path.setdefault("shared/src/contracts/index.ts", set()).update(contract_exports)
    exports_by_path.setdefault("shared/src/index.ts", set()).update(contract_exports)

    database_manifest = manifests.get("database", {})
    schema_exports = {
        str(value)
        for row in database_manifest.get("tables", [])
        if isinstance(row, dict)
        for value in (row.get("record_symbol"), row.get("table_symbol"))
        if value
    }
    exports_by_path.setdefault("backend/src/db/schema/index.ts", set()).update(schema_exports)
    module_barrels = {
        "DB": "backend/src/db/repositories/index.ts",
        "FUNC": "backend/src/functions/index.ts",
        "API": "backend/src/api/index.ts",
    }
    for kind, barrel_path in module_barrels.items():
        exports_by_path.setdefault(barrel_path, set()).update(
            str(value)
            for row in module_rows[kind].values()
            for value in row.get("exports", [])
        )

    generated_files = {
        str(path)
        for manifest in manifests.values()
        if isinstance(manifest, dict)
        for path in manifest.get("generated_files", [])
    } | set(sources)
    missing_generated = sorted(set(files) - generated_files)
    unknown_generated = sorted(generated_files - set(files))
    if missing_generated or unknown_generated:
        errors.append(
            "ARC3808 BACKEND_FILE_COVERAGE_INVALID: generated files differ from File Registry: "
            f"missing={missing_generated}, unknown={unknown_generated}."
        )
    file_rows = []
    for path, row in sorted(files.items()):
        file_rows.append(
            {
                "path": path,
                "role": row.get("role"),
                "owner": row.get("owner"),
                "mutability": row.get("mutability"),
                "origin": row.get("origin"),
                "symbol_ids": copy.deepcopy(row.get("symbol_ids", [])),
                "module_ids": copy.deepcopy(row.get("module_ids", [])),
                "exports": sorted(exports_by_path.get(path, set())),
                "imports": imports_by_path.get(path, []),
            }
        )

    all_module_rows = [
        copy.deepcopy(row)
        for kind in ("DB", "FUNC", "API")
        for _, row in sorted(module_rows[kind].items())
    ]
    return {
        "schema_version": BACKEND_MANIFEST_SCHEMA_VERSION,
        "status": BACKEND_MANIFEST_GENERATED if not errors else "BACKEND_MANIFEST_GENERATION_FAILED",
        "modules": all_module_rows,
        "routes": copy.deepcopy(routes),
        "deployment": {
            "working_directory": "backend",
            "start_command": "npm run start",
            "server_entry": "backend/dist/server.js",
            "frontend_dist": "frontend/dist",
            "spa_fallback": "frontend/dist/index.html",
            "database": {
                "client": "backend/src/db/client.ts",
                "url_environment_variable": "DATABASE_URL",
                "default_url": "./data/app.db",
                "initialization_mode": database_manifest.get("initialization", {}).get("mode"),
                "same_process_initialization": True,
            },
        },
        "files": file_rows,
    }


def _import(
    symbol: str,
    specifier: str,
    *,
    style: str = "named",
    type_only: bool = False,
    source: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "specifier": specifier,
        "import_style": style,
        "type_only": type_only,
    }
    if source is not None:
        row["from"] = source
    return row


def _normalize_module_import(row: dict[str, Any]) -> dict[str, Any]:
    return _import(
        str(row.get("symbol", "")),
        str(row.get("specifier", "")),
        type_only=bool(row.get("type_only")),
        source=str(row.get("from", "")) or None,
    )


def _deduplicate_imports(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("specifier", "")),
            str(row.get("symbol", "")),
            str(row.get("import_style", "named")),
            bool(row.get("type_only")),
        )
        if not key[0] or not key[1]:
            continue
        result[key] = copy.deepcopy(row)
    return [result[key] for key in sorted(result)]


def _render_imports(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in _deduplicate_imports(rows):
        symbol = str(row["symbol"])
        specifier = str(row["specifier"])
        style = str(row.get("import_style", "named"))
        if style == "default":
            lines.append(f'import {symbol} from "{specifier}";')
        elif style == "namespace":
            lines.append(f'import * as {symbol} from "{specifier}";')
        else:
            keyword = "import type" if row.get("type_only") else "import"
            lines.append(f'{keyword} {{ {symbol} }} from "{specifier}";')
    return lines


def _relative_specifier(current_path: str, target_path: str) -> str:
    relative = posixpath.relpath(target_path, posixpath.dirname(current_path))
    if not relative.startswith("."):
        relative = f"./{relative}"
    return re.sub(r"\.ts$", ".js", relative)


def _parse_module_id(module_id: str) -> tuple[str, str, str] | None:
    owner, separator, tail = module_id.partition("::")
    kind, dot, name = tail.partition(".")
    if not separator or not dot or not owner or kind not in {"DB", "FUNC", "API"} or not name:
        return None
    return owner, kind, name


def _kebab_case(value: str) -> str:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    words = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", normalized) if word]
    return "-".join(words) or "operation"
