from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.logging import SynchronousLog

from .model_client import StructuredModel, describe_model_error
from .trace_payload import format_payload_trace


SCHEMA_VERSION = 2

IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
FIELD_TYPES = {"string", "integer", "number", "boolean", "date", "datetime", "json", "uuid", "foreign_key"}
RELATIONSHIP_TYPES = {"ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_MANY"}
CONSTRAINT_TYPES = {
    "PRIMARY_KEY", "FOREIGN_KEY", "NOT_NULL", "UNIQUE", "COMPOSITE_UNIQUE"
}
FIELD_ORIGINS = {"REQUIREMENT", "RELATIONSHIP", "SYSTEM"}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


ENTITY_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirement_id", "reuse_entities", "new_entities"],
    "properties": {
        "requirement_id": {"type": "string"},
        "reuse_entities": {"type": "array", "items": {"type": "string"}},
        "new_entities": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["key", "description"],
                "properties": {"key": {"type": "string"}, "description": {"type": "string"}},
            },
        },
    },
}

FIELD_PROPERTIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "min_length", "max_length", "pattern", "enum", "format", "minimum", "maximum", "date_past",
        "date_future", "default",
    ],
    "properties": {
        "min_length": _nullable({"type": "integer", "minimum": 0}),
        "max_length": _nullable({"type": "integer", "minimum": 0}),
        "pattern": _nullable({"type": "string"}),
        "enum": _nullable({"type": "array", "items": {"type": "string"}, "minItems": 1}),
        "format": _nullable({"type": "string"}),
        "minimum": _nullable({"type": "number"}),
        "maximum": _nullable({"type": "number"}),
        "date_past": _nullable({"type": "boolean"}),
        "date_future": _nullable({"type": "boolean"}),
        "default": _nullable({"type": ["string", "number", "boolean"]}),
    },
}

FIELD_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["requirement_id", "entities"],
    "properties": {
        "requirement_id": {"type": "string"},
        "entities": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["entity", "reuse_fields", "new_fields"],
                "properties": {
                    "entity": {"type": "string"},
                    "reuse_fields": {"type": "array", "items": {"type": "string"}},
                    "new_fields": {
                        "type": "array",
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["name", "type", "nullable", "description", "properties"],
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "enum": sorted(FIELD_TYPES - {"uuid", "foreign_key"})},
                                "nullable": {"type": "boolean"},
                                "description": {"type": "string"},
                                "properties": FIELD_PROPERTIES_SCHEMA,
                            },
                        },
                    },
                },
            },
        },
    },
}

RELATIONSHIP_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["requirement_id", "relationships"],
    "properties": {
        "requirement_id": {"type": "string"},
        "relationships": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["parent", "child", "type", "child_required", "description"],
                "properties": {
                    "parent": {"type": "string"}, "child": {"type": "string"},
                    "type": {"type": "string", "enum": sorted(RELATIONSHIP_TYPES)},
                    "child_required": {"type": "boolean"}, "description": {"type": "string"},
                },
            },
        },
    },
}



ENTITY_INSTRUCTIONS = """You are a senior data-modeling specialist focused on persistent domain entities.
Your task is to perform the entity-discovery pass for one atomic requirement.
For exactly one atomic requirement, decide which persistent entities it reuses and which persistent entities must
be introduced. Existing entities are compiler symbols: reuse them when their meaning fits. Do not produce fields,
relationships, constraints, APIs, modules, or SQL. Entity keys are singular snake_case. Case-only name differences
are the same symbol. Terms explicitly paired by the requirement, such as "journey/train", are aliases: reuse the
existing entity whose description matches. UI selection, page context, form state, and other transient nouns are
not database entities. Include every persistent entity used by the requirement. When wording permits several names,
choose the existing entity whose persistent meaning best matches the requirement. If none matches, introduce one
concise entity. Do not emit alternatives or unresolved decisions.

Return exactly one JSON object with these keys and no others:
- requirement_id: copy the supplied requirement.requirement_id exactly.
- reuse_entities: array of existing entity keys; use [] when none.
- new_entities: array of {"key": singular_snake_case, "description": string}; use [] when none.

Valid output example:
{"requirement_id":"REQ-3.2","reuse_entities":["traveler","train_service"],"new_entities":[{"key":"booking","description":"A confirmed booking."}]}
Valid empty output example:
{"requirement_id":"REQ-4.1","reuse_entities":[],"new_entities":[]}
"""

FIELD_INSTRUCTIONS = """You are a senior database-schema specialist focused on scalar domain fields.
Your task is to perform the field-discovery pass for one atomic requirement.
For exactly one atomic requirement, discover scalar domain fields only for entities already associated with it.
Reuse existing fields whenever their meaning fits. Do not create entities, relationships, foreign-key fields,
cross-field constraints, APIs, modules, or SQL. Do not put id in new_fields because the compiler creates it; id may
appear in reuse_fields only when the requirement uses it. Relationship references such as user_id are forbidden
in new_fields because Pass 3 lowers relationships into foreign keys. Return every related entity.
Each properties member is required by the response protocol: use null when it does not apply, and false for an
inactive date_past/date_future flag. Never invent a default or format merely to fill the response structure.

Return exactly one JSON object with these keys and no others:
- requirement_id: copy the supplied requirement.requirement_id exactly.
- entities: one entry for every supplied related_entities item, even when it contributes no fields. Each entry is
  {"entity": existing_key, "reuse_fields": [existing field names], "new_fields": [field declarations]}.
- Every field declaration contains name, type, nullable, description, and properties. properties must contain all
  ten protocol keys: min_length, max_length, pattern, enum, format, minimum, maximum, date_past, date_future,
  default. Set every inapplicable value to null; false is also allowed for inactive date flags.
- Emit each entity and field once. Never put id or names ending in _id in new_fields.

Valid output example:
{"requirement_id":"REQ-3.2","entities":[{"entity":"booking","reuse_fields":[],"new_fields":[{"name":"booking_number","type":"string","nullable":false,"description":"Public booking number.","properties":{"min_length":1,"max_length":20,"pattern":null,"enum":null,"format":null,"minimum":null,"maximum":null,"date_past":null,"date_future":null,"default":null}}]},{"entity":"train_service","reuse_fields":["train_number"],"new_fields":[]},{"entity":"traveler","reuse_fields":[],"new_fields":[]}]}
Valid no-field output example:
{"requirement_id":"REQ-3.1","entities":[{"entity":"train_service","reuse_fields":[],"new_fields":[]}]}
"""

RELATIONSHIP_INSTRUCTIONS = """You are a senior data-modeling specialist focused on entity relationships.
Your task is to resolve persistent relationships for one atomic requirement.
For exactly one atomic requirement, decide only logical relationships among the supplied related entities. Choose
parent, child, ONE_TO_ONE / ONE_TO_MANY / MANY_TO_MANY, and whether the child reference is required. Do not create
entities, fields, foreign keys, constraints, APIs, modules, or SQL. The compiler lowers accepted relationships.
The input may contain existing_relationships. Do not contradict their cardinality; repeat the same declaration
only when this requirement also establishes or relies on that persistent relationship so traceability is retained.
Do not infer a relationship merely because two entities appear together. A read-only requirement, transient
selection context, or explicit statement that no record is created means no new relationship. If the requirement
creates or persistently associates records, choose the narrowest cardinality supported by the behavior. A child row
that stores its parent's identity is normally child_required=true. Return an empty relationship list when no
persistent association is established. Do not emit alternatives or unresolved decisions.

Return exactly one JSON object with these keys and no others:
- requirement_id: copy the supplied requirement.requirement_id exactly.
- relationships: array of {"parent": entity key, "child": entity key, "type": "ONE_TO_ONE" |
  "ONE_TO_MANY" | "MANY_TO_MANY", "child_required": boolean, "description": string}.

Valid output example:
{"requirement_id":"REQ-3.2","relationships":[{"parent":"traveler","child":"booking","type":"ONE_TO_MANY","child_required":true,"description":"A traveler may own many bookings."}]}
Valid no-relationship output example:
{"requirement_id":"REQ-3.1","relationships":[]}
"""



@dataclass(slots=True)
class DatabasePassResult:
    schema: dict[str, Any]
    node_states: dict[str, str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class DatabaseSchemaState:
    """Compiler-owned state; semantic passes can mutate it only through apply methods."""

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, Any]] = {}
        self.relationships: dict[str, dict[str, Any]] = {}
        self.constraints: dict[str, dict[str, Any]] = {}
        self.requirement_entity_links: dict[str, set[str]] = {}
        self.requirement_field_links: dict[str, set[str]] = {}
        self.requirement_relationship_links: dict[str, set[str]] = {}
        self.requirement_constraint_links: dict[str, set[str]] = {}
        self.warnings: list[str] = []

    def entity_headers(self) -> list[dict[str, str]]:
        return [{"key": item["key"], "description": item["description"]} for item in self._ordered_entities()]

    def ensure_requirement(self, requirement_id: str) -> None:
        """Retain traceability even when a requirement needs no persistent data."""

        self.requirement_entity_links.setdefault(requirement_id, set())

    def related_entity_keys(self, requirement_id: str) -> list[str]:
        return sorted(self.requirement_entity_links.get(requirement_id, set()))

    def schema_slice(self, requirement_id: str) -> dict[str, Any]:
        keys = set(self.related_entity_keys(requirement_id))
        return {
            "entities": [self._public_entity(self.entities[key]) for key in sorted(keys)],
            "relationships": [copy.deepcopy(item) for item in self.relationships.values() if item["parent"] in keys and item["child"] in keys],
        }

    def apply_entities(self, requirement_id: str, decision: dict[str, Any], errors: list[str]) -> None:
        for raw_key in decision.get("reuse_entities", []):
            key = _normalize_identifier(str(raw_key))
            if key not in self.entities:
                errors.append(_format_error("ARC2211", f"Pass 1 reuses an unknown entity: {raw_key}.", node_id=requirement_id))
            else:
                self._link_entity(requirement_id, key)
        for raw in decision.get("new_entities", []):
            key = _normalize_identifier(str(raw.get("key", "")))
            if not IDENTIFIER_PATTERN.fullmatch(key):
                errors.append(_format_error("ARC2212", f"Pass 1 emitted an invalid entity key: {raw.get('key')}.", node_id=requirement_id))
                continue
            if key not in self.entities:
                self.entities[key] = {
                    "key": key, "description": str(raw.get("description", "")).strip(),
                    "requirement_ids": [], "fields": {},
                }
                self._add_field(key, {
                    "name": "id", "type": "uuid", "nullable": False,
                    "description": "Compiler-provided stable primary key.", "properties": {"format": "uuid"},
                    "origin": "SYSTEM", "references": None,
                }, requirement_id, errors, trace=False)
            self._link_entity(requirement_id, key)

    def apply_fields(self, requirement_id: str, decision: dict[str, Any], errors: list[str]) -> None:
        related = set(self.related_entity_keys(requirement_id))
        seen: set[str] = set()
        for item in decision.get("entities", []):
            key = _normalize_identifier(str(item.get("entity", "")))
            if key not in related:
                errors.append(_format_error("ARC2221", f"Pass 2 references an entity outside its requirement slice: {key}.", node_id=requirement_id))
                continue
            # Structured model output can repeat an entity or split its fields
            # across several entries. Applying every fragment is deterministic:
            # reuse links are sets and _add_field rejects incompatible declarations.
            seen.add(key)
            for raw_name in item.get("reuse_fields", []):
                name = _normalize_identifier(str(raw_name))
                if name not in self.entities[key]["fields"]:
                    errors.append(_format_error("ARC2223", f"Pass 2 reuses an unknown field: {key}.{name}.", node_id=requirement_id))
                else:
                    self._link_field(requirement_id, key, name)
            for raw_field in item.get("new_fields", []):
                name = _normalize_identifier(str(raw_field.get("name", "")))
                if name == "id" or name.endswith("_id"):
                    errors.append(_format_error("ARC2224", f"Pass 2 cannot create system or foreign-key field: {key}.{name}.", node_id=requirement_id))
                    continue
                self._add_field(key, {
                    "name": name, "type": raw_field.get("type"), "nullable": raw_field.get("nullable"),
                    "description": str(raw_field.get("description", "")).strip(),
                    "properties": _compact_properties(raw_field.get("properties", {})),
                    "origin": "REQUIREMENT", "references": None,
                }, requirement_id, errors)
        if related - seen:
            errors.append(_format_error("ARC2225", "Pass 2 omitted related entities: " + ", ".join(sorted(related - seen)) + ".", node_id=requirement_id))

    def apply_relationships(self, requirement_id: str, decision: dict[str, Any], errors: list[str]) -> None:
        related = set(self.related_entity_keys(requirement_id))
        for raw in decision.get("relationships", []):
            parent = _normalize_identifier(str(raw.get("parent", "")))
            child = _normalize_identifier(str(raw.get("child", "")))
            relationship_type = str(raw.get("type", ""))
            if parent not in related or child not in related or parent == child:
                # Relationship decisions are advisory and scoped to the
                # supplied slice; discard an invalid edge without failing the
                # otherwise usable entity/field design.
                continue
            if relationship_type not in RELATIONSHIP_TYPES:
                errors.append(_format_error("ARC2232", f"Pass 3 emitted invalid cardinality: {relationship_type}.", node_id=requirement_id))
                continue
            conflict = next((item for item in self.relationships.values() if item["parent"] == parent and item["child"] == child and item["type"] != relationship_type), None)
            if conflict:
                errors.append(_format_error("ARC2233", f"Conflicting relationship cardinality: {parent} -> {child}.", node_id=requirement_id))
                continue
            relationship_id = _relationship_id(parent, child, relationship_type)
            relationship = self.relationships.get(relationship_id)
            if relationship is None:
                relationship = {
                    "id": relationship_id, "parent": parent, "child": child, "type": relationship_type,
                    "child_required": bool(raw.get("child_required")), "description": str(raw.get("description", "")).strip(),
                    "fk_entity": None, "fk_field": None, "association_entity": None, "requirement_ids": [],
                }
                self.relationships[relationship_id] = relationship
                self._lower_relationship(relationship, requirement_id, errors)
            else:
                relationship["child_required"] = relationship["child_required"] or bool(raw.get("child_required"))
                if relationship["fk_entity"] and relationship["fk_field"]:
                    fk_entity, fk_field = relationship["fk_entity"], relationship["fk_field"]
                    field_item = self.entities[fk_entity]["fields"][fk_field]
                    field_item["nullable"] = not relationship["child_required"]
                    self._link_field(requirement_id, fk_entity, fk_field)
                if relationship["association_entity"]:
                    association = relationship["association_entity"]
                    self._link_entity(requirement_id, association)
                    for target in (parent, child):
                        self._link_field(requirement_id, association, f"{target}_id")
            _append_unique(relationship["requirement_ids"], requirement_id)
            self.requirement_relationship_links.setdefault(requirement_id, set()).add(relationship_id)


    def add_static_constraints(self) -> None:
        """Lower structural facts to constraints without an LLM call."""

        for entity in self._ordered_entities():
            for field_item in entity["fields"].values():
                ref = f"{entity['key']}.{field_item['name']}"
                for source in field_item["requirement_ids"] or ["SYSTEM"]:
                    if field_item.get("primary_key"):
                        self._add_constraint("PRIMARY_KEY", [ref], "Entity primary key.", source)
                    if not field_item["nullable"]:
                        self._add_constraint("NOT_NULL", [ref], "Field is required.", source)
                    if field_item.get("references"):
                        self._add_constraint(
                            "FOREIGN_KEY", [ref, field_item["references"]], "Relationship foreign key.", source,
                        )
        for relationship in self.relationships.values():
            if relationship["type"] == "ONE_TO_ONE" and relationship["fk_entity"] and relationship["fk_field"]:
                for source in relationship["requirement_ids"] or ["SYSTEM"]:
                    self._add_constraint(
                        "UNIQUE", [f"{relationship['fk_entity']}.{relationship['fk_field']}"],
                        "One-to-one relationship.", source,
                    )
            if relationship["type"] == "MANY_TO_MANY" and relationship["association_entity"]:
                association = relationship["association_entity"]
                fields = [
                    f"{association}.{relationship['parent']}_id",
                    f"{association}.{relationship['child']}_id",
                ]
                for source in relationship["requirement_ids"] or ["SYSTEM"]:
                    self._add_constraint(
                        "COMPOSITE_UNIQUE", fields, "Association pair must be unique.", source,
                    )

    def to_schema(self, *, status: str) -> dict[str, Any]:
        requirement_ids = (
            set(self.requirement_entity_links)
            | set(self.requirement_field_links)
            | set(self.requirement_relationship_links)
            | set(self.requirement_constraint_links)
        )
        traceability = {
            requirement_id: {
                "entities": sorted(self.requirement_entity_links.get(requirement_id, set())),
                "fields": sorted(self.requirement_field_links.get(requirement_id, set())),
                "relationships": sorted(self.requirement_relationship_links.get(requirement_id, set())),
                "constraints": sorted(self.requirement_constraint_links.get(requirement_id, set())),
            }
            for requirement_id in sorted(requirement_ids)
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "entities": [self._public_entity(item) for item in self._ordered_entities()],
            "relationships": [copy.deepcopy(self.relationships[key]) for key in sorted(self.relationships)],
            "constraints": [copy.deepcopy(self.constraints[key]) for key in sorted(self.constraints)],
            "traceability": {"requirements": traceability},
        }

    def _ordered_entities(self) -> list[dict[str, Any]]:
        return [self.entities[key] for key in sorted(self.entities)]

    def _public_entity(self, entity: dict[str, Any]) -> dict[str, Any]:
        fields = [copy.deepcopy(entity["fields"][name]) for name in sorted(entity["fields"])]
        return {
            "key": entity["key"],
            "description": entity["description"],
            "requirement_ids": list(entity["requirement_ids"]),
            "fields": fields,
        }

    def _link_entity(self, requirement_id: str, key: str) -> None:
        _append_unique(self.entities[key]["requirement_ids"], requirement_id)
        self.requirement_entity_links.setdefault(requirement_id, set()).add(key)

    def _link_field(self, requirement_id: str, entity_key: str, field_name: str) -> None:
        field_item = self.entities[entity_key]["fields"][field_name]
        _append_unique(field_item["requirement_ids"], requirement_id)
        self.requirement_field_links.setdefault(requirement_id, set()).add(f"{entity_key}.{field_name}")

    def _add_field(
        self,
        entity_key: str,
        raw: dict[str, Any],
        requirement_id: str,
        errors: list[str],
        *,
        trace: bool = True,
    ) -> None:
        name = _normalize_identifier(str(raw.get("name", "")))
        field_type = str(raw.get("type", ""))
        if not IDENTIFIER_PATTERN.fullmatch(name) or field_type not in FIELD_TYPES or not isinstance(raw.get("nullable"), bool):
            errors.append(_format_error("ARC2226", f"Invalid field declaration: {entity_key}.{name}.", node_id=requirement_id))
            return
        existing = self.entities[entity_key]["fields"].get(name)
        if existing is not None:
            if existing["type"] != field_type or existing.get("references") != raw.get("references"):
                errors.append(_format_error("ARC2227", f"Conflicting field declaration: {entity_key}.{name}.", node_id=requirement_id))
                return
            existing["nullable"] = existing["nullable"] and bool(raw["nullable"])
            _merge_properties(existing["properties"], raw.get("properties", {}), entity_key, name, requirement_id, errors)
        else:
            existing = {
                "name": name,
                "type": field_type,
                "nullable": bool(raw["nullable"]),
                "primary_key": name == "id",
                "description": str(raw.get("description", "")).strip(),
                "properties": copy.deepcopy(raw.get("properties", {})),
                "origin": str(raw.get("origin", "REQUIREMENT")),
                "references": raw.get("references"),
                "requirement_ids": [],
            }
            self.entities[entity_key]["fields"][name] = existing
        if requirement_id != "SYSTEM":
            _append_unique(existing["requirement_ids"], requirement_id)
            if trace:
                self.requirement_field_links.setdefault(requirement_id, set()).add(f"{entity_key}.{name}")

    def _lower_relationship(self, relationship: dict[str, Any], requirement_id: str, errors: list[str]) -> None:
        parent, child = relationship["parent"], relationship["child"]
        if relationship["type"] in {"ONE_TO_MANY", "ONE_TO_ONE"}:
            field_name = f"{parent}_id"
            relationship.update({"fk_entity": child, "fk_field": field_name})
            self._add_field(child, {
                "name": field_name, "type": "foreign_key", "nullable": not relationship["child_required"],
                "description": f"Reference to {parent}.", "properties": {}, "origin": "RELATIONSHIP",
                "references": f"{parent}.id",
            }, requirement_id, errors)
            return

        association = _association_entity_key(parent, child)
        relationship["association_entity"] = association
        if association not in self.entities:
            self.entities[association] = {
                "key": association, "description": f"Association between {parent} and {child}.",
                "requirement_ids": [], "fields": {},
            }
            self._add_field(association, {
                "name": "id", "type": "uuid", "nullable": False,
                "description": "Compiler-provided stable primary key.", "properties": {"format": "uuid"},
                "origin": "SYSTEM", "references": None,
            }, requirement_id, errors, trace=False)
        self._link_entity(requirement_id, association)
        for target in (parent, child):
            name = f"{target}_id"
            self._add_field(association, {
                "name": name, "type": "foreign_key", "nullable": False, "description": f"Reference to {target}.",
                "properties": {}, "origin": "RELATIONSHIP", "references": f"{target}.id",
            }, requirement_id, errors)
        self._add_constraint(
            "COMPOSITE_UNIQUE", [f"{association}.{parent}_id", f"{association}.{child}_id"],
            "Association pair must be unique.", requirement_id,
        )

    def _add_constraint(
        self,
        constraint_type: str,
        fields: list[str],
        description: str,
        requirement_id: str,
    ) -> None:
        identity = {"type": constraint_type, "fields": fields, "description": description}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
        constraint_id = f"constraint_{constraint_type.lower()}_{digest}"
        constraint = self.constraints.setdefault(constraint_id, {
            "id": constraint_id, "type": constraint_type, "fields": list(fields), "description": description,
            "requirement_ids": [],
        })
        if requirement_id != "SYSTEM":
            _append_unique(constraint["requirement_ids"], requirement_id)
            self.requirement_constraint_links.setdefault(requirement_id, set()).add(constraint_id)

class DatabaseSchemaPass:
    """Design entities, fields and relationships, then derive structural constraints."""

    def __init__(self, model: StructuredModel, artifact_root: Path) -> None:
        self._model = model
        self._artifact_root = artifact_root.expanduser().resolve()
        self._log = SynchronousLog("DatabaseSchemaPass", workspace_root=self._artifact_root.parent)
        self._retry_count = _bounded_env_int("ARC_STRUCTURED_OUTPUT_RETRY_COUNT", 2, 0, 10)
        self._trace_enabled = _enabled_env_flag("ARC_DATABASE_TRACE", default=True)

    def compile(
        self,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any] | None = None,
    ) -> DatabasePassResult:
        nodes = requirement_ir.get("nodes", {})
        requirements = [
            node_id
            for wave in _ordered_waves(requirement_ir.get("atomic_units", []), dependency_graph or {})
            for node_id in wave
        ]
        effective_dependencies = (dependency_graph or {}).get("atomic_dependencies", {})
        states = {node_id: "DISCOVERED" for node_id in requirements}
        errors: list[str] = []
        state = DatabaseSchemaState()
        for node_id in requirements:
            state.ensure_requirement(node_id)
        passes: list[tuple[str, str, dict[str, Any], str, Callable[..., None]]] = [
            ("pass1_entities", "arc_database_entities", ENTITY_DECISION_SCHEMA, ENTITY_INSTRUCTIONS, state.apply_entities),
            ("pass2_fields", "arc_database_fields", FIELD_DECISION_SCHEMA, FIELD_INSTRUCTIONS, state.apply_fields),
            ("pass3_relationships", "arc_database_relationships", RELATIONSHIP_DECISION_SCHEMA, RELATIONSHIP_INSTRUCTIONS, state.apply_relationships),
        ]
        for phase, schema_name, output_schema, instructions, apply_decision in passes:
            self._trace(f"PASS_START phase={phase} requirements={len(requirements)}")
            for node_id in requirements:
                node = nodes.get(node_id)
                if not isinstance(node, dict):
                    errors.append(_format_error("ARC2101", "Atomic requirement is missing from Requirement IR.", node_id=node_id))
                    states[node_id] = "FAILED"
                    continue
                if phase == "pass3_relationships" and len(state.related_entity_keys(node_id)) < 2:
                    states[node_id] = "RELATIONSHIPS_RESOLVED"
                    continue
                decision = self._run_unit(
                    phase=phase,
                    schema_name=schema_name,
                    output_schema=output_schema,
                    instructions=instructions,
                    node_id=node_id,
                    context=self._context_for(
                        phase, node_id, node, nodes, state, effective_dependencies
                    ),
                    errors=errors,
                    warnings=state.warnings,
                )
                if decision is None:
                    states[node_id] = "FAILED"
                    continue
                before = len(errors)
                apply_decision(node_id, decision, errors)
                if len(errors) != before:
                    states[node_id] = "FAILED"
                    continue
                states[node_id] = {
                    "pass1_entities": "ENTITIES_DISCOVERED",
                    "pass2_fields": "FIELDS_DISCOVERED",
                    "pass3_relationships": "RELATIONSHIPS_RESOLVED",
                }[phase]
                self._trace(
                    f"DECISION_APPLIED phase={phase} requirement={node_id} "
                    f"state={states[node_id]}"
                )
            self._trace(f"PASS_COMPLETED phase={phase} requirements={len(requirements)}")
            # Design diagnostics are accumulated across all three passes. Do
            # not stop after an invalid entity/field/relationship decision;
            # later passes may still recover usable schema information and the
            # controller will continue with the partial result.

        # Structural constraints are derived from fields and relationships.
        state.add_static_constraints()
        schema = state.to_schema(status="RESOLVED")
        errors.extend(validate_database_schema(schema, expected_requirement_ids=set(requirements)))
        schema["status"] = "RESOLVED" if not errors else "PROPOSED"
        states.update({node_id: "SCHEMA_ANALYZED" if not errors else "FAILED" for node_id in requirements})
        return DatabasePassResult(schema, states, errors, state.warnings)

    def _context_for(
        self,
        phase: str,
        node_id: str,
        node: dict[str, Any],
        nodes: dict[str, Any],
        state: DatabaseSchemaState,
        effective_dependencies: dict[str, Any],
    ) -> dict[str, Any]:
        requirement = _node_context(
            node,
            nodes,
            effective_dependencies.get(node_id, node.get("dependencies", [])),
        )
        if phase == "pass1_entities":
            return {"requirement": requirement, "existing_entities": state.entity_headers()}
        if phase == "pass2_fields":
            return {"requirement": requirement, "related_entities": state.schema_slice(node_id)["entities"]}
        if phase == "pass3_relationships":
            schema_slice = state.schema_slice(node_id)
            return {
                "requirement": requirement,
                "related_entities": schema_slice["entities"],
                "existing_relationships": schema_slice["relationships"],
            }
        raise ValueError(f"Unknown database design phase: {phase}")

    def _run_unit(
        self,
        *,
        phase: str,
        schema_name: str,
        output_schema: dict[str, Any],
        instructions: str,
        node_id: str,
        context: dict[str, Any],
        errors: list[str],
        warnings: list[str],
    ) -> dict[str, Any] | None:
        feedback: list[str] = []
        for attempt in range(self._retry_count + 1):
            payload = copy.deepcopy(context)
            if feedback:
                payload["previous_validation_errors"] = feedback
            started_at = time.monotonic()
            self._trace(
                f"MODEL_REQUEST phase={phase} requirement={node_id} "
                f"attempt={attempt + 1}/{self._retry_count + 1} schema={schema_name}"
            )
            request_payload = {
                "schema_name": schema_name,
                "instructions": instructions,
                "input_payload": payload,
                "output_schema": output_schema,
            }
            self._trace(
                _context_audit(
                    phase=phase,
                    requirement_id=node_id,
                    attempt=attempt + 1,
                    request_payload=request_payload,
                )
            )
            self._trace(
                f"MODEL_INPUT phase={phase} requirement={node_id} attempt={attempt + 1}\n"
                + format_payload_trace(request_payload)
            )
            try:
                decision = self._model.generate_json(
                    schema_name=schema_name,
                    instructions=instructions,
                    input_payload=payload,
                    output_schema=output_schema,
                )
            except Exception as exc:
                duration_ms = round((time.monotonic() - started_at) * 1000)
                feedback = [f"Model call failed: {describe_model_error(exc)}"]
                self._trace(
                    f"MODEL_ERROR phase={phase} requirement={node_id} "
                    f"attempt={attempt + 1} duration_ms={duration_ms} error={feedback[0]}"
                )
            else:
                duration_ms = round((time.monotonic() - started_at) * 1000)
                self._trace(
                    f"MODEL_OUTPUT phase={phase} requirement={node_id} "
                    f"attempt={attempt + 1} duration_ms={duration_ms}\n"
                    + format_payload_trace(decision)
                )
                decision, normalization_notes = _normalize_decision_context(
                    phase, decision, context
                )
                for note in normalization_notes:
                    self._trace(
                        f"MODEL_NORMALIZED phase={phase} requirement={node_id} "
                        f"action={note}"
                    )
                feedback = _validate_decision_shape(phase, node_id, decision)
                feedback.extend(_validate_decision_context(phase, decision, context))
                if not feedback:
                    self._trace(
                        f"MODEL_ACCEPTED phase={phase} requirement={node_id} "
                        f"attempt={attempt + 1} duration_ms={duration_ms}"
                    )
                    return decision
            self._trace(f"MODEL_REJECTED phase={phase} requirement={node_id} errors={'; '.join(feedback)}")
        errors.append(_format_error("ARC2103", f"{phase} failed: {'; '.join(feedback)}", node_id=node_id))
        return None


    def _trace(self, message: str) -> None:
        if self._trace_enabled:
            self._log.info(message)


def schema_for_requirement(schema: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    """Query final Schema IR through its explicit requirement traceability index."""

    traceability = schema.get("traceability", {}).get("requirements", {})
    links = traceability.get(requirement_id, {}) if isinstance(traceability, dict) else {}
    entity_keys = set(links.get("entities", []))
    relationship_ids = set(links.get("relationships", []))
    constraint_ids = set(links.get("constraints", []))
    return {
        "entities": [copy.deepcopy(item) for item in schema.get("entities", []) if item.get("key") in entity_keys],
        "relationships": [
            copy.deepcopy(item) for item in schema.get("relationships", []) if item.get("id") in relationship_ids
        ],
        "constraints": [
            copy.deepcopy(item) for item in schema.get("constraints", []) if item.get("id") in constraint_ids
        ],
    }


def database_traceability(schema: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Project provenance to requirement -> entity -> field names."""

    raw = schema.get("traceability", {}).get("requirements", {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, list[str]]] = {}
    for requirement_id, links in sorted(raw.items()):
        if not isinstance(links, dict):
            continue
        entity_fields: dict[str, set[str]] = {
            str(entity): set() for entity in links.get("entities", [])
        }
        for reference in links.get("fields", []):
            entity, separator, field_name = str(reference).partition(".")
            if separator and entity and field_name:
                entity_fields.setdefault(entity, set()).add(field_name)
        result[str(requirement_id)] = {
            entity: sorted(fields) for entity, fields in sorted(entity_fields.items())
        }
    return result


def validate_database_schema(
    schema: dict[str, Any],
    *,
    expected_requirement_ids: set[str] | None = None,
) -> list[str]:
    """Statically validate a final Stage 1 schema without modifying it."""

    if not isinstance(schema, dict):
        return ["schema must be a JSON object"]
    if schema.get("schema_version") != SCHEMA_VERSION:
        return [f"schema_version must be {SCHEMA_VERSION}"]
    if schema.get("status") != "RESOLVED":
        return ["schema status must be RESOLVED"]
    entities = schema.get("entities")
    relationships = schema.get("relationships")
    constraints = schema.get("constraints")
    traceability_container = schema.get("traceability")
    traceability = traceability_container.get("requirements") if isinstance(traceability_container, dict) else None
    if not isinstance(entities, list) or not isinstance(relationships, list) or not isinstance(constraints, list):
        return ["entities, relationships, and constraints must be arrays"]
    if not isinstance(traceability, dict):
        return ["traceability.requirements must be an object"]

    errors: list[str] = []
    entity_map: dict[str, dict[str, Any]] = {}
    field_refs: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict):
            errors.append("entity must be an object")
            continue
        key = str(entity.get("key", ""))
        if not IDENTIFIER_PATTERN.fullmatch(key) or key in entity_map:
            errors.append(f"invalid or duplicate entity key: {key}")
            continue
        entity_map[key] = entity
        fields = entity.get("fields")
        if not isinstance(fields, list):
            errors.append(f"entity fields must be an array: {key}")
            continue
        names: set[str] = set()
        for field_item in fields:
            name = str(field_item.get("name", "")) if isinstance(field_item, dict) else ""
            if not IDENTIFIER_PATTERN.fullmatch(name) or name in names:
                errors.append(f"invalid or duplicate field: {key}.{name}")
                continue
            names.add(name)
            field_refs.add(f"{key}.{name}")
            if field_item.get("type") not in FIELD_TYPES:
                errors.append(f"invalid field type: {key}.{name}")
            if field_item.get("origin") not in FIELD_ORIGINS:
                errors.append(f"invalid field origin: {key}.{name}")
            if not isinstance(field_item.get("nullable"), bool):
                errors.append(f"invalid nullable flag: {key}.{name}")
            errors.extend(_field_property_errors(key, field_item))
        if "id" not in names:
            errors.append(f"entity has no id field: {key}")
        else:
            id_field = next(item for item in fields if isinstance(item, dict) and item.get("name") == "id")
            if id_field.get("type") != "uuid" or not id_field.get("primary_key") or id_field.get("origin") != "SYSTEM":
                errors.append(f"entity id is not a compiler-provided uuid primary key: {key}")
    for entity_key, entity in entity_map.items():
        for field_item in entity.get("fields", []):
            reference = field_item.get("references")
            if reference is not None and reference not in field_refs:
                errors.append(f"foreign key target does not exist: {entity_key}.{field_item.get('name')} -> {reference}")
            if reference is not None and (field_item.get("type") != "foreign_key" or field_item.get("origin") != "RELATIONSHIP"):
                errors.append(f"invalid foreign key field: {entity_key}.{field_item.get('name')}")

    relationship_ids: set[str] = set()
    for relationship in relationships:
        relationship_id = str(relationship.get("id", "")) if isinstance(relationship, dict) else ""
        if not relationship_id or relationship_id in relationship_ids:
            errors.append(f"invalid or duplicate relationship id: {relationship_id}")
            continue
        relationship_ids.add(relationship_id)
        if relationship.get("parent") not in entity_map or relationship.get("child") not in entity_map:
            errors.append(f"relationship endpoint does not exist: {relationship_id}")
        if relationship.get("type") not in RELATIONSHIP_TYPES:
            errors.append(f"invalid relationship type: {relationship_id}")
        if relationship.get("fk_entity") is not None and f"{relationship.get('fk_entity')}.{relationship.get('fk_field')}" not in field_refs:
            errors.append(f"relationship foreign key does not exist: {relationship_id}")
        if relationship.get("type") == "MANY_TO_MANY" and relationship.get("association_entity") not in entity_map:
            errors.append(f"relationship association entity does not exist: {relationship_id}")
        if relationship.get("type") in {"ONE_TO_ONE", "ONE_TO_MANY"}:
            expected_ref = f"{relationship.get('parent')}.id"
            fk_entity = entity_map.get(str(relationship.get("fk_entity")), {})
            fk = next(
                (item for item in fk_entity.get("fields", []) if item.get("name") == relationship.get("fk_field")),
                {},
            )
            if relationship.get("fk_entity") != relationship.get("child") or fk.get("references") != expected_ref:
                errors.append(f"relationship is not lowered on the child side: {relationship_id}")
        if not relationship.get("requirement_ids"):
            errors.append(f"relationship has no requirement traceability: {relationship_id}")

    constraint_ids: set[str] = set()
    for constraint in constraints:
        constraint_id = str(constraint.get("id", "")) if isinstance(constraint, dict) else ""
        if not constraint_id or constraint_id in constraint_ids:
            errors.append(f"invalid or duplicate constraint id: {constraint_id}")
            continue
        constraint_ids.add(constraint_id)
        if constraint.get("type") not in CONSTRAINT_TYPES:
            errors.append(f"invalid constraint: {constraint_id}")
        constraint_fields = constraint.get("fields", [])
        if not isinstance(constraint_fields, list):
            errors.append(f"constraint fields must be an array: {constraint_id}")
            constraint_fields = []
        elif any(reference not in field_refs for reference in constraint_fields):
            errors.append(f"constraint references an unknown field: {constraint_id}")
        if constraint.get("type") == "UNIQUE" and len(constraint_fields) != 1:
            errors.append(f"UNIQUE constraint must reference one field: {constraint_id}")
        if constraint.get("type") == "COMPOSITE_UNIQUE" and len(constraint_fields) < 2:
            errors.append(f"COMPOSITE_UNIQUE constraint must reference multiple fields: {constraint_id}")

    if expected_requirement_ids is not None:
        if expected_requirement_ids - set(traceability):
            errors.append("missing requirement traceability: " + ", ".join(sorted(expected_requirement_ids - set(traceability))))
        if set(traceability) - expected_requirement_ids:
            errors.append("unexpected requirement traceability: " + ", ".join(sorted(set(traceability) - expected_requirement_ids)))
    for requirement_id, links in traceability.items():
        if not isinstance(links, dict):
            errors.append(f"invalid requirement traceability: {requirement_id}")
            continue
        if any(key not in entity_map for key in links.get("entities", [])):
            errors.append(f"traceability references an unknown entity: {requirement_id}")
        if any(ref not in field_refs for ref in links.get("fields", [])):
            errors.append(f"traceability references an unknown field: {requirement_id}")
        if any(value not in relationship_ids for value in links.get("relationships", [])):
            errors.append(f"traceability references an unknown relationship: {requirement_id}")
        if any(value not in constraint_ids for value in links.get("constraints", [])):
            errors.append(f"traceability references an unknown constraint: {requirement_id}")
    return errors


def _field_property_errors(entity_key: str, field_item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    name = str(field_item.get("name", ""))
    field_type = field_item.get("type")
    properties = field_item.get("properties")
    if not isinstance(properties, dict):
        return [f"field properties must be an object: {entity_key}.{name}"]
    unknown = set(properties) - {
        "min_length", "max_length", "pattern", "enum", "format", "minimum", "maximum", "date_past",
        "date_future", "default",
    }
    if unknown:
        errors.append(f"unknown field properties: {entity_key}.{name} -> {', '.join(sorted(unknown))}")
    active = {key for key, value in properties.items() if value is not None and value is not False}
    if active & {"min_length", "max_length", "pattern"} and field_type != "string":
        errors.append(f"string property used on non-string field: {entity_key}.{name}")
    if "format" in active and field_type not in {"string", "uuid", "date", "datetime"}:
        errors.append(f"format property used on incompatible field: {entity_key}.{name}")
    if active & {"minimum", "maximum"} and field_type not in {"integer", "number"}:
        errors.append(f"numeric property used on non-numeric field: {entity_key}.{name}")
    if active & {"date_past", "date_future"} and field_type not in {"date", "datetime"}:
        errors.append(f"date property used on non-date field: {entity_key}.{name}")
    if "enum" in active and field_type != "string":
        errors.append(f"enum property used on non-string field: {entity_key}.{name}")
    if properties.get("min_length") is not None and properties.get("max_length") is not None:
        if properties["min_length"] > properties["max_length"]:
            errors.append(f"invalid length range: {entity_key}.{name}")
    if properties.get("minimum") is not None and properties.get("maximum") is not None:
        if properties["minimum"] > properties["maximum"]:
            errors.append(f"invalid numeric range: {entity_key}.{name}")
    return errors


def _validate_decision_shape(phase: str, node_id: str, decision: Any) -> list[str]:
    if not isinstance(decision, dict):
        return ["decision must be an object"]
    errors: list[str] = []
    if decision.get("requirement_id") != node_id:
        errors.append("requirement_id does not match target")
    required_arrays = {
        "pass1_entities": ("reuse_entities", "new_entities"),
        "pass2_fields": ("entities",),
        "pass3_relationships": ("relationships",),
    }[phase]
    errors.extend(f"{key} must be an array" for key in required_arrays if not isinstance(decision.get(key), list))
    if errors:
        return errors
    if phase == "pass1_entities":
        if any(not isinstance(value, str) for value in decision["reuse_entities"]):
            errors.append("Pass 1 reuse_entities contains a non-string value")
        for item in decision["new_entities"]:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("description"), str):
                errors.append("Pass 1 new_entities contains an invalid entity")
    elif phase == "pass2_fields":
        for item in decision["entities"]:
            if not isinstance(item, dict) or not isinstance(item.get("entity"), str):
                errors.append("Pass 2 contains an invalid entity decision")
                continue
            if not isinstance(item.get("reuse_fields"), list) or not isinstance(item.get("new_fields"), list):
                errors.append("Pass 2 field lists must be arrays")
                continue
            if any(not isinstance(value, str) for value in item["reuse_fields"]):
                errors.append("Pass 2 reuse_fields contains a non-string value")
            for field_item in item["new_fields"]:
                if not isinstance(field_item, dict):
                    errors.append("Pass 2 new_fields contains a non-object")
                    continue
                if field_item.get("type") not in FIELD_TYPES - {"uuid", "foreign_key"}:
                    errors.append("Pass 2 new field has an invalid type")
                if not isinstance(field_item.get("name"), str) or not isinstance(field_item.get("nullable"), bool):
                    errors.append("Pass 2 new field has an invalid name or nullable flag")
                if not isinstance(field_item.get("properties"), dict):
                    errors.append("Pass 2 new field properties must be an object")
                    continue
                errors.extend(
                    f"Pass 2 invalid field properties: {error}"
                    for error in _field_property_errors(
                        "decision",
                        {
                            "name": field_item.get("name"),
                            "type": field_item.get("type"),
                            "nullable": field_item.get("nullable"),
                            "properties": _compact_properties(field_item.get("properties")),
                        },
                    )
                )
    elif phase == "pass3_relationships":
        for item in decision["relationships"]:
            if not isinstance(item, dict):
                errors.append("Pass 3 relationships contains a non-object")
                continue
            if item.get("type") not in RELATIONSHIP_TYPES or not isinstance(item.get("child_required"), bool):
                errors.append("Pass 3 relationship has invalid cardinality or required flag")
            if not isinstance(item.get("parent"), str) or not isinstance(item.get("child"), str):
                errors.append("Pass 3 relationship has invalid endpoints")
    return errors


def _validate_decision_context(
    phase: str,
    decision: Any,
    context: dict[str, Any],
) -> list[str]:
    """Reject structurally valid decisions that contradict their supplied slice."""

    if not isinstance(decision, dict):
        return []
    errors: list[str] = []
    if phase == "pass1_entities":
        existing = {
            _normalize_identifier(str(item.get("key", "")))
            for item in context.get("existing_entities", [])
            if isinstance(item, dict)
        }
        reused = {
            _normalize_identifier(str(value))
            for value in decision.get("reuse_entities", [])
            if isinstance(value, str)
        }
        if reused - existing:
            errors.append("Pass 1 reuses unknown entities: " + ", ".join(sorted(reused - existing)))
        return errors

    related_entities = context.get("related_entities")
    if phase in {"pass2_fields", "pass3_relationships"} and not isinstance(related_entities, list):
        return []
    related_by_key = {
        _normalize_identifier(str(item.get("key", ""))): item
        for item in related_entities or []
        if isinstance(item, dict)
    }
    related = set(related_by_key)

    if phase == "pass2_fields":
        entities = decision.get("entities")
        if not isinstance(entities, list):
            return []
        actual = {
            _normalize_identifier(str(item.get("entity", "")))
            for item in entities
            if isinstance(item, dict)
        }
        if related - actual:
            errors.append("Pass 2 omitted related entities: " + ", ".join(sorted(related - actual)))
        if actual - related:
            errors.append(
                "Pass 2 referenced entities outside its supplied slice: "
                + ", ".join(sorted(actual - related))
            )
        for item in entities:
            if not isinstance(item, dict):
                continue
            key = _normalize_identifier(str(item.get("entity", "")))
            known_fields = {
                _normalize_identifier(str(field_item.get("name", "")))
                for field_item in related_by_key.get(key, {}).get("fields", [])
                if isinstance(field_item, dict)
            }
            reused = {
                _normalize_identifier(str(value))
                for value in item.get("reuse_fields", [])
                if isinstance(value, str)
            }
            if reused - known_fields:
                errors.append(
                    f"Pass 2 reuses unknown fields on {key}: "
                    + ", ".join(sorted(reused - known_fields))
                )
            new_names = {
                _normalize_identifier(str(field_item.get("name", "")))
                for field_item in item.get("new_fields", [])
                if isinstance(field_item, dict)
            }
            forbidden = {name for name in new_names if name == "id" or name.endswith("_id")}
            if forbidden:
                errors.append(
                    f"Pass 2 cannot create system or foreign-key fields on {key}: "
                    + ", ".join(sorted(forbidden))
                )
            if new_names & known_fields:
                errors.append(
                    f"Pass 2 redeclares existing fields on {key}; use reuse_fields: "
                    + ", ".join(sorted(new_names & known_fields))
                )
        return errors

    if phase == "pass3_relationships":
        seen: dict[tuple[str, str], str] = {}
        for item in decision.get("relationships", []):
            if not isinstance(item, dict):
                continue
            parent = _normalize_identifier(str(item.get("parent", "")))
            child = _normalize_identifier(str(item.get("child", "")))
            relationship_type = str(item.get("type", ""))
            if parent not in related or child not in related or parent == child:
                # Ignore out-of-slice and self edges. They can be introduced
                # later once both endpoints are present in one design slice.
                continue
            pair = (parent, child)
            if pair in seen and seen[pair] != relationship_type:
                errors.append(f"Pass 3 emits conflicting cardinalities: {parent} -> {child}")
            seen[pair] = relationship_type
        for existing in context.get("existing_relationships", []):
            if not isinstance(existing, dict):
                continue
            pair = (str(existing.get("parent", "")), str(existing.get("child", "")))
            if pair in seen and seen[pair] != existing.get("type"):
                errors.append(f"Pass 3 conflicts with an existing relationship: {pair[0]} -> {pair[1]}")
        return errors

    return errors


def _normalize_decision_context(
    phase: str,
    decision: Any,
    context: dict[str, Any],
) -> tuple[Any, list[str]]:
    """Repair omissions and repetitions whose meaning is deterministic."""


    if phase != "pass2_fields" or not isinstance(decision, dict):
        return decision, []
    entities = decision.get("entities")
    related_entities = context.get("related_entities")
    if (
        not isinstance(entities, list)
        or not isinstance(related_entities, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("entity"), str)
            or not isinstance(item.get("reuse_fields"), list)
            or not isinstance(item.get("new_fields"), list)
            for item in entities
        )
    ):
        return decision, []

    normalized = copy.deepcopy(decision)
    grouped: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for item in normalized["entities"]:
        key = _normalize_identifier(item["entity"])
        target = grouped.get(key)
        if target is None:
            target = {"entity": key, "reuse_fields": [], "new_fields": []}
            grouped[key] = target
        else:
            notes.append(f"merged_repeated_entity:{key}")
        for field_name in item["reuse_fields"]:
            if field_name not in target["reuse_fields"]:
                target["reuse_fields"].append(field_name)
        for field_item in item["new_fields"]:
            if field_item not in target["new_fields"]:
                target["new_fields"].append(field_item)

    expected = [
        _normalize_identifier(str(item.get("key", "")))
        for item in related_entities
        if isinstance(item, dict)
    ]
    for key in expected:
        if key not in grouped:
            grouped[key] = {"entity": key, "reuse_fields": [], "new_fields": []}
            notes.append(f"completed_missing_entity:{key}")
    ordered_keys = expected + sorted(set(grouped) - set(expected))
    normalized["entities"] = [grouped[key] for key in ordered_keys]
    return normalized, notes


def _node_context(
    node: dict[str, Any],
    nodes: dict[str, Any],
    effective_dependencies: Any,
) -> dict[str, Any]:
    ancestors: list[dict[str, str]] = []
    parent_id = node.get("parent_id")
    while isinstance(parent_id, str) and parent_id:
        parent = nodes.get(parent_id)
        if not isinstance(parent, dict):
            break
        ancestors.append({
            "id": parent_id,
            "name": str(parent.get("name", "")),
            "description": str(parent.get("description", "")),
        })
        parent_id = parent.get("parent_id")
    ancestors.reverse()
    return {
        "requirement_id": str(node.get("id", "")),
        "name": str(node.get("name", "")),
        "description": str(node.get("description", "")),
        "scenarios": copy.deepcopy(node.get("scenarios", [])),
        "dependencies": copy.deepcopy(effective_dependencies),
        "ancestors": ancestors,
    }


def _compact_properties(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if value is not None and not (key in {"date_past", "date_future"} and value is False)
    }


def _merge_properties(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    entity: str,
    field_name: str,
    requirement_id: str,
    errors: list[str],
) -> None:
    for key, value in _compact_properties(incoming).items():
        if key in existing and existing[key] != value:
            errors.append(_format_error("ARC2228", f"Conflicting field property: {entity}.{field_name}.{key}.", node_id=requirement_id))
        else:
            existing[key] = copy.deepcopy(value)


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _relationship_id(parent: str, child: str, relationship_type: str) -> str:
    return f"relationship_{parent}_{child}_{relationship_type.lower()}"


def _association_entity_key(parent: str, child: str) -> str:
    return f"{parent}_{child}_association"


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
        values.sort()


def _format_error(code: str, message: str, *, node_id: str | None = None) -> str:
    return f"{code}: {message}" + (f" ({node_id})" if node_id else "")


def _ordered_waves(atomic_ids: Any, dependency_graph: dict[str, Any]) -> list[list[str]]:
    declared = {str(item) for item in atomic_ids if str(item)} if isinstance(atomic_ids, list) else set()
    result: list[list[str]] = []
    seen: set[str] = set()
    raw_waves = dependency_graph.get("atomic_implementation_waves")
    if not isinstance(raw_waves, list) or not raw_waves:
        raw_waves = dependency_graph.get("implementation_waves", [])
    for raw_wave in raw_waves:
        if not isinstance(raw_wave, list):
            continue
        wave = sorted(({str(item) for item in raw_wave} & declared) - seen)
        if wave:
            result.append(wave)
            seen.update(wave)
    if declared - seen:
        result.append(sorted(declared - seen))
    return result


def _context_audit(
    *,
    phase: str,
    requirement_id: str,
    attempt: int,
    request_payload: dict[str, Any],
) -> str:
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    input_payload = request_payload.get("input_payload", {})
    section_sizes = sorted(
        (
            (str(key), size(value))
            for key, value in input_payload.items()
        )
        if isinstance(input_payload, dict)
        else [],
        key=lambda item: item[1],
        reverse=True,
    )
    model_context = {
        "instructions": request_payload.get("instructions", ""),
        "input_payload": input_payload,
        "output_schema": request_payload.get("output_schema", {}),
    }
    return (
        f"CONTEXT_AUDIT phase={phase} requirement={requirement_id} attempt={attempt} "
        f"context_total_chars={size(model_context)} "
        f"instructions_chars={size(request_payload.get('instructions', ''))} "
        f"input_payload_chars={size(input_payload)} "
        f"output_schema_chars={size(request_payload.get('output_schema', {}))} "
        f"section_chars="
        + ",".join(f"{key}:{value}" for key, value in section_sizes)
    )


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.environ.get(name, str(default))), maximum))
    except ValueError:
        return default


def _enabled_env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


__all__ = [
    "DatabasePassResult",
    "DatabaseSchemaPass",
    "DatabaseSchemaState",
    "ENTITY_DECISION_SCHEMA",
    "ENTITY_INSTRUCTIONS",
    "FIELD_DECISION_SCHEMA",
    "FIELD_INSTRUCTIONS",
    "RELATIONSHIP_DECISION_SCHEMA",
    "RELATIONSHIP_INSTRUCTIONS",
    "database_traceability",
    "schema_for_requirement",
    "validate_database_schema",
]
