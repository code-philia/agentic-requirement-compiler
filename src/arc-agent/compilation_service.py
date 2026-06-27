import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from agent_workflow import ARCWorkflowManager
from prompts.stack import upsert_metadata
from utils import detect_requirement_path

@dataclass(slots=True)
class CompilationConfig:
    project_path: str
    requirement_path: str
    clear_all: bool = False
    app_type: str = "web"


def prepare_compilation(
    project_path: str,
    requirement_path: Optional[str],
    clear_all: bool = False,
    app_type: str = "web",
) -> CompilationConfig:
    normalized_project_path = os.path.abspath(project_path)
    if not os.path.isdir(normalized_project_path):
        raise FileNotFoundError(f"Project path does not exist: {normalized_project_path}")

    normalized_requirement_path = detect_requirement_path(normalized_project_path, requirement_path)
    if not os.path.exists(normalized_requirement_path):
        raise FileNotFoundError(f"Requirement file does not exist: {normalized_requirement_path}")

    upsert_metadata(normalized_project_path, app_type)
    return CompilationConfig(
        project_path=normalized_project_path,
        requirement_path=normalized_requirement_path,
        clear_all=clear_all,
        app_type=app_type,
    )


async def start_compilation(
    config: CompilationConfig,
    log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None],
):
    await log_cb("Compiler", "ARC compilation started.")
    workflow_manager = ARCWorkflowManager(
        workspace_path=config.project_path,
        requirement_path=config.requirement_path,
        app_type=config.app_type,
        log_cb=log_cb,
    )

    if config.clear_all:
        cleaned = await workflow_manager.cleanup_workspace()
        if not cleaned:
            return {"ok": False, "failed_nodes": []}

    requirement_tree = await workflow_manager.load_requirement_tree()
    if not requirement_tree:
        return {"ok": False, "failed_nodes": []}

    init_ok = await workflow_manager.initialize_project()
    if init_ok is False:
        await log_cb("Compiler", "Project initialization failed.", "error")
        return {"ok": False, "failed_nodes": []}

    compile_result = await workflow_manager.compile_requirement_tree(requirement_tree)

    failed_nodes = compile_result.get("failed_nodes", [])
    if failed_nodes:
        await log_cb("Compiler",f"Compilation finished with {len(failed_nodes)} failed node(s): {', '.join(failed_nodes)}","error",)
    else:
        await log_cb("Compiler", "Compilation finished successfully.")

    if config.app_type in {"android", "web"}:
        from traceability.test_result_tracker import TestResultTracker

        tracker = TestResultTracker(os.path.join(config.project_path, ".arc"))
        await log_cb("Compiler", tracker.format_summary())

    return compile_result
