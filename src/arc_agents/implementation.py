from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .base import BaseStructuredAgent, JsonModel
from .contracts import ProposedEdit, ProposedPatch


IMPLEMENTATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edits"],
    "properties": {
        "edits": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "search", "replacement"],
                "properties": {
                    "file": {"type": "string"},
                    "search": {"type": "string"},
                    "replacement": {"type": "string"},
                },
            },
        },
    },
}


IMPLEMENTATION_INSTRUCTIONS = """Implement the supplied requirement in the editable source files.
When aggregate_requirement is supplied, repair its failing user journey through
the supplied editable source files, including dependency modules where necessary.
Dependencies are read-only unless explicitly included in editable_files for this task.
Preserve dependency public contracts and behavior used by other requirements.
The requirement is the source of behavior. Read the complete related source files for
existing signatures, imports, exports, types and dependencies. If test_output is
present, it is the unmodified output of the test/build command, not a diagnosis.
Tests are read-only unless test_correction explicitly authorizes a listed file after
failure diagnosis. In that case correct only the evidenced test defect against the
requirement and public contracts. Preserve scenarios, meaningful assertions and test
isolation; never skip/delete tests, accept erroneous behavior, or mock away the public
seam to obtain a pass. Fix implementation defects as well when diagnosis is MIXED.
When failure_diagnosis identifies implementation_errors, repair those exact files,
including diagnosed backend dependencies during an E2E repair. Unmentioned
dependencies remain read-only. implementation_feedback is compiler guidance, not
raw test output; test_output contains the test runner's failure output.
Do not invent paths or edit read-only files. Preserve existing public
interfaces, routes and generated glue. Return only JSON with exact file/search/replacement
edits. Copy search verbatim from a unique fragment of the current editable file.
The keys of editable_files are the complete write allowlist for this invocation.
related_files are read-only context.
The supplied router and related source files define the HTTP method, path and input source: use query for query inputs
and body for body inputs in both handlers and test requests. Do not infer the method
from a function name or change generated routes to accommodate an incorrect test.
If a screen is only in related_files, implement its owned components now; the page
will be handled in its own subsequent invocation.
"""

FAILURE_DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "reason", "test_errors", "implementation_errors"],
    "properties": {
        "verdict": {"type": "string", "enum": ["IMPLEMENTATION", "TEST", "MIXED", "DESIGN", "UNKNOWN"]},
        "reason": {"type": "string"},
        "test_errors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "test_evidence", "contract_evidence", "correction"],
            "properties": {key: {"type": "string"} for key in
                           ("file", "test_evidence", "contract_evidence", "correction")},
        }},
        "implementation_errors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "source_evidence", "contract_evidence", "correction"],
            "properties": {key: {"type": "string"} for key in
                           ("file", "source_evidence", "contract_evidence", "correction")},
        }},
    },
}

FAILURE_DIAGNOSIS_INSTRUCTIONS = """Diagnose this failed test batch before editing anything.
Compare the original requirement and scenarios, public contracts in related files, generated router,
test source, implementation and raw failure output. Requirements determine behavior;
frozen contracts determine public interfaces, HTTP method/path and input_source.
Use TEST only for a concrete test defect, IMPLEMENTATION for application defects,
MIXED for both, DESIGN when the required API/module is missing or the design cannot
satisfy the requirement, UNKNOWN when evidence is insufficient. A 404 or assertion
failure alone does not prove the test is wrong. Check routing before handler logic.
For TEST/MIXED list only defective files from candidate_test_files, quote an exact
source fragment as test_evidence, cite the conflicting requirement/contract as
contract_evidence, and describe the correction. Do not treat unimplemented behavior
as a test defect. Never propose removing scenarios, skipping tests, weakening valid
assertions, or inventing routes. Other verdicts must return an empty test_errors array.
Return only the diagnosis object; no edits in this step.
Use recent_changes to identify the actual accepted patches preceding this failure.
For IMPLEMENTATION/MIXED, implementation_errors must locate defective application
files from candidate_implementation_files, with an exact source_evidence excerpt,
requirement/contract evidence and a proposed correction. Do not name a dependency
merely because it is called. Other verdicts return implementation_errors=[].
"""

FRONTEND_IMPLEMENTATION_INSTRUCTIONS = IMPLEMENTATION_INSTRUCTIONS + """
Use Tailwind CSS utilities in JSX to implement the actual visual styling, not just
semantic markup. Tailwind does not generate CSS for invented semantic class names
such as field-row or brand-header. Use a custom class only if its definition exists
in a supplied stylesheet; otherwise replace it with complete Tailwind utility names.
Do not concatenate utility fragments dynamically; use complete literal alternatives.

When visual_references contain analyses, follow their layout_cues, style_cues,
regions and text_cues for composition, color, typography, spacing and controls.
Translate that evidence into concrete responsive Tailwind classes. Reuse the same
visual language across related components, preserving existing shared components.
When no reference analysis is supplied, choose a beautiful, minimal style suited to
the product: restrained colors, readable type, consistent spacing, clear hierarchy,
and whitespace. Follow existing application styling where it is already established.
Avoid unnecessary cards, gradients and shadows. Style forms, buttons and navigation,
including focus-visible, hover, disabled, loading and error states as applicable.
Keep the layout usable on mobile and desktop. The compiler's initial base stylesheet
is not a finished design. Before returning edits, check that all visible UI has real
styling and that every non-utility class you use has an existing CSS definition.
Passing behavioral tests alone does not establish visual completion.

Implement every supplied editable frontend screen and component, including their layout,
navigation, accessible controls, empty/loading/error states, and shared visual language.
Do not leave any 'Implementation pending' skeletons, even if E2E tests do not visit them.
Replace the compiler's default slate shell and remove data-arc-page,
data-arc-component, data-arc-layout, and data-arc-obligation skeleton markers.
Implement layout and behavior from the requirement, its visual_references analyses,
and the supplied source files. Preserve typed props, component exports and existing
shared components. Complete return bodies, event wiring, handlers and effects;
replace TODO and Not implemented placeholders. Add local state, refs or derived
values when required. Compose existing child components instead of duplicating
their markup. Use existing navigation contracts rather than inventing routes.
Only the keys of editable_files define the writable scope.
"""


@dataclass(frozen=True, slots=True)
class ImplementationRequest:
    requirement_id: str
    requirement: dict[str, Any]
    code_binding_registry: dict[str, Any]
    target_module_ids: tuple[str, ...] = ()
    test_output: str = ""
    test_files: tuple[str, ...] = ()
    frontend_ir: dict[str, Any] | None = None
    iteration: int = 0
    iteration_limit: int = 5
    test_layer: str = ""
    implementation_feedback: str = ""
    recent_changes: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class ImplementationResult:
    requirement_id: str
    status: str
    patch: ProposedPatch | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "PATCH_PROPOSED" and self.patch is not None and not self.errors


class ImplementationAgent:
    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        retries: int = 2,
        max_context_characters: int = 600_000,
        trace: Callable[[str], None] | None = None,
        model_log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self._max_context_characters = max_context_characters
        self._allowed_kinds: set[str] | None = {"DB", "FUNC", "API"}
        self._agent = BaseStructuredAgent(
            model,
            schema_name="arc_implementation_patch",
            instructions=IMPLEMENTATION_INSTRUCTIONS,
            output_schema=IMPLEMENTATION_OUTPUT_SCHEMA,
            retries=retries,
            trace=trace,
            model_log=model_log,
            agent_name="ImplementationAgent",
        )
        self._diagnosis_agent = BaseStructuredAgent(
            model, schema_name="arc_implementation_failure_diagnosis",
            instructions=FAILURE_DIAGNOSIS_INSTRUCTIONS,
            output_schema=FAILURE_DIAGNOSIS_SCHEMA, retries=retries,
            trace=trace, model_log=model_log, agent_name="ImplementationFailureDiagnosis",
        )
        self._trace = trace

    def implement(
        self,
        request: ImplementationRequest,
        *,
        accept_patch: Callable[[ProposedPatch], list[str]] | None = None,
    ) -> ImplementationResult:
        requirement_id = str(request.requirement_id).strip()
        try:
            context, hashes = self._context(request)
        except (KeyError, ValueError, OSError, UnicodeError) as exc:
            return ImplementationResult(requirement_id, "CONTEXT_REJECTED", errors=[str(exc)])
        if request.test_layer and request.test_output:
            visible = {**context["related_files"], **context["editable_files"]}
            candidate_files = {
                row["file"] for row in request.code_binding_registry.get("code_bindings", [])
                if row.get("file") in visible and row.get("kind") in
                {"DB", "FUNC", "API", "COMPONENT", "PAGE", "LAYOUT", "STORE", "API_CLIENT"}
                and row["file"].startswith(("backend/src/", "frontend/src/"))
            }
            diagnosis = self._diagnosis_agent.invoke(
                {**context, "candidate_test_files": list(request.test_files),
                 "candidate_implementation_files": sorted(candidate_files),
                 "recent_changes": list(request.recent_changes)},
                validate=lambda output: self._validate_diagnosis(output, request, visible, candidate_files),
            )
            if not diagnosis.ok or diagnosis.output is None:
                return ImplementationResult(requirement_id, "DIAGNOSIS_REJECTED", errors=diagnosis.errors)
            decision = diagnosis.output
            if self._trace:
                self._trace(
                    f"FAILURE_DIAGNOSED requirement={requirement_id} layer={request.test_layer} "
                    f"iteration={request.iteration}/{request.iteration_limit} "
                    f"verdict={decision['verdict']} reason={decision['reason']}"
                )
            context["failure_diagnosis"] = decision
            if decision["verdict"] in {"DESIGN", "UNKNOWN"}:
                return ImplementationResult(requirement_id, "DIAGNOSIS_BLOCKED", errors=[decision["reason"]])
            if decision["verdict"] == "TEST":
                context["related_files"].update(context["editable_files"])
                context["editable_files"] = {}
                hashes.clear()
            promoted_files = []
            for defect in decision["implementation_errors"]:
                relative = defect["file"]
                if relative not in context["editable_files"]:
                    source = context["related_files"].pop(relative)
                    context["editable_files"][relative] = source
                    hashes[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
                    promoted_files.append(relative)
            if promoted_files and self._trace:
                self._trace(f"IMPLEMENTATION_CORRECTION_AUTHORIZED requirement={requirement_id} files={promoted_files!r}")
            if decision["test_errors"]:
                context["test_correction"] = decision["test_errors"]
                for defect in decision["test_errors"]:
                    relative = defect["file"]
                    source = context["related_files"].pop(relative)
                    context["editable_files"][relative] = source
                    hashes[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
                if self._trace:
                    self._trace(f"TEST_CORRECTION_AUTHORIZED requirement={requirement_id} "
                                f"files={[row['file'] for row in decision['test_errors']]}")
        proposed_patch: ProposedPatch | None = None

        def validate_attempt(output: dict[str, Any]) -> list[str]:
            nonlocal proposed_patch
            errors = self._validate(output, hashes)
            if errors:
                return errors
            patch = ProposedPatch(requirement_id=requirement_id, edits=tuple(
                ProposedEdit(
                    file=row["file"], expected_sha256=hashes[row["file"]],
                    search=row["search"], replacement=row["replacement"],
                ) for row in output["edits"]
            ))
            if accept_patch is not None:
                errors = accept_patch(patch)
                if errors:
                    return errors
            proposed_patch = patch
            return []

        invocation = self._agent.invoke(context, validate=validate_attempt)
        if not invocation.ok or invocation.output is None:
            return ImplementationResult(
                requirement_id, "MODEL_REJECTED",
                attempts=invocation.attempts, errors=invocation.errors,
            )
        return ImplementationResult(
            requirement_id, "PATCH_PROPOSED",
            patch=proposed_patch,
            attempts=invocation.attempts,
        )

    def _context(self, request: ImplementationRequest) -> tuple[dict[str, Any], dict[str, str]]:
        from compiler.code_binding import CodeTargetResolver
        from compiler.file_context import direct_dependencies, file_selection_log

        resolved = CodeTargetResolver(request.code_binding_registry).resolve_requirement_targets(
            request.requirement_id
        )
        requested = set(request.target_module_ids)
        owned = [
            row for row in resolved["owned_targets"]
            if (not requested or row["module_id"] in requested)
            and (self._allowed_kinds is None or row["kind"] in self._allowed_kinds)
        ]
        if requested - {row["module_id"] for row in owned}:
            raise ValueError("Requested target is not writable by this requirement and agent.")
        if not owned and not (request.test_layer and request.test_files):
            raise ValueError("No writable source files for this requirement.")
        editable_paths = {row["file"] for row in owned}
        dependencies = direct_dependencies(
            owned, [*resolved["owned_targets"], *resolved["dependency_targets"]])
        if request.test_layer and request.test_output:
            # Diagnose across frontend/backend boundaries without granting writes.
            candidates = [*resolved["owned_targets"], *resolved["dependency_targets"]]
            seeds = [*resolved["owned_targets"], *dependencies]
            feedback = request.test_output.replace(chr(92), "/")
            seeds.extend(row for row in candidates if row.get("file") and row["file"] in feedback)
            changed = {path for change in request.recent_changes for path in change.get("changed_files", [])}
            seeds.extend(row for row in candidates if row.get("file") in changed)
            dependencies.extend([*seeds, *direct_dependencies(seeds, candidates)])
        # Failure evidence may add context, never silently grant write access.
        dependencies.extend(row for row in resolved["dependency_targets"]
                            if row.get("file") and row["file"] in request.test_output)
        type_ids = {reference["type_id"] for row in [*owned, *dependencies]
                    for reference in [row.get("input_type"), row.get("output_type"), row.get("props_type"),
                                      *row.get("store_types", [])]
                    if isinstance(reference, dict) and reference.get("type_id")}
        dependencies.extend(row for row in resolved["type_targets"] if row["type_id"] in type_ids)
        paths = sorted(editable_paths | {
            str(row.get("file", "")) for row in dependencies if row.get("file")
        })
        related = set(paths)
        router = "backend/src/generated/router.ts"
        if request.test_output and (self.output_root / router).is_file():
            related.add(router)
        entrypoints = {
            "API": "backend/src/app.ts",
            "PAGE": "frontend/src/app/router.tsx",
            "LAYOUT": "frontend/src/app/router.tsx",
        }
        for row in owned:
            entrypoint = entrypoints.get(str(row.get("kind", "")))
            if entrypoint and (self.output_root / entrypoint).is_file():
                related.add(entrypoint)
        for relative in sorted(editable_paths):
            source = self._read_file(relative)
            for specifier in re.findall(r'(?:from\s+|import\s+)["\']([^"\']+)["\']', source):
                if specifier == "@arc/shared":
                    candidate = "shared/src/index.ts"
                elif specifier.startswith("."):
                    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(relative), specifier))
                    if candidate.endswith(".js"):
                        candidate = candidate[:-3] + ".ts"
                    if not (self.output_root / candidate).is_file() and candidate.endswith(".ts"):
                        candidate = candidate[:-3] + ".tsx"
                    if not PurePosixPath(candidate).suffix:
                        candidate = next(
                            (path for path in (candidate + ".ts", candidate + ".tsx", candidate + "/index.ts", candidate + "/index.tsx")
                             if (self.output_root / path).is_file()),
                            candidate,
                        )
                else:
                    continue
                if candidate.startswith(("backend/src/", "frontend/src/", "shared/src/")) and (self.output_root / candidate).is_file():
                    related.add(candidate)
        for workspace in ("backend", "frontend"):
            if any(relative.startswith(workspace + "/") for relative in editable_paths):
                related.add(workspace + "/package.json")
        if any(relative.startswith("frontend/") for relative in editable_paths):
            # Component files do not import the app's base stylesheet directly.
            # Supply it explicitly so models can distinguish existing CSS classes
            # from names that would otherwise render without styling.
            for relative in ("frontend/src/index.css", "frontend/src/main.tsx", "frontend/vite.config.ts"):
                if (self.output_root / relative).is_file():
                    related.add(relative)
        paths = sorted(related)
        editable_files: dict[str, str] = {}
        related_files: dict[str, str] = {}
        hashes: dict[str, str] = {}
        for relative in paths:
            source = self._read_file(relative)
            if relative in editable_paths:
                editable_files[relative] = source
                hashes[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            else:
                related_files[relative] = source
        for relative in request.test_files:
            if relative not in paths:
                related_files[relative] = self._read_file(relative)
        if self._trace:
            self._trace(file_selection_log(request.requirement_id, "implementation", editable_files, "current_targets"))
            self._trace(file_selection_log(request.requirement_id, "implementation", related_files,
                                           "direct_dependencies_types_imports_support_and_failure_evidence"))
        from compiler.frontend_context import implementation_requirement
        requirement, missing_visuals = implementation_requirement(
            request.requirement, self.output_root, frontend=request.frontend_ir is not None)
        if missing_visuals and self._trace:
            self._trace(f"VISUAL_ANALYSIS_MISSING requirement={request.requirement_id} paths={missing_visuals!r}")
        context = {
            "requirement_id": request.requirement_id,
            "iteration": request.iteration,
            "iteration_limit": request.iteration_limit,
            "test_layer": request.test_layer,
            "implementation_mode": "frontend" if request.frontend_ir is not None else "backend",
            "requirement": requirement,
            "editable_files": editable_files,
            "related_files": related_files,
        }
        if request.test_output:
            context["test_output"] = request.test_output
        if request.implementation_feedback:
            context["implementation_feedback"] = request.implementation_feedback
        if len(str(context)) > self._max_context_characters:
            raise ValueError("Implementation context exceeds its size limit.")
        return context, hashes

    @staticmethod
    def _validate_diagnosis(output: dict[str, Any], request: ImplementationRequest,
                            sources: dict[str, str], candidate_files: set[str]) -> list[str]:
        if not isinstance(output, dict) or set(output) != {"verdict", "reason", "test_errors", "implementation_errors"}:
            return ["Return verdict, reason, test_errors and implementation_errors."]
        if not isinstance(output["verdict"], str) or output["verdict"] not in {
            "IMPLEMENTATION", "TEST", "MIXED", "DESIGN", "UNKNOWN"
        }:
            return ["Unknown diagnosis verdict."]
        if not isinstance(output["reason"], str) or not output["reason"].strip():
            return ["Diagnosis requires a reason supported by the supplied evidence."]
        defects = output["test_errors"]
        if not isinstance(defects, list):
            return ["test_errors must be an array."]
        if bool(defects) != (output["verdict"] in {"TEST", "MIXED"}):
            return ["Only TEST/MIXED must include concrete test_errors."]
        errors: list[str] = []
        implementation_errors = output["implementation_errors"]
        if not isinstance(implementation_errors, list):
            return ["implementation_errors must be an array."]
        if bool(implementation_errors) != (output["verdict"] in {"IMPLEMENTATION", "MIXED"}):
            errors.append("Only IMPLEMENTATION/MIXED must identify concrete implementation_errors.")
        seen_implementation: set[str] = set()
        for defect in implementation_errors:
            if not isinstance(defect, dict) or set(defect) != {
                "file", "source_evidence", "contract_evidence", "correction"
            } or not all(isinstance(value, str) and value.strip() for value in defect.values()):
                errors.append("Implementation defects require file, source_evidence, contract_evidence and correction.")
                continue
            relative = defect["file"]
            if relative not in candidate_files or relative in seen_implementation:
                errors.append("Implementation correction must name a unique candidate_implementation_files entry.")
            elif defect["source_evidence"] not in sources[relative]:
                errors.append(f"{relative}: source_evidence must quote the supplied source exactly.")
            seen_implementation.add(relative)
        seen: set[str] = set()
        for defect in defects:
            if not isinstance(defect, dict) or set(defect) != {
                "file", "test_evidence", "contract_evidence", "correction"
            } or not all(isinstance(value, str) and value.strip() for value in defect.values()):
                errors.append("Each test defect needs file, exact test_evidence, contract_evidence and correction.")
                continue
            relative = defect["file"]
            if relative not in request.test_files or relative in seen:
                errors.append("Test correction must name a unique file from this failed batch.")
            elif not relative.startswith(f"tests/{request.test_layer.lower()}/"):
                errors.append("Test correction must stay in the current test layer.")
            elif defect["test_evidence"] not in sources.get(relative, ""):
                errors.append(f"{relative}: test_evidence must quote the supplied test exactly.")
            seen.add(relative)
        return errors

    def _read_file(self, relative: str) -> str:
        path = PurePosixPath(relative.replace(chr(92), "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("Unsafe source file path: " + relative)
        absolute = (self.output_root / Path(*path.parts)).resolve()
        if self.output_root not in absolute.parents or not absolute.is_file():
            raise ValueError("Source file missing or outside the workspace: " + relative)
        with absolute.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()

    @staticmethod
    def _validate(output: dict[str, Any], hashes: dict[str, str]) -> list[str]:
        if not isinstance(output, dict) or set(output) != {"edits"}:
            return ["Return only an edits array."]
        edits = output["edits"]
        if not isinstance(edits, list) or not edits:
            return ["Return at least one exact edit."]
        errors: list[str] = []
        for row in edits:
            if not isinstance(row, dict) or set(row) != {"file", "search", "replacement"}:
                errors.append("Each edit needs file, search and replacement.")
            elif not isinstance(row["file"], str) or row["file"] not in hashes:
                errors.append(
                    f"Edit targets read-only or unknown file {row['file']!r}. "
                    "Regenerate edits using only the keys of editable_files; "
                    "related_files are context, not editable targets."
                )
            elif not isinstance(row["search"], str) or not row["search"]:
                errors.append("Search must contain an exact source fragment.")
            elif not isinstance(row["replacement"], str):
                errors.append("Replacement must be text.")
        return errors


class FrontendImplementationAgent(ImplementationAgent):
    def __init__(
        self,
        model: JsonModel,
        output_root: Path,
        *,
        retries: int = 2,
        max_context_characters: int = 600_000,
        trace: Callable[[str], None] | None = None,
        model_log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(
            model, output_root, retries=retries,
            max_context_characters=max_context_characters, trace=trace,
            model_log=model_log,
        )
        self._allowed_kinds = {"COMPONENT", "PAGE", "LAYOUT", "STORE", "API_CLIENT"}
        self._agent = BaseStructuredAgent(
            model,
            schema_name="arc_frontend_implementation_patch",
            instructions=FRONTEND_IMPLEMENTATION_INSTRUCTIONS,
            output_schema=IMPLEMENTATION_OUTPUT_SCHEMA,
            retries=retries,
            trace=trace,
            model_log=model_log,
            agent_name="FrontendImplementationAgent",
        )
