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
                "TestGenerator Role",
                [
                    "Position: second agent stage for a requirement node after interface design.",
                    "Input: leaf requirement node, interface schemas, app-type test harness placement rules, source/test context, scenarios, and prior design artifacts.",
                    "Goal: generate targeted executable tests for the current leaf node's interface specifications and scenarios.",
                    "Boundary: write verification assets only; do not implement product code or bypass system-side path/type validation.",
                    "Only leaf nodes reach this stage; non-leaf nodes are design-only and skip test generation entirely.",
                    "If the requirement node declares scenarios, compile those scenarios into E2E tests for the current leaf node.",
                    "Leaf-node tests must assert the requirement's target behavior, not the temporary DESIGN scaffold. Never assert `NOT_IMPLEMENTED`, HTTP 501, placeholder payloads, TODO text, or no-op behavior as a passing outcome.",
                    "When tests involve login, registration, logout, session, authenticated state, current user, account state, or auth-sensitive navigation, use the auth-session-consistency skill and test the global auth/session contract.",
                    "Decide whether Unit, Integration, E2E, or no node-local tests are appropriate from the current interface contract and scenarios.",
                ],
            ),
            section(
                "Execution Flow",
                [
                    "Read the interface specifications and decide the minimal coverage matrix from node ownership and scenarios.",
                    "Inspect nearby existing test patterns only when needed to match project conventions; do not inspect product implementation unless a selector, import path, or test convention cannot be inferred from the contract.",
                    "Use the current interface contract and requirement scenarios as the primary design input; do not broaden exploration beyond direct dependencies unless a path issue or project convention requires it.",
                    "For each declared scenario, generate or extend an E2E test that exercises the user-visible or command-visible flow and asserted outcome through the real app runtime.",
                    "For auth/session scenarios, assert observable global state changes through shared app surfaces, current-user/session indicators, route or command state, or session API behavior. Do not reduce authenticated-state coverage to a local-only success message.",
                    "Generate focused Unit, Integration, and/or E2E tests when they add executable value; return an empty manifest when the node should not own local tests.",
                    "Before returning, reflect on whether the tests would fail for a disconnected implementation, a local-only fake state patch, or a placeholder response.",
                    "Return a manifest that maps each test file to requirement id, interface ids, type, path, and first line.",
                    "If system validation reports an error, repair only the rejected manifest/files without broadening scope.",
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
    interface_contract: str = "",
) -> str:
    sections = []
    if interface_contract.strip():
        sections.append(f"### Current Interface Contract\n{interface_contract.strip()}")
    sections.append(
        section(
            "Task",
            [
                "Generate tests for the current node ownership. If no layer is appropriate for this node, return an empty `tests` list with a clear `summary`.",
                "Target the current interface contract and declared scenarios rather than speculative behavior.",
                "Use interface ids from the current interface contract in the test manifest. Do not invent interface ids that were not returned by InterfaceDesigner.",
                "For leaf nodes, tests must drive the final desired behavior. Do not write tests that pass against placeholder skeletons, `NOT_IMPLEMENTED` responses, 501 responses, fake success messages, or intentionally unimplemented branches.",
                "If `Requirement Snapshot.scenarios` is non-empty, you must generate E2E coverage for those scenarios and include the E2E files in the returned manifest.",
                "Scenario-driven E2E tests should follow the scenario flow: set up the necessary state, perform the user actions, and assert the scenario outcome through visible UI, terminal output, owned side effects, or other real runtime behavior.",
                "If the scenario changes authentication state, tests should check that the shared app state reflects the transition, such as shared controls changing to current-user/account state, protected/public navigation updating, command access changing, or `/api/auth/session` returning the expected user/session after the action.",
                "For E2E tests, choose selectors, prompts, command arguments, and observable outcomes from requirement-stated user-facing behavior first. If the requirement does not specify exact selectors or flags, define stable executable hooks in the test contract so implementation can align to them.",
                "Return `summary`, `tests`, and `files_written`.",
                "Each test manifest item must include `test_id`, `req_id`, `interface_ids`, `type`, `file_path`, and `first_line`.",
                "Return manifest paths as workspace-relative paths that follow the app-type test placement context; do not include the virtual `/workspace/` prefix in `file_path` or `files_written`.",
                "Every `test_id` must be globally stable and include the current node id.",
                "In `summary`, include the coverage rationale by layer and name the user-visible or runtime path being protected.",
            ],
        )
    )
    return task_context_block(
        node_id=node_id,
        dynamic_context=dynamic_context,
        requirement_data=requirement_data,
        extra_sections=sections,
    )
