import json
import os
from typing import Any, Dict, List

from .arc_agent import ARCAgent
from .prompt_sections import (
    get_common_session_guidance,
    get_compiler_role_guidance,
    get_using_your_tools_guidance,
)


class TestFailureVerifier(ARCAgent):
    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="TestFailureVerifier",
            log_cb=log_cb,
        )

    def get_system_prompt(self) -> str:
        return f"""{get_compiler_role_guidance(
    role_name="TestFailureVerifier",
    stage_name="independent failure analysis",
    mission=[
        "Your job is to analyze one failing test batch in an isolated read-only session before the main TDD repair loop continues.",
        "You gather only the evidence needed to explain why the latest test batch failed and what the next likely repair path is.",
        "You do not edit files. You produce a compact, implementation-facing conclusion for the main TestDrivenDeveloper session.",
    ],
    outputs=[
        "A compact explanation of the most likely failure causes grounded in direct evidence.",
        "Notes on whether the failure more likely comes from test logic, locator/assertion drift, environment/setup drift, or implementation behavior, without forcing a rigid taxonomy.",
        "Concrete next repair options and the next files the main TDD session should inspect or edit.",
    ],
)}

Rules:
- This is a read-only evidence session. Do not modify files.
- Start from the provided node context, target test code, and latest failing output.
- Prefer direct evidence over speculation. If you lack proof, say that confidence is limited.
- Check whether the failing test still matches the requirement and interfaces before blaming the implementation.
- For E2E failures, explicitly consider whether locators, visible text expectations, routing assumptions, or environment startup assumptions are stale.
- Consider environment and setup issues only when the output or config evidence points there.
- Keep the conclusion light and structured. Do not force the result into a narrow error taxonomy.
- Return exactly one JSON object in a `json` markdown block.

{get_common_session_guidance()}

{get_using_your_tools_guidance()}
"""

    def get_tool_names(self) -> List[str]:
        return [
            "read_file",
            "list_directory",
            "glob",
            "grep",
            "run_build",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "get_node_relations",
        ]

    @staticmethod
    def _read_target_test_code(test_files: list[str], max_chars_per_file: int = 4000, max_files: int = 3) -> str:
        from utils import get_abs_path

        blocks: list[str] = []
        for file_path in test_files[:max_files]:
            abs_path = get_abs_path(file_path)
            if not abs_path or not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except Exception:
                continue
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + "\n// ... [truncated]"
            blocks.append(f"// === {file_path} ===\n{content}")
        if not blocks:
            return ""
        return "<target_test_code>\n" + "\n\n".join(blocks) + "\n</target_test_code>"

    @staticmethod
    def _build_verifier_prompt(
        node_id: str,
        test_type: str,
        dynamic_ctx: str,
        test_files: list[str],
        latest_test_output: str,
        target_test_code: str,
    ) -> str:
        serialized_files = json.dumps(test_files, indent=2, ensure_ascii=False)
        return f"""
### Current Node Context
Read this first. The current requirement payload below is the authoritative task input for node `{node_id}`.
{dynamic_ctx}

### Target Test Batch
- Test type: {test_type}
- Target test files:
{serialized_files}

{target_test_code}

### Latest Failing Test Output
<latest_test_output>
{latest_test_output}
</latest_test_output>

### Task
Investigate this failing batch in an isolated verifier-style session.
You may inspect the failing tests, related owner files, route/layout/provider wiring, and nearby configuration or environment files.
Do not edit anything. Gather the smallest sufficient evidence set, then return one compact JSON object in a `json` markdown block:

{{
  "failure_summary": "one short grounded summary",
  "likely_causes": [
    {{
      "summary": "most likely cause",
      "evidence": ["direct evidence"],
      "repair_options": ["likely repair path"]
    }}
  ],
  "requirement_alignment_notes": ["whether the test still matches the requirement/interfaces"],
  "test_asset_notes": ["locator/assertion/setup concerns if any"],
  "environment_notes": ["environment or startup concerns if any"],
  "implementation_notes": ["implementation or wiring concerns if any"],
  "recommended_next_steps": ["what the main TDD session should do next"],
  "confidence": "low|medium|high"
}}

Rules for the JSON:
- Keep it compact.
- `likely_causes` should usually contain 1-3 items.
- Every cause must be backed by evidence you actually observed.
- If you suspect the test itself is wrong, say why in requirement-facing terms.
- If no environment issue is evidenced, keep `environment_notes` empty.
- Do not append prose after the JSON block.
"""

    def build_initial_messages(
        self,
        node_id: str,
        test_files: list[str],
        test_type: str,
        latest_test_output: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            target_test_files=test_files,
        )
        target_test_code = self._read_target_test_code(test_files)
        user_prompt = self._build_verifier_prompt(
            node_id=node_id,
            test_type=test_type,
            dynamic_ctx=dynamic_ctx,
            test_files=test_files,
            latest_test_output=latest_test_output,
            target_test_code=target_test_code,
        )
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        tools = [TOOL_REGISTRY[name]["schema"] for name in self.get_tool_names() if name in TOOL_REGISTRY]
        return messages, tools

