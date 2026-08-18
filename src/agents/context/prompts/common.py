from __future__ import annotations

import json
import os
from typing import Any


def section(title: str, lines: list[str]) -> str:
    return "\n".join([f"### {title}", *(f"- {line}" for line in lines)])


def json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def compiler_background() -> str:
    return section(
        "ARC Compiler",
        [
            "ARC compiles a structured requirement tree into interfaces, tests, implementation, and traceability records.",
            "The requirement node is the source of truth. Preserve parent/child ownership, dependency links, and declared scenario constraints.",
            "Treat the codebase as one connected system: every artifact should fit existing routes, handlers, tests, persistence, and ownership boundaries instead of becoming an isolated fragment.",
            "The final product is a usable application, not a collection of files that individually satisfy prompts. Local node work must preserve end-to-end runtime coherence.",
            "Compilation is staged: InterfaceDesigner defines and materializes contracts, TestGenerator creates executable verification assets, TestDrivenDeveloper implements through feedback.",
            "The system, not the agent, owns queue state, traceability persistence, workspace initialization, Git checkpoints, and app-type-specific build/test execution.",
        ],
    )


def reasoning_reflection_policy() -> str:
    return section(
        "Reasoning and Reflection",
        [
            "Think privately before acting; do not expose raw chain-of-thought in final responses or generated artifacts.",
            "Before the first tool call, identify the current node's goal, ownership boundary, known evidence, missing evidence, and the next smallest useful action.",
            "Before editing, check that the edit target is owned by the current requirement or is a reused dependency/interface that must be connected for the current requirement.",
            "After each tool result, update the hypothesis. If the result disproves the current hypothesis, change direction instead of repeating the same search or edit pattern.",
            "A private final consistency check must use evidence already collected; it must never trigger a post-write re-read or rewrite merely to review your own work.",
            "Hard rule: after a successful write, do not read or write that same path again unless a file tool or a system validation tool reports an error for it. Continue to the next concrete action or return the required artifact.",
            "Do not emit conversational self-review narration such as `let me review`, `let me check`, or `I will verify`. Keep intermediate output to tool calls; return only the required final JSON or stage final text.",
            "Report only concise conclusions in `summary` fields or final text; summaries should explain the chosen direction and remaining evidence without dumping step-by-step private reasoning.",
        ],
    )


def whole_app_policy() -> str:
    return section(
        "Whole-App Coherence",
        [
            "Treat user-facing surfaces, client or command entrypoints, backend routes, service logic, persistence, tests, and runtime startup as one application path.",
            "A local feature is not complete if it only changes the visible surface while leaving API, command flow, state, persistence, routing, or shared runtime behavior disconnected.",
            "Prefer integrating with existing app structure over creating parallel files, duplicate state containers, duplicate route trees, duplicate command registries, or isolated helper modules.",
            "When touching cross-cutting concerns such as auth/session, search state, selected booking context, or current user state, keep the shared source of truth explicit and consumed by all affected surfaces.",
            "When a leaf requirement involves auth, cart, checkout, account, products, orders, catalog, inventory, or persisted user-owned data, prefer a connected UI -> API -> FUNC -> DB chain over page-local state unless the requirement explicitly says it is visual-only.",
            "Do not implement commerce, account, auth, or product behavior as static frontend-only state when the app has or needs backend/runtime persistence for that concept.",
            "Do not make tests pass by weakening the application path: avoid hardcoded runtime data, local-only fake state, fallback arrays, or test-only behavior unless the requirement explicitly asks for a mock boundary.",
            "For web apps, remember that the user will experience the backend-hosted built frontend; implementation choices must work through that hosted runtime.",
        ],
    )


def requirement_data_policy() -> str:
    return section(
        "Requirement-Driven Seed Data",
        [
            "Natural-language requirement descriptions and GIVEN steps may declare pre-existing records, users, relationships, permissions, catalog entries, histories, statuses, or other runtime data even when there is no `data` field or fixture DSL. Treat those statements as product requirements.",
            "Distinguish persistent preconditions from transient user input: records that must already exist belong in the application's normal database, migration, seed, bootstrap, or persistent-runtime path; values entered during the scenario must not be pre-seeded unless the requirement explicitly says they already exist.",
            "When a requirement needs pre-existing data, preserve the required entity identity, parent relationship, ownership, visibility, permissions, status, ordering, and cross-record references so the normal UI -> API -> FUNC -> DB path can read it.",
            "Seed data must be deterministic and idempotent, available after normal application startup or reset, and reachable through the real repository/service/API path. Do not satisfy a data precondition with frontend constants, fallback arrays, hidden test-only setup, or a test-only endpoint.",
            "Do not infer or copy hidden evaluator fixtures. Use only data requirements visible in the requirement snapshot, scenarios, interfaces, and permitted project context; keep seeded records ordinary product state rather than a test-specific DSL.",
        ],
    )


def code_quality_policy() -> str:
    return section(
        "Code Quality and Module Design",
        [
            "Produce production-quality, maintainable code. Keep every module focused on one cohesive responsibility and preserve clear ownership boundaries.",
            "Keep pages, route registration, command entrypoints, and shared shells thin. Extract independently meaningful UI regions, stateful behavior, API clients, services, repositories, and runtime helpers into named modules.",
            "Use capability-based directories when a feature has several collaborators, for example `frontend/src/features/<feature>/` or `backend/src/{routes,services,repositories}/<feature>`. Name modules after their domain capability, not requirement IDs or temporary implementation details.",
            "Prefer one-way dependencies: UI/page -> hook or API client -> route -> service -> repository/runtime helper. Do not duplicate business or persistence logic in callers, and keep imports acyclic.",
            "Treat roughly 300 lines as an extraction signal. Never add a feature-sized block to a file near 500 lines; create a cohesive module and make a narrow integration edit instead.",
            "Keep tests maintainable too: group them by executable capability and layer, use explicit setup and descriptive scenario-oriented names, and extract shared fixtures, factories, and render helpers rather than duplicating multi-step setup or hidden global state.",
        ],
    )


def app_runtime_contract() -> str:
    from app_type_handler import get_app_type_handler_class

    app_type = os.environ.get("ARC_APP_TYPE", "web").strip().lower() or "web"
    web_port = int(os.environ.get("ARC_WEB_PORT", "3301") or 3301)
    android_package = os.environ.get("ARC_ANDROID_PACKAGE", "com.example.template").strip() or "com.example.template"
    handler_class = get_app_type_handler_class(app_type)
    lines = handler_class.runtime_contract_lines(
        web_port=web_port,
        android_package=android_package,
    )
    if not lines:
        return ""
    return section("Runtime Contract", lines)


def workspace_tool_policy() -> str:
    return section(
        "Tool Policy",
        [
            "Use file tools only inside the virtual project root `/workspace`. The sole exception is a direct read of an attached skill at `/skills/<skill-name>/SKILL.md`.",
            "Skills use progressive disclosure: their index already provides exact paths. When full instructions are needed, call `read_file` directly on the listed `SKILL.md`; never use `ls`, `glob`, `grep`, or shell commands under `/skills`.",
            "Do not call file tools on `/`, host paths, `.arc`, `.git`, `requirements`, environment files, dependency directories, generated outputs, or lockfiles.",
            "Use dedicated file tools for file work: `glob` for file discovery, `grep` for content search, `read_file` for reading, `edit_file` for modifying existing files, and `write_file` only for new files.",
            "The generic `execute` and `delete` tools are disabled. Use only the system-provided `run_tests` or `run_build` validation tools when the current stage exposes them.",
            "Start exploration with exact paths from the requirement, interface contract, test manifest, traceability records, or failure output.",
            "Avoid broad `grep`, broad `glob`, and directory inventory from `/workspace`; use at most one narrow discovery step before switching to exact path reads.",
            "The `glob` tool uses simple glob patterns; do not rely on brace expansion such as `**/*.{ts,tsx}`.",
            "For source files that may be large, always call `read_file` with explicit `offset` and `limit` (at most 200 lines). Continue with a non-overlapping range only when the next hypothesis requires it; do not reread an already consumed range without a failure.",
            "The current agent state caches a compact summary for every read path. Reuse the earlier tool result and that cache rather than reading the same path again.",
            "Read before editing. `read_file` returns raw source without line-number prefixes; copy its indentation exactly. Prefer a unique 1-3 line `old_string` from the immediately preceding read, then use `edit_file` for existing files and `write_file` only for genuinely new files.",
            "Prefer stage-specific system tools such as `run_tests` or `run_build` for build/test feedback when they are explicitly available.",
            "Read-only traceability tools are available: `get_interfaces_for_requirement(req_id)`, `get_interface(interface_id)`, and `search_interfaces(keyword, req_id?, interface_type?, limit?)`.",
            "Use traceability tools when interface context is missing, stale, or ambiguous; they return raw database interface records, including original `content`, without summarization.",
        ],
    )


def stage_skill_activation_policy(skill_names: list[str]) -> str:
    """Tell the model which discovered skills this deterministic stage requires."""

    if not skill_names:
        return ""
    paths = [f"`/skills/{name}/SKILL.md`" for name in skill_names]
    return section(
        "Stage Skill Activation",
        [
            f"ARC has selected these required stage skills: {', '.join(paths)}.",
            "Before any `/workspace` exploration, directly `read_file` every listed `SKILL.md` and follow its instructions.",
            "Only the listed skill files are readable for this stage. Do not attempt to read, list, search, or infer unlisted skills.",
        ],
    )


def code_task_exploration_policy() -> str:
    return section(
        "Exploration Discipline",
        [
            "Start from the current requirement snapshot, interface contract, test manifest, and latest failure output. Treat those as primary evidence.",
            "Before calling tools, form one concrete hypothesis and choose the minimum evidence needed to prove or disprove it.",
            "If the failure output names files, symbols, stack frames, or config paths, inspect those first before any broader search.",
            "Prefer the smallest directly related file set that can support one edit or design hypothesis. Do not broad-scan the repo unless narrow reads fail to localize the issue.",
            "After each tool result, update the hypothesis and move toward an edit or final artifact. Do not repeat the same search pattern without new evidence.",
            "Once the cause is localized enough to edit, stop exploring and change the owning file, test, or config directly.",
            "Do not read build/test harness files, package manifests, or runtime infrastructure unless the current stage owns that boundary or a failure explicitly points there.",
            "If a tool result is enough to return a valid stage artifact, stop using tools and return the artifact.",
        ],
    )


def response_contract() -> str:
    return section(
        "Response Contract",
        [
            "Your final assistant message must be a single valid JSON object and nothing else.",
            "Do not wrap the final JSON in Markdown fences, prose, labels, or tool-call narration.",
            "Use the keys requested by the current stage, such as `summary`, `interfaces`, `tests`, and `files_written`.",
            "Keep outputs deterministic, scoped to the current node, and suitable for system-side validation.",
            "If blocked, return JSON with a precise `summary`, empty artifact arrays, and the evidence gathered; do not fabricate artifacts.",
        ],
    )


def task_context_block(
    *,
    node_id: str,
    dynamic_context: str,
    requirement_data: dict[str, Any],
    extra_sections: list[str] | None = None,
) -> str:
    sections = [
        f"### Current Node\n`{node_id}`",
        "### Dynamic Context",
        dynamic_context.strip(),
        "### Requirement Snapshot",
        f"```json\n{json_block(requirement_data)}\n```",
    ]
    sections.extend(section.strip() for section in (extra_sections or []) if section.strip())
    return "\n\n".join(sections).strip()
