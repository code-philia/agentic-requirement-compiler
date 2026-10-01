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

DIRECT_IMPLEMENTATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["edits", "create_files"],
    "properties": {
        "edits": IMPLEMENTATION_OUTPUT_SCHEMA["properties"]["edits"],
        "create_files": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "content"],
            "properties": {"file": {"type": "string"}, "content": {"type": "string"}},
        }},
    },
}


IMPLEMENTATION_INSTRUCTIONS = """Implement the supplied requirement in the editable source files.
Dependencies are read-only unless explicitly included in editable_files for this task.
Preserve dependency public contracts and behavior used by other requirements.
The requirement is the source of behavior. Read the complete related source files for
existing signatures, imports, exports, types and dependencies.
This is the initial implementation stage. All supplied tests are read-only.
implementation_feedback is compiler guidance, not a test execution result.
Do not invent paths or edit read-only files. Preserve existing public
interfaces, routes and generated glue. Return only JSON with exact file/search/replacement
edits. Copy search verbatim from a unique fragment of the current editable file.
The keys of editable_files are the complete write allowlist for this invocation.
related_files are read-only context.
If planned_units is supplied, implement each unit in its declared file and export
the declared export_name. Do not move a unit to another file or merge units into
one file. Update only the relevant entry-point imports/exports and route wiring.
The supplied router and related source files define the HTTP method, path and input source: use query for query inputs
and body for body inputs in both handlers and test requests. Do not infer the method
from a function name or change generated routes to accommodate an incorrect test.
When a page and its owned components are both editable, implement their composition
and behavior together in this invocation. Read-only screens remain in related_files.
Maintain a one-file-per-unit source layout. Each independent backend module,
service, API handler, or interface belongs in its own source file. Each React
component or page belongs in its own .tsx/.jsx file. Do not append unrelated
components, pages, handlers, or modules to an existing file. Entry points,
barrel files, app files, and routers may only import/export units and compose
routes; put the implementation in the unit file itself. Preserve named exports,
default exports, and relative import paths, and update imports/exports whenever
you split or reuse a unit. Never create duplicate symbols in the same module.
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
Keep every page and component in its own file. A router, app shell, or barrel file
must contain only imports, exports, and composition; it must not contain the body
of a page or an unrelated component. Keep each frontend hook/store/service unit
in its own file when it has an independent responsibility.
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
    test_files: tuple[str, ...] = ()
    frontend_ir: dict[str, Any] | None = None
    iteration: int = 0
    implementation_feedback: str = ""
    direct_mode: bool = False


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
        # The regular IR pipeline uses DB/FUNC/API.  The without-IR planner
        # additionally names source units explicitly (SERVICE/HANDLER/STORE/
        # TYPE), so those kinds must remain writable when the direct registry
        # is supplied.  The file allowlist is still enforced below.
        self._allowed_kinds: set[str] | None = {
            "DB", "FUNC", "API", "SERVICE", "HANDLER", "STORE", "TYPE",
        }
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
            ), create_files=tuple(
                (str(row["file"]), str(row["content"]))
                for row in output.get("create_files", [])
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
        from compiler.file_context import requirement_dependencies, file_selection_log

        if request.direct_mode:
            return self._direct_context(request)
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
        candidates = [*resolved["owned_targets"], *resolved["dependency_targets"]]
        dependencies = requirement_dependencies(owned, candidates, request.requirement_id, request.frontend_ir)
        type_ids = {reference["type_id"] for row in [*owned, *dependencies]
                    for reference in [row.get("input_type"), row.get("output_type"), row.get("props_type"),
                                      *row.get("store_types", [])]
                    if isinstance(reference, dict) and reference.get("type_id")}
        dependencies.extend(row for row in resolved["type_targets"] if row["type_id"] in type_ids)
        paths = sorted(editable_paths | {
            str(row.get("file", "")) for row in dependencies if row.get("file")
        })
        related = set(paths)
        module_files = {row["file"] for row in candidates if row.get("file")}
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
                    if candidate in module_files and candidate not in related:
                        continue  # Imports must not bypass requirement-scoped module selection.
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
            "implementation_mode": "frontend" if request.frontend_ir is not None else "backend",
            "requirement": requirement,
            "editable_files": editable_files,
            "related_files": related_files,
        }
        direct_units = request.code_binding_registry.get("direct_units", {}).get(request.requirement_id)
        if direct_units:
            context["planned_units"] = direct_units
        if request.implementation_feedback:
            context["implementation_feedback"] = request.implementation_feedback
        context_chars = len(str(context))
        if context_chars > self._max_context_characters:
            largest = sorted(((path, len(source)) for path, source in
                              {**related_files, **editable_files}.items()),
                             key=lambda item: (-item[1], item[0]))[:5]
            raise ValueError(
                f"Implementation context exceeds its size limit: {context_chars} > "
                f"{self._max_context_characters} characters; largest files={largest!r}.")
        return context, hashes

    def _direct_context(self, request: ImplementationRequest) -> tuple[dict[str, Any], dict[str, str]]:
        """Expose all application source to the without-IR full-stack agent."""
        roots = ("backend/src/", "frontend/src/", "shared/src/")
        files: dict[str, str] = {}
        for root in roots:
            base = self.output_root / root
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"}:
                    relative = path.relative_to(self.output_root).as_posix()
                    files[relative] = path.read_text(encoding="utf-8")
        hashes = {path: hashlib.sha256(source.encode("utf-8")).hexdigest() for path, source in files.items()}
        source_index = []
        for path, source in files.items():
            imports = sorted(set(re.findall(r'(?:from|import)\s+["\']([^"\']+)["\']', source)))[:20]
            symbols = sorted(set(re.findall(r'\b(?:export\s+)?(?:async\s+)?(?:function|class|const|type|interface)\s+([A-Za-z_$][\w$]*)', source)))[:40]
            source_index.append({"path": path, "symbols": symbols, "imports": imports,
                                 "summary": " ".join(line.strip() for line in source.splitlines()
                                                     if line.strip() and not line.lstrip().startswith(("//", "/*", "*")))[:500]})
        test_sources: dict[str, str] = {}
        for value in request.test_files:
            relative = str(value).replace(chr(92), "/").strip()
            parts = relative.split("/")
            if not relative or relative.startswith("/") or "." in parts or ".." in parts:
                continue
            path = (self.output_root / Path(*parts)).resolve()
            if self.output_root not in path.parents or not path.is_file():
                continue
            try:
                test_sources[relative] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
        context = {
            "requirement_id": request.requirement_id,
            "iteration": request.iteration,
            "implementation_mode": "direct_full_stack",
            "requirement": request.requirement,
            "editable_files": files,
            "related_files": {},
            "source_index": source_index,
            "test_files": list(request.test_files),
            "test_sources": test_sources,
            "file_creation_allowed": True,
        }
        if request.implementation_feedback:
            context["implementation_feedback"] = request.implementation_feedback
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
        if not isinstance(output, dict) or set(output) not in ({"edits"}, {"edits", "create_files"}):
            return ["Return edits and create_files only."]
        edits = output["edits"]
        creates = output.get("create_files", [])
        if not isinstance(edits, list) or not isinstance(creates, list) or (not edits and not creates):
            return ["Return at least one edit or created source file."]
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
        for row in creates:
            if not isinstance(row, dict) or set(row) != {"file", "content"}:
                errors.append("Each create_files item needs file and content.")
            elif not isinstance(row["file"], str) or not isinstance(row["content"], str) or not row["content"].strip():
                errors.append("Created source files need a non-empty file and content.")
        return errors


class DirectImplementationAgent(ImplementationAgent):
    """Single full-stack implementation call used by the without-IR ablation."""

    def __init__(self, model: JsonModel, output_root: Path, **kwargs: Any) -> None:
        super().__init__(model, output_root, **kwargs)
        self._allowed_kinds = None
        self._agent = BaseStructuredAgent(
            model, schema_name="arc_direct_implementation_patch",
            instructions=(IMPLEMENTATION_INSTRUCTIONS + """
This is a direct full-stack task. You may modify any application source file under
backend/src, frontend/src, or shared/src, and may create new source modules using
create_files. Keep one independent module, service, handler, page, or component per
file. Update imports, exports, server registration, and route composition in the
same response. Tests, node_modules, lockfiles, and package metadata are read-only.
Use `requirement`, `test_files`, and `source_index` to select files: first identify
the public API, route, component, or symbol exercised by the current requirement's
tests, then inspect the corresponding test source in `test_sources` and full source in `editable_files`. Do not edit
unrelated files merely because they are visible. If no existing file owns the
behavior, create the smallest new module and update the relevant entry point.
Return create_files for new files and edits for existing files; do not put file
contents in an edit replacement when creating a file.
"""),
            output_schema=DIRECT_IMPLEMENTATION_OUTPUT_SCHEMA,
            retries=2, trace=kwargs.get("trace"), model_log=kwargs.get("model_log"),
            agent_name="DirectImplementationAgent",
        )


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
