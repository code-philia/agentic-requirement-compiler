"""Compile requirement seed declarations into validated, compiler-owned Fixture IR."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging import SynchronousLog

from .database_stage import schema_for_requirement
from .model_client import StructuredModel, describe_model_error


FIXTURE_IR_SCHEMA_VERSION = 1
FIXTURE_STATUS = "FIXTURES_FROZEN"

FIXTURE_VALUE_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"type": "string"},
        {"type": "integer"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["fixture_key", "field"],
            "properties": {
                "fixture_key": {"type": "string"},
                "field": {"type": "string"},
            },
        },
    ]
}

FIXTURE_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fixture_sets"],
    "properties": {
        "fixture_sets": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "rows"],
                "properties": {
                    "name": {"type": "string"},
                    "rows": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["entity_key", "fixture_key", "values"],
                            "properties": {
                                "entity_key": {"type": "string"},
                                "fixture_key": {"type": "string"},
                                "values": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["field", "value"],
                                        "properties": {
                                            "field": {"type": "string"},
                                            "value": FIXTURE_VALUE_SCHEMA,
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}

FIXTURE_INSTRUCTIONS = """You are a senior test-data and fixture designer.
Compile the declared test starting state into a validated fixture plan for one atomic requirement.
Use only the supplied seed declarations and local Database Schema slice. Return semantic entity keys and field names
exactly as supplied. Include every non-nullable field that has no default, except primary keys: the compiler owns
primary keys, row ids, insert order, and timestamps/defaults. Use fixture_key to name a row. A field may reference a
previous row with {"fixture_key":"...","field":"id"}. Do not emit SQL, TypeScript, table names, column names,
routes, implementation behavior, or undeclared example records. Return exactly one JSON object and no prose.
If a declared record cannot be mapped to the local schema, do not invent another entity
or alter its values. The compiler will reject unmatched declarations. Return an empty
fixture_sets array only for a declaration explicitly requesting an empty starting database.
"""


@dataclass(slots=True)
class FixturePassResult:
    fixture_ir: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class FixturePass:
    def __init__(self, model: StructuredModel, artifact_root: Path) -> None:
        self._model = model
        self._retries = _bounded_int("ARC_STRUCTURED_OUTPUT_RETRY_COUNT", 2, 0, 10)
        self._log = SynchronousLog("FixturePass", workspace_root=artifact_root.resolve().parent)

    def compile(
        self,
        requirement_ir: dict[str, Any],
        database_schema: dict[str, Any],
    ) -> FixturePassResult:
        sets: list[dict[str, Any]] = []
        errors: list[str] = []
        nodes = requirement_ir.get("nodes", {})
        for requirement_id in requirement_ir.get("atomic_units", []):
            node = nodes.get(requirement_id, {}) if isinstance(nodes, dict) else {}
            declarations = node.get("seed_fixtures", []) if isinstance(node, dict) else []
            if not declarations:
                continue
            local_schema = schema_for_requirement(database_schema, str(requirement_id))
            explicit = _explicit_decision(declarations)
            prose = [
                declaration for declaration in declarations
                if not declaration.get("records") and not _is_empty_seed(declaration)
            ]
            decision = explicit or {"fixture_sets": []}
            if prose:
                inferred = self._decide(
                    str(requirement_id), node, prose, local_schema, errors,
                )
                if inferred is None:
                    continue
                if not any(fixture_set.get("rows") for fixture_set in inferred["fixture_sets"]):
                    errors.append(
                        f"ARC2402 FIXTURE_INVALID: {requirement_id}: declared seed data produced no rows."
                    )
                decision["fixture_sets"].extend(inferred["fixture_sets"])
            compiled, local_errors = _compile_decision(
                str(requirement_id), decision, local_schema
            )
            sets.extend(compiled)
            errors.extend(local_errors)
            if not any(fixture_set["rows"] for fixture_set in compiled) and not all(
                _is_empty_seed(declaration) for declaration in declarations
            ):
                errors.append(
                    f"ARC2402 FIXTURE_INVALID: {requirement_id}: declared seed data produced no rows."
                )
        fixture_ir = {
            "schema_version": FIXTURE_IR_SCHEMA_VERSION,
            "status": FIXTURE_STATUS if not errors else "FIXTURE_FAILED",
            "fixture_sets": sets,
        }
        return FixturePassResult(fixture_ir, list(dict.fromkeys(errors)))

    def _decide(
        self,
        requirement_id: str,
        node: dict[str, Any],
        declarations: list[dict[str, Any]],
        local_schema: dict[str, Any],
        errors: list[str],
    ) -> dict[str, Any] | None:
        feedback: list[str] = []
        for attempt in range(self._retries + 1):
            payload = {
                "requirement": {
                    key: copy.deepcopy(node.get(key))
                    for key in ("id", "name", "description", "scenarios")
                },
                "seed_declarations": copy.deepcopy(declarations),
                "database_schema": copy.deepcopy(local_schema),
            }
            if feedback:
                payload["validation_feedback"] = feedback
            self._log.info(
                f"MODEL_REQUEST requirement={requirement_id} attempt={attempt + 1}/{self._retries + 1}"
            )
            try:
                decision = self._model.generate_json(
                    schema_name="arc_fixture_ir",
                    instructions=FIXTURE_INSTRUCTIONS,
                    input_payload=payload,
                    output_schema=FIXTURE_DECISION_SCHEMA,
                )
            except Exception as exc:
                feedback = [describe_model_error(exc)]
                continue
            if isinstance(decision.get("fixture_sets"), list):
                return decision
            feedback = [
                "fixture_sets must be an array; do not omit unmatched declarations."
            ]
        errors.append(
            f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
            + (feedback[-1] if feedback else "no valid fixture output")
        )
        return None


def _compile_decision(
    requirement_id: str,
    decision: dict[str, Any],
    local_schema: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    entities = {
        str(entity.get("key", "")): entity
        for entity in local_schema.get("entities", [])
        if isinstance(entity, dict)
    }
    compiled: list[dict[str, Any]] = []
    errors: list[str] = []
    known_keys: set[str] = set()
    known_rows: dict[str, dict[str, Any]] = {}
    for set_index, raw_set in enumerate(decision.get("fixture_sets", []), start=1):
        if not isinstance(raw_set, dict):
            errors.append(f"ARC2402 FIXTURE_INVALID: {requirement_id}: fixture set must be an object.")
            continue
        rows: list[dict[str, Any]] = []
        for row_index, raw_row in enumerate(raw_set.get("rows", []), start=1):
            if not isinstance(raw_row, dict):
                errors.append(f"ARC2402 FIXTURE_INVALID: {requirement_id}: row must be an object.")
                continue
            entity_key = str(raw_row.get("entity_key", ""))
            entity = entities.get(entity_key)
            if entity is None:
                errors.append(
                    f"ARC2402 FIXTURE_INVALID: {requirement_id}: {entity_key!r} is not in the local schema."
                )
                continue
            fixture_key = str(raw_row.get("fixture_key", "")).strip() or f"row_{set_index}_{row_index}"
            if fixture_key in known_keys:
                errors.append(
                    f"ARC2402 FIXTURE_INVALID: {requirement_id}: duplicate fixture key {fixture_key!r}."
                )
                continue
            fields = {
                str(field.get("name", "")): field
                for field in entity.get("fields", [])
                if isinstance(field, dict)
            }
            values = _row_values(raw_row.get("values"))
            for unknown in sorted(set(values) - set(fields)):
                errors.append(
                    f"ARC2402 FIXTURE_INVALID: {requirement_id}: unknown field {entity_key}.{unknown}."
                )
                values.pop(unknown, None)
            for field_name, value in list(values.items()):
                field = fields[field_name]
                if isinstance(value, dict) and set(value) == {"fixture_key", "field"}:
                    target_key = str(value["fixture_key"])
                    target_field = str(value["field"])
                    target_values = known_rows.get(target_key)
                    if target_values is None or target_field not in target_values:
                        errors.append(
                            f"ARC2402 FIXTURE_INVALID: {requirement_id}: unknown fixture reference "
                            f"{fixture_key}.{field_name} -> {target_key}.{target_field}."
                        )
                        values.pop(field_name, None)
                    else:
                        values[field_name] = copy.deepcopy(target_values[target_field])
                elif not _value_matches(value, str(field.get("type", "")), bool(field.get("nullable"))):
                    errors.append(
                        f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
                        f"{entity_key}.{field_name} expects {field.get('type')}."
                    )
            for field_name, field in fields.items():
                if field_name in values or bool(field.get("nullable")) or _has_default(field):
                    continue
                if _is_primary_key(field):
                    values[field_name] = _stable_primary_key(
                        requirement_id, fixture_key, field_name, str(field.get("type", ""))
                    )
                    continue
                errors.append(
                    f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
                    f"missing required field {entity_key}.{field_name}."
                )
            known_keys.add(fixture_key)
            known_rows[fixture_key] = copy.deepcopy(values)
            rows.append(
                {
                    "entity_key": entity_key,
                    "fixture_key": fixture_key,
                    "values": values,
                }
            )
        name = str(raw_set.get("name", "seed")).strip() or "seed"
        compiled.append(
            {
                "id": _stable_id("FIXTURE_SET", requirement_id, f"{set_index}:{name}"),
                "requirement_id": requirement_id,
                "name": name,
                "rows": rows,
            }
        )
    return compiled, errors


def _explicit_decision(declarations: list[dict[str, Any]]) -> dict[str, Any] | None:
    records = [
        record
        for declaration in declarations
        if isinstance(declaration, dict)
        for record in declaration.get("records", [])
        if isinstance(record, dict)
    ]
    if not records:
        return None
    rows = []
    for index, record in enumerate(records, start=1):
        raw_values = record.get("values", {})
        values = (
            [
                {"field": str(key), "value": value}
                for key, value in raw_values.items()
            ]
            if isinstance(raw_values, dict)
            else [{"field": "", "value": raw_values}]
        )
        rows.append({
            "entity_key": str(record.get("entity", "")),
            "fixture_key": f"record_{index}",
            "values": values,
        })
    return {
        "fixture_sets": [
            {
                "name": "seed",
                "rows": rows,
            }
        ]
    }


def _is_empty_seed(declaration: dict[str, Any]) -> bool:
    description = str(declaration.get("description", "")).strip()
    return not declaration.get("records") and bool(re.fullmatch(
        r"(?:empty|empty (?:database|db|dataset|state)|no (?:seed|initial) data|"
        r"空数据库|无(?:初始|种子)?数据|初始数据为空)[\s.!。！]*",
        description,
        re.IGNORECASE,
    ))


def _row_values(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): copy.deepcopy(item) for key, item in value.items()}
    if not isinstance(value, list):
        return {}
    return {
        str(item.get("field", "")): copy.deepcopy(item.get("value"))
        for item in value
        if isinstance(item, dict) and str(item.get("field", ""))
    }


def _value_matches(value: Any, field_type: str, nullable: bool) -> bool:
    if value is None:
        return nullable
    if field_type in {"string", "date", "datetime", "uuid"}:
        return isinstance(value, str)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "foreign_key":
        return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
    if field_type == "json":
        return isinstance(value, (dict, list, str, int, float, bool, type(None)))
    return False


def _has_default(field: dict[str, Any]) -> bool:
    properties = field.get("properties", {})
    return isinstance(properties, dict) and "default" in properties


def _is_primary_key(field: dict[str, Any]) -> bool:
    if field.get("primary_key") is True:
        return True
    return any(
        isinstance(row, dict) and row.get("type") == "PRIMARY_KEY"
        for row in field.get("constraints", [])
    )


def _stable_primary_key(
    requirement_id: str,
    fixture_key: str,
    field_name: str,
    field_type: str,
) -> Any:
    seed = f"arc:{requirement_id}:{fixture_key}:{field_name}"
    if field_type == "integer":
        return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16) or 1
    if field_type == "uuid":
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _stable_id(prefix: str, requirement_id: str, value: str) -> str:
    digest = hashlib.sha256(f"{requirement_id}\0{value}".encode("utf-8")).hexdigest()[:12]
    return f"{prefix}.{digest}"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))
