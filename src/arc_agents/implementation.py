from __future__ import annotations

import copy
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
Dependency ownership does not prohibit edits when the file is in editable_files.
Preserve dependency public contracts and behavior used by other requirements.
The requirement is the source of behavior. Read the complete related source files for
existing signatures, imports, exports, types and dependencies. If test_output is
present, it is the unmodified output of the test/build command, not a diagnosis.
Tests are read-only unless test_correction explicitly authorizes a listed file after
failure diagnosis. In that case correct only the evidenced test defect against the
requirement and public contracts. Preserve scenarios, meaningful assertions and test
isolation; never skip/delete tests, accept erroneous behavior, or mock away the public
seam to obtain a pass. Fix implementation defects as well when diagnosis is MIXED.
Do not invent paths or edit read-only files. Preserve existing public
interfaces, routes and generated glue. Return only JSON with exact file/search/replacement
edits. Copy search verbatim from a unique fragment of the current editable file.
The keys of editable_files are the complete write allowlist for this invocation.
related_files and paths mentioned only in frontend_design are read-only context.
api_contracts fixes the HTTP method, path and input_source: use query for query inputs
and body for body inputs in both handlers and test requests. Do not infer the method
from a function name or change generated routes to accommodate an incorrect test.
If a screen is only in related_files, implement its owned components now; the page
will be handled in its own subsequent invocation.
"""

FAILURE_DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "reason", "test_errors"],
    "properties": {
        "verdict": {"type": "string", "enum": ["IMPLEMENTATION", "TEST", "MIXED", "DESIGN", "UNKNOWN"]},
        "reason": {"type": "string"},
        "test_errors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "test_evidence", "contract_evidence", "correction"],
            "properties": {key: {"type": "string"} for key in
                           ("file", "test_evidence", "contract_evidence", "correction")},
        }},
    },
}

FAILURE_DIAGNOSIS_INSTRUCTIONS = """Diagnose this failed test batch before editing anything.
Compare the original requirement and scenarios, api_contracts, generated router,
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
"""

FRONTEND_IMPLEMENTATION_INSTRUCTIONS = IMPLEMENTATION_INSTRUCTIONS + """
Implement every supplied editable frontend screen and component, including their layout,
navigation, accessible controls, empty/loading/error states, and shared visual language.
Do not leave any 'Implementation pending' skeletons, even if E2E tests do not visit them.
Replace the compiler's default slate shell and remove data-arc-page,
data-arc-component, data-arc-layout, and data-arc-obligation skeleton markers.
Use frontend_design screens, editable_modules, components, and visual reference
analyses as design requirements:
match their composition, colors, typography, spacing, and prominent controls as closely
as the supplied evidence allows. Reuse the reference style across related screens that
do not have their own image. Passing E2E assertions alone is not completion.
Only edit supplied editable files; preserve component contracts and existing behavior.
Treat editable_modules.component_ids as composition contracts: implement child modules
first, place shared header/navigation/footer in their page regions, and do not recreate
their markup beside a decorative or empty child invocation. A CONTENT_SLOT component
owns the shared presentation and renders its children; the page supplies its actual
form fields and submit behavior inside that component, never as a sibling form.

For the current seven-entity frontend IR, frontend_design.components is the complete
component scope visible to this requirement and frontend_design.ui is the render tree
that each component must return. properties and data are the state, ref, derived and
input/output contracts; events, handlers and effects are the behavior contracts.
Implement the return layout from the UI records and supplied visual analysis: honor
children, condition, repeat, attributes, text and arguments, and pass declared data to
child components. Implement event wiring and handler/effect logic from reads, writes,
invokes, emits, target, dependencies, activation and cleanup. Add a local React
STATE, REF, DERIVED value or boundary data adapter only when the requirement or
visual evidence truly needs it, and keep it local to the writable component; do not
invent a new global store. The requirement's associated
components and their associated UI/behavior entities are the writable design scope;
reuse an existing component or UI node instead of creating a duplicate. Preserve the
existing typed props and component exports while replacing lowering placeholders.
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
            diagnosis = self._diagnosis_agent.invoke(
                {**context, "candidate_test_files": list(request.test_files)},
                validate=lambda output: self._validate_diagnosis(output, request),
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
            if decision["test_errors"]:
                context["test_correction"] = decision["test_errors"]
                for defect in decision["test_errors"]:
                    relative = defect["file"]
                    source = context["related_files"].pop(relative)
                    context["editable_files"][relative] = source
                    hashes[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
                context["writable_file_paths"] = sorted(hashes)
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
        from compiler.frontend_thin_design import project_frontend_runtime_ir

        resolved = CodeTargetResolver(request.code_binding_registry).resolve_requirement_targets(
            request.requirement_id
        )
        requested = set(request.target_module_ids)
        owned = [
            row for row in resolved["owned_targets"]
            if (not requested or row["module_id"] in requested)
            and (self._allowed_kinds is None or row["kind"] in self._allowed_kinds)
        ]
        owned.extend(
            row for row in resolved["dependency_targets"]
            if (self._allowed_kinds is None or row["kind"] in self._allowed_kinds)
            and row["module_id"] not in {item["module_id"] for item in owned}
        )
        if requested - {row["module_id"] for row in owned}:
            raise ValueError("Requested target is not writable by this requirement and agent.")
        if not owned and not (request.test_layer and request.test_files):
            raise ValueError("No writable source files for this requirement.")
        editable_paths = {row["file"] for row in owned}
        dependencies = [*resolved["dependency_targets"], *resolved["type_targets"]]
        paths = sorted(editable_paths | {
            str(row.get("file", "")) for row in dependencies if row.get("file")
        })
        related = set(paths)
        router = "backend/src/generated/router.ts"
        if (self.output_root / router).is_file():
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
        context = {
            "requirement_id": request.requirement_id,
            "iteration": request.iteration,
            "iteration_limit": request.iteration_limit,
            "test_layer": request.test_layer,
            "implementation_mode": "frontend" if request.frontend_ir is not None else "backend",
            "requirement": request.requirement,
            "editable_files": editable_files,
            "writable_file_paths": sorted(editable_files),
            "related_files": related_files,
            "api_contracts": [copy.deepcopy(row) for row in
                              request.code_binding_registry.get("code_bindings", [])
                              if row.get("kind") == "API"],
        }
        if request.frontend_ir is not None and "root_component_id" in request.frontend_ir:
            from compiler.frontend_context import frontend_subgraph, visual_context
            context["frontend_design"] = frontend_subgraph(
                request.frontend_ir, request.requirement_id,
                {str(row.get("module_id", "")) for row in owned})
            context["frontend_design"]["editable_modules"] = copy.deepcopy(owned)
            editable_ids = {
                str(row.get("module_id", "")) for row in owned if row.get("module_id")
            }
            context["frontend_design"]["editable_component_ids"] = sorted(
                editable_ids & {
                    str(row.get("id", ""))
                    for row in context["frontend_design"].get("components", [])
                }
            )
            context["frontend_design"]["editable_scope"] = (
                "Only components in editable_component_ids and their associated UI, "
                "properties, events, handlers and effects may be changed; other rows "
                "are read-only dependencies."
            )
            context["frontend_design"]["visual_references"] = visual_context(self.output_root, request.requirement_id)
            context["frontend_design"]["implementation_guidance"] = (
                "Implement the seven-entity IR: component return bodies and composition, UI/data bindings, "
                "events, handlers and effects. Replace TODO and Not implemented placeholders. "
                "The generated React app starts at /; reach conditional pages through designed interactions, "
                "not invented routes. Preserve typed contracts and edit only writable files.")
        elif request.frontend_ir is not None:
            frontend_ir = request.frontend_ir
            target_ids = {str(row.get("source_ir_id", row.get("module_id", ""))) for row in owned}
            runtime_ir = project_frontend_runtime_ir(frontend_ir)
            frontend_modules = [
                row for table in ("pages", "components", "layouts")
                for row in runtime_ir.get(table, [])
                if isinstance(row, dict) and str(row.get("id", "")) in target_ids
            ]
            child_ids = {
                str(child_id)
                for row in frontend_modules
                for child_id in row.get("component_ids", [])
            }
            component_contracts = [
                row for row in runtime_ir.get("components", [])
                if isinstance(row, dict) and str(row.get("id", "")) in child_ids
            ]
            screens = [
                row for row in frontend_ir.get("screens", [])
                if isinstance(row, dict) and (
                    str(row.get("id", "")) in target_ids
                    or request.requirement_id in row.get("requirement_ids", [])
                )
            ]
            screen_ids = {str(row.get("id", "")) for row in screens}
            components = [
                row for row in frontend_ir.get("screen_components", [])
                if isinstance(row, dict) and str(row.get("screen_id", "")) in screen_ids
            ]
            visual_ids = {
                str(visual_id)
                for row in [*screens, *components, *frontend_modules, *component_contracts]
                for visual_id in row.get("visual_reference_ids", [])
            }
            if not visual_ids:
                visual_ids = {
                    str(row.get("id", ""))
                    for row in frontend_ir.get("visual_references", [])[:4]
                    if isinstance(row, dict) and row.get("id")
                }
            context["frontend_design"] = {
                "screens": copy.deepcopy(screens),
                "screen_components": copy.deepcopy(components),
                "editable_modules": copy.deepcopy(frontend_modules),
                "child_component_contracts": copy.deepcopy(component_contracts),
                "visual_references": [
                    {
                        "id": row.get("id"),
                        "source_path": row.get("source_path"),
                        "analysis": copy.deepcopy(row.get("analysis", {})),
                    }
                    for row in frontend_ir.get("visual_references", [])
                    if isinstance(row, dict) and str(row.get("id", "")) in visual_ids
                ],
            }
        if request.test_output:
            context["test_output"] = request.test_output
        if len(str(context)) > self._max_context_characters:
            raise ValueError("Implementation context exceeds its size limit.")
        return context, hashes

    @staticmethod
    def _validate_diagnosis(output: dict[str, Any], request: ImplementationRequest) -> list[str]:
        if not isinstance(output, dict) or set(output) != {"verdict", "reason", "test_errors"}:
            return ["Return verdict, reason and test_errors."]
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
        writable_paths = sorted(hashes)
        for row in edits:
            if not isinstance(row, dict) or set(row) != {"file", "search", "replacement"}:
                errors.append("Each edit needs file, search and replacement.")
            elif not isinstance(row["file"], str) or row["file"] not in hashes:
                errors.append(
                    f"Edit targets read-only or unknown file {row['file']!r}. "
                    f"Regenerate edits using only writable_file_paths={writable_paths!r}; "
                    "related_files and frontend_design paths are context, not editable targets."
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
        self._allowed_kinds = None
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
