"""Public ARC compiler interface."""

from .controller import Compiler
from .code_binding import CodeTargetResolver, resolve_requirement_targets
from .exact_file_patcher import ExactFilePatcher, FilePatchResult
from .models import CompilationRequest
from .test_runner import TestRunResult, TestRunner, TestSelection
from .tdd_orchestrator import NodeTDDOrchestrator, NodeTDDPolicy, TDDStageResult
from arc_agents.contracts import ProposedEdit, ProposedPatch

__all__ = [
    "CodeTargetResolver",
    "CompilationRequest",
    "Compiler",
    "ExactFilePatcher",
    "FilePatchResult",
    "NodeTDDOrchestrator",
    "NodeTDDPolicy",
    "TDDStageResult",
    "ProposedEdit",
    "ProposedPatch",
    "TestRunResult",
    "TestRunner",
    "TestSelection",
    "resolve_requirement_targets",
]
