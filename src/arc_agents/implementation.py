from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .base import CONTEXT_SEGMENTS_KEY, BaseStructuredAgent, JsonModel
from .contracts import ProposedEdit, ProposedPatch


IMPLEMENTATION_AGENT_SCHEMA_VERSION = 1
ACTIONABLE_FAILURE_CLASSES = {
    "TYPE_CONTRACT",
    "IMPLEMENTATION_BEHAVIOR",
    "VISUAL_BEHAVIOR",
}


IMPLEMENTATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edits"],
    "properties": {
        "edits": {
            "type": "array",
            "minItems": 0,
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "search", "replacement"],
                "properties": {
                    "file": {"type": "string", "minLength": 1},
                    "search": {"type": "string", "minLength": 1, "maxLength": 80000},
                    "replacement": {
                        "type": "string",
                    },
                },
            },
        },
    },
}

FRONTEND_IMPLEMENTATION_OUTPUT_SCHEMA = copy.deepcopy(IMPLEMENTATION_OUTPUT_SCHEMA)
# Frontend JSX/TSX regions are substantially larger and more coupled than
# backend fragments. Keep a bounded number of exact edits per response while
# allowing multiple non-overlapping edits in the same source file.
FRONTEND_IMPLEMENTATION_OUTPUT_SCHEMA["properties"]["edits"]["maxItems"] = 8


_PROJECT_CONVENTIONS: dict[str, Any] = {
    "frontend_styling": "Tailwind CSS v4 via @tailwindcss/vite",
    "frontend_css_entry": "frontend/src/index.css",
    "frontend_edits_use_static_utility_classes": True,
    "reference_images_are_visual_guidance_not_content_fixtures": True,
    "backend_runtime": {
        "already_imported_in_every_backend_module": [
            "newId",
            "now",
            "nowIso",
            "HttpError",
            "NotImplementedError",
        ],
        "identifiers": "Mint every new identifier with newId(prefix?) from runtime/ids; never hand-roll uuids, counters, or Date.now-based ids.",
        "clock": "Read the current time with now() (Date) or nowIso() (ISO-8601 string) from runtime/clock; never call new Date() or Date.now() directly.",
        "errors": "Signal an expected failure by throwing HttpError.badRequest/unauthorized/forbidden/notFound/conflict/unprocessable(message, code?, details?). The compiler already wraps every API region in try/catch and serializes the response as {\"error\":{code,message,details?}}; never build that envelope, set a status code, or call res.json for a failure.",
        "drizzle_operators": "and, or, eq, ne, gt, gte, lt, lte, like, inArray, isNull, isNotNull, asc, desc, and sql are already imported in every DB module.",
    },
    "frontend_runtime": {
        "already_imported_in_every_page_and_component": [
            "useState",
            "useEffect",
            "useMemo",
            "useRef",
            "useCallback",
            "useNavigate",
        ],
        "stores": "A Store is created by the compiler through createStore(initialState, ({getState, setState}) => actions) and persisted by persistStore. Inside a Store region, return only the actions object and mutate through setState. Read a Store from a Page or Component with useStoreState(store) for the whole state or useStore(store, select) for a slice; never mutate store state directly and never call its subscribe/getState from render.",
        "navigation": "Navigate with the injected useNavigate() hook; never assign window.location.",
        "screen_composition": "A partitioned Page owns no requirement: its region only composes the Components listed in its child props and forwards route inputs. Every behavior, API call, and store read for a requirement lives in the Component that owns it, and that Component is self-sufficient — it fetches its own data and reads its own stores instead of expecting the Page to pass them down.",
        "api_errors": "requestJson throws ApiClientError with status, code, and details when a route fails. Catch it to render user-facing error states instead of inspecting raw responses.",
    },
}


IMPLEMENTATION_INSTRUCTIONS = """You are a senior software engineer specializing in bounded,
test-driven backend implementation and cross-layer defect repair.
Implement the smallest coherent code change for the supplied requirement and implementation mode.

Mode semantics:
- UNIT_REPAIR fixes a failing unit test using backend business source.
- INTEGRATION_REPAIR fixes a failing integration test using the complete backend surface.
- E2E_BACKEND_REPAIR fixes server/API/DB evidence from an end-to-end failure.
- E2E_FRONTEND_REPAIR fixes browser/UI evidence from an end-to-end failure.
- The first TDD call implements the supplied target before any test feedback exists.
- AGGREGATE handles non-atomic frontend composition without creating new compiler glue.

When no failure report is supplied, implement the supplied target from its requirement, related
module context, and writable source files. Then the TDD loop runs tests. Later calls receive test
results and use that evidence to make the smallest coherent fix. Every request must return at least
one exact edit.

Context layout:
- Context arrives as two user messages. The first, "stable_project_context", holds policy, project conventions,
  design evidence, and writable_targets (each with its complete source). The second, "current_task",
  holds the requirement, contract, and the ids you may edit. After tests run it also holds the
  failure cluster and failed-test analysis/source.
- The second message is the task. Read the first for facts, then satisfy the second.

Authority and evidence:
- Required behavior comes from requirement, scenarios, requirement_contract, and the failed-test
  source/analysis sections when present.
- Module behavior and reference-derived frontend layout/style evidence may come from design_context.
- Failure localization comes from the compiler's failure cluster; for Playwright
  the readable failure_analysis text is an evidence-first execution record. It
  contains the exact failed error, completed test status, ordered pw:api steps,
  the last observable action before a timeout, and the relevant frozen test
  source. Do not expect a second natural-language diagnosis or infer an
  assertion failure when the record says RUN_TIMEOUT or TEST_RUN_TIMEOUT.
- Real files, symbols, call edges, and complete source text come from
  `writable_targets` (the module cards). Each card contains `module_id`, `file`, and `source`.
- Read-only dependency interfaces are included only when needed by design evidence; they must never be edited.
- Scope warnings are authoritative routing signals. If a warning identifies a
  compiler-owned, frozen-test, contract, infrastructure, or dependency issue,
  do not guess around it or edit outside the allowed writable files.
- Each writable_targets entry contains its complete source, so use that field as the source of truth.
  To construct an edit, first select a writable target module card, copy its `file` value exactly
  as the relative output path, then copy an exact old fragment from that card's `source` into
  `search`. Do not infer paths from module_id and do not look for a separate source-file section.
- For a repair, the writable surface contains the localized failing modules plus their direct callers/callees.
  First TDD and aggregate calls contain only their explicitly requested target. Diagnose within this supplied
  surface and do not rewrite unrelated siblings merely because they share a requirement.
- For TYPECHECK/TYPE_CONTRACT failures, inspect every source file and line/column
  listed in failure_analysis. Do not assume the first or test-local file is the
  whole problem; type errors commonly span several callers and callees.

Hard scope rules:
- Return replacements only for files represented by the supplied writable target module cards.
- Each edit must include a short, exact `search` fragment copied from the supplied source and a
  `replacement` containing only its replacement text. The search fragment must be unique in the
  selected writable source file. Exact file-level matching is the authority.
- The `edits` list is file-based. A relative file path may appear more than once, and this is
  intentional: use one row per exact search/replacement pair. Never merge unrelated edits merely
  because they share a file. Do not return module_id in the output.
- Do not modify tests, assertions, signatures, routes, generated types, configs, or compiler glue.
  Imports and exports may be changed when they are part of a supplied writable
  file and are covered by an exact search/replacement edit.
- Use only symbols already available in the supplied source file and public read-only interfaces.
- Do not invent files, modules, APIs, fields, routes, database tables, or requirement behavior.
- In TDD mode, do not weaken or work around frozen tests. In AGGREGATE mode, no frozen tests exist;
  requirement, design_context, and Code Binding ownership are authoritative.
- Do not mock requirement-owned modules. Mocking external systems is not part of this implementation patch.

Implementation rules:
- Address the supplied failure cluster, or the supplied aggregate scope in AGGREGATE mode, not future requirements.
- Prefer the smallest coherent vertical change and avoid speculative refactoring.
- Preserve async behavior, TypeScript types, observable UI behavior, and declared call edges.
- In a DB module, use the compiler-injected `database` Drizzle client together with the imported schema table symbols.
  READ/CREATE/UPDATE/DELETE effects must execute through database.select/insert/update/delete respectively. A table
  symbol is schema metadata, not a repository: never inspect rows, data, items, or arbitrary properties on it.
- Use the injected Drizzle operators (`eq`, `ne`, `and`, `or`, `gt`, `gte`, `lt`, `lte`, `like`, `inArray`, `isNull`,
  `isNotNull`, `asc`, `desc`, `sql`) for predicates, ordering, and aggregates; do not emulate filtering, sorting, or
  counting in TypeScript after loading an entire table when the database can express it.
- Never create another SQLite/Drizzle connection, replace persistence with a module-level array or object, or report a
  successful database write after merely constructing an id or return value. The injected client is the sole owner of
  the connection, including when DATABASE_URL is `:memory:` during tests.
- A backend skeleton calls recordStubHit immediately before its placeholder response/error. A real DB/FUNC/API
  implementation must remove that call together with the placeholder; leaving it behind falsely reports the module
  as unimplemented and will keep Integration/E2E red even when the remaining code appears functional.
- Seed fixtures are compiler-owned test setup. Never hard-code fixture records or `Seed data:` literals in a DB/FUNC/API
  implementation, and never make a read repository insert, synthesize, or return missing fixture rows. Tests must apply
  requirement.seed_fixtures through the compiler-owned seeding support before exercising application behavior.
- In an HTTP handler, map only expected requirement-level validation and conflict failures to 4xx responses by throwing
  the injected `HttpError` (badRequest/unauthorized/forbidden/notFound/conflict/unprocessable). The compiler owns the
  surrounding try/catch and the `{"error":{code,message}}` envelope, so never assemble an error body, set a status code,
  or send a failure response yourself. Let unexpected database, runtime, and compiler-glue errors propagate so the global
  error handler and test diagnostics preserve the real root cause; never disguise every exception as invalid user input.
- Use the compiler-owned runtime instead of re-implementing it: `newId()` for identifiers and `now()`/`nowIso()` for the
  current time in backend modules, `useNavigate()` for frontend navigation, and the Store's `setState`/`useStoreState`/
  `useStore` for shared state. These symbols are already imported; see project_conventions for the exact contract.
- Every frontend Page, Layout, or Component edit must be a finished, responsive UI implementation, even when the
  current failure is classified as functional rather than visual. Do not stop at unstyled semantic markup.
- A generated UI skeleton may contain data-arc-obligation placeholders, but your replacement must remove those
  placeholders and render real semantic controls/content. A page that only returns labels, spans, or an empty shell is
  incomplete and must not be proposed as a successful implementation.
- The frontend styling system is Tailwind CSS v4 through @tailwindcss/vite. Use static Tailwind utility className
  strings inside the target function body. Do not invent undefined semantic class names, add style tags, or use inline
  style objects when a Tailwind utility can express the design.
- Preserve the existing function signature and place local state, effects, handlers, and JSX inside
  the supplied function body. Do not emit another function declaration unless the exact searched
  fragment already includes that declaration.
- Page and Component implementation bodies receive compiler-wired references through `_dependencies`. Destructure and
  use its hooks, API clients, and runtime Stores; their business call parameters are intentionally left for this
  implementation step. Do not bypass an available client with ad-hoc fetch calls or create a second persistence store.
- Implement navigation using the exact design_context.frontend.screens[].navigation_targets[].target_route values. A Home or
  main-interface transition targets the compiler-owned system route `/`; after a successful action, navigate there.
- Store reload behavior is defined by design_context.frontend.shared_state_policies[].persistence. Keep transient state in MEMORY and
  implement reload-surviving state through the supplied LOCAL_STORAGE runtime Store and storage_key.
- For a Store target, replace only its runtime implementation body: return the Actions object built from the supplied
  `setState`/`getState` helpers. Preserve its State, Actions, and Value interfaces, the createStore call, and the
  compiler-owned persistence boundary around that region.
- Translate design_context into an internally coherent visual direction: content hierarchy, page composition,
  responsive containers, spacing rhythm, typography scale, palette, borders, surfaces, states, and one restrained
  signature detail appropriate to the product. Keep the direction consistent across all supplied frontend modules.
- Visual references are guidance for layout, style, and content hierarchy, not source data. Preserve requirement-owned
  labels, behavior, and records; never copy unrelated image content. If no reference is supplied, derive a deliberate
  domain-appropriate direction from the requirement instead of using a generic demo-page aesthetic.
- Preserve accessibility: semantic controls, associated labels, visible keyboard focus, sufficient contrast, and
  reduced-motion-safe behavior. Ensure navigation between declared pages remains discoverable and coherent.
- Treat screens and their navigation targets as one connected product graph. Keep navigation, shared visual language,
  compiler-derived API ownership, and cross-page state coherent across every supplied screen; choose layout and internal
  component decomposition here. Do not recreate requirement links or API association tables.
- If several supplied failures are consequences of the same root cause, fix that root cause once.
- Prefer the smallest coherent search/replacement pair and preserve surrounding source.

Return exactly one JSON object and no prose:
{"edits":[{"file":"frontend/src/path/File.tsx","search":"unique old source fragment","replacement":"new source fragment"}]}
"""


FRONTEND_IMPLEMENTATION_INSTRUCTIONS = """You are a senior frontend product engineer and UI implementation specialist.
Implement the current requirement's frontend experience as a coherent, runnable UI.

Mode semantics:
- The first TDD call builds the supplied frontend target before test feedback exists.
- E2E_FRONTEND_REPAIR fixes direct browser/UI evidence while preserving declared routes and API clients.
- AGGREGATE completes non-atomic page, layout, navigation, and shared-state composition.

When no failure report is supplied, implement the supplied frontend target from the
requirement, placement/design context, API client contract, and complete current source. There is
no Playwright failure yet. Do not invent additional pages or files; return exact edits.

Use the supplied requirement and requirement_contract for behavior, and use design_context
and its visual references for layout, hierarchy, styling, responsive composition, and content
direction. The frontend target modules and their complete source are in writable_targets.
The current failure analysis is evidence about what is broken; preserve exact observed facts
and do not weaken frozen tests.

Frontend priorities:
- Implement the requirement-owned Page, Component, Layout, and Store modules as one connected flow.
- Wire the injected API client and Store dependencies; do not use ad-hoc fetch, a second store,
  window.location, or invented routes.
- Use the exact routes, controls, labels, API symbols, Store actions, and target modules supplied
  by design_context and writable_targets.
- Build a finished responsive interface with semantic controls, associated labels, keyboard focus,
  sufficient contrast, loading/error/success states, and Tailwind CSS v4 utility classes.
- Use reference images only as visual guidance. Do not copy unrelated image content or invent
  requirement data. Keep navigation and shared state coherent across all supplied screens.
- Remove skeleton placeholders and data-arc-obligation markers from the proposed implementation.

Patch scope:
- Edit only frontend writable files represented by the supplied writable target module cards.
- For every edit, obtain `file` and the old text from the same `writable_targets` card; never
  invent a path, use a module_id as a path, or rely on a separate `target_modules` list for source.
- Return an exact `search` fragment copied from the current source and its replacement. The search
  fragment must be unique in the selected source file. A replacement may be empty when the exact
  fragment must be deleted.
- Do not change signatures, routes, generated types, tests, or compiler glue.
  Imports and exports in the supplied target file are valid edit locations.
- Return exact file-based edits. The same relative file may appear multiple times when it needs
  multiple non-overlapping replacements. Do not emit module_id in the output.
- Keep each replacement concise and complete; never truncate JSX, strings, or object literals.
- Return only one module that needs to change for the current failure or first-call scope. If
  several modules are supplied, choose the first coherent module that can make progress; the caller
  will invoke you again for the remaining modules. Never combine multiple modules into one edit.

Return exactly one JSON object:
{"edits":[{"file":"frontend/src/path/File.tsx","search":"unique old JSX or logic fragment","replacement":"new JSX or logic fragment"}]}
"""


@dataclass(frozen=True, slots=True)
class ImplementationRequest:
    requirement_id: str
    requirement: dict[str, Any]
    requirement_contract: dict[str, Any]
    test_manifest: dict[str, Any]
    code_binding_registry: dict[str, Any]
    failure_reports: tuple[Any, ...]
    iteration: int
    mode: str = "TDD"
    design_context: dict[str, Any] | None = None
    previous_patch_metadata: dict[str, Any] | None = None
    failure_analysis_text: str = ""
    retry_feedback: tuple[str, ...] = ()
    target_module_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class ImplementationResult:
    requirement_id: str
    status: str
    patch: ProposedPatch | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    schema_version: int = IMPLEMENTATION_AGENT_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return self.status == "PATCH_PROPOSED" and self.patch is not None and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImplementationAgent:
    """Produce one bounded implementation patch without owning any side effects."""

    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        retries: int = 2,
        max_context_characters: int = 600_000,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self._max_context_characters = max(
            50_000,
            min(int(max_context_characters), 1_000_000),
        )
        self._allowed_kinds: set[str] | None = None
        self._agent = BaseStructuredAgent(
            model,
            schema_name="arc_implementation_patch",
            instructions=IMPLEMENTATION_INSTRUCTIONS,
            output_schema=IMPLEMENTATION_OUTPUT_SCHEMA,
            retries=retries,
            trace=trace,
            model_log=self._write_model_log,
            agent_name="ImplementationAgent",
        )

    def _write_model_log(self, payload: dict[str, Any]) -> None:
        log_root = self.output_root / ".arc" / "model_logs" / "implementation_agent"
        log_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        agent_slug = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            str(payload.get("agent_name", "ImplementationAgent")),
        )
        requirement_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(payload.get("requirement_id", "unknown")))
        attempt = int(payload.get("attempt", 0) or 0)
        path = log_root / f"{agent_slug}-{stamp}-{requirement_id}-attempt-{attempt}.log"
        path.write_text(_format_model_log(payload), encoding="utf-8")


    def implement(self, request: ImplementationRequest) -> ImplementationResult:
        requirement_id = str(request.requirement_id).strip()
        context, source_hashes, _focus_ids, errors = self._build_context(request)
        if errors:
            return ImplementationResult(
                requirement_id=requirement_id,
                status="CONTEXT_REJECTED",
                errors=errors,
            )

        invocation = self._agent.invoke(
            context,
            validate=lambda output: _validate_decision(
                output,
            ),
            initial_feedback=list(request.retry_feedback),
        )
        if not invocation.ok or invocation.output is None:
            return ImplementationResult(
                requirement_id=requirement_id,
                status="MODEL_REJECTED",
                attempts=invocation.attempts,
                errors=invocation.errors,
            )

        output_rows = invocation.output["edits"]
        if not output_rows:
            return ImplementationResult(
                requirement_id=requirement_id,
                status="MODEL_REJECTED",
                attempts=invocation.attempts,
                errors=[
                    "ARC4534 IMPLEMENTATION_OUTPUT_INVALID: implementation requests "
                    "must return at least one exact edit."
                ],
            )

        edits = tuple(
            ProposedEdit(
                file=str(row["file"]),
                expected_sha256=source_hashes[str(row["file"])],
                search=str(row["search"]),
                replacement=str(row["replacement"]),
            )
            for row in output_rows
        )
        return ImplementationResult(
            requirement_id=requirement_id,
            status="PATCH_PROPOSED",
            patch=ProposedPatch(requirement_id=requirement_id, edits=edits),
            attempts=invocation.attempts,
        )

    def _build_context(
        self,
        request: ImplementationRequest,
    ) -> tuple[dict[str, Any], dict[str, str], set[str], list[str]]:
        requirement_id = str(request.requirement_id).strip()
        mode = str(request.mode).strip().upper()
        errors: list[str] = []
        if not requirement_id:
            errors.append("ARC4530 IMPLEMENTATION_CONTEXT_INVALID: requirement_id is required.")
        if not all(
            isinstance(value, dict)
            for value in (
                request.requirement,
                request.requirement_contract,
                request.test_manifest,
                request.code_binding_registry,
            )
        ):
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: requirement, contract, manifests, "
                "and bindings must be objects."
            )
            return {}, {}, set(), errors
        if not isinstance(request.iteration, int) or request.iteration < 1:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: iteration must be positive."
            )
        requirement_payload_id = str(request.requirement.get("id", "")).strip()
        if requirement_payload_id and requirement_payload_id != requirement_id:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: requirement payload id does not match."
            )
        if mode not in {
            "TDD",
            "UNIT_REPAIR",
            "INTEGRATION_REPAIR",
            "E2E_BACKEND_REPAIR",
            "E2E_FRONTEND_REPAIR",
            "AGGREGATE",
        }:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: unsupported implementation mode."
            )
        if mode in {
            "TDD",
            "UNIT_REPAIR",
            "INTEGRATION_REPAIR",
            "E2E_BACKEND_REPAIR",
            "E2E_FRONTEND_REPAIR",
        } and request.test_manifest.get("status") != "TESTS_FROZEN":
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: Test Manifest is not frozen."
            )
        if request.code_binding_registry.get("status") != "CODE_BINDING_READY":
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: Code Binding Registry is not ready."
            )
        reports = [
            row
            for value in request.failure_reports
            if (row := _report_dict(value)) is not None
        ]
        if len(reports) != len(request.failure_reports):
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: every failure report must be serializable."
            )
        first_tdd_call = mode == "TDD" and not reports
        if not reports and not first_tdd_call:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: one failure cluster is required."
            )
        report_requirements = {
            str(row.get("requirement_id", "")) for row in reports if row
        }
        if report_requirements and report_requirements != {requirement_id}:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: failure reports belong to another requirement."
            )
        failure_classes = {
            str(row.get("failure_class", "")) for row in reports if row
        }
        if reports and len(failure_classes) != 1:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: reports must share one failure class."
            )
        non_actionable = sorted(failure_classes - ACTIONABLE_FAILURE_CLASSES) if reports else []
        if non_actionable:
            errors.append(
                "ARC4531 IMPLEMENTATION_FAILURE_NOT_ACTIONABLE: fixed workflow must handle "
                f"{non_actionable}."
            )
        fingerprints = {
            str(row.get("failure_fingerprint", ""))
            for row in reports
            if str(row.get("failure_fingerprint", ""))
        }
        if reports and len(fingerprints) != 1:
            errors.append(
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: reports must form one failure cluster."
            )

        requirement_targets = next(
            (
                row
                for row in request.code_binding_registry.get("requirement_targets", [])
                if isinstance(row, dict)
                and str(row.get("requirement_id", "")) == requirement_id
            ),
            None,
        )
        if requirement_targets is None:
            errors.append(
                f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: unknown requirement {requirement_id}."
            )
            return {}, {}, set(), errors
        bindings = {
            str(row.get("module_id", "")): row
            for row in request.code_binding_registry.get("code_bindings", [])
            if isinstance(row, dict) and str(row.get("module_id", ""))
        }
        writable_ids = {
            str(value) for value in requirement_targets.get("writable", []) if str(value)
        }
        if self._allowed_kinds is not None:
            writable_ids = {
                module_id
                for module_id in writable_ids
                if str(bindings.get(module_id, {}).get("kind", "")).upper()
                in self._allowed_kinds
            }
        requested_target_ids = {
            str(value).strip()
            for value in request.target_module_ids
            if str(value).strip()
        }
        if requested_target_ids:
            # The request already carries the finite source cards for this call.
            # Do not reject a target because an older ownership projection did
            # not classify it as writable; the exact file set is the scope.
            writable_ids = set(requested_target_ids) & set(bindings)
        read_only_ids = {
            str(value) for value in requirement_targets.get("read_only", []) if str(value)
        }
        reported_writable_ids = {
            str(target.get("module_id", ""))
            for report in reports
            for target in report.get("writable_targets", [])
            if isinstance(target, dict) and str(target.get("module_id", ""))
        }
        focus_files = {
            _safe_workspace_file(str(bindings[module_id].get("file", "")), source=True)
            for module_id in (reported_writable_ids & writable_ids)
            if module_id in bindings
        }
        focus_files = {value for value in focus_files if value}
        # The general implementation agent may inspect a cross-layer backend
        # failure, but it must not opportunistically rewrite frontend modules
        # unless the failure cluster itself identifies a frontend target.  The
        # frontend specialist is selected by the orchestrator for those
        # clusters. This keeps a backend repair from opportunistically producing
        # a large UI rewrite and new frontend type errors.
        frontend_kinds = {"PAGE", "COMPONENT", "LAYOUT", "STORE"}
        reported_frontend = any(
            str(target.get("kind", "")).upper() in frontend_kinds
            for report in reports
            for target in report.get("writable_targets", [])
            if isinstance(target, dict)
        )
        if self._allowed_kinds is None and not reported_frontend:
            writable_ids = {
                module_id
                for module_id in writable_ids
                if str(bindings.get(module_id, {}).get("kind", "")).upper()
                not in frontend_kinds
            }
        relevant_writable = set(writable_ids)
        localized_ids: set[str] = set()
        if not requested_target_ids:
            localized_ids = reported_writable_ids & writable_ids
            if localized_ids:
                direct_neighbors = {
                    module_id
                    for module_id in writable_ids
                    if module_id in localized_ids
                    or localized_ids.intersection(
                        {
                            str(value)
                            for value in bindings.get(module_id, {}).get("callees", [])
                            if str(value)
                        }
                    )
                    or any(
                        module_id
                        in {
                            str(value)
                            for value in bindings.get(localized_id, {}).get("callees", [])
                            if str(value)
                        }
                        for localized_id in localized_ids
                    )
                }
                relevant_writable = direct_neighbors
        full_visual_context = first_tdd_call or any(
            str(report.get("phase", "")) == "FRONTEND_BOOTSTRAP" for report in reports
        )
        layer_scope = (
            "EXPLICIT_TARGETS"
            if requested_target_ids
            else "LOCALIZED_TARGETS_PLUS_DIRECT_NEIGHBORS"
            if localized_ids
            else "ALL_REQUIREMENT_OWNED_WRITABLE_MODULES"
        )
        reported_read_only_ids = {
            str(target.get("module_id", ""))
            for report in reports
            for target in report.get("read_only_dependencies", [])
            if isinstance(target, dict) and str(target.get("module_id", ""))
        }
        relevant_read_only = reported_read_only_ids & read_only_ids
        for module_id in relevant_writable:
            relevant_read_only.update(
                str(value)
                for value in bindings.get(module_id, {}).get("callees", [])
                if str(value) in read_only_ids
            )
        relevant_read_only.update(
            module_id
            for module_id in read_only_ids
            if relevant_writable.intersection(
                {
                    str(value)
                    for value in bindings.get(module_id, {}).get("callees", [])
                    if str(value)
                }
            )
        )

        source_hashes: dict[str, str] = {}
        writable_cards: list[dict[str, Any]] = []
        for module_id in sorted(relevant_writable):
            binding = bindings.get(module_id)
            if binding is None:
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: writable binding unavailable: {module_id}."
                )
                continue
            relative = _safe_workspace_file(str(binding.get("file", "")), source=True)
            if relative is None:
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: unsafe source path for {module_id}."
                )
                continue
            target = (self.output_root / Path(relative)).resolve()
            if self.output_root not in target.parents or not target.is_file():
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: source escapes workspace "
                    f"or does not exist: {relative}."
                )
                continue
            try:
                source = _read_text(target)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except (OSError, UnicodeError) as exc:
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: cannot read {relative}: {exc}"
                )
                continue
            existing_digest = source_hashes.get(relative)
            if existing_digest is not None and existing_digest != digest:
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: inconsistent file hash for {relative}."
                )
            source_hashes[relative] = digest
            card = _source_card(binding, digest=digest)
            card["source"] = source
            writable_cards.append(card)

        frozen_tests, test_errors = ([], [])
        if mode in {
            "TDD",
            "UNIT_REPAIR",
            "INTEGRATION_REPAIR",
            "E2E_BACKEND_REPAIR",
            "E2E_FRONTEND_REPAIR",
        } and not first_tdd_call:
            frozen_tests, test_errors = self._frozen_tests(
                requirement_id,
                request.test_manifest,
                layers=set(),
            )
        errors.extend(test_errors)
        if errors:
            return {}, {}, focus_files, list(dict.fromkeys(errors))

        projected_design_context = _project_design_context(
            request.design_context,
            requirement_id=requirement_id,
            target_ids=relevant_writable | relevant_read_only,
            full_visual_analysis=full_visual_context,
            frontend_only=self._allowed_kinds is not None,
        )
        # Stable segment: everything that stays byte-identical across the iterations
        # of one node, ordered cheapest-to-largest so the provider prefix cache keeps
        # as long a common prefix as possible. Source files come last because they are
        # both the largest section and the one that changes once a patch lands.
        stable_context = {
            "schema_version": IMPLEMENTATION_AGENT_SCHEMA_VERSION,
            "implementation_mode": mode,
            "policy": {
                "one_failure_cluster": bool(reports),
                "tests_are_frozen": mode == "TDD",
                "target_module_ids": sorted(requested_target_ids),
                "tests_limited_to_failed_layers": False,
                "writable_context_scope": layer_scope,
                "writable_layer_scope": layer_scope,
                "design_context_scope": layer_scope,
                "aggregate_mode_uses_design_and_binding_authority": mode == "AGGREGATE",
                "output_is_region_replacement_only": True,
                "side_effects_owned_by_orchestrator": True,
            },
            "project_conventions": _PROJECT_CONVENTIONS,
            "design_context": projected_design_context,
            "writable_targets": writable_cards,
        }
        # Dynamic segment: the current task. It is the last user message, so the model
        # reads it closest to its own turn and no cached prefix is invalidated by it.
        dynamic_context = {
            "requirement_id": requirement_id,
            "implementation_phase": (
                "TDD"
                if first_tdd_call
                else mode
                if mode in {
                    "UNIT_REPAIR",
                    "INTEGRATION_REPAIR",
                    "E2E_BACKEND_REPAIR",
                    "E2E_FRONTEND_REPAIR",
                    "AGGREGATE",
                }
                else "TDD_REPAIR"
            ),
            "requirement": request.requirement,
            "requirement_contract": (
                _project_frontend_contract(request.requirement_contract)
                if self._allowed_kinds is not None
                else request.requirement_contract
            ),
            "target_module_ids": sorted(requested_target_ids),
            "provided_source_files": sorted(source_hashes),
        }
        if not first_tdd_call:
            analysis = _failure_analysis_with_failed_tests(
                request.failure_analysis_text or _fallback_failure_analysis(reports),
                frozen_tests,
                reports,
            )
            dynamic_context.update({
                "iteration": request.iteration,
                "failure_analysis": (
                    _project_frontend_failure_analysis(analysis)
                    if self._allowed_kinds is not None else analysis
                ),
                "scope_warnings": _scope_warnings(reports, read_only_ids=read_only_ids),
                "prior_retry_feedback": list(request.retry_feedback),
            })
            if request.previous_patch_metadata is not None:
                dynamic_context["previous_patch_metadata"] = request.previous_patch_metadata
        context = {
            CONTEXT_SEGMENTS_KEY: [
                {"name": "stable_project_context", "payload": stable_context},
                {"name": "current_task", "payload": dynamic_context},
            ]
        }
        sections = {**stable_context, **dynamic_context}
        context_size = len(
            json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )
        if context_size > self._max_context_characters:
            section_sizes = {
                key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                for key, value in sections.items()
            }
            largest_sections = ", ".join(
                f"{key}={size}"
                for key, size in sorted(
                    section_sizes.items(), key=lambda item: item[1], reverse=True
                )[:6]
            )
            return (
                {},
                {},
                focus_files,
                [
                    "ARC4532 IMPLEMENTATION_CONTEXT_TOO_LARGE: "
                    f"{context_size} characters exceeds {self._max_context_characters}; "
                    f"largest_sections: {largest_sections}."
                ],
            )
        return context, source_hashes, focus_files, []

    def _frozen_tests(
        self,
        requirement_id: str,
        manifest: dict[str, Any],
        *,
        layers: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        tests: list[dict[str, Any]] = []
        errors: list[str] = []
        rows = [
            row
            for row in manifest.get("files", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
            and (
                not layers
                or str(row.get("layer", "")).upper() in layers
            )
        ]
        if not rows:
            return [], [
                "ARC4530 IMPLEMENTATION_CONTEXT_INVALID: no frozen tests for "
                f"current requirement {requirement_id}."
            ]
        for row in rows:
            relative = _safe_workspace_file(str(row.get("test_file", "")), source=False)
            if relative is None or row.get("status") != "FROZEN":
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: invalid frozen test row {row}."
                )
                continue
            target = (self.output_root / Path(relative)).resolve()
            if self.output_root not in target.parents or not target.is_file():
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: test escapes workspace "
                    f"or does not exist: {relative}."
                )
                continue
            try:
                raw = target.read_bytes()
                source = raw.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(
                    f"ARC4530 IMPLEMENTATION_CONTEXT_INVALID: cannot read {relative}: {exc}"
                )
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if digest != str(row.get("content_sha256", "")):
                errors.append(
                    f"ARC4533 FROZEN_TEST_CHANGED: integrity mismatch for {relative}."
                )
                continue
            tests.append(
                {
                    "layer": str(row.get("layer", "")),
                    "test_file": relative,
                    "test_ids": [str(value) for value in row.get("test_ids", [])],
                    "source": source,
                }
            )
        return tests, errors


class FrontendImplementationAgent(ImplementationAgent):
    """Frontend-specialized agent with a frontend-only writable surface."""

    FRONTEND_KINDS = {"PAGE", "COMPONENT", "LAYOUT", "STORE"}

    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        retries: int = 2,
        max_context_characters: int = 600_000,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            model,
            output_root,
            retries=retries,
            max_context_characters=max_context_characters,
            trace=trace,
        )
        self._allowed_kinds = set(self.FRONTEND_KINDS)
        self._agent = BaseStructuredAgent(
            model,
            schema_name="arc_frontend_implementation_patch",
            instructions=FRONTEND_IMPLEMENTATION_INSTRUCTIONS,
            output_schema=FRONTEND_IMPLEMENTATION_OUTPUT_SCHEMA,
            retries=retries,
            trace=trace,
            model_log=self._write_model_log,
            agent_name="FrontendImplementationAgent",
        )


def _format_model_log(payload: dict[str, Any]) -> str:
    sections = [
        "ARC MODEL INVOCATION",
        f"agent_name: {payload.get('agent_name', 'StructuredAgent')}",
        f"schema_name: {payload.get('schema_name', '')}",
        f"requirement_id: {payload.get('requirement_id', '')}",
        f"implementation_phase: {_model_log_phase(payload)}",
        f"iteration: {payload.get('iteration', '')}",
        f"attempt: {payload.get('attempt', '')}",
        f"duration_ms: {payload.get('duration_ms', '')}",
        "",
        "===== INSTRUCTIONS =====",
        str(payload.get("instructions", "")),
        "",
        "===== INPUT PAYLOAD =====",
        json.dumps(payload.get("input_payload", {}), ensure_ascii=False, indent=2, default=str),
        "",
        "===== OUTPUT SCHEMA =====",
        json.dumps(payload.get("output_schema", {}), ensure_ascii=False, indent=2, default=str),
        "",
        "===== MODEL OUTPUT =====",
        json.dumps(payload.get("output"), ensure_ascii=False, indent=2, default=str)
        if payload.get("output") is not None
        else "(no parsed model output)",
        "",
        "===== ERROR =====",
        str(payload.get("error") or "(none)"),
        "",
    ]
    return "\n".join(sections)


def _model_log_phase(payload: dict[str, Any]) -> str:
    input_payload = payload.get("input_payload")
    if not isinstance(input_payload, dict):
        return ""
    segments = input_payload.get(CONTEXT_SEGMENTS_KEY)
    if not isinstance(segments, list):
        return ""
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("name") != "current_task":
            continue
        task = segment.get("payload")
        if isinstance(task, dict):
            return str(task.get("implementation_phase", ""))
    return ""


def _validate_decision(
    decision: dict[str, Any],
) -> list[str]:
    if not isinstance(decision, dict) or set(decision) != {"edits"}:
        return ["ARC4534 IMPLEMENTATION_OUTPUT_INVALID: output must contain edits only."]
    edits = decision.get("edits")
    errors: list[str] = []
    if not isinstance(edits, list) or len(edits) > 16:
        return [*errors, "ARC4534 IMPLEMENTATION_OUTPUT_INVALID: edits must contain 0..16 rows."]
    for row in edits:
        if not isinstance(row, dict) or set(row) != {"file", "search", "replacement"}:
            errors.append("ARC4534 IMPLEMENTATION_OUTPUT_INVALID: malformed edit row.")
            continue
        file = str(row.get("file", "")).strip()
        search = row.get("search")
        replacement = row.get("replacement")
        if not isinstance(replacement, str):
            errors.append(
                f"ARC4534 IMPLEMENTATION_OUTPUT_INVALID: {file!r} has no replacement."
            )
        if not isinstance(search, str) or not search.strip():
            errors.append(
                f"ARC4534 IMPLEMENTATION_OUTPUT_INVALID: {file!r} has no search fragment."
            )
        elif len(search.encode("utf-8")) > 80_000:
            errors.append(
                f"ARC4534 IMPLEMENTATION_OUTPUT_INVALID: {file!r} search fragment is too large."
            )
        elif isinstance(replacement, str) and len(replacement.encode("utf-8")) > 200_000:
            errors.append(
                f"ARC4534 IMPLEMENTATION_OUTPUT_INVALID: {file!r} replacement is too large."
            )
    return list(dict.fromkeys(errors))


def _report_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        serialized = serializer()
        return serialized if isinstance(serialized, dict) else None
    return None


def _project_frontend_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Keep behavior needed by UI implementation without backend decomposition noise."""

    if not isinstance(contract, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("spec", "server_state", "inputs", "outputs", "effects", "obligations"):
        if key in contract:
            result[key] = copy.deepcopy(contract[key])
    return result


def _project_design_context(
    design_context: dict[str, Any] | None,
    *,
    requirement_id: str,
    target_ids: set[str],
    full_visual_analysis: bool = True,
    frontend_only: bool = False,
) -> dict[str, Any]:
    """Keep only Design evidence reachable from this implementation request."""

    if not isinstance(design_context, dict):
        return {}
    frontend = design_context.get("frontend", {})
    if not isinstance(frontend, dict):
        frontend = {}

    available_screens = [
        row for row in frontend.get("screens", []) if isinstance(row, dict)
    ]
    screens = [
        _compact_frontend_design_row(row, "screen")
        for row in available_screens
        if str(row.get("id", "")) in target_ids
    ]
    available_components = [
        row for row in frontend.get("screen_components", []) if isinstance(row, dict)
    ]
    placements = [
        _compact_frontend_design_row(row, "placement")
        for row in frontend.get("placements", [])
        if isinstance(row, dict)
        and str(row.get("requirement_id", "")) == requirement_id
    ]
    owned_components = [
        row for row in available_components if str(row.get("id", "")) in target_ids
    ]
    # A partitioned page owns nothing itself, so the writable component is what
    # pulls its host screen into scope.
    for row in owned_components:
        host_id = str(row.get("screen_id", ""))
        if host_id and host_id not in {str(item.get("id", "")) for item in screens}:
            host = next(
                (item for item in available_screens if str(item.get("id", "")) == host_id),
                None,
            )
            if host is not None:
                screens.append(_compact_frontend_design_row(host, "screen"))
    primary_screen_ids = {str(row.get("id", "")) for row in screens}
    screen_components = [
        _compact_frontend_design_row(row, "component")
        for row in available_components
        if str(row.get("screen_id", "")) in primary_screen_ids
    ]
    route_index = {
        str(row.get("route", "")): row
        for row in available_screens
        if str(row.get("route", ""))
    }
    navigation_routes = {
        str(target.get("target_route", ""))
        for screen in screens
        for target in screen.get("navigation_targets", [])
        if isinstance(target, dict) and str(target.get("target_route", ""))
    }
    screen_ids = set(primary_screen_ids)
    for route in sorted(navigation_routes):
        destination = route_index.get(route)
        destination_id = str((destination or {}).get("id", ""))
        if destination is not None and destination_id not in screen_ids:
            screens.append(_compact_frontend_design_row(destination, "screen"))
            screen_ids.add(destination_id)

    shared_state_policies = [
        _compact_frontend_design_row(row, "state")
        for row in frontend.get("shared_state_policies", [])
        if isinstance(row, dict) and str(row.get("id", "")) in target_ids
    ]
    visual_ids = {
        str(value)
        for screen in screens
        for value in screen.get("visual_reference_ids", [])
        if str(value)
    }
    visual_references = [
        _compact_visual_reference(row, full_analysis=full_visual_analysis)
        for row in frontend.get("visual_references", [])
        if isinstance(row, dict) and str(row.get("id", "")) in visual_ids
    ]
    referenced_api_ids = {
        str(value)
        for row in [*screens, *screen_components]
        for value in row.get("required_api_ids", [])
        if str(value)
    }

    return {
        "requirement_id": requirement_id,
        "backend_modules": [
            _compact_design_module(row)
            for row in design_context.get("backend_modules", [])
            if isinstance(row, dict)
            and (
                str(row.get("id", row.get("module_id", ""))) in target_ids
                or (
                    frontend_only
                    and str(row.get("id", row.get("module_id", "")))
                    in referenced_api_ids
                )
            )
            and (
                not frontend_only
                or str(row.get("kind", "")).upper() in {"API", "API_CLIENT"}
            )
        ],
        "frontend": {
            "screens": screens,
            "placements": placements,
            "screen_components": screen_components,
            "shared_state_policies": shared_state_policies,
            "visual_references": visual_references,
        },
    }


def _compact_design_module(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id", "module_id", "kind", "name", "description", "requirement_id",
        "effects", "callees", "callers", "obligations", "behavioral_obligations",
        "constraints",
    )
    return {key: copy.deepcopy(row.get(key)) for key in fields if key in row}


def _compact_frontend_design_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    fields = {
        "screen": (
            "id",
            "route",
            "purpose",
            "route_inputs",
            "requirement_ids",
            "entry_conditions",
            "observable_states",
            "required_api_ids",
            "navigation_targets",
            "visual_reference_ids",
            "surface_keys",
        ),
        "journey": (
            "id",
            "requirement_id",
            "source_screen_id",
            "trigger",
            "api_id",
            "success_target_route",
            "failure_behavior",
        ),
        "component": ("id", "screen_id", "purpose", "requirement_ids", "inputs", "required_api_ids", "shared_state_ids", "observable_states", "visual_reference_ids"),
        "placement": ("requirement_id", "ui_scope", "screen_id", "component_id", "strategy"),
        "api_usage": ("screen_id", "api_id", "request_bindings", "response_bindings"),
        "state": ("id", "purpose", "requirement_ids", "persistence", "state", "actions"),
        "visual": ("id", "source_path", "analysis"),
    }[kind]
    return {key: copy.deepcopy(row.get(key)) for key in fields if key in row}


def _compact_visual_reference(
    row: dict[str, Any],
    *,
    full_analysis: bool,
) -> dict[str, Any]:
    """Stage visual evidence by phase.

    The first frontend TDD call establishes the visual direction and needs
    the complete analysis. Every later functional repair only needs to keep the
    established composition recognizable, so the layout and style cue lists are
    dropped and only the region/control inventory survives.
    """

    projected = _compact_frontend_design_row(row, "visual")
    analysis = projected.get("analysis")
    if full_analysis or not isinstance(analysis, dict):
        return projected
    projected["analysis"] = {
        key: copy.deepcopy(analysis[key])
        for key in ("reference_id", "regions", "visible_controls")
        if key in analysis
    }
    projected["analysis_scope"] = "REGIONS_AND_CONTROLS_ONLY"
    return projected


def _failure_analysis_with_failed_tests(
    analysis: str,
    frozen_tests: list[dict[str, Any]],
    reports: list[dict[str, Any]],
) -> str:
    """Attach only failed test sources to the corresponding failure context.

    The manifest is still validated internally, but the model does not need every
    frozen test. This keeps unrelated tests out of the implementation prompt while
    preserving the exact source for each test implicated by the failure cluster.
    """

    if not frozen_tests:
        return analysis
    failed_ids = {
        str(test_id)
        for report in reports
        for test_id in report.get("test_ids", [])
        if str(test_id)
    }
    analysis_text = str(analysis or "")
    # E2E already carries the exact selected test source in its direct evidence
    # record.  Re-attaching manifest rows here both duplicates the source and,
    # for an interrupted browser run, can incorrectly attach unit tests that
    # share the same requirement id.
    if "E2E EXECUTION FEEDBACK" in analysis_text:
        return analysis_text
    mentioned_files = set(re.findall(r"(?m)^test_file:\s*(\S+)", analysis_text))
    selected: list[dict[str, Any]] = []
    for test in frozen_tests:
        test_ids = {str(value) for value in test.get("test_ids", []) if str(value)}
        test_file = str(test.get("test_file", ""))
        if (
            failed_ids.intersection(test_ids)
            or (test_file and test_file in analysis_text)
            or (test_file and PurePosixPath(test_file).name in mentioned_files)
        ):
            selected.append(test)
    if not selected:
        return analysis_text
    if "FAILED TEST SOURCE:" in analysis_text or "test_source:\n" in analysis_text:
        return analysis_text
    source_sections = [
        "FAILED TEST SOURCE:\n"
        f"test_file: {test.get('test_file', '(unknown)')}\n"
        f"layer: {test.get('layer', '(unknown)')}\n"
        f"test_ids: {', '.join(str(value) for value in test.get('test_ids', []))}\n"
        f"source:\n{test.get('source', '')}"
        for test in selected
    ]
    return "\n\n".join(value for value in (analysis_text, *source_sections) if value)


def _project_frontend_failure_analysis(analysis: str) -> str:
    """Drop duplicated implementation excerpts; writable_targets already carry full source."""

    return re.sub(
        r"\nrelevant_source:\n.*?(?=\nFAILED TEST:|\Z)",
        "\nrelevant_source: (see writable_targets.source)\n",
        str(analysis or ""),
        flags=re.DOTALL,
    )


def _fallback_failure_analysis(reports: list[dict[str, Any]]) -> str:
    """Keep a plain-text repair brief when no synthesized analysis is present."""

    entries: list[str] = []
    for report in reports:
        target_modules = report.get("target_modules", [])
        if not isinstance(target_modules, list):
            target_modules = []
        entries.append(
            "FAILURE ANALYSIS\n"
            f"failure_class={report.get('failure_class', '')}\n"
            f"phase={report.get('phase', '')}\n"
            f"message={report.get('message', '')}\n"
            f"target_modules={', '.join(str(value) for value in target_modules) or '(not localized)'}\n"
            f"details={report.get('diagnostic_output') or '(none)'}"
        )
    return "\n\n".join(entries)


def _scope_warnings(
    reports: list[dict[str, Any]],
    *,
    read_only_ids: set[str],
) -> list[str]:
    """Make non-writable failure causes explicit in the agent's task message."""

    warnings: list[str] = []
    classes = {str(report.get("failure_class", "")) for report in reports}
    compiler_owned = {
        "TEST_MATERIALIZATION":
            "Test generation/materialization is compiler-owned; repair the compiler or frozen test artifact, not business code.",
        "INFRASTRUCTURE":
            "The failure is infrastructure-owned; do not invent an application patch to compensate for it.",
        "TEST_OR_CONTRACT_INCONSISTENT":
            "The test/contract is inconsistent or compiler-owned; do not weaken frozen tests or change generated glue.",
    }
    for failure_class, message in compiler_owned.items():
        if failure_class in classes:
            warnings.append(f"COMPILER_OWNED_WARNING [{failure_class}]: {message}")
    if "DEFERRED_DEPENDENCY" in classes or read_only_ids:
        warnings.append(
            "DEPENDENCY_SCOPE_WARNING: dependency-owned modules are read-only. "
            "Dependency source is not included in this implementation context."
        )
    diagnostic_text = "\n".join(
        str(report.get("diagnostic_output", "")) for report in reports
    ).lower()
    compiler_tokens = (
        "frozen test",
        "generated glue",
        "compiler-owned",
        "route is compiler-owned",
    )
    if any(token in diagnostic_text for token in compiler_tokens):
        warnings.append(
            "COMPILER_OWNED_WARNING: the diagnostic references compiler-owned structure "
            "(tests, routes, generated glue, or config); keep that structure read-only."
        )
    warnings.append(
        "EDIT_SCOPE_WARNING: every supplied writable source file is available for diagnosis, "
        "and edits must use exact unique fragments from those files."
    )
    return list(dict.fromkeys(warnings))


def _source_card(binding: dict[str, Any], *, digest: str | None) -> dict[str, Any]:
    card = {
        key: binding.get(key)
        for key in (
            "module_id",
            "kind",
            "file",
            "symbol",
            "public_signature",
            "input_type",
            "output_type",
            "props_type",
            "route",
            "callees",
        )
    }
    if digest is not None:
        card["source_sha256"] = digest
    return card


def _safe_workspace_file(value: str, *, source: bool) -> str | None:
    normalized = str(value).replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    roots = ("backend/src/", "frontend/src/") if source else ("tests/",)
    suffixes = (".ts", ".tsx") if source else (".spec.ts",)
    if (
        not normalized
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or not normalized.startswith(roots)
        or not normalized.endswith(suffixes)
    ):
        return None
    return normalized


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()
