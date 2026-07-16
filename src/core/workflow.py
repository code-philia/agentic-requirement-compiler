from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from app_type_handler import create_app_type_handler, normalize_app_type
from core.phases import WorkflowPhaseRunner
from core.service import configure_runtime
from core.utils import (
    build_commit_message,
    load_project_env,
    load_requirements,
    read_json_file,
    set_app_type,
    set_web_port,
    set_workspace_root,
    write_json_file,
)
from tools.logging import append_debug_log, write_terminal_log


load_project_env()

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

QUEUE_FILENAME = "processing_queue.json"

PHASE_DESIGN = "DESIGN"
PHASE_IMPLEMENT = "IMPLEMENT"

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"

NODE_UNSEEN = "UNSEEN"
NODE_DESIGNED = "DESIGNED"
NODE_PASSED = "PASSED"
NODE_CONVERGED = "CONVERGED"
NODE_CONVERGED_WITH_FAILED_CHILDREN = "CONVERGED_WITH_FAILED_CHILDREN"
NODE_FAILED = "FAILED"


class ARCWorkflowManager:
    """Manage the ARC requirement-tree compilation queue."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        app_type: str = "web",
        web_port: int = 3301,
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
        self.runtime = None

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

    async def initialize_project(self) -> bool:
        await self._log("System", f"Initializing project environment in {self.workspace_path}...")
        Path(self.arc_dir).mkdir(parents=True, exist_ok=True)
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)
        set_web_port(self.web_port)

        db_path = os.environ.get("ARCBENCH_TRACEABILITY_DB_PATH", "").strip() or os.path.join(
            self.arc_dir,
            "traceability.db",
        )
        self.runtime = configure_runtime(
            project_dir=self.workspace_path,
            traceability_db_path=db_path,
            app_type=self.app_type,
            web_port=self.web_port,
        )
        self.runtime.traceability.init_db(reset=False)

        app_handler = create_app_type_handler(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=self.interface_designer,
            log_cb=self.log_cb,
        )
        init_ok = await app_handler.initialize_workspace()
        if not init_ok:
            return False

        await self._log("System", "Initializing Git repository...")
        self.runtime.git.ensure_repo(create_initial_commit=True)
        return True

    async def prepare_resume_context(self) -> None:
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)
        set_web_port(self.web_port)
        db_path = os.environ.get("ARCBENCH_TRACEABILITY_DB_PATH", "").strip() or os.path.join(
            self.arc_dir,
            "traceability.db",
        )
        self.runtime = configure_runtime(
            project_dir=self.workspace_path,
            traceability_db_path=db_path,
            app_type=self.app_type,
            web_port=self.web_port,
        )
        self.runtime.traceability.init_db(reset=False)
        self.runtime.events.mark_run_resumed("ARC compilation resumed from processing queue.")

    async def start_compilation(
        self,
        *,
        clear_all: bool = False,
        resume_from_queue: bool = False,
    ) -> dict[str, Any]:
        await self._log("Compiler", "ARC compilation started.")
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

        result = await self.compile_requirement_tree(requirement_tree)
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

    async def compile_requirement_tree(self, requirement_tree: dict[str, Any]) -> dict[str, Any]:
        root_id = str(requirement_tree.get("id") or "").strip()
        if not root_id:
            await self._log("Compiler", "Requirement root node id is missing.", "error")
            return {"ok": False, "failed_nodes": []}

        self.runtime.traceability.store_requirement_tree(requirement_tree)
        queue_state = self._load_or_create_processing_queue(requirement_tree)
        self._recover_interrupted_queue(queue_state)
        self._save_processing_queue(queue_state)
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

            task["status"] = TASK_RUNNING
            queue_state["last_task_id"] = task["task_id"]
            self._save_processing_queue(queue_state)

            await self._log("Compiler", f"Running {phase} for node {node_id}...", node_id=node_id)
            task_ok = await self._run_task(task)

            if task_ok:
                task["status"] = TASK_COMPLETED
                new_state = self._resolve_completed_node_state(node_id, phase)
                self._set_node_state(queue_state["node_states"], node_id, new_state)
                self._save_processing_queue(queue_state)
                if phase == PHASE_DESIGN:
                    self.runtime.events.mark_design_done(node_id)
                else:
                    self.runtime.events.mark_implementation_done(node_id)
                    self.runtime.events.mark_test_passed(node_id)
                await self._commit_phase_checkpoint(node_id, phase, requirement_data)
                await self._log("Compiler", f"{phase} completed for node {node_id}.", node_id=node_id)
                continue

            task["status"] = TASK_FAILED
            self._set_node_state(queue_state["node_states"], node_id, NODE_FAILED)
            self._mark_remaining_node_tasks_failed(queue_state, node_id)
            self._save_processing_queue(queue_state)
            if phase == PHASE_DESIGN:
                self.runtime.events.mark_design_failed(node_id)
            else:
                self.runtime.events.mark_test_failed(node_id)
            await self._commit_phase_checkpoint(node_id, f"{phase}-FAILED", requirement_data)
            await self._log("Compiler", f"{phase} failed for node {node_id}.", "error", node_id)

        return self._build_compile_result(queue_state)

    def _load_or_create_processing_queue(self, requirement_tree: dict[str, Any]) -> dict[str, Any]:
        os.makedirs(self.arc_dir, exist_ok=True)
        root_id = str(requirement_tree.get("id", ""))
        expected_tasks = self._build_processing_tasks(requirement_tree)
        expected_task_ids = [task["task_id"] for task in expected_tasks]
        node_ids = self._collect_node_ids(expected_tasks)
        existing_queue = read_json_file(self.queue_path)
        if self._is_compatible_queue(existing_queue, root_id, expected_task_ids):
            queue_state = existing_queue
            queue_state.setdefault("node_states", {})
            for node_id in node_ids:
                queue_state["node_states"].setdefault(node_id, NODE_UNSEEN)
            self._apply_saved_states_to_tasks(queue_state)
            return queue_state
        queue_state = {
            "root_id": root_id,
            "tasks": expected_tasks,
            "node_states": {node_id: NODE_UNSEEN for node_id in node_ids},
            "last_task_id": None,
        }
        self._apply_saved_states_to_tasks(queue_state)
        return queue_state

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
        return [task.get("task_id") for task in queue_state.get("tasks", [])] == expected_task_ids

    @staticmethod
    def _apply_saved_states_to_tasks(queue_state: dict[str, Any]) -> None:
        for task in queue_state["tasks"]:
            node_state = queue_state["node_states"].get(task["node_id"], NODE_UNSEEN)
            if node_state in {NODE_PASSED, NODE_CONVERGED, NODE_CONVERGED_WITH_FAILED_CHILDREN}:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_DESIGNED and task["phase"] == PHASE_DESIGN:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_FAILED:
                task["status"] = TASK_FAILED

    @staticmethod
    def _recover_interrupted_queue(queue_state: dict[str, Any]) -> None:
        for task in queue_state["tasks"]:
            if task["status"] == TASK_RUNNING:
                task["status"] = TASK_PENDING

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

    async def _run_task(self, task: dict[str, Any]) -> bool:
        node_id = task["node_id"]
        requirement_data = self.runtime.traceability.get_requirement(node_id)
        if not requirement_data:
            await self._log("System", f"Requirement node {node_id} not found in database.", "error", node_id)
            return False
        if task["phase"] == PHASE_DESIGN:
            return await self.phase_runner.run_design_phase(node_id, requirement_data)
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

    @staticmethod
    def _build_compile_result(queue_state: dict[str, Any]) -> dict[str, Any]:
        failed_nodes = sorted(
            node_id for node_id, state in queue_state["node_states"].items() if state == NODE_FAILED
        )
        completed_tasks = [task["task_id"] for task in queue_state["tasks"] if task["status"] == TASK_COMPLETED]
        all_completed = all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])
        return {
            "ok": all_completed and not failed_nodes,
            "failed_nodes": failed_nodes,
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
