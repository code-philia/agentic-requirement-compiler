import inspect
from typing import Any, Awaitable, Callable

from compiler_state import CompileContext, NodeState


class ARCCompilerDriver:
    """Recursive DFS orchestration with explicit node states."""

    def __init__(
        self,
        workflow_manager,
        roots: list[str],
        requirement_getter: Callable[[str], dict | None],
        on_state_change: Callable[[str, NodeState], Awaitable[None] | None] | None = None,
    ):
        self.workflow_manager = workflow_manager
        self.roots = roots or []
        self.requirement_getter = requirement_getter
        self.on_state_change = on_state_change
        self.context = CompileContext()

    async def _emit_state(self, node_id: str, state: NodeState) -> None:
        self.context.set_state(node_id, state)
        if self.on_state_change is not None:
            maybe = self.on_state_change(node_id, state)
            if inspect.isawaitable(maybe):
                await maybe

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
        state = self.context.get_state(node_id)
        if state == NodeState.DONE:
            return True
        if state == NodeState.FAILED:
            return False
        if state == NodeState.WORKING or node_id in active_path:
            await self._emit_state(node_id, NodeState.FAILED)
            return False

        await self._emit_state(node_id, NodeState.WORKING)
        self.context.visit_order.append(node_id)

        requirement_data = self.requirement_getter(node_id)
        if requirement_data is None:
            await self._emit_state(node_id, NodeState.FAILED)
            return False

        prepared_payload: dict[str, Any] | None = None
        try:
            prepared_payload = await self.workflow_manager.prepare_node(node_id)
            node_ok = bool(prepared_payload and prepared_payload.get("ok", False))
        except Exception:
            node_ok = False

        children_ok = True
        next_path = set(active_path)
        next_path.add(node_id)
        for child_id in self._extract_children_ids(requirement_data):
            child_ok = await self._compile_node(child_id, next_path)
            if child_ok:
                edge_recorder = getattr(self.workflow_manager, "record_parent_child_call_edges", None)
                if edge_recorder:
                    try:
                        maybe = edge_recorder(node_id, child_id)
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception:
                        child_ok = False
            children_ok = children_ok and child_ok

        implement_ok = True
        if node_ok and children_ok:
            try:
                implement_ok = bool(await self.workflow_manager.implement_node(node_id, prepared_payload or {}))
            except Exception:
                implement_ok = False
        else:
            implement_ok = False

        final_ok = node_ok and children_ok and implement_ok
        await self._emit_state(node_id, NodeState.DONE if final_ok else NodeState.FAILED)
        return final_ok

    async def compile_all(self) -> dict[str, Any]:
        all_ok = True
        for root_id in self.roots:
            root_ok = await self._compile_node(root_id, set())
            all_ok = all_ok and root_ok

        return {
            "ok": all_ok and not self.context.failed_nodes,
            "failed_nodes": sorted(self.context.failed_nodes),
            "visit_order": list(self.context.visit_order),
            "states": {k: v.value for k, v in self.context.node_states.items()},
        }

    def get_state(self, node_id: str) -> NodeState:
        return self.context.get_state(node_id)
