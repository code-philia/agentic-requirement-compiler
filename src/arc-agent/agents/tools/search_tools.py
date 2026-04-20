import os
import re
import sqlite3
import json
import aiofiles
from utils import get_abs_path
from traceability.database import DB_PATH

async def grep_search_impl(pattern: str, dir_path: str = ".") -> str:
    """Search for a regex pattern in the contents of files within a directory"""
    abs_dir = get_abs_path(dir_path)
    results = []
    max_results = 300
    try:
        # compile regex pattern
        regex = re.compile(pattern)
        
        for root, _, files in os.walk(abs_dir):
            for file in files:
                if file.endswith(('.py', '.js', '.ts', '.yaml', '.md', '.jsx', '.tsx')): 
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            for i, line in enumerate(f):
                                if regex.search(line):
                                    results.append(f"{file_path}:{i+1}: {line.strip()}")
                                    if len(results) >= max_results:
                                        return "\n".join(results) + "\n... [grep results truncated]"
                    except Exception:
                        pass
        return "\n".join(results) if results else "No matches found."
    except Exception as e:
        return f"Grep search error: {str(e)}"

async def get_node_relations_impl(node_id: str) -> str:
    """
    Get the parent and children nodes for a given requirement node, along with their designed interfaces.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get the target node to find its parent and children IDs
        cursor.execute('SELECT parent_id, children_ids FROM requirements WHERE req_id = ?', (node_id,))
        node_row = cursor.fetchone()
        
        if not node_row:
            conn.close()
            return f"Requirement node '{node_id}' not found in database."
            
        parent_id = node_row['parent_id']
        try:
            children_ids = json.loads(node_row['children_ids']) if node_row['children_ids'] else []
        except:
            children_ids = []
            
        result = f"### Relational Context for Node [{node_id}]\n\n"
        
        # Helper to fetch node details + interfaces
        def fetch_node_details(n_id, label):
            cursor.execute('SELECT description FROM requirements WHERE req_id = ?', (n_id,))
            r_row = cursor.fetchone()
            if not r_row: return ""
            
            res = f"#### {label}: [{n_id}]\n"
            res += f"Description: {r_row['description'][:200]}...\n"
            
            search_term = f'%"{n_id}"%'
            cursor.execute('SELECT interface_id, type, file_path, first_line FROM interfaces WHERE req_ids LIKE ?', (search_term,))
            ifaces = cursor.fetchall()
            
            if ifaces:
                res += "Interfaces:\n"
                for iface in ifaces:
                    res += f"  - ID: {iface['interface_id']} (Type: {iface['type']})\n"
                    if iface['file_path']:
                        res += f"    Path: `{iface['file_path']}`\n"
                    if iface['first_line']:
                        res += f"    Signature: `{iface['first_line']}`\n"
            else:
                res += "No interfaces designed yet for this node.\n"
            return res + "\n"

        # Fetch Parent
        if parent_id:
            result += fetch_node_details(parent_id, "Parent Node")
        else:
            result += "This is a root node (No parent).\n\n"
            
        # Fetch Children
        if children_ids:
            result += f"#### Children Nodes ({len(children_ids)} total):\n"
            for child_id in children_ids:
                result += fetch_node_details(child_id, "Child Node")
        else:
            result += "This node has no children.\n"
            
        conn.close()
        return result
        
    except Exception as e:
        return f"Database retrieval error: {str(e)}"

async def find_interface_impacts_impl(interface_id: str) -> str:
    """
    Find all interfaces that call the given interface_id (static analysis via traceability DB).
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # We need to find interfaces where 'callees' JSON array contains interface_id
        # Simple LIKE query works since interface_id is unique
        search_term = f'%"{interface_id}"%'
        cursor.execute('SELECT interface_id, req_ids, type, file_path, first_line FROM interfaces WHERE callees LIKE ?', (search_term,))
        rows = cursor.fetchall()
        
        conn.close()
        
        if not rows:
            return f"No interfaces found that call '{interface_id}'. It is safe to modify."
            
        result = f"### Impact Analysis for Interface [{interface_id}]\n"
        result += "The following interfaces call this interface and might be affected by your changes:\n\n"
        
        for row in rows:
            result += f"- **ID**: {row['interface_id']} (Type: {row['type']})\n"
            if row['file_path']:
                result += f"  - Path: `{row['file_path']}`\n"
            if row['first_line']:
                result += f"  - Signature: `{row['first_line']}`\n"
            result += f"  - Used in Req IDs: {row['req_ids']}\n\n"
            
        return result
        
    except Exception as e:
        return f"Database retrieval error: {str(e)}"

async def search_interfaces_by_keyword_impl(keyword: str, limit: int = 10) -> str:
    """
    Search for interfaces by keyword in their name or description.
    Useful for finding reusable functionality like 'auth', 'database', 'user'.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        search_term = f"%{keyword}%"
        
        # content contains name and description in JSON
        cursor.execute('''
        SELECT interface_id, req_ids, type, file_path, first_line, content FROM interfaces 
        WHERE interface_id LIKE ? OR content LIKE ? LIMIT ?
        ''', (search_term, search_term, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return f"No interfaces found matching keyword: '{keyword}'"
            
        result = f"### Interfaces matching '{keyword}'\n\n"
        for row in rows:
            result += f"- **ID**: `{row['interface_id']}` (Type: {row['type']})\n"
            if row['file_path']:
                result += f"  - Path: `{row['file_path']}`\n"
            if row['first_line']:
                result += f"  - Signature: `{row['first_line']}`\n"
            try:
                content = json.loads(row['content'])
                if 'name' in content and content['name']:
                    result += f"  - Name: {content['name']}\n"
                if 'description' in content and content['description']:
                    result += f"  - Description: {content['description']}\n"
            except:
                pass
            result += f"  - Used in Req IDs: {row['req_ids']}\n\n"
            
        return result
        
    except Exception as e:
        return f"Database search error: {str(e)}"

async def search_interfaces_by_relation_impl(node_id: str, relation_type: str = "all") -> str:
    """
    Find interfaces belonging to related requirement nodes (parent, children, siblings, dependencies).
    relation_type can be: 'parent', 'children', 'siblings', 'dependencies', 'all'
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 1. Get the current node's relations
        cursor.execute('SELECT parent_id, children_ids, dependencies FROM requirements WHERE req_id = ?', (node_id,))
        node_row = cursor.fetchone()
        
        if not node_row:
            conn.close()
            return f"Requirement node '{node_id}' not found."
            
        parent_id = node_row['parent_id']
        try:
            children_ids = json.loads(node_row['children_ids']) if node_row['children_ids'] else []
        except:
            children_ids = []
            
        try:
            dependencies = json.loads(node_row['dependencies']) if node_row['dependencies'] else []
        except:
            dependencies = []
            
        siblings = []
        if parent_id:
            cursor.execute('SELECT children_ids FROM requirements WHERE req_id = ?', (parent_id,))
            p_row = cursor.fetchone()
            if p_row:
                try:
                    p_children = json.loads(p_row['children_ids']) if p_row['children_ids'] else []
                    siblings = [c for c in p_children if c != node_id]
                except:
                    pass

        target_nodes = set()
        if relation_type in ["parent", "all"] and parent_id:
            target_nodes.add(parent_id)
        if relation_type in ["children", "all"]:
            target_nodes.update(children_ids)
        if relation_type in ["siblings", "all"]:
            target_nodes.update(siblings)
        if relation_type in ["dependencies", "all"]:
            target_nodes.update(dependencies)
            
        if not target_nodes:
            conn.close()
            return f"No related nodes found for relation type: {relation_type}"

        # 2. Fetch interfaces for these nodes
        result = f"### Interfaces in Related Nodes (Relation: {relation_type})\n\n"
        found_any = False
        
        for n_id in target_nodes:
            search_term = f'%"{n_id}"%'
            cursor.execute('SELECT interface_id, type, file_path, first_line, content FROM interfaces WHERE req_ids LIKE ?', (search_term,))
            ifaces = cursor.fetchall()
            
            if ifaces:
                found_any = True
                result += f"#### From Node [{n_id}]:\n"
                for row in ifaces:
                    result += f"- **ID**: `{row['interface_id']}` (Type: {row['type']})\n"
                    if row['file_path']:
                        result += f"  - Path: `{row['file_path']}`\n"
                    if row['first_line']:
                        result += f"  - Signature: `{row['first_line']}`\n"
                    try:
                        content = json.loads(row['content'])
                        if 'description' in content and content['description']:
                            result += f"  - Description: {content['description']}\n"
                    except:
                        pass
                result += "\n"
                
        conn.close()
        
        if not found_any:
            return f"Related nodes found, but they do not have any designed interfaces yet."
            
        return result
        
    except Exception as e:
        return f"Database relation search error: {str(e)}"
