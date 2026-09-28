"""Small model-facing decisions; metadata and full IR rows belong to the compiler."""

from __future__ import annotations

from typing import Any


TABLES = {
    "components": "C", "data": "D", "properties": "P", "ui": "U",
    "events": "E", "handlers": "H", "effects": "F",
}


def obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def nullable(value: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [value, {"type": "null"}]}


def enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


STRING = {"type": "string"}
STRINGS = array(STRING)
EXPR = ref("expression")
TYPE = ref("value_type")
ARGUMENTS = array(obj({"parameter_id": STRING, "value": EXPR}))
DEFS = {
    "value_type": {"anyOf": [
        obj({"kind": enum("string", "number", "integer", "boolean", "null", "ui")}),
        obj({"kind": enum("array"), "items": TYPE}),
        obj({"kind": enum("object"), "fields": array(obj({
            "name": STRING, "type": TYPE, "required": {"type": "boolean"},
        }))}),
        obj({"kind": enum("union"), "variants": array(TYPE)}),
    ]},
    "expression": {"anyOf": [
        # A JSON literal string keeps arbitrary object literals compatible with strict providers.
        # Only this leaf is decoded; expressions themselves are never executable strings.
        obj({"kind": enum("LITERAL"), "value_json": STRING}),
        obj({"kind": enum("REF"), "ref_id": STRING, "path": ref("path")}),
        obj({"kind": enum("ITEM"), "ui_id": STRING, "path": ref("path")}),
        obj({"kind": enum("UI_REF"), "ui_id": STRING}),
        obj({"kind": enum("OP"), "operator": enum(
            "NOT", "LENGTH", "IF", "AND", "OR", "EQ", "NE", "GT", "GE", "LT", "LE",
            "ADD", "SUB", "MUL", "DIV", "CONCAT", "COALESCE",
        ), "args": array(EXPR)}),
    ]},
    "path": array({"anyOf": [STRING, {"type": "integer"}]}),
}

FIELD_SCHEMAS = {
    "name": STRING, "spec": STRING, "owner_id": STRING,
    "direction": enum("INPUT", "OUTPUT"), "type": TYPE,
    "required": {"type": "boolean"}, "default": nullable(EXPR),
    "kind": enum("STATE", "DERIVED", "REF", "ELEMENT", "TEXT", "FRAGMENT", "COMPONENT",
                 "SLOT", "UI", "CUSTOM", "REQUEST", "SUBSCRIPTION", "STORAGE", "DOM",
                 "NAVIGATION", "TIMER", "OTHER"),
    "initial": nullable(EXPR), "derive": nullable(EXPR),
    "element": nullable(STRING), "component_ref": nullable(STRING),
    "slot_data_id": nullable(STRING),
    "attributes": array(obj({"name": STRING, "value": EXPR})),
    "text": nullable(EXPR), "children": STRINGS, "condition": nullable(EXPR),
    "repeat": nullable(obj({"source": EXPR, "item_name": STRING, "key": EXPR})),
    "arguments": ARGUMENTS,
    "callbacks": array(obj({"event_id": STRING, "handler_id": STRING, "arguments": ARGUMENTS})),
    "presentation": STRING, "ui_id": nullable(STRING), "event_name": STRING,
    "handler_id": nullable(STRING), "reads": STRINGS, "writes": STRINGS,
    "invokes": array(obj({"effect_id": STRING, "arguments": ARGUMENTS})),
    "emits": array(obj({"event_id": STRING, "arguments": ARGUMENTS})),
    "activation": enum("INVOKED", "REACTIVE"), "dependencies": STRINGS,
    "target": STRING, "async_policy": enum("NOT_ASYNC", "LATEST_WINS", "SERIAL", "PARALLEL"),
    "cleanup": nullable(STRING),
}

ENTITY_FIELDS = {
    "components": {"name", "spec"},
    "data": {"name", "spec", "owner_id", "direction", "type", "required", "default"},
    "properties": {"name", "spec", "kind", "type", "initial", "derive"},
    "ui": {"name", "spec", "kind", "element", "component_ref", "slot_data_id", "attributes",
           "text", "children", "condition", "repeat", "arguments", "callbacks", "presentation"},
    "events": {"name", "spec", "kind", "ui_id", "event_name", "handler_id", "arguments"},
    "handlers": {"name", "spec", "reads", "writes", "invokes", "emits"},
    "effects": {"name", "spec", "kind", "activation", "dependencies", "arguments", "reads",
                "writes", "target", "async_policy", "cleanup"},
}

OBSERVATION_SCHEMA = obj({"observations": array(obj({
    "existing_observation_id": nullable(STRING), "name": STRING, "spec": STRING,
    "content": array(obj({"name": STRING, "mode": enum("STATIC", "DYNAMIC", "INPUT", "REPEAT"),
                          "type_hint": STRING, "description": STRING})),
    "structure_hint": STRING,
    "behavior_hints": array(obj({"name": STRING,
        "trigger": enum("UI", "MOUNT", "DEPENDENCY", "CLEANUP", "CUSTOM"),
        "description": STRING, "api_ids": STRINGS})),
}))})

ASSEMBLY_SCHEMA = obj({
    "components": array(obj({
        "key": STRING, "existing_component_id": nullable(STRING), "name": STRING,
        "spec": STRING, "observation_ids": STRINGS,
        "inputs": array(obj({"key": STRING, "name": STRING, "spec": STRING, "type": TYPE,
                             "required": {"type": "boolean"}, "default": nullable(EXPR)})),
        "properties": array(obj({"key": STRING, "name": STRING, "spec": STRING,
            "kind": enum("STATE", "DERIVED", "REF"), "type": TYPE,
            "initial": nullable(EXPR), "derive": nullable(EXPR)})),
    })),
    "placements": array(obj({"parent_ref": STRING, "child_ref": STRING, "spec": STRING})),
})
ASSEMBLY_SCHEMA["$defs"] = DEFS


def patch_schema(stage: str) -> dict[str, Any]:
    allowed = ["data", "properties", "ui"] if stage == "ui" else [
        "data", "properties", "ui", "events", "handlers", "effects",
    ]
    names = set().union(*(ENTITY_FIELDS[t] for t in allowed))
    fields = array(ref("edit_field"))
    schema = obj({
        "creates": array(obj({"table": enum(*allowed), "key": STRING, "fields": fields})),
        "updates": array(obj({"id": STRING, "fields": fields})),
    })
    schema["$defs"] = {**DEFS, "edit_field": {"anyOf": [
        obj({"field": enum(name), "value": FIELD_SCHEMAS[name]}) for name in sorted(names)
    ]}}
    return schema


COMMON = """Design React UI from the supplied requirements. Return only the small requested decision.
Never output source files, JSX, global IDs for new entities, or run metadata.
Reuse supplied IDs and API contracts; do not invent APIs. A screenshot provides appearance, not hidden behavior.
Keep names/specs concise but make behavior precise. Do not repeat the complete application IR.
Keep the response under 20,000 characters. Prefer a handful of meaningful records per call.
"""
OBSERVATION_INSTRUCTIONS = COMMON + """Identify UI regions, displayed/input data, layout hints and behaviors.
Return observations only for this requirement fragment or image. Use existing_observation_id for an explicit
match in the supplied catalog; otherwise null. Backend-only text may produce an empty observations array.
Include loading/empty/error states when required. Repeated rows are one template, not copies per sample item.
Record UI interaction and mount/dependency/cleanup behaviors; do not design handlers or components yet.
"""
ASSEMBLY_INSTRUCTIONS = COMMON + """Assign the supplied observations to reusable components. App already exists.
Use existing_component_id to extend a supplied component, otherwise null with a unique local key.
Reference newly declared components/inputs/properties as @key. All keys in this response must be unique.
Declare only direct placements needed to connect these components, in display order. Do not repeat an existing
placement. A component can have multiple use sites. Do not create a new root or copy the App component.
Use few cohesive components. Declare known shared state and child input contracts now; empty arrays are fine.
Every supplied observation must be assigned. Component observations are task associations, not final IR fields.
Reuse existing input/property IDs from the catalog; do not redeclare them. Initial/default expressions use
LITERAL with value_json (JSON text), or REF with ref_id and path; new parameter references use @key.
"""
PATCH_INSTRUCTIONS = COMMON + """Return at most 32 creates and updates in total, each with fields:[{field,value}]. Omitted fields
are unchanged. Arrays replace the complete field: preserve existing siblings/attributes/callbacks.
New entities have a unique key; references to new entities use @key, existing entities use their ID.
The compiler supplies id, component_id, requirement_ids and irrelevant empty fields. Data must specify owner_id
(a Component, Event, Handler or Effect). Do not change entity identity or ownership. You can update only IDs
provided in editable_ids. Related component contracts are read-only.
Types: {kind:string|number|integer|boolean|null|ui}, array with items, object with fields[{name,type,required}],
or union with variants. Pure expressions: LITERAL with value_json (JSON-encoded literal, e.g. \"[]\" or \"false\"),
REF with ref_id/path, ITEM with repeat ui_id/path, UI_REF with ui_id, OP with operator/args. Never embed JS.
Data is a boundary parameter: specify owner_id, INPUT/OUTPUT direction, type and required. Component Data is
INPUT; Event Data is OUTPUT. STATE/DERIVED/REF Property is component-owned. Only STATE/REF may be written.
ELEMENT uses element/attributes/children; TEXT uses text; FRAGMENT uses children; COMPONENT uses component_ref,
arguments[{parameter_id,value}], callbacks[{event_id,handler_id,arguments}]; SLOT uses slot_data_id.
Every new UI must be connected to the current region or used through UI_REF. Keep compiler-created roots
and regions; place actual layout inside them. Render conditions and repeat{source,item_name,key} live on UI.
"""
UI_INSTRUCTIONS = PATCH_INSTRUCTIONS + """Expand only the current region (or the supplied assembly skeleton).
Produce UI details and data contracts/state. Fill attributes/text/condition/repeat and child arguments.
Do not create events or effects yet. Do not recreate existing input or state with another ID.
Supply Property initial (STATE/REF) or derive (DERIVED); make nullable/array types explicit.
For the assembly skeleton task, arrange existing region/use-site nodes and model page selection; keep every
existing child reachable, do not render mutually exclusive pages simultaneously without conditions.
"""
BEHAVIOR_INSTRUCTIONS = PATCH_INSTRUCTIONS + """Complete only the supplied behaviors, reusing existing entities.
UI Event: kind UI, ui_id, event_name, handler_id, arguments mapping event OUTPUT Data to Handler INPUT Data.
CUSTOM Event: kind CUSTOM, event_name; notify through Handler.emits and parent UI.callbacks.
Handler: reads, writes, invokes[{effect_id,arguments}], emits[{event_id,arguments}], spec with ordering/guards.
Effect: kind REQUEST/SUBSCRIPTION/STORAGE/DOM/NAVIGATION/TIMER/OTHER, activation INVOKED or REACTIVE,
target, reads/writes, dependencies, arguments, async_policy and cleanup. Only INVOKED effects are invoked by
handlers. REACTIVE effects run on mount/dependency changes and need no fake event; arguments fill their inputs.
Describe loading, success, failure, cancellation and stale results in spec. All behaviors belong to the current
component. Reads are data/property IDs; writes are mutable property IDs. Event payload and Effect result fields
are Data with direction OUTPUT. For list interactions put item/key in event payload. Use actual supplied API IDs.
Behavior Data must explicitly include type, direction, required and owner_id. You may add missing status UI/state.
For use-site wiring tasks complete child input arguments and connect supplied custom events to parent handlers.
"""


def check_shape(value: Any, schema: dict[str, Any], defs: dict[str, Any] | None = None,
                path: str = "$", depth: int = 0) -> None:
    """Validate the small schema vocabulary used above, without a semantic validator dependency."""
    if depth > 80:
        raise ValueError(f"{path}: excessive nesting")
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return check_shape(value, defs[schema["$ref"].split("/")[-1]], defs, path, depth + 1)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                check_shape(value, option, defs, path, depth + 1)
                return
            except ValueError:
                pass
        raise ValueError(f"{path}: does not match an allowed shape")
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "null": value is None}
    if kind and not valid.get(kind, False):
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid value {value!r}")
    if kind == "object":
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - schema["properties"].keys()
        if missing or extra:
            raise ValueError(f"{path}: missing {sorted(missing)}, unknown {sorted(extra)}")
        for key, item in value.items():
            check_shape(item, schema["properties"][key], defs, f"{path}.{key}", depth + 1)
    elif kind == "array":
        for index, item in enumerate(value):
            check_shape(item, schema["items"], defs, f"{path}[{index}]", depth + 1)
