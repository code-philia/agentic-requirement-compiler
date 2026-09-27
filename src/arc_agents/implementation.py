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
the current child requirement's owned source files; do not edit other owners' files.
The requirement is the source of behavior. Read the complete related source files for
existing signatures, imports, exports, types and dependencies. If test_output is
present, it is the unmodified output of the test/build command, not a diagnosis.
Use the supplied test source only to understand the expected behavior; do not edit it.
Do not invent paths, change tests or edit read-only files. Preserve existing public
interfaces, routes and generated glue. Return only JSON with exact file/search/replacement
edits. Copy search verbatim from a unique fragment of the current editable file.
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
            agent_name="ImplementationAgent",
        )

    def implement(self, request: ImplementationRequest) -> ImplementationResult:
        requirement_id = str(request.requirement_id).strip()
        try:
            context, hashes = self._context(request)
        except (KeyError, ValueError, OSError, UnicodeError) as exc:
            return ImplementationResult(requirement_id, "CONTEXT_REJECTED", errors=[str(exc)])
        invocation = self._agent.invoke(
            context,
            validate=lambda output: self._validate(output, hashes),
        )
        if not invocation.ok or invocation.output is None:
            return ImplementationResult(
                requirement_id, "MODEL_REJECTED",
                attempts=invocation.attempts, errors=invocation.errors,
            )
        edits = tuple(
            ProposedEdit(
                file=row["file"], expected_sha256=hashes[row["file"]],
                search=row["search"], replacement=row["replacement"],
            )
            for row in invocation.output["edits"]
        )
        return ImplementationResult(
            requirement_id, "PATCH_PROPOSED",
            patch=ProposedPatch(requirement_id=requirement_id, edits=edits),
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
        if requested - {row["module_id"] for row in owned}:
            raise ValueError("Requested target is not writable by this requirement and agent.")
        if not owned:
            raise ValueError("No writable source files for this requirement.")
        editable_paths = {row["file"] for row in owned}
        dependencies = [*resolved["dependency_targets"], *resolved["type_targets"]]
        paths = sorted(editable_paths | {
            str(row.get("file", "")) for row in dependencies if row.get("file")
        })
        related = set(paths)
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
            "requirement": request.requirement,
            "editable_files": editable_files,
            "related_files": related_files,
        }
        if request.frontend_ir is not None:
            frontend_ir = request.frontend_ir
            target_ids = {str(row.get("source_ir_id", row.get("module_id", ""))) for row in owned}
            runtime_ir = project_frontend_runtime_ir(frontend_ir)
            frontend_modules = [
                row for table in ("pages", "components", "layouts")
                for row in runtime_ir.get(table, [])
                if isinstance(row, dict) and str(row.get("id", "")) in target_ids
            ]
            screens = [
                row for row in frontend_ir.get("screens", [])
                if isinstance(row, dict) and (
                    str(row.get("id", "")) in target_ids
                    or requirement_id in row.get("requirement_ids", [])
                )
            ]
            screen_ids = {str(row.get("id", "")) for row in screens}
            components = [
                row for row in frontend_ir.get("screen_components", [])
                if isinstance(row, dict) and str(row.get("screen_id", "")) in screen_ids
            ]
            visual_ids = {
                str(visual_id)
                for row in [*screens, *components, *frontend_modules]
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
            elif row["file"] not in hashes:
                errors.append("Edit refers to a read-only or unknown file.")
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
    ) -> None:
        super().__init__(
            model, output_root, retries=retries,
            max_context_characters=max_context_characters, trace=trace,
        )
        self._allowed_kinds = None
        self._agent = BaseStructuredAgent(
            model,
            schema_name="arc_frontend_implementation_patch",
            instructions=FRONTEND_IMPLEMENTATION_INSTRUCTIONS,
            output_schema=IMPLEMENTATION_OUTPUT_SCHEMA,
            retries=retries,
            trace=trace,
            agent_name="FrontendImplementationAgent",
        )
