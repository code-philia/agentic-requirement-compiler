import os
import json
import sqlite3
from typing import Dict, Any, List
from traceability.database import (
    DB_PATH, 
    get_requirement_by_id, 
    get_interfaces_by_req_id, 
    get_tests_by_req_id
)

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

    def _get_global_context(self) -> str:
        """
        Layer 1: Global Context (Tech stack, project rules, directory structure).
        Reads from a standard file like `.arc_context.md` if it exists.
        """
        context_file = os.path.join(self.workspace_dir, ".arc", "metadata.md")
        if os.path.exists(context_file):
            with open(context_file, "r", encoding="utf-8") as f:
                content = f.read()
                if len(content) > self.max_global_chars:
                    content = content[:self.max_global_chars] + "\n...[global context truncated]"
                return f"<global_context>\n{content}\n</global_context>"
        return "<global_context>\nNo global context file (.arc/metadata.md) found.\n</global_context>"

    def _get_relational_interfaces(self, node_id: str) -> List[Dict]:
        """
        Fetch interfaces from parent, children, and dependencies to provide integration context.
        """
        conn = sqlite3.connect(DB_PATH)
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
                # TDD cares about implementation status and exact inputs
                res += f"  Status: {'Implemented' if iface['implemented'] else 'Stubbed'}\n"
                res += f"  Inputs: {content.get('inputs', [])}\n"
            elif agent_type == "TestGenerator":
                res += f"  Desc: {content.get('description', '')}\n"
                res += f"  Inputs: {content.get('inputs', [])}\n"
                res += f"  Outputs: {content.get('outputs', [])}\n"
        except:
            pass
        return res

    def build_agent_context(self, node_id: str, agent_type: str) -> str:
        """
        Layer 2: Local/Task Context.
        Prefetches the exact data needed for the current node based on the agent's role.
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
        
        # 1. Inject Global Context
        context_parts.append(self._get_global_context())
        
        # 2. Current Node Data
        req_json = json.dumps(req_data, indent=2, ensure_ascii=False)
        if len(req_json) > self.max_requirement_chars:
            req_json = req_json[:self.max_requirement_chars] + "\n...[requirement truncated]"
        context_parts.append(f"<current_requirement id=\"{node_id}\">\n{req_json}\n</current_requirement>")
        
        # 3. Existing Interfaces for this node
        own_interfaces = get_interfaces_by_req_id(node_id)[:self.max_interfaces]
        if own_interfaces:
            ifaces_str = "\n".join([self._format_interface(i, agent_type) for i in own_interfaces])
            context_parts.append(f"<own_interfaces>\n{ifaces_str}\n</own_interfaces>")
            
        # 4. Role-Specific Prefetching
        if agent_type == "InterfaceDesigner":
            # Designer needs to know about surrounding architecture to reuse interfaces
            related_ifaces = self._get_relational_interfaces(node_id)
            if related_ifaces:
                rel_str = "\n".join([self._format_interface(i, agent_type) for i in related_ifaces])
                context_parts.append(f"<related_architecture_interfaces>\n{rel_str}\n</related_architecture_interfaces>")
                
        elif agent_type == "TestDrivenDeveloper":
            # TDD needs to know about existing tests
            tests = get_tests_by_req_id(node_id)[:self.max_tests]
            if tests:
                tests_str = ""
                for t in tests:
                    tests_str += f"- [{t['test_id']}] Type: {t['type']} Path: `{t['file_path']}`\n"
                context_parts.append(f"<existing_tests>\n{tests_str}\n</existing_tests>")
                
        elif agent_type == "TestGenerator":
            # Test Generator needs to know what interfaces exist and what architecture it connects to
            related_ifaces = self._get_relational_interfaces(node_id)
            if related_ifaces:
                rel_str = "\n".join([self._format_interface(i, agent_type) for i in related_ifaces])
                context_parts.append(f"<related_architecture_interfaces>\n{rel_str}\n</related_architecture_interfaces>")

        return "\n\n".join(context_parts)

# Singleton instance
context_pipeline = ContextPipeline()
