from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

from arcbench_agent_runtime.runtime import AgentRuntime

from .artifacts import CompilerArtifactStore
from .backend_lowering import BackendGlueLowerer
from .code_binding import CodeBindingLowerer, validate_code_binding_registry
from .database_stage import (
    DatabasePassResult,
    DatabaseSchemaPass,
    database_artifact_tables,
    database_structure,
    database_traceability,
    hydrate_database_schema,
    validate_database_schema,
    validate_database_structure,
)
from .design_stage import DesignPass, DesignPassResult, design_traceability
from .design_projection import project_api_contracts
from .file_planning import GlobalFilePlanner
from .frontend_thin_design import (
    ThinFrontendDesignPass,
    frontend_design_traceability,
    materialize_screen_components,
    project_frontend_runtime_ir,
)
from .frontend_lowering import (
    FrontendFilePlanner,
    FrontendGlobalSymbolPlanner,
    FrontendSkeletonLowerer,
)
from .fixture_stage import (
    FixturePass,
    attach_fixture_sets,
    validate_fixture_ir,
)
from .fixture_lowering import FixtureLowerer, fixture_source_paths
from .preprocessing_stage import RequirementPreprocessor
from .model_client import Model, ModelConfigurationError, StructuredModel
from .models import CompilationRequest, CompilationResult
from .module_lowering import ModuleSkeletonLowerer
from .project_build import ProjectBuilder
from .project_initialization import (
    ProjectInitializer,
    validate_frontend_environment,
)
from .skeleton_lowering import DatabaseSchemaLowerer, TypeLowerer
from .symbol_planning import GlobalSymbolPlanner
from .tdd_orchestrator import NodeTDDOrchestrator
from .test_generation import TestEnvironmentInitializer
from .visual_reference import VisualReferenceAnalyzer, VisualReferenceResolver


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
        stage_order = {
            "PREPROCESSING": 0,
            "DATABASE": 1,
            "DESIGN": 2,
            "FRONTEND": 3,
            "PROJECT": 4,
            "SKELETON": 5,
            "TDD": 6,
        }
        start_from = str(request.start_from or "PREPROCESSING").strip().upper()
        if start_from not in stage_order:
            await self._log("Compiler", f"Unknown start stage: {start_from}", "error")
            return CompilationResult(ok=False)
        start_rank = stage_order[start_from]
        database_reused = start_rank > stage_order["DATABASE"]
        design_reused = start_rank > stage_order["DESIGN"]
        frontend_design_reused = start_rank > stage_order["FRONTEND"]
        project_reused = start_rank > stage_order["PROJECT"]
        fixtures_reused = start_rank >= stage_order["DESIGN"]

        # ===================================================================
        #                    Requirement Preprocessing Stage
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic PREPROCESSING pass."
            if start_from == "PREPROCESSING"
            else f"START_PROBE stage={start_from} upstream=PREPROCESSING source=requirements status=VALIDATING",
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

        artifact_store = CompilerArtifactStore(request.output_dir)
        artifacts: dict[str, str] = {}
        if start_from == "PREPROCESSING":
            artifacts.update(artifact_store.write_preprocessing(
                requirement_ir=preprocessing.requirement_ir,
                dependency_graph=preprocessing.dependency_graph,
            ))

        if preprocessing.normalized_tree:
            self._runtime.traceability.store_requirement_tree(preprocessing.normalized_tree)

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
        if start_from != "PREPROCESSING":
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=PREPROCESSING source=requirements status=VALIDATED",
            )

        # ===================================================================
        #                    Compiler Database Stage
        # ===================================================================

        model: StructuredModel | None = self._model
        if database_reused:
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=DATABASE source=.arc/database status=VALIDATING",
            )
            existing_structure, read_error = artifact_store.read_database()
            if read_error:
                validation_errors = [read_error]
                reusable_schema = {}
            else:
                structure = existing_structure or {}
                validation_errors = validate_database_structure(structure)
                links = self._runtime.traceability.read_database_schema_links_from_requirements()
                reusable_schema = hydrate_database_schema(structure, links)
                if not validation_errors:
                    validation_errors = validate_database_schema(
                        reusable_schema,
                        expected_requirement_ids=set(atomic_ids),
                    )
            if validation_errors:
                for error in validation_errors:
                    await self._log("Compiler", f"ARC2104: Cannot reuse database schema: {error}", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            database = DatabasePassResult(
                schema=reusable_schema,
                node_states={node_id: "SCHEMA_REUSED" for node_id in atomic_ids},
            )
            states.update(database.node_states)
            artifacts["database_schema"] = str(artifact_store.database_root / "database_schema.json")
            artifacts["database_relationships"] = str(artifact_store.database_root / "relationships.json")
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=DATABASE source=.arc/database status=VALIDATED",
            )
        else:
            await self._log(
                "Compiler",
                "Running ENTITY, FIELD, RELATIONSHIP, and CONSTRAINT database passes.",
            )
            database = None

        if database is None:
            try:
                model = model or Model.from_env()
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
            # Stage 1 is deliberately synchronous: each database pass and each
            # requirement completes before the next one starts.
            database = database_stage.compile(
                preprocessing.requirement_ir,
                preprocessing.dependency_graph,
            )
            states.update(database.node_states)
            artifacts.update(database.pass_artifacts)
            structure = database_structure(database.schema)
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
            entities, relationships = database_artifact_tables(structure)
            artifacts.update(artifact_store.write_database(
                entities=entities,
                relationships=relationships,
            ))
            self._runtime.traceability.merge_database_schema_links(links)

        # ===================================================================
        #                    Compiler Fixture IR Stage
        # ===================================================================

        fixture_ir: dict[str, Any]
        if fixtures_reused:
            fixture_ir, fixture_read_error = artifact_store.read_fixture_ir()
            fixture_errors = (
                [fixture_read_error]
                if fixture_read_error
                else validate_fixture_ir(
                    fixture_ir or {},
                    database.schema,
                    expected_requirement_ids=set(atomic_ids),
                )
            )
            if fixture_errors:
                for error in fixture_errors:
                    await self._log("Compiler", f"ARC2402: Cannot reuse Fixture IR: {error}", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            fixture_ir = fixture_ir or {}
            artifacts["fixture_ir"] = str(artifact_store.fixtures_root / "fixture_ir.json")
        else:
            try:
                model = model or Model.from_env()
            except ModelConfigurationError as exc:
                await self._log("Compiler", str(exc), "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            fixture_result = FixturePass(model, artifact_store.root).compile(
                preprocessing.requirement_ir,
                database.schema,
            )
            for warning in fixture_result.warnings:
                await self._log("Compiler", warning, "warning")
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
        attach_fixture_sets(preprocessing.requirement_ir, fixture_ir)

        # ===================================================================
        #                    Compiler Design Stage
        # ===================================================================

        if design_reused:
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=DESIGN source=.arc/design status=VALIDATING",
            )
            reusable_design, read_error = artifact_store.read_design(
                expected_requirement_ids=set(atomic_ids),
            )
            if read_error:
                await self._log("Compiler", f"ARC3104: Cannot reuse Design IR: {read_error}", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            design = DesignPassResult(
                design_ir=reusable_design or {},
                node_states={node_id: "DESIGN_REUSED" for node_id in atomic_ids},
            )
            states.update(design.node_states)
            for name, filename in (
                ("design_requirement_contracts", "requirement_contracts.json"),
                ("design_api_contracts", "api_contracts.json"),
            ):
                artifacts[name] = str(artifact_store.design_root / filename)
            artifacts["design_api_modules"] = str(
                artifact_store.backend_design_root / "api_modules.json"
            )
            artifacts["design_function_modules"] = str(
                artifact_store.backend_design_root / "function_modules.json"
            )
            artifacts["design_db_modules"] = str(
                artifact_store.backend_design_root / "db_modules.json"
            )
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=DESIGN source=.arc/design status=VALIDATED",
            )
        else:
            try:
                model = model or Model.from_env()
            except ModelConfigurationError as exc:
                await self._log("Compiler", str(exc), "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            design_stage = DesignPass(model, artifact_store.root)
            await self._log(
                "Compiler",
                "Running REQUIREMENT CONTRACT, REQUIREMENT TO API, and full top-down MODULE DECOMPOSITION passes.",
            )
            # Stage 2 keeps contract generation and module materialization serial.
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
        if not design_reused:
            # Partial design is a first-class diagnostic/checkpoint artifact.
            # Persist it before the success gate so a failed requirement does
            # not discard every earlier validated requirement in the run.
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

        if not design_reused:
            self._runtime.traceability.merge_design_links(design_traceability(design.design_ir))

        # ===================================================================
        #                 Compiler Frontend Design Stage
        # ===================================================================

        backend_api_ids = {
            str(module.get("id"))
            for module in design.design_ir.get("modules", [])
            if isinstance(module, dict)
            and module.get("kind") == "API"
            and str(module.get("id", "")).strip()
        }
        frontend_design_ir: dict[str, object] = {}
        frontend_errors: list[str] = []

        if frontend_design_reused:
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=FRONTEND_DESIGN "
                "source=.arc/design/frontend status=VALIDATING",
            )
            reusable_frontend, read_error = artifact_store.read_frontend_design(
                requirement_links=(
                    self._runtime.traceability.read_frontend_design_links_from_requirements()
                ),
                expected_requirement_ids=set(requirement_ids),
                backend_api_ids=backend_api_ids,
            )
            if read_error:
                frontend_errors.append(f"ARC4150: Cannot reuse Frontend Design IR: {read_error}")
            else:
                frontend_design_ir = reusable_frontend or {}
                for table_name in (
                    "visual_references",
                    "screens",
                    "placements",
                    "screen_components",
                    "journeys",
                    "api_usages",
                    "shared_state_policies",
                ):
                    artifacts[f"frontend_design_{table_name}"] = str(
                        artifact_store.frontend_design_root / f"{table_name}.json"
                    )
                await self._log(
                    "Compiler",
                    f"START_PROBE stage={start_from} upstream=FRONTEND_DESIGN "
                    "source=.arc/design/frontend status=VALIDATED",
                )
        else:
            try:
                model = model or Model.from_env()
            except ModelConfigurationError as exc:
                frontend_errors.append(str(exc))
            if model is None:
                frontend_errors.append("ARC4120 UI_SCOPE_MODEL_FAILED: Frontend Design has no configured model.")

        if not frontend_design_reused and not frontend_errors:
            assert model is not None
            await self._log(
                "Compiler",
                "Running VISUAL REFERENCE RESOLUTION and multimodal ANALYSIS serially.",
            )
            visual_resolution = VisualReferenceResolver().resolve(
                request.requirement_path,
                preprocessing.requirement_ir,
            )
            for issue in visual_resolution.errors:
                await self._log(
                    "Compiler",
                    f"{issue.format()} Skipping this optional visual reference.",
                    "warning",
                )
            if visual_resolution.references:
                visual_analysis = VisualReferenceAnalyzer.from_env(artifact_store.root).analyze(
                    visual_resolution.references
                )
                for issue in visual_analysis.errors:
                    await self._log(
                        "Compiler",
                        f"{issue.format()} Skipping this optional visual analysis.",
                        "warning",
                    )
                analyzed_visuals: list[dict[str, object]] = visual_analysis.references
            else:
                analyzed_visuals = []

            if not frontend_errors:
                await self._log(
                    "Compiler",
                    "Running multi-pass THIN FRONTEND DESIGN: one product-wide screen/route/navigation architecture pass followed by requirement-ordered UI placement passes; implementation targets and API associations are compiler-derived.",
                )
                ui_scope = ThinFrontendDesignPass(model, artifact_store.root).compile(
                    preprocessing.requirement_ir,
                    preprocessing.dependency_graph,
                    design.design_ir,
                    analyzed_visuals,
                )
                if not ui_scope.ok:
                    frontend_errors.extend(ui_scope.errors)
                    states.update(ui_scope.node_states)
                else:
                    frontend_design_ir = ui_scope.frontend_ir

            if not frontend_errors:
                try:
                    artifacts.update(artifact_store.write_frontend_design(
                        frontend_ir=frontend_design_ir,
                    ))
                except ValueError as exc:
                    frontend_errors.append(f"ARC4150 DUAL_DESIGN_INVALID: {exc}")

        if not frontend_errors and not frontend_design_ir.get("screen_components"):
            await self._log(
                "Compiler",
                "Deriving frontend implementation components deterministically from requirement placements.",
            )
            frontend_design_ir = materialize_screen_components(
                frontend_design_ir,
                preprocessing.requirement_ir,
            )
            try:
                artifacts.update(artifact_store.write_frontend_design(
                    frontend_ir=frontend_design_ir,
                ))
            except ValueError as exc:
                frontend_errors.append(f"ARC4150 DUAL_DESIGN_INVALID: {exc}")

        if frontend_errors:
            for node_id in requirement_ids:
                states[node_id] = "FAILED"
            for error in frontend_errors:
                await self._log("Compiler", error, "error")
            await self._log("Compiler", "FRONTEND_DESIGN pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=requirement_ids,
                artifacts=artifacts,
            )

        dual_design_state = (
            "DUAL_DESIGN_REUSED" if frontend_design_reused else "DUAL_DESIGN_FROZEN"
        )
        for node_id in requirement_ids:
            states[node_id] = dual_design_state
        self._runtime.traceability.merge_frontend_design_links(
            frontend_design_traceability(frontend_design_ir)
        )
        design_queue_status = dual_design_state
        await self._log(
            "Compiler",
            f"{dual_design_state}: Backend Design IR and Frontend Design IR are validated and frozen.",
        )

        # ===================================================================
        #                    Project Initialization Stage
        # ===================================================================

        project_ok = True
        project_errors: list[str] = []
        project_manifest = None
        if project_reused:
            await self._log(
                "Compiler",
                f"START_PROBE stage={start_from} upstream=PROJECT source=.arc/project/project-manifest.json status=VALIDATING",
            )
            project_manifest, project_error = artifact_store.read_project_manifest()
            if project_error:
                project_ok = False
                project_errors.append(f"ARC3201: Cannot reuse initialized project: {project_error}")
            else:
                artifacts["project_manifest"] = str(
                    artifact_store.root / "project" / "project-manifest.json"
                )
                await self._log(
                    "Compiler",
                    f"START_PROBE stage={start_from} upstream=PROJECT source=.arc/project/project-manifest.json status=VALIDATED",
                )
        else:
            await self._log(
                "Compiler",
                "Running deterministic PROJECT_INITIALIZATION with official ecosystem scaffolders.",
            )
            initializer = ProjectInitializer(
                request.output_dir,
                web_port=request.web_port,
            )
            project = initializer.initialize()
            project_ok = project.ok
            project_errors.extend(project.errors)
            artifacts.update(project.artifacts)
            if project_ok:
                project_manifest, project_error = artifact_store.read_project_manifest()
                if project_error:
                    project_ok = False
                    project_errors.append(
                        f"ARC3201: Initialized project manifest is invalid: {project_error}"
                    )
        for error in project_errors:
            await self._log("Compiler", error, "error")
        if not project_ok:
            await self._log("Compiler", "PROJECT_INITIALIZATION pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        if project_ok and project_manifest is not None:
            frontend_environment_errors = validate_frontend_environment(
                request.output_dir,
                project_manifest,
            )
            if frontend_environment_errors:
                project_ok = False
                for error in frontend_environment_errors:
                    await self._log("Compiler", error, "error")
        if not project_ok:
            await self._log(
                "Compiler",
                "PROJECT_INITIALIZATION validation failed.",
                "error",
            )
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        assert project_manifest is not None
        await self._log(
            "Compiler",
            (
                "Project workspace and manifest validated for node-by-node TDD."
                if start_from == "TDD"
                else "Project workspace initialized; Skeleton lowering may consume the frozen project manifest."
            ),
        )

        if start_from == "TDD":
            await self._log(
                "Compiler",
                "START_PROBE stage=TDD upstream=CODE_BINDING "
                "source=.arc/code/code_bindings.json status=VALIDATING",
            )
            reusable_bindings, read_error = artifact_store.read_code_bindings()
            binding_errors = (
                [f"ARC4308: Cannot reuse Code Binding Registry: {read_error}"]
                if read_error
                else validate_code_binding_registry(
                    reusable_bindings or {},
                    output_root=request.output_dir,
                    expected_requirement_ids=set(requirement_ids),
                )
            )
            if binding_errors:
                for error in binding_errors:
                    await self._log("Compiler", error, "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=atomic_ids,
                    artifacts=artifacts,
                )
            artifacts["code_bindings"] = str(
                artifact_store.code_root / "code_bindings.json"
            )
            await self._log(
                "Compiler",
                "START_PROBE stage=TDD upstream=CODE_BINDING "
                "source=.arc/code/code_bindings.json status=VALIDATED",
            )
            return await self._run_tdd(
                request=request,
                artifact_store=artifact_store,
                requirement_ir=preprocessing.requirement_ir,
                dependency_graph=preprocessing.dependency_graph,
                database_schema=database.schema,
                design_ir=design.design_ir,
                frontend_ir=frontend_design_ir,
                code_binding_registry=reusable_bindings or {},
                model=model,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )

        # ===================================================================
        #                  Skeleton Stage 3.1: Symbol Planning
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic GLOBAL_SYMBOL_PLANNING over Design IR and Database Schema IR.",
        )
        symbol_planning = GlobalSymbolPlanner().plan(
            design.design_ir,
            database.schema,
            project_manifest,
        )
        artifacts["backend_symbol_registry"] = artifact_store.write_symbol_registry(
            symbol_planning.registry
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
        #                   Skeleton Stage 3.1: File Planning
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
        artifacts["backend_file_registry"] = artifact_store.write_file_registry(
            file_planning.registry
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
        #                    Skeleton Stage 3.1: Type Lowering
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic TYPE_LOWERING for canonical TypeScript definitions.",
        )
        type_lowering = TypeLowerer().lower(
            symbol_planning.registry,
            file_planning.registry,
        )
        artifacts["backend_type_manifest"] = artifact_store.write_type_manifest(
            type_lowering.manifest
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
        artifacts.update(artifact_store.write_generated_sources(type_lowering.sources))
        await self._log("Compiler", "Canonical TypeScript type world generated.")

        # ===================================================================
        #              Skeleton Stage 3.1: Database Schema Lowering
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic DATABASE_SCHEMA_LOWERING for SQLite and Drizzle.",
        )
        database_lowering = DatabaseSchemaLowerer().lower(
            database.schema,
            symbol_planning.registry,
            file_planning.registry,
        )
        fixture_lowering = FixtureLowerer().lower(
            fixture_ir,
            database_lowering.manifest,
        )
        database_lowering.errors.extend(fixture_lowering.errors)
        database_lowering.sources.update(fixture_lowering.sources)
        if not database_lowering.errors:
            database_lowering.manifest["planned_files"] = sorted(database_lowering.sources)
            database_lowering.manifest["generated_files"] = sorted(database_lowering.sources)
        artifacts["backend_database_schema_manifest"] = (
            artifact_store.write_database_schema_manifest(database_lowering.manifest)
        )
        for warning in database_lowering.warnings:
            await self._log("Compiler", warning, "warning")
        for error in database_lowering.errors:
            await self._log("Compiler", error, "error")
        if not database_lowering.ok:
            await self._log("Compiler", "DATABASE_SCHEMA_LOWERING pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        artifacts.update(artifact_store.write_generated_sources(database_lowering.sources))
        await self._log(
            "Compiler",
            "SQLite/Drizzle schema lowered; DB Module Skeleton generation is next.",
        )

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
        artifacts["backend_db_modules_manifest"] = artifact_store.write_module_manifest(
            "DB",
            db_modules.manifest,
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
        artifacts["backend_func_modules_manifest"] = artifact_store.write_module_manifest(
            "FUNC",
            func_modules.manifest,
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
        artifacts["backend_api_modules_manifest"] = artifact_store.write_module_manifest(
            "API",
            api_modules.manifest,
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
                "database": database_lowering.manifest,
                "DB": db_modules.manifest,
                "FUNC": func_modules.manifest,
                "API": api_modules.manifest,
            },
            default_port=request.web_port,
        )
        artifacts.update(
            artifact_store.write_backend_lowering(
                route_registry=backend_glue.route_registry,
                import_plan=backend_glue.import_plan,
                manifest=backend_glue.manifest,
            )
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

        # ===================================================================
        #              Skeleton Stage 3.2: Frontend Symbol Planning
        # ===================================================================

        await self._log(
            "Compiler",
            "Projecting Thin Frontend IR to compiler runtime seams for routes, API clients, and editable screen modules.",
        )
        frontend_runtime_ir = project_frontend_runtime_ir(frontend_design_ir)
        frontend_symbols = FrontendGlobalSymbolPlanner().plan(
            frontend_runtime_ir,
            project_api_contracts(design.design_ir),
            symbol_planning.registry,
            project_manifest,
        )
        artifacts["frontend_symbol_registry"] = artifact_store.write_frontend_symbol_registry(
            frontend_symbols.registry
        )
        for error in frontend_symbols.errors:
            await self._log("Compiler", error, "error")
        if not frontend_symbols.ok:
            await self._log("Compiler", "FRONTEND_GLOBAL_SYMBOL_PLANNING pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )

        # ===================================================================
        #                Skeleton Stage 3.2: Frontend File Planning
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic FRONTEND_FILE_PLANNING over the Frontend Symbol Registry.",
        )
        frontend_files = FrontendFilePlanner(request.output_dir).plan(
            frontend_runtime_ir,
            frontend_symbols.registry,
            project_manifest,
        )
        artifacts["frontend_file_registry"] = artifact_store.write_frontend_file_registry(
            frontend_files.registry
        )
        for error in frontend_files.errors:
            await self._log("Compiler", error, "error")
        if not frontend_files.ok:
            await self._log("Compiler", "FRONTEND_FILE_PLANNING pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )

        # ===================================================================
        #             Skeleton Stage 3.2: Frontend Skeleton and Glue
        # ===================================================================

        await self._log(
            "Compiler",
            "Running deterministic FRONTEND_SKELETON_LOWERING for Props, Events, Stores, API Clients, UI modules, Routes, Barrels, and Imports.",
        )
        frontend_lowering = FrontendSkeletonLowerer().lower(
            frontend_runtime_ir,
            project_api_contracts(design.design_ir),
            backend_glue.route_registry,
            frontend_symbols.registry,
            frontend_files.registry,
            backend_port=request.web_port,
        )
        artifacts.update(
            artifact_store.write_frontend_lowering(
                route_registry=frontend_lowering.route_registry,
                import_plan=frontend_lowering.import_plan,
                manifest=frontend_lowering.manifest,
            )
        )
        for error in frontend_lowering.errors:
            await self._log("Compiler", error, "error")
        if not frontend_lowering.ok:
            await self._log("Compiler", "FRONTEND_SKELETON_LOWERING pass failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        artifacts.update(artifact_store.write_generated_sources(frontend_lowering.sources))
        await self._log(
            "Compiler",
            "Frontend Props/Event/Store/API Client skeletons, UI modules, Routes, Barrels, Import Plan, and Frontend Manifest generated.",
        )

        # ===================================================================
        #               Skeleton: Synchronous Build Acceptance
        # ===================================================================

        await self._log(
            "Compiler",
            "Running synchronous PROJECT_BUILD acceptance gate with npm run build.",
        )
        project_build = ProjectBuilder(request.output_dir).build()
        for error in project_build.errors:
            await self._log("Compiler", error, "error")
        if not project_build.ok:
            await self._log("Compiler", "PROJECT_BUILD acceptance gate failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )

        await self._log("Compiler", "PROJECT_BUILD acceptance gate completed successfully.")

        # ===================================================================
        #          Skeleton Stage 3.3: IR-to-Source Code Binding
        # ===================================================================

        await self._log(
            "Compiler",
            "Building and validating the deterministic IR-to-source Code Binding Registry.",
        )
        code_bindings = CodeBindingLowerer().lower(
            output_root=request.output_dir,
            requirement_ir=preprocessing.requirement_ir,
            dependency_graph=preprocessing.dependency_graph,
            database_schema=database.schema,
            design_ir=design.design_ir,
            frontend_ir=frontend_runtime_ir,
            backend_symbol_registry=symbol_planning.registry,
            backend_type_manifest=type_lowering.manifest,
            backend_module_manifests={
                "DB": db_modules.manifest,
                "FUNC": func_modules.manifest,
                "API": api_modules.manifest,
            },
            backend_route_registry=backend_glue.route_registry,
            frontend_symbol_registry=frontend_symbols.registry,
            frontend_file_registry=frontend_files.registry,
            frontend_route_registry=frontend_lowering.route_registry,
        )
        artifacts["code_bindings"] = artifact_store.write_code_bindings(
            code_bindings.registry
        )
        for error in code_bindings.errors:
            await self._log("Compiler", error, "error")
        if not code_bindings.ok:
            await self._log("Compiler", "CODE_BINDING validation failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        await self._log(
            "Compiler",
            "CODE_BINDING_READY: Design IR, TypeScript types, and real source targets are linked.",
        )

        return await self._run_tdd(
            request=request,
            artifact_store=artifact_store,
            requirement_ir=preprocessing.requirement_ir,
            dependency_graph=preprocessing.dependency_graph,
            database_schema=database.schema,
            design_ir=design.design_ir,
            frontend_ir=frontend_design_ir,
            code_binding_registry=code_bindings.registry,
            model=model,
            root_id=root_id,
            states=states,
            artifacts=artifacts,
        )

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
        model: StructuredModel | None,
        root_id: str | None,
        states: dict[str, str],
        artifacts: dict[str, str],
    ) -> CompilationResult:
        # ===================================================================
        #               Stage 4.1: Global Test Environment
        # ===================================================================

        await self._log(
            "Compiler",
            "Validating the compiler-owned Vitest, Supertest, and Playwright environment from Project Initialization.",
        )
        test_environment = TestEnvironmentInitializer(
            request.output_dir,
            backend_port=request.web_port,
        ).initialize()
        artifacts["test_environment_manifest"] = (
            artifact_store.write_test_environment_manifest(test_environment.manifest)
        )
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
            "TEST_ENVIRONMENT_READY: the preinstalled test workspace was reused without npm installation.",
        )

        # ===================================================================
        #             Stage 5: Requirement-local Node TDD
        # ===================================================================

        try:
            model = model or Model.from_env()
        except ModelConfigurationError as exc:
            await self._log("Compiler", str(exc), "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                artifacts=artifacts,
            )
        await self._log(
            "Compiler",
            "Starting node-by-node TDD in atomic dependency order.",
        )
        atomic_ids = {
            str(value)
            for value in requirement_ir.get("atomic_units", [])
            if str(value)
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
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=sorted(atomic_ids),
                artifacts=artifacts,
            )

        existing_test_manifest = None
        if str(request.start_from).upper() == "TDD":
            existing_test_manifest, manifest_error = artifact_store.read_test_manifest()
            if manifest_error:
                await self._log("Compiler", f"ARC4501: {manifest_error}", "error")
                return CompilationResult(
                    ok=False,
                    root_id=root_id,
                    states=states,
                    failed_nodes=sorted(atomic_ids),
                    artifacts=artifacts,
                )

        orchestrator = NodeTDDOrchestrator(
            model,
            request.output_dir,
            artifact_store=artifact_store,
            requirement_ir=requirement_ir,
            dependency_graph=dependency_graph,
            database_schema=database_schema,
            design_ir=design_ir,
            frontend_ir=frontend_ir,
            code_binding_registry=code_binding_registry,
            environment_manifest=test_environment.manifest,
            test_manifest=existing_test_manifest,
            resume=str(request.start_from).upper() == "TDD",
        )
        tdd_failed_nodes: list[str] = []
        atomic_layer_outcomes: dict[str, dict[str, str]] = {}
        atomic_checkpoints: dict[str, list[str]] = {}
        atomic_blocked_nodes: dict[str, str] = {}
        for requirement_id in order:
            await self._log(
                "NodeTDDOrchestrator",
                f"NODE_TDD_STARTED: generating frozen tests for {requirement_id}.",
                node_id=requirement_id,
            )
            node_result = orchestrator.run_node(requirement_id)
            states[requirement_id] = node_result.status
            atomic_layer_outcomes[requirement_id] = dict(node_result.layer_outcomes)
            atomic_checkpoints[requirement_id] = list(node_result.checkpoint_files)
            if node_result.status.startswith("BLOCKED_"):
                atomic_blocked_nodes[requirement_id] = node_result.status
            for name, path in node_result.artifacts.items():
                artifact_name = (
                    f"tdd_result:{requirement_id}" if name == "result" else name
                )
                artifacts[artifact_name] = path
            if (
                orchestrator.test_manifest is not None
                and orchestrator.test_manifest.get("status") == "TESTS_FROZEN"
            ):
                self._runtime.traceability.merge_test_links(
                    orchestrator.test_manifest
                )
            for error in node_result.errors:
                await self._log(
                    "NodeTDDOrchestrator",
                    error,
                    "warning" if not node_result.ok else "error",
                    requirement_id,
                )
            if not node_result.ok:
                tdd_failed_nodes.append(requirement_id)
                layer_summary = (
                    f" layer_outcomes={node_result.layer_outcomes}"
                    if node_result.layer_outcomes
                    else ""
                )
                await self._log(
                    "Compiler",
                    f"NODE_TDD_INCOMPLETE: {requirement_id} finished with "
                    f"{node_result.status};{layer_summary} the latest compilable "
                    "checkpoint was retained and compilation will continue with the "
                    "next atomic requirement.",
                    "warning",
                    requirement_id,
                )
                continue
            await self._log(
                "NodeTDDOrchestrator",
                f"NODE_ACCEPTED: {requirement_id} passed its frozen tests and impacted "
                f"regressions; layer_outcomes={node_result.layer_outcomes}.",
                node_id=requirement_id,
            )

        folder_ids = {
            str(value)
            for value in requirement_ir.get("folder_nodes", [])
            if str(value)
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
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=sorted(set(tdd_failed_nodes) | folder_ids),
                artifacts=artifacts,
            )

        aggregate_failed_nodes: list[str] = []
        aggregate_incomplete_targets: dict[str, list[str]] = {}
        aggregate_checkpoints: dict[str, list[str]] = {}
        aggregate_accepted_nodes: list[str] = []
        aggregate_noop_nodes: list[str] = []
        for requirement_id in folder_order:
            await self._log(
                "NodeTDDOrchestrator",
                f"AGGREGATE_IMPLEMENTATION_STARTED: resolving owned targets for {requirement_id}.",
                node_id=requirement_id,
            )
            node_result = orchestrator.run_aggregate_node(requirement_id)
            states[requirement_id] = node_result.status
            aggregate_checkpoints[requirement_id] = list(node_result.checkpoint_files)
            if node_result.status == "AGGREGATE_IMPLEMENTATION_INCOMPLETE":
                aggregate_incomplete_targets[requirement_id] = list(node_result.incomplete_targets)
            for name, path in node_result.artifacts.items():
                if name == "result":
                    artifacts[f"aggregate_result:{requirement_id}"] = path
            for error in node_result.errors:
                await self._log(
                    "NodeTDDOrchestrator", error, "warning", requirement_id
                )
            if not node_result.ok:
                aggregate_failed_nodes.append(requirement_id)
                continue
            if node_result.status == "NO_IMPLEMENTATION_REQUIRED":
                aggregate_noop_nodes.append(requirement_id)
            else:
                aggregate_accepted_nodes.append(requirement_id)
            await self._log(
                "NodeTDDOrchestrator",
                (
                    f"{node_result.status}: {requirement_id} aggregate frontend work "
                    "was processed."
                ),
                node_id=requirement_id,
            )

        failed_nodes = sorted(set(tdd_failed_nodes) | set(aggregate_failed_nodes))
        if failed_nodes:
            await self._log(
                "Compiler",
                "IMPLEMENTATION_COMPLETE_WITH_SKIPS: all atomic and aggregate requirements "
                f"were processed; atomic_failed={sorted(set(tdd_failed_nodes))}; "
                f"aggregate_failed={sorted(set(aggregate_failed_nodes))}; "
                f"aggregate_accepted={aggregate_accepted_nodes}; "
                f"aggregate_noop={aggregate_noop_nodes}; "
                f"atomic_layer_outcomes={atomic_layer_outcomes}; "
                f"atomic_blocked={atomic_blocked_nodes}; "
                f"atomic_checkpoints={atomic_checkpoints}; "
                f"aggregate_incomplete_targets={aggregate_incomplete_targets}; "
                f"aggregate_checkpoints={aggregate_checkpoints}.",
                "warning",
            )
        else:
            await self._log(
                "Compiler",
                "IMPLEMENTATION_COMPLETE: every atomic and aggregate requirement was "
                f"processed; aggregate_accepted={aggregate_accepted_nodes}; "
                f"aggregate_noop={aggregate_noop_nodes}; "
                f"atomic_layer_outcomes={atomic_layer_outcomes}; "
                f"atomic_checkpoints={atomic_checkpoints}; "
                f"aggregate_checkpoints={aggregate_checkpoints}.",
            )
        await self._log(
            "Compiler",
            "Running final PROJECT_BUILD so frontend/dist contains the implemented UI served by the backend.",
        )
        final_build = ProjectBuilder(request.output_dir).build()
        for error in final_build.errors:
            await self._log("Compiler", error, "error")
        if not final_build.ok:
            await self._log("Compiler", "FINAL_PROJECT_BUILD failed.", "error")
            return CompilationResult(
                ok=False,
                root_id=root_id,
                states=states,
                failed_nodes=failed_nodes,
                artifacts=artifacts,
            )
        await self._log(
            "Compiler",
            "FINAL_PROJECT_BUILD completed; backend static hosting now points at the current frontend/dist.",
        )
        return CompilationResult(
            # TDD node exhaustion is a non-fatal partial outcome: every node
            # was visited and its precise terminal state remains observable.
            ok=True,
            root_id=root_id,
            states=states,
            failed_nodes=failed_nodes,
            artifacts=artifacts,
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
