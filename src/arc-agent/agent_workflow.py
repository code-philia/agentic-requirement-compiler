import inspect
import os
import shutil
from typing import Any, Awaitable, Callable

from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from app_types import create_app_type_handler, normalize_app_type
from traceability import store_all_requirement
from traceability.database import get_requirement_by_id, init_db, set_db_path, upsert_node_state
from utils import (
    build_commit_message,
    ensure_arc_gitignore,
    load_requirements,
    read_json_file,
    run_git_commit,
    run_git_init,
    set_app_type,
    set_workspace_root,
    write_json_file,
)

from dotenv import load_dotenv
load_dotenv()

# ======================================================================================
#                              Workflow Queue Constants
# ======================================================================================

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
NODE_FAILED = "FAILED"


# ======================================================================================
#                              Workflow Manager
# ======================================================================================

class ARCWorkflowManager:
    """Manage the end-to-end compilation queue for the ARC compiler."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        app_type: str = "web",
        log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None] | None = None,
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.app_type = normalize_app_type(app_type)
        self.log_cb = log_cb

        self.arc_dir = os.path.join(self.workspace_path, ".arc")
        self.queue_path = os.path.join(self.arc_dir, QUEUE_FILENAME)

        # Keep agent instances here so compile steps can directly orchestrate them later.
        self.interface_designer = InterfaceDesigner(log_cb)
        self.test_generator = TestGenerator(log_cb)
        self.test_driven_developer = TestDrivenDeveloper(log_cb)

    # ==================================================================================
    #                              Setup And Input Loading
    # ==================================================================================

    async def cleanup_workspace(self) -> bool:
        await self.log_cb("Compiler", "Clear-and-recompile requested. Cleaning workspace...")
        try:
            for item in os.listdir(self.workspace_path):
                if item == "requirements":
                    continue
                item_path = os.path.join(self.workspace_path, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.remove(item_path)

            await self.log_cb("Compiler", "Workspace cleaned successfully.")
            return True
        except Exception as exc:
            await self.log_cb("Compiler", f"Failed to clean workspace: {str(exc)}", "error")
            return False

    async def load_requirement_tree(self) -> dict[str, Any] | None:
        await self.log_cb("RequirementLoader", f"Reading requirements file: {self.requirement_path}")
        try:
            requirement_tree = load_requirements(self.requirement_path)
            if not requirement_tree:
                await self.log_cb("RequirementLoader", "Failed to read requirements file or file is empty.", "error")
                return None
            return requirement_tree
        except Exception as exc:
            await self.log_cb("RequirementLoader", f"Error while reading requirements file: {str(exc)}", "error")
            return None

    async def initialize_project(self):
        await self.log_cb("System", f"Initializing project environment in {self.workspace_path}...")

        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)

        db_path = os.path.join(self.arc_dir, "traceability.db")
        await self.log_cb("System", f"Initializing traceability database at {db_path}...")
        set_db_path(db_path)
        init_db()

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

        await self.log_cb("System", "Full-stack workspace initialized completely.")
        gitignore_path = ensure_arc_gitignore(self.workspace_path)
        await self.log_cb("System", "Initializing Git repository...")

        await run_git_init(self.workspace_path, self.log_cb)
        return True

    async def start_compilation(self, clear_all: bool = False) -> dict[str, Any]:
        await self.log_cb("Compiler", "ARC compilation started.")

        if clear_all:
            cleaned = await self.cleanup_workspace()
            if not cleaned:
                return {"ok": False, "failed_nodes": []}

        requirement_tree = await self.load_requirement_tree()
        if not requirement_tree:
            return {"ok": False, "failed_nodes": []}

        init_ok = await self.initialize_project()
        if init_ok is False:
            await self.log_cb("Compiler", "Project initialization failed.", "error")
            return {"ok": False, "failed_nodes": []}

        compile_result = await self.compile_requirement_tree(requirement_tree)

        failed_nodes = compile_result.get("failed_nodes", [])
        if failed_nodes:
            await self.log_cb(
                "Compiler",
                f"Compilation finished with {len(failed_nodes)} failed node(s): {', '.join(failed_nodes)}",
                "error",
            )
        else:
            await self.log_cb("Compiler", "Compilation finished successfully.")

        if self.app_type in {"android", "web"}:
            from traceability.test_result_tracker import TestResultTracker

            tracker = TestResultTracker(self.arc_dir)
            await self.log_cb("Compiler", tracker.format_summary())

        return compile_result

    # ==================================================================================
    #                              Compilation Entry
    # ==================================================================================

    async def compile_requirement_tree(self, requirement_tree: dict[str, Any]) -> dict[str, Any]:
        root_id = requirement_tree.get("id") if isinstance(requirement_tree, dict) else None
        if not root_id:
            await self.log_cb("Compiler", "Requirement root node id is missing.", "error")
            return {"ok": False, "failed_nodes": []}

        await self.log_cb("Compiler", "Persisting requirement tree and preparing processing queue...")
        store_all_requirement(requirement_tree)

        queue_state = self._load_or_create_processing_queue(requirement_tree)
        self._recover_interrupted_queue(queue_state)
        self._save_processing_queue(queue_state)

        await self.log_cb(
            "Compiler",
            f"Loaded processing queue with {len(queue_state['tasks'])} task(s) for root node {root_id}.",
        )

        while True:
            task = self._next_runnable_task(queue_state)
            if task is None:
                break

            node_id = task["node_id"]
            phase = task["phase"]
            requirement_data = get_requirement_by_id(node_id) or {}

            task["status"] = TASK_RUNNING
            queue_state["last_task_id"] = task["task_id"]
            self._save_processing_queue(queue_state)

            await self.log_cb("Compiler", f"Running {phase} for node {node_id}...", None, node_id)
            task_ok = await self._run_task(task)

            if task_ok:
                task["status"] = TASK_COMPLETED
                new_state = NODE_DESIGNED if phase == PHASE_DESIGN else NODE_PASSED
                self._set_node_state(queue_state["node_states"], node_id, new_state)
                self._save_processing_queue(queue_state)
                await self._commit_phase_checkpoint(node_id, phase, requirement_data)
                await self.log_cb("Compiler", f"{phase} completed for node {node_id}.", None, node_id)
            else:
                task["status"] = TASK_FAILED
                self._set_node_state(queue_state["node_states"], node_id, NODE_FAILED)
                self._save_processing_queue(queue_state)
                await self._commit_phase_checkpoint(node_id, f"{phase}-FAILED", requirement_data)
                await self.log_cb("Compiler", f"{phase} failed for node {node_id}.", "error", node_id)
                return self._build_compile_result(queue_state)

        return self._build_compile_result(queue_state)

    # ==================================================================================
    #                              Queue Construction And Recovery
    # ==================================================================================

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

    def _collect_node_ids(self, tasks: list[dict[str, Any]]) -> list[str]:
        seen: list[str] = []
        for task in tasks:
            node_id = task["node_id"]
            if node_id not in seen:
                seen.append(node_id)
        return seen

    def _is_compatible_queue(self, queue_state: dict[str, Any] | None, root_id: str, expected_task_ids: list[str]) -> bool:
        if not queue_state:
            return False
        if queue_state.get("root_id") != root_id:
            return False
        existing_task_ids = [task.get("task_id") for task in queue_state.get("tasks", [])]
        return existing_task_ids == expected_task_ids

    def _apply_saved_states_to_tasks(self, queue_state: dict[str, Any]) -> None:
        for task in queue_state["tasks"]:
            node_state = queue_state["node_states"].get(task["node_id"], NODE_UNSEEN)
            if node_state == NODE_PASSED:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_DESIGNED and task["phase"] == PHASE_DESIGN:
                task["status"] = TASK_COMPLETED
            elif node_state == NODE_FAILED and task["phase"] == PHASE_DESIGN:
                task["status"] = TASK_COMPLETED

    def _recover_interrupted_queue(self, queue_state: dict[str, Any]) -> None:
        for task in queue_state["tasks"]:
            if task["status"] in {TASK_RUNNING, TASK_FAILED}:
                task["status"] = TASK_PENDING

    # ==================================================================================
    #                              Task Scheduling And Execution
    # ==================================================================================

    def _next_runnable_task(self, queue_state: dict[str, Any]) -> dict[str, Any] | None:
        for task in queue_state["tasks"]:
            if task["status"] == TASK_PENDING:
                return task
        return None

    async def _run_task(self, task: dict[str, Any]) -> bool:
        node_id = task["node_id"]
        if task["phase"] == PHASE_DESIGN:
            return await self._run_design_phase(node_id)
        return await self._run_implement_phase(node_id)

    async def _run_design_phase(self, node_id: str) -> bool:
        requirement_data = get_requirement_by_id(node_id)
        if not requirement_data:
            await self.log_cb("System", f"Requirement node {node_id} not found in database.", "error", node_id)
            return False

        await self.log_cb(
            "InterfaceDesigner",
            f"[Placeholder] DESIGN phase for node {node_id}. Direct agent orchestration will be added here.",
            None,
            node_id,
        )
        return True

    async def _run_implement_phase(self, node_id: str) -> bool:
        requirement_data = get_requirement_by_id(node_id)
        if not requirement_data:
            await self.log_cb("System", f"Requirement node {node_id} not found in database.", "error", node_id)
            return False

        await self.log_cb(
            "TestDrivenDeveloper",
            f"[Placeholder] IMPLEMENT phase for node {node_id}. Direct agent orchestration will be added here.",
            None,
            node_id,
        )
        return True

    # ==================================================================================
    #                              Result And State Persistence
    # ==================================================================================

    async def _commit_phase_checkpoint(self, node_id: str, phase: str, requirement_data: dict[str, Any]) -> None:
        commit_message = build_commit_message(node_id, phase, requirement_data)
        await self.log_cb("Compiler", f"Running git checkpoint for {phase} on node {node_id}...", None, node_id)
        await run_git_commit(self.workspace_path, commit_message, self.log_cb)

    def _set_node_state(self, node_states: dict[str, str], node_id: str, state: str) -> None:
        node_states[node_id] = state
        upsert_node_state(node_id, state)

    def _build_compile_result(self, queue_state: dict[str, Any]) -> dict[str, Any]:
        failed_nodes = sorted(
            node_id for node_id, state in queue_state["node_states"].items() if state == NODE_FAILED
        )
        completed_tasks = [
            task["task_id"] for task in queue_state["tasks"] if task["status"] == TASK_COMPLETED
        ]
        all_completed = all(task["status"] == TASK_COMPLETED for task in queue_state["tasks"])
        return {
            "ok": all_completed and not failed_nodes,
            "failed_nodes": failed_nodes,
            "visit_order": completed_tasks,
            "states": dict(queue_state["node_states"]),
        }

    def _save_processing_queue(self, queue_state: dict[str, Any]) -> None:
        write_json_file(self.queue_path, queue_state)
