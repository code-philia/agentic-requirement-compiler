"""Small model-facing decisions; metadata and full IR rows belong to the compiler."""

from __future__ import annotations

import json
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
REFERENCE = ref("entity_reference")
REFERENCES = array(REFERENCE)
ARGUMENTS = array(obj({"parameter_id": REFERENCE, "value": EXPR}))
DEFS = {
    "entity_reference": {"anyOf": [obj({"id": STRING}), obj({"local": STRING})]},
    "json_value": {"anyOf": [
        STRING, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
        array(ref("json_value")),
        {"type": "object", "properties": {}, "additionalProperties": ref("json_value")},
    ]},
    "value_type": {"anyOf": [
        obj({"kind": enum("string", "number", "integer", "boolean", "null", "ui")}),
        obj({"kind": enum("array"), "items": TYPE}),
        obj({"kind": enum("object"), "fields": array(obj({
            "name": STRING, "type": TYPE, "required": {"type": "boolean"},
        }))}),
        obj({"kind": enum("union"), "variants": array(TYPE)}),
    ]},
    "expression": {"anyOf": [
        obj({"kind": enum("LITERAL"), "value": ref("json_value")}),
        obj({"kind": enum("REF"), "ref_id": REFERENCE, "path": ref("path")}),
        obj({"kind": enum("ITEM"), "ui_id": REFERENCE, "path": ref("path")}),
        obj({"kind": enum("UI_REF"), "ui_id": REFERENCE}),
        obj({"kind": enum("OP"), "operator": enum(
            "NOT", "LENGTH", "IF", "AND", "OR", "EQ", "NE", "GT", "GE", "LT", "LE",
            "ADD", "SUB", "MUL", "DIV", "CONCAT", "COALESCE",
        ), "args": array(EXPR)}),
    ]},
    "path": array({"anyOf": [STRING, {"type": "integer"}]}),
}

FIELD_SCHEMAS = {
    "name": STRING, "spec": STRING, "owner_id": REFERENCE,
    "direction": enum("INPUT", "OUTPUT"), "type": TYPE,
    "required": {"type": "boolean"}, "default": nullable(EXPR),
    "kind": enum("STATE", "DERIVED", "REF", "ELEMENT", "TEXT", "FRAGMENT", "COMPONENT",
                 "SLOT", "UI", "CUSTOM", "REQUEST", "SUBSCRIPTION", "STORAGE", "DOM",
                 "NAVIGATION", "TIMER", "OTHER"),
    "initial": nullable(EXPR), "derive": nullable(EXPR),
    "element": nullable(STRING), "component_ref": nullable(REFERENCE),
    "slot_data_id": nullable(REFERENCE),
    "attributes": array(obj({"name": STRING, "value": EXPR})),
    "text": nullable(EXPR), "children": REFERENCES, "condition": nullable(EXPR),
    "repeat": nullable(obj({"source": EXPR, "item_name": STRING, "key": EXPR})),
    "arguments": ARGUMENTS,
    "callbacks": array(obj({"event_id": REFERENCE, "handler_id": REFERENCE, "arguments": ARGUMENTS})),
    "presentation": STRING, "ui_id": nullable(REFERENCE), "event_name": STRING,
    "handler_id": nullable(REFERENCE), "reads": REFERENCES, "writes": REFERENCES,
    "invokes": array(obj({"effect_id": REFERENCE, "arguments": ARGUMENTS})),
    "emits": array(obj({"event_id": REFERENCE, "arguments": ARGUMENTS})),
    "activation": enum("INVOKED", "REACTIVE"), "dependencies": REFERENCES,
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



KIND_VALUES = {
    "properties": ("STATE", "DERIVED", "REF"),
    "ui": ("ELEMENT", "TEXT", "FRAGMENT", "COMPONENT", "SLOT"),
    "events": ("UI", "CUSTOM"),
    "effects": ("REQUEST", "SUBSCRIPTION", "STORAGE", "DOM", "NAVIGATION", "TIMER", "OTHER"),
}


def entity_field_schema(table: str, name: str) -> dict[str, Any]:
    if name == "kind":
        return enum(*KIND_VALUES[table])
    return FIELD_SCHEMAS[name]


def record_schema(table: str, *, create: bool) -> dict[str, Any]:
    """Full final-record fields for every kind; only compiler metadata is optional."""
    fields = {name: entity_field_schema(table, name) for name in sorted(ENTITY_FIELDS[table])}
    fields.update(id=nullable(STRING) if create else STRING, requirement_ids=nullable(STRINGS))
    ignored = {"requirement_ids"}
    if create:
        fields["key"] = STRING
        ignored.add("id")
    if table not in {"data", "components"}:
        fields["component_id"] = nullable(REFERENCE) if create else REFERENCE
    if table == "components":
        fields["ui_root_id"] = nullable(REFERENCE)
        ignored.add("ui_root_id")
    schema = obj(fields)
    schema["required"] = [name for name in fields if name not in ignored]
    for name in ignored:
        schema["properties"][name] = {**schema["properties"][name],
                                     "description": "Compiler-owned: omit or echo; never used to change compiler metadata."}
    return schema


def create_schema(table: str) -> dict[str, Any]:
    return record_schema(table, create=True)


def patch_schema(stage: str) -> dict[str, Any]:
    allowed = ["data", "properties", "ui"] if stage in {"ui", "assemble"} else [
        "data", "properties", "ui", "events", "handlers", "effects",
    ]
    # Both creates and updates select an entity-specific schema. No shared union of fields.
    definitions = dict(DEFS)
    update_variants = []
    for table in [*allowed, "components"]:
        update_variants.append(obj({"table": enum(table), "record": record_schema(table, create=False)}))
    schema = obj({
        "creates": obj({table: array(create_schema(table)) for table in allowed}),
        "updates": array({"anyOf": update_variants}),
        "associations": REFERENCES,
    })
    schema["$defs"] = definitions
    # Arbitrary JSON literals and omitted update fields intentionally use JSON mode.
    schema["x-arc-output-mode"] = "json_object"
    if stage == "ui":
        schema["properties"]["requirement_mode"] = enum("DESIGN", "SUMMARY", "NONE")
        schema["required"].append("requirement_mode")
    if stage == "assemble":
        schema["properties"]["components"] = array(obj({
            "key": STRING, "existing_component_id": nullable(REFERENCE), "name": STRING, "spec": STRING,
            "ui_ids": REFERENCES, "property_ids": REFERENCES, "data_ids": REFERENCES,
        }))
        schema["required"].append("components")
    return schema


def normalize_patch(value: dict[str, Any]) -> dict[str, Any]:
    """Convert the typed model protocol into the existing atomic workspace edit protocol."""
    creates = []
    for table, rows in value["creates"].items():
        for row in rows:
            creates.append({"table": table, "key": row["key"],
                            "component_ref": row.get("component_id"),
                            "fields": [{"field": key, "value": item} for key, item in row.items()
                                       if key in ENTITY_FIELDS[table]]})
    updates = [{"table": row["table"], "id": {"id": row["record"]["id"]},
                "component_id": row["record"].get("component_id"),
                "ui_root_id": row["record"].get("ui_root_id"),
                "fields": [{"field": k, "value": v} for k, v in row["record"].items()
                           if k in ENTITY_FIELDS[row["table"]]]}
               for row in value["updates"]]
    return {**value, "creates": creates, "updates": updates}


COMMON = """Design React UI from the supplied requirements. Return only the small requested decision.
Never output source files, JSX, global IDs for new entities, or run metadata.
Reuse supplied IDs and API contracts; do not invent APIs. A screenshot provides appearance, not hidden behavior.
Keep names/specs concise but make behavior precise. Do not repeat the complete application IR.
Return the complete incremental batch for this requirement; do not return the whole application.
protocol_example is an illustrative patch for a hypothetical empty component, not a feature request.
Never copy its EXAMPLE IDs or invent its search feature; replace them with supplied IDs and actual requirement data.
Reuse already generated entities across all requirements.
"""
PATCH_INSTRUCTIONS = COMMON + """Return creates as an OBJECT of entity arrays: data, properties, ui (and in the
behavior pass events, handlers, effects). Each new entity is a named object, NOT a fields list. Use [] for empty
arrays. Updates use {table,record:{...full entity record...}}.
Creates and updates use the FULL final entity field set, identical to supplied entities; never shorten by kind.
Updates replace all model-owned fields: copy the existing record and modify it, preserving siblings and callbacks.
Prefer updating the existing UI node over replacing it with a new node for the same role.
For example, change an existing TEXT node's text from LITERAL to a state-dependent expression while preserving
its ID and its parent's children. Do not create replacement captions and disconnect the old nodes.
Create new UI only for genuinely new roles or occurrences; associate unchanged reused UI without copying it.
Only compiler-owned id (on creates), requirement_ids, and component ui_root_id may be omitted or echoed.
Compiler-owned fields are explicitly described in Schema and compiler_fields; they are not model decisions.
New entities have a unique plain key. ALL entity references are objects: {local:key} for new entities,
{id:existing_id} for existing entities. Never use @ prefixes or bare reference strings.
Catalog identity id/key fields are strings; relationship fields are reference objects.
Property/UI/Event/Handler/Effect records use component_id for their owning component. On creates only,
component_id:null selects default_component_id. Data uses owner_id, and has no component_id.
For a COMPONENT UI, component_ref means the CHILD component rendered, not the owner.
The compiler allocates new id and requirement_ids. Include ALL entity fields, even those inactive for this kind:
inactive scalars are null and inactive arrays are []. Data must specify owner_id
(a Component, Event, Handler or Effect). Do not change entity identity or ownership through fields.
You may edit multiple components in ONE response, but update only IDs provided in editable_ids.
Return associations: references to existing or newly created entities directly relevant to this requirement.
Reuse without modification by returning associations only; do not copy or update a record just to associate it.
Reference validity is global; editable_ids restricts mutations, not reads or retained references.
Preserve existing references even when the referenced full record is omitted from this local context.
Types: {kind:string|number|integer|boolean|null|ui}, array with items, object with fields[{name,type,required}],
or union with variants. Pure expressions: LITERAL with native JSON value (string, number, boolean, null, array or object),
REF with ref_id/path, ITEM with repeat ui_id/path, UI_REF with ui_id, OP with operator/args. Never embed JS.
Data is a boundary parameter: specify owner_id, INPUT/OUTPUT direction, type and required. Component Data is
INPUT; Event Data is OUTPUT. STATE/DERIVED/REF Property is component-owned. Only STATE/REF may be written.
DECISION RULE: internal session/form/search state belongs in creates.properties, NEVER creates.data.
Data has no kind/initial/derive. Property has no owner_id/direction/required/default/presentation.
Property STATE/REF has initial and derive:null; DERIVED has initial:null and derive. Always include both fields.
Do not invent UI fields on Property: use spec for its meaning, not presentation.
ELEMENT uses element/attributes/children; TEXT uses text; FRAGMENT uses children; COMPONENT uses component_ref,
arguments[{parameter_id,value}], callbacks[{event_id,handler_id,arguments}]; SLOT uses slot_data_id.
ELEMENT includes text:null: create a TEXT child for a caption. FRAGMENT includes attributes:[].
Every UI has element, component_ref, slot_data_id, attributes, text, children, condition, repeat, arguments,
callbacks and presentation, regardless of kind. CUSTOM Event has ui_id:null, handler_id:null, arguments:[].
Include name/spec on every new entity. In UI variants condition/repeat may be null, presentation may be empty.
Every new UI must be connected to its component root or used through UI_REF. Keep compiler-created roots
as FRAGMENT; place actual layout inside them. Render conditions and repeat{source,item_name,key} live on UI.
"""
UI_INSTRUCTIONS = PATCH_INSTRUCTIONS + """Visit the CURRENT REQUIREMENT and produce its UI/data as a small increment.
Return requirement_mode DESIGN for independent UI/data/behavior requirements. Return SUMMARY only when
the parent merely summarizes supplied child requirements, has no reference image and no additional behavior.
For SUMMARY return empty creates/updates and associate a few core child entities; do not design another page.
Return NONE only when this requirement has no frontend UI, data, state or behavior to implement and no
reference image. NONE must have empty creates, updates and associations. If behavior may still be needed,
return DESIGN even when this pass creates no UI.
Do not output observations or schedule per-element expansion.
Reuse existing UI by ID when requirements describe the same interface. Backend-only requirements may return
NONE. Initially UI belongs to App; component extraction happens in the assembly pass.
Derive data FROM each UI's displayed content, input value, repeated items and visibility needs, not from an
independent inventory. Determine each UI's concrete data association in this pass. Express it using REF/ITEM
inside text, attributes, condition, repeat.source and arguments; never leave the association only in prose.
Static content uses LITERAL and needs no artificial Data entity. Internal mutable/derived values are Property;
Data remains a boundary parameter. Reuse the same entity when multiple UI nodes consume the same value.
Specify data type, initial/default value where applicable, and source meaning in spec. Do not invent backend APIs.
Fill attributes/text/condition/repeat and child arguments. A stateful input's value must reference its Property;
the event and handler that change it are completed in pass three.
Do not create events or effects yet. Do not recreate existing input or state with another ID.
Supply Property initial (STATE/REF) or derive (DERIVED); make nullable/array types explicit.
"""
REQUIREMENT_ASSEMBLY_INSTRUCTIONS = PATCH_INSTRUCTIONS + """Visit the CURRENT REQUIREMENT again to organize
its ALREADY GENERATED UI into components and layout. Do not regenerate UI or describe unimplemented regions.
This pass ONLY establishes component boundaries, containment and reuse. Each Component will lower to a React
component function; UI nodes remain its render tree, not automatically separate functions.
Preserve the UI-to-data associations established in pass one (ui_data summarizes their referenced entities).
Do not design new business data, user interactions, events, handlers or effects. Ownership moves and boundary
parameter forwarding required by extraction are structural bookkeeping, not an opportunity to redesign data.
Use the supplied reference image AND visual_analysis to infer grouping, nesting, layout and page boundaries.
Group UI into one component when it represents one cohesive purpose, a repeated pattern, a shared data/state
boundary or an independently reusable region (for example a header, form, list or detail panel).
Do not turn every element or visually adjacent group into a component. Appearance alone is not a reuse contract.
A complete page may itself be a component containing region components and remaining UI. App is the composition
root, not automatically the page. Preserve existing child COMPONENT use-sites when extracting a page container.
Before creating anything, inspect component_catalog: requirement summaries, render roots, inputs, custom events
and existing use-sites. Reuse by responsibility and compatible contracts, not just names or visual similarity.
Catalogs are bounded (counts show omissions), referenceable summaries, NOT grants of edit permission.
If an existing component already covers the requirement, associate it and reuse its use-site; no extraction is needed.
For another occurrence, create a COMPONENT UI referencing the existing component and connect its arguments;
do not create a second component definition or move unrelated UI into an existing component merely to reuse it.
Do not assume omitted input/event contracts are absent. Prefer existing use-sites when full contracts are unavailable.
For nested extraction in one batch, use child key.use in the outer declaration when it is the subtree being moved.
Later image supplements refine this same requirement's existing components; do not create one page per screenshot.
Return components plus creates/updates. components may be empty when existing ownership is appropriate.
Each component entry has key, existing_component_id (null for new), name, spec, ui_ids (existing subtree roots),
property_ids and data_ids (existing component inputs). Extraction moves complete UI subtrees, only the explicitly
listed state/inputs, and preserves entity IDs. The compiler connects the extracted component at the old location.
When extending a component already used by the same parent, the existing use-site is reused, not duplicated.
ui_ids must share one current owner. Do not extract App's root. Reuse components deliberately, not by name alone.
Reference this component as {local:key}, its FRAGMENT root as {local:"key.root"}, and its parent use-site as {local:"key.use"}.
These root/use records are CREATED BY THE COMPILER. Never add creates.ui records with key.root or key.use keys.
Only reference those symbols. The compiler also preserves the extracted UI's position; do not duplicate that wiring.
All local symbols are registered before creates are materialized; extraction follows its use-site dependencies.
data_ids/property_ids may also name same-batch creates owned by this component; these are already bound, not moved.
The .use symbol exists only for a declaration extracting a nonempty UI subtree from another component.
Use creates/updates only for structural wrappers/use-sites and the parameter/reference wiring required by extraction.
Preserve existing conditions and data expressions except for necessary boundary reference rewiring.
State shared with UI remaining in the parent stays in the parent; declare child inputs and rewire REF expressions.
Keep all existing siblings connected. Do not display mutually exclusive pages simultaneously.
"""
BEHAVIOR_INSTRUCTIONS = PATCH_INSTRUCTIONS + """Visit the CURRENT REQUIREMENT and complete its behaviors across
all supplied components in one response, reusing existing entities. Also check mount/dependency/cleanup behavior.
This pass owns component coordination and UI behavior, not component decomposition or visual redesign.
Start from each supplied component's assembled UI and the established ui_data associations. Complete the UI's
interactions and the component's Event/Handler/Effect entities around those values, rather than inventing another
data model. Determine event names/payloads, handler reads/writes, effect inputs/results and UI feedback together.
New behavior-only payloads, loading/error state or result Data may be added when scenarios require them;
existing UI data identities and meaning must be retained.
Use requirement scenarios as interaction sequences: trigger, payload, handler, state change or effect, observable
UI result. Reuse existing events/handlers/effects; do not duplicate a behavior already supplied by another requirement.
Keep local state local; coordinate siblings through their common parent. Send child intent through CUSTOM Event,
bind the parent's Handler through COMPONENT UI.callbacks, and pass updated parent state down through Data/arguments.
Never write another component's Property directly. Complete both sides of each required callback/input binding.
For page switching, the parent Handler updates current-page state and COMPONENT UI.condition selects the page.
Use NAVIGATION Effect only when URL/history/external navigation is required; specify destination, parameters,
push/replace/back semantics and any required restoration/popstate synchronization. Do not infer interaction from pixels.
Preserve the assembled tree and ownership. Only add small missing feedback UI/state needed by actual scenarios.
UI Event: kind UI, ui_id, event_name, handler_id, arguments mapping event OUTPUT Data to Handler INPUT Data.
CUSTOM Event: kind CUSTOM, event_name; notify through Handler.emits and parent UI.callbacks.
Handler: reads, writes, invokes[{effect_id,arguments}], emits[{event_id,arguments}], spec with ordering/guards.
Effect: kind REQUEST/SUBSCRIPTION/STORAGE/DOM/NAVIGATION/TIMER/OTHER, activation INVOKED or REACTIVE,
target, reads/writes, dependencies, arguments, async_policy and cleanup. Only INVOKED effects are invoked by
handlers. REACTIVE effects run on mount/dependency changes and need no fake event; arguments fill their inputs.
Describe loading, success, failure, cancellation and stale results in spec. Each behavior belongs to its declared
component. Reads are data/property IDs; writes are mutable property IDs. Event payload and Effect result fields
are Data with direction OUTPUT. For list interactions put item/key in event payload. Use actual supplied API IDs.
REQUEST.target is a string copied EXACTLY from api_contracts.id, including the requirement prefix (for example,
REQ-1.1::API.RegisterTravelerAccount). Never shorten it to API.RegisterTravelerAccount or use an API name.
If no supplied API fits, omit that REQUEST and describe the missing integration in the Handler spec;
do not fabricate an API target or use a different Effect kind to disguise an unbound request.
Behavior Data must explicitly include type, direction, required and owner_id. You may add missing status UI/state.
Complete this requirement's child input arguments and connect custom events to parent handlers in this response.
Create child CUSTOM events and parent callbacks/handlers together when this requirement needs them; forward
references across components are supported in the same response. No separate per-component wiring pass follows.
"""


def protocol_examples(component_id: str, root_ui_id: str) -> dict[str, Any]:
    """One small complete patch contrasts component input, local state and rendered content."""
    return {
        "creates": {
            "data": [{"key": "initialQuery", "name": "initialQuery",
                      "spec": "Optional initial query supplied to this component.", "owner_id": {"id": component_id},
                      "direction": "INPUT", "type": {"kind": "string"}, "required": False,
                      "default": {"kind": "LITERAL", "value": ""}}],
            "properties": [{"key": "query", "component_id": None, "name": "query",
                            "spec": "Editable query owned by this component.", "kind": "STATE",
                            "type": {"kind": "string"},
                            "initial": {"kind": "REF", "ref_id": {"local": "initialQuery"}, "path": []}, "derive": None}],
            "ui": [{"key": "queryText", "component_id": None, "name": "Query text",
                    "spec": "Display the query.", "kind": "TEXT", "condition": None,
                    "repeat": None, "presentation": "",
                    "element": None, "component_ref": None, "slot_data_id": None,
                    "attributes": [], "children": [], "arguments": [], "callbacks": [],
                    "text": {"kind": "REF", "ref_id": {"local": "query"}, "path": []}}],
        },
        "updates": [{"table": "ui", "record": {
            "id": root_ui_id, "component_id": {"id": component_id}, "name": "Root", "spec": "Component root.",
            "kind": "FRAGMENT", "element": None, "component_ref": None, "slot_data_id": None,
            "attributes": [], "text": None, "children": [{"local": "queryText"}], "condition": None,
            "repeat": None, "arguments": [], "callbacks": [], "presentation": "",
        }}],
    }


class ShapeError(ValueError):
    def __init__(self, path: str, message: str, actual: Any) -> None:
        self.path = path
        self.detail = message
        self.actual = actual
        super().__init__(f"{path}: {message}")

    def feedback(self) -> dict[str, Any]:
        hint = "Use the original input and supplied schema to return a complete batch addressing this error."
        if ".data[" in self.path:
            hint += " Data is a boundary contract: no kind/initial/derive. Internal STATE/DERIVED/REF belongs in properties."
        elif ".properties[" in self.path:
            hint += " Property ownership uses component_id, not owner_id. Include initial and derive; the inactive one is null."
        return {"path": self.path, "error": self.detail,
                "actual": json.dumps(self.actual, ensure_ascii=False, default=str)[:500], "hint": hint}


class BatchValidationError(ShapeError):
    """Independent diagnostics collected before spending the single repair attempt."""
    def __init__(self, errors: list[ShapeError]) -> None:
        self.errors = errors
        super().__init__(errors[0].path,
                         f"{len(errors)} validation errors: " + "; ".join(str(e) for e in errors),
                         errors[0].actual)

    def feedback(self) -> dict[str, Any]:
        return {"error": f"{len(self.errors)} validation errors", "errors": [e.feedback() for e in self.errors],
                  "hint": "Return a complete replacement batch addressing all listed errors, using the original input."}


def collect_shape_errors(value: Any, schema: dict[str, Any], defs: dict[str, Any] | None = None,
                         path: str = "$", depth: int = 0) -> list[ShapeError]:
    """Collect sibling field/record errors; choose discriminated union branches deterministically."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if depth > 80:
        return [ShapeError(path, "excessive nesting", value)]
    if "$ref" in schema:
        return collect_shape_errors(value, defs[schema["$ref"].split("/")[-1]], defs, path, depth + 1)
    if "anyOf" in schema:
        options = schema["anyOf"]
        if isinstance(value, dict):
            for tag in ("table", "kind", "field"):
                matches = [s for s in options if value.get(tag) in s.get("properties", {}).get(tag, {}).get("enum", [])]
                if matches:
                    options = matches
                    break
        candidates = [collect_shape_errors(value, s, defs, path, depth + 1)
                      for s in options if _schema_type_matches(value, s)]
        if candidates:
            return min(candidates, key=len)
    if schema.get("type") == "object" and isinstance(value, dict):
        errors = []
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - schema["properties"].keys() if schema.get("additionalProperties") is False else set()
        if missing or extra:
            errors.append(ShapeError(path, f"missing {sorted(missing)}, unknown {sorted(extra)}", value))
        for key, child in value.items():
            if key in extra:
                continue
            child_schema = schema["properties"].get(key, schema.get("additionalProperties", {}))
            errors.extend(collect_shape_errors(child, child_schema, defs, f"{path}.{key}", depth + 1))
        return errors
    if schema.get("type") == "array" and isinstance(value, list):
        return [error for i, child in enumerate(value)
                for error in collect_shape_errors(child, schema["items"], defs, f"{path}[{i}]", depth + 1)]
    try:
        check_shape(value, schema, defs, path, depth)
        return []
    except ShapeError as exc:
        return [exc]


def _schema_type_matches(value: Any, schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    return not kind or {"object": isinstance(value, dict), "array": isinstance(value, list),
                       "string": isinstance(value, str), "boolean": isinstance(value, bool),
                       "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                       "integer": isinstance(value, int) and not isinstance(value, bool),
                       "null": value is None}.get(kind, False)


def check_shape(value: Any, schema: dict[str, Any], defs: dict[str, Any] | None = None,
                path: str = "$", depth: int = 0) -> None:
    """Validate the small schema vocabulary used above, without a semantic validator dependency."""
    if depth > 80:
        raise ShapeError(path, "excessive nesting", value)
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return check_shape(value, defs[schema["$ref"].split("/")[-1]], defs, path, depth + 1)
    if "anyOf" in schema:
        options = schema["anyOf"]
        # Select a discriminated branch first so its real error is not swallowed by anyOf.
        if isinstance(value, dict):
            for discriminator in ("table", "kind", "field"):
                tagged = [o for o in options if discriminator in o.get("properties", {})
                          and "enum" in o["properties"][discriminator]]
                if tagged and discriminator in value:
                    matches = [o for o in tagged if value[discriminator] in o["properties"][discriminator]["enum"]]
                    if not matches:
                        allowed = sorted({v for o in tagged for v in o["properties"][discriminator]["enum"]})
                        raise ShapeError(f"{path}.{discriminator}", f"expected one of {allowed}", value[discriminator])
                    options = matches
                    break
        options = [o for o in options if _schema_type_matches(value, o)]
        errors = []
        for option in options:
            try:
                check_shape(value, option, defs, path, depth + 1)
                return
            except ShapeError as exc:
                errors.append(exc)
        if errors:
            raise max(errors, key=lambda exc: len(exc.path))
        raise ShapeError(path, "expected " + " or ".join(o.get("type", "object") for o in schema["anyOf"]), value)
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "null": value is None}
    if kind and not valid.get(kind, False):
        raise ShapeError(path, f"expected {kind}", value)
    if "enum" in schema and value not in schema["enum"]:
        raise ShapeError(path, f"expected one of {schema['enum']}", value)
    if kind == "object":
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - schema["properties"].keys() if schema.get("additionalProperties") is False else set()
        if missing or extra:
            raise ShapeError(path, f"missing {sorted(missing)}, unknown {sorted(extra)}; "
                             f"allowed fields: {list(schema['properties'])}", value)
        for key, item in value.items():
            child_schema = schema["properties"].get(key, schema.get("additionalProperties", {}))
            check_shape(item, child_schema, defs, f"{path}.{key}", depth + 1)
    elif kind == "array":
        for index, item in enumerate(value):
            check_shape(item, schema["items"], defs, f"{path}[{index}]", depth + 1)
