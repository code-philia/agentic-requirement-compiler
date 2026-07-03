import json
from typing import Any, Dict, List

from .arc_agent import ARCAgent
from .prompt_sections import (
    get_codebase_explorer_guidance,
    get_common_session_guidance,
    get_compiler_role_guidance,
)


class CodebaseExplorer(ARCAgent):
    def __init__(self, log_cb=None, agent_name: str = "CodebaseExplorer"):
        super().__init__(
            agent_name=agent_name,
            log_cb=log_cb,
        )

    def get_system_prompt(self) -> str:
        return f"""{get_compiler_role_guidance(
    role_name=self.agent_name,
    stage_name="read-only codebase exploration",
    mission=[
        "Your job is to localize the smallest high-value evidence set for the current node before a writer agent continues.",
        "You identify likely owner files, reusable boundaries, nearby tests or config, and the next few reads or edit targets.",
        "You stop as soon as the implementation-facing localization problem is solved; you do not keep exploring for completeness.",
    ],
    outputs=[
        "A compact localization report grounded in direct evidence.",
        "Likely owner files, reuse candidates, and adjacent test or configuration files when relevant.",
        "The next recommended reads and likely edit targets for the caller agent.",
    ],
)}

Rules:
- This is a read-only session. Do not modify files.
- Use the prefetched node context, file cards, and interface relations first.
- If the context already localizes the task well enough, do not call tools just to restate it.
- Search narrowly. Prefer relation search and targeted grep/glob before reading files.
- Read only the few files needed to identify likely ownership, boundaries, and the next concrete action.
- Return exactly one JSON object in a `json` markdown block.

{get_common_session_guidance()}

{get_codebase_explorer_guidance()}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file",
            "list_directory",
            "glob",
            "grep",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "find_interface_impacts",
            "get_node_relations",
        ]

    @staticmethod
    def _normalize_string_list(value: Any, limit: int = 6) -> list[str]:
        if not isinstance(value, list):
            return []
        items: list[str] = []
        for raw in value:
            text = str(raw or "").strip()
            if text and text not in items:
                items.append(text)
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _normalize_path_reason_list(value: Any, limit: int = 6) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, str]] = []
        for raw in value:
            if isinstance(raw, dict):
                path = str(raw.get("path", "") or raw.get("file_path", "") or "").strip()
                why = str(raw.get("why", "") or raw.get("reason", "") or "").strip()
            else:
                path = str(raw or "").strip()
                why = ""
            if not path:
                continue
            item = {"path": path, "why": why}
            if item not in items:
                items.append(item)
            if len(items) >= limit:
                break
        return items

    @classmethod
    def _normalize_report(cls, report: Dict[str, Any]) -> dict[str, Any]:
        confidence = str(report.get("confidence", "") or "").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        return {
            "summary": str(report.get("summary", "") or "").strip(),
            "likely_owner_files": cls._normalize_path_reason_list(report.get("likely_owner_files")),
            "reuse_candidates": cls._normalize_path_reason_list(report.get("reuse_candidates")),
            "test_or_config_files": cls._normalize_path_reason_list(report.get("test_or_config_files")),
            "constraints": cls._normalize_string_list(report.get("constraints")),
            "recommended_next_reads": cls._normalize_string_list(report.get("recommended_next_reads")),
            "recommended_edit_targets": cls._normalize_string_list(report.get("recommended_edit_targets")),
            "open_questions": cls._normalize_string_list(report.get("open_questions")),
            "confidence": confidence,
        }

    @classmethod
    def _build_fallback_report(cls, summary: str) -> dict[str, Any]:
        return cls._normalize_report(
            {
                "summary": summary,
                "likely_owner_files": [],
                "reuse_candidates": [],
                "test_or_config_files": [],
                "constraints": [],
                "recommended_next_reads": [],
                "recommended_edit_targets": [],
                "open_questions": ["Explorer output was not valid JSON."],
                "confidence": "low",
            }
        )

    @staticmethod
    def format_report_block(report: dict[str, Any]) -> str:
        return (
            "<codebase_explorer_report>\n"
            "A separate read-only codebase exploration session localized the task before the main agent continues.\n"
            "Use this as a high-priority file-location hint unless direct file evidence disproves it.\n"
            "```json\n"
            f"{json.dumps(report, indent=2, ensure_ascii=False)}\n"
            "```\n"
            "</codebase_explorer_report>"
        )

    def build_initial_messages(
        self,
        node_id: str,
        task_brief: str,
        focus_hints: list[str] | None = None,
        preloaded_source: str | None = None,
        target_test_files: list[str] | None = None,
        extra_context: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
            target_test_files=target_test_files,
        )

        focus_hint_lines = ""
        if focus_hints:
            focus_hint_lines = "\n".join(
                f"- {str(item).strip()}"
                for item in focus_hints
                if str(item).strip()
            )
            if focus_hint_lines:
                focus_hint_lines = f"\n### Focus Hints\n{focus_hint_lines}\n"

        extra_context_block = ""
        if extra_context.strip():
            extra_context_block = f"\n### Extra Context\n{extra_context.strip()}\n"

        user_prompt = f"""
### Current Node Context
Read this first. The current requirement payload below is the authoritative task input for node `{node_id}`.
{dynamic_ctx}
{focus_hint_lines}{extra_context_block}
### Task
{task_brief}

Return exactly one JSON object in a `json` markdown block:
{{
  "summary": "one short grounded localization summary",
  "likely_owner_files": [
    {{
      "path": "relative path",
      "why": "why this file likely owns or anchors the work"
    }}
  ],
  "reuse_candidates": [
    {{
      "path": "relative path",
      "why": "why this existing file or boundary should be reused"
    }}
  ],
  "test_or_config_files": [
    {{
      "path": "relative path",
      "why": "why this test/setup/config file matters"
    }}
  ],
  "constraints": ["important ownership or contract constraints"],
  "recommended_next_reads": ["the next few highest-value files to read"],
  "recommended_edit_targets": ["files the caller will most likely edit"],
  "open_questions": ["what remains uncertain if anything"],
  "confidence": "low|medium|high"
}}

Rules for the JSON:
- Keep it compact.
- Prefer 1-5 likely owner files, not exhaustive lists.
- Use `test_or_config_files` only when they materially affect the task.
- `recommended_next_reads` and `recommended_edit_targets` should be file paths, not prose paragraphs.
- If the task is already well localized from the provided context, keep the report short and do not search broadly.
- Do not append prose after the JSON block.
"""
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        return messages, tools

    async def explore(
        self,
        node_id: str,
        task_brief: str,
        focus_hints: list[str] | None = None,
        preloaded_source: str | None = None,
        target_test_files: list[str] | None = None,
        extra_context: str = "",
        max_steps: int = 10,
    ) -> dict[str, Any]:
        messages, tools = self.build_initial_messages(
            node_id=node_id,
            task_brief=task_brief,
            focus_hints=focus_hints,
            preloaded_source=preloaded_source,
            target_test_files=target_test_files,
            extra_context=extra_context,
        )
        report_text, _ = await self.run_from_messages(
            messages=messages,
            node_id=node_id,
            max_steps=max_steps,
            tools=tools,
        )
        parsed = self._extract_json_object(report_text or "")
        if isinstance(parsed, dict):
            return self._normalize_report(parsed)
        return self._build_fallback_report(
            "Read-only exploration did not return valid JSON; use the existing prefetched context and inspect only the next directly relevant files."
        )
