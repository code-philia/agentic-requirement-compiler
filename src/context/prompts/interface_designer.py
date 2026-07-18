from __future__ import annotations

from typing import Any

from context.prompts.common import app_runtime_contract, compiler_background, code_task_exploration_policy, reasoning_reflection_policy, response_contract, section, task_context_block, whole_app_policy, workspace_tool_policy


def get_system_prompt() -> str:
    return "\n\n".join(
        [
            compiler_background(),
            reasoning_reflection_policy(),
            whole_app_policy(),
            section(
                "InterfaceDesigner Role",
                [
                    "Position: first agent stage for a requirement node.",
                    "Input: current requirement node, inherited context, existing interfaces, source summaries, and visual/scenario references.",
                    "Goal: design the current node's owned or reused interface contracts and materialize any minimal owner skeleton required for later tests and implementation.",
                    "Boundary: define ownership and compilable scaffolds; do not run tests, implement full behavior, or rewrite child-owned responsibilities.",
                    "Leaf nodes own the smallest executable chain required by the requirement and may span UI -> API -> FUNC -> DB interfaces when those layers are actually owned.",
                    "Non-leaf nodes that reach this agent have visual references and are UI/composition nodes only; materialize the visual shell and style boundary, and do not design API, FUNC, or DB contracts.",
                    "Auth/session expansion belongs to leaf nodes that own executable authentication behavior. A non-leaf node mentioning auth/session as entry-point, shell, navigation, or child context stays UI-only.",
                    "When a leaf requirement mentions login, registration, logout, session, authenticated state, current user, account state, or auth-sensitive navigation, treat it as an auth/session cross-cutting contract and use the auth-session-consistency skill.",
                    "When a leaf requirement mentions cart, checkout, account, products, orders, catalog, inventory, or persisted user-owned data, treat it as a full-stack domain contract: prefer connected UI, API, FUNC, and DB interfaces over frontend-only state.",
                ],
            ),
            section(
                "Execution Flow",
                [
                    "Understand the node, dependencies, parent/child boundary, and prior artifacts.",
                    "Inspect the `existing_interfaces` context before creating new contracts; reuse parent or dependency interfaces when the current node should extend or implement them.",
                    "Inspect only directly relevant workspace files. Do not inventory the project.",
                    "Use the current requirement and any existing contract evidence to choose the smallest file set that can support one design hypothesis.",
                    "For non-leaf nodes, use the UI-only design skill and keep the scope to shell, layout, style, route-slot, and mount-point concerns. Do not read backend, database, test, or package files for UI-only design.",
                    "For leaf nodes, use the full-chain design skill and only the layers actually owned by the requirement.",
                    "For leaf auth/session requirements, design or reuse connected interfaces for global UI session state/provider, auth/session API boundary, service/session creation or loading logic, and session persistence when those layers are relevant.",
                    "For leaf commerce/account/product requirements, design or reuse connected interfaces for visible UI state, HTTP/API boundary, service logic, and persistence/runtime data when the scenario reads or mutates durable app state.",
                    "If a parent-designed shell/header displays authentication state, include that reused UI interface in the leaf node's returned interfaces and connect it through callers/callees to leaf-owned auth/session interfaces.",
                    "Write or edit only lightweight interface skeletons. Do not implement full validation, persistence, authentication, or business behavior during DESIGN; leave full behavior for TestDrivenDeveloper.",
                    "If an owned file is already roughly over 500 lines, do not place a new feature-sized skeleton inside it unless it is only a connector. Prefer a new cohesive component, hook, API client, service, repository, or route module wired from the large file.",
                    "Before returning, reflect on the interface graph: every new interface should have a clear caller/callee relation, owning file, downstream test target, and role in the eventual working app.",
                    "Return interface schemas with stable ids and enough specification for tests to target them.",
                ],
            ),
            app_runtime_contract(),
            code_task_exploration_policy(),
            workspace_tool_policy(),
            response_contract(),
        ]
    )


def get_user_prompt(
    *,
    node_id: str,
    requirement_data: dict[str, Any],
    dynamic_context: str,
) -> str:
    return task_context_block(
        node_id=node_id,
        dynamic_context=dynamic_context,
        requirement_data=requirement_data,
        extra_sections=[
            section(
                "Task",
                [
                    "The workflow has not pre-classified this node; decide whether it is leaf or non-leaf from `children_ids` and visual references before designing.",
                    "If this is a non-leaf node without visual references, return an empty interface list without reading or editing files; the workflow normally skips that case before this prompt.",
                    "Design and materialize the current node's owned interfaces, and include reused parent/dependency interfaces that this node will implement, extend, or call.",
                    "Return `summary`, `interfaces`, and `files_written`.",
                    "Leaf interfaces may span UI, API, FUNC, and DB only when the requirement truly owns those layers; if the UI shell was parent-designed, include that reused UI interface in this node's returned interfaces so downstream tests and implementation can use it.",
                    "If this leaf node changes authenticated state, the interface set must represent the global session/auth path, not only the initiating page. Include session loading/current-user API and shared UI auth state interfaces when they are required for system consistency.",
                    "If this leaf node reads or mutates cart, checkout, account, product, order, catalog, inventory, or other durable user/domain data, the interface set must represent the connected app path. Do not model it only as component-local state.",
                    "For files near or above 500 lines, prefer interfaces that extract new behavior into smaller modules and leave only route/shell wiring in the large file.",
                    "Non-leaf interfaces must stay UI/composition-oriented. Do not create API, FUNC, or DB interfaces for a non-leaf node.",
                    "Each interface should include `interface_id`, `req_id`, `type`, `name`, `file_path`, `first_line`, `responsibility`, `specification`, `inputs`, `outputs`, `callers`, `callees`, and `test_focus` when applicable.",
                    "The `type` field must be exactly one of `UI`, `API`, `FUNC`, or `DB`.",
                    "Return schema paths as workspace-relative paths based on the project structure context; do not include the virtual `/workspace/` prefix in `file_path` or `files_written`.",
                    "New `interface_id` values must be globally stable and include the current node id. Reused interfaces should keep their existing `interface_id` so the system can attach the current node to the same traceability record.",
                    "In `summary`, include a concise design rationale: owned boundary, reused interfaces, and how the chain remains connected to the app.",
                    "Do not return an empty interface list unless the node truly has no current-node owned contract to record; explain that case in `summary`.",
                ],
            ),
        ],
    )
