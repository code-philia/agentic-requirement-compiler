"""Compile application baseline data from requirements into Fixture IR."""

from __future__ import annotations

import copy
import hashlib
import os
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

FIXTURE_INSTRUCTIONS = """You are an application baseline-data designer.
Read the requirement description and every scenario, especially Given/precondition steps. Identify records that
must already exist when the application starts so the described flows can begin.
Do not seed records that a scenario creates through its own actions, records used only as transient test inputs,
or example values that do not imply pre-existing application data.
If no pre-existing application records are required, return {"fixture_sets": []}.
Use only facts in the supplied requirement and local Database Schema slice. Return semantic entity keys and field
names exactly as supplied. Include every non-nullable field that has no default, except primary keys: the compiler owns
primary keys, row ids, insert order, and timestamps/defaults. Use fixture_key to name a row. A field may reference a
previous row with {"fixture_key":"...","field":"id"}. Do not emit SQL, TypeScript, table names, column names,
routes, implementation behavior, or invented business records. Return exactly one JSON object and no prose.
For a required foreign key, create a minimal supporting parent row before the dependent row
using the supplied relationship and parent entity, then reference its generated id by fixture_key.
Supporting rows are permitted only to satisfy baseline rows' required relationships.
If a required record cannot be mapped to the local schema, do not invent another entity or alter its values.
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
            if not isinstance(node, dict) or not (node.get("description") or node.get("scenarios")):
                continue
            local_schema = _fixture_schema_for_requirement(database_schema, str(requirement_id))
            if not local_schema["entities"]:
                # A scenario can require existing data owned by another requirement.
                local_schema = {
                    key: copy.deepcopy(database_schema.get(key, []))
                    for key in ("entities", "relationships", "constraints")
                }
            if not local_schema["entities"]:
                continue
            decision = self._decide(str(requirement_id), node, local_schema, errors)
            if decision is None:
                continue
            compiled, local_errors = _compile_decision(
                str(requirement_id), decision, local_schema
            )
            sets.extend(compiled)
            errors.extend(local_errors)
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
                self._log.info(
                    f"MODEL_REJECTED requirement={requirement_id} attempt={attempt + 1} "
                    f"errors={'; '.join(feedback)}"
                )
                continue
            if not isinstance(decision, dict) or not isinstance(decision.get("fixture_sets"), list):
                feedback = ["fixture_sets must be an array."]
            else:
                _, feedback = _compile_decision(requirement_id, decision, local_schema)
                if not feedback:
                    return decision
            self._log.info(
                f"MODEL_REJECTED requirement={requirement_id} attempt={attempt + 1} "
                f"errors={'; '.join(feedback)}"
            )
        detail = feedback[-1] if feedback else "no valid fixture output"
        prefix = f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
        errors.append(detail if detail.startswith(prefix) else prefix + detail)
        return None


def _fixture_schema_for_requirement(
    schema: dict[str, Any], requirement_id: str,
) -> dict[str, Any]:
    local = schema_for_requirement(schema, requirement_id)
    all_entities = {
        str(entity.get("key", "")): entity
        for entity in schema.get("entities", []) if isinstance(entity, dict)
    }
    included = {
        str(entity.get("key", ""))
        for entity in local.get("entities", []) if isinstance(entity, dict)
    }
    relationships = {
        str(row.get("id", "")): row
        for row in local.get("relationships", []) if isinstance(row, dict)
    }
    while True:
        added = False
        for relationship in schema.get("relationships", []):
            if not isinstance(relationship, dict):
                continue
            child = str(relationship.get("fk_entity", ""))
            parent = str(relationship.get("parent", ""))
            fk_field = str(relationship.get("fk_field", ""))
            if child not in included or parent not in all_entities:
                continue
            if not any(
                field.get("name") == fk_field and not field.get("nullable")
                for field in all_entities.get(child, {}).get("fields", [])
                if isinstance(field, dict)
            ):
                continue
            relationships[str(relationship.get("id", ""))] = relationship
            if parent not in included:
                included.add(parent)
                added = True
        if not added:
            break
    local["entities"] = [
        copy.deepcopy(entity) for key, entity in all_entities.items() if key in included
    ]
    local["relationships"] = [copy.deepcopy(row) for row in relationships.values()]
    return local


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
    required_parents = {
        (str(row.get("fk_entity", "")), str(row.get("fk_field", ""))): str(row.get("parent", ""))
        for row in local_schema.get("relationships", [])
        if isinstance(row, dict) and row.get("child_required") and row.get("fk_entity")
        and row.get("fk_field") and row.get("parent")
    }
    compiled: list[dict[str, Any]] = []
    errors: list[str] = []
    known_keys: set[str] = set()
    known_rows: dict[str, tuple[str, dict[str, Any]]] = {}
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
                    target = known_rows.get(target_key)
                    if target is None or target_field not in target[1]:
                        errors.append(
                            f"ARC2402 FIXTURE_INVALID: {requirement_id}: unknown fixture reference "
                            f"{fixture_key}.{field_name} -> {target_key}.{target_field}."
                        )
                        values.pop(field_name, None)
                    else:
                        values[field_name] = copy.deepcopy(target[1][target_field])
                elif not _value_matches(value, str(field.get("type", "")), bool(field.get("nullable"))):
                    errors.append(
                        f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
                        f"{entity_key}.{field_name} expects {field.get('type')}."
                    )
                parent = required_parents.get((entity_key, field_name))
                if parent and field_name in values and not any(
                    row_entity == parent and row_values.get("id") == values[field_name]
                    for row_entity, row_values in known_rows.values()
                ):
                    errors.append(
                        f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
                        f"{entity_key}.{field_name} must reference a preceding {parent} fixture row."
                    )
            for field_name, field in fields.items():
                if field_name in values or bool(field.get("nullable")) or _has_default(field):
                    continue
                if _is_primary_key(field):
                    values[field_name] = _stable_primary_key(
                        requirement_id, fixture_key, field_name, str(field.get("type", ""))
                    )
                    continue
                if field_name in {"created_at", "updated_at", "viewed_at"} and field.get("type") in {"date", "datetime"}:
                    values[field_name] = (
                        "2000-01-01" if field["type"] == "date" else "2000-01-01T00:00:00Z"
                    )
                    continue
                errors.append(
                    f"ARC2402 FIXTURE_INVALID: {requirement_id}: "
                    f"missing required field {entity_key}.{field_name}."
                )
            known_keys.add(fixture_key)
            known_rows[fixture_key] = (entity_key, copy.deepcopy(values))
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
