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
                                if re.search(pattern, line):
                                    results.append(f"{file_path}:{i+1}: {line.strip()}")
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
            
            cursor.execute('SELECT interface_id, type, file_path, first_line FROM interfaces WHERE req_id = ?', (n_id,))
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

async def retrieve_context_impl(query: str, limit: int = 5) -> str:
    """
    Search the traceability database (requirements and interfaces) to retrieve related context.
    Uses basic SQL LIKE matching for simplicity.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        search_term = f"%{query}%"
        
        # 1. Search Requirements
        cursor.execute('''
        SELECT req_id, description FROM requirements 
        WHERE req_id LIKE ? OR description LIKE ? LIMIT ?
        ''', (search_term, search_term, limit))
        
        req_rows = cursor.fetchall()
        
        # 2. Search Interfaces
        cursor.execute('''
        SELECT interface_id, req_id, type, file_path, first_line, content FROM interfaces 
        WHERE interface_id LIKE ? OR content LIKE ? LIMIT ?
        ''', (search_term, search_term, limit))
        
        iface_rows = cursor.fetchall()
        
        conn.close()
        
        result = f"### Retrieval Results for '{query}'\n\n"
        
        if req_rows:
            result += "#### Related Requirement Nodes:\n"
            for row in req_rows:
                result += f"- **[{row['req_id']}]**: {row['description'][:200]}...\n"
            result += "\n"
            
        if iface_rows:
            result += "#### Related Interfaces:\n"
            for row in iface_rows:
                result += f"- **ID**: {row['interface_id']} (Type: {row['type']})\n"
                if row['file_path']:
                    result += f"  - Path: `{row['file_path']}`\n"
                if row['first_line']:
                    result += f"  - Signature: `{row['first_line']}`\n"
                try:
                    content = json.loads(row['content'])
                    if 'description' in content:
                        result += f"  - Description: {content['description']}\n"
                except:
                    pass
            result += "\n"
            
        if not req_rows and not iface_rows:
            return f"No context found in database for query: '{query}'"
            
        return result
        
    except Exception as e:
        return f"Database retrieval error: {str(e)}"