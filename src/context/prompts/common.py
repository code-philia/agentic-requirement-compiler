from __future__ import annotations

import json
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
            "Before returning, run a private consistency check: requirement satisfied, interfaces/tests/implementation connected, no detached files, no fake placeholder success, and no contradiction with parent or dependency ownership.",
            "Report only concise conclusions in `summary` fields or final text; summaries should explain the chosen direction and remaining evidence without dumping step-by-step private reasoning.",
        ],
    )


def whole_app_policy() -> str:
    return section(
        "Whole-App Coherence",
        [
            "Treat UI, client API calls, backend routes, service logic, persistence, tests, and runtime startup as one application path.",
            "A local feature is not complete if it only changes the visible page while leaving API, state, persistence, routing, or shared shell behavior disconnected.",
            "Prefer integrating with existing app structure over creating parallel files, duplicate state containers, duplicate route trees, or isolated helper modules.",
            "When touching cross-cutting concerns such as auth/session, search state, selected booking context, or current user state, keep the shared source of truth explicit and consumed by all affected surfaces.",
            "Do not make tests pass by weakening the application path: avoid hardcoded runtime data, local-only fake state, fallback arrays, or test-only behavior unless the requirement explicitly asks for a mock boundary.",
            "For web apps, remember that the user will experience the backend-hosted built frontend; implementation choices must work through that hosted runtime.",
        ],
    )


def web_runtime_contract() -> str:
    return section(
        "Web Runtime Contract",
        [
            "For web apps, the hosted runtime is backend-led: enter `frontend` and run `npm run build`, then enter `backend` and run `npm run start` to serve the built frontend dist.",
            "The backend process is responsible for hosting `frontend/dist` on the single web port; do not assume a separate frontend dev server is part of the runtime.",
            "E2E and runtime verification should target the backend-hosted origin after the frontend build completes.",
        ],
    )


def workspace_tool_policy() -> str:
    return section(
        "Tool Policy",
        [
            "Use file tools only inside the virtual project root `/workspace`.",
            "Do not call file tools on `/`, host paths, `.arc`, `.git`, `requirements`, environment files, dependency directories, generated outputs, or lockfiles.",
            "Use dedicated file tools for file work: `glob` for file discovery, `grep` for content search, `read_file` for reading, `edit_file` for modifying existing files, and `write_file` only for new files.",
            "Do not use the shell `execute` tool for work that a dedicated file tool can do.",
            "Start exploration with exact paths from the requirement, interface contract, test manifest, traceability records, or failure output.",
            "Avoid broad `grep`, broad `glob`, and directory inventory from `/workspace`; use at most one narrow discovery step before switching to exact path reads.",
            "The `glob` tool uses simple glob patterns; do not rely on brace expansion such as `**/*.{ts,tsx}`.",
            "Read before editing. Prefer `edit_file` for existing files and `write_file` only for genuinely new files.",
            "Shell execution is available through the built-in `execute` tool. Run commands only from `/workspace` or a subdirectory, and never target host paths outside `/workspace`.",
            "Prefer stage-specific system tools such as `run_tests` or `run_build` for build/test feedback when they are explicitly available; use shell commands only for scoped inspection, diagnostics, or skill-required scripts.",
            "Read-only traceability tools are available: `get_interfaces_for_requirement(req_id)`, `get_interface(interface_id)`, and `search_interfaces(keyword, req_id?, interface_type?, limit?)`.",
            "Use traceability tools when interface context is missing, stale, or ambiguous; they return raw database interface records, including original `content`, without summarization.",
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
