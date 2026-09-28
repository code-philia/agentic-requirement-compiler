"""Multi-pass thin Frontend Design and projection to compiler runtime seams."""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging import SynchronousLog

from .frontend_ir import FrontendDesignErrorCode, FrontendDesignIssue, schema_shape_errors
from .frontend_thin_ir import FRONTEND_DESIGN_IR_SCHEMA, FRONTEND_IR_SCHEMA_VERSION, repair_shape, shape_errors
from .model_client import StructuredModel, describe_model_error
from .trace_payload import format_payload_trace


THIN_FRONTEND_INSTRUCTIONS = """You are a senior product frontend architect.
Design the product-wide screen architecture from the supplied UI coverage inventory.
Return only screens and routes. Include every user-visible product surface required by the inventory.
Do not assign individual requirements, API ids, navigation targets, or shared-state consumers in this pass.
Later passes design navigation/shared state and place requirements; the compiler derives components,
journeys, API usages, and requirement links.
Do not design layouts, components, JSX, CSS, files, props, events, component trees, or page-local state.
Reference images are evidence for each screen's eventual composition and visual language. Preserve their ids.
Reuse one screen for coverage items that represent the same route and product surface. Screen ids use
PAGE.<PascalName>; shared state ids use STORE.<PascalName>;
Use the same lower-case dot-separated surface key on screens that share a reusable UI surface.
Routes are absolute. Only use supplied visual ids. Return exactly one JSON object and no prose."""

COVERAGE_INVENTORY_INSTRUCTIONS = """You are a senior frontend product analyst.
Build a complete UI coverage inventory from the supplied requirement catalog.
Return exactly one row for every requirement id, including non-atomic requirements.
For each row choose UI_REQUIRED, UI_AFFECTING, or NO_UI and describe the user-visible capability
in a short surface_hint. Do not create pages, routes, components, API ids, or files.
Never omit a requirement. If uncertain, choose UI_AFFECTING rather than NO_UI."""

NAVIGATION_STATE_INSTRUCTIONS = """You are a senior frontend interaction architect.
Connect the already-created screens into a coherent navigation graph and define only genuinely
cross-page shared state. For each state list every consuming screen in consumer_screen_ids
and any shared UI surface keys that directly use it in consumer_surface_keys.
You may reference only supplied screen ids and state ids.
Do not create screens, routes, components, API ids, or files. Return one JSON object and no prose."""

THIN_FRONTEND_SCREEN_DECISION_SCHEMA = copy.deepcopy(
    FRONTEND_DESIGN_IR_SCHEMA["properties"]["screens"]
)
THIN_FRONTEND_SCREEN_DECISION_SCHEMA["items"]["required"] = [
    key
    for key in THIN_FRONTEND_SCREEN_DECISION_SCHEMA["items"]["required"]
    if key not in {"required_api_ids", "requirement_ids"}
]
THIN_FRONTEND_SCREEN_DECISION_SCHEMA["items"]["properties"].pop(
    "required_api_ids"
)
THIN_FRONTEND_SCREEN_DECISION_SCHEMA["items"]["properties"].pop(
    "requirement_ids"
)

THIN_FRONTEND_STATE_DECISION_SCHEMA = copy.deepcopy(
    FRONTEND_DESIGN_IR_SCHEMA["properties"]["shared_state_policies"]
)
THIN_FRONTEND_STATE_DECISION_SCHEMA["items"]["required"] = [
    key
    for key in THIN_FRONTEND_STATE_DECISION_SCHEMA["items"]["required"]
    if key != "requirement_ids"
]
THIN_FRONTEND_STATE_DECISION_SCHEMA["items"]["properties"].pop(
    "requirement_ids"
)

THIN_FRONTEND_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["screens", "shared_state_policies"],
    "properties": {
        "screens": THIN_FRONTEND_SCREEN_DECISION_SCHEMA,
        "shared_state_policies": THIN_FRONTEND_STATE_DECISION_SCHEMA,
    },
}

COVERAGE_ROW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["requirement_id", "ui_scope", "surface_hint"],
    "properties": {
        "requirement_id": {"type": "string", "minLength": 1},
        "ui_scope": {"type": "string", "enum": ["UI_REQUIRED", "UI_AFFECTING", "NO_UI"]},
        "surface_hint": {"type": "string", "minLength": 1},
        "cross_page_state_needs": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1}},
    },
}
COVERAGE_DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["coverage"],
    "properties": {"coverage": {"type": "array", "items": COVERAGE_ROW_SCHEMA}},
}

SCREEN_ARCHITECTURE_SCHEMA = copy.deepcopy(THIN_FRONTEND_SCREEN_DECISION_SCHEMA)
SCREEN_ARCHITECTURE_SCHEMA["items"]["properties"]["surface_keys"] = {
    "type": "array", "maxItems": 32,
    "items": {"type": "string", "minLength": 1},
}
SCREEN_ARCHITECTURE_SCHEMA["items"]["required"].append("surface_keys")
SCREEN_ARCHITECTURE_SCHEMA["items"]["properties"].pop("navigation_targets", None)
SCREEN_ARCHITECTURE_SCHEMA["items"]["required"] = [
    key for key in SCREEN_ARCHITECTURE_SCHEMA["items"]["required"]
    if key != "navigation_targets"
]
SCREEN_ARCHITECTURE_DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["screens"],
    "properties": {"screens": SCREEN_ARCHITECTURE_SCHEMA},
}

NAVIGATION_ROW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["source_screen_id", "trigger", "target_screen_id", "condition"],
    "properties": {
        "source_screen_id": {"type": "string", "minLength": 1},
        "trigger": {"type": "string", "minLength": 1},
        "target_screen_id": {"type": "string", "minLength": 1},
        "condition": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
    },
}
NAVIGATION_STATE_DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["navigation", "shared_state_policies"],
    "properties": {
        "navigation": {"type": "array", "maxItems": 128, "items": NAVIGATION_ROW_SCHEMA},
        "shared_state_policies": THIN_FRONTEND_STATE_DECISION_SCHEMA,
    },
}

@dataclass(slots=True)
class ThinFrontendDesignResult:
    frontend_ir: dict[str, Any]
    node_states: dict[str, str]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ThinFrontendDesignPass:
    """Design global navigation once, then place requirements in bounded calls."""

    def __init__(self, model: StructuredModel, artifact_root: Path) -> None:
        self._model = model
        self._log = SynchronousLog("ThinFrontendDesignPass", workspace_root=artifact_root.resolve().parent)

    def compile(self, requirement_ir: dict[str, Any], dependency_graph: dict[str, Any], backend_design_ir: dict[str, Any], visual_references: list[dict[str, Any]]) -> ThinFrontendDesignResult:
        nodes = requirement_ir.get("nodes", {})
        if not isinstance(nodes, dict):
            nodes = {}
        ordered_ids = [
            str(value)
            for value in requirement_ir.get("node_order", [])
            if str(value) in nodes
        ]
        ordered_ids.extend(sorted(set(str(value) for value in nodes) - set(ordered_ids)))
        requirement_ids = set(ordered_ids)
        api_ids = {
            str(row.get("id"))
            for row in backend_design_ir.get("modules", [])
            if isinstance(row, dict) and row.get("kind") == "API"
        }
        coverage = self._generate_coverage_inventory(
            ordered_ids=ordered_ids,
            nodes=nodes,
            dependency_graph=dependency_graph,
        )
        architecture_payload = {
            "ui_coverage_inventory": coverage,
            "dependency_graph": copy.deepcopy(
                dependency_graph.get("atomic_dependencies", {})
            ),
            "visual_references": [
                {
                    "id": row.get("id"),
                    "source_path": row.get("source_path"),
                    "analysis": row.get("analysis", {}),
                }
                for row in visual_references
                if isinstance(row, dict)
            ],
        }
        architecture = self._generate_architecture(architecture_payload)
        if architecture is None:
            message = "ARC4120 UI_SCOPE_MODEL_FAILED: frontend architecture pass produced no usable decision."
            return ThinFrontendDesignResult(
                {}, {rid: "FAILED" for rid in requirement_ids}, [message]
            )

        navigation_state = self._generate_navigation_and_state(
            screens=architecture.get("screens", []),
            coverage=coverage,
            visual_references=visual_references,
        )
        candidate = {
            "schema_version": FRONTEND_IR_SCHEMA_VERSION,
            "visual_references": copy.deepcopy(visual_references),
            "screen_components": [],
            "placements": [],
            "journeys": [],
            "api_usages": [],
            "requirement_links": [],
            **architecture,
            "shared_state_policies": navigation_state.get("shared_state_policies", []),
        }
        _apply_navigation_targets(candidate, navigation_state.get("navigation", []))
        _normalize_frontend_architecture(
            candidate,
            requirement_ids=requirement_ids,
            backend_api_ids=api_ids,
        )
        _derive_placements_from_coverage(
            candidate,
            ordered_ids=ordered_ids,
            coverage=coverage,
        )
        _assign_unplaced_screens(candidate, coverage)
        _derive_frontend_journeys(candidate, backend_design_ir)
        _complete_requirement_api_dependencies(candidate, backend_design_ir)
        _canonicalize_frontend_associations(candidate, backend_design_ir, requirement_ids)
        issues = validate_thin_frontend_design(
            candidate,
            expected_requirement_ids=requirement_ids,
            backend_api_ids=api_ids,
        )
        if issues:
            return ThinFrontendDesignResult(
                {}, {rid: "FAILED" for rid in requirement_ids},
                [issue.format() for issue in issues],
            )
        canonical = _canonicalize(candidate)
        states = {
            rid: (
                "UI_NOT_REQUIRED"
                if _link(canonical, rid).get("ui_scope") == "NO_UI"
                else "UI_SCOPE_PLANNED"
            )
            for rid in requirement_ids
        }
        return ThinFrontendDesignResult(canonical, states)

    def _generate_coverage_inventory(
        self,
        *,
        ordered_ids: list[str],
        nodes: dict[str, Any],
        dependency_graph: dict[str, Any],
    ) -> list[dict[str, Any]]:
        payload = {
            "requirements": [
                {
                    "requirement_id": rid,
                    **_architecture_requirement_summary(rid, nodes.get(rid, {})),
                }
                for rid in ordered_ids
            ],
            "dependency_graph": copy.deepcopy(dependency_graph.get("atomic_dependencies", {})),
        }
        for attempt in range(1, 4):
            envelope = {
                "instructions": COVERAGE_INVENTORY_INSTRUCTIONS,
                "input_payload": payload,
                "output_schema": _provider_schema(COVERAGE_DECISION_SCHEMA),
            }
            self._log.info(f"MODEL_REQUEST phase=frontend_ui_coverage attempt={attempt}/3")
            self._log.info(_context_audit(phase="frontend_ui_coverage", attempt=attempt, request_payload=envelope))
            try:
                raw = self._model.generate_json(
                    schema_name="frontend_ui_coverage",
                    instructions=COVERAGE_INVENTORY_INSTRUCTIONS,
                    input_payload=payload,
                    output_schema=envelope["output_schema"],
                )
            except Exception as exc:
                self._log.info(f"MODEL_RETRY phase=frontend_ui_coverage error={describe_model_error(exc)}")
                continue
            self._log.info("MODEL_OUTPUT phase=frontend_ui_coverage\n" + format_payload_trace(raw))
            decision = repair_shape(raw, COVERAGE_DECISION_SCHEMA)
            rows = decision.get("coverage", []) if isinstance(decision, dict) else []
            normalized = _normalize_coverage(rows, ordered_ids)
            if len(normalized) == len(ordered_ids):
                return normalized
            payload["validation_feedback"] = [
                f"Coverage must contain every requirement exactly once; expected={len(ordered_ids)} actual={len(normalized)}."
            ]
        self._log.info("MODEL_FALLBACK phase=frontend_ui_coverage decision=deterministic_fallback")
        return _normalize_coverage([], ordered_ids, nodes=nodes)

    def _generate_navigation_and_state(
        self,
        *,
        screens: list[dict[str, Any]],
        coverage: list[dict[str, Any]],
        visual_references: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "screens": copy.deepcopy(screens),
            "ui_coverage_inventory": copy.deepcopy(coverage),
            "shared_state_candidates": [],
            "visual_references": [
                {"id": row.get("id"), "analysis": row.get("analysis", {})}
                for row in visual_references if isinstance(row, dict)
            ],
        }
        for attempt in range(1, 4):
            envelope = {
                "instructions": NAVIGATION_STATE_INSTRUCTIONS,
                "input_payload": payload,
                "output_schema": _provider_schema(NAVIGATION_STATE_DECISION_SCHEMA),
            }
            self._log.info(f"MODEL_REQUEST phase=frontend_navigation_state attempt={attempt}/3")
            self._log.info(_context_audit(phase="frontend_navigation_state", attempt=attempt, request_payload=envelope))
            try:
                raw = self._model.generate_json(
                    schema_name="frontend_navigation_state",
                    instructions=NAVIGATION_STATE_INSTRUCTIONS,
                    input_payload=payload,
                    output_schema=envelope["output_schema"],
                )
            except Exception as exc:
                self._log.info(f"MODEL_RETRY phase=frontend_navigation_state error={describe_model_error(exc)}")
                continue
            self._log.info("MODEL_OUTPUT phase=frontend_navigation_state\n" + format_payload_trace(raw))
            decision = repair_shape(raw, NAVIGATION_STATE_DECISION_SCHEMA)
            if isinstance(decision, dict):
                return _normalize_navigation_state(decision, screens)
        self._log.info("MODEL_FALLBACK phase=frontend_navigation_state decision=empty_graph")
        return {"navigation": [], "shared_state_policies": []}

    def _generate_architecture(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        feedback: list[str] = []
        last_decision: dict[str, Any] | None = None
        for attempt in range(1, 4):
            request = {**payload, **({"validation_feedback": feedback} if feedback else {})}
            envelope = {
                "instructions": THIN_FRONTEND_INSTRUCTIONS,
                "input_payload": request,
                "output_schema": _provider_schema(SCREEN_ARCHITECTURE_DECISION_SCHEMA),
            }
            self._log.info(
                f"MODEL_REQUEST phase=frontend_architecture attempt={attempt}/3"
            )
            self._log.info(
                _context_audit(
                    phase="frontend_architecture",
                    attempt=attempt,
                    request_payload=envelope,
                )
            )
            started = time.perf_counter()
            try:
                raw = self._model.generate_json(
                    schema_name="frontend_architecture",
                    instructions=THIN_FRONTEND_INSTRUCTIONS,
                    input_payload=request,
                    output_schema=envelope["output_schema"],
                )
            except Exception as exc:
                feedback = [f"Frontend architecture call failed: {describe_model_error(exc)}"]
                continue
            self._log.info(
                "MODEL_OUTPUT phase=frontend_architecture "
                f"duration_ms={int((time.perf_counter() - started) * 1000)}\n"
                + format_payload_trace(raw)
            )
            decision = repair_shape(raw, SCREEN_ARCHITECTURE_DECISION_SCHEMA)
            if not isinstance(decision, dict):
                feedback = ["Frontend architecture output must be one JSON object."]
                continue
            last_decision = decision
            feedback = schema_shape_errors(decision, SCREEN_ARCHITECTURE_DECISION_SCHEMA)
            if not feedback:
                return decision
        if last_decision is not None:
            self._log.info(
                "MODEL_FALLBACK phase=frontend_architecture decision=last_model_output "
                f"warnings={feedback}"
            )
        return last_decision



def _context_audit(
    *,
    phase: str,
    attempt: int,
    request_payload: dict[str, Any],
) -> str:
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    input_payload = request_payload.get("input_payload", {})
    section_sizes = sorted(
        ((str(key), size(value)) for key, value in input_payload.items()),
        key=lambda item: item[1],
        reverse=True,
    ) if isinstance(input_payload, dict) else []
    return (
        f"CONTEXT_AUDIT phase={phase} attempt={attempt} "
        f"context_total_chars={size(request_payload)} "
        f"instructions_chars={size(request_payload.get('instructions', ''))} "
        f"input_payload_chars={size(input_payload)} "
        f"output_schema_chars={size(request_payload.get('output_schema', {}))} "
        f"section_chars="
        + ",".join(f"{key}:{value}" for key, value in section_sizes)
    )


def validate_thin_frontend_design(frontend_ir: dict[str, Any], *, expected_requirement_ids: set[str] | None = None, backend_api_ids: set[str] | None = None) -> list[FrontendDesignIssue]:
    """Validate only semantic architecture facts.

    Components, journeys, API usages, and requirement links are compiler
    projections. They are intentionally not cross-validated here; doing so
    would recreate the old multi-table consistency gate and make a derived
    representation able to reject an otherwise usable frontend design.
    """
    errors = shape_errors(frontend_ir)
    if errors:
        return [_issue(FrontendDesignErrorCode.IR_INVALID, message) for message in errors]
    issues: list[FrontendDesignIssue] = []
    for table in ("screens", "shared_state_policies"):
        ids = [str(row["id"]) for row in frontend_ir[table]]
        if len(ids) != len(set(ids)):
            issues.append(_issue(FrontendDesignErrorCode.SYMBOL_DUPLICATE, f"{table} contains duplicate ids."))
    screens = {str(row["id"]): row for row in frontend_ir["screens"]}
    routes: dict[str, str] = {}
    for screen_id, screen in screens.items():
        route = str(screen["route"])
        if route in routes:
            issues.append(_issue(FrontendDesignErrorCode.ROUTE_CONFLICT, f"Route {route!r} is shared by {routes[route]} and {screen_id}."))
        routes[route] = screen_id
    visuals = {str(row["id"]) for row in frontend_ir["visual_references"]}
    placement_keys: list[tuple[str, str | None]] = []
    for placement in frontend_ir["placements"]:
        requirement_id = str(placement["requirement_id"])
        screen_id = placement["screen_id"]
        placement_keys.append((requirement_id, None if screen_id is None else str(screen_id)))
        if expected_requirement_ids is not None and requirement_id not in expected_requirement_ids:
            issues.append(_issue(FrontendDesignErrorCode.REFERENCE_UNKNOWN, f"Placement references unknown requirement {requirement_id}."))
        if screen_id is not None and str(screen_id) not in screens:
            issues.append(_issue(FrontendDesignErrorCode.REFERENCE_UNKNOWN, f"Placement for {requirement_id} references unknown screen {screen_id}."))
        if placement["strategy"] == "USE_SCREEN" and (screen_id is None or placement.get("component_id") is not None):
            issues.append(_issue(FrontendDesignErrorCode.COMPONENT_DECISION_INVALID, f"Placement for {requirement_id} must identify a screen, not a requirement-specific component."))
        if placement["strategy"] == "NO_FRONTEND_IMPLEMENTATION" and (screen_id is not None or placement.get("component_id") is not None):
            issues.append(_issue(FrontendDesignErrorCode.COMPONENT_DECISION_INVALID, f"NO_FRONTEND_IMPLEMENTATION placement for {requirement_id} cannot identify a screen or component."))
    if len(placement_keys) != len(set(placement_keys)):
        issues.append(_issue(FrontendDesignErrorCode.SYMBOL_DUPLICATE, "placements contains duplicate requirement/screen pairs."))
    if expected_requirement_ids is not None:
        placed_requirements = {requirement_id for requirement_id, _ in placement_keys}
        if placed_requirements != expected_requirement_ids:
            issues.append(_issue(FrontendDesignErrorCode.REQUIREMENT_UNCOVERED, f"Placement coverage mismatch: missing={sorted(expected_requirement_ids - placed_requirements)} extra={sorted(placed_requirements - expected_requirement_ids)}."))
    for screen_id, screen in screens.items():
        if not screen["requirement_ids"]:
            issues.append(_issue(FrontendDesignErrorCode.REQUIREMENT_UNCOVERED, f"Screen {screen_id} has no requirement owner."))
        issues.extend(_unknown(screen_id, "visual", screen["visual_reference_ids"], visuals, FrontendDesignErrorCode.REFERENCE_UNKNOWN))
        for navigation in screen["navigation_targets"]:
            target_route = str(navigation["target_route"])
            if target_route != "/" and target_route not in routes:
                issues.append(_issue(FrontendDesignErrorCode.REFERENCE_UNKNOWN, f"Screen {screen_id} navigates to unknown route {target_route}."))
    return issues


def _complete_requirement_api_dependencies(
    frontend_ir: dict[str, Any],
    backend_design_ir: dict[str, Any],
) -> None:
    """Join screen requirements to their owned backend APIs deterministically.

    The model chooses screens and user journeys, but API ownership is already
    fixed by the backend Design IR. Keeping this join in compiler code prevents
    a screen from omitting an API in ``required_api_ids`` while another table
    (such as ``api_usages``) references it.
    """

    owner_by_api: dict[str, str] = {}
    for module in backend_design_ir.get("modules", []):
        if not isinstance(module, dict) or str(module.get("kind", "")) != "API":
            continue
        api_id = str(module.get("id", "")).strip()
        if not api_id:
            continue
        owner_by_api[api_id] = str(module.get("owner_requirement", "")).strip() or api_id.split("::", 1)[0]

    screens = [row for row in frontend_ir.get("screens", []) if isinstance(row, dict)]
    selected_apis: dict[str, set[str]] = {str(row["id"]): set() for row in screens}
    for api_id, owner in owner_by_api.items():
        candidates = [
            screen for screen in screens if owner in screen.get("requirement_ids", [])
        ]
        action = api_id.split("::API.", 1)[-1]
        action_terms = {part.lower() for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", action)}
        scores = {
            str(screen["id"]): len(action_terms & set(re.findall(
                r"[a-z0-9]+", " ".join([
                    str(screen["id"]), *[str(key) for key in screen.get("surface_keys", [])],
                ]).lower(),
            )))
            for screen in candidates
        }
        best = max(scores.values(), default=0)
        for screen in candidates:
            screen_id = str(screen["id"])
            if best == 0 or scores[screen_id] == best:
                selected_apis[screen_id].add(api_id)

    for screen in screens:
        screen["required_api_ids"] = sorted(selected_apis.get(str(screen["id"]), set()))


def _normalize_frontend_architecture(
    frontend_ir: dict[str, Any],
    *,
    requirement_ids: set[str],
    backend_api_ids: set[str],
) -> None:
    """Canonicalize model choices before deriving any repeated relation.

    Screen membership is the only model-authored requirement placement fact.
    Duplicate screens are merged, foreign references are discarded, and
    navigation is limited to routes in the same architecture. Downstream tables
    therefore never need to negotiate inconsistent copies of these relations.
    """

    by_id: dict[str, dict[str, Any]] = {}
    route_owner: dict[str, str] = {}
    screen_aliases: dict[str, str] = {}
    for raw_screen in frontend_ir.get("screens", []):
        if not isinstance(raw_screen, dict):
            continue
        screen = copy.deepcopy(raw_screen)
        screen_id = str(screen.get("id", "")).strip()
        route = str(screen.get("route", "")).strip()
        if not screen_id or not route:
            continue
        canonical_id = route_owner.get(route, screen_id)
        route_owner.setdefault(route, canonical_id)
        screen_aliases[screen_id] = canonical_id
        current = by_id.get(canonical_id)
        if current is None:
            screen["id"] = canonical_id
            by_id[canonical_id] = screen
            current = screen
        for key in (
            "requirement_ids",
            "entry_conditions",
            "observable_states",
            "required_api_ids",
            "visual_reference_ids",
            "surface_keys",
        ):
            current[key] = list(dict.fromkeys([
                *current.get(key, []),
                *screen.get(key, []),
            ]))
        current["navigation_targets"] = list(dict.fromkeys(
            json.dumps(row, ensure_ascii=False, sort_keys=True)
            for row in [
                *current.get("navigation_targets", []),
                *screen.get("navigation_targets", []),
            ]
            if isinstance(row, dict)
        ))
        current["navigation_targets"] = [
            json.loads(value) for value in current["navigation_targets"]
        ]

    known_routes = {str(screen.get("route", "")) for screen in by_id.values()}
    known_visual_ids = {
        str(row.get("id", ""))
        for row in frontend_ir.get("visual_references", [])
        if isinstance(row, dict) and str(row.get("id", ""))
    }
    for screen in by_id.values():
        screen["requirement_ids"] = sorted({
            str(value) for value in screen.get("requirement_ids", [])
            if str(value) in requirement_ids
        })
        screen["required_api_ids"] = sorted({
            str(value) for value in screen.get("required_api_ids", [])
            if str(value) in backend_api_ids
        })
        screen["visual_reference_ids"] = sorted({
            str(value) for value in screen.get("visual_reference_ids", [])
            if str(value) in known_visual_ids
        })
        screen["navigation_targets"] = [
            row for row in screen.get("navigation_targets", [])
            if str(row.get("target_route", "")) == "/"
            or str(row.get("target_route", "")) in known_routes
        ]
    frontend_ir["screens"] = sorted(by_id.values(), key=lambda row: str(row["id"]))

    stores: dict[str, dict[str, Any]] = {}
    for raw_store in frontend_ir.get("shared_state_policies", []):
        if not isinstance(raw_store, dict):
            continue
        store = copy.deepcopy(raw_store)
        store_id = str(store.get("id", "")).strip()
        if not store_id or store_id in stores:
            continue
        store["requirement_ids"] = sorted({
            str(value) for value in store.get("requirement_ids", [])
            if str(value) in requirement_ids
        })
        store["consumer_screen_ids"] = sorted({
            screen_aliases[str(value)] for value in store.get("consumer_screen_ids", [])
            if screen_aliases.get(str(value)) in by_id
        })
        store["consumer_surface_keys"] = sorted({
            str(value) for value in store.get("consumer_surface_keys", [])
            if any(value in screen.get("surface_keys", []) for screen in by_id.values())
        })
        stores[store_id] = store
    frontend_ir["shared_state_policies"] = sorted(
        stores.values(), key=lambda row: str(row["id"])
    )


def _architecture_requirement_summary(
    requirement_id: str,
    requirement: Any,
) -> dict[str, Any]:
    """Keep only product-surface evidence in the global architecture prompt."""

    row = requirement if isinstance(requirement, dict) else {}
    result = {"requirement_id": requirement_id}
    for key in (
        "name",
        "description",
        "type",
        "kind",
        "parent_id",
        "children",
        "acceptance_criteria",
    ):
        if key in row:
            result[key] = copy.deepcopy(row[key])
    return result


def _normalize_coverage(
    rows: Any,
    ordered_ids: list[str],
    *,
    nodes: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        requirement_id = str(row.get("requirement_id", "")).strip()
        if requirement_id not in ordered_ids or requirement_id in by_id:
            continue
        scope = str(row.get("ui_scope", "")).strip()
        if scope not in {"UI_REQUIRED", "UI_AFFECTING", "NO_UI"}:
            scope = "UI_AFFECTING"
        hint = str(row.get("surface_hint", "")).strip()
        by_id[requirement_id] = {
            "requirement_id": requirement_id,
            "ui_scope": scope,
            "surface_hint": hint or requirement_id,
            "cross_page_state_needs": sorted({
                str(value) for value in row.get("cross_page_state_needs", []) if str(value)
            }),
        }
    for requirement_id in ordered_ids:
        if requirement_id in by_id:
            continue
        requirement = (nodes or {}).get(requirement_id, {}) if isinstance(nodes, dict) else {}
        name = str(requirement.get("name", "")).strip() if isinstance(requirement, dict) else ""
        by_id[requirement_id] = {
            "requirement_id": requirement_id,
            "ui_scope": "UI_AFFECTING",
            "surface_hint": name or requirement_id,
            "cross_page_state_needs": [],
        }
    return [by_id[requirement_id] for requirement_id in ordered_ids]


def _normalize_navigation_state(
    decision: dict[str, Any],
    screens: list[dict[str, Any]],
) -> dict[str, Any]:
    screen_ids = {str(row.get("id", "")) for row in screens if isinstance(row, dict)}
    surface_keys = {
        str(key) for screen in screens if isinstance(screen, dict)
        for key in screen.get("surface_keys", [])
    }
    navigation: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in decision.get("navigation", []):
        if not isinstance(row, dict):
            continue
        source = str(row.get("source_screen_id", ""))
        target = str(row.get("target_screen_id", ""))
        trigger = str(row.get("trigger", "")).strip()
        if source not in screen_ids or target not in screen_ids or not trigger:
            continue
        key = (source, trigger, target)
        if key in seen:
            continue
        seen.add(key)
        navigation.append({
            "source_screen_id": source,
            "trigger": trigger,
            "target_screen_id": target,
            "condition": row.get("condition"),
        })
    states: list[dict[str, Any]] = []
    seen_states: set[str] = set()
    for row in decision.get("shared_state_policies", []):
        if not isinstance(row, dict):
            continue
        state_id = str(row.get("id", ""))
        if not state_id or state_id in seen_states:
            continue
        seen_states.add(state_id)
        state = copy.deepcopy(row)
        state["requirement_ids"] = []
        state["consumer_screen_ids"] = sorted({
            str(value) for value in row.get("consumer_screen_ids", [])
            if str(value) in screen_ids
        })
        state["consumer_surface_keys"] = sorted({
            str(value) for value in row.get("consumer_surface_keys", [])
            if str(value) in surface_keys
        })
        states.append(state)
    return {"navigation": navigation, "shared_state_policies": states}


def _apply_navigation_targets(
    frontend_ir: dict[str, Any],
    navigation: list[dict[str, Any]],
) -> None:
    screens = {
        str(row.get("id", "")): row
        for row in frontend_ir.get("screens", [])
        if isinstance(row, dict)
    }
    routes = {screen_id: str(row.get("route", "/")) for screen_id, row in screens.items()}
    for screen in screens.values():
        screen["navigation_targets"] = []
    for row in navigation:
        source = str(row.get("source_screen_id", ""))
        target = str(row.get("target_screen_id", ""))
        if source not in screens or target not in routes:
            continue
        screens[source]["navigation_targets"].append({
            "trigger": str(row.get("trigger", "Navigate")),
            "target_route": routes[target],
            "condition": row.get("condition"),
        })


def _derive_placements_from_coverage(
    frontend_ir: dict[str, Any],
    *,
    ordered_ids: list[str],
    coverage: list[dict[str, Any]],
) -> None:
    """Compute requirement placements from coverage and screen surface keys."""

    screens = [row for row in frontend_ir.get("screens", []) if isinstance(row, dict)]
    coverage_by_id = {
        str(row.get("requirement_id", "")): row
        for row in coverage
        if isinstance(row, dict)
    }
    placements: list[dict[str, Any]] = []
    for requirement_id in ordered_ids:
        item = coverage_by_id.get(requirement_id, {})
        scope = str(item.get("ui_scope", "UI_AFFECTING"))
        hint_tokens = _surface_terms(str(item.get("surface_hint", requirement_id)))
        scores = {
            str(screen["id"]): len(hint_tokens & _surface_terms(" ".join(
                str(value) for value in screen.get("surface_keys", [])
                if not str(value).lower().startswith("site.")
            )))
            for screen in (screens if scope != "NO_UI" else [])
        }
        best_score = max(scores.values(), default=0)
        selected = [
            screen for screen in screens
            if best_score > 0 and scores.get(str(screen["id"])) == best_score
        ]
        if not selected and scope != "NO_UI" and screens:
            selected = [_best_fallback_screen_row(requirement_id, item, screens)]
        if not selected:
            placements.append({
                "requirement_id": requirement_id,
                "ui_scope": "NO_UI" if scope == "NO_UI" else scope,
                "screen_id": None,
                "component_id": None,
                "strategy": "NO_FRONTEND_IMPLEMENTATION",
            })
            continue
        for screen in selected:
            screen_id = str(screen.get("id", ""))
            placements.append({
                "requirement_id": requirement_id,
                "ui_scope": scope,
                "screen_id": screen_id,
                "component_id": None,
                "strategy": "USE_SCREEN",
            })
            screen.setdefault("requirement_ids", []).append(requirement_id)
    for screen in screens:
        screen["requirement_ids"] = sorted(set(str(value) for value in screen.get("requirement_ids", []) if str(value)))
    stores = [row for row in frontend_ir.get("shared_state_policies", []) if isinstance(row, dict)]
    placed_screens: dict[str, set[str]] = {}
    surface_keys_by_screen = {
        str(screen["id"]): set(screen.get("surface_keys", [])) for screen in screens
    }
    for placement in placements:
        if placement["screen_id"] is not None:
            placed_screens.setdefault(str(placement["requirement_id"]), set()).add(str(placement["screen_id"]))
    for store in stores:
        store["requirement_ids"] = []
    for requirement_id in ordered_ids:
        needs = set(re.findall(
            r"[a-z0-9]+",
            " ".join(str(value) for value in coverage_by_id.get(requirement_id, {}).get("cross_page_state_needs", [])),
        ))
        for store in stores:
            store_terms = set(re.findall(
                r"[a-z0-9]+",
                f"{store.get('id', '')} {store.get('purpose', '')}".lower(),
            ))
            if (
                placed_screens.get(requirement_id, set())
                & set(store.get("consumer_screen_ids", []))
                or any(
                    surface_keys_by_screen.get(screen_id, set())
                    & set(store.get("consumer_surface_keys", []))
                    for screen_id in placed_screens.get(requirement_id, set())
                )
                or needs and needs.intersection(store_terms)
            ):
                store["requirement_ids"].append(requirement_id)
        for store in stores:
            store["requirement_ids"] = sorted(set(store["requirement_ids"]))
    frontend_ir["placements"] = placements


def _assign_unplaced_screens(frontend_ir: dict[str, Any], coverage: list[dict[str, Any]]) -> None:
    screens = [row for row in frontend_ir.get("screens", []) if isinstance(row, dict)]
    coverage_by_id = {
        str(row.get("requirement_id", "")): row
        for row in coverage if isinstance(row, dict)
    }
    for screen in screens:
        if screen.get("requirement_ids"):
            continue
        screen_terms = _surface_terms(" ".join(
            str(screen.get(key, "")) for key in ("id", "route", "purpose", "surface_keys")
        )) - {"page", "screen", "site", "app"}
        incoming = [
            source for source in screens
            if source is not screen and any(
                str(link.get("target_route", "")) == str(screen.get("route", ""))
                for link in source.get("navigation_targets", []) if isinstance(link, dict)
            )
        ]
        ranked: list[tuple[int, str]] = []
        for source in incoming:
            source_terms = _surface_terms(" ".join(
                str(source.get(key, "")) for key in ("id", "route", "purpose", "surface_keys")
            )) - {"page", "screen", "site", "app"}
            for requirement_id in source.get("requirement_ids", []):
                if coverage_by_id.get(str(requirement_id), {}).get("ui_scope") == "NO_UI":
                    continue
                ranked.append((len(screen_terms & source_terms), str(requirement_id)))
        if not ranked:
            ranked = [
                (len(screen_terms & _surface_terms(str(row.get("surface_hint", "")))), requirement_id)
                for requirement_id, row in coverage_by_id.items()
                if row.get("ui_scope") != "NO_UI"
            ]
        if not ranked:
            continue
        score, requirement_id = sorted(ranked, key=lambda item: (-item[0], item[1]))[0]
        if score == 0:
            continue
        screen["requirement_ids"] = [requirement_id]
        frontend_ir["placements"] = [
            row for row in frontend_ir["placements"]
            if row.get("requirement_id") != requirement_id or row.get("screen_id") is not None
        ]
        frontend_ir["placements"].append({
            "requirement_id": requirement_id,
            "ui_scope": "UI_AFFECTING",
            "screen_id": str(screen["id"]),
            "component_id": None,
            "strategy": "USE_SCREEN",
        })


def _surface_terms(value: str) -> set[str]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return set(re.findall(r"[a-z0-9]+", separated.lower()))


def _best_fallback_screen_row(
    requirement_id: str,
    coverage: dict[str, Any],
    screens: list[dict[str, Any]],
) -> dict[str, Any]:
    terms = _surface_terms(f"{requirement_id} {coverage.get('surface_hint', '')}")
    return sorted(
        screens,
        key=lambda screen: (
            -len(terms & _surface_terms(" ".join(
                str(screen.get(key, "")) for key in ("id", "route", "purpose", "surface_keys")
            ))),
            str(screen.get("id", "")),
        ),
    )[0]


def _normalize_placement_decision(
    decision: dict[str, Any] | None,
    *,
    requirement_id: str,
    requirement: Any,
    available_screens: list[dict[str, Any]],
    available_states: list[dict[str, Any]],
) -> dict[str, Any]:
    """Constrain a placement answer to supplied candidates with a safe fallback."""

    row = decision if isinstance(decision, dict) else {}
    known_screen_ids = {
        str(screen.get("id", ""))
        for screen in available_screens
        if str(screen.get("id", ""))
    }
    known_state_ids = {
        str(state.get("id", ""))
        for state in available_states
        if str(state.get("id", ""))
    }
    screen_ids = sorted({
        str(value) for value in row.get("screen_ids", [])
        if str(value) in known_screen_ids
    })
    state_ids = sorted({
        str(value) for value in row.get("shared_state_ids", [])
        if str(value) in known_state_ids
    })
    scope = str(row.get("ui_scope", ""))
    if scope not in {"UI_REQUIRED", "UI_AFFECTING", "NO_UI"}:
        scope = "UI_REQUIRED" if screen_ids else "NO_UI"
    if scope == "NO_UI":
        return {
            "ui_scope": "NO_UI",
            "screen_ids": [],
            "shared_state_ids": [],
        }
    if not screen_ids and available_screens:
        screen_ids = [_best_fallback_screen(requirement_id, requirement, available_screens)]
    return {
        "ui_scope": scope,
        "screen_ids": screen_ids,
        "shared_state_ids": state_ids,
    }


def _best_fallback_screen(
    requirement_id: str,
    requirement: Any,
    available_screens: list[dict[str, Any]],
) -> str:
    row = requirement if isinstance(requirement, dict) else {}
    terms = set(re.findall(
        r"[a-z0-9]+",
        " ".join(
            str(value)
            for value in (
                requirement_id,
                row.get("name", ""),
                row.get("description", ""),
            )
        ).lower(),
    ))
    ranked = sorted(
        available_screens,
        key=lambda screen: (
            -len(terms.intersection(set(re.findall(
                r"[a-z0-9]+",
                " ".join(
                    str(screen.get(key, ""))
                    for key in ("id", "route", "purpose")
                ).lower(),
            )))),
            str(screen.get("id", "")),
        ),
    )
    return str(ranked[0].get("id", ""))


def _apply_ui_placement_decisions(
    frontend_ir: dict[str, Any],
    *,
    ordered_ids: list[str],
    decisions: dict[str, dict[str, Any]],
) -> None:
    """Materialize requirement choices into the canonical architecture tables."""

    screens = {
        str(row.get("id", "")): row
        for row in frontend_ir.get("screens", [])
        if isinstance(row, dict) and str(row.get("id", ""))
    }
    stores = {
        str(row.get("id", "")): row
        for row in frontend_ir.get("shared_state_policies", [])
        if isinstance(row, dict) and str(row.get("id", ""))
    }
    for screen in screens.values():
        screen["requirement_ids"] = []
    for store in stores.values():
        store["requirement_ids"] = []

    placements: list[dict[str, Any]] = []
    for requirement_id in ordered_ids:
        decision = decisions.get(requirement_id, {
            "ui_scope": "NO_UI",
            "screen_ids": [],
            "shared_state_ids": [],
        })
        scope = str(decision.get("ui_scope", "NO_UI"))
        screen_ids = [
            str(value) for value in decision.get("screen_ids", [])
            if str(value) in screens
        ]
        for store_id in decision.get("shared_state_ids", []):
            value = str(store_id)
            if value in stores and requirement_id not in stores[value]["requirement_ids"]:
                stores[value]["requirement_ids"].append(requirement_id)
        if not screen_ids:
            placements.append({
                "requirement_id": requirement_id,
                "ui_scope": scope,
                "screen_id": None,
                "component_id": None,
                "strategy": "NO_FRONTEND_IMPLEMENTATION",
            })
            continue
        for screen_id in screen_ids:
            if requirement_id not in screens[screen_id]["requirement_ids"]:
                screens[screen_id]["requirement_ids"].append(requirement_id)
            placements.append({
                "requirement_id": requirement_id,
                "ui_scope": scope,
                "screen_id": screen_id,
                "component_id": None,
                "strategy": "USE_SCREEN",
            })
    for screen in screens.values():
        screen["requirement_ids"] = sorted(screen["requirement_ids"])
    for store in stores.values():
        store["requirement_ids"] = sorted(store["requirement_ids"])
    frontend_ir["placements"] = placements


def _derive_frontend_journeys(
    frontend_ir: dict[str, Any],
    backend_design_ir: dict[str, Any],
) -> None:
    """Derive navigation journeys instead of asking the model to duplicate them."""

    apis_by_requirement: dict[str, list[str]] = {}
    for module in backend_design_ir.get("modules", []):
        if not isinstance(module, dict) or str(module.get("kind", "")) != "API":
            continue
        api_id = str(module.get("id", "")).strip()
        if api_id:
            owner = str(module.get("owner_requirement", "")).strip() or api_id.split("::", 1)[0]
            apis_by_requirement.setdefault(owner, []).append(api_id)
    journeys: list[dict[str, Any]] = []
    for screen in frontend_ir.get("screens", []):
        if not isinstance(screen, dict):
            continue
        screen_id = str(screen.get("id", ""))
        requirement_ids = sorted({str(value) for value in screen.get("requirement_ids", []) if str(value)})
        requirement_id = requirement_ids[0] if requirement_ids else screen_id
        owned_apis = sorted(apis_by_requirement.get(requirement_id, []))
        for index, navigation in enumerate(screen.get("navigation_targets", []), 1):
            if not isinstance(navigation, dict):
                continue
            target_route = str(navigation.get("target_route", ""))
            if not target_route:
                continue
            journeys.append({
                "id": f"JOURNEY.{_component_token(screen_id)}{index}",
                "requirement_id": requirement_id,
                "source_screen_id": screen_id,
                "trigger": str(navigation.get("trigger", "Navigate")),
                "api_id": owned_apis[0] if len(owned_apis) == 1 else None,
                "success_target_route": target_route,
                "failure_behavior": str(navigation.get("condition") or "Remain on the current screen."),
            })
    frontend_ir["journeys"] = journeys


def _canonicalize_frontend_associations(
    frontend_ir: dict[str, Any],
    backend_design_ir: dict[str, Any],
    requirement_ids: set[str],
) -> None:
    """Normalize all repeated frontend association tables from one join.

    API bindings remain model-authored, but their screen/API keys, missing
    usage rows, and requirement links are compiler-owned derived relations.
    Invalid model references are discarded before validation instead of being
    allowed to poison every later frontend stage.
    """

    known_api_ids = {
        str(module.get("id", ""))
        for module in backend_design_ir.get("modules", [])
        if isinstance(module, dict)
        and str(module.get("kind", "")) == "API"
        and str(module.get("id", "")).strip()
    }
    screens = {
        str(screen.get("id", "")): screen
        for screen in frontend_ir.get("screens", [])
        if isinstance(screen, dict) and str(screen.get("id", "")).strip()
    }
    screen_api_ids: dict[str, set[str]] = {
        screen_id: {
            str(value)
            for value in screen.get("required_api_ids", [])
            if str(value) in known_api_ids
        }
        for screen_id, screen in screens.items()
    }

    # Keep only valid usage rows and merge duplicate rows without losing
    # bindings. Missing usage rows are generated for every screen/API pair.
    usages: dict[tuple[str, str], dict[str, Any]] = {}
    for usage in frontend_ir.get("api_usages", []):
        if not isinstance(usage, dict):
            continue
        key = (str(usage.get("screen_id", "")), str(usage.get("api_id", "")))
        if key[0] not in screens or key[1] not in screen_api_ids.get(key[0], set()):
            continue
        current = usages.setdefault(
            key,
            {
                "screen_id": key[0],
                "api_id": key[1],
                "request_bindings": [],
                "response_bindings": [],
            },
        )
        for field in ("request_bindings", "response_bindings"):
            for binding in usage.get(field, []):
                if binding not in current[field]:
                    current[field].append(copy.deepcopy(binding))
    for screen_id, api_ids in screen_api_ids.items():
        for api_id in api_ids:
            usages.setdefault(
                (screen_id, api_id),
                {
                    "screen_id": screen_id,
                    "api_id": api_id,
                    "request_bindings": [],
                    "response_bindings": [],
                },
            )
    frontend_ir["api_usages"] = sorted(
        usages.values(), key=lambda row: (str(row["screen_id"]), str(row["api_id"]))
    )

    for journey in frontend_ir.get("journeys", []):
        if not isinstance(journey, dict):
            continue
        current_api = str(journey.get("api_id") or "")
        screen_apis = screen_api_ids.get(str(journey.get("source_screen_id", "")), set())
        trigger_terms = set(re.findall(r"[a-z0-9]+", str(journey.get("trigger", "")).lower()))
        matching_apis = [
            api_id for api_id in screen_apis
            if trigger_terms & {part.lower() for part in re.findall(
                r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", api_id.split("::API.", 1)[-1]
            )}
        ]
        journey["api_id"] = (
            current_api if current_api in matching_apis
            else matching_apis[0] if len(matching_apis) == 1 else None
        )

    existing_links = {
        str(row.get("requirement_id", "")): row
        for row in frontend_ir.get("requirement_links", [])
        if isinstance(row, dict)
    }
    links: list[dict[str, Any]] = []
    placed_screens: dict[str, set[str]] = {}
    placement_scope: dict[str, str] = {}
    for placement in frontend_ir.get("placements", []):
        if not isinstance(placement, dict):
            continue
        requirement_id = str(placement.get("requirement_id", ""))
        screen_id = placement.get("screen_id")
        placement_scope[requirement_id] = str(placement.get("ui_scope", "NO_UI"))
        if screen_id is not None:
            placed_screens.setdefault(requirement_id, set()).add(str(screen_id))
    for requirement_id in sorted(requirement_ids):
        old = existing_links.get(requirement_id, {})
        linked_screens = sorted(placed_screens.get(requirement_id, set()))
        ui_scope = placement_scope.get(requirement_id, "NO_UI")
        if ui_scope == "UI_REQUIRED" and not linked_screens:
            ui_scope = "UI_AFFECTING"
        linked_state_ids = {
            str(value) for value in old.get("shared_state_ids", [])
        }
        linked_state_ids.update(
            str(store.get("id", ""))
            for store in frontend_ir.get("shared_state_policies", [])
            if isinstance(store, dict)
            and requirement_id in {str(value) for value in store.get("requirement_ids", [])}
        )
        linked_visual_ids = {
            str(value) for value in old.get("visual_reference_ids", [])
        }
        linked_visual_ids.update(
            str(value)
            for screen_id in linked_screens
            for value in screens[screen_id].get("visual_reference_ids", [])
        )
        links.append({
            "requirement_id": requirement_id,
            "ui_scope": ui_scope,
            "screen_ids": linked_screens,
            "shared_state_ids": sorted({
                value for value in linked_state_ids
                if any(str(store.get("id", "")) == value for store in frontend_ir.get("shared_state_policies", []) if isinstance(store, dict))
            }),
            "visual_reference_ids": sorted({
                value for value in linked_visual_ids
                if any(str(visual.get("id", "")) == value for visual in frontend_ir.get("visual_references", []) if isinstance(visual, dict))
            }),
        })
    frontend_ir["requirement_links"] = links




def _component_token(value: str) -> str:
    text = re.sub(r'[^A-Za-z0-9]+', ' ', str(value)).strip()
    return ''.join(part[:1].upper() + part[1:] for part in text.split()) or 'Feature'


def project_frontend_runtime_ir(frontend_ir: dict[str, Any]) -> dict[str, Any]:
    """Lower one page per route and one shared component per repeated surface."""
    state_consumers = {
        str(row["id"]): set(row.get("consumer_screen_ids", []))
        for row in frontend_ir.get("shared_state_policies", [])
    }
    screens = frontend_ir.get("screens", [])
    surface_screens: dict[str, list[dict[str, Any]]] = {}
    for screen in screens:
        for surface_key in set(screen.get("surface_keys", [])):
            surface_screens.setdefault(str(surface_key), []).append(screen)
    shared_by_screen: dict[str, list[str]] = {}
    shared_components: list[dict[str, Any]] = []
    component_ids = {
        str(row.get("id", "")) for row in frontend_ir.get("screen_components", [])
        if isinstance(row, dict)
    }
    for surface_key, owners in sorted(surface_screens.items()):
        if len(owners) < 2:
            continue
        base_id = f"COMPONENT.{_component_token(surface_key)}"
        component_id = base_id
        suffix = 2
        while component_id in component_ids:
            component_id = f"{base_id}{suffix}"
            suffix += 1
        component_ids.add(component_id)
        requirements = sorted({rid for screen in owners for rid in screen["requirement_ids"]})
        content_slot = surface_key.endswith(".form")
        shared_components.append({
            "id": component_id,
            "spec": (
                f"Shared {surface_key} form presentation. Render children as the sole form "
                "content; the parent page owns fields, submission, and validation."
                if content_slot else f"Shared {surface_key} surface; render its reusable UI here, not again in each page."
            ),
            "composition_mode": "CONTENT_SLOT" if content_slot else "SELF_CONTAINED",
            "inputs": [],
            "scope": "SHARED", "owner_page_id": None, "owner_layout_id": None,
            "events": [], "requirement_ids": requirements, "layout_id": None,
            "component_ids": [], "api_dependencies": [], "store_dependencies": [
                str(store["id"]) for store in frontend_ir.get("shared_state_policies", [])
                if surface_key in store.get("consumer_surface_keys", [])
            ],
            "render_obligations": [], "visual_reference_ids": sorted({
                visual_id for screen in owners for visual_id in screen["visual_reference_ids"]
            }),
        })
        for screen in owners:
            shared_by_screen.setdefault(str(screen["id"]), []).append(component_id)
    components_by_screen: dict[str, list[dict[str, Any]]] = {}
    for row in frontend_ir.get("screen_components", []):
        if isinstance(row, dict):
            components_by_screen.setdefault(str(row.get("screen_id", "")), []).append(row)
    component_owner: dict[str, str] = {}
    components: list[dict[str, Any]] = shared_components[:]
    pages = []
    for screen in frontend_ir.get("screens", []):
        owned = sorted(components_by_screen.get(str(screen["id"]), []), key=lambda row: str(row["id"]))
        for component in owned:
            for api_id in component.get("required_api_ids", []):
                component_owner[f"{screen['id']}::{api_id}"] = str(component["id"])
            components.append({
                "id": component["id"], "spec": component["purpose"], "inputs": copy.deepcopy(component["inputs"]),
                "scope": "PAGE", "owner_page_id": str(screen["id"]), "owner_layout_id": None,
                "events": [], "requirement_ids": copy.deepcopy(component["requirement_ids"]), "layout_id": None,
                "component_ids": [], "api_dependencies": copy.deepcopy(component["required_api_ids"]),
                "store_dependencies": copy.deepcopy(component["shared_state_ids"]),
                "render_obligations": _render_obligations(component["observable_states"]),
                "visual_reference_ids": copy.deepcopy(component["visual_reference_ids"]),
            })
        pages.append({
            "id": screen["id"], "spec": screen["purpose"], "route": screen["route"], "route_inputs": copy.deepcopy(screen["route_inputs"]),
            "requirement_ids": copy.deepcopy(screen["requirement_ids"]),
            "layout_id": None, "component_ids": [*shared_by_screen.get(str(screen["id"]), []), *[str(row["id"]) for row in owned]],
            "api_dependencies": copy.deepcopy(screen["required_api_ids"]),
            "store_dependencies": [
                state_id for state_id, consumers in state_consumers.items()
                if screen["id"] in consumers
            ],
            "render_obligations": _render_obligations(screen["observable_states"]),
            "navigation": [{"trigger": row["trigger"], "target": row["target_route"], "target_route": row["target_route"], "condition": row["condition"]} for row in screen["navigation_targets"]],
            "visual_reference_ids": copy.deepcopy(screen["visual_reference_ids"]),
        })
    stores = [{"id": row["id"], "spec": row["purpose"], "state": copy.deepcopy(row["state"]), "actions": copy.deepcopy(row["actions"]), "persistence": copy.deepcopy(row["persistence"]), "requirement_ids": copy.deepcopy(row["requirement_ids"])} for row in frontend_ir.get("shared_state_policies", [])]
    dependencies = [{"consumer_id": component_owner.get(f"{row['screen_id']}::{row['api_id']}", row["screen_id"]), "api_id": row["api_id"], "bindings": copy.deepcopy(row["request_bindings"] + row["response_bindings"])} for row in frontend_ir.get("api_usages", [])]
    owned_components = _components_by_requirement(frontend_ir)
    links = [{
        "requirement_id": row["requirement_id"], "ui_scope": row["ui_scope"],
        "symbol_ids": sorted(set(owned_components.get(str(row["requirement_id"]), [])) | set(row["screen_ids"]) | set(row["shared_state_ids"]) | {
            component_id for screen_id in row["screen_ids"]
            for component_id in shared_by_screen.get(str(screen_id), [])
        }),
        "visual_reference_ids": copy.deepcopy(row["visual_reference_ids"]),
    } for row in frontend_ir.get("requirement_links", [])]
    return {"schema_version": 2, "visual_references": copy.deepcopy(frontend_ir.get("visual_references", [])), "layouts": [], "pages": pages, "components": components, "stores": stores, "api_dependencies": dependencies, "requirement_links": links}


def _render_obligations(observable_states: list[Any]) -> list[dict[str, Any]]:
    return [{"id": f"state_{index}", "kind": "REGION", "label": str(value), "semantic_id": None, "required": True} for index, value in enumerate(list(observable_states)[:12], 1)]


def _components_by_requirement(frontend_ir: dict[str, Any]) -> dict[str, list[str]]:
    """Map each requirement to the components that own it, if any exist."""

    owners: dict[str, list[str]] = {}
    for row in frontend_ir.get("screen_components", []):
        if not isinstance(row, dict):
            continue
        for requirement_id in row.get("requirement_ids", []):
            owners.setdefault(str(requirement_id), []).append(str(row.get("id", "")))
    return {key: sorted(set(value)) for key, value in owners.items()}


def frontend_design_traceability(frontend_ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    owned_components = _components_by_requirement(frontend_ir)
    shared_components = project_frontend_runtime_ir(frontend_ir)["components"]
    return {str(row["requirement_id"]): {"ui_scope": row["ui_scope"], "layout_ids": [], "component_ids": sorted(set(owned_components.get(str(row["requirement_id"]), [])) | {
        str(component["id"]) for component in shared_components
        if component["scope"] == "SHARED" and row["requirement_id"] in component["requirement_ids"]
    }), "page_ids": copy.deepcopy(row["screen_ids"]), "store_ids": copy.deepcopy(row["shared_state_ids"]), "visual_reference_ids": copy.deepcopy(row["visual_reference_ids"])} for row in frontend_ir.get("requirement_links", [])}


def _canonicalize(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    for table in ("screens", "journeys", "shared_state_policies", "screen_components"):
        if table in result:
            result[table] = sorted(result[table], key=lambda row: str(row["id"]))
    result["placements"] = sorted(
        result.get("placements", []),
        key=lambda row: (
            str(row.get("requirement_id", "")),
            str(row.get("screen_id") or ""),
        ),
    )
    result["api_usages"] = sorted(result["api_usages"], key=lambda row: (row["screen_id"], row["api_id"]))
    result["requirement_links"] = sorted(result["requirement_links"], key=lambda row: row["requirement_id"])
    return result


def _link(frontend_ir: dict[str, Any], rid: str) -> dict[str, Any]:
    return next((row for row in frontend_ir["requirement_links"] if row["requirement_id"] == rid), {})


def _unknown(owner: str, kind: str, values: list[Any], allowed: set[str], code: FrontendDesignErrorCode) -> list[FrontendDesignIssue]:
    return [_issue(code, f"{owner} references unknown {kind} {value}.") for value in values if str(value) not in allowed]


def _issue(code: FrontendDesignErrorCode, message: str) -> FrontendDesignIssue:
    return FrontendDesignIssue(code, message, "THIN_FRONTEND_DESIGN", "FRONTEND")


def _provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    unsupported = {"maxLength", "uniqueItems"}
    return {key: _provider_schema(value) if isinstance(value, dict) else [_provider_schema(row) if isinstance(row, dict) else row for row in value] if isinstance(value, list) else value for key, value in schema.items() if key not in unsupported}
