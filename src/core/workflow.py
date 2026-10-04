from __future__ import annotations

import os
import shutil
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from app_type_handler import create_app_type_handler, normalize_app_type
from agents.context.pipeline import context_pipeline
from core import commits, config, files, sessions
from core.phases import WorkflowPhaseRunner
from core.database import DatabasePreparation
from core.service import configure_runtime
from core.commits import build_commit_message
from core.config import load_project_env, set_app_type, set_web_port, set_workspace_root
from core.files import load_requirements, read_json_file, write_json_file
from core.logging import append_debug_log, write_terminal_log


load_project_env()

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

QUEUE_FILENAME = "processing_queue.json"
QUEUE_SCHEMA_VERSION = 2
PROJECT_METADATA_FILENAME = "project.json"
PROJECT_METADATA_SCHEMA_VERSION = 1
WORKSPACE_MODE_SCAFFOLD = "scaffold"
WORKSPACE_MODE_EVOLUTION = "evolution"
WORKSPACE_CONTROL_NAMES = {".arc", ".git", "requirements"}

PHASE_DESIGN = "DESIGN"
PHASE_IMPLEMENT = "IMPLEMENT"

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"

NODE_UNSEEN = "UNSEEN"
NODE_DESIGNING = "DESIGNING"
NODE_DESIGNED = "DESIGNED"
NODE_IMPLEMENTING = "IMPLEMENTING"
NODE_PASSED = "PASSED"
NODE_CONVERGED = "CONVERGED"
NODE_CONVERGED_WITH_FAILED_CHILDREN = "CONVERGED_WITH_FAILED_CHILDREN"
NODE_FAILED = "FAILED"


def load_project_metadata(workspace_path: str) -> dict[str, Any] | None:
    """Load validated workspace metadata used by the CLI resume path."""
    metadata_path = Path(workspace_path).expanduser().resolve() / ".arc" / PROJECT_METADATA_FILENAME
    metadata = read_json_file(metadata_path)
    if not metadata:
        return None

    app_type = str(metadata.get("app_type", "")).strip().lower()
    if app_type != normalize_app_type(app_type):
        return None

    try:
        web_port = int(metadata.get("web_port", 3000))
    except (TypeError, ValueError):
        return None
    if not 1 <= web_port <= 65535:
        return None

    return {**metadata, "app_type": app_type, "web_port": web_port}


class ARCWorkflowManager:
    """Manage the ARC requirement-tree compilation queue."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        app_type: str = "web",
        web_port: int = 3000,
        log_cb: LogCallback | None = None,
    ) -> None:
        self.workspace_path = str(Path(workspace_path).expanduser().resolve())
        self.requirement_path = str(Path(requirement_path).expanduser().resolve()) if requirement_path else ""
        self.app_type = normalize_app_type(app_type)
        self.web_port = int(web_port)
        set_workspace_root(self.workspace_path)
        self.log_cb = log_cb or _default_log_cb

        self.arc_dir = os.path.join(self.workspace_path, ".arc")
        self.queue_path = os.path.join(self.arc_dir, QUEUE_FILENAME)
        self.project_metadata_path = os.path.join(self.arc_dir, PROJECT_METADATA_FILENAME)
        self.runtime = None
        self.database_preparation = DatabasePreparation(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            log_cb=self.log_cb,
        )
        self.workspace_mode = WORKSPACE_MODE_SCAFFOLD

        set_web_port(self.web_port)
        self.interface_designer = InterfaceDesigner(
            self.log_cb,
            workspace_root=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
        )
        self.test_generator = TestGenerator(
            self.log_cb,
            workspace_root=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
        )
        self.test_driven_developer = TestDrivenDeveloper(
            self.log_cb,
            workspace_root=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
        )
        self.phase_runner = WorkflowPhaseRunner(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=self.interface_designer,
            test_generator=self.test_generator,
            test_driven_developer=self.test_driven_developer,
            log_cb=self.log_cb,
        )

    async def cleanup_workspace(self) -> bool:
        await self._log("Compiler", "Clear-and-recompile requested. Cleaning workspace...")
        try:
            Path(self.workspace_path).mkdir(parents=True, exist_ok=True)
            for item in os.listdir(self.workspace_path):
                if item == "requirements":
                    continue
                item_path = os.path.join(self.workspace_path, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.remove(item_path)
            return True
        except Exception as exc:
            await self._log("Compiler", f"Failed to clean workspace: {exc}", "error")
            return False

    async def load_requirement_tree(self) -> dict[str, Any] | None:
        await self._log("RequirementLoader", f"Reading requirements file: {self.requirement_path}")
        try:
            return load_requirements(self.requirement_path)
        except Exception as exc:
            await self._log("RequirementLoader", f"Error while reading requirements file: {exc}", "error")
            return None

    def _has_existing_application(self) -> bool:
        """Return whether the output contains application content, not ARC inputs/state."""
        workspace = Path(self.workspace_path)
        if not workspace.is_dir():
            return False
        for entry in workspace.iterdir():
            if entry.name in WORKSPACE_CONTROL_NAMES:
                continue
            if entry.is_file() or entry.is_symlink():
                return True
            if entry.is_dir() and any(path.is_file() or path.is_symlink() for path in entry.rglob("*")):
                return True
        return False

    def _reset_fresh_compilation_state(self) -> None:
        """Discard old compiler state while preserving the application and runner inputs."""
        arc_dir = Path(self.arc_dir)
        Path(self.queue_path).unlink(missing_ok=True)
        (arc_dir / "visual_analysis_cache.json").unlink(missing_ok=True)
        shutil.rmtree(arc_dir / "node_sessions", ignore_errors=True)

    async def initialize_project(self) -> bool:
        self.workspace_mode = (
            WORKSPACE_MODE_EVOLUTION if self._has_existing_application() else WORKSPACE_MODE_SCAFFOLD
        )
        os.environ["ARC_WORKSPACE_MODE"] = self.workspace_mode
        await self._log("System", f"Initializing project environment in {self.workspace_path}...")
        Path(self.arc_dir).mkdir(parents=True, exist_ok=True)
        self._reset_fresh_compilation_state()
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)
        set_web_port(self.web_port)

        traceability_dir = os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or os.path.join(
            self.arc_dir,
            "traceability",
        )
        self.runtime = configure_runtime(
            project_dir=self.workspace_path,
            traceability_dir=traceability_dir,
            app_type=self.app_type,
            web_port=self.web_port,
        )
        # A non-resume invocation always compiles a new requirement document.
        # Old application code may be the evolution baseline, but old ARC
        # queue/traceability state must not suppress nodes in the new tree.
        self.runtime.traceability.init_store(reset=True)
        self._save_project_metadata()

        app_handler = create_app_type_handler(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=self.interface_designer,
            log_cb=self.log_cb,
        )
        init_ok = await app_handler.initialize_workspace(
            seed_template=self.workspace_mode == WORKSPACE_MODE_SCAFFOLD,
        )
        if not init_ok:
            return False

        await self._log(
            "System",
            "Recording the existing application as the evolution baseline."
            if self.workspace_mode == WORKSPACE_MODE_EVOLUTION
            else "Initializing Git repository...",
        )
        self.runtime.git.ensure_repo(create_initial_commit=True)
        return True

    def _save_project_metadata(self) -> None:
        """Persist workspace-level settings needed to safely resume compilation."""
        existing = read_json_file(self.project_metadata_path) or {}
        now = datetime.now(timezone.utc).isoformat()
        write_json_file(
            self.project_metadata_path,
            {
                "schema_version": PROJECT_METADATA_SCHEMA_VERSION,
                "app_type": self.app_type,
                "web_port": self.web_port,
                "requirement_path": self.requirement_path,
                "workspace_mode": self.workspace_mode,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            },
        )

    async def prepare_resume_context(self) -> None:
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)
        set_web_port(self.web_port)
        traceability_dir = os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or os.path.join(
            self.arc_dir,
            "traceability",
        )
        self.runtime = configure_runtime(
            project_dir=self.workspace_path,
            traceability_dir=traceability_dir,
            app_type=self.app_type,
            web_port=self.web_port,
        )
        self.runtime.traceability.init_store(reset=False)
        metadata = read_json_file(self.project_metadata_path) or {}
        stored_mode = str(metadata.get("workspace_mode") or WORKSPACE_MODE_SCAFFOLD).strip().lower()
        self.workspace_mode = (
            stored_mode if stored_mode in {WORKSPACE_MODE_SCAFFOLD, WORKSPACE_MODE_EVOLUTION}
            else WORKSPACE_MODE_SCAFFOLD
        )
        os.environ["ARC_WORKSPACE_MODE"] = self.workspace_mode
        # Backfill metadata for workspaces created before project.json existed.
        if not metadata:
            self._save_project_metadata()
        self.runtime.events.mark_run_resumed("ARC compilation resumed from processing queue.")

    async def start_compilation(
        self,
        *,
        clear_all: bool = False,
        resume_from_queue: bool = False,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
        rerun_tdd_node_id: str | None = None,
        selected_test_ids: list[str] | None = None,
        add_tests_node_id: str | None = None,
        regenerate_tests_node_id: str | None = None,
        regenerate_test_id: str | None = None,
        test_intent: str | None = None,
        sync_requirements: bool = False,
    ) -> dict[str, Any]:
        await self._log("Compiler", "ARC compilation started.")
        os.environ["ARC_DATABASE_READY"] = "0"
        if rerun_tdd_node_id and not resume_from_queue:
            raise ValueError("Selected-test TDD requires resuming an existing ARC compilation workspace.")
        if rerun_tdd_node_id and (retry_failed or retry_node_ids):
            raise ValueError("Selected-test TDD cannot be combined with node retry operations.")
        if (add_tests_node_id or regenerate_tests_node_id) and not resume_from_queue:
            raise ValueError("Intent-based test generation requires resuming an existing ARC compilation workspace.")
        if add_tests_node_id and regenerate_tests_node_id:
            raise ValueError("Only one intent-based test generation operation may be requested at a time.")
        if sync_requirements and not resume_from_queue:
            raise ValueError("Requirement synchronization requires resuming an existing ARC compilation workspace.")
        if clear_all:
            cleaned = await self.cleanup_workspace()
            if not cleaned:
                return {"ok": False, "failed_nodes": []}

        requirement_tree = await self.load_requirement_tree()
        if not requirement_tree:
            return {"ok": False, "failed_nodes": []}

        if resume_from_queue:
            await self._log("Compiler", f"Resuming from existing queue: {self.queue_path}")
            await self.prepare_resume_context()
        else:
            init_ok = await self.initialize_project()
            if not init_ok:
                await self._log("Compiler", "Project initialization failed.", "error")
                return {"ok": False, "failed_nodes": []}
            self.runtime.events.mark_run_started("ARC compilation run started.")

        result = await self.compile_requirement_tree(
            requirement_tree,
            retry_failed=retry_failed,
            retry_node_ids=retry_node_ids,
            rerun_tdd_node_id=rerun_tdd_node_id,
            selected_test_ids=selected_test_ids,
            add_tests_node_id=add_tests_node_id,
            regenerate_tests_node_id=regenerate_tests_node_id,
            regenerate_test_id=regenerate_test_id,
            test_intent=test_intent,
            sync_requirements=sync_requirements,
        )
        if result.get("ok"):
            self.runtime.events.mark_run_completed("ARC compilation completed.")
            await self._log("Compiler", "Compilation finished successfully.")
        else:
            self.runtime.events.mark_run_failed("ARC compilation finished with failures.")
            failed_nodes = result.get("failed_nodes", [])
            await self._log(
                "Compiler",
                f"Compilation finished with {len(failed_nodes)} failed node(s): {', '.join(failed_nodes)}",
                "error",
            )
        return result

    async def compile_requirement_tree(
        self,
        requirement_tree: dict[str, Any],
        *,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
        rerun_tdd_node_id: str | None = None,
        selected_test_ids: list[str] | None = None,
        add_tests_node_id: str | None = None,
        regenerate_tests_node_id: str | None = None,
        regenerate_test_id: str | None = None,
        test_intent: str | None = None,
        sync_requirements: bool = False,
    ) -> dict[str, Any]:
        root_id = str(requirement_tree.get("id") or "").strip()
        if not root_id:
            await self._log("Compiler", "Requirement root node id is missing.", "error")
            return {"ok": False, "failed_nodes": []}

        previous_requirements = self.runtime.traceability.list_requirements() if sync_requirements else []
        sync_plan = self._plan_requirement_sync(previous_requirements, requirement_tree) if sync_requirements else None
        self.runtime.traceability.store_requirement_tree(requirement_tree)
        retry_requested = retry_failed or bool(retry_node_ids)
        queue_state = self._load_or_create_processing_queue(
            requirement_tree,
            require_compatible_existing_queue=retry_requested,
            allow_requirement_tree_change=sync_requirements,
        )
        pending_sync = queue_state.get("pending_requirement_sync")
        if pending_sync:
            if sync_plan is None:
                sync_plan = pending_sync
            else:
                sync_plan["affected_node_ids"] = sorted(
                    set(sync_plan["affected_node_ids"]) | set(pending_sync["affected_node_ids"])
                )
        if sync_plan is not None:
            queue_state["pending_requirement_sync"] = sync_plan
        self._sync_queue_node_states(queue_state)
        recovered_tasks = self._recover_interrupted_queue(queue_state)
        retry_plan = self._apply_retry_plan(
            queue_state,
            retry_failed=retry_failed,
            retry_node_ids=retry_node_ids,
        )
        selected_tdd_operation = None
        if rerun_tdd_node_id:
            selected_tdd_operation = self._append_selected_tdd_task(
                queue_state,
                node_id=rerun_tdd_node_id,
                test_ids=selected_test_ids,
            )
        test_generation_operation = self._append_test_generation_task(
            queue_state,
            node_id=regenerate_tests_node_id or add_tests_node_id,
            intent=test_intent,
            replace_intent=bool(regenerate_tests_node_id),
            replace_test_id=regenerate_test_id,
        )
        # Persist the node queue first so a failed global stage is resumable without
        # losing retry/interactive operations. No node may run until this gate passes.
        self._save_processing_queue(queue_state)
        database_state = await self.database_preparation.prepare(
            requirement_tree, self._requirement_tree_revision(requirement_tree), self.runtime,
        )
        queue_state["database"] = {
            key: database_state.get(key) for key in ("status", "requirements_revision", "error")
        }
        self._save_processing_queue(queue_state)
        context_pipeline.cache.clear()
        if database_state.get("status") != TASK_COMPLETED:
            return {**self._build_compile_result(queue_state), "ok": False,
                    "failed_stage": "DATABASE_PREPARE", "error": database_state.get("error", "")}
        if sync_plan is not None:
            affected = set(sync_plan["affected_node_ids"])
            affected.update(database_state.get("affected_node_ids") or [])
            # A shared table change can affect any consumer, its reverse dependencies,
            # and their composition ancestors. Propagate until a fixed point.
            records = self._flatten_requirement_tree(requirement_tree)
            known_ids = {record["req_id"] for record in records}
            affected &= known_ids
            while True:
                before = set(affected)
                for record in records:
                    if set(record["dependencies"]) & affected:
                        affected.add(record["req_id"])
                    if record["req_id"] in affected and record["parent_id"]:
                        affected.add(record["parent_id"])
                if affected == before:
                    break
            sync_plan["affected_node_ids"] = sorted(affected)
        incremental_tasks = self._append_incremental_requirement_tasks(
            queue_state,
            requirement_tree=requirement_tree,
            affected_node_ids=sync_plan["affected_node_ids"] if sync_plan else [],
        )
        queue_state.pop("pending_requirement_sync", None)
        self._save_processing_queue(queue_state)
        # Only clear schema impact after the incremental tasks are durably queued.
        database_state["affected_node_ids"] = []
        write_json_file(self.database_preparation.state_path, database_state)
        for recovered in recovered_tasks:
            await self._log(
                "Compiler",
                (
                    f"Recovered interrupted {recovered['phase']} task for node {recovered['node_id']}; "
                    "preserving existing workspace and traceability artifacts for the resumed agent."
                ),
                status="warning",
                node_id=recovered["node_id"],
            )
        for node_id in retry_plan:
            await self._log(
                "Compiler",
                f"Queued node {node_id} for retry using the existing workspace and traceability artifacts.",
                status="warning",
                node_id=node_id,
            )
        if selected_tdd_operation:
            await self._log(
                "Compiler",
                (
                    f"Queued selected-test TDD operation {selected_tdd_operation['operation_id']} "
                    f"for node {selected_tdd_operation['node_id']}: "
                    f"{', '.join(selected_tdd_operation['test_ids'])}"
                ),
                node_id=selected_tdd_operation["node_id"],
            )
        if test_generation_operation:
            await self._log(
                "Compiler",
                (
                    f"Queued test-generation operation {test_generation_operation['operation_id']} "
                    f"for node {test_generation_operation['node_id']}, test "
                    f"{test_generation_operation.get('test_id') or 'new tests'}, and intent: "
                    f"{test_generation_operation['intent']}"
                ),
                node_id=test_generation_operation["node_id"],
            )
        if sync_plan is not None:
            await self._log(
                "Compiler",
                (
                    "Requirement sync identified "
                    f"{len(sync_plan['new_node_ids'])} new and {len(sync_plan['changed_node_ids'])} changed node(s); "
                    f"queued {len(incremental_tasks)} incremental task(s)."
                ),
            )
        await self._log(
            "Compiler",
            f"Loaded processing queue with {len(queue_state['tasks'])} task(s) for root node {root_id}.",
        )

        while True:
            task = self._next_runnable_task(queue_state)
            if task is None:
                break

            node_id = task["node_id"]
            phase = task["phase"]
            requirement_data = self.runtime.traceability.get_requirement(node_id) or {}
            is_scoped_operation = bool(task.get("operation_id")) and not bool(task.get("affects_node_state"))

            task["status"] = TASK_RUNNING
            if is_scoped_operation:
                self._set_operation_status(queue_state, task, TASK_RUNNING)
            else:
                self._mark_task_running(queue_state["node_states"], node_id, phase)
            queue_state["last_task_id"] = task["task_id"]
            self._save_processing_queue(queue_state)

            await self._log("Compiler", f"Running {phase} for node {node_id}...", node_id=node_id)
            task_ok = await self._run_task(task)

            if task_ok:
                task["status"] = TASK_COMPLETED
                if task.get("operation_id"):
                    self._set_operation_status(queue_state, task, TASK_COMPLETED)
                if not is_scoped_operation:
                    sessions.merge_node_session(node_id, {"resume_context": {}})
                    new_state = self._resolve_completed_node_state(node_id, phase)
                    self._set_node_state(queue_state["node_states"], node_id, new_state)
                self._save_processing_queue(queue_state)
                if is_scoped_operation:
                    operation_label = str(task.get("mode") or phase).upper().replace("_", "-")
                    await self._commit_phase_checkpoint(node_id, f"{phase}-{operation_label}", requirement_data)
                    await self._log("Compiler", f"Interactive task completed for node {node_id}.", node_id=node_id)
                    continue
                if phase == PHASE_DESIGN:
                    self.runtime.events.mark_design_done(node_id)
                else:
                    self.runtime.events.mark_implementation_done(node_id)
                    self.runtime.events.mark_test_passed(node_id)
                await self._commit_phase_checkpoint(node_id, phase, requirement_data)
                await self._log("Compiler", f"{phase} completed for node {node_id}.", node_id=node_id)
                continue

            task["status"] = TASK_FAILED
            if task.get("operation_id"):
                self._set_operation_status(queue_state, task, TASK_FAILED)
            if not is_scoped_operation:
                self._set_node_state(queue_state["node_states"], node_id, NODE_FAILED)
                self._mark_remaining_node_tasks_failed(queue_state, node_id)
            self._save_processing_queue(queue_state)
            if is_scoped_operation:
                operation_label = str(task.get("mode") or phase).upper().replace("_", "-")
                await self._commit_phase_checkpoint(node_id, f"{phase}-{operation_label}-FAILED", requirement_data)
                await self._log(
                    "Compiler",
                    f"Interactive task failed for node {node_id}; preserving its prior compile state.",
                    "error",
                    node_id,
                )
                continue
            if phase == PHASE_DESIGN:
                self.runtime.events.mark_design_failed(node_id)
            else:
                self.runtime.events.mark_implementation_failed(node_id)
                self.runtime.events.mark_test_failed(node_id)
            await self._commit_phase_checkpoint(node_id, f"{phase}-FAILED", requirement_data)
            await self._log("Compiler", f"{phase} failed for node {node_id}.", "error", node_id)

        return self._build_compile_result(queue_state)

    def _append_selected_tdd_task(
        self,
        queue_state: dict[str, Any],
        *,
        node_id: str | None,
        test_ids: list[str] | None,
    ) -> dict[str, Any] | None:
        normalized_node_id = str(node_id or "").strip()
        normalized_test_ids = list(dict.fromkeys(str(test_id or "").strip() for test_id in test_ids or [] if str(test_id or "").strip()))
        if not normalized_node_id and not normalized_test_ids:
            return None
        if not normalized_node_id or not normalized_test_ids:
            raise ValueError("Selected-test TDD requires both a node id and at least one test id.")
        if normalized_node_id not in queue_state.get("node_states", {}):
            raise ValueError(f"Selected-test TDD requested for unknown node {normalized_node_id}.")

        registered_tests = {
            str(test.get("test_id") or "").strip(): test
            for test in self.runtime.traceability.list_tests(req_id=normalized_node_id)
            if isinstance(test, dict) and str(test.get("test_id") or "").strip()
        }
        unknown_test_ids = [test_id for test_id in normalized_test_ids if test_id not in registered_tests]
        if unknown_test_ids:
            raise ValueError(
                f"Selected-test TDD requested unregistered test id(s) for node {normalized_node_id}: "
                f"{', '.join(unknown_test_ids)}"
            )

        operations = queue_state.setdefault("operations", {})
        if not isinstance(operations, dict):
            raise ValueError("Processing queue operations must be an object.")
        operation_id = f"op-{len(operations) + 1:04d}"
        while operation_id in operations:
            operation_id = f"op-{int(operation_id.split('-')[-1]) + 1:04d}"
        order = max((int(task.get("order", -1)) for task in queue_state.get("tasks", [])), default=-1) + 1
        task = {
            "task_id": f"{operation_id}:{normalized_node_id}:{PHASE_IMPLEMENT}",
            "operation_id": operation_id,
            "node_id": normalized_node_id,
            "phase": PHASE_IMPLEMENT,
            "mode": "selected_tests",
            "test_ids": normalized_test_ids,
            "order": order,
            "status": TASK_PENDING,
        }
        queue_state.setdefault("tasks", []).append(task)
        operations[operation_id] = {
            "kind": "rerun_tdd",
            "node_id": normalized_node_id,
            "test_ids": normalized_test_ids,
            "status": TASK_PENDING,
        }
        queue_state["last_operation_id"] = operation_id
        return task

    @staticmethod
    def _set_operation_status(queue_state: dict[str, Any], task: dict[str, Any], status: str) -> None:
        operation_id = str(task.get("operation_id") or "").strip()
        operation = queue_state.get("operations", {}).get(operation_id)
        if isinstance(operation, dict):
            operation["status"] = status

    def _plan_requirement_sync(
        self,
        previous_requirements: list[dict[str, Any]],
        requirement_tree: dict[str, Any],
    ) -> dict[str, list[str]]:
        current_requirements = self._flatten_requirement_tree(requirement_tree)
        previous_by_id = {
            str(requirement.get("req_id") or requirement.get("id") or "").strip(): requirement
            for requirement in previous_requirements
            if isinstance(requirement, dict) and str(requirement.get("req_id") or requirement.get("id") or "").strip()
        }
        current_by_id = {str(requirement["req_id"]): requirement for requirement in current_requirements}
        removed_node_ids = sorted(set(previous_by_id) - set(current_by_id))
        if removed_node_ids:
            raise ValueError(
                "Requirement synchronization does not support deleting nodes; run a full compile instead. "
                f"Removed node id(s): {', '.join(removed_node_ids)}"
            )
        moved_node_ids = sorted(
            node_id
            for node_id in set(previous_by_id) & set(current_by_id)
            if str(previous_by_id[node_id].get("parent_id") or "").strip()
            != str(current_by_id[node_id].get("parent_id") or "").strip()
        )
        if moved_node_ids:
            raise ValueError(
                "Requirement synchronization does not support moving nodes or changing their parent; run a full compile instead. "
                f"Moved node id(s): {', '.join(moved_node_ids)}"
            )

        new_node_ids = sorted(set(current_by_id) - set(previous_by_id))
        changed_node_ids = sorted(
            node_id
            for node_id in set(previous_by_id) & set(current_by_id)
            if self._requirement_revision_payload(previous_by_id[node_id])
            != self._requirement_revision_payload(current_by_id[node_id])
        )
        affected = set(new_node_ids) | set(changed_node_ids)
        parent_by_id = {node_id: str(requirement.get("parent_id") or "").strip() for node_id, requirement in current_by_id.items()}
        for node_id in list(affected):
            parent_id = parent_by_id.get(node_id, "")
            while parent_id:
                affected.add(parent_id)
                parent_id = parent_by_id.get(parent_id, "")
        changed_or_new = set(new_node_ids) | set(changed_node_ids)
        for node_id, requirement in current_by_id.items():
            dependencies = {str(value or "").strip() for value in requirement.get("dependencies") or []}
            if dependencies & changed_or_new:
                affected.add(node_id)
        for node_id in list(affected):
            parent_id = parent_by_id.get(node_id, "")
            while parent_id:
                affected.add(parent_id)
                parent_id = parent_by_id.get(parent_id, "")
        return {
            "new_node_ids": new_node_ids,
            "changed_node_ids": changed_node_ids,
            "affected_node_ids": sorted(affected),
        }

    @staticmethod
    def _flatten_requirement_tree(requirement_tree: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def walk(node: dict[str, Any], parent_id: str | None = None) -> None:
            node_id = str(node.get("id") or node.get("req_id") or "").strip()
            if not node_id:
                return
            children = [child for child in node.get("children", []) or [] if isinstance(child, dict)]
            records.append(
                {
                    "req_id": node_id,
                    "name": str(node.get("name") or "").strip(),
                    "description": str(node.get("description") or "").strip(),
                    "visual_reference": list(node.get("visual_reference") or []),
                    "scenarios": [dict(item) for item in node.get("scenarios", []) or [] if isinstance(item, dict)],
                    "parent_id": parent_id,
                    "children_ids": [
                        str(child.get("id") or child.get("req_id") or "").strip()
                        for child in children
                        if str(child.get("id") or child.get("req_id") or "").strip()
                    ],
                    "dependencies": [str(value).strip() for value in node.get("dependencies", []) or [] if str(value).strip()],
                }
            )
            for child in children:
                walk(child, node_id)

        walk(requirement_tree)
        return records

    @staticmethod
    def _requirement_revision_payload(requirement: dict[str, Any]) -> str:
        payload = {
            "name": str(requirement.get("name") or "").strip(),
            "description": str(requirement.get("description") or "").strip(),
            "visual_reference": requirement.get("visual_reference") or [],
            "scenarios": requirement.get("scenarios") or [],
            "parent_id": str(requirement.get("parent_id") or "").strip(),
            "children_ids": requirement.get("children_ids") or [],
            "dependencies": requirement.get("dependencies") or [],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def _append_incremental_requirement_tasks(
        self,
        queue_state: dict[str, Any],
        *,
        requirement_tree: dict[str, Any],
        affected_node_ids: list[str],
    ) -> list[dict[str, Any]]:
        affected = {str(node_id).strip() for node_id in affected_node_ids if str(node_id).strip()}
        if not affected:
            return []
        queue_state.setdefault("node_states", {})
        for node_id in affected:
            queue_state["node_states"].setdefault(node_id, NODE_UNSEEN)

        operations = queue_state.setdefault("operations", {})
        tasks: list[dict[str, Any]] = []
        next_order = max((int(task.get("order", -1)) for task in queue_state.get("tasks", [])), default=-1) + 1
        next_operation_number = len(operations) + 1
        for template in self._build_processing_tasks(requirement_tree):
            if template["node_id"] not in affected:
                continue
            operation_id = f"op-{next_operation_number:04d}"
            while operation_id in operations:
                next_operation_number += 1
                operation_id = f"op-{next_operation_number:04d}"
            task = {
                **template,
                "task_id": f"{operation_id}:{template['node_id']}:{template['phase']}",
                "operation_id": operation_id,
                "mode": "full",
                "affects_node_state": True,
                "order": next_order,
                "status": TASK_PENDING,
            }
            queue_state.setdefault("tasks", []).append(task)
            operations[operation_id] = {
                "kind": "incremental_requirement_compile",
                "node_id": template["node_id"],
                "phase": template["phase"],
                "status": TASK_PENDING,
            }
            queue_state["last_operation_id"] = operation_id
            tasks.append(task)
            next_order += 1
            next_operation_number += 1
        return tasks

    def _append_test_generation_task(
        self,
        queue_state: dict[str, Any],
        *,
        node_id: str | None,
        intent: str | None,
        replace_intent: bool,
        replace_test_id: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_node_id = str(node_id or "").strip()
        normalized_intent = str(intent or "").strip()
        if not normalized_node_id and not normalized_intent:
            return None
        if not normalized_node_id or not normalized_intent:
            raise ValueError("Intent-based test generation requires both a node id and a non-empty intent.")
        requirement = self.runtime.traceability.get_requirement(normalized_node_id)
        if not requirement:
            raise ValueError(f"Intent-based test generation requested for unknown node {normalized_node_id}.")
        if requirement.get("children_ids"):
            raise ValueError("Intent-based test generation is only available for leaf requirement nodes.")
        normalized_replace_test_id = str(replace_test_id or "").strip()
        if replace_intent:
            if not normalized_replace_test_id:
                raise ValueError("Regenerating a test requires a test id.")
            registered_tests = {
                str(test.get("test_id") or "").strip()
                for test in self.runtime.traceability.list_tests(req_id=normalized_node_id)
                if isinstance(test, dict)
            }
            if normalized_replace_test_id not in registered_tests:
                raise ValueError(
                    f"Test regeneration requested unregistered test id {normalized_replace_test_id} "
                    f"for node {normalized_node_id}."
                )

        operations = queue_state.setdefault("operations", {})
        if not isinstance(operations, dict):
            raise ValueError("Processing queue operations must be an object.")
        operation_id = f"op-{len(operations) + 1:04d}"
        while operation_id in operations:
            operation_id = f"op-{int(operation_id.split('-')[-1]) + 1:04d}"
        mode = "tests_only_replace" if replace_intent else "tests_only_append"
        order = max((int(task.get("order", -1)) for task in queue_state.get("tasks", [])), default=-1) + 1
        task = {
            "task_id": f"{operation_id}:{normalized_node_id}:{PHASE_DESIGN}",
            "operation_id": operation_id,
            "node_id": normalized_node_id,
            "phase": PHASE_DESIGN,
            "mode": mode,
            "intent": normalized_intent,
            "order": order,
            "status": TASK_PENDING,
        }
        if normalized_replace_test_id:
            task["test_id"] = normalized_replace_test_id
        queue_state.setdefault("tasks", []).append(task)
        operations[operation_id] = {
            "kind": "regenerate_tests" if replace_intent else "add_tests",
            "node_id": normalized_node_id,
            "intent": normalized_intent,
            "status": TASK_PENDING,
        }
        if normalized_replace_test_id:
            operations[operation_id]["test_id"] = normalized_replace_test_id
        queue_state["last_operation_id"] = operation_id
        return task

    def _load_or_create_processing_queue(
        self,
        requirement_tree: dict[str, Any],
        *,
        require_compatible_existing_queue: bool = False,
        allow_requirement_tree_change: bool = False,
    ) -> dict[str, Any]:
        os.makedirs(self.arc_dir, exist_ok=True)
        root_id = str(requirement_tree.get("id", ""))
        expected_tasks = self._build_processing_tasks(requirement_tree)
        expected_task_ids = [task["task_id"] for task in expected_tasks]
        node_ids = self._collect_node_ids(expected_tasks)
        existing_queue = read_json_file(self.queue_path)
        if self._is_compatible_queue(existing_queue, root_id, expected_task_ids):
            queue_state = self._migrate_processing_queue(existing_queue, requirement_tree)
            queue_state.setdefault("node_states", {})
            for node_id in node_ids:
                queue_state["node_states"].setdefault(node_id, NODE_UNSEEN)
            self._apply_saved_states_to_tasks(queue_state)
            return queue_state
        if allow_requirement_tree_change and existing_queue and existing_queue.get("root_id") == root_id:
            queue_state = self._migrate_processing_queue(existing_queue, requirement_tree)
            queue_state["tree_revision"] = self._requirement_tree_revision(requirement_tree)
            return queue_state
        if require_compatible_existing_queue:
            raise ValueError(
                "Retry requested, but the existing processing queue is missing or incompatible with the current requirement tree."
            )
        queue_state = {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "tree_revision": self._requirement_tree_revision(requirement_tree),
            "root_id": root_id,
            "tasks": expected_tasks,
            "node_states": {node_id: NODE_UNSEEN for node_id in node_ids},
            "last_task_id": None,
            "last_operation_id": None,
            "operations": {},
        }
        self._apply_saved_states_to_tasks(queue_state)
        return queue_state

    @staticmethod
    def _requirement_tree_revision(requirement_tree: dict[str, Any]) -> str:
        canonical_tree = json.dumps(
            requirement_tree,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical_tree.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def _migrate_processing_queue(
        self,
        queue_state: dict[str, Any],
        requirement_tree: dict[str, Any],
    ) -> dict[str, Any]:
        """Add interactive-queue metadata without changing legacy task semantics."""

        migrated = dict(queue_state)
        migrated.setdefault("schema_version", QUEUE_SCHEMA_VERSION)
        migrated.setdefault("tree_revision", self._requirement_tree_revision(requirement_tree))
        migrated.setdefault("operations", {})
        migrated.setdefault("last_operation_id", None)
        return migrated

    def _build_processing_tasks(self, root_node: dict[str, Any]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            node_id = str(node.get("id", "")).strip()
            if not node_id:
                return
            tasks.append(self._make_task(node_id, PHASE_DESIGN, len(tasks)))
            for child in node.get("children", []) or []:
                if isinstance(child, dict):
                    walk(child)
            tasks.append(self._make_task(node_id, PHASE_IMPLEMENT, len(tasks)))

        walk(root_node)
        return tasks

    def _make_task(self, node_id: str, phase: str, order: int) -> dict[str, Any]:
        return {
            "task_id": f"{node_id}:{phase}",
            "node_id": node_id,
            "phase": phase,
            "order": order,
            "status": TASK_PENDING,
        }

    @staticmethod
    def _collect_node_ids(tasks: list[dict[str, Any]]) -> list[str]:
        seen: list[str] = []
        for task in tasks:
            node_id = task["node_id"]
            if node_id not in seen:
                seen.append(node_id)
        return seen

    @staticmethod
    def _is_compatible_queue(queue_state: dict[str, Any] | None, root_id: str, expected_task_ids: list[str]) -> bool:
        if not queue_state or queue_state.get("root_id") != root_id:
            return False
        base_task_ids = [
            task.get("task_id")
            for task in queue_state.get("tasks", [])
            if not task.get("operation_id")
        ]
        return base_task_ids == expected_task_ids

    @staticmethod
    def _apply_saved_states_to_tasks(queue_state: dict[str, Any]) -> None:
        for task in queue_state["tasks"]:
            if task.get("operation_id"):
                continue
            node_state = queue_state["node_states"].get(task["node_id"], NODE_UNSEEN)
            if node_state in {NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN}:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_DESIGNED and task["phase"] == PHASE_DESIGN:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_FAILED:
                task["status"] = TASK_FAILED

    def _recover_interrupted_queue(self, queue_state: dict[str, Any]) -> list[dict[str, str]]:
        recovered: list[dict[str, str]] = []
        git_status = ""
        if self.runtime is not None:
            try:
                git_status = self.runtime.git.status_porcelain().strip()
            except Exception:
                git_status = ""
        for task in queue_state["tasks"]:
            if task["status"] == TASK_RUNNING:
                node_id = str(task.get("node_id", "") or "").strip()
                phase = str(task.get("phase", "") or "").strip()
                if task.get("operation_id"):
                    task["status"] = TASK_PENDING
                    self._set_operation_status(queue_state, task, TASK_PENDING)
                    recovered.append({"node_id": node_id, "phase": phase, "task_id": str(task.get("task_id", ""))})
                    continue
                previous_state = str(queue_state.get("node_states", {}).get(node_id, NODE_UNSEEN) or NODE_UNSEEN)
                task["status"] = TASK_PENDING
                fallback_state = NODE_DESIGNED if phase == PHASE_IMPLEMENT else NODE_UNSEEN
                if node_id:
                    queue_state["node_states"][node_id] = fallback_state
                    if self.runtime is not None:
                        self.runtime.traceability.upsert_node_state(node_id, fallback_state)
                    sessions.merge_node_session(
                        node_id,
                        {
                            "resume_context": {
                                "interrupted": True,
                                "task_id": str(task.get("task_id", "") or "").strip(),
                                "phase": phase,
                                "previous_node_state": previous_state,
                                "recovered_node_state": fallback_state,
                                "git_status": git_status.splitlines()[:80],
                                "instruction": (
                                    "This node is resuming after an interrupted agent stage. "
                                    "Preserve useful existing source, test, and traceability artifacts; inspect the listed dirty files "
                                    "and current-node records before regenerating or overwriting work."
                                ),
                            },
                            "phase_status": {phase.lower(): "interrupted"} if phase else {},
                        },
                    )
                recovered.append({"node_id": node_id, "phase": phase, "task_id": str(task.get("task_id", ""))})
        queue_state["recovered_interrupted_tasks"] = recovered
        return recovered

    @staticmethod
    def _next_runnable_task(queue_state: dict[str, Any]) -> dict[str, Any] | None:
        for task in queue_state["tasks"]:
            if task["status"] == TASK_PENDING:
                return task
        return None

    @staticmethod
    def _mark_remaining_node_tasks_failed(queue_state: dict[str, Any], node_id: str) -> None:
        for task in queue_state["tasks"]:
            if task["node_id"] == node_id and task["status"] in {TASK_PENDING, TASK_RUNNING}:
                task["status"] = TASK_FAILED

    def _apply_retry_plan(
        self,
        queue_state: dict[str, Any],
        *,
        retry_failed: bool = False,
        retry_node_ids: list[str] | None = None,
    ) -> list[str]:
        requested_ids: list[str] = []
        if retry_failed:
            requested_ids = [
                node_id
                for node_id, state in queue_state.get("node_states", {}).items()
                if str(state or "").strip().upper() == NODE_FAILED
            ]
        elif retry_node_ids:
            requested_ids = [str(node_id).strip() for node_id in retry_node_ids if str(node_id).strip()]

        requested_ids = list(dict.fromkeys(requested_ids))
        if not requested_ids:
            return []

        known_ids = set(queue_state.get("node_states", {}).keys())
        unknown_ids = [node_id for node_id in requested_ids if node_id not in known_ids]
        if unknown_ids:
            raise ValueError(f"Retry requested for unknown node id(s): {', '.join(unknown_ids)}")

        for node_id in requested_ids:
            self._reset_node_for_retry(queue_state, node_id)

        queue_state["last_task_id"] = None
        queue_state["retry_plan"] = {"requested_node_ids": requested_ids, "retry_failed": bool(retry_failed)}
        return requested_ids

    def _reset_node_for_retry(self, queue_state: dict[str, Any], node_id: str) -> str:
        design_task = None
        implement_task = None
        for task in queue_state["tasks"]:
            if task["node_id"] != node_id:
                continue
            if task["phase"] == PHASE_DESIGN:
                design_task = task
            elif task["phase"] == PHASE_IMPLEMENT:
                implement_task = task
        if design_task is None or implement_task is None:
            raise ValueError(f"Retry requested for node {node_id}, but its queue tasks are incomplete.")

        design_status = str(design_task.get("status") or "").strip().upper()
        implement_status = str(implement_task.get("status") or "").strip().upper()
        design_failed = design_status == TASK_FAILED
        implement_failed = implement_status == TASK_FAILED

        if design_failed or design_status != TASK_COMPLETED:
            self._reset_node_from_design_retry(queue_state, node_id, design_task, implement_task)
            return "design"

        if implement_failed:
            self._reset_node_from_implement_retry(queue_state, node_id, design_task, implement_task)
            return "implement"

        self._reset_node_for_full_retry(queue_state, node_id, design_task, implement_task)
        return "design"

    def _reset_node_from_design_retry(
        self,
        queue_state: dict[str, Any],
        node_id: str,
        design_task: dict[str, Any],
        implement_task: dict[str, Any],
    ) -> None:
        design_task["status"] = TASK_PENDING
        implement_task["status"] = TASK_PENDING
        self.runtime.traceability.clear_node_design_artifacts(node_id)
        self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        self._set_node_state(queue_state["node_states"], node_id, NODE_UNSEEN)
        sessions.merge_node_session(
            node_id,
            {
                "interfaces": [],
                "materialized_files": [],
                "test_artifacts": [],
                "recent_failure_summary": "",
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "resume_context": {},
                "result_state": "",
            },
        )
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)

    def _reset_node_from_implement_retry(
        self,
        queue_state: dict[str, Any],
        node_id: str,
        design_task: dict[str, Any],
        implement_task: dict[str, Any],
    ) -> None:
        design_task["status"] = TASK_COMPLETED
        implement_task["status"] = TASK_PENDING
        self.runtime.traceability.reset_test_pass_statuses_for_requirement(node_id)
        self._set_node_state(queue_state["node_states"], node_id, NODE_DESIGNED)
        sessions.merge_node_session(
            node_id,
            {
                "phase_status": {"implement": "pending"},
                "resume_context": {},
                "result_state": "",
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

    def _reset_node_for_full_retry(
        self,
        queue_state: dict[str, Any],
        node_id: str,
        design_task: dict[str, Any],
        implement_task: dict[str, Any],
    ) -> None:
        design_task["status"] = TASK_PENDING
        implement_task["status"] = TASK_PENDING
        self._set_node_state(queue_state["node_states"], node_id, NODE_UNSEEN)
        sessions.merge_node_session(
            node_id,
            {
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "resume_context": {},
                "result_state": "",
                "recent_failure_summary": "",
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

    async def _run_task(self, task: dict[str, Any]) -> bool:
        node_id = task["node_id"]
        requirement_data = self.runtime.traceability.get_requirement(node_id)
        if not requirement_data:
            await self._log("System", f"Requirement node {node_id} not found in database.", "error", node_id)
            return False
        if task["phase"] == PHASE_DESIGN:
            if task.get("mode") in {"tests_only_append", "tests_only_replace"}:
                return await self.phase_runner.run_test_generation_phase(
                    node_id,
                    requirement_data,
                    intent=str(task.get("intent") or "").strip(),
                    replace_test_id=str(task.get("test_id") or "").strip() or None,
                )
            return await self.phase_runner.run_design_phase(node_id, requirement_data)
        if task.get("mode") == "selected_tests":
            return await self.phase_runner.run_implement_phase(
                node_id,
                requirement_data,
                test_ids=task.get("test_ids") or [],
            )
        return await self.phase_runner.run_implement_phase(node_id, requirement_data)

    async def _commit_phase_checkpoint(self, node_id: str, phase: str, requirement_data: dict[str, Any]) -> None:
        commit_message = build_commit_message(node_id, phase, requirement_data)
        await self._log("Compiler", f"Running git checkpoint for {phase} on node {node_id}...", node_id=node_id)
        committed = self.runtime.git.commit(commit_message)
        if not committed:
            await self._log("Compiler", "No file changes detected for this checkpoint.", node_id=node_id)

    def _resolve_completed_node_state(self, node_id: str, phase: str) -> str:
        if phase == PHASE_DESIGN:
            return NODE_DESIGNED
        session = read_json_file(os.path.join(self.arc_dir, "node_sessions", f"{node_id}.json")) or {}
        result_state = str(session.get("result_state", "")).strip().upper()
        if result_state == NODE_CONVERGED_WITH_FAILED_CHILDREN:
            return NODE_CONVERGED_WITH_FAILED_CHILDREN
        if result_state == NODE_CONVERGED:
            return NODE_CONVERGED
        return NODE_PASSED

    def _set_node_state(self, node_states: dict[str, str], node_id: str, state: str) -> None:
        node_states[node_id] = state
        self.runtime.traceability.upsert_node_state(node_id, state)

    def _sync_queue_node_states(self, queue_state: dict[str, Any]) -> None:
        for node_id, state in queue_state.get("node_states", {}).items():
            normalized_state = str(state or NODE_UNSEEN).strip().upper() or NODE_UNSEEN
            self.runtime.traceability.upsert_node_state(node_id, normalized_state)

    def _mark_task_running(self, node_states: dict[str, str], node_id: str, phase: str) -> None:
        if phase == PHASE_DESIGN:
            self._set_node_state(node_states, node_id, NODE_DESIGNING)
            self.runtime.events.mark_design_started(node_id)
            return
        self._set_node_state(node_states, node_id, NODE_IMPLEMENTING)
        self.runtime.events.mark_implementation_started(node_id)

    @staticmethod
    def _build_compile_result(queue_state: dict[str, Any]) -> dict[str, Any]:
        failed_nodes = sorted(
            node_id for node_id, state in queue_state["node_states"].items() if state == NODE_FAILED
        )
        completed_tasks = [task["task_id"] for task in queue_state["tasks"] if task["status"] == TASK_COMPLETED]
        base_tasks = [task for task in queue_state["tasks"] if not task.get("operation_id")]
        base_compilation_completed = all(task["status"] == TASK_COMPLETED for task in base_tasks)
        last_operation_id = str(queue_state.get("last_operation_id") or "").strip()
        last_operation = queue_state.get("operations", {}).get(last_operation_id)
        last_operation_completed = not last_operation_id or (
            isinstance(last_operation, dict) and last_operation.get("status") == TASK_COMPLETED
        )
        failed_operations = sorted(
            operation_id
            for operation_id, operation in queue_state.get("operations", {}).items()
            if isinstance(operation, dict) and operation.get("status") == TASK_FAILED
        )
        return {
            "ok": base_compilation_completed and last_operation_completed and not failed_nodes
            and queue_state.get("database", {}).get("status", TASK_COMPLETED) == TASK_COMPLETED,
            "failed_nodes": failed_nodes,
            "failed_operations": failed_operations,
            "visit_order": completed_tasks,
            "states": dict(queue_state["node_states"]),
        }

    def _save_processing_queue(self, queue_state: dict[str, Any]) -> None:
        write_json_file(self.queue_path, queue_state)

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        result = self.log_cb(agent_name, message, status, node_id)
        if hasattr(result, "__await__"):
            await result


class _CompletedLogAwaitable:
    def __await__(self):
        if False:
            yield None
        return None


def _default_log_cb(
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> _CompletedLogAwaitable:
    append_debug_log(agent_name, message, status=status, node_id=node_id)
    write_terminal_log(agent_name, message, status=status, node_id=node_id)
    return _CompletedLogAwaitable()
