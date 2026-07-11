import os
import re
import json
from typing import Dict, Any, List
from core.service import get_runtime


def _store():
    return get_runtime().traceability

class NodeContextCache:
    """Caches static context layers for a node across agent phases.
    Avoids re-reading files, re-querying DB, and re-traversing the project tree
    when the data hasn't changed between phases.
    """
    # Layers that are truly global (never change during a run)
    GLOBAL_LAYERS = {"tech_stack_context", "project_structure"}
    # Layers that change only when files are written (after stub impl, test gen, TDD fix)
    FILE_DEPENDENT_LAYERS = {"source_code", "test_code"}
    # Layers that change when DB is updated (after insert_interface, insert_test)
    DB_DEPENDENT_LAYERS = set()

    def __init__(self):
        self._cache = {}  # (node_id, layer_name) -> content_str

    def get_or_compute(self, node_id: str, layer_name: str, compute_fn) -> str:
        key = (node_id, layer_name)
        if key not in self._cache:
            self._cache[key] = compute_fn()
        return self._cache[key]

    def invalidate(self, node_id: str, layer_name: str = None):
        """Invalidate cached layers when underlying data changes."""
        if layer_name:
            self._cache.pop((node_id, layer_name), None)
        else:
            # Invalidate all layers for this node
            self._cache = {k: v for k, v in self._cache.items() if k[0] != node_id}

    def invalidate_file_layers(self, node_id: str):
        """Invalidate layers that depend on file contents (after write_file, run_build)."""
        stale_keys = []
        for cache_node_id, layer_name in self._cache.keys():
            if cache_node_id != node_id:
                continue
            if (
                layer_name in self.FILE_DEPENDENT_LAYERS
                or layer_name.startswith("test_code::")
                or layer_name.startswith("project_structure::")
            ):
                stale_keys.append((cache_node_id, layer_name))
        for cache_key in stale_keys:
            self._cache.pop(cache_key, None)

    def invalidate_db_layers(self, node_id: str):
        """Invalidate layers that depend on DB state (after insert_interface, insert_test)."""
        for layer in self.DB_DEPENDENT_LAYERS:
            self._cache.pop((node_id, layer), None)

    def clear(self):
        self._cache.clear()


class ContextPipeline:
    """
    Inspired by claude-code's context engine.
    Automatically prefetches and layers context before sending to the LLM.
    """
    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = workspace_dir
        self.max_interfaces = 20
        self.max_related_interfaces = 30
        self.max_tests = 20
        self.cache = NodeContextCache()

    @staticmethod
    def _truncate_text(text: Any, limit: int) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[:limit].rstrip() + "... [truncated]"

    @staticmethod
    def _compact_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _normalize_step_keyword(step: Dict[str, Any]) -> str:
        return str(step.get("keyword") or step.get("type") or "").strip().upper()

    def _build_scenario_digest(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        steps = scenario.get("steps") or []
        flow: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            keyword = self._normalize_step_keyword(step)
            content = str(step.get("content", "") or "").strip()
            if keyword and content:
                flow.append(f"{keyword}: {content}")
            elif content:
                flow.append(content)
        return {
            "scenario_id": scenario.get("scenario_id") or scenario.get("id", ""),
            "name": scenario.get("name", ""),
            "flow": flow,
        }

    def _build_visual_digest(self, visual_reference: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payload: list[Dict[str, Any]] = []
        for item in visual_reference:
            payload.append(
                {
                    "image_path": item.get("image_path", ""),
                    "analysis": str(item.get("analysis", "") or ""),
                }
            )
        return payload

    @staticmethod
    def _limit_string_list(values: List[Any], limit: int = 6, item_limit: int = 160) -> List[str]:
        items: list[str] = []
        for raw in values[:limit]:
            text = str(raw or "").strip()
            if not text:
                continue
            if len(text) > item_limit:
                text = text[:item_limit].rstrip() + "... [truncated]"
            items.append(text)
        return items

    def _build_acceptance_gate(self, node_id: str, req_data: Dict[str, Any]) -> str:
        scenarios = req_data.get("scenarios") or []
        visual_reference = req_data.get("visual_reference") or []
        gate = {
            "req_id": req_data.get("req_id", node_id),
            "primary_outcomes": [
                "Implement the current node's owned behavior, not just a renderable shell.",
                "Use real owned runtime wiring for fetched or persisted data when this node owns that chain.",
                "If runtime data is not owned here, render explicit loading, empty, or error states instead of fake records.",
            ],
            "scenario_targets": [
                str(item.get("name", "")).strip()
                for item in scenarios
                if str(item.get("name", "")).strip()
            ],
            "visual_rule": (
                "Use visual reference for layout and style only; do not copy screenshot business data."
                if visual_reference
                else "No visual-reference-specific acceptance rule."
            ),
            "forbidden_shortcuts": [
                "hardcoded sample rows",
                "fallback arrays that bypass the owned path",
                "fake success messages detached from real writes",
                "placeholder-only panels presented as complete features",
            ],
        }
        return "<acceptance_gate>\n" + self._compact_json(gate) + "\n</acceptance_gate>"

    @staticmethod
    def _dedupe_records_by_file_path_keep_latest(
        records: List[Dict[str, Any]],
        file_path_key: str = "file_path",
    ) -> List[Dict[str, Any]]:
        deduped_reversed: list[Dict[str, Any]] = []
        seen_paths: set[str] = set()

        for record in reversed(records):
            normalized_path = str(record.get(file_path_key, "") or "").strip().replace("\\", "/")
            if not normalized_path:
                deduped_reversed.append(record)
                continue
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            deduped_reversed.append(record)

        deduped_reversed.reverse()
        return deduped_reversed

    def _build_requirement_focus(self, node_id: str, req_data: Dict[str, Any]) -> str:
        scenarios = req_data.get("scenarios") or []
        visual_reference = req_data.get("visual_reference") or []
        focus = {
            "req_id": req_data.get("req_id", node_id),
            "name": req_data.get("name", ""),
            "description": req_data.get("description", ""),
            "dependencies": req_data.get("dependencies", []),
            "children_ids": req_data.get("children_ids", []),
            "scenario_count": len(scenarios),
            "visual_reference_count": len(visual_reference),
        }

        parts = [
            "<requirement_focus>",
            self._compact_json(focus),
        ]

        if scenarios:
            scenario_payload = [
                self._build_scenario_digest(scenario)
                for scenario in scenarios
            ]
            parts.append("<scenarios>")
            parts.append(self._compact_json(scenario_payload))
            parts.append("</scenarios>")

        if visual_reference:
            visual_payload = self._build_visual_digest(visual_reference)
            parts.append("<visual_reference>")
            parts.append(self._compact_json(visual_payload))
            parts.append("</visual_reference>")

        parts.append("</requirement_focus>")
        return "\n".join(parts)

    def _get_tech_stack_context(self) -> str:
        """
        Layer 1: Global Context (Tech stack and runtime rules).
        Build it directly from the active app type instead of reading metadata files.
        """
        from app_type_handler import get_app_type_handler_class
        from core.utils import get_android_package, get_app_type

        app_type = get_app_type()
        handler_class = get_app_type_handler_class(app_type)
        content = handler_class.build_stack_block()

        if app_type == "android":
            package_name = get_android_package()
            pkg_dir = package_name.replace(".", "/")
            content += (
                "\n\n## Android Package Configuration\n"
                f"- **Package**: `{package_name}`\n"
                f"- **Package directory**: `{pkg_dir}`\n"
                f"- **Main source**: `app/src/main/java/{pkg_dir}/`\n"
                f"- **Unit tests**: `app/src/test/java/{pkg_dir}/unit/` --package `{package_name}.unit`\n"
                f"- **Integration tests**: `app/src/test/java/{pkg_dir}/integration/` --package `{package_name}.integration`\n"
                f"- **E2E tests**: `app/src/test/java/{pkg_dir}/e2e/` --package `{package_name}.e2e`\n"
            )
        return f"<tech_stack_context>\n{content}\n</tech_stack_context>"

    def _get_project_structure(self, agent_type: str = "") -> str:
        """Layer 1.5: Minimal runtime roots only. Do not inject full directory trees."""
        from core.utils import get_app_type, get_web_port
        app_type = get_app_type()
        if app_type == "web":
            lines = [
                "- Web structure rules:",
                f"  - Single runtime port: backend serves frontend dist on port {get_web_port()}",
                "  - Backend runtime root: backend/",
                "  - Frontend source root: frontend/src/",
                "  - Backend source root: backend/src/",
                "  - Shared database scaffold: backend/src/database/",
                "  - Backend Vitest tests: backend/tests/...",
                "  - Frontend Vitest tests: frontend/tests/...",
                "  - Playwright E2E tests: backend/test-e2e/...",
                "  - Database-using tests must allocate an isolated test DB through the scaffold instead of pointing at the default runtime database.db file.",
                "  - Prefer entrypoints, route files, and owner files before broader search.",
            ]
        else:
            lines = [
                "- Android structure rules:",
                "- Main source root: app/src/main/java/...",
                "- Unit tests: app/src/test/java/.../unit/",
                "- Integration tests: app/src/test/java/.../integration/",
                "- E2E tests: app/src/test/java/.../e2e/",
                "- Prefer app entrypoints, activities, fragments, and owner classes before broader search.",
            ]
        return "<project_structure>\n" + "\n".join(lines) + "\n</project_structure>"

    def _get_relational_interfaces(self, node_id: str) -> List[Dict]:
        """
        Fetch interfaces from parent, children, and dependencies to provide integration context.
        """
        node_row = _store().get_requirement(node_id)
        if not node_row:
            return []
            
        target_nodes = set()
        if node_row.get("parent_id"):
            target_nodes.add(node_row["parent_id"])
        target_nodes.update(node_row.get("children_ids") or [])
        target_nodes.update(node_row.get("dependencies") or [])
        
        related_interfaces = []
        for n_id in target_nodes:
            related_interfaces.extend(_store().list_interfaces(req_id=str(n_id or "").strip()))
        unique = {}
        for iface in related_interfaces:
            unique[iface.get("interface_id")] = iface
        return list(unique.values())[:self.max_related_interfaces]

    def _get_source_code_for_interfaces(self, node_id: str, max_files: int | None = None,
                                         max_chars_per_file: int = 2000, total_budget: int = 15000) -> str:
        """Build candidate source file cards instead of injecting full file contents."""
        interfaces = _store().list_interfaces(req_id=node_id)
        if not interfaces:
            return ""
        interfaces = self._dedupe_records_by_file_path_keep_latest(interfaces)

        cards: list[Dict[str, Any]] = []
        selected_interfaces = interfaces if max_files is None else interfaces[:max_files]
        for iface in selected_interfaces:
            fp = str(iface.get('file_path', '') or '').strip()
            if not fp:
                continue
            content_obj: Dict[str, Any] = {}
            try:
                raw_content = iface.get("content", "")
                if raw_content:
                    content_obj = json.loads(raw_content)
            except Exception:
                content_obj = {}
            cards.append(
                {
                    "file_path": fp,
                    "first_line": str(iface.get("first_line", "") or "").strip(),
                    "interface_id": str(iface.get("interface_id", "") or "").strip(),
                    "type": str(iface.get("type", "") or "").strip(),
                    "implemented": bool(iface.get("implemented")),
                    "responsibility": self._truncate_text(content_obj.get("responsibility", ""), 180),
                    "specification": self._truncate_text(content_obj.get("specification", ""), 220),
                    "test_focus": self._limit_string_list(content_obj.get("test_focus") or [], limit=4, item_limit=120),
                    "callers": self._limit_string_list(content_obj.get("callers") or [], limit=3, item_limit=80),
                    "callees": self._limit_string_list(content_obj.get("callees") or [], limit=3, item_limit=80),
                    "why_relevant": "Current-node owned interface file.",
                }
            )

        if not cards:
            return ""
        return "<source_file_cards>\n" + self._compact_json(cards) + "\n</source_file_cards>"

    def _get_node_session_layers(self, node_id: str) -> str:
        from core.utils import load_node_session

        session = load_node_session(node_id)
        if not session:
            return ""

        sections: list[str] = []
        interfaces = session.get("interfaces")
        if interfaces:
            sections.append(
                "<interfaces>\n"
                + self._compact_json(interfaces)
                + "\n</interfaces>"
            )

        return "\n".join(sections)

    def _get_recent_failure_summary(self, node_id: str) -> str:
        from core.utils import load_node_session

        session = load_node_session(node_id)
        if not session:
            return ""
        summary = str(session.get("recent_failure_summary", "") or "").strip()
        if not summary:
            handoff = session.get("tdd_handoff") or {}
            summary = str(handoff.get("last_failed_output_summary", "") or "").strip()
        if not summary:
            return ""
        return "<recent_failure_summary>\n" + summary + "\n</recent_failure_summary>"

    def _get_test_code_for_node(
        self,
        node_id: str,
        max_files: int | None = None,
        max_chars_per_file: int = 2000,
        total_budget: int = 12000,
        target_test_files: list[str] | None = None,
    ) -> str:
        """Build candidate test file cards instead of injecting full test contents."""
        tests = [item for item in _store().list_tests(req_id=node_id) if item is not None]
        if not tests:
            return ""
        tests = self._dedupe_records_by_file_path_keep_latest(tests)

        normalized_targets = {
            str(path or "").strip().replace("\\", "/")
            for path in (target_test_files or [])
            if str(path or "").strip()
        }
        if normalized_targets:
            tests = [
                test for test in tests
                if str(test.get("file_path", "")).strip().replace("\\", "/") in normalized_targets
            ]
            if not tests:
                return ""

        cards: list[Dict[str, Any]] = []
        selected_tests = tests if max_files is None else tests[:max_files]
        for t in selected_tests:
            fp = str(t.get('file_path', '') or '').strip()
            if not fp:
                continue
            cards.append(
                {
                    "file_path": fp,
                    "first_line": str(t.get("first_line", "") or "").strip(),
                    "type": str(t.get("type", "") or "").strip(),
                    "interface_ids": self._limit_string_list(t.get("interface_ids") or [], limit=6, item_limit=80),
                    "passed": t.get("passed"),
                    "why_relevant": "Current-node generated test artifact.",
                }
            )

        if not cards:
            return ""
        return "<test_file_cards>\n" + self._compact_json(cards) + "\n</test_file_cards>"

    def _format_interface(self, iface: Dict, agent_type: str) -> str:
        """Format interface data differently based on agent focus."""
        res = f"- [{iface['interface_id']}] Type: {iface['type']}\n"
        if iface['file_path']:
            res += f"  File: `{iface['file_path']}`\n"
        if iface['first_line']:
            res += f"  Signature: `{iface['first_line']}`\n"
            
        try:
            content = json.loads(iface['content'])
            if agent_type == "InterfaceDesigner":
                # Designer cares about what it does and what it connects to
                res += f"  Desc: {content.get('description', '')}\n"
                res += f"  Inputs: {content.get('inputs', [])}\n"
                res += f"  Outputs: {content.get('outputs', [])}\n"
            elif agent_type == "TestDrivenDeveloper":
                # TDD needs full contract: inputs, outputs, callers, callees
                res += f"  Status: {'Implemented' if iface['implemented'] else 'Stubbed'}\n"
                res += f"  Desc: {content.get('description', '')}\n"
                res += f"  Inputs: {content.get('inputs', [])}\n"
                res += f"  Outputs: {content.get('outputs', [])}\n"
                res += f"  Callers: {content.get('callers', [])}\n"
                res += f"  Callees: {content.get('callees', [])}\n"
            elif agent_type == "TestGenerator":
                res += f"  Desc: {content.get('description', '')}\n"
                res += f"  Inputs: {content.get('inputs', [])}\n"
                res += f"  Outputs: {content.get('outputs', [])}\n"
        except:
            pass
        return res

    def build_agent_context(
        self,
        node_id: str,
        agent_type: str,
        preloaded_source: str = None,
        target_test_files: list[str] | None = None,
    ) -> str:
        """
        Layer 2: Local/Task Context.
        Prefetches the exact data needed for the current node based on the agent's role.
        Uses NodeContextCache to avoid redundant I/O across phases.
        If preloaded_source is provided, uses it instead of re-reading source files from disk.
        """
        req_data = _store().get_requirement(node_id)
        if not req_data:
            return f"<error>Requirement node {node_id} not found in database.</error>"

        context_parts = []

        # Keep the active requirement payload ahead of generic project context.
        requirement_focus = self._build_requirement_focus(node_id, req_data)
        if requirement_focus:
            context_parts.append(requirement_focus)
        context_parts.append(self._build_acceptance_gate(node_id, req_data))

        # 1. Inject Global Context (cached globally — never changes)
        context_parts.append(
            self.cache.get_or_compute(node_id, "tech_stack_context", self._get_tech_stack_context)
        )

        # 1.5 Inject Project Structure (cached per node, invalidated after writes)
        project_structure = self.cache.get_or_compute(
            node_id, f"project_structure::{agent_type}", lambda: self._get_project_structure(agent_type)
        )
        if project_structure:
            context_parts.append(project_structure)

        node_session_layers = self.cache.get_or_compute(
            node_id,
            "node_session",
            lambda: self._get_node_session_layers(node_id),
        )
        if node_session_layers:
            context_parts.append(node_session_layers)

        # 4. Role-Specific Prefetching
        if agent_type == "InterfaceDesigner":
            # Use preloaded_source if available, otherwise cache the disk read
            if preloaded_source:
                context_parts.append(preloaded_source)
            else:
                source_code = self.cache.get_or_compute(
                    node_id, "source_code",
                    lambda: self._get_source_code_for_interfaces(node_id)
                )
                if source_code:
                    context_parts.append(source_code)
        elif agent_type in {"TestDrivenDeveloper", "TestFailureVerifier"}:
            # TDD needs both source code AND test code pre-injected with larger budgets
            if preloaded_source:
                context_parts.append(preloaded_source)
            else:
                source_code = self.cache.get_or_compute(
                    node_id, "source_code",
                    lambda: self._get_source_code_for_interfaces(node_id)
                )
                if source_code:
                    context_parts.append(source_code)
            if target_test_files:
                normalized_targets = sorted(
                    {
                        str(path or "").strip().replace("\\", "/")
                        for path in target_test_files
                        if str(path or "").strip()
                    }
                )
                test_cache_key = f"test_code::{json.dumps(normalized_targets, ensure_ascii=False)}"
                test_code = self.cache.get_or_compute(
                    node_id,
                    test_cache_key,
                    lambda: self._get_test_code_for_node(
                        node_id,
                        target_test_files=normalized_targets,
                    ),
                )
            else:
                test_code = self.cache.get_or_compute(
                    node_id, "test_code",
                    lambda: self._get_test_code_for_node(node_id)
                )
            if test_code:
                context_parts.append(test_code)

        elif agent_type == "TestGenerator":
            # Use preloaded_source if available, otherwise cache the disk read
            if preloaded_source:
                context_parts.append(preloaded_source)
            else:
                source_code = self.cache.get_or_compute(
                    node_id, "source_code",
                    lambda: self._get_source_code_for_interfaces(node_id)
                )
                if source_code:
                    context_parts.append(source_code)
        recent_failure_summary = self.cache.get_or_compute(
            node_id,
            "recent_failure_summary",
            lambda: self._get_recent_failure_summary(node_id),
        )
        if recent_failure_summary:
            context_parts.append(recent_failure_summary)
        return "\n\n".join(context_parts)

    def build_agent_context_split(
        self,
        node_id: str,
        agent_type: str,
        preloaded_source: str = None,
        target_test_files: list[str] | None = None,
    ) -> tuple:
        """Split context into (static_context, dynamic_context).
        Static context goes into the system prompt (sent once, rarely changes).
        Dynamic context goes into the user prompt (changes per phase/iteration).
        This reduces per-step token cost since the user prompt is smaller.
        """
        full_context = self.build_agent_context(
            node_id,
            agent_type,
            preloaded_source,
            target_test_files=target_test_files,
        )
        static = self.get_static_context(node_id, agent_type)
        dynamic = full_context

        # Remove static layers while preserving the leading requirement payload.
        # The dynamic user prompt must still start with the current node context
        # (`<requirement_focus>`, scenarios, visual refs, contracts).
        if static:
            static_parts = [part.strip() for part in static.split("\n\n") if part.strip()]
            for part in static_parts:
                dynamic = dynamic.replace(part, "", 1)
            dynamic = re.sub(r"\n{3,}", "\n\n", dynamic).strip()
        else:
            static = ""
        return static, dynamic

    def get_static_context(self, node_id: str, agent_type: str = "") -> str:
        """Return only the static context layers (for system prompt injection).
        These layers rarely change and can be moved to the system prompt
        to reduce per-step token cost.
        """
        parts = []
        global_ctx = self.cache.get_or_compute(node_id, "tech_stack_context", self._get_tech_stack_context)
        parts.append(global_ctx)
        proj_struct = self.cache.get_or_compute(
            node_id,
            f"project_structure::{agent_type}",
            lambda: self._get_project_structure(agent_type),
        )
        if proj_struct:
            parts.append(proj_struct)
        return "\n\n".join(parts)

    def build_incremental_context(self, node_id: str, modified_files: list = None) -> str:
        """For TDD iterations after the first, only inject changed files.
        Returns a compact delta instead of full context.
        """
        if not modified_files:
            return ""
        from core.utils import get_abs_path
        lines = []
        total = 0
        for fp in modified_files:
            abs_path = get_abs_path(fp)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                if len(content) > 3000:
                    content = content[:3000] + "\n// ... [truncated]"
                lines.append(f"// === {fp} (modified) ===\n{content}")
                total += len(content)
                if total > 15000:
                    break
            except Exception:
                continue
        if not lines:
            return ""
        return "<modified_files>\n" + "\n\n".join(lines) + "\n</modified_files>"

    def prewarm(self, node_id: str):
        """Eagerly populate global cache layers so subsequent calls get cache hits."""
        self.cache.get_or_compute(node_id, "tech_stack_context", self._get_tech_stack_context)
        self.cache.get_or_compute(node_id, "project_structure::", lambda: self._get_project_structure(""))

# Singleton instance
context_pipeline = ContextPipeline()
