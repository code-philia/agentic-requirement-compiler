from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


SYMBOL_REGISTRY_SCHEMA_VERSION = 1
SYMBOLS_PLANNED = "SYMBOLS_PLANNED"
MODULE_KINDS = {"API", "FUNC", "DB"}
TYPESCRIPT_RESERVED_WORDS = {
    "any",
    "as",
    "async",
    "await",
    "boolean",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "constructor",
    "continue",
    "debugger",
    "declare",
    "default",
    "delete",
    "do",
    "else",
    "enum",
    "export",
    "extends",
    "false",
    "finally",
    "for",
    "from",
    "function",
    "get",
    "if",
    "implements",
    "import",
    "in",
    "infer",
    "instanceof",
    "interface",
    "is",
    "keyof",
    "let",
    "module",
    "namespace",
    "never",
    "new",
    "null",
    "number",
    "object",
    "of",
    "package",
    "private",
    "protected",
    "public",
    "readonly",
    "require",
    "return",
    "set",
    "static",
    "string",
    "super",
    "switch",
    "symbol",
    "this",
    "throw",
    "true",
    "try",
    "type",
    "typeof",
    "undefined",
    "unique",
    "unknown",
    "var",
    "void",
    "while",
    "with",
    "yield",
}


@dataclass(slots=True)
class SymbolPlanningResult:
    registry: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class GlobalSymbolPlanner:
    """Plan every public TypeScript symbol before file planning starts."""

    def __init__(self) -> None:
        self._symbols: dict[str, dict[str, Any]] = {}
        self._used_names: dict[str, str] = {}
        self._contract_ids: dict[str, str] = {}
        self._module_bindings: list[dict[str, Any]] = []
        self._semantic_values: dict[str, dict[str, Any]] = {}
        self._errors: list[str] = []

    def plan(
        self,
        design_ir: dict[str, Any],
        database_schema: dict[str, Any],
        project_manifest: dict[str, Any],
    ) -> SymbolPlanningResult:
        self._reset()
        self._validate_project_manifest(project_manifest)
        if self._errors:
            return self._result()

        requirements = design_ir.get("requirements", []) if isinstance(design_ir, dict) else []
        modules = design_ir.get("modules", []) if isinstance(design_ir, dict) else []
        entities = database_schema.get("entities", []) if isinstance(database_schema, dict) else []

        self._collect_requirement_semantics(requirements)
        self._plan_entities(entities)
        self._plan_modules(modules)
        self._validate_call_edges(modules)
        return self._result()

    def _reset(self) -> None:
        self._symbols.clear()
        self._used_names.clear()
        self._contract_ids.clear()
        self._module_bindings.clear()
        self._semantic_values.clear()
        self._errors.clear()

    def _validate_project_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict) or manifest.get("status") != "PROJECT_INITIALIZED":
            self._errors.append(
                "ARC3101 PROJECT_NOT_INITIALIZED: project-manifest.json is missing or not initialized."
            )
            return
        allowed = manifest.get("allowedOutputRoots", {}).get("skeleton", [])
        required = {"backend/src", "shared/src/contracts", "shared/src/index.ts"}
        if not isinstance(allowed, list) or not required <= {str(value) for value in allowed}:
            self._errors.append(
                "ARC3102 PROJECT_MANIFEST_INVALID: skeleton output roots are incomplete."
            )

    def _collect_requirement_semantics(self, requirements: Any) -> None:
        if not isinstance(requirements, list):
            self._errors.append("ARC3103 DESIGN_IR_INVALID: requirements must be a list.")
            return
        if any(not isinstance(value, dict) for value in requirements):
            self._errors.append(
                "ARC3103 DESIGN_IR_INVALID: requirements must contain only objects."
            )
        for item in sorted(
            (value for value in requirements if isinstance(value, dict)),
            key=lambda value: str(value.get("id", "")),
        ):
            requirement_id = str(item.get("id", "")).strip()
            if not requirement_id:
                self._errors.append(
                    "ARC3103 DESIGN_IR_INVALID: requirement id must not be empty."
                )
                continue
            contract = item.get("contract", {})
            if not isinstance(contract, dict):
                self._errors.append(
                    f"ARC3103 DESIGN_IR_INVALID: requirement {requirement_id!r} has no contract object."
                )
                continue
            for direction in ("inputs", "outputs"):
                self._collect_semantic_fields(
                    contract.get(direction, []),
                    source_id=requirement_id,
                    source_kind="REQUIREMENT",
                    direction=direction[:-1].upper(),
                )

    def _plan_entities(self, entities: Any) -> None:
        if not isinstance(entities, list):
            self._errors.append("ARC3104 DATABASE_SCHEMA_INVALID: entities must be a list.")
            return
        if any(not isinstance(value, dict) for value in entities):
            self._errors.append(
                "ARC3104 DATABASE_SCHEMA_INVALID: entities must contain only objects."
            )
        seen: set[str] = set()
        for entity in sorted(
            (value for value in entities if isinstance(value, dict)),
            key=lambda value: str(value.get("key", "")),
        ):
            key = str(entity.get("key", "")).strip()
            if not key or key in seen:
                self._errors.append(
                    f"ARC3105 ENTITY_SYMBOL_CONFLICT: invalid or duplicate entity key {key!r}."
                )
                continue
            seen.add(key)
            entity_name = _pascal_case(key)
            type_id = f"ENTITY.{key}"
            table_id = f"ENTITY_TABLE.{key}"
            self._register_symbol(
                type_id,
                {
                    "id": type_id,
                    "kind": "ENTITY_TYPE",
                    "typescript_kind": "type",
                    "entity": key,
                    "fields": copy.deepcopy(entity.get("fields", [])),
                },
                f"{entity_name}Record",
                qualifier=entity_name,
            )
            self._register_symbol(
                table_id,
                {
                    "id": table_id,
                    "kind": "ENTITY_TABLE",
                    "typescript_kind": "const",
                    "entity": key,
                },
                f"{_camel_case(key)}Table",
                qualifier=entity_name,
            )
            # Database fields participate in the global semantic registry too.
            # Their namespace is explicit so an API projection named ``id``
            # cannot collide with the persistent ``id`` field.
            for field_item in entity.get("fields", []):
                if not isinstance(field_item, dict):
                    continue
                field_name = str(field_item.get("name", "")).strip()
                field_type = str(field_item.get("type", "")).strip()
                if not field_name or not field_type:
                    continue
                self._record_semantic_value(
                    {
                        "semantic_id": field_name,
                        "name": field_name,
                        "type": field_type,
                        "required": not bool(field_item.get("nullable")),
                    },
                    source_id=key,
                    source_kind="DB",
                    direction="FIELD",
                )

    def _plan_modules(self, modules: Any) -> None:
        if not isinstance(modules, list):
            self._errors.append("ARC3103 DESIGN_IR_INVALID: modules must be a list.")
            return
        if any(not isinstance(value, dict) for value in modules):
            self._errors.append(
                "ARC3103 DESIGN_IR_INVALID: modules must contain only objects."
            )
        seen: set[str] = set()
        ordered = sorted(
            (value for value in modules if isinstance(value, dict)),
            key=lambda value: str(value.get("id", "")),
        )
        for module in ordered:
            module_id = str(module.get("id", "")).strip()
            module_kind = str(module.get("kind", "")).strip().upper()
            parsed = _parse_module_id(module_id)
            if not module_id or module_id in seen:
                self._errors.append(
                    f"ARC3106 MODULE_SYMBOL_CONFLICT: invalid or duplicate module id {module_id!r}."
                )
                continue
            seen.add(module_id)
            if parsed is None or module_kind not in MODULE_KINDS or parsed[1] != module_kind:
                self._errors.append(
                    f"ARC3107 MODULE_ID_INVALID: module {module_id!r} does not match kind {module_kind!r}."
                )
                continue
            owner_requirement, _, local_name = parsed
            declared_owner = str(module.get("owner_requirement", owner_requirement)).strip()
            if declared_owner and declared_owner != owner_requirement:
                self._errors.append(
                    f"ARC3108 MODULE_OWNER_CONFLICT: {module_id} owner is {declared_owner!r}."
                )
                continue

            function_symbol = self._register_symbol(
                module_id,
                {
                    "id": module_id,
                    "kind": "MODULE",
                    "typescript_kind": "function",
                    "module_kind": module_kind,
                    "owner_requirement": owner_requirement,
                },
                self._module_symbol_base(module, module_kind, local_name),
                qualifier=_pascal_case(owner_requirement),
            )
            input_contract = self._plan_contract(module, module_kind, local_name, "INPUT")
            output_contract = self._plan_contract(module, module_kind, local_name, "OUTPUT")
            self._module_bindings.append(
                {
                    "module_id": module_id,
                    "function_symbol": function_symbol,
                    "input_contract_id": input_contract,
                    "output_contract_id": output_contract,
                }
            )

    def _plan_contract(
        self,
        module: dict[str, Any],
        module_kind: str,
        local_name: str,
        direction: str,
    ) -> str | None:
        field_key = "inputs" if direction == "INPUT" else "outputs"
        fields = module.get(field_key, [])
        module_id = str(module["id"])
        if not isinstance(fields, list):
            self._errors.append(
                f"ARC3109 MODULE_INTERFACE_INVALID: {module_id} {field_key} must be a list."
            )
            return None
        normalized_fields: list[dict[str, Any]] = []
        seen_semantics: set[str] = set()
        for field_item in fields:
            normalized = self._normalize_interface_field(field_item, module_id, direction)
            if normalized is None:
                continue
            semantic_id = normalized["semantic_id"]
            if semantic_id in seen_semantics:
                self._errors.append(
                    f"ARC3110 MODULE_INTERFACE_DUPLICATE: {module_id} repeats {semantic_id}."
                )
                continue
            seen_semantics.add(semantic_id)
            normalized_fields.append(normalized)
        normalized_fields = self._coalesce_contract_field_names(
            normalized_fields,
            module_id=module_id,
            direction=direction,
        )
        if not normalized_fields:
            return None
        normalized_fields.sort(key=lambda value: value["semantic_id"])
        signature_payload = {
            "module_kind": module_kind,
            "direction": direction,
            "fields": normalized_fields,
        }
        signature = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        existing_id = self._contract_ids.get(signature)
        if existing_id is not None:
            consumers = self._symbols[existing_id].setdefault("consumers", [])
            if module_id not in consumers:
                consumers.append(module_id)
                consumers.sort()
            return existing_id

        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
        contract_id = f"CONTRACT.{module_kind}.{direction}.{digest}"
        self._contract_ids[signature] = contract_id
        symbol = self._register_symbol(
            contract_id,
            {
                "id": contract_id,
                "kind": "DATA_CONTRACT",
                "typescript_kind": "type",
                "module_kind": module_kind,
                "direction": direction,
                "fields": normalized_fields,
                "consumers": [module_id],
            },
            self._contract_symbol_base(module_kind, local_name, direction),
            qualifier=_pascal_case(module_id.partition("::")[0]),
        )
        self._symbols[contract_id]["symbol"] = symbol
        return contract_id

    def _coalesce_contract_field_names(
        self,
        fields: list[dict[str, Any]],
        *,
        module_id: str,
        direction: str,
    ) -> list[dict[str, Any]]:
        """Make the wire-level TypeScript property surface unambiguous.

        Different semantic values can legitimately refer to the same payload
        property (for example ``login.password`` and ``account.password`` in
        an authentication function).  Rendering both rows would emit invalid
        TypeScript.  Equal-name/equal-type rows are therefore one property;
        requiredness is the union of all semantic occurrences and the
        required semantic value remains the canonical binding.  Equal names
        with incompatible types are rejected before file lowering.
        """

        by_name: dict[str, dict[str, Any]] = {}
        result: list[dict[str, Any]] = []
        for field in fields:
            name = str(field.get("name", "")).strip()
            existing = by_name.get(name)
            if existing is None:
                by_name[name] = field
                result.append(field)
                continue
            if existing.get("type") != field.get("type"):
                self._errors.append(
                    f"ARC3115 MODULE_INTERFACE_NAME_CONFLICT: {module_id} {direction.lower()} "
                    f"maps property {name!r} to incompatible types "
                    f"{existing.get('type')!r} and {field.get('type')!r}."
                )
                continue
            # Prefer the required semantic field as canonical.  This keeps
            # authentication inputs such as `password` required even when a
            # stored-account projection also contributes an optional alias.
            if bool(field.get("required")) and not bool(existing.get("required")):
                field["required"] = True
                by_name[name] = field
                index = result.index(existing)
                result[index] = field
            else:
                existing["required"] = bool(existing.get("required")) or bool(
                    field.get("required")
                )
        return result

    def _normalize_interface_field(
        self,
        value: Any,
        source_id: str,
        direction: str,
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self._errors.append(
                f"ARC3109 MODULE_INTERFACE_INVALID: {source_id} contains a non-object field."
            )
            return None
        semantic_id = self._scope_semantic_id(
            str(value.get("semantic_id", "")).strip(), source_kind="MODULE", source_id=source_id
        )
        name = str(value.get("name", "")).strip()
        field_type = str(value.get("type", "")).strip()
        required = bool(value.get("required", True))
        if not semantic_id or not name or not field_type:
            self._errors.append(
                f"ARC3109 MODULE_INTERFACE_INVALID: {source_id} has an incomplete {direction} field."
            )
            return None
        normalized = {
            "semantic_id": semantic_id,
            "name": name,
            "type": field_type,
            "required": required,
        }
        self._record_semantic_value(
            normalized,
            source_id=source_id,
            source_kind="MODULE",
            direction=direction,
        )
        return normalized

    def _collect_semantic_fields(
        self,
        fields: Any,
        *,
        source_id: str,
        source_kind: str,
        direction: str,
    ) -> None:
        if not isinstance(fields, list):
            self._errors.append(
                f"ARC3103 DESIGN_IR_INVALID: {source_id} {direction.lower()} fields must be a list."
            )
            return
        for value in fields:
            if not isinstance(value, dict):
                self._errors.append(
                    f"ARC3103 DESIGN_IR_INVALID: {source_id} contains a non-object field."
                )
                continue
            normalized = {
                "semantic_id": str(value.get("semantic_id", "")).strip(),
                "name": str(value.get("name", "")).strip(),
                "type": str(value.get("type", "")).strip(),
                "required": bool(value.get("required", True)),
            }
            if not normalized["semantic_id"] or not normalized["type"]:
                self._errors.append(
                    f"ARC3103 DESIGN_IR_INVALID: {source_id} contains an incomplete semantic field."
                )
                continue
            self._record_semantic_value(
                normalized,
                source_id=source_id,
                source_kind=source_kind,
                direction=direction,
            )

    def _record_semantic_value(
        self,
        field_item: dict[str, Any],
        *,
        source_id: str,
        source_kind: str,
        direction: str,
    ) -> None:
        raw_semantic_id = str(field_item["semantic_id"])
        semantic_id = self._scope_semantic_id(raw_semantic_id, source_kind=source_kind, source_id=source_id)
        field_item["source_semantic_id"] = raw_semantic_id
        field_item["semantic_id"] = semantic_id
        field_type = str(field_item["type"])
        existing = self._semantic_values.get(semantic_id)
        if existing is None:
            existing = {
                "semantic_id": semantic_id,
                "type": field_type,
                "occurrences": [],
            }
            self._semantic_values[semantic_id] = existing
        elif existing["type"] != field_type:
            self._errors.append(
                f"ARC3111 SEMANTIC_TYPE_CONFLICT: {semantic_id} is both "
                f"{existing['type']!r} and {field_type!r}."
            )
            return
        occurrence = {
            "source_id": source_id,
            "source_kind": source_kind,
            "direction": direction,
            "name": str(field_item.get("name", "")),
            "required": bool(field_item.get("required", True)),
        }
        if occurrence not in existing["occurrences"]:
            existing["occurrences"].append(occurrence)

    @staticmethod
    def _scope_semantic_id(raw_id: str, *, source_kind: str, source_id: str) -> str:
        """Keep equal field names from unrelated sources in separate namespaces."""
        raw = raw_id.strip()
        if not raw:
            return raw
        if raw.startswith(("REQ.", "API.", "FUNC.", "DB.", "MODULE.")):
            return raw
        if source_kind == "REQUIREMENT":
            return f"REQ.{source_id}.{raw}"
        if source_kind == "MODULE":
            parsed = _parse_module_id(source_id)
            if parsed:
                _, module_kind, local_name = parsed
                return f"{module_kind}.{local_name}.{raw}"
            return f"MODULE.{source_id}.{raw}"
        return f"{source_kind}.{source_id}.{raw}"

    def _validate_call_edges(self, modules: Any) -> None:
        if not isinstance(modules, list):
            return
        module_ids = {
            symbol_id
            for symbol_id, symbol in self._symbols.items()
            if symbol.get("kind") == "MODULE"
        }
        for module in modules:
            if not isinstance(module, dict):
                continue
            module_id = str(module.get("id", ""))
            for field_name in ("callers", "callees"):
                references = module.get(field_name, [])
                if not isinstance(references, list):
                    self._errors.append(
                        f"ARC3112 MODULE_EDGE_INVALID: {module_id} {field_name} must be a list."
                    )
                    continue
                missing = sorted({str(value) for value in references if str(value) not in module_ids})
                if missing:
                    self._errors.append(
                        f"ARC3113 MODULE_SYMBOL_MISSING: {module_id} {field_name} reference {missing}."
                    )

    def _register_symbol(
        self,
        symbol_id: str,
        record: dict[str, Any],
        base_name: str,
        *,
        qualifier: str,
    ) -> str:
        if symbol_id in self._symbols:
            self._errors.append(f"ARC3114 SYMBOL_ID_CONFLICT: duplicate symbol id {symbol_id}.")
            return str(self._symbols[symbol_id].get("symbol", ""))
        candidate = _safe_typescript_identifier(base_name)
        if candidate in TYPESCRIPT_RESERVED_WORDS:
            candidate = f"{candidate}Symbol"
        original = candidate
        owner = self._used_names.get(candidate)
        if owner is not None and owner != symbol_id:
            suffix = _pascal_case(qualifier) or "Generated"
            candidate = f"{candidate}{suffix}"
            index = 2
            while candidate in self._used_names and self._used_names[candidate] != symbol_id:
                candidate = f"{original}{suffix}{index}"
                index += 1
            record["disambiguated_from"] = original
        self._used_names[candidate] = symbol_id
        record["symbol"] = candidate
        self._symbols[symbol_id] = record
        return candidate

    @staticmethod
    def _module_symbol_base(module: dict[str, Any], module_kind: str, local_name: str) -> str:
        local = _camel_case(local_name)
        if module_kind == "API":
            return f"{local}Handler"
        if module_kind == "DB":
            effects = [value for value in module.get("effects", []) if isinstance(value, dict)]
            if len(effects) == 1:
                operation = str(effects[0].get("operation", "")).upper()
                target = str(effects[0].get("target", "")).strip()
                if operation in {"CREATE", "UPDATE", "DELETE"} and target:
                    return f"{operation.lower()}{_pascal_case(target)}Record"
            return local
        return local

    @staticmethod
    def _contract_symbol_base(module_kind: str, local_name: str, direction: str) -> str:
        name = _pascal_case(local_name)
        suffixes = {
            ("API", "INPUT"): "Request",
            ("API", "OUTPUT"): "Response",
            ("FUNC", "INPUT"): "Command",
            ("FUNC", "OUTPUT"): "Result",
            ("DB", "INPUT"): "Input",
            ("DB", "OUTPUT"): "Result",
        }
        return f"{name}{suffixes[(module_kind, direction)]}"

    def _result(self) -> SymbolPlanningResult:
        for value in self._semantic_values.values():
            value["occurrences"].sort(
                key=lambda item: (
                    item["source_id"],
                    item["source_kind"],
                    item["direction"],
                    item["name"],
                )
            )
        registry = {
            "schema_version": SYMBOL_REGISTRY_SCHEMA_VERSION,
            "status": SYMBOLS_PLANNED if not self._errors else "SYMBOL_PLANNING_FAILED",
            "symbols": [copy.deepcopy(self._symbols[key]) for key in sorted(self._symbols)],
            "module_bindings": sorted(
                copy.deepcopy(self._module_bindings),
                key=lambda item: item["module_id"],
            ),
            "semantic_values": [
                copy.deepcopy(self._semantic_values[key]) for key in sorted(self._semantic_values)
            ],
        }
        return SymbolPlanningResult(registry=registry, errors=list(dict.fromkeys(self._errors)))


def _parse_module_id(module_id: str) -> tuple[str, str, str] | None:
    owner, separator, tail = module_id.partition("::")
    kind, dot, name = tail.partition(".")
    if not separator or not dot or not owner or kind not in MODULE_KINDS:
        return None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name) is None:
        return None
    return owner, kind, name


def _word_parts(value: str) -> list[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return [part for part in re.split(r"[^A-Za-z0-9]+", normalized) if part]


def _pascal_case(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in _word_parts(value))


def _camel_case(value: str) -> str:
    pascal = _pascal_case(value)
    return pascal[:1].lower() + pascal[1:] if pascal else "generated"


def _safe_typescript_identifier(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_$]", "", value)
    if not candidate:
        candidate = "generatedSymbol"
    if not re.match(r"[A-Za-z_$]", candidate):
        candidate = f"generated{candidate}"
    return candidate
