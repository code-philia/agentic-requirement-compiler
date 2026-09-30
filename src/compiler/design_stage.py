"""Design IR compiler with top-down module decomposition."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from core.logging import SynchronousLog

from .model_client import StructuredModel, describe_model_error
from .trace_payload import format_payload_trace


PRIMITIVE_TYPES = {"string", "integer", "number", "boolean", "date", "datetime", "uuid", "json"}
DATABASE_EFFECT_OPERATIONS = {"READ", "CREATE", "UPDATE", "DELETE"}
EFFECT_OPERATIONS = DATABASE_EFFECT_OPERATIONS | {"SESSION_WRITE", "COOKIE_WRITE", "EXTERNAL_IO"}
OBLIGATION_KINDS = {
    "VALIDATION", "AUTHORIZATION", "COMPUTATION", "STATE_TRANSITION",
    "PERSISTENCE", "TRANSACTION", "IDEMPOTENCY", "EXTERNAL_INTERACTION",
    "ERROR_MAPPING",
}
SELF_OBLIGATION_OWNER_KINDS = frozenset({"API", "FUNC", "DB"})
API_REUSE_DECISIONS = {"REUSE", "EXTEND", "CREATE"}
UI_VOCABULARY = frozenset({
    "banner", "breadcrumb", "button", "buttons", "checkbox", "click", "clicked", "css",
    "dialog", "div", "dropdown", "frontend", "highlight", "hover", "keypress", "modal",
    "mouseover", "navbar", "onclick", "pixel", "placeholder", "popup", "render",
    "rendered", "scroll", "sidebar", "span", "spinner", "stylesheet", "textbox",
    "toast", "tooltip", "ui", "viewport", "widget",
})
REPAIR_CURRENT = "REPAIR_CURRENT"
REOPEN_PARENT = "REOPEN_PARENT"
UNRESOLVED = "UNRESOLVED"


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["semantic_id", "name", "type", "description", "required"],
    "properties": {
        "semantic_id": {"type": "string", "pattern": r"^[a-z][a-z0-9_.]*$"},
        "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
        "type": {"type": "string", "enum": sorted(PRIMITIVE_TYPES)},
        "description": {"type": "string"},
        "required": {"type": "boolean"},
    },
}

EFFECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "operation", "target", "fields"],
    "properties": {
        "id": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
        "operation": {"type": "string", "enum": sorted(EFFECT_OPERATIONS)},
        "target": _nullable({"type": "string"}),
        "fields": {"type": "array", "items": {"type": "string"}},
    },
}

OBLIGATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "description", "scenario_ids"],
    "properties": {
        "id": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
        "kind": {"type": "string", "enum": sorted(OBLIGATION_KINDS)},
        "description": {"type": "string"},
        "scenario_ids": {"type": "array", "items": {"type": "string"}},
    },
}

REQUIREMENT_CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirement_id", "spec", "server_state", "inputs", "outputs", "effects", "obligations"],
    "properties": {
        "requirement_id": {"type": "string"},
        "spec": {"type": "string"},
        "server_state": {"type": "boolean"},
        "inputs": {"type": "array", "items": FIELD_SCHEMA},
        "outputs": {"type": "array", "items": FIELD_SCHEMA},
        "effects": {"type": "array", "items": EFFECT_SCHEMA},
        "obligations": {"type": "array", "items": OBLIGATION_SCHEMA},
    },
}

MODULE_INTERFACE_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["semantic_id", "name", "type", "required"],
    "properties": {
        "semantic_id": {"type": "string", "pattern": r"^[a-z][a-z0-9_.]*$"},
        "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
        "type": {"type": "string", "enum": sorted(PRIMITIVE_TYPES)},
        "required": {"type": "boolean"},
    },
}

MODULE_EFFECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "operation", "target", "fields"],
    "properties": {
        "id": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"},
        "operation": {
            "type": "string",
            "enum": sorted(EFFECT_OPERATIONS),
        },
        "target": _nullable({"type": "string"}),
        "fields": {"type": "array", "items": {"type": "string"}},
    },
}

DECOMPOSED_MODULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "name", "spec", "inputs", "outputs", "effects", "obligation_ids"],
    "properties": {
        "kind": {"type": "string", "enum": ["FUNC", "DB"]},
        # Module names are semantic source names. Snake_case is valid and is
        # common in model output; the compiler will sanitize only characters
        # that cannot participate in a qualified module id.
        "name": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_]*$"},
        "spec": {"type": "string"},
        "inputs": {"type": "array", "items": MODULE_INTERFACE_FIELD_SCHEMA},
        "outputs": {"type": "array", "items": MODULE_INTERFACE_FIELD_SCHEMA},
        "effects": {"type": "array", "items": MODULE_EFFECT_SCHEMA},
        "obligation_ids": {"type": "array", "items": {"type": "string"}},
    },
}

MODULE_DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["self_obligation_ids", "modules"],
    "properties": {
        "self_obligation_ids": {"type": "array", "items": {"type": "string"}},
        "modules": {"type": "array", "minItems": 0, "items": DECOMPOSED_MODULE_SCHEMA},
    },
}

API_MODULE_SCHEMA = copy.deepcopy(DECOMPOSED_MODULE_SCHEMA)
API_MODULE_SCHEMA["properties"]["kind"]["enum"] = ["API"]
API_MODULE_SCHEMA["properties"]["reuse_decision"] = {
    "type": "string",
    "enum": sorted(API_REUSE_DECISIONS),
}
API_MODULE_SCHEMA["properties"]["reuse_target_module_id"] = _nullable(
    {"type": "string"}
)
API_MODULE_SCHEMA["required"] = [
    *API_MODULE_SCHEMA["required"],
    "reuse_decision",
    "reuse_target_module_id",
]

API_DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["self_obligation_ids", "modules"],
    "properties": {
        "self_obligation_ids": {"type": "array", "items": {"type": "string"}},
        "modules": {"type": "array", "minItems": 0, "items": API_MODULE_SCHEMA},
    },
}


REQUIREMENT_CONTRACT_INSTRUCTIONS = """You are a senior product-contract analyst and domain modeler.
Analyze one atomic requirement as a black box and fill the supplied fixed
template. Keep every required key and use [] when a section is empty. Describe only the requirement spec, external
inputs, observable outputs, required effects, and behavioral obligations. Set server_state to true only when satisfying
the requirement requires server-side state or server responsibilities (database access, runtime session/cookie state,
external interaction, authorization, persistence, transaction, idempotency, or another server-owned state transition).
Presentation, navigation, and client-local state are server_state=false and must have no server effects or server-owned
obligations. Every supplied scenario id must be covered
by at least one obligation. Obligations capture validation, authorization, computation, state transition, persistence,
transaction, idempotency, external interaction, or error mapping responsibilities; keep them implementation-neutral.
Whenever the contract contains a READ, CREATE, UPDATE, or DELETE effect, include at least one PERSISTENCE obligation
covering that database responsibility.
Represent database reads as READ effects and state changes as their
corresponding effect operation. Use only supplied database entities and fields. Constraints in the context must be
reflected in the spec when they affect behavior; do not return constraint ids or allocate constraints to modules.
Every database access required to satisfy observable behavior must appear as a READ, CREATE, UPDATE, or DELETE effect.
This includes persisted session records: when `session` is a supplied database entity, inserting a session row is
CREATE on `session`, not SESSION_WRITE. Reserve SESSION_WRITE for a non-database runtime session store. If the
requirement writes a browser cookie, include COOKIE_WRITE. In particular, checking whether values already exist for
UNIQUE or COMPOSITE_UNIQUE constraints requires a READ effect even when the database also enforces the constraint. Do
not hide required effects inside the prose spec.
Database access is implemented through one shared client injected into DB modules. Treat schema table symbols
as Drizzle table definitions, never as repositories or in-memory data containers; do not model table rows through
properties such as rows, data, or items.
Reuse the established vocabulary. The context lists every API contract already frozen by earlier requirements as
existing_api_contracts. When this requirement exposes or consumes a value that one of them already carries, copy that
semantic_id character for character instead of minting a synonym; a new semantic_id must describe genuinely new data.
When declaring EXTEND of an existing API, preserve every inherited input and output field from the target API exactly
(same semantic_id, type, and required flag). Inherited fields may be absent from this requirement's contract because
the contract describes only the new requirement delta; the compiler will carry them forward. New fields must still be
copied exactly from the supplied contract and must not be inferred from database columns, fixtures, or domain guesses.
Interface fields carry domain data only. Never put presentation vocabulary such as button, click, modal, dialog, toast,
placeholder, sidebar, spinner, or any other rendering concept into a semantic_id or a field name; describe what the
value is, not how a screen shows it. When the contract declares a database effect and returns anything, at least one
output semantic_id must be prefixed with the entity it belongs to, for example book.id rather than id or result.
Do not design modules, calls, steps, bindings, outcomes, guards, or algorithms. Keep every id concise (64 characters
or fewer). Return only the structured object.

Example shape:
{"requirement_id":"REQ-1.1","spec":"Register one traveler.","server_state":true,"inputs":[{"semantic_id":"registration.username","name":"username","type":"string","description":"Requested username.","required":true}],"outputs":[{"semantic_id":"traveler.id","name":"traveler_id","type":"uuid","description":"Created traveler id.","required":true}],"effects":[{"id":"create_traveler","operation":"CREATE","target":"traveler","fields":["username"]}],"obligations":[{"id":"validate_registration","kind":"VALIDATION","description":"Reject invalid registration data.","scenario_ids":["REQ-1.1:scenario:1"]}]}
"""

API_DECOMPOSITION_INSTRUCTIONS = """You are a senior backend API architect.
Turn one Requirement Contract into API modules. Keep one user action in one API
unless the requirement explicitly defines multiple operations. Every API contains exactly kind, name, spec, inputs,
outputs, effects, and obligation_ids. Set kind to API. Copy interface fields and effects from the supplied contract without changing
their semantic identifiers, types, or required flags. Field names are local parameter labels and may be made clearer
without changing the represented data. A module whose spec describes a database read or write must own the
corresponding effect from the supplied contract; never describe hidden database access on a module with an empty
effects list. Allocate every supplied obligation id to exactly one API. Treat negative or preventive obligations
(for example prevent_duplicate_write, prevent_book_persistence, or no_session_on_failure) as real obligations;
they must not be omitted because they describe something that must not happen. Before returning, compare the
complete parent obligation id set with the union of API obligation_ids exactly.
For REUSE, copy the target API interface exactly. For EXTEND, copy every target input/output exactly and add only
the genuinely new fields from the fixed requirement contract; inherited fields such as note.content or note.pinned
are valid even when they are not repeated in the current contract. Never invent fields from database columns, UI
controls, fixtures, or prose assumptions.
Do not design child functions or implementation steps. Return only `{\"modules\": [...]}`.
"""

MODULE_DECOMPOSITION_INSTRUCTIONS = """You are a senior backend decomposition architect.
Read the layered Markdown context and decompose the current module from the top down.
Silently plan how the parent responsibility is completed, then return only its direct child modules in execution order.

Every child contains exactly seven top-level fields: kind, name, spec, inputs, outputs, effects, and obligation_ids. Each interface field
contains semantic_id, name, type, and required. Preserve required exactly when reusing a parent field; mark newly
introduced values required only when the child cannot complete without them. An API may call FUNC only. A
FUNC may contain its own logic and may call FUNC or DB modules. The compiler-provided schema is authoritative: below an
API, at most two FUNC levels may be materialized; when the current FUNC is already at level two, only DB children (or no
children) are legal. A DB module is always a leaf. The list order is the call
order. A module input must come from the parent inputs or an earlier child output. Copy the exact interface field and
effect identifiers supplied in the Markdown. Keep child responsibilities cohesive and smaller than the parent. Return
an empty modules list when a FUNC can complete its remaining pure logic itself. Database effects are exactly READ,
CREATE, UPDATE, and DELETE; they must be delegated to DB modules and may not remain inside a terminal FUNC.
SESSION_WRITE, COOKIE_WRITE, and EXTERNAL_IO are application effects and must never be assigned to a DB module. Every
DB child must own at least one database effect. Never invent an effect: when the parent effect table is empty, all
child effects must be [] and no DB child may be introduced. A child whose spec describes database access must own one
of the exact effects listed in the parent table.
Copy obligation ids exactly. An API must delegate every obligation to one direct FUNC child. A FUNC may implement
non-persistence obligations itself, but every PERSISTENCE obligation must eventually be delegated to a DB leaf.
DB children may own only PERSISTENCE obligations. Never invent, rename, omit, or duplicate an obligation id.
Before returning, perform an obligation ledger check: the union of direct-child obligation_ids must equal the
parent's required obligation ids exactly. Negative obligations such as prevent/no-write/no-create are still real
behavioral obligations and must be allocated. For every PERSISTENCE obligation, ensure at least one child path can
continue to a DB leaf that owns the corresponding persistence responsibility.

The compiler supplies every DB leaf with one shared Drizzle `database` client and its relevant schema table imports.
The table symbols describe SQL tables; they are not data-bearing objects and must never be described as exposing
rows, data, items, or other in-memory collections. A DB leaf performs its declared effect through that client.

Treat semantic_id as the stable identity of a data value and type as its canonical data type. The name is only a local
parameter label and may differ between modules. A child output may introduce new data or pass through/refine data that
is already available, such as returning account.id after verifying its password. When reusing a semantic_id, preserve
its type exactly. Within one module interface, different semantic_id values must use different name values; for example,
call a stored account password `stored_password` when the supplied login password is already named `password`. Do not
invent a new semantic_id merely to represent that an existing value passed validation.

Do not output any fields beyond the seven listed above. The compiler derives symbol IDs and graph relationships. Return
only the structured `{\"modules\": [...]}` object.
"""

# Keep the ownership rule explicit and symmetric for API/FUNC decisions.  These
# assignments intentionally follow the long prompt constants above so the
# provider receives the corrected Plan-B contract without changing call sites.
API_DECOMPOSITION_INSTRUCTIONS = API_DECOMPOSITION_INSTRUCTIONS.replace(
    "Allocate every supplied obligation id to exactly one API.",
    "The top-level self_obligation_ids array declares obligations implemented by the current API itself. "
    "Every parent obligation must appear exactly once in either self_obligation_ids or the union of API obligation_ids.",
).replace(
    "they must not be omitted because they describe something that must not happen. Before returning, compare the\ncomplete parent obligation id set with the union of API obligation_ids exactly.",
    "they must not be omitted because they describe something that must not happen. Use self_obligation_ids for "
    "API-level validation, routing, error mapping, or prevention that needs no child. Do not put a PERSISTENCE "
    "obligation there unless this API is the actual persistence owner. Before returning, compare the complete parent "
    "obligation id set with the union of self_obligation_ids and API obligation_ids exactly.\n\n"
    "Example: for parent obligations [\"validate_input\", \"prevent_book_persistence\"], a valid result is "
    "{\"self_obligation_ids\":[\"validate_input\"],\"modules\":[{\"kind\":\"API\",\"name\":\"cancel\",\"spec\":\"Handle cancellation without persisting the book.\",\"inputs\":[],\"outputs\":[],\"effects\":[],\"obligation_ids\":[\"prevent_book_persistence\"]}]}.",
).replace(
    "Return only `{\"modules\": [...]}`.",
    "Return only `{\"self_obligation_ids\": [...], \"modules\": [...]}`.",
)
API_DECOMPOSITION_INSTRUCTIONS += """
Reuse before create. The context lists every API contract frozen by earlier requirements as existing_api_contracts,
each with its module id, spec, effect signatures, and input/output semantic ids. Judge every API you return against
that list and report the judgement on the module itself:
- REUSE: an existing API already performs this operation. Set reuse_target_module_id to its module id and copy its
  input semantic ids, output semantic ids, and effects exactly. Prefer REUSE whenever the frozen contract already
  carries the data this requirement needs.
- EXTEND: the existing operation is the right one but this requirement genuinely adds data or a distinct behavioral
  obligation. Set reuse_target_module_id to its module id, keep every semantic id it already declares, and add only
  fields with required=false. An EXTEND may add no public field when the new requirement specializes behavior while
  preserving the same interface; this is valid and must not be rejected as a schema error. Never drop a field, never
  change a type, and never add a required field.
- CREATE: no existing API performs this operation. Set reuse_target_module_id to null. Do not choose CREATE for an
  operation whose inputs, outputs, and effects already match a frozen contract.
Reuse is decided by semantic identifiers and effects, not by wording: two APIs that read the same entity fields are
the same operation even when their specs are phrased differently.
Interface fields carry domain data only. Never introduce presentation vocabulary such as button, click, modal, dialog,
toast, placeholder, sidebar, or any other rendering concept into a semantic_id or a field name.
"""

MODULE_DECOMPOSITION_INSTRUCTIONS = MODULE_DECOMPOSITION_INSTRUCTIONS.replace(
    "Every child contains exactly seven top-level fields: kind, name, spec, inputs, outputs, effects, and obligation_ids.",
    "Every child contains exactly seven top-level fields: kind, name, spec, inputs, outputs, effects, and obligation_ids. "
    "The top-level self_obligation_ids array declares obligations implemented by the current parent itself; it is not a child allocation.",
).replace(
    "Copy obligation ids exactly. An API must delegate every obligation to one direct FUNC child. A FUNC may implement\nnon-persistence obligations itself, but every PERSISTENCE obligation must eventually be delegated to a DB leaf.",
    "Copy obligation ids exactly. Every parent obligation must appear exactly once in either self_obligation_ids or the "
    "union of direct-child obligation_ids. Use self_obligation_ids when the current parent implements the obligation "
    "without a child. An API or FUNC must not silently drop a negative or preventive obligation. Every PERSISTENCE "
    "obligation not listed in self_obligation_ids must continue to a DB leaf; self-own it only when the current module "
    "is the actual persistence owner.",
).replace(
    "Before returning, perform an obligation ledger check: the union of direct-child obligation_ids must equal the\nparent's required obligation ids exactly.",
    "Before returning, perform an obligation ledger check: the union of self_obligation_ids and direct-child obligation_ids "
    "must equal the parent's required obligation ids exactly. Example: {\"self_obligation_ids\":[\"prevent_book_persistence\"],\"modules\":[]} "
    "is valid when this FUNC itself is the guard owner; leaving both arrays empty is invalid.",
).replace(
    "Do not output any fields beyond the seven listed above. The compiler derives symbol IDs and graph relationships. Return\nonly the structured `{\"modules\": [...]}` object.",
    "Do not output fields beyond the seven child fields and top-level self_obligation_ids. The compiler derives symbol IDs "
    "and graph relationships. Return only `{\"self_obligation_ids\": [...], \"modules\": [...]}`.",
)

_SELF_OWNERSHIP_RULE = (
    "Authoritative ownership rule: only module kinds "
    + ", ".join(sorted(SELF_OBLIGATION_OWNER_KINDS))
    + " may use self_obligation_ids. The validator reads this same compiler constant."
)
API_DECOMPOSITION_INSTRUCTIONS = (
    API_DECOMPOSITION_INSTRUCTIONS.rstrip() + "\n\n" + _SELF_OWNERSHIP_RULE + "\n"
)
API_DECOMPOSITION_INSTRUCTIONS += """
Effect allocation rule: a READ effect may be copied to more than one independent API
when each API genuinely needs the same lookup. CREATE, UPDATE, DELETE, SESSION_WRITE,
COOKIE_WRITE, and EXTERNAL_IO effects are single-owner and must appear in exactly one API.
Never duplicate a mutating effect merely to satisfy coverage.
"""
MODULE_DECOMPOSITION_INSTRUCTIONS = (
    MODULE_DECOMPOSITION_INSTRUCTIONS.rstrip() + "\n\n" + _SELF_OWNERSHIP_RULE + "\n"
)

REQUIREMENT_CONTRACT_INSTRUCTIONS += """
Completeness checklist: inspect every scenario's setup, action, successful outcome,
boundary/invalid input, authorization, empty result and prohibited side effects.
Include supporting reads needed for real user journeys (available choices/dates,
detail lookup, current session) when required by the requirement; reuse existing
operations rather than inventing test-only APIs or fixture/seed endpoints. Include
all data needed by the frontend to display the required result. Do not omit a
responsibility merely because it occurs before the main action. Do not add generic
CRUD operations or business rules unsupported by this requirement.
"""
API_DECOMPOSITION_INSTRUCTIONS += """
Cover every required server operation, including prerequisite reads and observable
results; one user action may require multiple distinct operations. Reuse an existing
operation when its behavior and contract match, not merely because it reads the same
table. Do not combine unrelated operations just to minimize the API count.
"""
API_DECOMPOSITION_INSTRUCTIONS = API_DECOMPOSITION_INSTRUCTIONS.replace(
    "two APIs that read the same entity fields are\nthe same operation even when their specs are phrased differently.",
    "matching entity fields alone do not establish equivalent behavior or authorization.",
)


@dataclass(slots=True)
class DesignIssue:
    code: str
    message: str
    owner_phase: str
    blame_symbol: str
    repair_action: str = REPAIR_CURRENT
    severity: str = "ERROR"
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "owner_phase": self.owner_phase,
            "blame_symbol": self.blame_symbol,
            "repair_action": self.repair_action,
            "message": self.message,
            "context": copy.deepcopy(self.context),
        }

    def feedback(self) -> str:
        if self.code == "OBLIGATION_ALLOCATION_MISMATCH":
            return (
                f"{self.code}: {self.message} Repair each missing obligation id explicitly: "
                "put the exact id in either top-level self_obligation_ids (the current parent owns it) "
                "or exactly one direct child's obligation_ids. Do not rename, duplicate, or leave it unowned."
            )
        guidance = {
            "FLOW_SOURCE_MISSING": "Repair: every child input must reuse a parent input or an output of an earlier child; do not invent a new source value.",
            "PARENT_OUTPUT_UNREALIZED": "Repair: ensure the child sequence produces every required parent output, preserving the exact semantic_id and type.",
            "EFFECT_CONTRACT_MISMATCH": "Repair: copy the parent effect id, operation, target, and fields exactly; do not paraphrase or alter it.",
            "DB_EFFECT_REQUIRED": "Repair: a DB child must own at least one exact READ/CREATE/UPDATE/DELETE effect from the parent.",
            "DB_OBLIGATION_INVALID": "Repair: DB children may own only PERSISTENCE obligations; move validation/error/control obligations to FUNC children.",
            "PERSISTENCE_OBLIGATION_DB_MISSING": "Repair: add a DB implementation owner on the obligation's child path; negative persistence rules still require a DB-backed responsibility.",
            "OBLIGATION_IMPLEMENTATION_MISSING": (
                "Repair: assign this exact obligation id through self_obligation_ids on one of the "
                f"allowed owner kinds ({', '.join(sorted(SELF_OBLIGATION_OWNER_KINDS))}), or to a terminal FUNC/DB owner."
            ),
            "API_FIELD_OUT_OF_CONTRACT": (
                "Repair: for REUSE/EXTEND, inherited fields from reuse_target_module_id are allowed only when "
                "copied exactly; any genuinely new field must appear in the fixed requirement contract. Do not "
                "infer fields from database columns, fixtures, or UI controls."
            ),
        }.get(self.code)
        return f"{self.code}: {self.message}" + (f" {guidance}" if guidance else "")


@dataclass(slots=True)
class DesignPassResult:
    design_ir: dict[str, Any]
    node_states: dict[str, str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class DecisionResult:
    value: dict[str, Any] | None
    issues: list[DesignIssue]


class DesignState:
    """Transactional symbol registry and authoritative design graph state."""

    def __init__(self) -> None:
        self.requirement_contracts: dict[str, dict[str, Any]] = {}
        self.modules: dict[str, dict[str, Any]] = {}
        self.invocations: list[dict[str, Any]] = []
        self.requirement_modules: dict[str, set[str]] = {}

    def clone(self) -> "DesignState":
        return copy.deepcopy(self)

    def to_ir(self) -> dict[str, Any]:
        callers_by_module: dict[str, set[str]] = {}
        callees_by_module: dict[str, list[str]] = {}
        for invocation in self.invocations:
            caller_id = str(invocation["caller"])
            callee_id = str(invocation["callee"])
            callers_by_module.setdefault(callee_id, set()).add(caller_id)
            callees = callees_by_module.setdefault(caller_id, [])
            if callee_id not in callees:
                callees.append(callee_id)
        modules: list[dict[str, Any]] = []
        for module_id in sorted(self.modules):
            module = copy.deepcopy(self.modules[module_id])
            module["callers"] = sorted(callers_by_module.get(module_id, set()))
            module["callees"] = copy.deepcopy(callees_by_module.get(module_id, []))
            modules.append(module)
        return {
            "requirements": [{
                "id": requirement_id,
                "contract": copy.deepcopy(self.requirement_contracts[requirement_id]),
                "api_ids": sorted(
                    module_id for module_id in self.requirement_modules.get(requirement_id, set())
                    if self.modules[module_id]["kind"] == "API"
                ),
            } for requirement_id in sorted(self.requirement_contracts)],
            "modules": modules,
        }


class DesignPass:
    """Compile Design IR through top-down module decomposition."""

    def __init__(self, model: StructuredModel, artifact_root: Path) -> None:
        self._model = model
        arc_root = artifact_root.expanduser().resolve()
        self._log = SynchronousLog("DesignPass", workspace_root=arc_root.parent)
        self._local_retries = _env_int("ARC_STRUCTURED_OUTPUT_RETRY_COUNT", 2, 0, 10)
        self._reopen_budget = _env_int("ARC_DESIGN_REOPEN_COUNT", 2, 0, 6)
        self._trace_enabled = _env_flag("ARC_DESIGN_TRACE", True)
        self._soft_warnings: list[str] = []

    def compile(
        self,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any],
        database_schema: dict[str, Any],
    ) -> DesignPassResult:
        nodes = requirement_ir.get("nodes", {})
        dependencies = dependency_graph.get("atomic_dependencies", {})
        order = [node for wave in _waves(requirement_ir, dependency_graph) for node in wave]
        state = DesignState()
        all_issues: list[DesignIssue] = []
        warnings: list[str] = []
        self._soft_warnings = []
        states: dict[str, str] = {}

        for requirement_id in order:
            requirement = _requirement_context(nodes, requirement_id)
            design_context = project_design_context(database_schema, requirement_id, dependencies)
            model_design_context = _model_design_context(design_context)
            frozen_api_contracts = _existing_api_contracts(state)
            contract_result = self._decision(
                phase="requirement_contract",
                unit_id=requirement_id,
                schema_name="arc_requirement_contract",
                instructions=REQUIREMENT_CONTRACT_INSTRUCTIONS,
                output_schema=REQUIREMENT_CONTRACT_SCHEMA,
                context={
                    "requirement": requirement,
                    "design_context": model_design_context,
                    "existing_api_contracts": frozen_api_contracts,
                    "fixed_template": _requirement_contract_template(requirement_id),
                },
                validator=lambda value: _requirement_contract_issues(
                    value, requirement_id, design_context, requirement
                ),
            )
            if contract_result.value is None:
                states[requirement_id] = "FAILED"
                all_issues.extend(contract_result.issues)
                break

            base = state.clone()
            contract = _normalize_requirement_contract(contract_result.value)
            contract, normalized_effect_ids = _normalize_database_backed_session_effects(
                contract,
                design_context=design_context,
            )
            if normalized_effect_ids:
                self._trace(
                    "CONTRACT_EFFECTS_NORMALIZED "
                    f"requirement={requirement_id} effects={normalized_effect_ids} "
                    "from=SESSION_WRITE to=CREATE source=database_entities"
                )
            contract, completed_effect_ids = _complete_constraint_read_effects(
                contract,
                requirement_id=requirement_id,
                design_context=design_context,
            )
            if completed_effect_ids:
                self._trace(
                    "CONTRACT_EFFECTS_COMPLETED "
                    f"requirement={requirement_id} effects={completed_effect_ids} "
                    "source=database_unique_constraints"
                )
            base.requirement_contracts[requirement_id] = contract
            if not contract.get("server_state", True):
                compiled, issues = base, []
            else:
                compiled, issues = self._compile_requirement(
                    base,
                    requirement_id,
                    requirement,
                    contract,
                    design_context,
                )
            if compiled is None:
                states[requirement_id] = "FAILED"
                all_issues.extend(issues)
                break
            state = compiled
            collapsed, collapse_warnings = _collapse_passthrough_modules(state, requirement_id)
            state = collapsed
            warnings.extend(collapse_warnings)
            states[requirement_id] = "DESIGN_VALIDATED"

        warnings.extend(self._soft_warnings)
        final_issues = all_issues
        design = state.to_ir()
        if not final_issues:
            final_issues = _behavioral_completeness_issues(design)
            if final_issues:
                warnings.extend(
                    "DESIGN_VALIDATION_WARNING: " + issue.message
                    for issue in final_issues
                )
                # A structurally materialized design is still emitted.  The
                # warnings make the soft-fail explicit without aborting later
                # lowering/TDD stages.
                final_issues = []
        errors = [f"{issue.code}: {issue.message}" for issue in final_issues]
        return DesignPassResult(design, states, errors, warnings)


    def _compile_requirement(
        self,
        base: DesignState,
        requirement_id: str,
        requirement: dict[str, Any],
        contract: dict[str, Any],
        design_context: dict[str, Any],
    ) -> tuple[DesignState | None, list[DesignIssue]]:
        upstream_feedback: list[str] = []
        seen_failures: set[str] = set()
        last_issues: list[DesignIssue] = []
        last_trial: DesignState | None = None
        existing_apis = _existing_api_contracts(base)
        for reopen_attempt in range(self._reopen_budget + 1):
            context = {
                "requirement": requirement,
                "fixed_requirement_contract": contract,
                "existing_api_contracts": existing_apis,
                "fixed_template": _api_template(),
            }
            if upstream_feedback:
                context["upstream_validation_feedback"] = upstream_feedback
            result = self._decision(
                phase="requirement_api",
                unit_id=requirement_id,
                schema_name="arc_requirement_api",
                instructions=API_DECOMPOSITION_INSTRUCTIONS,
                output_schema=_allocation_output_schema(
                    API_DECOMPOSITION_SCHEMA,
                    contract.get("obligations", []),
                ),
                context=context,
                validator=lambda value: _api_plan_issues(
                    value, requirement_id, contract, existing_apis
                ),
            )
            if result.value is None:
                return None, result.issues
            for decided in result.value.get("modules", []):
                self._trace(
                    "API_REUSE_DECISION "
                    f"requirement={requirement_id} api={decided.get('name', '')} "
                    f"decision={decided.get('reuse_decision', '')} "
                    f"target={decided.get('reuse_target_module_id') or '-'}"
                )
            trial = base.clone()
            last_trial = trial
            api_ids, issues = _materialize_apis(trial, requirement_id, contract, result.value)
            if issues:
                return None, issues
            failure: list[DesignIssue] = []
            for api_id in api_ids:
                expanded, child_issues = self._expand_module(
                    trial,
                    requirement_id,
                    requirement,
                    api_id,
                    contract,
                    design_context,
                    depth=0,
                )
                if expanded is None:
                    failure = child_issues
                    break
                trial = expanded
            if not failure:
                completeness = _behavioral_completeness_issues(
                    trial.to_ir(),
                    requirement_ids={requirement_id},
                )
                if not completeness:
                    return trial, []
                for issue in completeness:
                    issue.repair_action = REOPEN_PARENT
                failure = completeness
            last_issues = failure
            if not any(issue.repair_action == REOPEN_PARENT for issue in failure):
                return None, failure
            fingerprint = _hash({
                "decision": result.value,
                "issues": [issue.as_dict() for issue in failure],
            })
            if fingerprint in seen_failures:
                self._soft_warnings.append(
                    f"DESIGN_VALIDATION_WARNING requirement={requirement_id}: "
                    "repeated upstream conflicts; keeping the latest materialized design."
                )
                return last_trial or base, []
            seen_failures.add(fingerprint)
            upstream_feedback = [issue.feedback() for issue in failure]
            self._trace(f"UPSTREAM_REOPEN phase=requirement_api requirement={requirement_id} attempt={reopen_attempt + 1}/{self._reopen_budget + 1} feedback={'; '.join(upstream_feedback)}")
        self._soft_warnings.append(
            f"DESIGN_VALIDATION_WARNING requirement={requirement_id}: "
            f"upstream repair budget exhausted; keeping the latest materialized design. "
            f"issues={'; '.join(issue.message for issue in last_issues)}"
        )
        return last_trial or base, []

    def _decision(
        self,
        *,
        phase: str,
        unit_id: str,
        schema_name: str,
        instructions: str,
        output_schema: dict[str, Any],
        context: dict[str, Any],
        validator: Callable[[dict[str, Any]], list[DesignIssue]],
    ) -> DecisionResult:
        feedback: list[str] = []
        last_issues: list[DesignIssue] = []
        last_decision: dict[str, Any] | None = None
        for attempt in range(self._local_retries + 1):
            payload = copy.deepcopy(context)
            if feedback:
                if phase == "module_decomposition" and isinstance(payload.get("context_markdown"), str):
                    payload["context_markdown"] += _feedback_markdown(feedback)
                else:
                    payload["validation_feedback"] = feedback
            self._trace(f"MODEL_REQUEST phase={phase} unit={unit_id} attempt={attempt + 1}/{self._local_retries + 1}")
            provider_schema = _provider_output_schema(output_schema)
            request_payload = {
                "instructions": instructions,
                "input_payload": payload,
                "output_schema": provider_schema,
                "local_validation_schema": output_schema,
                "schema_name": schema_name,
            }
            self._trace(
                _context_audit(
                    phase=phase,
                    unit_id=unit_id,
                    attempt=attempt + 1,
                    request_payload=request_payload,
                )
            )
            self._trace_json("MODEL_INPUT", phase, unit_id, request_payload)
            started = time.perf_counter()
            try:
                decision = self._model.generate_json(
                    schema_name=schema_name,
                    instructions=instructions,
                    input_payload=payload,
                    output_schema=provider_schema,
                )
            except Exception as exc:
                issue = DesignIssue("MODEL_CALL_FAILED", describe_model_error(exc), phase.upper(), unit_id)
                last_issues = [issue]
                error_text = str(exc).lower()
                feedback = [
                    "The previous response was empty or not valid JSON. Return exactly one non-empty JSON object, with no Markdown, prose, or code fence."
                    if "empty" in error_text or "jsondecodeerror" in error_text
                    else issue.feedback()
                ]
                self._trace(f"MODEL_ERROR phase={phase} unit={unit_id} errors={issue.feedback()}")
                continue
            duration = int((time.perf_counter() - started) * 1000)
            self._trace_json("MODEL_OUTPUT", phase, unit_id, decision, duration)
            if isinstance(decision, dict):
                last_decision = copy.deepcopy(decision)
            issues = _shape_issues(decision, output_schema, phase, unit_id)
            if not issues:
                issues = validator(decision)
            if not issues:
                self._trace(f"MODEL_ACCEPTED phase={phase} unit={unit_id} attempt={attempt + 1} duration_ms={duration}")
                return DecisionResult(decision, [])
            last_issues = issues
            feedback = [issue.feedback() for issue in issues]
            self._trace(f"MODEL_REJECTED phase={phase} unit={unit_id} errors={'; '.join(feedback)}")
        if last_decision is not None:
            warning = (
                f"DESIGN_VALIDATION_WARNING phase={phase} unit={unit_id}: "
                f"accepted the last structured model result after {self._local_retries + 1} attempts; "
                f"remaining issues={'; '.join(issue.message for issue in last_issues)}"
            )
            self._soft_warnings.append(warning)
            self._trace(warning)
            return DecisionResult(last_decision, [])
        return DecisionResult(None, last_issues)

    def _trace(self, message: str) -> None:
        if self._trace_enabled:
            self._log.info(message)

    def _trace_json(self, marker: str, phase: str, unit_id: str, payload: Any, duration: int | None = None) -> None:
        suffix = f" duration_ms={duration}" if duration is not None else ""
        self._trace(f"{marker} phase={phase} unit={unit_id}{suffix}\n{format_payload_trace(payload)}")

    def _expand_module(
        self,
        state: DesignState,
        requirement_id: str,
        requirement: dict[str, Any],
        module_id: str,
        requirement_contract: dict[str, Any],
        design_context: dict[str, Any],
        *,
        depth: int,
    ) -> tuple[DesignState | None, list[DesignIssue]]:
        module = state.modules[module_id]
        if module["kind"] == "DB":
            return state, []

        child_feedback: list[str] = []
        seen_failures: set[str] = set()
        last_issues: list[DesignIssue] = []
        last_trial: DesignState | None = None
        for reopen_attempt in range(self._reopen_budget + 1):
            decomposition_schema = _module_decomposition_output_schema(module, depth=depth)
            context_markdown = _module_decomposition_markdown(
                requirement,
                design_context,
                state,
                module_id,
                decomposition_schema,
            )
            if child_feedback:
                context_markdown += _feedback_markdown(child_feedback, heading="上层重新拆解反馈")
            context = {"context_markdown": context_markdown}
            result = self._decision(
                phase="module_decomposition",
                unit_id=f"{requirement_id}__{_safe(module_id)}",
                schema_name="arc_module_decomposition",
                instructions=MODULE_DECOMPOSITION_INSTRUCTIONS,
                output_schema=decomposition_schema,
                context=context,
                validator=lambda value: _simple_decomposition_issues(value, module),
            )
            if result.value is None:
                issues = result.issues
                if any(issue.code in {"FLOW_SOURCE_MISSING", "PARENT_CONTRACT_MISSING", "PARENT_OUTPUT_UNREALIZED"} for issue in issues):
                    for issue in issues:
                        issue.repair_action = REOPEN_PARENT
                        issue.owner_phase = "PARENT_MODULE_DECOMPOSITION"
                return None, issues
            trial = state.clone()
            last_trial = trial
            child_ids, issues = _materialize_simple_decomposition(
                trial,
                requirement_id,
                module_id,
                result.value,
            )
            if issues:
                return None, issues
            failure: list[DesignIssue] = []
            for child_id in child_ids:
                child = trial.modules[child_id]
                if child["kind"] != "FUNC" or child["owner_requirement"] != requirement_id:
                    continue
                expanded, child_issues = self._expand_module(
                    trial,
                    requirement_id,
                    requirement,
                    child_id,
                    requirement_contract,
                    design_context,
                    depth=depth + 1,
                )
                if expanded is None:
                    failure = child_issues
                    break
                trial = expanded
            if not failure:
                return trial, []
            last_issues = failure
            if not any(issue.repair_action == REOPEN_PARENT for issue in failure):
                return None, failure
            fingerprint = _hash({
                "module_id": module_id,
                "decision": result.value,
                "issues": [issue.as_dict() for issue in failure],
            })
            if fingerprint in seen_failures:
                self._soft_warnings.append(
                    f"DESIGN_VALIDATION_WARNING module={module_id}: repeated child conflicts; "
                    "keeping the latest materialized decomposition."
                )
                return last_trial or state, []
            seen_failures.add(fingerprint)
            child_feedback = [issue.feedback() for issue in failure]
            self._trace(f"UPSTREAM_REOPEN phase=module_decomposition unit={module_id} depth={depth} attempt={reopen_attempt + 1}/{self._reopen_budget + 1} feedback={'; '.join(child_feedback)}")
        self._soft_warnings.append(
            f"DESIGN_VALIDATION_WARNING module={module_id}: decomposition repair budget exhausted; "
            "keeping the latest materialized decomposition."
        )
        return last_trial or state, []


def project_design_context(schema: dict[str, Any], requirement_id: str, dependencies: dict[str, Any]) -> dict[str, Any]:
    """Project the one authoritative Stage 1 view used by prompts and validators."""

    allowed_requirements = {requirement_id} | _dependency_closure(requirement_id, dependencies)
    entities: list[dict[str, Any]] = []
    allowed_entities: set[str] = set()
    for entity in schema.get("entities", []):
        requirement_ids = set(entity.get("requirement_ids", []))
        fields = [
            copy.deepcopy(item)
            for item in entity.get("fields", [])
            if not item.get("requirement_ids")
            or set(item.get("requirement_ids", [])) & allowed_requirements
        ]
        if requirement_ids & allowed_requirements or fields:
            key = str(entity.get("key", "")).lower()
            allowed_entities.add(key)
            entities.append({
                "key": key,
                "description": entity.get("description", ""),
                "fields": [{
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "nullable": item.get("nullable"),
                    "references": item.get("references"),
                    "properties": copy.deepcopy(item.get("properties", {})),
                    "origin": item.get("origin"),
                    "primary_key": bool(item.get("primary_key")),
                } for item in fields],
            })
    relationships = [
        copy.deepcopy(item)
        for item in schema.get("relationships", [])
        if str(item.get("parent", "")).lower() in allowed_entities
        and str(item.get("child", "")).lower() in allowed_entities
    ]
    constraints: list[dict[str, Any]] = []
    for raw in schema.get("constraints", []):
        fields = [str(value) for value in raw.get("fields", [])]
        referenced = {value.partition(".")[0].lower() for value in fields if "." in value}
        requirement_ids = set(raw.get("requirement_ids", []))
        if referenced and not referenced <= allowed_entities:
            continue
        if requirement_ids and not requirement_ids & allowed_requirements:
            continue
        item = copy.deepcopy(raw)
        item["id"] = str(raw.get("id") or f"constraint_{_hash(raw)[:12]}")
        constraints.append(item)
    return {
        "requirement_id": requirement_id,
        "allowed_requirement_ids": sorted(allowed_requirements),
        "entities": sorted(entities, key=lambda item: item["key"]),
        "relationships": relationships,
        "constraints": constraints,
        "allowed_entities": sorted(allowed_entities),
    }


def _model_design_context(context: dict[str, Any]) -> dict[str, Any]:
    """Project database facts that can affect the requirement contract."""

    return {
        "requirement_id": context.get("requirement_id"),
        "allowed_requirement_ids": copy.deepcopy(context.get("allowed_requirement_ids", [])),
        "allowed_entities": copy.deepcopy(context.get("allowed_entities", [])),
        "entities": copy.deepcopy(context.get("entities", [])),
        "relationships": copy.deepcopy(context.get("relationships", [])),
        "constraints": copy.deepcopy(context.get("constraints", [])),
    }


def _existing_api_contracts(state: DesignState, limit: int = 40) -> list[dict[str, Any]]:
    """Summarize every frozen API contract so a later requirement can reuse one.

    The summary carries identity only - module id, spec, effect signatures, and
    the semantic ids on each side of the interface - because reuse is decided by
    semantic identity rather than by prose.
    """

    summaries: list[dict[str, Any]] = []
    for module_id in sorted(state.modules):
        module = state.modules[module_id]
        if str(module.get("kind", "")) != "API":
            continue
        summaries.append({
            "module_id": module_id,
            "owner_requirement": str(module.get("owner_requirement", "")),
            "spec": str(module.get("spec", "")),
            "inputs": sorted(_semantic_id_set(module.get("inputs", []))),
            "outputs": sorted(_semantic_id_set(module.get("outputs", []))),
            # Keep compact semantic-id sets for reuse matching, and preserve
            # complete field metadata for EXTEND/REUSE validation.
            "input_fields": [
                {
                    "semantic_id": str(item.get("semantic_id", "")),
                    "name": str(item.get("name", "")),
                    "type": str(item.get("type", "")),
                    "required": bool(item.get("required", True)),
                }
                for item in module.get("inputs", [])
                if isinstance(item, dict) and str(item.get("semantic_id", ""))
            ],
            "output_fields": [
                {
                    "semantic_id": str(item.get("semantic_id", "")),
                    "name": str(item.get("name", "")),
                    "type": str(item.get("type", "")),
                    "required": bool(item.get("required", True)),
                }
                for item in module.get("outputs", [])
                if isinstance(item, dict) and str(item.get("semantic_id", ""))
            ],
            "effects": sorted(_effect_signature_set(module.get("effects", []))),
        })
    return summaries[-limit:]


def _semantic_id_set(fields: Iterable[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("semantic_id", ""))
        for item in fields
        if isinstance(item, dict) and str(item.get("semantic_id", ""))
    }


def _effect_signature_set(effects: Iterable[dict[str, Any]]) -> set[str]:
    signatures: set[str] = set()
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        operation, target, fields = _effect_signature(effect)
        signatures.add(f"{operation}:{target or '-'}({','.join(fields)})")
    return signatures


def _api_signature(api: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    return (
        _semantic_id_set(api.get("inputs", [])),
        _semantic_id_set(api.get("outputs", [])),
        _effect_signature_set(api.get("effects", [])),
    )


def _summary_signature(summary: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    return (
        {str(item) for item in summary.get("inputs", [])},
        {str(item) for item in summary.get("outputs", [])},
        {str(item) for item in summary.get("effects", [])},
    )


def _vocabulary_tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}


def _ui_vocabulary_issues(
    fields: Iterable[dict[str, Any]],
    label: str,
    phase: str,
    blame: str,
) -> list[DesignIssue]:
    """Reject presentation vocabulary on a data contract field."""

    issues: list[DesignIssue] = []
    for field_item in fields:
        if not isinstance(field_item, dict):
            continue
        semantic_id = str(field_item.get("semantic_id", ""))
        tokens = _vocabulary_tokens(semantic_id) | _vocabulary_tokens(
            str(field_item.get("name", ""))
        )
        offending = sorted(tokens & UI_VOCABULARY)
        if offending:
            issues.append(_issue(
                "FIELD_UI_VOCABULARY",
                f"{label} field {semantic_id or '(unnamed)'} uses presentation vocabulary "
                f"{offending}; contract fields name domain data, not rendering",
                phase,
                blame,
            ))
    return issues


def _entity_prefixed(semantic_id: str, allowed_entities: set[str]) -> bool:
    """Tell whether a semantic id is namespaced by one available entity."""

    head = re.split(r"[._]", semantic_id.lower(), maxsplit=1)[0]
    if not head:
        return False
    for entity in allowed_entities:
        normalized = str(entity).lower().strip()
        if not normalized:
            continue
        parts = set(normalized.split("_")) | {normalized}
        candidates = parts | {part.rstrip("s") for part in parts}
        if head in candidates or head.rstrip("s") in candidates:
            return True
    return False


def _api_reuse_issues(
    api: dict[str, Any],
    label: str,
    existing_by_id: dict[str, dict[str, Any]],
) -> list[DesignIssue]:
    """Validate one declared REUSE/EXTEND/CREATE judgement against frozen APIs."""

    decision = str(api.get("reuse_decision", "") or "").upper()
    target_id = str(api.get("reuse_target_module_id") or "").strip()
    if decision not in API_REUSE_DECISIONS:
        return [_issue(
            "API_REUSE_DECISION_INVALID",
            f"API.{label} must declare reuse_decision as one of {sorted(API_REUSE_DECISIONS)}",
            "REQUIREMENT_API",
            label,
        )]
    signature = _api_signature(api)
    inputs, outputs, effects = signature
    issues: list[DesignIssue] = []
    if decision == "CREATE":
        if target_id:
            issues.append(_issue(
                "API_REUSE_DECISION_INVALID",
                f"API.{label} declares CREATE and must set reuse_target_module_id to null",
                "REQUIREMENT_API",
                label,
            ))
        duplicate = next(
            (
                module_id
                for module_id in sorted(existing_by_id)
                if _summary_signature(existing_by_id[module_id]) == signature
            ),
            "",
        )
        if duplicate:
            issues.append(_issue(
                "API_REUSE_MISSED",
                f"API.{label} repeats the frozen contract of {duplicate}; declare "
                f"reuse_decision=REUSE with reuse_target_module_id={duplicate}",
                "REQUIREMENT_API",
                label,
            ))
        return issues
    target = existing_by_id.get(target_id)
    if target is None:
        known = sorted(existing_by_id)
        return [_issue(
            "API_REUSE_TARGET_UNKNOWN",
            f"API.{label} declares {decision} against unknown module id "
            f"{target_id or '(empty)'}; choose one of {known[:12]} or declare CREATE",
            "REQUIREMENT_API",
            label,
        )]
    target_inputs, target_outputs, target_effects = _summary_signature(target)
    if decision == "REUSE":
        if signature != (target_inputs, target_outputs, target_effects):
            issues.append(_issue(
                "API_REUSE_SIGNATURE_MISMATCH",
                f"API.{label} declares REUSE of {target_id} but differs on inputs "
                f"{sorted(inputs ^ target_inputs)}, outputs {sorted(outputs ^ target_outputs)}, "
                f"effects {sorted(effects ^ target_effects)}; copy the frozen contract exactly "
                "or declare EXTEND",
                "REQUIREMENT_API",
                label,
            ))
        return issues
    dropped = sorted((target_inputs - inputs) | (target_outputs - outputs))
    if dropped:
        issues.append(_issue(
            "API_EXTEND_DROPS_FIELD",
            f"API.{label} declares EXTEND of {target_id} but drops {dropped}; "
            "EXTEND keeps every frozen field",
            "REQUIREMENT_API",
            label,
        ))
    added = (inputs - target_inputs) | (outputs - target_outputs)
    required_added = sorted(
        str(field_item.get("semantic_id", ""))
        for field_item in [*api.get("inputs", []), *api.get("outputs", [])]
        if isinstance(field_item, dict)
        and str(field_item.get("semantic_id", "")) in added
        and bool(field_item.get("required"))
    )
    if required_added:
        issues.append(_issue(
            "API_EXTEND_REQUIRED_FIELD",
            f"API.{label} declares EXTEND of {target_id} and adds required fields "
            f"{required_added}; appended fields must be optional",
            "REQUIREMENT_API",
            label,
        ))
    # An extension may preserve the public interface while adding a distinct
    # behavioral obligation (for example SaveDraft vs SavePage). REUSE remains
    # the preferred label for an identical operation, but rejecting a
    # field-preserving EXTEND would make behavior-only specializations
    # impossible to express.
    return issues


def _requirement_contract_issues(
    value: dict[str, Any],
    requirement_id: str,
    context: dict[str, Any],
    requirement: dict[str, Any],
) -> list[DesignIssue]:
    issues: list[DesignIssue] = []
    if value.get("requirement_id") != requirement_id:
        issues.append(_issue("CONTRACT_ID_MISMATCH", "requirement_id must match the fixed requirement", "REQUIREMENT_CONTRACT", requirement_id))
    if not str(value.get("spec", "")).strip():
        issues.append(_issue("CONTRACT_SPEC_EMPTY", "spec must not be empty", "REQUIREMENT_CONTRACT", requirement_id))
    server_state = value.get("server_state")
    if not isinstance(server_state, bool):
        issues.append(_issue("CONTRACT_SERVER_STATE_INVALID", "server_state must be a boolean", "REQUIREMENT_CONTRACT", requirement_id))
    issues.extend(_field_issues(value.get("inputs", []), "requirement inputs", requirement_id))
    issues.extend(_field_issues(value.get("outputs", []), "requirement outputs", requirement_id))
    issues.extend(_ui_vocabulary_issues(
        value.get("inputs", []), "Requirement input", "REQUIREMENT_CONTRACT", requirement_id
    ))
    issues.extend(_ui_vocabulary_issues(
        value.get("outputs", []), "Requirement output", "REQUIREMENT_CONTRACT", requirement_id
    ))
    input_fields = _simple_field_catalog(value.get("inputs", []))
    output_fields = _simple_field_catalog(value.get("outputs", []))
    for semantic_id in set(input_fields) & set(output_fields):
        if _simple_field_type(input_fields[semantic_id]) != _simple_field_type(output_fields[semantic_id]):
            issues.append(_issue(
                "FIELD_SEMANTIC_ID_CONFLICT",
                f"Requirement output changes the type of input {semantic_id}",
                "REQUIREMENT_CONTRACT",
                requirement_id,
            ))
    database_fields = _database_field_catalog(context)
    allowed_entities = set(context["allowed_entities"])
    effect_ids: set[str] = set()
    for effect in value.get("effects", []):
        effect_id = str(effect.get("id", ""))
        if effect_id in effect_ids:
            issues.append(_issue("DUPLICATE_EFFECT", f"Duplicate effect id: {effect_id}", "REQUIREMENT_CONTRACT", requirement_id))
        effect_ids.add(effect_id)
        operation = str(effect.get("operation", ""))
        target = str(effect.get("target") or "").lower()
        if operation in DATABASE_EFFECT_OPERATIONS and target not in allowed_entities:
            issues.append(_issue("DATA_DOMAIN_OUT_OF_SCOPE", f"Effect {effect_id} references unavailable entity: {target}", "REQUIREMENT_CONTRACT", requirement_id))
        unknown = set(effect.get("fields", [])) - database_fields.get(target, set())
        database_backed = operation in DATABASE_EFFECT_OPERATIONS or (
            operation == "SESSION_WRITE" and target in allowed_entities
        )
        if database_backed and unknown:
            issues.append(_issue("UNKNOWN_DATABASE_FIELD", f"Effect {effect_id} references unknown fields: {sorted(unknown)}", "REQUIREMENT_CONTRACT", requirement_id))
    obligation_ids: set[str] = set()
    covered_scenarios: set[str] = set()
    known_scenarios = {
        str(item.get("scenario_id") or item.get("id"))
        for item in requirement.get("scenarios", [])
        if isinstance(item, dict) and str(item.get("scenario_id") or item.get("id", ""))
    }
    for obligation in value.get("obligations", []):
        obligation_id = str(obligation.get("id", ""))
        if obligation_id in obligation_ids:
            issues.append(_issue("DUPLICATE_OBLIGATION", f"Duplicate obligation id: {obligation_id}", "REQUIREMENT_CONTRACT", requirement_id))
        obligation_ids.add(obligation_id)
        if not str(obligation.get("description", "")).strip():
            issues.append(_issue("OBLIGATION_DESCRIPTION_EMPTY", f"Obligation {obligation_id} needs a description", "REQUIREMENT_CONTRACT", requirement_id))
        scenario_ids = {str(value) for value in obligation.get("scenario_ids", [])}
        unknown_scenarios = scenario_ids - known_scenarios
        if unknown_scenarios:
            issues.append(_issue("OBLIGATION_SCENARIO_UNKNOWN", f"Obligation {obligation_id} references unknown scenarios: {sorted(unknown_scenarios)}", "REQUIREMENT_CONTRACT", requirement_id))
        covered_scenarios.update(scenario_ids & known_scenarios)
    missing_scenarios = known_scenarios - covered_scenarios
    if missing_scenarios:
        issues.append(_issue("SCENARIO_OBLIGATION_COVERAGE", f"Scenarios have no behavioral obligation: {sorted(missing_scenarios)}", "REQUIREMENT_CONTRACT", requirement_id))
    has_database_effect = any(
        str(effect.get("operation", "")) in DATABASE_EFFECT_OPERATIONS
        for effect in value.get("effects", [])
    )
    if server_state is False:
        server_effects = [
            str(effect.get("operation", ""))
            for effect in value.get("effects", [])
            if str(effect.get("operation", "")) in EFFECT_OPERATIONS
        ]
        server_obligations = [
            str(obligation.get("kind", ""))
            for obligation in value.get("obligations", [])
            if str(obligation.get("kind", "")) in {
                "AUTHORIZATION", "PERSISTENCE", "TRANSACTION", "IDEMPOTENCY", "EXTERNAL_INTERACTION",
            }
        ]
        if server_effects:
            issues.append(_issue("FRONTEND_ONLY_SERVER_EFFECT", f"server_state=false cannot declare server effects: {sorted(set(server_effects))}", "REQUIREMENT_CONTRACT", requirement_id))
        if server_obligations:
            issues.append(_issue("FRONTEND_ONLY_SERVER_OBLIGATION", f"server_state=false cannot declare server obligations: {sorted(set(server_obligations))}", "REQUIREMENT_CONTRACT", requirement_id))
    outputs = [item for item in value.get("outputs", []) if isinstance(item, dict)]
    if has_database_effect and outputs and not any(
        _entity_prefixed(str(item.get("semantic_id", "")), allowed_entities)
        for item in outputs
    ):
        issues.append(_issue(
            "CONTRACT_OUTPUT_ENTITY_PREFIX",
            "A database-backed contract must expose at least one output whose semantic_id is "
            f"prefixed with the entity it belongs to, for example {sorted(allowed_entities)[:3]}; "
            f"got {sorted(str(item.get('semantic_id', '')) for item in outputs)}",
            "REQUIREMENT_CONTRACT",
            requirement_id,
        ))
    if has_database_effect and not any(
        obligation.get("kind") == "PERSISTENCE"
        for obligation in value.get("obligations", [])
    ):
        issues.append(_issue(
            "PERSISTENCE_OBLIGATION_MISSING",
            "Database effects require at least one PERSISTENCE obligation",
            "REQUIREMENT_CONTRACT",
            requirement_id,
        ))
    return issues


def _api_plan_issues(
    value: dict[str, Any],
    requirement_id: str,
    contract: dict[str, Any],
    existing_apis: list[dict[str, Any]] | None = None,
) -> list[DesignIssue]:
    issues: list[DesignIssue] = []
    existing_by_id = {
        str(row.get("module_id", "")): row
        for row in (existing_apis or [])
        if isinstance(row, dict) and str(row.get("module_id", ""))
    }
    apis = value.get("modules", [])
    names = [str(item.get("name", "")) for item in apis]
    if len(names) != len(set(names)):
        issues.append(_issue("DUPLICATE_API", "API names must be unique within the requirement", "REQUIREMENT_API", requirement_id))
    contract_inputs = _simple_field_catalog(contract.get("inputs", []))
    contract_outputs = _simple_field_catalog(contract.get("outputs", []))
    expected_effects = {item["id"]: item for item in contract.get("effects", [])}
    expected_obligations = {str(item["id"]) for item in contract.get("obligations", [])}
    exposed_inputs: set[str] = set()
    exposed_outputs: set[str] = set()
    allocated_effects: list[str] = []
    allocated_obligations: list[str] = []
    self_obligations = [str(value) for value in value.get("self_obligation_ids", [])]
    if self_obligations and "API" not in SELF_OBLIGATION_OWNER_KINDS:
        issues.append(_issue("SELF_OBLIGATION_OWNER_INVALID", "API may not own obligations directly", "REQUIREMENT_API", requirement_id))
    allocated_obligations.extend(self_obligations)
    unknown_self = set(self_obligations) - expected_obligations
    if unknown_self:
        issues.append(_issue("OBLIGATION_OUT_OF_CONTRACT", f"API self ownership declares unknown obligations: {sorted(unknown_self)}", "REQUIREMENT_API", requirement_id))
    for api in apis:
        name = str(api.get("name", ""))
        issues.extend(_simple_interface_field_issues(api.get("inputs", []), f"API.{name} inputs", requirement_id))
        issues.extend(_simple_interface_field_issues(api.get("outputs", []), f"API.{name} outputs", requirement_id))
        if not str(api.get("spec", "")).strip():
            issues.append(_issue("API_SPEC_EMPTY", f"API.{name} spec must not be empty", "REQUIREMENT_API", name))
        issues.extend(_ui_vocabulary_issues(
            [*api.get("inputs", []), *api.get("outputs", [])],
            f"API.{name}",
            "REQUIREMENT_API",
            name,
        ))
        issues.extend(_api_reuse_issues(api, name, existing_by_id))
        reuse_decision = str(api.get("reuse_decision", "") or "").upper()
        reuse_target = existing_by_id.get(str(api.get("reuse_target_module_id") or "").strip())
        inherited_inputs = _simple_field_catalog(
            reuse_target.get("input_fields", [])
            if reuse_target and reuse_decision in {"REUSE", "EXTEND"}
            else []
        )
        inherited_outputs = _simple_field_catalog(
            reuse_target.get("output_fields", [])
            if reuse_target and reuse_decision in {"REUSE", "EXTEND"}
            else []
        )
        for field_item in api.get("inputs", []):
            semantic_id = str(field_item.get("semantic_id", ""))
            expected = contract_inputs.get(semantic_id)
            if expected is None:
                expected = inherited_inputs.get(semantic_id)
            if expected is None or _simple_field_type(expected) != _simple_field_type(field_item):
                issues.append(_issue("API_FIELD_OUT_OF_CONTRACT", f"API.{name} changes or invents input {semantic_id}", "REQUIREMENT_API", name))
            exposed_inputs.add(semantic_id)
        for field_item in api.get("outputs", []):
            semantic_id = str(field_item.get("semantic_id", ""))
            expected = contract_outputs.get(semantic_id)
            if expected is None:
                expected = inherited_outputs.get(semantic_id)
            if expected is None or _simple_field_type(expected) != _simple_field_type(field_item):
                issues.append(_issue("API_FIELD_OUT_OF_CONTRACT", f"API.{name} changes or invents output {semantic_id}", "REQUIREMENT_API", name))
            exposed_outputs.add(semantic_id)
        for effect in api.get("effects", []):
            effect_id = str(effect.get("id", ""))
            expected = expected_effects.get(effect_id)
            allocated_effects.append(effect_id)
            if expected is None or _effect_signature(expected) != _effect_signature(effect):
                issues.append(_issue("API_EFFECT_OUT_OF_CONTRACT", f"API.{name} changes or invents effect {effect_id}", "REQUIREMENT_API", name))
        allocated_obligations.extend(str(value) for value in api.get("obligation_ids", []))
    missing_inputs = set(contract_inputs) - exposed_inputs
    missing_outputs = set(contract_outputs) - exposed_outputs
    if missing_inputs:
        issues.append(_issue("API_INPUT_COVERAGE", f"Contract inputs are not exposed: {sorted(missing_inputs)}", "REQUIREMENT_API", requirement_id))
    if missing_outputs:
        issues.append(_issue("API_OUTPUT_COVERAGE", f"Contract outputs are not exposed: {sorted(missing_outputs)}", "REQUIREMENT_API", requirement_id))
    # A READ is a reusable lookup contract: independent APIs may legitimately
    # perform the same lookup. Mutating effects remain single-owner because
    # duplicating CREATE/UPDATE/DELETE would duplicate side effects.
    unique_effects = {
        effect_id
        for effect_id, effect in expected_effects.items()
        if str(effect.get("operation", "")).upper() != "READ"
    }
    allocated_unique_effects = [
        effect_id for effect_id in allocated_effects if effect_id in unique_effects
    ]
    issues.extend(_allocation_issues(
        allocated_unique_effects,
        unique_effects,
        "effect",
        "REQUIREMENT_API",
        requirement_id,
    ))
    missing_read_effects = sorted(
        effect_id
        for effect_id, effect in expected_effects.items()
        if str(effect.get("operation", "")).upper() == "READ"
        and effect_id not in allocated_effects
    )
    if missing_read_effects:
        issues.append(_issue(
            "EFFECT_ALLOCATION_MISMATCH",
            f"effect allocation must cover the parent exactly: missing={missing_read_effects}.",
            "REQUIREMENT_API",
            requirement_id,
        ))
    issues.extend(_allocation_issues(allocated_obligations, expected_obligations, "obligation", "REQUIREMENT_API", requirement_id))
    return issues


def _simple_decomposition_issues(
    value: dict[str, Any],
    parent: dict[str, Any],
) -> list[DesignIssue]:
    """Validate only the small public module contract returned by the model."""

    issues: list[DesignIssue] = []
    parent_id = str(parent["id"])
    available = _simple_field_catalog(parent.get("inputs", []))
    parent_outputs = _simple_field_catalog(parent.get("outputs", []))
    expected_effects = {item["id"]: item for item in _module_visible_effects(parent)}
    expected_obligations = {
        str(item["id"]): item for item in parent.get("obligations", []) if item.get("id")
    }
    allocated_effects: list[str] = []
    allocated_obligations: list[str] = []
    self_obligations = [str(item) for item in value.get("self_obligation_ids", [])]
    if self_obligations and str(parent.get("kind", "")) not in SELF_OBLIGATION_OWNER_KINDS:
        issues.append(_issue("SELF_OBLIGATION_OWNER_INVALID", f"{parent.get('kind')} may not own obligations directly", "MODULE_DECOMPOSITION", parent_id))
    allocated_obligations.extend(self_obligations)
    unknown_self = set(self_obligations) - set(expected_obligations)
    if unknown_self:
        issues.append(_issue("OBLIGATION_OUT_OF_CONTRACT", f"Self ownership declares unknown obligations: {sorted(unknown_self)}", "MODULE_DECOMPOSITION", parent_id))
    names = [str(module.get("name", "")) for module in value.get("modules", [])]
    if len(names) != len(set(names)):
        issues.append(_issue(
            "DUPLICATE_MODULE",
            "Direct child module names must be unique",
            "MODULE_DECOMPOSITION",
            parent_id,
        ))

    for index, module in enumerate(value.get("modules", []), start=1):
        label = str(module.get("name") or f"module {index}")
        kind = str(module.get("kind", ""))
        if not str(module.get("spec", "")).strip():
            issues.append(_issue(
                "MODULE_SPEC_EMPTY",
                f"Module {label} spec must not be empty",
                "MODULE_DECOMPOSITION",
                parent_id,
            ))
        inputs = module.get("inputs", [])
        outputs = module.get("outputs", [])
        issues.extend(_simple_interface_field_issues(inputs, f"{label} inputs", parent_id))
        issues.extend(_simple_interface_field_issues(outputs, f"{label} outputs", parent_id))

        for field_item in inputs:
            semantic_id = str(field_item.get("semantic_id", ""))
            source = available.get(semantic_id)
            if source is None:
                issues.append(_issue(
                    "FLOW_SOURCE_MISSING",
                    f"Module {label} input has no parent or earlier-child source: {semantic_id}",
                    "MODULE_DECOMPOSITION",
                    parent_id,
                ))
            elif _simple_field_type(source) != _simple_field_type(field_item):
                issues.append(_issue(
                    "FIELD_SEMANTIC_ID_CONFLICT",
                    f"Module {label} changes the type of {semantic_id}: "
                    f"expected {_simple_field_type(source)}, got {_simple_field_type(field_item)}",
                    "MODULE_DECOMPOSITION",
                    parent_id,
                ))

        for field_item in outputs:
            semantic_id = str(field_item.get("semantic_id", ""))
            source = available.get(semantic_id)
            if source is not None and _simple_field_type(source) != _simple_field_type(field_item):
                issues.append(_issue(
                    "FIELD_SEMANTIC_ID_CONFLICT",
                    f"Module {label} reuses {semantic_id} with a different type: "
                    f"expected {_simple_field_type(source)}, got {_simple_field_type(field_item)}",
                    "MODULE_DECOMPOSITION",
                    parent_id,
                ))
            elif source is None:
                available[semantic_id] = copy.deepcopy(field_item)

        module_effects = module.get("effects", [])
        module_obligations = [str(value) for value in module.get("obligation_ids", [])]
        allocated_obligations.extend(module_obligations)
        unknown_obligations = set(module_obligations) - set(expected_obligations)
        if unknown_obligations:
            issues.append(_issue("OBLIGATION_OUT_OF_CONTRACT", f"Module {label} declares unknown obligations: {sorted(unknown_obligations)}", "MODULE_DECOMPOSITION", parent_id))
        if kind == "DB":
            invalid = sorted(
                obligation_id for obligation_id in module_obligations
                if expected_obligations.get(obligation_id, {}).get("kind") != "PERSISTENCE"
            )
            if invalid:
                issues.append(_issue("DB_OBLIGATION_INVALID", f"DB module {label} may own only PERSISTENCE obligations: {invalid}", "MODULE_DECOMPOSITION", parent_id))
        if kind == "DB" and not module_effects:
            issues.append(_issue(
                "DB_EFFECT_REQUIRED",
                f"DB module {label} must own at least one READ, CREATE, UPDATE, or DELETE effect",
                "MODULE_DECOMPOSITION",
                parent_id,
            ))
        for effect in module_effects:
            effect_id = str(effect.get("id", ""))
            allocated_effects.append(effect_id)
            operation = str(effect.get("operation", ""))
            if kind == "DB" and operation not in DATABASE_EFFECT_OPERATIONS:
                issues.append(_issue(
                    "DB_EFFECT_INVALID",
                    f"DB module {label} cannot own {operation or 'an empty operation'}; "
                    "DB modules accept only READ, CREATE, UPDATE, or DELETE",
                    "MODULE_DECOMPOSITION",
                    parent_id,
                ))
            expected = expected_effects.get(effect_id)
            if expected is None:
                issues.append(_issue(
                    "EFFECT_OUT_OF_CONTRACT",
                    f"Module {label} declares an unknown effect: {effect_id}",
                    "MODULE_DECOMPOSITION",
                    parent_id,
                ))
                continue
            expected_signature = (
                expected.get("operation"),
                expected.get("target"),
                tuple(sorted(expected.get("fields", []))),
            )
            actual_signature = (
                effect.get("operation"),
                effect.get("target"),
                tuple(sorted(effect.get("fields", []))),
            )
            if actual_signature != expected_signature:
                issues.append(_issue(
                    "EFFECT_CONTRACT_MISMATCH",
                    f"Module {label} changes effect {effect_id}; copy it exactly from the parent",
                    "MODULE_DECOMPOSITION",
                    parent_id,
                ))

    missing_outputs = set(parent_outputs) - set(available)
    if parent.get("kind") == "API" and missing_outputs:
        issues.append(_issue(
            "PARENT_OUTPUT_UNREALIZED",
            f"Child modules do not produce parent outputs: {sorted(missing_outputs)}",
            "MODULE_DECOMPOSITION",
            parent_id,
        ))
    required_effects = (
        set(expected_effects)
        if parent.get("kind") == "API"
        else {
            effect_id
            for effect_id, effect in expected_effects.items()
            if effect.get("operation") in DATABASE_EFFECT_OPERATIONS
        }
    )
    allocated_required = [effect_id for effect_id in allocated_effects if effect_id in required_effects]
    issues.extend(_allocation_issues(
        allocated_required,
        required_effects,
        "effect",
        "MODULE_DECOMPOSITION",
        parent_id,
    ))
    required_obligations = set(expected_obligations)
    issues.extend(_allocation_issues(
        allocated_obligations,
        required_obligations,
        "obligation",
        "MODULE_DECOMPOSITION",
        parent_id,
    ))
    repeated_obligations = sorted({value for value in allocated_obligations if allocated_obligations.count(value) > 1})
    if repeated_obligations:
        issues.append(_issue("OBLIGATION_ALLOCATED_TWICE", f"obligations are allocated more than once: {repeated_obligations}", "MODULE_DECOMPOSITION", parent_id))
    repeated_effects = sorted({effect_id for effect_id in allocated_effects if allocated_effects.count(effect_id) > 1})
    if repeated_effects and not any(issue.code == "EFFECT_ALLOCATED_TWICE" for issue in issues):
        issues.append(_issue(
            "EFFECT_ALLOCATED_TWICE",
            f"effects are allocated more than once: {repeated_effects}",
            "MODULE_DECOMPOSITION",
            parent_id,
        ))
    return issues


def _simple_interface_field_issues(
    fields: list[dict[str, Any]],
    label: str,
    blame: str,
) -> list[DesignIssue]:
    issues: list[DesignIssue] = []
    seen: set[str] = set()
    names: dict[str, dict[str, Any]] = {}
    for item in fields:
        semantic_id = str(item.get("semantic_id", ""))
        if semantic_id in seen:
            issues.append(_issue(
                "DUPLICATE_FIELD",
                f"{label} repeats semantic id: {semantic_id}",
                "MODULE_DECOMPOSITION",
                blame,
            ))
        seen.add(semantic_id)
        name = str(item.get("name", "")).strip()
        prior = names.get(name)
        if prior is not None and prior.get("type") != item.get("type"):
            issues.append(_issue(
                "DUPLICATE_FIELD_NAME_TYPE_CONFLICT",
                f"{label} maps property name {name!r} to incompatible types",
                "MODULE_DECOMPOSITION",
                blame,
            ))
        elif prior is not None:
            issues.append(_issue(
                "DUPLICATE_FIELD_NAME",
                f"{label} reuses property name {name!r}; use distinct names for distinct semantic values",
                "MODULE_DECOMPOSITION",
                blame,
            ))
        names[name] = item
    return issues


def _behavioral_completeness_issues(
    design_ir: dict[str, Any],
    *,
    requirement_ids: set[str] | None = None,
) -> list[DesignIssue]:
    """Ensure every frozen obligation reaches an implementable module seam."""

    issues: list[DesignIssue] = []
    modules_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for module in design_ir.get("modules", []):
        if isinstance(module, dict):
            modules_by_requirement.setdefault(
                str(module.get("owner_requirement", "")), []
            ).append(module)
    for requirement in design_ir.get("requirements", []):
        requirement_id = str(requirement.get("id", ""))
        if requirement_ids is not None and requirement_id not in requirement_ids:
            continue
        contract = requirement.get("contract", {})
        if contract.get("server_state") is False:
            continue
        modules = modules_by_requirement.get(requirement_id, [])
        for obligation in contract.get("obligations", []):
            obligation_id = str(obligation.get("id", ""))
            implementers = [
                module for module in modules
                if (
                    (
                        str(module.get("kind", "")) in SELF_OBLIGATION_OWNER_KINDS
                        and obligation_id in {
                            str(value) for value in module.get("self_obligation_ids", [])
                        }
                    )
                    or (
                        str(module.get("kind", "")) in {"FUNC", "DB"}
                        and not module.get("callees")
                        and obligation_id in {
                            str(row.get("id", ""))
                            for row in module.get("obligations", [])
                            if isinstance(row, dict)
                        }
                    )
                )
            ]
            if not implementers:
                issues.append(_issue(
                    "OBLIGATION_IMPLEMENTATION_MISSING",
                    f"Behavioral obligation {obligation_id} has no implementation owner; "
                    f"self owners are {sorted(SELF_OBLIGATION_OWNER_KINDS)}",
                    "DESIGN_COMPLETENESS",
                    requirement_id,
                ))
            elif (
                obligation.get("kind") == "PERSISTENCE"
                and not any(module.get("kind") == "DB" for module in implementers)
                and not any(
                    obligation_id in {
                        str(value)
                        for value in module.get("self_obligation_ids", [])
                    }
                    for module in implementers
                )
            ):
                issues.append(_issue(
                    "PERSISTENCE_OBLIGATION_DB_MISSING",
                    f"Persistence obligation {obligation_id} has no DB implementation owner",
                    "DESIGN_COMPLETENESS",
                    requirement_id,
                ))
    return issues


def _materialize_apis(state: DesignState, requirement_id: str, contract: dict[str, Any], decision: dict[str, Any]) -> tuple[list[str], list[DesignIssue]]:
    fields = _field_catalog([*contract.get("inputs", []), *contract.get("outputs", [])])
    effects = {item["id"]: item for item in contract.get("effects", [])}
    obligations = {str(item["id"]): item for item in contract.get("obligations", [])}
    api_ids: list[str] = []
    for item in decision.get("modules", []):
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            # The semantic validator has already reported the malformed entry;
            # soft-fail must not turn it into an unhandled KeyError.
            continue
        reuse_decision = str(item.get("reuse_decision", "") or "").upper()
        reuse_target = state.modules.get(str(item.get("reuse_target_module_id") or "").strip())
        # EXTEND/REUSE APIs keep the frozen target interface.  The current
        # requirement contract may contain only the delta, so add inherited
        # fields to the materialization catalog before expanding model output.
        if reuse_target and reuse_decision in {"REUSE", "EXTEND"}:
            fields = _field_catalog([
                *fields.values(),
                *reuse_target.get("inputs", []),
                *reuse_target.get("outputs", []),
            ])
        module_id = _qualified_module_id(requirement_id, "API", item["name"])
        if module_id in state.modules:
            return [], [_issue("MODULE_ID_CONFLICT", f"Module id already exists: {module_id}", "REQUIREMENT_API", module_id)]
        module = {
            "id": module_id,
            "kind": "API",
            "owner_requirement": requirement_id,
            "spec": item["spec"],
            "inputs": [_expand_interface_field(field_item, fields) for field_item in item.get("inputs", [])],
            "outputs": [_expand_interface_field(field_item, fields) for field_item in item.get("outputs", [])],
            "effects": [
                _compact_module_effect(effects[item_effect["id"]])
                for item_effect in item.get("effects", [])
                if isinstance(item_effect, dict) and item_effect.get("id") in effects
            ],
            "obligations": [
                copy.deepcopy(obligations[value])
                for value in item.get("obligation_ids", [])
                if value in obligations
            ],
            "self_obligation_ids": (
                [str(value) for value in decision.get("self_obligation_ids", [])]
                if not api_ids
                else []
            ),
            "parent_id": None,
        }
        state.modules[module_id] = module
        state.requirement_modules.setdefault(requirement_id, set()).add(module_id)
        api_ids.append(module_id)
    return api_ids, []


def _materialize_simple_decomposition(
    state: DesignState,
    requirement_id: str,
    parent_id: str,
    decision: dict[str, Any],
) -> tuple[list[str], list[DesignIssue]]:
    """Create child symbols and calls from the seven-field module format."""

    parent = state.modules[parent_id]
    existing_self_obligations = [
        str(value) for value in parent.get("self_obligation_ids", [])
    ]
    current_self_obligations = [
        str(value) for value in decision.get("self_obligation_ids", [])
    ]
    parent["self_obligation_ids"] = list(dict.fromkeys(
        [*existing_self_obligations, *current_self_obligations]
    ))
    authoritative_effects = {item["id"]: item for item in _module_visible_effects(parent)}
    authoritative_obligations = {
        str(item["id"]): item for item in parent.get("obligations", []) if item.get("id")
    }
    field_catalog = _field_catalog([*parent.get("inputs", []), *parent.get("outputs", [])])
    child_ids: list[str] = []

    for item in decision.get("modules", []):
        if (
            not isinstance(item, dict)
            or not str(item.get("name", "")).strip()
            or str(item.get("kind", "")).upper() not in {"FUNC", "DB"}
        ):
            continue
        # The schema is intentionally tolerant at this boundary.  Qualified
        # module ids are compiler-owned and always use the canonical kind
        # spelling, regardless of the model's casing.
        kind = str(item["kind"]).strip().upper()
        child_inputs = [_expand_interface_field(field_item, field_catalog) for field_item in item.get("inputs", [])]
        child_outputs = [_expand_interface_field(field_item, field_catalog) for field_item in item.get("outputs", [])]
        for field_item in [*child_inputs, *child_outputs]:
            field_catalog.setdefault(field_item["semantic_id"], copy.deepcopy(field_item))
        selected_effects = [
            copy.deepcopy(authoritative_effects[effect["id"]])
            for effect in item.get("effects", [])
            if isinstance(effect, dict) and effect.get("id") in authoritative_effects
        ]
        child_contract = {
            "kind": kind,
            "owner_requirement": requirement_id,
            "spec": str(item.get("spec", "")).strip(),
            "inputs": child_inputs,
            "outputs": child_outputs,
            "effects": [_compact_module_effect(effect) for effect in selected_effects],
            "obligations": [
                copy.deepcopy(authoritative_obligations[value])
                for value in item.get("obligation_ids", [])
                if value in authoritative_obligations
            ],
            "parent_id": parent_id,
        }
        child_id = _allocate_local_module_id(
            state,
            requirement_id,
            kind,
            str(item["name"]),
        )
        child = {
            **child_contract,
            "id": child_id,
        }
        state.modules[child_id] = child
        state.requirement_modules.setdefault(requirement_id, set()).add(child_id)

        state.invocations.append({
            "caller": parent_id,
            "callee": child_id,
        })
        child_ids.append(child_id)

    return child_ids, []


def _collapse_passthrough_modules(
    state: DesignState,
    requirement_id: str,
) -> tuple[DesignState, list[str]]:
    """Remove FUNC nodes that only forward to one equivalent FUNC child."""

    warnings: list[str] = []
    changed = True
    while changed:
        changed = False
        for parent_id in sorted(
            list(state.requirement_modules.get(requirement_id, set()))
        ):
            parent = state.modules.get(parent_id)
            if not parent or parent.get("kind") != "FUNC":
                continue
            children = [
                str(invocation.get("callee", ""))
                for invocation in state.invocations
                if str(invocation.get("caller", "")) == parent_id
            ]
            if len(children) != 1:
                continue
            child_id = children[0]
            child = state.modules.get(child_id)
            if not child or child.get("kind") != "FUNC":
                continue
            if parent.get("self_obligation_ids"):
                continue
            comparable = ("inputs", "outputs", "effects", "obligations")
            if any(parent.get(key, []) != child.get(key, []) for key in comparable):
                continue
            callers = [
                dict(invocation)
                for invocation in state.invocations
                if str(invocation.get("callee", "")) == parent_id
            ]
            state.invocations = [
                invocation
                for invocation in state.invocations
                if not (
                    str(invocation.get("caller", "")) == parent_id
                    or str(invocation.get("callee", "")) == parent_id
                )
            ]
            for invocation in callers:
                replacement = {"caller": invocation["caller"], "callee": child_id}
                if replacement not in state.invocations:
                    state.invocations.append(replacement)
            child_callers = sorted({
                str(invocation.get("caller", ""))
                for invocation in state.invocations
                if str(invocation.get("callee", "")) == child_id
            })
            child["parent_id"] = child_callers[0] if len(child_callers) == 1 else None
            state.modules.pop(parent_id, None)
            state.requirement_modules.setdefault(requirement_id, set()).discard(parent_id)
            warnings.append(
                f"ARC3118: merged pass-through module {parent_id} into {child_id}"
            )
            changed = True
            break
    return state, warnings


def _normalize_requirement_contract(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    # Reused pre-M8 artifacts did not carry this discriminator; treat them as
    # server-backed so they remain conservative when hydrated.
    result["server_state"] = bool(result.get("server_state", True))
    result["inputs"] = _normalize_fields(result.get("inputs", []))
    result["outputs"] = _normalize_fields(result.get("outputs", []))
    for effect in result.get("effects", []):
        if effect.get("target"):
            effect["target"] = str(effect["target"]).lower()
        effect["fields"] = sorted(set(effect.get("fields", [])))
    for obligation in result.get("obligations", []):
        obligation["scenario_ids"] = sorted(set(obligation.get("scenario_ids", [])))
    result["obligations"] = sorted(
        result.get("obligations", []), key=lambda item: str(item.get("id", ""))
    )
    return result


def _normalize_database_backed_session_effects(
    contract: dict[str, Any],
    *,
    design_context: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Represent writes to a persisted session entity as database CREATE effects.

    SESSION_WRITE is reserved for runtime session stores that are not represented
    in Database IR. A model may still use that operation for an insertion into a
    supplied `session` entity; normalize that ambiguity before the contract is
    frozen so every persisted DB module uses the canonical CRUD vocabulary.
    """

    result = copy.deepcopy(contract)
    database_entities = {
        str(value).lower()
        for value in design_context.get("allowed_entities", [])
        if str(value).strip()
    }
    normalized: list[str] = []
    for effect in result.get("effects", []):
        operation = str(effect.get("operation", "")).upper()
        target = str(effect.get("target") or "").lower()
        if operation != "SESSION_WRITE" or target not in database_entities:
            continue
        effect["operation"] = "CREATE"
        normalized.append(str(effect.get("id", "")))
    return result, normalized


def _complete_constraint_read_effects(
    contract: dict[str, Any],
    *,
    requirement_id: str,
    design_context: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Complete READ effects implied by this requirement's uniqueness rules.

    UNIQUE constraints are enforced by the database, but requirements commonly
    need a preflight lookup to return a domain error instead of surfacing a raw
    constraint failure. The Database IR is authoritative for the entity/field
    identity, so this completion is deterministic and keeps all later module
    decomposition inside the frozen Requirement Contract.
    """

    result = copy.deepcopy(contract)
    effects = result.setdefault("effects", [])
    existing_ids = {str(effect.get("id", "")) for effect in effects}
    existing_read_fields: dict[str, set[str]] = {}
    for effect in effects:
        if str(effect.get("operation", "")).upper() != "READ":
            continue
        target = str(effect.get("target") or "").lower()
        existing_read_fields.setdefault(target, set()).update(
            str(value) for value in effect.get("fields", []) if str(value).strip()
        )
    fields_by_target: dict[str, set[str]] = {}

    for constraint in design_context.get("constraints", []):
        if str(constraint.get("type", "")).upper() not in {
            "UNIQUE",
            "COMPOSITE_UNIQUE",
        }:
            continue
        owner_ids = {
            str(value)
            for value in constraint.get("requirement_ids", [])
            if str(value).strip()
        }
        if owner_ids and requirement_id not in owner_ids:
            continue
        qualified_fields = [
            str(value).strip()
            for value in constraint.get("fields", [])
            if str(value).strip()
        ]
        targets = {value.partition(".")[0].lower() for value in qualified_fields if "." in value}
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        fields = {
            value.partition(".")[2]
            for value in qualified_fields
            if value.partition(".")[0].lower() == target and value.partition(".")[2]
        }
        if fields:
            fields_by_target.setdefault(target, set()).update(fields)

    completed: list[str] = []
    for target, fields in sorted(fields_by_target.items()):
        ordered_fields = sorted(fields - existing_read_fields.get(target, set()))
        if not ordered_fields:
            continue
        base_id = _stable_effect_id("read_existing", target, ordered_fields)
        effect_id = base_id
        suffix = 2
        while effect_id in existing_ids:
            effect_id = f"{base_id[:61]}_{suffix}"
            suffix += 1
        effects.append({
            "id": effect_id,
            "operation": "READ",
            "target": target,
            "fields": ordered_fields,
        })
        existing_ids.add(effect_id)
        existing_read_fields.setdefault(target, set()).update(ordered_fields)
        completed.append(effect_id)

    effects.sort(key=lambda effect: str(effect.get("id", "")))
    return result, completed


def _stable_effect_id(prefix: str, target: str, fields: list[str]) -> str:
    parts = [
        re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        for value in (prefix, target, *fields)
    ]
    readable = "_".join(value for value in parts if value)
    if len(readable) <= 64:
        return readable
    digest = _hash({"prefix": prefix, "target": target, "fields": fields})[:10]
    return f"{readable[:53].rstrip('_')}_{digest}"


def _field_issues(fields: Any, label: str, blame: str) -> list[DesignIssue]:
    issues: list[DesignIssue] = []
    seen: dict[str, tuple[str, str]] = {}
    for item in fields if isinstance(fields, list) else []:
        semantic_id = str(item.get("semantic_id", ""))
        signature = (str(item.get("name", "")), str(item.get("type", "")))
        if semantic_id in seen and seen[semantic_id] != signature:
            issues.append(_issue("FIELD_SEMANTIC_ID_CONFLICT", f"{label} reuses {semantic_id} with an incompatible shape", "CONTRACT", blame))
        elif semantic_id in seen:
            issues.append(_issue("DUPLICATE_FIELD", f"{label} repeats semantic id: {semantic_id}", "CONTRACT", blame))
        seen[semantic_id] = signature
    return issues


def _allocation_issues(values: list[str], expected: set[str], label: str, phase: str, blame: str) -> list[DesignIssue]:
    actual = set(values)
    issues: list[DesignIssue] = []
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if label == "obligation":
            guidance = (
                "Repair by assigning every missing parent obligation id to exactly one direct child; "
                "copy ids verbatim, do not rename or invent ids, and do not drop negative obligations "
                "such as prevent/no-write/no-create. A PERSISTENCE obligation must remain on a child "
                "path that eventually reaches a DB leaf."
            )
        else:
            guidance = (
                "Repair by assigning every missing parent effect id to exactly one eligible direct child; "
                "copy the complete effect unchanged and do not invent or duplicate effects."
            )
        issue = _issue(
            f"{label.upper()}_ALLOCATION_MISMATCH",
            f"{label} allocation must cover the parent exactly: missing={missing} extra={extra}. {guidance}",
            phase,
            blame,
        )
        issue.context.update({"missing": missing, "extra": extra, "expected": sorted(expected), "actual": sorted(actual)})
        issues.append(issue)
    repeated = sorted({value for value in values if values.count(value) > 1})
    if repeated:
        issues.append(_issue(f"{label.upper()}_ALLOCATED_TWICE", f"{label} obligations are allocated more than once: {repeated}", phase, blame))
    return issues


def _shape_issues(value: Any, schema: dict[str, Any], phase: str, unit_id: str) -> list[DesignIssue]:
    errors = _shape_errors(value, schema, "$")
    return [_issue("STRUCTURED_OUTPUT_INVALID", error, phase.upper(), unit_id) for error in errors]


def _provider_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the strict-schema subset supported by OpenAI-compatible providers.

    Several compatible endpoints reject these JSON Schema annotation keywords
    before invoking the model. No schema declares them any more: bounded text
    length is not a compiler constraint, because a provider that never sees the
    bound cannot satisfy it and a response is not wrong for being verbose.
    """

    unsupported = {"maxLength", "uniqueItems"}
    return {
        key: _provider_output_schema(value) if isinstance(value, dict) else [
            _provider_output_schema(item) if isinstance(item, dict) else item
            for item in value
        ] if isinstance(value, list) else value
        for key, value in schema.items()
        if key not in unsupported
    }


def _module_decomposition_output_schema(parent: dict[str, Any], *, depth: int) -> dict[str, Any]:
    """Specialize the decomposition form so illegal layer choices are unavailable."""

    schema = copy.deepcopy(MODULE_DECOMPOSITION_SCHEMA)
    step_properties = schema["properties"]["modules"]["items"]["properties"]
    if parent.get("kind") == "API":
        allowed = {"FUNC"}
    elif depth >= 2:
        allowed = {"DB"}
    else:
        allowed = {"FUNC", "DB"}
    step_properties["kind"]["enum"] = sorted(allowed)
    obligation_ids = [
        str(item.get("id", ""))
        for item in parent.get("obligations", [])
        if isinstance(item, dict) and str(item.get("id", ""))
    ]
    schema["properties"]["self_obligation_ids"]["items"] = {
        "type": "string",
        "enum": sorted(set(obligation_ids)),
    }
    step_properties["obligation_ids"]["items"] = {
        "type": "string",
        "enum": sorted(set(obligation_ids)),
    }
    schema["properties"]["modules"]["minItems"] = 0
    return schema


def _allocation_output_schema(
    schema: dict[str, Any], obligations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Constrain allocation ids to the frozen parent ledger."""

    result = copy.deepcopy(schema)
    items = result["properties"]["modules"]["items"]["properties"]
    ids = sorted(
        {
            str(item.get("id", ""))
            for item in obligations
            if isinstance(item, dict) and str(item.get("id", ""))
        }
    )
    items["obligation_ids"]["items"] = {"type": "string", "enum": ids}
    result["properties"]["self_obligation_ids"]["items"] = {
        "type": "string",
        "enum": ids,
    }
    return result


def _shape_errors(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    if "anyOf" in schema:
        if any(not _shape_errors(value, option, path) for option in schema["anyOf"]):
            return []
        return [f"{path} does not match any allowed shape"]
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool), "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected and not type_ok.get(expected, True):
        return [f"{path} must be {expected}"]
    errors: list[str] = []
    if isinstance(value, dict) and expected == "object":
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        errors.extend(f"{path}.{key} is required" for key in sorted(missing))
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{key} is not allowed" for key in sorted(set(value) - set(properties)))
        for key in set(value) & set(properties):
            errors.extend(_shape_errors(value[key], properties[key], f"{path}.{key}"))
    if isinstance(value, list) and expected == "array":
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} has too many items")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            errors.append(f"{path} must contain unique items")
        for index, item in enumerate(value):
            errors.extend(_shape_errors(item, schema.get("items", {}), f"{path}[{index}]"))
    if isinstance(value, str):
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path} must be one of {schema['enum']}")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path} does not match {schema['pattern']}")
    return errors


def _issue(code: str, message: str, phase: str, blame: str, repair: str = REPAIR_CURRENT) -> DesignIssue:
    return DesignIssue(code, message, phase, blame, repair)


def _field_catalog(fields: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in fields:
        key = str(item.get("semantic_id", ""))
        if key and (key not in result or _field_signature(result[key]) == _field_signature(item)):
            result[key] = copy.deepcopy(item)
    return result


def _field_signature(item: dict[str, Any]) -> tuple[str, bool]:
    return str(item.get("type", "")), bool(item.get("required"))


def _normalize_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((copy.deepcopy(item) for item in fields), key=lambda item: item["semantic_id"])


def _database_field_catalog(context: dict[str, Any]) -> dict[str, set[str]]:
    return {str(entity.get("key", "")).lower(): {str(item.get("name", "")) for item in entity.get("fields", [])} for entity in context.get("entities", [])}


def _compact_module_effect(effect: dict[str, Any]) -> dict[str, Any]:
    """Project the semantic effect fields consumed by downstream passes."""

    return {
        key: copy.deepcopy(effect[key])
        for key in ("id", "operation", "target", "fields")
        if effect.get(key) is not None
    }


def _module_visible_effects(module: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the compact effects exposed by a module contract."""

    return [
        {
            "id": effect.get("id"),
            "operation": effect.get("operation"),
            "target": effect.get("target"),
            "fields": copy.deepcopy(effect.get("fields", [])),
        }
        for effect in module.get("effects", [])
        if effect.get("id")
    ]


def _simple_field_catalog(fields: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("semantic_id", "")): {
            "semantic_id": item.get("semantic_id"),
            "name": item.get("name"),
            "type": item.get("type"),
        }
        for item in fields
        if item.get("semantic_id")
    }


def _simple_field_type(field_item: dict[str, Any]) -> str:
    """Return the cross-module compatibility key for one semantic value."""

    return str(field_item.get("type", ""))


def _effect_signature(effect: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    return (
        str(effect.get("operation", "")),
        str(effect.get("target") or ""),
        tuple(sorted(str(value) for value in effect.get("fields", []))),
    )


def _expand_interface_field(
    field_item: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    semantic_id = str(field_item["semantic_id"])
    existing = catalog.get(semantic_id)
    if existing is not None:
        return {
            "semantic_id": semantic_id,
            "name": str(existing["name"]),
            "type": str(existing["type"]),
            "required": bool(existing.get("required", True)),
        }
    return {
        "semantic_id": semantic_id,
        "name": str(field_item["name"]),
        "type": str(field_item["type"]),
        "required": bool(field_item.get("required", True)),
    }


def _allocate_local_module_id(state: DesignState, requirement_id: str, kind: str, name: str) -> str:
    base = _qualified_module_id(requirement_id, kind, name)
    if base not in state.modules:
        return base
    index = 2
    while f"{base}{index}" in state.modules:
        index += 1
    return f"{base}{index}"


def _qualified_module_id(requirement_id: str, kind: str, name: str) -> str:
    # Module names are model-authored semantic labels.  Keep the label
    # readable, but make the compiler-owned qualified id deterministic even
    # when tolerant validation accepted whitespace or punctuation.
    raw_name = str(name or "").strip()
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_name)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")
    if not safe_name:
        safe_name = "generated_module"
    if not re.match(r"^[A-Za-z_]", safe_name):
        safe_name = f"generated_{safe_name}"
    return f"{requirement_id}::{kind}.{safe_name}"


def _module_decomposition_markdown(
    requirement: dict[str, Any],
    design_context: dict[str, Any],
    state: DesignState,
    module_id: str,
    output_schema: dict[str, Any],
) -> str:
    """Render only the decision-local context not already encoded by the schema."""

    module = state.modules[module_id]
    allowed_kinds = output_schema["properties"]["modules"]["items"]["properties"]["kind"]["enum"]
    lines = [
        "# Current decomposition decision",
        "",
        "## Requirement",
        "",
        f"- ID: `{_md(requirement.get('requirement_id', ''))}`",
        f"- Name: {_md(requirement.get('name', ''))}",
        f"- Summary: {_md(requirement.get('description', ''))}",
        "",
        "## Parent module",
        "",
        f"- ID: `{_md(module.get('id'))}`",
        f"- Kind: `{_md(module.get('kind'))}`",
        f"- Responsibility: {_md(module.get('spec'))}",
        f"- Allowed direct child kinds: {_inline_list(allowed_kinds)}",
        "",
        "### Available inputs",
        "",
        *_field_table(module.get("inputs", [])),
        "",
        "### Required outputs",
        "",
        *_field_table(module.get("outputs", [])),
        "",
        "### Effects that children may own",
        "",
        *_effect_table(_module_visible_effects(module)),
        "",
        "### Behavioral obligations",
        "",
        *_obligation_table(module.get("obligations", [])),
        "",
        "### Obligation allocation ledger (mandatory)",
        "",
        "- self_obligation_ids plus direct-child obligation_ids must cover every parent obligation id exactly once.",
        "- Copy ids verbatim; do not rename, omit, invent, or duplicate them.",
        "- Put an id in self_obligation_ids when this parent implements it; otherwise put it in exactly one child.",
        "- Negative obligations (prevent/no-write/no-create) are real obligations, not empty behavior.",
        "- PERSISTENCE obligations must continue through a child path to a DB leaf.",
        "",
        "## Relevant database slice",
        "",
    ]
    entities = design_context.get("entities", [])
    if not entities:
        lines.append("- None")
    for entity in entities:
        fields = ", ".join(
            f"{field.get('name')}:{field.get('type')}"
            for field in entity.get("fields", [])
        )
        lines.append(f"- `{_md(entity.get('key'))}`: {_md(fields or 'no fields')}")
    return "\n".join(lines).strip()


def _field_table(fields: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Semantic ID | Name | Type | Required | Description |",
        "|---|---|---|---:|---|",
    ]
    if not fields:
        lines.append("| — | — | — | — | — |")
    for item in fields:
        lines.append(
            f"| {_md(item.get('semantic_id'))} | {_md(item.get('name'))} | {_md(item.get('type'))} "
            f"| {_yes_no(item.get('required'))} | {_md(item.get('description'))} |"
        )
    return lines


def _effect_table(effects: list[dict[str, Any]]) -> list[str]:
    lines = ["| ID | Operation | Target | Fields |", "|---|---|---|---|"]
    if not effects:
        lines.append("| — | — | — | — |")
    for item in effects:
        lines.append(
            f"| {_md(item.get('id'))} | {_md(item.get('operation'))} | {_md(item.get('target'))} "
            f"| {_inline_list(item.get('fields', []))} |"
        )
    return lines


def _obligation_table(obligations: list[dict[str, Any]]) -> list[str]:
    lines = ["| ID | Kind | Description | Scenarios |", "|---|---|---|---|"]
    if not obligations:
        lines.append("| None | None | None | None |")
    for item in obligations:
        lines.append(
            f"| {_md(item.get('id'))} | {_md(item.get('kind'))} | "
            f"{_md(item.get('description'))} | {_inline_list(item.get('scenario_ids', []))} |"
        )
    return lines


def _feedback_markdown(feedback: list[str], *, heading: str = "校验反馈（请修复后重新输出）") -> str:
    return "\n\n## " + heading + "\n\n" + "\n".join(f"- {_md(item)}" for item in feedback)


def _inline_list(values: Iterable[Any]) -> str:
    rendered = [f"`{_md(value)}`" for value in values]
    return ", ".join(rendered) if rendered else "—"


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _md(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _requirement_contract_template(requirement_id: str) -> dict[str, Any]:
    """Return the compact fixed shape shown to the contract model."""

    return {
        "requirement_id": requirement_id,
        "spec": "",
        "server_state": True,
        "inputs": [],
        "outputs": [],
        "effects": [],
        "obligations": [],
    }


def _api_template() -> dict[str, Any]:
    """Return a minimal API decision example without embedding a large schema."""

    return {
        "modules": [{
            "kind": "API",
            "name": "OperationName",
            "spec": "Expose one requirement operation.",
            "inputs": [],
            "outputs": [],
            "effects": [],
            "obligation_ids": [],
            "reuse_decision": "CREATE",
            "reuse_target_module_id": None,
        }],
    }


def _requirement_context(nodes: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    raw = copy.deepcopy(nodes.get(requirement_id, {}))
    return {**raw, "requirement_id": requirement_id}


def _waves(requirement_ir: dict[str, Any], dependency_graph: dict[str, Any]) -> list[list[str]]:
    atomic_ids = {
        str(item)
        for item in requirement_ir.get("atomic_units", [])
        if str(item).strip()
    }
    waves = dependency_graph.get("atomic_implementation_waves")
    if not isinstance(waves, list) or not waves:
        waves = dependency_graph.get("implementation_waves")
    if isinstance(waves, list) and waves:
        return [
            [str(item) for item in wave if str(item) in atomic_ids]
            for wave in waves
            if isinstance(wave, list)
            and any(str(item) in atomic_ids for item in wave)
        ]
    return [[item] for item in sorted(atomic_ids)]


def _dependency_closure(node_id: str, dependencies: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    pending = list(dependencies.get(node_id, []))
    while pending:
        current = str(pending.pop())
        if current in result:
            continue
        result.add(current)
        pending.extend(dependencies.get(current, []))
    return result


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _context_audit(
    *,
    phase: str,
    unit_id: str,
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
        f"CONTEXT_AUDIT phase={phase} unit={unit_id} attempt={attempt} "
        f"context_total_chars={size(model_context)} "
        f"instructions_chars={size(request_payload.get('instructions', ''))} "
        f"input_payload_chars={size(input_payload)} "
        f"output_schema_chars={size(request_payload.get('output_schema', {}))} "
        f"section_chars="
        + ",".join(f"{key}:{value}" for key, value in section_sizes)
    )


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off"}


def design_traceability(design_ir: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Project compact requirement-to-symbol links from the authoritative Design IR."""

    modules_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for module in design_ir.get("modules", []):
        if not isinstance(module, dict):
            continue
        requirement_id = str(module.get("owner_requirement") or "").strip()
        module_id = str(module.get("id") or "").strip()
        if requirement_id and module_id:
            modules_by_requirement.setdefault(requirement_id, []).append(module)
    result: dict[str, dict[str, list[str]]] = {}
    for requirement in design_ir.get("requirements", []):
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("id") or "").strip()
        if not requirement_id:
            continue
        modules = modules_by_requirement.get(requirement_id, [])
        result[requirement_id] = {
            "api_ids": sorted({str(value) for value in requirement.get("api_ids", []) if str(value).strip()}),
            "module_ids": sorted({str(module["id"]) for module in modules}),
        }
    return result


__all__ = [
    "API_DECOMPOSITION_INSTRUCTIONS", "API_DECOMPOSITION_SCHEMA", "MODULE_DECOMPOSITION_INSTRUCTIONS",
    "MODULE_DECOMPOSITION_SCHEMA", "REQUIREMENT_CONTRACT_INSTRUCTIONS", "REQUIREMENT_CONTRACT_SCHEMA",
    "DesignIssue", "DesignPass", "DesignPassResult", "DesignState", "design_traceability",
    "project_design_context",
]
