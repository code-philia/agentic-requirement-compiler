import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import warnings
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests
import urllib3

from runtime_sdk import get_runtime
from .arc_agent import ARCAgent
from .prompt_sections import (
    get_common_session_guidance,
    get_compiler_role_guidance,
    get_interface_designer_guidance,
)
from utils import read_json_file, write_json_file

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)


class InterfaceDesigner(ARCAgent):
    VISUAL_ANALYSIS_PROMPT_VERSION = "frontend-style-requirements"

    def __init__(self, log_cb=None):
        super().__init__(
            agent_name="InterfaceDesigner",
            log_cb=log_cb,
        )
        self._existing_file_write_guard = False
        self._materialization_state_by_node: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _normalize_path(path: str) -> str:
        return str(path or "").strip().replace("\\", "/")

    @staticmethod
    def _append_unique_limited(items: list[str], value: str, limit: int = 12) -> None:
        text = str(value or "").strip()
        if not text or text in items:
            return
        items.append(text)
        if len(items) > limit:
            del items[:-limit]

    def _build_materialization_state(
        self,
        node_id: str,
        interfaces: list[dict[str, Any]],
        design_mode: str,
    ) -> dict[str, Any]:
        owner_files: list[str] = []
        interface_statuses: dict[str, str] = {}
        file_to_interfaces: dict[str, list[str]] = {}

        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            interface_id = str(interface.get("interface_id", "") or "").strip()
            file_path = self._normalize_path(interface.get("file_path", ""))
            if interface_id:
                interface_statuses[interface_id] = "pending"
            if not file_path:
                continue
            if file_path not in owner_files:
                owner_files.append(file_path)
            file_to_interfaces.setdefault(file_path, [])
            if interface_id and interface_id not in file_to_interfaces[file_path]:
                file_to_interfaces[file_path].append(interface_id)

        return {
            "node_id": node_id,
            "design_mode": design_mode,
            "owner_files": owner_files,
            "interface_statuses": interface_statuses,
            "file_to_interfaces": file_to_interfaces,
            "read_counts": {},
            "known_facts": [],
            "verified_reuse_no_change_files": [],
            "target_edit_files": [],
            "open_questions": [],
            "modified_files": [],
            "allowed_reread_once": set(),
            "has_materialized_change": False,
            "has_run_build": False,
        }

    def _start_materialization_session(
        self,
        node_id: str,
        interfaces: list[dict[str, Any]],
        design_mode: str,
    ) -> None:
        state = self._build_materialization_state(node_id=node_id, interfaces=interfaces, design_mode=design_mode)
        self._materialization_state_by_node[node_id] = state
        if state["owner_files"]:
            self._append_unique_limited(
                state["known_facts"],
                "Frozen owner files: " + ", ".join(state["owner_files"]),
            )

    def _finish_materialization_session(self, node_id: str) -> None:
        self._materialization_state_by_node.pop(node_id, None)

    def _get_materialization_state(self, node_id: str | None) -> dict[str, Any] | None:
        if not node_id:
            return None
        return self._materialization_state_by_node.get(node_id)

    def _all_owner_files_read_once(self, state: dict[str, Any]) -> bool:
        owner_files = state.get("owner_files") or []
        if not owner_files:
            return False
        read_counts = state.get("read_counts") or {}
        return all(int(read_counts.get(path, 0)) > 0 for path in owner_files)

    def _mark_interface_status_for_path(self, state: dict[str, Any], path: str, status: str) -> None:
        normalized_path = self._normalize_path(path)
        for interface_id in state.get("file_to_interfaces", {}).get(normalized_path, []):
            state["interface_statuses"][interface_id] = status

    def _mark_files(self, state: dict[str, Any], key: str, files: list[str]) -> None:
        bucket = state.setdefault(key, [])
        for path in files:
            normalized_path = self._normalize_path(path)
            if normalized_path and normalized_path not in bucket:
                bucket.append(normalized_path)

    @staticmethod
    def _extract_path_tokens(text: str) -> list[str]:
        if not text:
            return []
        matches = re.findall(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)", text)
        paths: list[str] = []
        for match in matches:
            normalized = str(match or "").strip().replace("\\", "/")
            if normalized and normalized not in paths:
                paths.append(normalized)
        return paths

    @staticmethod
    def _extract_heading_block(text: str, heading: str) -> str:
        pattern = re.compile(
            rf"{re.escape(heading)}\s*:?(.*?)(?:\n[A-Z][A-Z_ ]{{2,}}\s*:|\Z)",
            re.DOTALL,
        )
        match = pattern.search(text)
        return match.group(1).strip() if match else ""

    def _update_materialization_state_from_assistant_text(
        self,
        assistant_text: str,
        node_id: str | None = None,
    ) -> None:
        state = self._get_materialization_state(node_id)
        if not state or not assistant_text:
            return

        known_facts_block = self._extract_heading_block(assistant_text, "KNOWN_FACTS")
        verified_block = self._extract_heading_block(assistant_text, "VERIFIED_REUSE_NO_CHANGE_FILES")
        target_block = self._extract_heading_block(assistant_text, "TARGET_EDIT_FILES")
        open_questions_block = self._extract_heading_block(assistant_text, "OPEN_QUESTIONS")
        interface_status_block = self._extract_heading_block(assistant_text, "INTERFACE_STATUSES")
        reread_reason_block = self._extract_heading_block(assistant_text, "RE_READ_REASON")
        target_file_block = self._extract_heading_block(assistant_text, "TARGET_FILE")
        next_action_block = self._extract_heading_block(assistant_text, "NEXT_ACTION")

        for line in known_facts_block.splitlines():
            cleaned = line.lstrip("- ").strip()
            if cleaned:
                self._append_unique_limited(state["known_facts"], cleaned)

        verified_files = self._extract_path_tokens(verified_block)
        target_files = self._extract_path_tokens(target_block)
        open_question_lines = [line.lstrip("- ").strip() for line in open_questions_block.splitlines() if line.strip()]

        self._mark_files(state, "verified_reuse_no_change_files", verified_files)
        self._mark_files(state, "target_edit_files", target_files)
        for path in verified_files:
            self._mark_interface_status_for_path(state, path, "verified_reuse_no_change")
        for path in target_files:
            self._mark_interface_status_for_path(state, path, "change_required")
        state["open_questions"] = open_question_lines[-6:]

        for raw_line in interface_status_block.splitlines():
            line = raw_line.strip().lstrip("- ").strip()
            if not line or ":" not in line:
                continue
            interface_id, status = line.split(":", 1)
            interface_id = interface_id.strip()
            normalized_status = status.strip().lower().replace(" ", "_")
            if interface_id in state["interface_statuses"] and normalized_status:
                state["interface_statuses"][interface_id] = normalized_status

        if reread_reason_block and target_file_block and "read" in next_action_block.lower():
            reread_targets = self._extract_path_tokens(target_file_block)
            if not reread_targets:
                reread_targets = self._extract_path_tokens(reread_reason_block)
            for path in reread_targets:
                state["allowed_reread_once"].add(self._normalize_path(path))

    def _record_materialization_tool_result(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        node_id: str | None = None,
    ) -> None:
        state = self._get_materialization_state(node_id)
        if not state:
            return

        if tool_name == "read_file":
            path = self._normalize_path(tool_args.get("path", ""))
            if not path:
                return
            read_counts = state.setdefault("read_counts", {})
            read_counts[path] = int(read_counts.get(path, 0)) + 1
            self._append_unique_limited(state["known_facts"], f"Read {path}.")
            current_status = "change_required" if path in state.get("target_edit_files", []) else "inspected"
            self._mark_interface_status_for_path(state, path, current_status)
            return

        if tool_name in {"edit_file", "write_file"}:
            path = self._normalize_path(tool_args.get("path", ""))
            if not path:
                return
            state["has_materialized_change"] = True
            self._mark_files(state, "modified_files", [path])
            self._mark_files(state, "target_edit_files", [path])
            self._append_unique_limited(state["known_facts"], f"Modified {path}.")
            self._mark_interface_status_for_path(state, path, "change_required")
            return

        if tool_name != "run_build":
            return

        state["has_run_build"] = True
        build_succeeded = "Exit Code: 0" in str(tool_result or "")
        if build_succeeded:
            self._append_unique_limited(state["known_facts"], "run_build passed.")
            for path in state.get("modified_files", []):
                self._mark_interface_status_for_path(state, path, "implemented")
            inspected_files = [
                path
                for path, count in (state.get("read_counts") or {}).items()
                if int(count) > 0 and path not in state.get("modified_files", [])
            ]
            self._mark_files(state, "verified_reuse_no_change_files", inspected_files)
            for path in inspected_files:
                self._mark_interface_status_for_path(state, path, "verified_reuse_no_change")
        else:
            self._append_unique_limited(state["known_facts"], "run_build failed.")
            if "run_build failed." not in state["open_questions"]:
                state["open_questions"] = (state.get("open_questions") or [])[-5:] + ["run_build failed."]

    def _build_materialization_progress_note(self, state: dict[str, Any]) -> str:
        known_facts = state.get("known_facts") or []
        verified_files = state.get("verified_reuse_no_change_files") or []
        target_files = state.get("target_edit_files") or []
        interface_statuses = state.get("interface_statuses") or {}
        open_questions = state.get("open_questions") or []

        unresolved_paths = [
            path
            for path in state.get("owner_files", [])
            if path not in verified_files and path not in target_files and path not in state.get("modified_files", [])
        ]
        if not open_questions:
            if not self._all_owner_files_read_once(state):
                open_questions = ["Read the remaining frozen owner files once before choosing edit targets."]
            elif unresolved_paths and not state.get("has_materialized_change") and not state.get("has_run_build"):
                open_questions = ["Choose the smallest target edit file or justify one re-read with RE_READ_REASON."]

        next_action = "read remaining owner files"
        if self._all_owner_files_read_once(state):
            next_action = "edit_file on TARGET_EDIT_FILES"
            if not target_files and not state.get("has_materialized_change"):
                next_action = "set TARGET_EDIT_FILES, then edit_file"
        if state.get("has_materialized_change") and not state.get("has_run_build"):
            next_action = "run_build"

        lines = [
            "### Materialization Working Memory",
            f"- Frozen owner files: {', '.join(state.get('owner_files', [])) or 'none'}",
            f"- Owner-file inspection complete: {'yes' if self._all_owner_files_read_once(state) else 'no'}",
            f"- Materialized change landed: {'yes' if state.get('has_materialized_change') else 'no'}",
            f"- Build already run: {'yes' if state.get('has_run_build') else 'no'}",
            "- Interface statuses:",
        ]
        if interface_statuses:
            lines.extend(f"  - {interface_id}: {status}" for interface_id, status in interface_statuses.items())
        else:
            lines.append("  - none")
        lines.append("- Known facts:")
        lines.extend(f"  - {fact}" for fact in known_facts[-8:]) if known_facts else lines.append("  - none")
        lines.append("- Verified reuse no-change files:")
        lines.extend(f"  - {path}" for path in verified_files) if verified_files else lines.append("  - none")
        lines.append("- Target edit files:")
        lines.extend(f"  - {path}" for path in target_files) if target_files else lines.append("  - none")
        lines.append("- Open questions:")
        lines.extend(f"  - {item}" for item in open_questions[:6]) if open_questions else lines.append("  - none")
        lines.append(f"- Next preferred action: {next_action}")
        lines.append("- Re-read gate: after every frozen owner file has been read once, do not re-read the same owner files from uncertainty alone.")
        return "\n".join(lines)

    async def _intercept_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        node_id: str | None = None,
    ) -> tuple[bool, Any]:
        state = self._get_materialization_state(node_id)
        if state and tool_name == "read_file":
            path = self._normalize_path(tool_args.get("path", ""))
            allow_once = path in state.get("allowed_reread_once", set())
            if allow_once:
                state["allowed_reread_once"].discard(path)
            elif (
                path
                and path in (state.get("read_counts") or {})
                and path in (state.get("owner_files") or [])
                and self._all_owner_files_read_once(state)
                and not state.get("has_materialized_change")
                and not state.get("has_run_build")
            ):
                return True, (
                    f"Repeated-read gate: `{path}` was already read in this materialization session, and every frozen owner file "
                    "has already been inspected once.\n"
                    "Do not keep rotating through the same owner files from uncertainty alone.\n\n"
                    "Before any justified re-read, first send a short progress note with these headings:\n"
                    "MATERIALIZATION_PROGRESS\n"
                    "KNOWN_FACTS:\n"
                    "VERIFIED_REUSE_NO_CHANGE_FILES:\n"
                    "TARGET_EDIT_FILES:\n"
                    "OPEN_QUESTIONS:\n"
                    "INTERFACE_STATUSES:\n"
                    "RE_READ_REASON:\n"
                    "TARGET_FILE:\n"
                    "NEXT_ACTION:\n\n"
                    "Preferred next action: choose `TARGET_EDIT_FILES` and call `edit_file`, or call `run_build` if you already landed a change."
                )

        if self._existing_file_write_guard and tool_name == "write_file":
            path = str(tool_args.get("path", "")).strip()
            if path and os.path.exists(path):
                return True, (
                    f"Error: `{path}` already exists. Do not overwrite existing files with `write_file` in "
                    "`InterfaceDesigner` materialization or convergence. Read the file and use `edit_file` for a "
                    "minimal targeted change instead."
                )
        return await super()._intercept_tool_call(tool_name, tool_args, node_id)

    async def _on_assistant_message_before_tool_calls(
        self,
        assistant_text: str,
        node_id: str | None = None,
    ) -> None:
        self._update_materialization_state_from_assistant_text(assistant_text, node_id=node_id)
        await super()._on_assistant_message_before_tool_calls(assistant_text, node_id=node_id)

    async def _get_stop_response_after_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        node_id: str | None = None,
    ) -> str | None:
        self._record_materialization_tool_result(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            node_id=node_id,
        )
        return await super()._get_stop_response_after_tool_call(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            node_id=node_id,
        )

    def _build_ephemeral_user_message(self, node_id: str | None = None) -> str | None:
        state = self._get_materialization_state(node_id)
        if not state:
            return None
        return self._build_materialization_progress_note(state)

    @staticmethod
    def build_visual_analysis_prompt() -> str:
        return """
**ROLE:** You extract frontend style requirements from an input UI image.
**SCENARIO:** Your output will be used as the visual requirement for frontend implementation. Focus on layout, composition, component styling, interaction surfaces, and data presentation patterns. Do NOT treat the image as a source of mock business data.

**PRIMARY GOAL**
Convert the image into implementation-oriented frontend style requirements that define:
- page layout hierarchy
- section composition
- component appearance
- typography and color usage
- spacing and alignment patterns
- how future runtime data should be displayed visually

**CORE DIRECTIVES**
1. **STYLE REQUIREMENTS OVER OCR:** Focus on structure, hierarchy, spacing rhythm, grouping, visual emphasis, and component roles. Do not perform exhaustive OCR transcription.
2. **NO BUSINESS-DATA EXTRACTION:** Do NOT copy screenshot-specific records, names, phone numbers, emails, IDs, dates, prices, counts, table rows, chart values, or other instance data as future mock content.
3. **EXTRACT DATA DISPLAY PATTERNS:** If the image shows lists, tables, cards, charts, schedules, dashboards, or other data regions, describe:
   - the container structure
   - the column/field types
   - the visual encoding style
   - the density and alignment
   - the expected future data shape
   Do NOT reproduce the actual row values.
4. **CAPTURE ONLY STRUCTURAL COPY:** Keep visible text only when it defines page chrome or interaction structure, such as navigation labels, section titles, field labels, button labels, tab names, or status categories. Prefer concise summaries over full transcription.
5. **STRICT STRUCTURAL HIERARCHY:** Describe the page as a tree structure (Parent -> Child -> Sibling).
6. **PRECISE VISUAL SPECS:** Estimate layout mode, proportions, spacing, emphasis, colors, border treatment, shadows, and typography scale. Use approximate values where helpful.
7. **REQUIREMENT LANGUAGE:** Write as frontend style requirements, not as an image caption and not as a pixel-perfect forensic report.

**OUTPUT FORMAT (Strict Markdown)**

### 1. Frontend Style Direction
* **Colors:** Primary, secondary, surfaces, borders, emphasis states (approximate hex allowed).
* **Typography:** Font style, size scale, weight pattern.
* **Spacing Rhythm:** Dense / medium / spacious, notable gaps/padding.
* **Overall Tone:** e.g. enterprise dashboard, lightweight consumer portal, dense admin console, etc.

### 2. Page Skeleton (Top to Bottom)
For each major section:
* **Section Name**
* **Purpose**
* **Container:** width behavior, layout mode, background, border/shadow, spacing.
* **Children:** ordered structural elements and their relationships.
* **Style Notes:** corner radius, dividers, emphasis, icon usage, visual weight.

### 3. Data Presentation Style
For each data-bearing area (table/list/cards/calendar/chart/schedule):
* **Pattern Type:** table / cards / timeline / schedule grid / chart / etc.
* **Visual Structure:** columns, lanes, cards, badges, legends, filters, pagination, empty/loading states.
* **Field Types:** what kinds of values appear there in the future.
* **Styling Rules:** alignment, emphasis, truncation, badge colors, density.
* **Do Not Copy Actual Values:** summarize the shape only.

### 4. Interaction Surfaces
* Main forms, filters, selectors, buttons, tabs, pagination, dialogs, and feedback areas.
* Note which controls appear primary vs secondary.
* Describe the expected visual style of controls rather than the concrete values shown in the image.

### 5. Frontend Requirements Summary
* **Must Preserve:** structural and style traits that should be kept in implementation.
* **Runtime-Driven Areas:** regions that must stay data-driven instead of hardcoded from the image.
* **Avoid:** screenshot-specific business content being turned into seeded UI data.
"""

    @staticmethod
    def _visual_cache_path(workspace_path: str) -> str:
        return os.path.join(workspace_path, ".arc", "visual_analysis_cache.json")

    @classmethod
    def _load_visual_cache(cls, workspace_path: str) -> dict[str, Any]:
        return read_json_file(cls._visual_cache_path(workspace_path)) or {}

    @classmethod
    def _save_visual_cache(cls, workspace_path: str, cache: dict[str, Any]) -> None:
        write_json_file(cls._visual_cache_path(workspace_path), cache)

    @staticmethod
    def _build_visual_cache_key(full_path: str, prompt_version: str) -> str:
        stat = os.stat(full_path)
        raw_key = (
            f"{os.path.abspath(full_path)}::{int(stat.st_mtime_ns)}::{stat.st_size}::{prompt_version}"
        )
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def resolve_visual_api_key() -> str:
        return os.environ.get("VISUAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    @staticmethod
    def resolve_visual_api_url() -> str:
        raw_url = (
            os.environ.get("VISUAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL", "")
        ).strip()
        if not raw_url:
            return ""

        parsed = urlparse(raw_url)
        normalized_path = parsed.path.rstrip("/")
        if normalized_path.endswith("/chat/completions"):
            return raw_url.rstrip("/")

        if not normalized_path:
            normalized_path = "/chat/completions"
        else:
            normalized_path = f"{normalized_path}/chat/completions"

        return parsed._replace(path=normalized_path).geturl()

    async def parse_and_store_visual_elements(
        self,
        workspace_path: str,
        requirements_dir: str,
        requirement_data: dict[str, Any],
    ) -> None:
        description = requirement_data.get("description", "")
        req_id = requirement_data.get("req_id") or requirement_data.get("id", "")
        if not description or not req_id:
            if self.log_cb:
                await self.log_cb("System", "Invalid requirement data", "error", req_id or None)
            return

        matches = re.findall(r"!\[image\]\(([^)]+)\)", description)
        if not matches:
            if self.log_cb:
                await self.log_cb("System", "No image found in the description.", "info", req_id)
            return

        visual_cache = self._load_visual_cache(workspace_path)
        cache_updated = False
        visual_references = []
        for image_path in matches:
            normalized_path = os.path.normpath(image_path)
            if normalized_path.startswith(os.sep):
                normalized_path = normalized_path.lstrip(os.sep)

            base_dir = requirements_dir if requirements_dir else workspace_path
            full_path = os.path.abspath(os.path.join(base_dir, normalized_path))
            if not os.path.exists(full_path):
                if self.log_cb:
                    await self.log_cb("System", f"Image not found: {full_path}", "warning", req_id)
                continue

            try:
                cache_key = self._build_visual_cache_key(
                    full_path,
                    self.VISUAL_ANALYSIS_PROMPT_VERSION,
                )
                cached_entry = visual_cache.get(cache_key)
                if cached_entry and cached_entry.get("analysis"):
                    visual_references.append({"image_path": image_path, "analysis": cached_entry["analysis"]})
                    if self.log_cb:
                        await self.log_cb("System", f"Reusing cached visual analysis: {image_path}", None, req_id)
                    continue

                mime_type, _ = mimetypes.guess_type(full_path)
                if not mime_type:
                    mime_type = "image/png"

                with open(full_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode("utf-8")

                if self.log_cb:
                    await self.log_cb("System", f"Analyzing visual element: {image_path}", None, req_id)

                visual_api_url = self.resolve_visual_api_url()
                visual_api_key = self.resolve_visual_api_key()
                if not visual_api_url:
                    raise Exception("Visual API base URL is not configured.")
                if not visual_api_key:
                    raise Exception("Visual API key is not configured.")

                response = await asyncio.to_thread(
                    requests.post,
                    visual_api_url,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {visual_api_key}",
                    },
                    data=json.dumps(
                        {
                            "model": os.environ.get("VISUAL_MODEL", os.environ.get("MODEL", "")),
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": self.build_visual_analysis_prompt()},
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": f"data:{mime_type};base64,{base64_image}",
                                            },
                                        },
                                    ],
                                }
                            ],
                        }
                    ),
                    verify=False,
                )

                if response.status_code != 200:
                    raise Exception(f"ModelArts API Error {response.status_code}: {response.text}")

                response_data = response.json()
                analysis = response_data["choices"][0]["message"]["content"]
                visual_cache[cache_key] = {
                    "image_path": image_path,
                    "full_path": os.path.abspath(full_path),
                    "prompt_version": self.VISUAL_ANALYSIS_PROMPT_VERSION,
                    "analysis": analysis,
                }
                cache_updated = True
                visual_references.append({"image_path": image_path, "analysis": analysis})
            except Exception as exc:
                if self.log_cb:
                    await self.log_cb("System", f"Failed to analyze image {image_path}: {exc}", "error", req_id)

        if cache_updated:
            self._save_visual_cache(workspace_path, visual_cache)

        if visual_references:
            get_runtime().traceability.update_requirement_fields(
                req_id,
                visual_reference=visual_references,
            )
            if self.log_cb:
                await self.log_cb(
                    "System",
                    f"Stored {len(visual_references)} visual references for {req_id}",
                    None,
                    req_id,
                )

    def get_system_prompt(self) -> str:
        return f"""{get_compiler_role_guidance(
    role_name="InterfaceDesigner",
    stage_name="interface design and materialization workflow",
    mission=[
        "Across this node's lifecycle, your job is to first understand the requirement, then explore the most relevant codebase evidence, then land the owned UI code or interface code, and finally summarize the result as strict structured interfaces when the task is a design-bundle call.",
        "For leaf nodes, that usually means a minimal executable chain across UI -> API -> FUNC -> DB.",
        "For non-leaf nodes, that usually means only parent shell assembly such as routes, layouts, providers, containers, and mount points.",
    ],
    outputs=[
        "A compact understanding of the current node grounded in the requirement and existing codebase.",
        "A minimal interface set with clear ownership, file paths, reuse decisions, and brief contract fields.",
        "Landed code for current-node UI and scaffolding for current-node non-UI interfaces when the call requires implementation work.",
    ],
)}

Rules:
- Reuse existing interfaces before inventing new ones.
- Respect `<requirement_focus>`, `<scenarios>`, `<visual_reference>`, and the declared interface ownership.
- If the requirement names exact UI ids or resource ids, keep them exact.
- For leaf work, design the smallest complete chain needed across UI -> API -> FUNC -> DB.
- For non-leaf work, stay at parent UI shell scope: routes, layouts, providers, page containers, mount points, and thin composition files.
- When visual reference shows business records or data values, treat them as display examples only. Do not convert them into seeded implementation data, fallback arrays, or interface-level mock payloads.
- In a design-bundle call, first understand and inspect, then write code, then return the final strict structured interface bundle.
- In a design-only call, do not write code files.
- In a frozen-contract materialization call, treat the provided interfaces as the contract unless the user prompt explicitly asks for a repair.
- In any implementation-bearing call, UI interfaces owned by the current node must be implemented as real UI code, not left as empty stubs.
- In any implementation-bearing call, do not rely on hardcoded sample rows, screenshot-derived values, fake success payloads, or fallback data to make the owned feature appear complete.
- In any implementation-bearing call, non-UI interfaces owned by the current node must be landed as minimal compilable code skeletons or stubs aligned with the declared contract fields.
- Prefer extending existing files with minimal edits over creating parallel files.
- Maintain compact working memory while materializing: `KNOWN_FACTS`, `VERIFIED_REUSE_NO_CHANGE_FILES`, `TARGET_EDIT_FILES`, `OPEN_QUESTIONS`, and per-interface status.
- Use only these interface status values when you need to reason explicitly about progress: `pending`, `inspected`, `change_required`, `verified_reuse_no_change`, `implemented`, `blocked`.
- Once every frozen owner file has been read once, prefer `edit_file` on the chosen target files. Do not keep re-reading the same owner files from uncertainty alone.

{get_common_session_guidance()}

{get_interface_designer_guidance()}
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

    def _get_implement_tool_names(self) -> List[str]:
        return [
            "read_file",
            "write_file",
            "edit_file",
            "delete_file",
            "list_directory",
            "glob",
            "grep",
            "run_build",
            "search_interfaces_by_keyword",
            "search_interfaces_by_relation",
            "find_interface_impacts",
            "get_node_relations",
        ]

    @staticmethod
    def _extract_json_object_from_markdown(raw_output: str) -> dict[str, Any] | None:
        if not raw_output:
            return None
        fenced = re.search(r"```json\s*(.*?)\s*```", raw_output, re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1) if fenced else raw_output
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else None
        except Exception:
            pass
        span = re.search(r"(\{\s*\"[\s\S]*\})", raw_output)
        if not span:
            return None
        try:
            data = json.loads(span.group(1))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _extract_json_array_from_markdown(raw_output: str) -> list[dict[str, Any]] | None:
        if not raw_output:
            return None
        fenced = re.search(r"```json\s*(.*?)\s*```", raw_output, re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1) if fenced else raw_output
        try:
            data = json.loads(candidate)
            return data if isinstance(data, list) else None
        except Exception:
            pass
        span = re.search(r"(\[\s*{[\s\S]*}\s*\])", raw_output)
        if not span:
            return None
        try:
            data = json.loads(span.group(1))
            return data if isinstance(data, list) else None
        except Exception:
            return None

    @staticmethod
    def _build_design_stage_task(design_mode: str) -> str:
        if design_mode == "leaf_full":
            return """
This is the design step of `InterfaceDesigner` for a leaf node.
- Analyze the current requirement, scenarios, visual reference, and the most relevant existing code.
- Design the smallest executable interface chain needed across UI -> API -> FUNC -> DB.
- Reuse existing interfaces whenever possible, and only add the minimum new contracts needed to land the feature.
- For each interface, include brief contract fields that say what responsibility it owns and how that interface should be tested.
- If the UI contains data-bearing regions, design them as runtime-driven surfaces backed by the owned chain, not as hardcoded example content from the reference image.
- Do not write code in this stage.
"""
        if design_mode == "non_leaf_full":
            return """
This is the design step of `InterfaceDesigner` for a non-leaf node with concrete scenarios.
- Analyze the current requirement, scenarios, visual reference if present, and the most relevant existing code.
- Produce a machine-verifiable parent contract through explicit parent-owned interfaces, not only prose guidance.
- Only include parent-owned interfaces when the current non-leaf truly needs shell-level UI, routes, layouts, providers, guards, slots, props/context exposure, or mount points.
- Make the parent contract explicit through shell-level interfaces that describe routes, layouts, providers, slots, navigation entry points, and data handoff points.
- If the visual reference shows lists, tables, dashboards, schedules, or cards, express them as shell-level presentation structure only, not as copied record content.
- Do not write code in this stage.
"""
        return """
This is the design step of `InterfaceDesigner` for a non-leaf UI-only parent node.
- Analyze the current parent shell and the most relevant existing shared files.
- Produce a machine-verifiable parent contract through explicit parent-owned interfaces, not only prose guidance.
- If shell-level UI is required, design only parent UI shell interfaces: top-level routes, layouts, providers, guards, slots, mount points, and thin composition files.
- Treat visual reference data regions as layout/style guidance only. Do not turn screenshot values into parent-owned mock content.
- Do not design API/FUNC/DB interfaces in this mode.
- Do not write code in this stage.
"""

    @staticmethod
    def _build_materialize_stage_task(design_mode: str) -> str:
        if design_mode == "leaf_full":
            return """
This is the materialization step of `InterfaceDesigner` for a leaf node.
- Materialize the current node's interfaces into code.
- For UI interfaces, land real UI code now.
- If the UI shows fetched or persisted data, wire the real owned runtime path or explicit loading/empty/error states. Do not hardcode sample records to make the page look complete.
- For non-UI interfaces, land the smallest compilable or runnable skeleton that matches the declared responsibility and test intent.
"""
        if design_mode == "non_leaf_full":
            return """
This is the materialization step of `InterfaceDesigner` for a non-leaf node with concrete scenarios.
- Materialize the parent-owned contract and page shell into code now.
- Land the parent shell interfaces for routes, layouts, providers, guards, slots, mount points, and shared composition files.
- Do not implement child business logic here.
- If the parent shell shows fetched or persisted data, wire the real parent-owned runtime path or explicit loading/empty/error states. Do not hardcode sample records to make the page look complete.
- For non-UI shell interfaces, land the smallest compilable or runnable skeleton that matches the declared responsibility and test intent.
"""
        return """
This is the materialization step of `InterfaceDesigner` for a non-leaf UI-only parent node.
- Materialize only the parent shell interfaces for routes, layouts, providers, guards, slots, mount points, and composition files.
- Do not expand into API/FUNC/DB work in this mode.
"""

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        items: list[str] = []
        for raw in value:
            text = str(raw or "").strip()
            if text and text not in items:
                items.append(text)
        return items

    @staticmethod
    def _enrich_interfaces_with_contracts(
        interfaces: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            merged = dict(interface)
            merged["responsibility"] = str(
                merged.get("responsibility")
                or merged.get("description")
                or ""
            ).strip()
            merged["specification"] = str(
                merged.get("specification")
                or merged.get("responsibility")
                or merged.get("description")
                or ""
            ).strip()
            raw_test_focus = merged.get("test_focus")
            if not isinstance(raw_test_focus, list) or not raw_test_focus:
                raw_test_focus = [
                    "Verify the interface is reachable from its declared callers or entrypoints.",
                    "Verify the primary observable behavior described by the requirement.",
                ]
            merged["test_focus"] = [str(item).strip() for item in raw_test_focus if str(item).strip()]
            raw_reuse_notes = merged.get("reuse_notes")
            if not isinstance(raw_reuse_notes, list):
                raw_reuse_notes = []
            merged["reuse_notes"] = [str(item).strip() for item in raw_reuse_notes if str(item).strip()]
            merged["file_path"] = str(merged.get("file_path", "") or "").strip()
            merged["first_line"] = str(merged.get("first_line", "") or "").strip()
            enriched.append(merged)
        return enriched

    @staticmethod
    def _build_design_repair_prompt() -> str:
        return """
Your previous reply did not return a valid design bundle JSON object.

Do not read more files.
Do not call any tools.
Do not make more code changes.
Based on the requirement, scenarios, and the evidence you already gathered, return the smallest valid design bundle now.

Return exactly one JSON object in a `json` markdown block with this schema:
{
  "interfaces": [
    {
      "interface_id": "stable explicit id",
      "reuse": true,
      "type": "UI/API/FUNC/DB",
      "name": "logical module name",
      "description": "purpose",
      "inputs": ["inputs"],
      "outputs": ["outputs"],
      "callers": ["caller interface ids"],
      "callees": ["callee interface ids"],
      "responsibility": "what this interface owns",
      "specification": "brief behavioral contract for this interface",
      "test_focus": ["what tests should verify for this interface"],
      "reuse_notes": ["important reuse or extension constraints"],
      "file_path": "relative file path",
      "first_line": ""
    }
  ]
}

Rules:
- Do not keep exploring.
- `first_line` may be empty if you already know the correct owner file but do not need another read to confirm the exact signature.
- For leaf nodes, return the minimal executable interface chain.
"""

    @classmethod
    def _build_unified_design_bundle_prompt(
        cls,
        node_id: str,
        dynamic_ctx: str,
        design_mode: str,
    ) -> str:
        return f"""
### Current Node Context
Read this first. The current requirement payload below is the authoritative task input for node `{node_id}`.
{dynamic_ctx}

### Task
This is a single-session design-bundle call of `InterfaceDesigner` for node `{node_id}`.
Work in this order:
1. Understand the requirement, scenarios, visual reference, and parent/child ownership.
2. Explore only the most relevant existing code and reusable interfaces.
3. Land the owned UI code or interface code for this node in the same session.
4. Return the final strict structured design bundle JSON.

### Phase 1: Design guidance
{cls._build_design_stage_task(design_mode).strip()}

### Phase 2: Materialization guidance
{cls._build_materialize_stage_task(design_mode).strip()}

Execution rules:
- This is a code-writing session, but your final response must be only the strict structured design bundle JSON.
- First understand and inspect the most relevant evidence, then edit code, then summarize the landed interfaces.
- If `<visual_reference>` exists, it is a primary UI contract for this node.
- For every current-node UI interface, land real UI code now. Do not leave TODO-only shells, placeholder divs, or interface-only JSON.
- For UI code, follow the visual reference's layout hierarchy, section ordering, stable chrome text, alignment, spacing rhythm, and overall composition as closely as the requirement allows.
- Use the visual reference to drive structure and presentation style, not to source concrete business data values.
- Do not fall back to the starter template look, generic Tailwind composition, or your own preferred layout when the visual reference already specifies one.
- For data-bearing UI regions, bind to the owned runtime data flow when this node owns it. Otherwise, implement explicit empty/loading/error states instead of hardcoded records or fallback arrays.
- Do not introduce screenshot-derived sample rows, hardcoded fallback payloads, fake success messages detached from real writes, or placeholder-only panels just to satisfy visual expectations or tests.
- For every current-node non-UI interface, land the smallest compilable or runnable skeleton that matches the declared responsibility and test intent.
- The final `interfaces` array is the source of truth for ownership, file paths, reuse decisions, and contract fields. Make the returned bundle consistent with the landed code.
- Reuse and minimally edit existing files when possible. If a target file already exists, read it first and use `edit_file`.
- Do not use `write_file` on an existing file in this step.
- Call `run_build` once after landing code when you made code changes.
- If you already know enough to name the smallest target edit file, stop reading and edit it.

When finished, return exactly one JSON object in a `json` markdown block with this schema:
{{
  "interfaces": [
    {{
      "interface_id": "stable explicit id",
      "reuse": true,
      "type": "UI/API/FUNC/DB",
      "name": "logical module name",
      "description": "purpose",
      "inputs": ["inputs"],
      "outputs": ["outputs"],
      "callers": ["caller interface ids"],
      "callees": ["callee interface ids"],
      "responsibility": "what this interface owns",
      "specification": "brief behavioral contract for this interface",
      "test_focus": ["what tests should verify for this interface"],
      "reuse_notes": ["important reuse or extension constraints"],
      "file_path": "relative file path",
      "first_line": "exact signature or declaration line"
    }}
  ]
}}

Rules for the returned JSON:
- Reuse existing interfaces whenever possible.
- Keep the interface chain minimal and executable.
- For non-leaf nodes, make the parent contract explicit through parent-owned shell interfaces whenever the parent owns routes, layouts, providers, guards, slots, mount points, or shared composition code.
- Include brief contract fields directly on each interface object instead of returning a separate `interface_spec` array.
- If `<visual_reference>` exists, use it to determine UI structure, major sections, stable chrome copy, data presentation style, and layout ownership for the UI interfaces.
- Do not copy screenshot-specific row values, names, metrics, or business records into interface specs, seeded mock payloads, or fake default UI data.
- Keep the output compact.
- Keep `responsibility`, `specification`, and `test_focus` brief and concrete.
- Prefer short phrases over full paragraphs.
- `first_line` may be empty if the owner file is clear and another read would add little value.
- Your final response must be the JSON bundle only. Do not append prose after it.
"""

    @staticmethod
    def _parse_design_bundle_payload(raw_output: str) -> dict[str, Any]:
        parsed = InterfaceDesigner._extract_json_object_from_markdown(raw_output) or {}
        interfaces = parsed.get("interfaces")
        if not isinstance(interfaces, list):
            interfaces = []
        interfaces = InterfaceDesigner._enrich_interfaces_with_contracts(interfaces)
        return {
            "interfaces": interfaces,
        }

    async def design_bundle(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        design_mode: str = "leaf_full",
        preloaded_source: str = None,
    ) -> tuple[dict[str, Any], list]:
        from .context_pipeline import context_pipeline
        from .tools import TOOL_REGISTRY

        static_ctx, dynamic_ctx = context_pipeline.build_agent_context_split(
            node_id=node_id,
            agent_type=self.agent_name,
            preloaded_source=preloaded_source,
        )
        user_prompt = self._build_unified_design_bundle_prompt(
            node_id=node_id,
            dynamic_ctx=dynamic_ctx,
            design_mode=design_mode,
        )
        system_content = self.get_system_prompt()
        if static_ctx:
            system_content = f"{system_content}\n\n{static_ctx}"
        design_messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        implement_tools = [TOOL_REGISTRY[name]["schema"] for name in self._get_implement_tool_names() if name in TOOL_REGISTRY]
        previous_guard = self._existing_file_write_guard
        self._existing_file_write_guard = True
        try:
            raw_output, design_messages = await self.run_from_messages(
                design_messages,
                node_id=node_id,
                max_steps=50,
                tools=implement_tools,
            )
        finally:
            self._existing_file_write_guard = previous_guard

        parsed_payload = self._parse_design_bundle_payload(raw_output)
        interfaces = parsed_payload["interfaces"]

        if not interfaces:
            design_messages.append({"role": "user", "content": self._build_design_repair_prompt()})
            repair_output, design_messages = await self.run_from_messages(
                design_messages,
                node_id=node_id,
                max_steps=2,
                tools=[],
            )
            parsed_payload = self._parse_design_bundle_payload(repair_output)
            interfaces = parsed_payload["interfaces"]

        return {
            "interfaces": interfaces,
        }, design_messages

