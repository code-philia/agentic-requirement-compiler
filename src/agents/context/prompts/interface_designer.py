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
            "Preserve implemented current-node backend code on retry; never downgrade working behavior to a skeleton. Retain stable backend interface ids, paths, signatures, and endpoint contracts when they still satisfy the requirement.",
            "Do not modify or claim another requirement's backend business modules. Share the prepared database runtime and existing infrastructure such as auth context and route registration; keep each node's API/service/repository implementation separate. Do not duplicate shared connection, schema, seed, or auth infrastructure.",
            "Return only node-owned backend API, FUNC, and DB operation contracts. Reference shared tables by their GLOBAL:DB:<table> ids in DB specification/callees; do not return those global records as node-owned interfaces.",
            "For non-leaf nodes, improve only the actual shell/layout/navigation implied by the requirement and visual reference, preserving child behavior. Return interfaces=[]; do not create backend or child business behavior. Nodes without visuals are normally skipped by the workflow.",
            "For CLI/Android, apply the same split to the command/UI entrypoint and its local service/persistence skeleton; introduce HTTP only when required by the application.",
            "Finish with summary, interfaces, and files_written. Include every modified frontend page/component/client and backend module in files_written so tests and TDD can locate the code without UI modeling.",
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
            "Do not return UI or frontend API-client interface records. Return interfaces=[] for a non-leaf or frontend-only requirement.",
            "Each backend interface needs interface_id, type (API/FUNC/DB), name, file_path, first_line, responsibility, specification, inputs, outputs, callers, callees, and test_focus where useful. API specification must state HTTP method/path and response/error shape when HTTP applies.",
            "Use stable current-node interface ids and workspace-relative file paths. Backend operation interfaces belong to this node; reference global table contracts without redefining them.",
            "Return a concise summary and files_written listing all created/edited UI, frontend client, route registration, and backend files. Do not add a separate frontend model.",
        ])],
    )
