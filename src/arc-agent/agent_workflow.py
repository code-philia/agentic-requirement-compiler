import inspect
import os
import shutil
from typing import Any, Awaitable, Callable

from agents.context_pipeline import context_pipeline
from agents.interface_designer import InterfaceDesigner
from agents.test_driven_developer import TestDrivenDeveloper
from agents.test_generator import TestGenerator
from compiler_state import CompileContext, NodeState
from project_bootstrap import ProjectBootstrapper
from prompts.stack import update_node_status
from utils import load_requirements
from traceability import store_all_requirement
from traceability.database import (
    get_interfaces_by_req_id,
    get_requirement_by_id,
    insert_call_edge,
    upsert_node_state,
)
from workflow_implementation import WorkflowImplementationService
from workflow_preparation import WorkflowPreparationService

from dotenv import load_dotenv
load_dotenv()

DEBUG_MODE = int(os.environ.get("ARC_DEBUG", "1"))


class ARCWorkflowManager:
    """Manage node-level workflow orchestration for the ARC compiler."""

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        app_type: str = "web",
        log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None] = None,
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.app_type = (app_type or "web").strip().lower()
        self.log_cb = log_cb
        self.compile_context = CompileContext()

        self.interface_designer = InterfaceDesigner(log_cb)
        self.test_generator = TestGenerator(log_cb)
        self.test_driven_developer = TestDrivenDeveloper(log_cb)

        self.preparation_service = WorkflowPreparationService(
            workspace_path=self.workspace_path,
            interface_designer=self.interface_designer,
            test_generator=self.test_generator,
            log_cb=self.log_cb,
        )
        self.implementation_service = WorkflowImplementationService(
            workspace_path=self.workspace_path,
            test_driven_developer=self.test_driven_developer,
            log_cb=self.log_cb,
        )

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

            status_file = os.path.join(self.workspace_path, ".arc", "status.json")
            if os.path.exists(status_file):
                os.remove(status_file)

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
        bootstrapper = ProjectBootstrapper(
            workspace_path=self.workspace_path,
            requirement_path=self.requirement_path,
            app_type=self.app_type,
            interface_designer=self.interface_designer,
            log_cb=self.log_cb,
        )
        return await bootstrapper.initialize_project()

    async def prepare_node(self, node_id: str) -> dict:
        requirement_data = get_requirement_by_id(node_id)
        if not requirement_data:
            await self.log_cb("System", f"Error: Requirement node {node_id} not found in database.", node_id=node_id)
            return {"ok": False, "node_id": node_id}

        context_pipeline.prewarm(node_id)

        children_ids = requirement_data.get("children_ids", [])
        is_leaf = not children_ids
        if is_leaf:
            await self.log_cb("System", f"Node {node_id} is a leaf node. Entering top-down synthesis.", node_id=node_id)
        else:
            await self.log_cb(
                "System",
                f"Node {node_id} is a non-leaf node (children: {children_ids}). Entering top-down synthesis.",
                node_id=node_id,
            )

        try:
            prepared = await self.preparation_service.run(
                node_id=node_id,
                requirement_data=requirement_data,
                is_leaf=is_leaf,
            )
            prepared["ok"] = True
            prepared["node_id"] = node_id
            return prepared
        except Exception as exc:
            await self.log_cb("System", f"Top-down synthesis failed due to an error: {str(exc)}", node_id=node_id)
            return {"ok": False, "node_id": node_id}

    async def implement_node(self, node_id: str, prepared: dict) -> bool:
        if not prepared or not prepared.get("ok", False):
            await self.log_cb(
                "System",
                f"Skipping bottom-up implementation for node {node_id} due to failed preparation.",
                node_id=node_id,
            )
            return False

        requirement_data = prepared.get("requirement_data", {}) or {}
        stub_artifacts = prepared.get("stub_artifacts", "")

        try:
            return await self.implementation_service.run(
                node_id=node_id,
                requirement_data=requirement_data,
                stub_artifacts=stub_artifacts,
            )
        except Exception as exc:
            await self.log_cb("System", f"Bottom-up implementation failed due to an error: {str(exc)}", node_id=node_id)
            return False

    async def process_node(self, node_id: str) -> bool:
        prepared = await self.prepare_node(node_id)
        if not prepared.get("ok", False):
            return False
        return await self.implement_node(node_id, prepared)

    async def compile_requirement_tree(self, requirement_tree: dict[str, Any]) -> dict[str, Any]:
        root_id = requirement_tree.get("id") if isinstance(requirement_tree, dict) else None
        if not root_id:
            await self.log_cb("Compiler", "Requirement root node id is missing.", "error")
            return {"ok": False, "failed_nodes": []}

        await self.log_cb("Compiler", "Persisting requirement tree and preparing DFS compilation...")
        store_all_requirement(requirement_tree)
        self.compile_context = CompileContext()

        await self.log_cb("Compiler", f"Starting workflow compilation from root node: {root_id}")
        all_ok = await self._compile_node(root_id, set())
        return {
            "ok": all_ok and not self.compile_context.failed_nodes,
            "failed_nodes": sorted(self.compile_context.failed_nodes),
            "visit_order": list(self.compile_context.visit_order),
            "states": {k: v.value for k, v in self.compile_context.node_states.items()},
        }

    async def _emit_state(self, node_id: str, state: NodeState) -> None:
        self.compile_context.set_state(node_id, state)
        upsert_node_state(node_id, state.value)

        if state == NodeState.WORKING:
            update_node_status(self.workspace_path, node_id, "analyzing")
        elif state == NodeState.DONE:
            update_node_status(self.workspace_path, node_id, "completed")
        elif state == NodeState.FAILED:
            update_node_status(self.workspace_path, node_id, "error")
            await self.log_cb("Compiler", f"Node {node_id} failed.", "error", node_id)

    async def _record_parent_child_call_edges(self, parent_node_id: str, child_node_id: str) -> int:
        try:
            parent_interfaces = get_interfaces_by_req_id(parent_node_id)
            child_interfaces = get_interfaces_by_req_id(child_node_id)
            edge_count = 0
            for parent_iface in parent_interfaces:
                parent_interface_id = parent_iface.get("interface_id")
                if not parent_interface_id:
                    continue
                for child_iface in child_interfaces:
                    child_interface_id = child_iface.get("interface_id")
                    if not child_interface_id:
                        continue
                    insert_call_edge(
                        source_req_id=parent_node_id,
                        target_req_id=child_node_id,
                        from_interface_id=parent_interface_id,
                        to_interface_id=child_interface_id,
                        edge_type="dfs_parent_child",
                    )
                    edge_count += 1

            if edge_count > 0:
                await self.log_cb(
                    "System",
                    f"Recorded {edge_count} call edge(s) from node {parent_node_id} to child {child_node_id}.",
                    None,
                    parent_node_id,
                )
            return edge_count
        except Exception as exc:
            await self.log_cb(
                "System",
                f"Failed to record call edges for parent {parent_node_id} -> child {child_node_id}: {str(exc)}",
                None,
                parent_node_id,
            )
            return 0

    def _extract_children_ids(self, requirement_data: dict[str, Any] | None) -> list[str]:
        if not requirement_data:
            return []

        children_ids = requirement_data.get("children_ids", [])
        if isinstance(children_ids, list):
            return [str(child_id) for child_id in children_ids if child_id]

        children = requirement_data.get("children", [])
        if isinstance(children, list):
            result = []
            for child in children:
                if isinstance(child, dict) and child.get("id"):
                    result.append(str(child["id"]))
            return result
        return []

    async def _compile_node(self, node_id: str, active_path: set[str]) -> bool:
        state = self.compile_context.get_state(node_id)
        if state == NodeState.DONE:
            return True
        if state == NodeState.FAILED:
            return False
        if state == NodeState.WORKING or node_id in active_path:
            await self._emit_state(node_id, NodeState.FAILED)
            return False

        await self._emit_state(node_id, NodeState.WORKING)
        self.compile_context.visit_order.append(node_id)

        requirement_data = get_requirement_by_id(node_id)
        if requirement_data is None:
            await self._emit_state(node_id, NodeState.FAILED)
            return False

        prepared_payload: dict[str, Any] | None = None
        try:
            prepared_payload = await self.prepare_node(node_id)
            node_ok = bool(prepared_payload and prepared_payload.get("ok", False))
        except Exception:
            node_ok = False

        children_ok = True
        next_path = set(active_path)
        next_path.add(node_id)
        for child_id in self._extract_children_ids(requirement_data):
            child_ok = await self._compile_node(child_id, next_path)
            if child_ok:
                await self._record_parent_child_call_edges(node_id, child_id)
            children_ok = children_ok and child_ok

        if node_ok and children_ok:
            try:
                implement_ok = bool(await self.implement_node(node_id, prepared_payload or {}))
            except Exception:
                implement_ok = False
        else:
            implement_ok = False

        final_ok = node_ok and children_ok and implement_ok
        await self._emit_state(node_id, NodeState.DONE if final_ok else NodeState.FAILED)
        return final_ok


async def run_agent_workflow(manager: ARCWorkflowManager, node_id: str, requirement_data: dict):
    _ = requirement_data
    return await manager.process_node(node_id)
