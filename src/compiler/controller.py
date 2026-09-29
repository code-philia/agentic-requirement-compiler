from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from arcbench_agent_runtime.runtime import AgentRuntime

from .artifacts import CompilerArtifactStore
from .checkpoints import CheckpointStore, START_FROM
from .backend_lowering import BackendGlueLowerer
from .code_binding import CodeTargetResolver, CodeBindingLowerer
from .database_stage import (
    DatabaseSchemaPass,
    database_traceability,
)
from .database_lowering import lower_database
from .design_stage import DesignPass, design_traceability
from .file_planning import GlobalFilePlanner
from .frontend_generation import FrontendIRGenerationPass, frontend_ir_traceability
from .frontend_react_lowering import FrontendReactLowerer
from .git_history import GitStageError, ProjectGitHistory
from .fixture_stage import (
    FixturePass,
)
from .fixture_lowering import fixture_source_paths
from .preprocessing_stage import RequirementPreprocessor, PreprocessingResult
from .model_client import Model, ModelConfigurationError, StructuredModel
from .models import CompilationRequest, CompilationResult
from .module_lowering import ModuleSkeletonLowerer
from .project_build import ProjectBuilder
from .project_initialization import (
    ProjectInitializer,
    validate_frontend_environment,
)
from .skeleton_lowering import TypeLowerer
from .symbol_planning import GlobalSymbolPlanner
from .tdd_orchestrator import NodeTDDOrchestrator
from .tdd_progress import TDDProgress, load_tdd_manifest
from .test_generation import RequirementTestGenerationPass, TestEnvironmentInitializer
from .test_runner import TestRunner
from .visual_reference import VisualReferenceResolver, VisualReferenceAnalyzer


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


def _tdd_postorder(requirement_ir: dict[str, Any]) -> list[str]:
    """Schedule the requirement tree in document order, without dependency waves."""
    nodes = requirement_ir.get("nodes", {})
    root = requirement_ir.get("root_id")
    targets = set(requirement_ir.get("atomic_units", [])) | set(requirement_ir.get("folder_nodes", []))
    visited: set[str] = set()
    active: set[str] = set()
    order: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in active:
            raise ValueError(f"Requirement tree cycle at {node_id}")
        if node_id in visited:
            return
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            raise ValueError(f"Unknown requirement tree node {node_id}")
        active.add(node_id)
        for child_id in node.get("children_ids", []):
            visit(child_id)
        active.remove(node_id)
        visited.add(node_id)
        if node_id in targets:
            order.append(node_id)

    visit(root)
    if missing := targets - visited:
        raise ValueError(f"Requirements unreachable from root: {sorted(missing)}")
    return order


class Compiler:
    """Compile requirements through deterministic, validated passes."""

    def __init__(
        self,
        runtime: AgentRuntime,
        log_cb: LogCallback,
        model: StructuredModel | None = None,
    ) -> None:
        self._runtime = runtime
        self._log_cb = log_cb
        self._preprocessor = RequirementPreprocessor()
        self._model = model

    async def compile(self, request: CompilationRequest) -> CompilationResult:
        try:
            return await self._compile(request)
        except GitStageError as exc:
            await self._log("Compiler", str(exc), "error")
            return CompilationResult(ok=False)

    async def _compile(self, request: CompilationRequest) -> CompilationResult:
        artifact_store = CompilerArtifactStore(request.output_dir)
        artifacts: dict[str, str] = {}
        history = ProjectGitHistory(request.output_dir)
        if request.start_from not in START_FROM:
            raise GitStageError(f"Unknown start_from: {request.start_from}")
        if request.resume and request.start_from != "zero":
            raise GitStageError("resume and start_from are mutually exclusive")
        rank = 5 if request.resume else START_FROM.index(request.start_from)
        checkpoints = CheckpointStore(request.output_dir, request.requirement_path, request.web_port)
        saved = checkpoints.load("lowered" if request.resume else request.start_from, resume=request.resume) if rank else {}
        if rank:
            artifacts["checkpoint"] = str(request.output_dir / ".arc/checkpoints/current.json")
            for name, relative in {
                "project_manifest": ".arc/project/project-manifest.json",
                "database_schema": ".arc/design/database/schema.json", "fixture_ir": ".arc/design/database/fixture_ir.json",
                "design_ir": ".arc/design/backend/modules.json", "frontend_design_ir": ".arc/design/frontend/components.json",
                "frontend_traceability": ".arc/design/frontend/requirements.json",
                "frontend_lowering_report": ".arc/lowering/frontend/lowering.json",
            }.items():
                if (request.output_dir / relative).is_file():
                    artifacts[name] = str(request.output_dir / relative)
            await self._log("Compiler", "Resuming unstarted TDD requirements in the existing project."
                            if request.resume else f"Starting after completed checkpoint: {request.start_from}")

        if rank == 0:
            self._runtime.events.mark_phase_started("PROJECT", "Initializing generated project.")
            project = ProjectInitializer(request.output_dir, web_port=request.web_port).initialize()
            artifacts.update(project.artifacts)
            if not project.ok:
                for error in project.errors:
                    await self._log("Compiler", error, "error")
                return CompilationResult(ok=False, artifacts=artifacts)
        project_manifest, project_error = artifact_store.read_project_manifest()
        if project_error or project_manifest is None:
            await self._log("Compiler", project_error or "Missing project manifest.", "error")
            return CompilationResult(ok=False, artifacts=artifacts)
        frontend_errors = validate_frontend_environment(request.output_dir, project_manifest)
        if frontend_errors:
            for error in frontend_errors:
                await self._log("Compiler", error, "error")
            return CompilationResult(ok=False, artifacts=artifacts)
        if rank == 0:
            try:
                history.initialize()
                history.commit("0 project initialization", [
                    ".gitignore", ".env.example", "README.md", "package.json", "package-lock.json",
                    "backend", "frontend", "shared", "tests", ".arc/project",
                ])
            except GitStageError as exc:
                await self._log("Compiler", str(exc), "error")
                return CompilationResult(ok=False, artifacts=artifacts)

        if rank == 0:
            checkpoints.save("initialized")

        # ===================================================================
        #                    Requirement Preprocessing Stage
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic PREPROCESSING pass." if rank <= 1 else "Restoring checkpoint preprocessing artifacts.",
        )
        preprocessing = (self._preprocessor.compile(request.requirement_path) if rank <= 1 else
                         PreprocessingResult(**saved["preprocessing"]))
        root_id = preprocessing.requirement_ir.get("root_id") if preprocessing.requirement_ir else None
        atomic_ids = list(preprocessing.requirement_ir.get("atomic_units", [])) if preprocessing.requirement_ir else []
        requirement_ids = (
            list(preprocessing.requirement_ir.get("node_order", []))
            if preprocessing.requirement_ir
            else []
        )
        states = {
            node_id: ("DISCOVERED" if preprocessing.ok else "FAILED")
            for node_id in requirement_ids
        }

        artifacts.update(artifact_store.write_preprocessing(
            requirement_ir=preprocessing.requirement_ir,
            dependency_graph=preprocessing.dependency_graph,
        ))

        if requirement_ids:
            self._runtime.traceability.store_requirement_ids(requirement_ids)

        for error in preprocessing.errors:
            await self._log("Compiler", error, "error")

        if not preprocessing.ok:
            await self._log("Compiler", "PREPROCESSING pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=requirement_ids,
                artifacts=artifacts,
            )
        if rank <= 1:
            history.commit("1 preprocessing", [".arc"])
        if rank == 5:
            self._runtime.traceability.merge_database_schema_links(database_traceability(saved["database"]))
            self._runtime.traceability.merge_design_links(design_traceability(saved["design"]))
            self._runtime.traceability.merge_frontend_design_links(frontend_ir_traceability(saved["frontend"]["frontend_ir"]))
            for rid in requirement_ids:
                states[rid] = "FRONTEND_LOWERED"
            failure = None if request.resume else await self._build_gate(request, "Restart from lowered", root_id, states, artifacts)
            if failure is not None:
                return failure
            return await self._continue_after_lowering(
                request=request, artifact_store=artifact_store, preprocessing=preprocessing,
                database_schema=saved["database"], design_ir=saved["design"],
                frontend_ir=saved["frontend"]["frontend_ir"], backend_routes=saved["backend_routes"],
                fixture_ir=saved["fixture_ir"], project_manifest=project_manifest,
                root_id=root_id, states=states, artifacts=artifacts,
            )

        # ===================================================================
        #                    Compiler Database Stage
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "DATABASE", "ARC database stage started."
        )

        await self._log("Compiler", "Running database schema design passes." if rank < 2 else "Restoring checkpoint database schema.")
        try:
            model = self._model or Model.from_env()
            if hasattr(model, "set_usage_path"):
                model.set_usage_path(artifact_store.root / "model_usage.jsonl")
        except ModelConfigurationError as exc:
            await self._log("Compiler", str(exc), "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=atomic_ids,
                artifacts=artifacts,
            )
        if rank < 2:
            database_stage = DatabaseSchemaPass(model, artifact_store.root)
            database = database_stage.compile(
                preprocessing.requirement_ir,
                preprocessing.dependency_graph,
            )
            states.update(database.node_states)
            links = database_traceability(database.schema)
            for error in database.errors:
                await self._log("Compiler", error, "error")
            for warning in database.warnings:
                await self._log("Compiler", warning, "warning")
            if not database.ok:
                failed_nodes = sorted(node_id for node_id, state in states.items() if state == "FAILED")
                await self._log("Compiler", "DATABASE_SCHEMA pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=failed_nodes,
                    artifacts=artifacts,
                )
            artifacts["database_schema"] = artifact_store.write_database_schema(database.schema)
            self._runtime.traceability.merge_database_schema_links(links)
            history.commit("2.1 database schema design", [".arc"])

        else:
            database = SimpleNamespace(schema=saved["database"])
            self._runtime.traceability.merge_database_schema_links(database_traceability(database.schema))

        if rank < 2:
            checkpoints.save("database", preprocessing={
                "requirement_ir": preprocessing.requirement_ir,
                "dependency_graph": preprocessing.dependency_graph,
            }, database=database.schema)

        # ===================================================================
        #                 Database Lowering: Seed Fixtures
        # ===================================================================

        if rank < 3:
            fixture_ir: dict[str, Any]
            fixture_result = FixturePass(model, artifact_store.root).compile(
                preprocessing.requirement_ir,
                database.schema,
            )
            for error in fixture_result.errors:
                await self._log("Compiler", error, "error")
            if not fixture_result.ok:
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            fixture_ir = fixture_result.fixture_ir
            artifacts["fixture_ir"] = artifact_store.write_fixture_ir(fixture_ir)

            lowered_database = lower_database(
                request.output_dir, artifact_store, database.schema, fixture_ir, project_manifest,
            )
            artifacts.update(lowered_database.artifacts)
            for warning in lowered_database.warnings:
                await self._log("Compiler", warning, "warning")
            for error in lowered_database.errors:
                await self._log("Compiler", error, "error")
            if not lowered_database.ok:
                return CompilationResult(
                    ok=False, root_id=root_id, states=states, artifacts=artifacts,
                )
            history.commit("2.2 database schema lowering", [
                ".arc", "backend/src/db", "backend/src/fixtures", "backend/init-db.mjs",
                "backend/database-baseline.mjs", "backend/database-baseline.d.mts", "shared/src",
            ])
            failed_gate = await self._build_gate(request, "2.2 database schema lowering", root_id, states, artifacts)
            if failed_gate is not None:
                return failed_gate

        else:
            fixture_ir = saved["fixture_ir"]
            lowered_database = SimpleNamespace(manifest=saved["database_manifest"])

        # ===================================================================
        #                    Compiler Design Stage
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "DESIGN", "ARC backend design stage started."
        )

        if rank < 3:
            design_stage = DesignPass(model, artifact_store.root)
            await self._log(
                "Compiler",
                "Running REQUIREMENT CONTRACT, REQUIREMENT TO API, and full top-down MODULE DECOMPOSITION passes.",
            )
            design = design_stage.compile(
                preprocessing.requirement_ir,
                preprocessing.dependency_graph,
                database.schema,
            )
            states.update(design.node_states)
            for error in design.errors:
                await self._log("Compiler", error, "error")
            for warning in design.warnings:
                await self._log("Compiler", warning, "warning")
            artifacts.update(artifact_store.write_design(design_ir=design.design_ir))
            if not design.ok:
                failed_nodes = sorted(node_id for node_id, state in states.items() if state == "FAILED")
                await self._log("Compiler", "DESIGN pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=failed_nodes,
                    artifacts=artifacts,
                )

            self._runtime.traceability.merge_design_links(design_traceability(design.design_ir))
            history.commit("3.1 backend design", [".arc"])

        else:
            design = SimpleNamespace(design_ir=saved["design"])
            self._runtime.traceability.merge_design_links(design_traceability(design.design_ir))

        if rank < 3:
            checkpoints.save("backend-ir", fixture_ir=fixture_ir,
                             database_manifest=lowered_database.manifest, design=design.design_ir)

        # ===================================================================
        #                  Backend Lowering: Symbol Planning
        # ===================================================================

        if rank < 4:
            self._runtime.events.mark_phase_started(
                "SKELETON", "ARC skeleton lowering stage started."
            )

            await self._log(
                "Compiler",
                "Running deterministic GLOBAL_SYMBOL_PLANNING over Design IR and Database Schema IR.",
            )
            symbol_planning = GlobalSymbolPlanner().plan(
                design.design_ir,
                database.schema,
                project_manifest,
            )
            for error in symbol_planning.errors:
                await self._log("Compiler", error, "error")
            if not symbol_planning.ok:
                await self._log("Compiler", "GLOBAL_SYMBOL_PLANNING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            await self._log(
                "Compiler",
                "Global Symbol Registry planned.",
            )

            # ===================================================================
            #                   Backend Lowering: File Planning
            # ===================================================================

            await self._log(
                "Compiler",
                "Running deterministic GLOBAL_FILE_PLANNING over Design IR and the Symbol Registry.",
            )
            file_planning = GlobalFilePlanner(request.output_dir).plan(
                design.design_ir,
                symbol_planning.registry,
                project_manifest,
                fixture_paths=fixture_source_paths(fixture_ir),
            )
            for error in file_planning.errors:
                await self._log("Compiler", error, "error")
            if not file_planning.ok:
                await self._log("Compiler", "GLOBAL_FILE_PLANNING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            await self._log(
                "Compiler",
                "Global File Registry planned.",
            )

            # ===================================================================
            #                    Backend Lowering: Type Lowering
            # ===================================================================

            await self._log(
                "Compiler",
                "Running deterministic TYPE_LOWERING for canonical TypeScript definitions.",
            )
            type_lowering = TypeLowerer().lower(
                symbol_planning.registry,
                file_planning.registry,
            )
            for error in type_lowering.errors:
                await self._log("Compiler", error, "error")
            if not type_lowering.ok:
                await self._log("Compiler", "TYPE_LOWERING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            artifacts.update(artifact_store.write_generated_sources({
                path: source for path, source in type_lowering.sources.items()
                if path.startswith("shared/src/")
            }))
            await self._log("Compiler", "Canonical TypeScript type world generated.")

            # ===================================================================
            #                Skeleton Stage 3.1: DB Module Lowering
            # ===================================================================

            await self._log(
                "Compiler",
                "Running deterministic DB_MODULE_LOWERING from frozen registries.",
            )
            module_lowerer = ModuleSkeletonLowerer()
            db_modules = module_lowerer.lower(
                "DB",
                design.design_ir,
                symbol_planning.registry,
                file_planning.registry,
            )
            for error in db_modules.errors:
                await self._log("Compiler", error, "error")
            if not db_modules.ok:
                await self._log("Compiler", "DB_MODULE_LOWERING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            artifacts.update(artifact_store.write_generated_sources(db_modules.sources))
            await self._log("Compiler", "DB Module Skeletons generated.")

            # ===================================================================
            #               Skeleton Stage 3.1: FUNC Module Lowering
            # ===================================================================

            await self._log(
                "Compiler",
                "Running deterministic FUNC_MODULE_LOWERING from frozen registries.",
            )
            func_modules = module_lowerer.lower(
                "FUNC",
                design.design_ir,
                symbol_planning.registry,
                file_planning.registry,
            )
            for error in func_modules.errors:
                await self._log("Compiler", error, "error")
            if not func_modules.ok:
                await self._log("Compiler", "FUNC_MODULE_LOWERING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            artifacts.update(artifact_store.write_generated_sources(func_modules.sources))
            await self._log("Compiler", "FUNC Module Skeletons generated.")

            # ===================================================================
            #                Skeleton Stage 3.1: API Module Lowering
            # ===================================================================

            await self._log(
                "Compiler",
                "Running deterministic API_MODULE_LOWERING from frozen registries.",
            )
            api_modules = module_lowerer.lower(
                "API",
                design.design_ir,
                symbol_planning.registry,
                file_planning.registry,
            )
            for error in api_modules.errors:
                await self._log("Compiler", error, "error")
            if not api_modules.ok:
                await self._log("Compiler", "API_MODULE_LOWERING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            artifacts.update(artifact_store.write_generated_sources(api_modules.sources))
            await self._log(
                "Compiler",
                "API Module Skeletons generated; global Glue Code generation is next.",
            )

            # ===================================================================
            #          Skeleton Stage 3.1: Global Glue and Backend Manifest
            # ===================================================================

            await self._log(
                "Compiler",
                "Running deterministic GLOBAL_GLUE_LOWERING with Route and Import Planning.",
            )
            backend_glue = BackendGlueLowerer().lower(
                design.design_ir,
                symbol_planning.registry,
                file_planning.registry,
                {
                    "type": type_lowering.manifest,
                    "database": lowered_database.manifest,
                    "DB": db_modules.manifest,
                    "FUNC": func_modules.manifest,
                    "API": api_modules.manifest,
                },
                default_port=request.web_port,
            )
            for error in backend_glue.errors:
                await self._log("Compiler", error, "error")
            if not backend_glue.ok:
                await self._log("Compiler", "GLOBAL_GLUE_LOWERING pass failed.", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    artifacts=artifacts,
                )
            artifacts.update(artifact_store.write_generated_sources(backend_glue.sources))
            artifacts.update(artifact_store.write_backend_lowering(
                report={
                    "status": "LOWERED",
                    "errors": list(backend_glue.errors),
                    "warnings": [],
                    "files": sorted(backend_glue.sources),
                    "manifest": backend_glue.manifest,
                    "import_plan": backend_glue.import_plan,
                    "route_registry": backend_glue.route_registry,
                },
                sources=backend_glue.sources,
                tables={
                    "symbols": symbol_planning.registry,
                    "files": file_planning.registry,
                    "types": type_lowering.manifest,
                    "database": lowered_database.manifest,
                    "modules_db": db_modules.manifest,
                    "modules_func": func_modules.manifest,
                    "modules_api": api_modules.manifest,
                    "routes": backend_glue.route_registry,
                    "imports": backend_glue.import_plan,
                },
            ))
            await self._log(
                "Compiler",
                "Global Glue Code, Route Registration, Barrel Export, Import Plan, and Backend Manifest generated.",
            )
            history.commit("3.2 backend lowering", [".arc/lowering/backend", "backend/src", "shared/src"])
            failed_gate = await self._build_gate(request, "3.2 backend lowering", root_id, states, artifacts)
            if failed_gate is not None:
                return failed_gate

        else:
            backend_glue = SimpleNamespace(route_registry=saved["backend_routes"])

        # ===================================================================
        #                 Compiler Frontend Design Stage
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "FRONTEND", "ARC frontend design stage started."
        )

        if rank < 4:
            await self._log("Compiler", "Resolving visual references for regional frontend design calls.")
            visuals = VisualReferenceResolver().resolve(
                request.requirement_path, preprocessing.requirement_ir,
            )
            visual_path = artifact_store.frontend_design_root / "visual_cache.json"
            visual_analysis = VisualReferenceAnalyzer.from_env(artifact_store.root).analyze_cached(
                visuals.references, visual_path,
            )
            if visual_path.is_file():
                artifacts["frontend_visual_references"] = str(visual_path)
            await self._log(
                "Compiler",
                "Generating frontend IR in three requirement-scheduled passes: UI/data, component assembly, behavior.",
            )
            frontend = FrontendIRGenerationPass(model, artifact_store.root).compile(
                preprocessing.requirement_ir,
                preprocessing.dependency_graph,
                design.design_ir,
                visuals.references,
                visual_analysis.references,
            )
            for issue in [*visuals.errors, *visual_analysis.errors]:
                frontend.report["warnings"].append(issue.format())
            if frontend.report["warnings"] and frontend.report["status"] == "GENERATED":
                frontend.report["status"] = "GENERATED_WITH_WARNINGS"
            # Always preserve partial design work; the old Thin validator does not apply.
            artifacts.update(artifact_store.write_frontend_design(
                frontend_ir=frontend.frontend_ir,
                report=frontend.report,
                batches=frontend.batches,
                traceability=frontend.traceability,
            ))
            self._runtime.traceability.merge_frontend_design_links(
                frontend_ir_traceability(frontend.frontend_ir)
            )
            failed_requirements = {
                rid for task in frontend.report["failed_tasks"] for rid in task["requirement_ids"]
            }
            for rid in requirement_ids:
                states[rid] = "FRONTEND_IR_GENERATED_WITH_WARNINGS" if rid in failed_requirements else "FRONTEND_IR_GENERATED"
            for warning in frontend.report["warnings"]:
                await self._log("Compiler", warning, "warning")
            for failure in frontend.report["failed_tasks"]:
                await self._log("Compiler", f"{failure['phase']}: {failure['message']}", "warning")
            history.commit("4.1 frontend IR generation", [".arc"])
        else:
            frontend = SimpleNamespace(frontend_ir=saved["frontend"]["frontend_ir"],
                                       report=saved["frontend"]["report"], ok=True)
            failed_requirements = set()
            self._runtime.traceability.merge_frontend_design_links(frontend_ir_traceability(frontend.frontend_ir))
            for rid in requirement_ids:
                states[rid] = "FRONTEND_IR_GENERATED"

        if not frontend.ok:
            await self._log("Compiler", "Frontend design produced no usable requirement input; React lowering cannot start.", "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states,
                failed_nodes=sorted(failed_requirements), artifacts=artifacts,
            )
        if rank < 4:
            checkpoints.save("frontend-ir", backend_routes=backend_glue.route_registry,
                             frontend={"frontend_ir": frontend.frontend_ir,
                                       "report": {"status": frontend.report["status"], "warnings": [], "failed_tasks": []}})
        self._runtime.events.mark_phase_started("FRONTEND_LOWERING", "Lowering frontend IR to React.")
        await self._log("Compiler", "Deterministically lowering React components, types, wiring and behavior placeholders; no model calls.")
        lowered_frontend = FrontendReactLowerer(request.output_dir).lower(
            frontend.frontend_ir, design.design_ir, backend_glue.route_registry, request.web_port,
        )
        artifacts.update(artifact_store.write_frontend_lowering(
            report=lowered_frontend.report, sources=lowered_frontend.sources, batches=lowered_frontend.batches,
        ))
        for error in lowered_frontend.errors:
            await self._log("Compiler", error, "error")
        if not lowered_frontend.ok:
            for rid in requirement_ids:
                states[rid] = "FRONTEND_LOWERING_FAILED"
            return CompilationResult(
                ok=False, root_id=root_id, states=states, artifacts=artifacts,
            )
        try:
            artifacts.update(artifact_store.write_frontend_sources(lowered_frontend.sources))
        except (OSError, ValueError) as exc:
            await self._log("Compiler", f"Cannot publish frontend skeleton: {exc}", "error")
            return CompilationResult(ok=False, root_id=root_id, states=states, artifacts=artifacts)
        for rid in requirement_ids:
            states[rid] = "FRONTEND_LOWERED"
        history.commit("4.2 frontend React lowering", [".arc", "frontend"])
        failed_gate = await self._build_gate(request, "Frontend lowering", root_id, states, artifacts)
        artifacts.update(artifact_store.write_frontend_lowering(
            report={**lowered_frontend.report, "build_status": "FAILED" if failed_gate is not None else "PASSED"},
            sources=lowered_frontend.sources, batches=lowered_frontend.batches,
        ))
        if failed_gate is not None:
            for rid in requirement_ids:
                states[rid] = "FRONTEND_BUILD_FAILED"
            return failed_gate
        history.commit("4.3 frontend build accepted", [".arc/lowering/frontend"])
        checkpoints.save("lowered")
        await self._log("Compiler", "Frontend React lowering completed; build and typecheck passed.", "success")
        return await self._continue_after_lowering(
            request=request, artifact_store=artifact_store, preprocessing=preprocessing,
            database_schema=database.schema, design_ir=design.design_ir,
            frontend_ir=frontend.frontend_ir, backend_routes=backend_glue.route_registry,
            fixture_ir=fixture_ir, project_manifest=project_manifest,
            root_id=root_id, states=states, artifacts=artifacts, model=model,
        )

    async def _continue_after_lowering(
        self, *, request, artifact_store, preprocessing, database_schema, design_ir,
        frontend_ir, backend_routes, fixture_ir, project_manifest, root_id, states, artifacts,
        model=None,
    ) -> CompilationResult:
        """Restore deterministic binding metadata without regenerating installed source files."""
        await self._log("Compiler", "Preparing code bindings; continuing to test generation and TDD.")
        try:
            model = model or self._model or Model.from_env()
            if hasattr(model, "set_usage_path"):
                model.set_usage_path(artifact_store.root / "model_usage.jsonl")
            def checked(result):
                if not result.ok:
                    raise ValueError("; ".join(result.errors))
                return result
            symbols = checked(GlobalSymbolPlanner().plan(design_ir, database_schema, project_manifest))
            files = checked(GlobalFilePlanner(request.output_dir).plan(
                design_ir, symbols.registry, project_manifest, fixture_paths=fixture_source_paths(fixture_ir)))
            types = checked(TypeLowerer().lower(symbols.registry, files.registry))
            modules = {kind: checked(ModuleSkeletonLowerer().lower(
                kind, design_ir, symbols.registry, files.registry)).manifest for kind in ("DB", "FUNC", "API")}
            report_path = request.output_dir / ".arc/lowering/frontend/lowering.json"
            if not report_path.is_file():
                report_path = request.output_dir / ".arc/code/frontend/lowering.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            bindings = checked(CodeBindingLowerer().lower(
                output_root=request.output_dir, requirement_ir=preprocessing.requirement_ir,
                dependency_graph=preprocessing.dependency_graph, design_ir=design_ir, frontend_ir=frontend_ir,
                backend_symbol_registry=symbols.registry, backend_type_manifest=types.manifest,
                backend_module_manifests=modules, backend_route_registry=backend_routes,
                frontend_symbol_registry={"symbols": []},
                frontend_file_registry={"ui_locations": [], "store_locations": [], "api_client_locations": []},
                frontend_route_registry={"routes": []}, frontend_lowering_report=report,
            ))
        except (ModelConfigurationError, OSError, ValueError, KeyError) as exc:
            await self._log("Compiler", f"TDD handoff failed: {exc}", "error")
            return CompilationResult(ok=False, root_id=root_id, states=states, artifacts=artifacts)
        artifacts["code_bindings"] = artifact_store.write_code_bindings(bindings.registry)
        ProjectGitHistory(request.output_dir).commit("4.4 code bindings", [".arc/lowering/code_bindings.json"])
        return await self._run_tdd(
            request=request, artifact_store=artifact_store, requirement_ir=preprocessing.requirement_ir,
            dependency_graph=preprocessing.dependency_graph, database_schema=database_schema,
            design_ir=design_ir, frontend_ir=frontend_ir, code_binding_registry=bindings.registry,
            model=model, root_id=root_id, states=states, artifacts=artifacts,
        )

    async def _build_gate(
        self,
        request: CompilationRequest,
        stage: str,
        root_id: str | None,
        states: dict[str, str],
        artifacts: dict[str, str],
    ) -> CompilationResult | None:
        build = ProjectBuilder(request.output_dir).build()
        if not build.ok:
            for output in build.errors:
                await self._log("Compiler", f"{stage}: {output}", "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states, artifacts=artifacts,
            )
        # The build already type-checks shared, frontend and backend; tests are
        # the only workspace omitted from the root build script.
        typecheck = TestRunner(request.output_dir).run_workspace_typecheck("tests")
        if typecheck.status != "PASSED":
            for output in [typecheck.stdout, typecheck.stderr, typecheck.error or ""]:
                if output:
                    await self._log("Compiler", f"{stage}: {output}", "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states, artifacts=artifacts,
            )
        await self._log("Compiler", f"{stage}: build and tests typecheck passed.")
        return None

    async def _run_tdd(
        self,
        *,
        request: CompilationRequest,
        artifact_store: CompilerArtifactStore,
        requirement_ir: dict[str, Any],
        dependency_graph: dict[str, Any],
        database_schema: dict[str, Any],
        design_ir: dict[str, Any],
        frontend_ir: dict[str, Any],
        code_binding_registry: dict[str, Any],
        model: StructuredModel,
        root_id: str | None,
        states: dict[str, str],
        artifacts: dict[str, str],
    ) -> CompilationResult:
        # ===================================================================
        #               Stage 5: Test Generation
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "TEST_GENERATION", "ARC test generation stage started."
        )

        await self._log(
            "Compiler",
            "Preparing the compiler-owned Vitest, Supertest, and Playwright test environment.",
        )
        test_environment = TestEnvironmentInitializer(
            request.output_dir,
            backend_port=request.web_port,
        ).initialize()
        for error in test_environment.errors:
            await self._log("Compiler", error, "error")
        if not test_environment.ok:
            await self._log("Compiler", "TEST_ENVIRONMENT initialization failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        await self._log(
            "Compiler",
            "TEST_ENVIRONMENT_READY: test workspace and browser prerequisites are available.",
        )

        atomic_ids = {
            str(value) for value in requirement_ir.get("atomic_units", []) if str(value)
        }
        folder_ids = {
            str(value) for value in requirement_ir.get("folder_nodes", []) if str(value)
        }
        try:
            order = _tdd_postorder(requirement_ir)
        except ValueError as exc:
            await self._log("Compiler", f"ARC4548 TDD_ORDER_INVALID: {exc}", "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states,
                artifacts=artifacts,
            )
        await self._log("Compiler", f"TDD post-order DFS (dependency waves ignored): {order}")

        history = ProjectGitHistory(request.output_dir)
        test_generation = RequirementTestGenerationPass(
            model, request.output_dir, artifact_store,
        )
        try:
            existing_manifest = load_tdd_manifest(request.output_dir) if request.resume else None
            progress = TDDProgress(request.output_dir, requirement_ir, order, resume=request.resume)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            await self._log("Compiler", f"Cannot restore TDD progress: {exc}", "error")
            return CompilationResult(ok=False, root_id=root_id, states=states, artifacts=artifacts)
        artifacts["tdd_progress"] = str(progress.path)
        if existing_manifest is not None:
            artifacts["test_manifest"] = str(artifact_store.tests_root / "test_manifest.json")
            self._runtime.traceability.merge_test_links(existing_manifest)
        previous_nodes = dict(progress.nodes) if request.resume else {}
        if request.resume:
            if existing_manifest is None and any(row["status"] == "TESTS_PASSED" for row in previous_nodes.values()):
                await self._log("Compiler", "Cannot resume: completed TDD nodes have no saved test manifest.", "error")
                return CompilationResult(ok=False, root_id=root_id, states=states, artifacts=artifacts)
            await self._log("Compiler", f"TDD resume: {len(previous_nodes)} already-started nodes retained; "
                            f"{len(order) - len(previous_nodes)} unstarted nodes remain.")
        orchestrator = NodeTDDOrchestrator(
            model,
            request.output_dir,
            requirement_ir=requirement_ir,
            code_binding_registry=code_binding_registry,
            frontend_ir=frontend_ir,
            test_manifest=existing_manifest,
        )
        self._runtime.events.mark_phase_started(
            "TDD", "ARC node-by-node test-driven implementation started.",
        )
        failed_requirements: set[str] = set()
        for rid, row in previous_nodes.items():
            states[rid] = row["status"]
            if row["status"] in {"STARTED", "FAILED"}:
                failed_requirements.add(rid)

        async def warn_node_failure(requirement_id, stage, result):
            failed = set(result.failed_requirements) if hasattr(result, "failed_requirements") else set()
            failed.add(requirement_id)
            failed_requirements.update(failed)
            for failed_id in failed:
                states[failed_id] = "FAILED"
                if failed_id in progress.data["order"]:
                    progress.mark(failed_id, "FAILED", stage)
            await self._log(
                "Compiler",
                f"{requirement_id}: {stage} failed; continuing with remaining stages where possible. "
                "Accepted changes are retained; rejected patches remain rolled back.",
                "warning",
            )

        for requirement_id in order:
            if requirement_id in previous_nodes:
                await self._log("Compiler", f"Resume skips already-started requirement {requirement_id}: "
                                f"{previous_nodes[requirement_id]['status']}")
                continue
            progress.mark(requirement_id, "STARTED", "test generation")
            if requirement_id in folder_ids:
                targets = CodeTargetResolver(code_binding_registry).resolve_requirement_targets(
                    requirement_id,
                )
                has_screen = any(
                    isinstance(screen, dict)
                    and requirement_id in screen.get("requirement_ids", [])
                    for screen in frontend_ir.get("components" if "root_component_id" in frontend_ir else "screens", [])
                )
                if not targets["owned_targets"] and not has_screen:
                    states[requirement_id] = "AGGREGATE_NO_UI"
                    progress.mark(requirement_id, "AGGREGATE_NO_UI", "no owned UI")
                    await self._log(
                        "Compiler",
                        f"{requirement_id} has no owned targets or associated UI screen; "
                        "its child requirements are verified independently.",
                        "warning",
                    )
                    continue
            await self._log("Compiler", f"Generating and freezing tests for {requirement_id}.")
            generated_tests = test_generation.generate_requirement(
                requirement_id=requirement_id,
                requirement_ir=requirement_ir,
                database_schema=database_schema,
                design_ir=design_ir,
                frontend_ir=frontend_ir,
                code_binding_registry=code_binding_registry,
                environment_manifest=test_environment.manifest,
                existing_manifest=orchestrator.test_manifest,
            )
            artifacts.update(generated_tests.artifacts)
            states.update(generated_tests.node_states)
            for error in generated_tests.errors:
                await self._log("Compiler", error, "warning")
            if not generated_tests.ok:
                await warn_node_failure(requirement_id, "test generation", generated_tests)
                continue
            orchestrator.test_manifest = generated_tests.manifest
            self._runtime.traceability.merge_test_links(generated_tests.manifest)
            history.commit(f"5 test generation {requirement_id}", [".arc", "tests"])

            if requirement_id in atomic_ids:
                backend_implementation = orchestrator.implement_backend([requirement_id])
                for error in backend_implementation.errors:
                    await self._log("NodeTDDOrchestrator", error, "warning")
                if not backend_implementation.ok:
                    await warn_node_failure(requirement_id, "backend implementation", backend_implementation)
                else:
                    states[requirement_id] = "BACKEND_IMPLEMENTED"
                if backend_implementation.changed_files:
                    history.commit(
                        f"6.1 backend implementation {requirement_id}",
                        backend_implementation.changed_files,
                    )

            frontend_implementation = orchestrator.implement_frontend([requirement_id])
            for error in frontend_implementation.errors:
                await self._log("NodeTDDOrchestrator", error, "warning")
            if not frontend_implementation.ok:
                await warn_node_failure(requirement_id, "frontend implementation", frontend_implementation)
            else:
                states[requirement_id] = "FRONTEND_IMPLEMENTED"
            if frontend_implementation.changed_files:
                history.commit(
                    f"6.2 frontend implementation {requirement_id}",
                    frontend_implementation.changed_files,
                )

            # Both initial implementations precede any behavioral test execution.
            tests = orchestrator.run_test_layers([requirement_id])
            for error in tests.errors:
                await self._log("NodeTDDOrchestrator", error, "warning")
            if not tests.ok:
                await warn_node_failure(requirement_id, "test-driven repair", tests)
                if tests.status == "PRECONDITION_BLOCKED":
                    continue
            # Later layers can repair earlier failures, but an E2E pass alone
            # does not prove the earlier assertions now pass. Verify all layers
            # against the final code without resetting any repair budget.
            await self._log("Compiler", f"Verifying all test layers after repairs for {requirement_id}.")
            verification = orchestrator.verify_test_layers([requirement_id])
            for error in verification.errors:
                await self._log("NodeTDDOrchestrator", error, "warning")
            if not verification.ok:
                await warn_node_failure(requirement_id, "final test verification", verification)
                continue
            failed_requirements.discard(requirement_id)
            states[requirement_id] = "TESTS_PASSED"
            progress.mark(requirement_id, "TESTS_PASSED", "final test verification")
            await self._log("Compiler", f"TDD completed for {requirement_id}.")

        failed_nodes = sorted(failed_requirements)
        for failed_id in failed_nodes:
            states[failed_id] = "FAILED"
        if failed_nodes:
            await self._log(
                "Compiler",
                "TDD traversal finished with failed requirements: " + ", ".join(failed_nodes),
                "warning",
            )
        final_validation_errors = orchestrator.validate_final()
        if final_validation_errors:
            for error in final_validation_errors:
                await self._log("Compiler", f"Final validation: {error}", "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states,
                failed_nodes=failed_nodes, artifacts=artifacts,
            )
        return CompilationResult(
            ok=not failed_nodes, root_id=root_id, states=states,
            failed_nodes=failed_nodes, artifacts=artifacts,
        )

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        result = self._log_cb(agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result
