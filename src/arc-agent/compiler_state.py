from dataclasses import dataclass, field
from enum import Enum


class NodeState(str, Enum):
    UNPROCESSED = "unprocessed"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"


@dataclass
class CompileContext:
    """In-memory compilation state for one run."""

    node_states: dict[str, NodeState] = field(default_factory=dict)
    failed_nodes: set[str] = field(default_factory=set)
    visit_order: list[str] = field(default_factory=list)

    def get_state(self, node_id: str) -> NodeState:
        return self.node_states.get(node_id, NodeState.UNPROCESSED)

    def set_state(self, node_id: str, state: NodeState) -> None:
        self.node_states[node_id] = state
        if state == NodeState.FAILED:
            self.failed_nodes.add(node_id)
        elif state == NodeState.DONE and node_id in self.failed_nodes:
            self.failed_nodes.remove(node_id)
