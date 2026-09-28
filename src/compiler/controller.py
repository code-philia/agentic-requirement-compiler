from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

from arcbench_agent_runtime.runtime import AgentRuntime

from .artifacts import CompilerArtifactStore
from .backend_lowering import BackendGlueLowerer
from .code_binding import CodeTargetResolver
from .database_stage import (
    DatabaseSchemaPass,
    database_traceability,
)
from .database_lowering import lower_database
from .design_stage import DesignPass, design_traceability
from .file_planning import GlobalFilePlanner
from .frontend_generation import FrontendIRGenerationPass, frontend_ir_traceability
from .git_history import GitStageError, ProjectGitHistory
from .fixture_stage import (
    FixturePass,
)
from .fixture_lowering import fixture_source_paths
from .preprocessing_stage import RequirementPreprocessor
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
from .test_generation import RequirementTestGenerationPass, TestEnvironmentInitializer
from .test_runner import TestRunner
from .visual_reference import VisualReferenceResolver


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


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
        try:
            history.initialize()
            history.commit("0 project initialization", [
                ".gitignore", ".env.example", "README.md", "package.json", "package-lock.json",
                "backend", "frontend", "shared", "tests", ".arc/project",
            ])
        except GitStageError as exc:
            await self._log("Compiler", str(exc), "error")
            return CompilationResult(ok=False, artifacts=artifacts)

        # ===================================================================
        #                    Requirement Preprocessing Stage
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic PREPROCESSING pass.",
        )
        preprocessing = self._preprocessor.compile(request.requirement_path)
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
        history.commit("1 preprocessing", [".arc"])

        # ===================================================================
        #                    Compiler Database Stage
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "DATABASE", "ARC database stage started."
        )

        await self._log("Compiler", "Running database schema design passes.")
        try:
            model = self._model or Model.from_env()
        except ModelConfigurationError as exc:
            await self._log("Compiler", str(exc), "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=atomic_ids,
                artifacts=artifacts,
            )
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

        # ===================================================================
        #                 Database Lowering: Seed Fixtures
        # ===================================================================

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
            ".arc", "backend/src/db", "backend/src/fixtures", "backend/init-db.mjs", "shared/src",
        ])
        failed_gate = await self._build_gate(request, "2.2 database schema lowering", root_id, states, artifacts)
        if failed_gate is not None:
            return failed_gate

        # ===================================================================
        #                    Compiler Design Stage
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "DESIGN", "ARC backend design stage started."
        )

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

        # ===================================================================
        #                  Backend Lowering: Symbol Planning
        # ===================================================================

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
        await self._log(
            "Compiler",
            "Global Glue Code, Route Registration, Barrel Export, Import Plan, and Backend Manifest generated.",
        )
        history.commit("3.2 backend lowering", [".arc", "backend/src", "shared/src"])
        failed_gate = await self._build_gate(request, "3.2 backend lowering", root_id, states, artifacts)
        if failed_gate is not None:
            return failed_gate

        # ===================================================================
        #                 Compiler Frontend Design Stage
        # ===================================================================

        self._runtime.events.mark_phase_started(
            "FRONTEND", "ARC frontend design stage started."
        )

        await self._log("Compiler", "Resolving visual references for regional frontend design calls.")
        visuals = VisualReferenceResolver().resolve(
            request.requirement_path, preprocessing.requirement_ir,
        )
        await self._log(
            "Compiler",
            "Generating frontend IR through bounded observation, assembly, UI/data and behavior passes.",
        )
        frontend = FrontendIRGenerationPass(model, artifact_store.root).compile(
            preprocessing.requirement_ir,
            preprocessing.dependency_graph,
            design.design_ir,
            visuals.references,
        )
        for issue in visuals.errors:
            frontend.report["warnings"].append(issue.format())
        if frontend.report["warnings"] and frontend.report["status"] == "GENERATED":
            frontend.report["status"] = "GENERATED_WITH_WARNINGS"
        # Always preserve partial design work; the old Thin validator does not apply.
        artifacts.update(artifact_store.write_frontend_design(
            frontend_ir=frontend.frontend_ir,
            report=frontend.report,
            observations=frontend.observations,
            batches=frontend.batches,
        ))
        self._runtime.traceability.merge_frontend_design_links(
            frontend_ir_traceability(frontend.frontend_ir)
        )
        failed_requirements = {
            rid for task in frontend.report["failed_tasks"] for rid in task["requirement_ids"]
        }
        for rid in requirement_ids:
            states[rid] = "FRONTEND_IR_PARTIAL" if rid in failed_requirements else "FRONTEND_IR_GENERATED"
        for warning in frontend.report["warnings"]:
            await self._log("Compiler", warning, "warning")
        for failure in frontend.report["failed_tasks"]:
            await self._log("Compiler", f"{failure['phase']}: {failure['message']}", "warning")
        history.commit("4.1 frontend IR generation", [".arc"])
        await self._log(
            "Compiler",
            f"Frontend IR {frontend.report['status']}; saved to {artifacts['frontend_design_ir']}. "
            "This run ends at frontend design; the legacy frontend lowering does not consume this metamodel.",
            "success" if frontend.ok else "warning",
        )
        return CompilationResult(
            ok=frontend.ok, root_id=root_id, states=states,
            failed_nodes=sorted(failed_requirements), artifacts=artifacts,
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
        typecheck = TestRunner(request.output_dir).run_workspace_typecheck()
        if typecheck.status != "PASSED":
            for output in [typecheck.stdout, typecheck.stderr, typecheck.error or ""]:
                if output:
                    await self._log("Compiler", f"{stage}: {output}", "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states, artifacts=artifacts,
            )
        await self._log("Compiler", f"{stage}: build and typecheck passed.")
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
        order = [
            str(requirement_id)
            for wave in dependency_graph.get("atomic_implementation_waves", [])
            if isinstance(wave, list)
            for requirement_id in wave
            if str(requirement_id) in atomic_ids
        ]
        if len(order) != len(set(order)) or set(order) != atomic_ids:
            message = (
                "ARC4548 TDD_ORDER_INVALID: atomic_implementation_waves must contain "
                "every atomic requirement exactly once."
            )
            await self._log("Compiler", message, "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states,
                failed_nodes=sorted(atomic_ids), artifacts=artifacts,
            )

        folder_ids = {
            str(value) for value in requirement_ir.get("folder_nodes", []) if str(value)
        }
        folder_order = [
            str(requirement_id)
            for wave in dependency_graph.get("implementation_waves", [])
            if isinstance(wave, list)
            for requirement_id in wave
            if str(requirement_id) in folder_ids
        ]
        if len(folder_order) != len(set(folder_order)) or set(folder_order) != folder_ids:
            message = (
                "ARC4548 AGGREGATE_ORDER_INVALID: implementation_waves must contain "
                "every folder requirement exactly once."
            )
            await self._log("Compiler", message, "error")
            return CompilationResult(
                ok=False, root_id=root_id, states=states,
                failed_nodes=sorted(folder_ids), artifacts=artifacts,
            )

        history = ProjectGitHistory(request.output_dir)
        test_generation = RequirementTestGenerationPass(
            model, request.output_dir, artifact_store,
        )
        orchestrator = NodeTDDOrchestrator(
            model,
            request.output_dir,
            requirement_ir=requirement_ir,
            code_binding_registry=code_binding_registry,
            frontend_ir=frontend_ir,
        )
        self._runtime.events.mark_phase_started(
            "TDD", "ARC node-by-node test-driven implementation started.",
        )
        for requirement_id in [*order, *folder_order]:
            if requirement_id in folder_ids:
                targets = CodeTargetResolver(code_binding_registry).resolve_requirement_targets(
                    requirement_id,
                )
                has_screen = any(
                    isinstance(screen, dict)
                    and requirement_id in screen.get("requirement_ids", [])
                    for screen in frontend_ir.get("screens", [])
                )
                if not targets["owned_targets"] and not has_screen:
                    states[requirement_id] = "AGGREGATE_NO_UI"
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
                await self._log("Compiler", error, "error")
            if not generated_tests.ok:
                return CompilationResult(
                    ok=False, root_id=root_id, states=states,
                    failed_nodes=[requirement_id], artifacts=artifacts,
                )
            orchestrator.test_manifest = generated_tests.manifest
            self._runtime.traceability.merge_test_links(generated_tests.manifest)
            history.commit(f"5 test generation {requirement_id}", [".arc", "tests"])

            if requirement_id in atomic_ids:
                backend_implementation = orchestrator.implement_backend([requirement_id])
                for error in backend_implementation.errors:
                    await self._log("NodeTDDOrchestrator", error, "error")
                if not backend_implementation.ok:
                    states[requirement_id] = "FAILED"
                    return CompilationResult(
                        ok=False, root_id=root_id, states=states,
                        failed_nodes=[requirement_id], artifacts=artifacts,
                    )
                states[requirement_id] = "BACKEND_IMPLEMENTED"
                if backend_implementation.changed_files:
                    history.commit(
                        f"6.1 backend implementation {requirement_id}",
                        backend_implementation.changed_files,
                    )

                backend_tests = orchestrator.run_test_layers(
                    [requirement_id], layers=("UNIT", "INTEGRATION"),
                )
                for error in backend_tests.errors:
                    await self._log("NodeTDDOrchestrator", error, "error")
                if not backend_tests.ok:
                    failed_nodes = sorted(set(backend_tests.failed_requirements) or {requirement_id})
                    for failed_id in failed_nodes:
                        states[failed_id] = "FAILED"
                    return CompilationResult(
                        ok=False, root_id=root_id, states=states,
                        failed_nodes=failed_nodes, artifacts=artifacts,
                    )

            frontend_implementation = orchestrator.implement_frontend([requirement_id])
            for error in frontend_implementation.errors:
                await self._log("NodeTDDOrchestrator", error, "error")
            if not frontend_implementation.ok:
                states[requirement_id] = "FAILED"
                return CompilationResult(
                    ok=False, root_id=root_id, states=states,
                    failed_nodes=[requirement_id], artifacts=artifacts,
                )
            states[requirement_id] = "FRONTEND_IMPLEMENTED"
            if frontend_implementation.changed_files:
                history.commit(
                    f"6.2 frontend implementation {requirement_id}",
                    frontend_implementation.changed_files,
                )

            tests = orchestrator.run_test_layers([requirement_id], layers=("E2E",))
            for error in tests.errors:
                await self._log("NodeTDDOrchestrator", error, "error")
            if not tests.ok:
                failed_nodes = sorted(set(tests.failed_requirements) or {requirement_id})
                for failed_id in failed_nodes:
                    states[failed_id] = "FAILED"
                return CompilationResult(
                    ok=False, root_id=root_id, states=states,
                    failed_nodes=failed_nodes, artifacts=artifacts,
                )
            states[requirement_id] = "TESTS_PASSED"
            await self._log("Compiler", f"TDD completed for {requirement_id}.")

        failed_nodes: list[str] = []
        final_build = ProjectBuilder(request.output_dir).build()
        for error in final_build.errors:
            await self._log("Compiler", error, "error")
        if not final_build.ok:
            return CompilationResult(
                ok=False, root_id=root_id, states=states,
                failed_nodes=failed_nodes, artifacts=artifacts,
            )
        ui_bindings = {
            str(row.get("module_id", "")): row
            for row in code_binding_registry.get("code_bindings", [])
            if isinstance(row, dict) and row.get("kind") in {"PAGE", "COMPONENT", "LAYOUT"}
        }
        writable_ids = {
            str(module_id)
            for row in code_binding_registry.get("requirement_targets", [])
            if isinstance(row, dict)
            for module_id in row.get("writable", [])
        }
        incomplete = [
            f"{screen.get('id')}: no writable page binding"
            for screen in frontend_ir.get("screens", [])
            if isinstance(screen, dict)
            and (
                str(screen.get("id", "")) not in ui_bindings
                or str(screen.get("id", "")) not in writable_ids
            )
        ]
        for module_id, binding in ui_bindings.items():
            source = request.output_dir / str(binding.get("file", ""))
            if module_id not in writable_ids:
                incomplete.append(f"{module_id}: no writable requirement")
            text = source.read_text(encoding="utf-8") if source.is_file() else ""
            if not source.is_file() or any(marker in text for marker in (
                "Implementation pending", "data-arc-obligation=",
                f"data-arc-{str(binding['kind']).lower()}=",
            )):
                incomplete.append(f"{module_id}: unfinished {binding.get('file', '')}")
        if incomplete:
            for item in sorted(set(incomplete)):
                await self._log("Compiler", f"ARC4550 FRONTEND_INCOMPLETE: {item}", "error")
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
