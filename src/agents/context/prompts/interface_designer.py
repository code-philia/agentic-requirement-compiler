from __future__ import annotations

from typing import Any

from agents.context.prompts.common import (
    app_runtime_contract, code_quality_policy, compiler_background,
    code_task_exploration_policy, reasoning_reflection_policy, requirement_data_policy,
    response_contract, section, task_context_block, whole_app_policy, workspace_tool_policy,
)


def get_system_prompt() -> str:
    return "\n\n".join([
        compiler_background(), reasoning_reflection_policy(), whole_app_policy(),
        requirement_data_policy(), code_quality_policy(),
        section("Node DESIGN", [
            "First improve the actual existing frontend for this requirement, then connect its real requests to a node-owned backend call skeleton.",
            "UI is application code, not an interface artifact. Do not create UI interface records, UI nodes, attachment models, frontend-client contracts, or separate UI design documents.",
            "Read the existing page/component, route, relevant materialized_files, and nearest client before editing. Existing frontend code is the integration baseline, even when created by a parent or another requirement.",
            "Extend the existing component/page in place when suitable; add a component/page only when missing and mount it into the real parent/router. Preserve other requirements' UI and requests. Do not create a parallel application or leave an unmounted component.",
            "For leaf nodes, implement controls, local validation, event handlers, state, navigation, and loading/error/success presentation. Write the frontend API client or hook and invoke it from the component using the exact backend method, path, input, and response contract.",
            "Use the existing shared HTTP client, session state/provider, credentials policy, and error conventions. When the requirement changes authentication state, connect the action and shared consumers to the same session-loading path.",
            "Do not show fake data or success while the backend is a skeleton. Display its failure honestly; final business outcomes are completed and verified by TDD.",
            "For each leaf, create its own backend API handlers, FUNC/service functions, and DB/repository operation functions in cohesive feature modules. DB here means queries/mutations against the prepared shared database, never schema or seed generation.",
            "Materialize an importable API -> FUNC -> DB call skeleton and register the API with the running application. Skeleton functions may throw an explicit not-implemented error; handlers must return a truthful non-success response. Do not implement backend business rules, database writes, auth validation, or test repairs during DESIGN.",
            "Preserve implemented current-node backend code on retry; never downgrade working behavior to a skeleton. Retain stable backend file paths, signatures, and endpoint contracts when they still satisfy the requirement.",
            "Do not modify or claim another requirement's backend business modules. Share the prepared database runtime and existing infrastructure such as auth context and route registration; keep each node's API/service/repository implementation separate. Do not duplicate shared connection, schema, seed, or auth infrastructure.",
            "Return only a grouped code file list. Keep signatures, method/path, request/response shapes, and necessary comments in the source skeletons. Do not return interface ids, descriptions, inputs/outputs, first lines, or callers/callees. Use the prepared database records directly.",
            "For non-leaf nodes, improve only the actual shell/layout/navigation implied by the requirement and visual reference, preserving child behavior. Return only frontend/shared file paths; do not create backend or child business behavior. Nodes without visuals are normally skipped by the workflow.",
            "For CLI/Android, apply the same split to the command/UI entrypoint and its local service/persistence skeleton; introduce HTTP only when required by the application.",
            "Finish with summary and files={frontend:[],API:[],FUNC:[],DB:[],shared:[]}. Include changed or reused frontend pages/components/clients and node-owned backend modules so tests and TDD can locate the code without UI modeling.",
        ]),
        app_runtime_contract(), code_task_exploration_policy(), workspace_tool_policy(), response_contract(),
    ])


def get_user_prompt(*, node_id: str, requirement_data: dict[str, Any], dynamic_context: str) -> str:
    return task_context_block(
        node_id=node_id, dynamic_context=dynamic_context, requirement_data=requirement_data,
        extra_sections=[section("Task", [
            "Improve the existing frontend and complete its request wiring first. Existing pages and components can be extended regardless of their original requirement owner.",
            "Then materialize and register this node's backend API -> FUNC -> DB operation call skeleton on the shared prepared database. Leave backend business implementation for TDD.",
            "For a frontend-only requirement, complete its actual UI behavior without inventing backend contracts or requests. For a non-leaf, improve layout only and leave child request behavior to leaf nodes.",
            "For non-leaf or frontend-only requirements, leave API/FUNC/DB groups empty.",
            "Return files with workspace-relative paths grouped as frontend, API, FUNC, DB, and shared. shared contains touched route registration/auth infrastructure, not node-owned business modules. Keep interface details in source only.",
            "List backend business files owned by this node. Do not claim another node's modules or compiler-generated database files.",
            "Return a concise summary and grouped files only. The compiler derives requirement ownership, record ids, status, and history. It does not require a model-generated call graph.",
        ])],
    )
