"""Small, implementation-agnostic Frontend Design IR contracts."""

from __future__ import annotations

import copy
from typing import Any

from .frontend_ir import (
    SEMANTIC_FIELD_SCHEMA,
    STORE_ACTION_SCHEMA,
    STORE_PERSISTENCE_SCHEMA,
    VISUAL_REFERENCE_SCHEMA,
    schema_shape_errors,
)

FRONTEND_IR_SCHEMA_VERSION = 6


def _strings(*, maximum: int = 64) -> dict[str, Any]:
    return {"type": "array", "maxItems": maximum, "items": {"type": "string", "minLength": 1}}


NAVIGATION_TARGET_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["trigger", "target_route", "condition"],
    "properties": {
        "trigger": {"type": "string", "minLength": 1},
        "target_route": {"type": "string", "pattern": r"^/"},
        "condition": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
    },
}

SCREEN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "route", "purpose", "route_inputs", "requirement_ids", "entry_conditions", "observable_states", "required_api_ids", "navigation_targets", "visual_reference_ids", "surface_keys"],
    "properties": {
        "id": {"type": "string", "pattern": r"^PAGE\.[A-Za-z][A-Za-z0-9]*$"},
        "route": {"type": "string", "pattern": r"^/"},
        "purpose": {"type": "string", "minLength": 1},
        "route_inputs": {"type": "array", "items": SEMANTIC_FIELD_SCHEMA},
        "requirement_ids": _strings(), "entry_conditions": _strings(maximum=16),
        "observable_states": _strings(maximum=24), "required_api_ids": _strings(maximum=24),
        "navigation_targets": {"type": "array", "items": NAVIGATION_TARGET_SCHEMA},
        "visual_reference_ids": _strings(maximum=24),
        "surface_keys": _strings(maximum=32),
    },
}

SCREEN_COMPONENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "screen_id", "purpose", "requirement_ids", "inputs", "required_api_ids", "shared_state_ids", "observable_states", "visual_reference_ids"],
    "properties": {
        "id": {"type": "string", "pattern": r"^COMPONENT\.[A-Za-z][A-Za-z0-9]*$"},
        "screen_id": {"type": "string", "pattern": r"^PAGE\."},
        "purpose": {"type": "string", "minLength": 1},
        "requirement_ids": _strings(),
        "inputs": {"type": "array", "items": SEMANTIC_FIELD_SCHEMA},
        "required_api_ids": _strings(maximum=24),
        "shared_state_ids": _strings(maximum=16),
        "observable_states": _strings(maximum=24),
        "visual_reference_ids": _strings(maximum=24),
    },
}

UI_PLACEMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "requirement_id",
        "ui_scope",
        "screen_id",
        "component_id",
        "strategy",
    ],
    "properties": {
        "requirement_id": {"type": "string", "minLength": 1},
        "ui_scope": {
            "type": "string",
            "enum": ["UI_REQUIRED", "UI_AFFECTING", "NO_UI"],
        },
        "screen_id": {
            "anyOf": [
                {"type": "string", "pattern": r"^PAGE\."},
                {"type": "null"},
            ]
        },
        "component_id": {
            "anyOf": [
                {"type": "string", "pattern": r"^COMPONENT\."},
                {"type": "null"},
            ]
        },
        "strategy": {
            "type": "string",
            "enum": ["USE_SCREEN", "NO_FRONTEND_IMPLEMENTATION"],
        },
    },
}

JOURNEY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "requirement_id", "source_screen_id", "trigger", "api_id", "success_target_route", "failure_behavior"],
    "properties": {
        "id": {"type": "string", "pattern": r"^JOURNEY\.[A-Za-z][A-Za-z0-9]*$"},
        "requirement_id": {"type": "string", "minLength": 1},
        "source_screen_id": {"type": "string", "pattern": r"^PAGE\."},
        "trigger": {"type": "string", "minLength": 1},
        "api_id": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
        "success_target_route": {"anyOf": [{"type": "string", "pattern": r"^/"}, {"type": "null"}]},
        "failure_behavior": {"type": "string", "minLength": 1},
    },
}

API_BINDING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["source_semantic_id", "target_semantic_id"],
    "properties": {"source_semantic_id": {"type": "string", "minLength": 1}, "target_semantic_id": {"type": "string", "minLength": 1}},
}
API_USAGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["screen_id", "api_id", "request_bindings", "response_bindings"],
    "properties": {
        "screen_id": {"type": "string", "pattern": r"^PAGE\."}, "api_id": {"type": "string", "minLength": 1},
        "request_bindings": {"type": "array", "items": API_BINDING_SCHEMA},
        "response_bindings": {"type": "array", "items": API_BINDING_SCHEMA},
    },
}
SHARED_STATE_POLICY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "purpose", "state", "actions", "persistence", "requirement_ids", "consumer_screen_ids", "consumer_surface_keys"],
    "properties": {
        "id": {"type": "string", "pattern": r"^STORE\.[A-Za-z][A-Za-z0-9]*$"},
        "purpose": {"type": "string", "minLength": 1},
        "state": {"type": "array", "items": SEMANTIC_FIELD_SCHEMA},
        "actions": {"type": "array", "items": STORE_ACTION_SCHEMA},
        "persistence": STORE_PERSISTENCE_SCHEMA,
        "requirement_ids": _strings(),
        "consumer_screen_ids": _strings(),
        "consumer_surface_keys": _strings(maximum=32),
    },
}
REQUIREMENT_LINK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["requirement_id", "ui_scope", "screen_ids", "shared_state_ids", "visual_reference_ids"],
    "properties": {
        "requirement_id": {"type": "string", "minLength": 1},
        "ui_scope": {"type": "string", "enum": ["UI_REQUIRED", "UI_AFFECTING", "NO_UI"]},
        "screen_ids": _strings(), "shared_state_ids": _strings(), "visual_reference_ids": _strings(),
    },
}

OPTIONAL_FRONTEND_DESIGN_TABLES = ("screen_components",)

FRONTEND_DESIGN_TABLE_SCHEMAS = {
    "visual_references": {"type": "array", "items": VISUAL_REFERENCE_SCHEMA},
    "screens": {"type": "array", "items": SCREEN_SCHEMA},
    "placements": {"type": "array", "items": UI_PLACEMENT_SCHEMA},
    "screen_components": {"type": "array", "items": SCREEN_COMPONENT_SCHEMA},
    "journeys": {"type": "array", "items": JOURNEY_SCHEMA},
    "api_usages": {"type": "array", "items": API_USAGE_SCHEMA},
    "shared_state_policies": {"type": "array", "items": SHARED_STATE_POLICY_SCHEMA},
}
FRONTEND_DESIGN_IR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": [
        "schema_version",
        *(key for key in FRONTEND_DESIGN_TABLE_SCHEMAS if key not in OPTIONAL_FRONTEND_DESIGN_TABLES),
        "requirement_links",
    ],
    "properties": {"schema_version": {"type": "integer", "const": FRONTEND_IR_SCHEMA_VERSION}, **FRONTEND_DESIGN_TABLE_SCHEMAS, "requirement_links": {"type": "array", "items": REQUIREMENT_LINK_SCHEMA}},
}


def repair_shape(value: Any, schema: dict[str, Any]) -> Any:
    if "anyOf" in schema:
        if value is None:
            return None
        return repair_shape(value, next((row for row in schema["anyOf"] if row.get("type") != "null"), {}))
    kind = schema.get("type")
    if kind == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        return {key: repair_shape(item, properties[key]) for key, item in value.items() if key in properties}
    if kind == "array" and isinstance(value, list):
        rows = value[:schema["maxItems"]] if isinstance(schema.get("maxItems"), int) else value
        return [repair_shape(row, schema.get("items", {})) for row in rows]
    return copy.deepcopy(value)


def shape_errors(value: Any) -> list[str]:
    return schema_shape_errors(value, FRONTEND_DESIGN_IR_SCHEMA)
