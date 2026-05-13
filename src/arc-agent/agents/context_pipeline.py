import os
import json
import sqlite3
from typing import Dict, Any, List
import traceability.database as db_module
from traceability.database import (
    get_requirement_by_id,
    get_interfaces_by_req_id,
    get_tests_by_req_id
)

class NodeContextCache:
    """Caches static context layers for a node across agent phases.
    Avoids re-reading files, re-querying DB, and re-traversing the project tree
    when the data hasn't changed between phases.
    """
    # Layers that are truly global (never change during a run)
    GLOBAL_LAYERS = {"global_context", "project_structure"}
    # Layers that change only when files are written (after stub impl, test gen, TDD fix)
    FILE_DEPENDENT_LAYERS = {"source_code", "test_code"}
    # Layers that change when DB is updated (after insert_interface, insert_test)
    DB_DEPENDENT_LAYERS = {"existing_interfaces", "own_interfaces", "related_interfaces"}

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
        for layer in self.FILE_DEPENDENT_LAYERS:
            self._cache.pop((node_id, layer), None)

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
        self.max_requirement_chars = 6000
        self.max_interfaces = 20
        self.max_related_interfaces = 30
        self.max_tests = 20
        self.cache = NodeContextCache()

    def _get_global_context(self) -> str:
        """
        Layer 1: Global Context (Tech stack, project rules, directory structure).
        Reads from a standard file like `.arc_context.md` if it exists.
        """
        from utils import get_abs_path
        context_file = get_abs_path(os.path.join(".arc", "metadata.md"))
        if os.path.exists(context_file):
            with open(context_file, "r", encoding="utf-8") as f:
                content = f.read()
                if len(content) > self.max_global_chars:
                    content = content[:self.max_global_chars] + "\n...[global context truncated]"
                return f"<global_context>\n{content}\n</global_context>"
        return "<global_context>\nNo global context file (.arc/metadata.md) found.\n</global_context>"

    def _get_project_structure(self) -> str:
        """Layer 1.5: Project directory tree (pre-fetched so LLM doesn't need list_directory)."""
        from utils import get_abs_path, WORKSPACE_ROOT
        import os as _os

        root = get_abs_path(".")
        max_depth = 4
        lines = []
        skip_dirs = {".git", ".arc", ".gradle", "build", ".idea"}

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

        _traverse(root, 1)
        if not lines:
            return ""
        # Cap at 200 lines to avoid bloating context
        if len(lines) > 200:
            lines = lines[:200]
            lines.append("... [truncated]")
        return "<project_structure>\n" + "\n".join(lines) + "\n</project_structure>"

    def _get_all_interfaces_summary(self) -> str:
        """Layer 1.6: Summary of ALL existing interfaces (so LLM doesn't need search_interfaces_by_keyword)."""
        conn = sqlite3.connect(db_module.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT interface_id, type, file_path, first_line, content FROM interfaces ORDER BY interface_id')
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return ""

        lines = []
        for row in rows:
            entry = f"- [{row['interface_id']}] Type: {row['type']}"
            if row['file_path']:
                entry += f" File: `{row['file_path']}`"
            if row['first_line']:
                entry += f" Sig: `{row['first_line']}`"
            # Add description from content JSON
            try:
                content = json.loads(row['content'])
                desc = content.get('description', '')
                if desc:
                    entry += f" Desc: {desc[:100]}"
            except Exception:
                pass
            lines.append(entry)

        if len(lines) > 50:
            lines = lines[:50]
            lines.append("... [more interfaces exist, use search_interfaces_by_keyword to find specific ones]")

        return "<existing_interfaces>\n" + "\n".join(lines) + "\n</existing_interfaces>"

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

    def _get_test_code_for_node(self, node_id: str, max_files: int = 8,
                                 max_chars_per_file: int = 2000, total_budget: int = 12000) -> str:
        """Pre-read test files for a node so TDD agent doesn't need read_file calls."""
        from utils import get_abs_path
        tests = get_tests_by_req_id(node_id)
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

    def build_agent_context(self, node_id: str, agent_type: str, preloaded_source: str = None) -> str:
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
        context_parts.append(
            "<context_policy>\n"
            "- Prefer reusing existing interfaces before creating new ones.\n"
            "- If modifying a reused interface, check impacts first.\n"
            "- Prefer minimal file reads and targeted grep over full scans.\n"
            "- Keep outputs deterministic and schema-valid.\n"
            "</context_policy>"
        )

        # 1. Inject Global Context (cached globally — never changes)
        context_parts.append(
            self.cache.get_or_compute(node_id, "global_context", self._get_global_context)
        )

        # 1.5 Inject Project Structure (cached per node, invalidated after writes)
        project_structure = self.cache.get_or_compute(
            node_id, "project_structure", self._get_project_structure
        )
        if project_structure:
            context_parts.append(project_structure)

        # 1.6 Inject All Existing Interfaces Summary (cached per node, invalidated after DB changes)
        all_ifaces = self.cache.get_or_compute(
            node_id, "existing_interfaces", self._get_all_interfaces_summary
        )
        if all_ifaces:
            context_parts.append(all_ifaces)

        # 2. Current Node Data
        req_json = json.dumps(req_data, indent=2, ensure_ascii=False)
        if len(req_json) > self.max_requirement_chars:
            req_json = req_json[:self.max_requirement_chars] + "\n...[requirement truncated]"
        context_parts.append(f"<current_requirement id=\"{node_id}\">\n{req_json}\n</current_requirement>")

        # 3. Existing Interfaces for this node (cached per node, invalidated after DB changes)
        def _compute_own_interfaces():
            own = get_interfaces_by_req_id(node_id)[:self.max_interfaces]
            if own:
                return f"<own_interfaces>\n" + "\n".join([self._format_interface(i, agent_type) for i in own]) + "\n</own_interfaces>"
            return ""

        own_ifaces_str = self.cache.get_or_compute(node_id, "own_interfaces", _compute_own_interfaces)
        if own_ifaces_str:
            context_parts.append(own_ifaces_str)

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
            # Designer needs to know about surrounding architecture to reuse interfaces
            def _compute_related():
                related = self._get_relational_interfaces(node_id)
                if related:
                    return f"<related_architecture_interfaces>\n" + "\n".join([self._format_interface(i, agent_type) for i in related]) + "\n</related_architecture_interfaces>"
                return ""
            related_str = self.cache.get_or_compute(node_id, "related_interfaces", _compute_related)
            if related_str:
                context_parts.append(related_str)

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
            test_code = self.cache.get_or_compute(
                node_id, "test_code",
                lambda: self._get_test_code_for_node(
                    node_id, max_files=10, max_chars_per_file=3000, total_budget=15000
                )
            )
            if test_code:
                context_parts.append(test_code)
            # TDD also needs to know about existing tests (metadata)
            tests = get_tests_by_req_id(node_id)[:self.max_tests]
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
            # Test Generator needs to know what interfaces exist and what architecture it connects to
            def _compute_related():
                related = self._get_relational_interfaces(node_id)
                if related:
                    return f"<related_architecture_interfaces>\n" + "\n".join([self._format_interface(i, agent_type) for i in related]) + "\n</related_architecture_interfaces>"
                return ""
            related_str = self.cache.get_or_compute(node_id, "related_interfaces", _compute_related)
            if related_str:
                context_parts.append(related_str)

        return "\n\n".join(context_parts)

    def build_agent_context_split(self, node_id: str, agent_type: str, preloaded_source: str = None) -> tuple:
        """Split context into (static_context, dynamic_context).
        Static context goes into the system prompt (sent once, rarely changes).
        Dynamic context goes into the user prompt (changes per phase/iteration).
        This reduces per-step token cost since the user prompt is smaller.
        """
        full_context = self.build_agent_context(node_id, agent_type, preloaded_source)
        static = self.get_static_context(node_id)
        # Remove the static portion from the full context to get the dynamic portion
        # The static layers appear at the start of full_context
        if static and static in full_context:
            idx = full_context.index(static)
            # Take everything after the static portion
            dynamic = full_context[idx + len(static):].lstrip("\n")
        else:
            # Fallback: put everything in dynamic (no split)
            dynamic = full_context
            static = ""
        return static, dynamic

    def get_static_context(self, node_id: str) -> str:
        """Return only the static context layers (for system prompt injection).
        These layers rarely change and can be moved to the system prompt
        to reduce per-step token cost.
        """
        parts = []
        # context_policy is static
        parts.append(
            "<context_policy>\n"
            "- Prefer reusing existing interfaces before creating new ones.\n"
            "- If modifying a reused interface, check impacts first.\n"
            "- Prefer minimal file reads and targeted grep over full scans.\n"
            "- Keep outputs deterministic and schema-valid.\n"
            "</context_policy>"
        )
        global_ctx = self.cache.get_or_compute(node_id, "global_context", self._get_global_context)
        parts.append(global_ctx)
        proj_struct = self.cache.get_or_compute(node_id, "project_structure", self._get_project_structure)
        if proj_struct:
            parts.append(proj_struct)
        all_ifaces = self.cache.get_or_compute(node_id, "existing_interfaces", self._get_all_interfaces_summary)
        if all_ifaces:
            parts.append(all_ifaces)
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
        self.cache.get_or_compute(node_id, "global_context", self._get_global_context)
        self.cache.get_or_compute(node_id, "project_structure", self._get_project_structure)

# Singleton instance
context_pipeline = ContextPipeline()
