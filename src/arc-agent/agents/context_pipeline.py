import os
import re
import json
import sqlite3
from typing import Dict, Any, List
import traceability.database as db_module
from traceability.database import (
    get_requirement_by_id,
    get_interfaces_by_req_id,
    get_tests_by_req_id,
)

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
        self.max_global_chars = 8000
        self.max_interfaces = 20
        self.max_related_interfaces = 30
        self.max_tests = 20
        self.cache = NodeContextCache()

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
            json.dumps(focus, indent=2, ensure_ascii=False),
        ]

        if scenarios:
            scenario_payload = []
            for scenario in scenarios[:8]:
                scenario_payload.append(
                    {
                        "scenario_id": scenario.get("scenario_id") or scenario.get("id", ""),
                        "name": scenario.get("name", ""),
                        "steps": scenario.get("steps", []),
                    }
                )
            parts.append("<scenarios>")
            parts.append(json.dumps(scenario_payload, indent=2, ensure_ascii=False))
            parts.append("</scenarios>")

        if visual_reference:
            visual_payload = []
            for item in visual_reference[:3]:
                visual_payload.append(
                    {
                        "image_path": item.get("image_path", ""),
                        "analysis": str(item.get("analysis", "") or ""),
                    }
                )
            parts.append("<visual_reference>")
            parts.append(json.dumps(visual_payload, indent=2, ensure_ascii=False))
            parts.append("</visual_reference>")

        parts.append("</requirement_focus>")
        return "\n".join(parts)

    def _get_tech_stack_context(self) -> str:
        """
        Layer 1: Global Context (Tech stack and runtime rules).
        Build it directly from the active app type instead of reading metadata files.
        """
        from app_types import get_app_type_handler_class
        from utils import get_android_package, get_app_type

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

        if len(content) > self.max_global_chars:
            content = content[:self.max_global_chars] + "\n...[global context truncated]"
        return f"<tech_stack_context>\n{content}\n</tech_stack_context>"

    def _get_project_structure(self, agent_type: str = "") -> str:
        """Layer 1.5: Relevant source and test roots (pre-fetched so LLM doesn't need list_directory)."""
        from utils import get_abs_path
        from utils import get_app_type, get_web_port
        import os as _os

        root = get_abs_path(".")

        max_depth = 4
        lines = []
        skip_dirs = {
            ".git", ".arc", ".gradle", "build", ".idea",
            "node_modules", ".venv", "venv", "dist", "out", "coverage",
            "__pycache__", "target",
        }
        relevant_roots = []
        app_type = get_app_type()
        web_test_roots = {"backend/tests", "frontend/tests", "backend/test-e2e"}

        def _collect_relevant_roots(path, depth):
            if depth > max_depth:
                return
            try:
                items = sorted(_os.listdir(path))
            except (PermissionError, OSError):
                return
            for item in items:
                if item in skip_dirs:
                    continue
                full = _os.path.join(path, item)
                if not _os.path.isdir(full):
                    continue
                rel = _os.path.relpath(full, root).replace("\\", "/")
                should_include = item == "src"
                if app_type == "web" and rel in web_test_roots:
                    should_include = True
                if should_include:
                    if agent_type == "TestGenerator" and app_type == "web":
                        if rel not in {"backend/src", "frontend/src", "backend/tests", "frontend/tests", "backend/test-e2e"}:
                            continue
                    relevant_roots.append(full)
                    continue
                _collect_relevant_roots(full, depth + 1)

        def _traverse(path, depth, prefix=""):
            if depth > max_depth:
                return
            try:
                items = sorted(_os.listdir(path))
            except (PermissionError, OSError):
                return
            for item in items:
                if item in skip_dirs:
                    continue
                full = _os.path.join(path, item)
                rel = f"{prefix}{item}"
                if _os.path.isdir(full):
                    lines.append(f"- {rel}/")
                    _traverse(full, depth + 1, f"{rel}/")
                else:
                    lines.append(f"- {rel}")

        _collect_relevant_roots(root, 1)
        if not relevant_roots:
            return "<project_structure>\nNo relevant source or test directories found.\n</project_structure>"

        guidance_lines = []
        if app_type == "web":
            guidance_lines = [
                "- Web structure rules:",
                f"  - Single runtime port: backend serves frontend dist on port {get_web_port()}",
                "  - Backend runtime root: backend/",
                "  - Frontend source root: frontend/src/",
                "  - Prefer TypeScript/TSX for frontend source files when creating or extending pages/components/hooks/api modules",
                "  - Tailwind CSS is available and should be used directly in component markup for page/component styling",
                "  - Backend Vitest tests: backend/tests/...",
                "  - Frontend Vitest tests: frontend/tests/...",
                "  - Playwright E2E tests: backend/test-e2e/...",
                "  - Prefer existing backend/frontend entrypoints before probing new directories",
            ]

        for src_root in sorted(relevant_roots):
            rel_root = _os.path.relpath(src_root, root).replace("\\", "/")
            lines.append(f"- {rel_root}/")
            _traverse(src_root, 1, f"{rel_root}/")

        if not lines:
            return ""
        # Cap at 200 lines to avoid bloating context
        if len(lines) > 200:
            lines = lines[:200]
            lines.append("... [truncated]")
        merged_lines = guidance_lines + lines if guidance_lines else lines
        return "<project_structure>\n" + "\n".join(merged_lines) + "\n</project_structure>"

    def _get_relational_interfaces(self, node_id: str) -> List[Dict]:
        """
        Fetch interfaces from parent, children, and dependencies to provide integration context.
        """
        conn = sqlite3.connect(db_module.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT parent_id, children_ids, dependencies FROM requirements WHERE req_id = ?', (node_id,))
        node_row = cursor.fetchone()
        
        if not node_row:
            conn.close()
            return []
            
        target_nodes = set()
        if node_row['parent_id']:
            target_nodes.add(node_row['parent_id'])
            
        try:
            children = json.loads(node_row['children_ids']) if node_row['children_ids'] else []
            target_nodes.update(children)
        except: pass
        
        try:
            deps = json.loads(node_row['dependencies']) if node_row['dependencies'] else []
            target_nodes.update(deps)
        except: pass
        
        related_interfaces = []
        for n_id in target_nodes:
            # Exact match in JSON array to avoid req_1 matching req_10
            search_term = f'%"{n_id}"%'
            cursor.execute('SELECT * FROM interfaces WHERE req_ids LIKE ?', (search_term,))
            for row in cursor.fetchall():
                related_interfaces.append(dict(row))
                
        conn.close()
        unique = {}
        for iface in related_interfaces:
            unique[iface.get("interface_id")] = iface
        return list(unique.values())[:self.max_related_interfaces]

    def _get_source_code_for_interfaces(self, node_id: str, max_files: int = 10,
                                         max_chars_per_file: int = 2000, total_budget: int = 15000) -> str:
        """Pre-read source files of interfaces for a node.
        Eliminates 50+ read_file calls by the LLM during test generation / implementation."""
        from utils import get_abs_path
        interfaces = get_interfaces_by_req_id(node_id)
        if not interfaces:
            return ""

        lines = []
        total = 0
        for iface in interfaces[:max_files]:
            fp = iface.get('file_path', '')
            if not fp:
                continue
            abs_path = get_abs_path(fp)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                if len(content) > max_chars_per_file:
                    content = content[:max_chars_per_file] + "\n// ... [truncated]"
                lines.append(f"// === {fp} ===\n{content}")
                total += len(content)
                if total > total_budget:
                    break
            except Exception:
                continue

        if not lines:
            return ""
        return "<source_code>\n" + "\n\n".join(lines) + "\n</source_code>"

    def _get_node_session_layers(self, node_id: str) -> str:
        from utils import load_node_session

        session = load_node_session(node_id)
        if not session:
            return ""

        sections: list[str] = []
        interfaces = session.get("materialized_interfaces") or session.get("interfaces")
        if interfaces:
            sections.append(
                "<interfaces>\n"
                + json.dumps(interfaces, indent=2, ensure_ascii=False)
                + "\n</interfaces>"
            )

        test_plan = session.get("test_plan")
        if test_plan:
            sections.append(
                "<test_plan>\n"
                + json.dumps(test_plan, indent=2, ensure_ascii=False)
                + "\n</test_plan>"
            )

        tdd_handoff = session.get("tdd_handoff")
        if tdd_handoff:
            sections.append(
                "<tdd_handoff>\n"
                + json.dumps(tdd_handoff, indent=2, ensure_ascii=False)
                + "\n</tdd_handoff>"
            )

        return "\n".join(sections)

    def _get_test_code_for_node(
        self,
        node_id: str,
        max_files: int = 8,
        max_chars_per_file: int = 2000,
        total_budget: int = 12000,
        target_test_files: list[str] | None = None,
    ) -> str:
        """Pre-read test files for a node so TDD agent doesn't need read_file calls."""
        from utils import get_abs_path
        tests = get_tests_by_req_id(node_id)
        if not tests:
            return ""

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

        lines = []
        total = 0
        for t in tests[:max_files]:
            fp = t.get('file_path', '')
            if not fp:
                continue
            abs_path = get_abs_path(fp)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                if len(content) > max_chars_per_file:
                    content = content[:max_chars_per_file] + "\n// ... [truncated]"
                lines.append(f"// === {fp} ===\n{content}")
                total += len(content)
                if total > total_budget:
                    break
            except Exception:
                continue

        if not lines:
            return ""
        return "<test_code>\n" + "\n\n".join(lines) + "\n</test_code>"

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
        req_data = get_requirement_by_id(node_id)
        if not req_data:
            return f"<error>Requirement node {node_id} not found in database.</error>"

        context_parts = []

        # Keep the active requirement payload ahead of generic project context.
        requirement_focus = self._build_requirement_focus(node_id, req_data)
        if requirement_focus:
            context_parts.append(requirement_focus)

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
                    lambda: self._get_source_code_for_interfaces(node_id, max_files=15, total_budget=20000)
                )
                if source_code:
                    context_parts.append(source_code)
        elif agent_type == "TestDrivenDeveloper":
            # TDD needs both source code AND test code pre-injected with larger budgets
            if preloaded_source:
                context_parts.append(preloaded_source)
            else:
                source_code = self.cache.get_or_compute(
                    node_id, "source_code",
                    lambda: self._get_source_code_for_interfaces(
                        node_id, max_files=15, max_chars_per_file=3000, total_budget=25000
                    )
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
                        max_files=10,
                        max_chars_per_file=3000,
                        total_budget=15000,
                        target_test_files=normalized_targets,
                    ),
                )
            else:
                test_code = self.cache.get_or_compute(
                    node_id, "test_code",
                    lambda: self._get_test_code_for_node(
                        node_id, max_files=10, max_chars_per_file=3000, total_budget=15000
                    )
                )
            if test_code:
                context_parts.append(test_code)
            # TDD also needs to know about existing tests (metadata)
            tests = get_tests_by_req_id(node_id)
            if target_test_files:
                normalized_targets = {
                    str(path or "").strip().replace("\\", "/")
                    for path in target_test_files
                    if str(path or "").strip()
                }
                tests = [
                    test for test in tests
                    if str(test.get("file_path", "")).strip().replace("\\", "/") in normalized_targets
                ]
            tests = tests[:self.max_tests]
            if tests:
                tests_str = ""
                for t in tests:
                    tests_str += f"- [{t['test_id']}] Type: {t['type']} Path: `{t['file_path']}`\n"
                context_parts.append(f"<existing_tests>\n{tests_str}\n</existing_tests>")

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
        from utils import get_abs_path
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
