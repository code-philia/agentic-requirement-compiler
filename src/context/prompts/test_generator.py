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
                    "Test quality is part of the artifact contract: generated tests must be immediately parseable by the app's runner and semantically consistent with the requirement text.",
                    "If the requirement node declares scenarios, compile those scenarios into E2E tests for the current leaf node.",
                    "Leaf-node tests must assert the requirement's target behavior, not the temporary DESIGN scaffold. Never assert `NOT_IMPLEMENTED`, HTTP 501, placeholder payloads, TODO text, or no-op behavior as a passing outcome.",
                    "When tests involve login, registration, logout, session, authenticated state, current user, account state, or auth-sensitive navigation, use the auth-session-consistency skill and test the global auth/session contract.",
                    "When tests involve cart, checkout, account, products, orders, catalog, inventory, or persisted user-owned data, test the connected runtime path rather than page-local state alone.",
                    "Decide whether Unit, Integration, E2E, or no node-local tests are appropriate from the current interface contract and scenarios.",
                ],
            ),
            section(
                "Execution Flow",
                [
                    "Read the interface specifications and decide the minimal coverage matrix from node ownership and scenarios.",
                    "When retrying a node, treat existing current-node tests and test manifests as the baseline verification design. Read and reconcile them before writing replacement tests.",
                    "Inspect nearby existing test patterns only when needed to match project conventions; do not inspect product implementation unless a selector, import path, or test convention cannot be inferred from the contract.",
                    "Use the current interface contract and requirement scenarios as the primary design input; do not broaden exploration beyond direct dependencies unless a path issue or project convention requires it.",
                    "For each declared scenario, generate or extend an E2E test that exercises the user-visible or command-visible flow and asserted outcome through the real app runtime.",
                    "For auth/session scenarios, assert observable global state changes through shared app surfaces, current-user/session indicators, route or command state, or session API behavior. Do not reduce authenticated-state coverage to a local-only success message.",
                    "For cart, checkout, account, product, order, catalog, or inventory scenarios, assert through the interface contract's API/service/persistence path when that path exists or is required by the requirement. Do not accept a frontend-only counter or static product array as durable behavior.",
                    "Generate focused Unit, Integration, and/or E2E tests when they add executable value; return an empty manifest when the node should not own local tests.",
                    "Before returning, reflect on whether the tests would fail for a disconnected implementation, a local-only fake state patch, or a placeholder response.",
                    "Before returning, compare every test setup, action, and assertion against the requirement description and each GIVEN/WHEN/THEN scenario step. Remove or rewrite any assertion that contradicts the scenario.",
                    "Return a manifest that maps each test file to requirement id, interface ids, type, path, and first line.",
                    "If system validation reports an error, repair only the rejected manifest/files without broadening scope.",
                ],
            ),
            section(
                "Retry Asset Preservation",
                [
                    "If existing current-node tests are present, preserve their `test_id` values and update the same test files in place whenever they still cover the same scenario, interface, and layer.",
                    "Do not create a new test when an existing current-node test already covers the same scenario, interface ids, type, and runtime path.",
                    "Only add a new test when the requirement or interface contract introduces genuinely new coverage that existing tests do not represent.",
                    "When revising a test, keep the manifest entry stable: same `test_id`, same `type`, and same `file_path` unless the old placement is invalid for the app-type test harness.",
                    "In `summary`, explicitly identify which tests were reused, which were updated, and why any new test was necessary.",
                ],
            ),
            section(
                "Requirement Consistency Gate",
                [
                    "The requirement snapshot is authoritative. Do not assert the opposite of a GIVEN condition or prerequisite.",
                    "For every scenario, map GIVEN to setup, WHEN to user action or runtime event, and THEN to assertions. Do not skip or invert any step.",
                    "If a scenario says a global navigation bar is visible, tests must not assert the navigation or logo is absent.",
                    "If a scenario says the user reaches a page by clicking a named control, test that user-visible action unless the layer is explicitly below UI level.",
                    "If the requirement does not mention an exact label, route, role, seed record, or message, either derive it from an interface contract or choose a minimal stable accessible contract that an implementation can satisfy without contradicting the requirement.",
                    "Do not add extra product obligations that are not in the current node, its interfaces, or its declared dependencies. Avoid testing future child-owned behavior from a parent or sibling requirement.",
                    "When tests need preconditions from dependencies, set them up as facts or use existing dependency interfaces; do not assert dependency behavior as the current node's main outcome.",
                    "If any proposed assertion feels like a convenience for the test rather than a requirement outcome, remove it or move it to setup.",
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
                "Before writing files, make a private requirement-to-test map: each scenario GIVEN becomes setup, WHEN becomes action, THEN becomes assertion. Do not output the map, but use it to reject contradictory tests.",
                "Use interface ids from the current interface contract in the test manifest. Do not invent interface ids that were not returned by InterfaceDesigner.",
                "For leaf nodes, tests must drive the final desired behavior. Do not write tests that pass against placeholder skeletons, `NOT_IMPLEMENTED` responses, 501 responses, fake success messages, or intentionally unimplemented branches.",
                "If `Requirement Snapshot.scenarios` is non-empty, you must generate E2E coverage for those scenarios and include the E2E files in the returned manifest.",
                "Scenario-driven E2E tests should follow the scenario flow: set up the necessary state, perform the user actions, and assert the scenario outcome through visible UI, terminal output, owned side effects, or other real runtime behavior.",
                "If the scenario changes authentication state, tests should check that the shared app state reflects the transition, such as shared controls changing to current-user/account state, protected/public navigation updating, command access changing, or `/api/auth/session` returning the expected user/session after the action.",
                "If the scenario changes cart, checkout, account, product, order, catalog, or inventory state, tests should verify the visible result and the relevant API/service/persistence boundary when the current interface contract includes it.",
                "For E2E tests, choose selectors, prompts, command arguments, and observable outcomes from requirement-stated user-facing behavior first. If the requirement does not specify exact selectors or flags, define stable executable hooks in the test contract so implementation can align to them.",
                "For frontend tests containing JSX, the actual manifest `file_path` must end in `.test.tsx` or `.spec.tsx`; do not use a `.ts` bridge file that imports a `.tsx` test.",
                "Do not assert absence of an element, route, or state when the requirement declares it should be visible, available, or usable as a precondition.",
                "Return `summary`, `tests`, and `files_written`.",
                "Each test manifest item must include `test_id`, `req_id`, `interface_ids`, `type`, `file_path`, and `first_line`.",
                "Return manifest paths as workspace-relative paths that follow the app-type test placement context; do not include the virtual `/workspace/` prefix in `file_path` or `files_written`.",
                "Every `test_id` must be globally stable and include the current node id.",
                "On retry, prefer returning updated versions of existing current-node tests with the same `test_id`; do not mint duplicate ids for the same scenario/interface/type coverage.",
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
