import json
import os
import yaml
import shutil
import asyncio
from typing import List, Dict, Set, Callable, Awaitable

def load_requirements(file_path: str):
    if not os.path.exists(file_path):
        print(f"Requirements file not found: {file_path}")
        return None
    with open(file_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def get_all_leaves(node: dict) -> Dict[str, dict]:
    """Recursively find all leaf nodes (nodes without children)"""
    leaves = {}
    
    # Check if current node is a leaf (has no children or empty children)
    children = node.get('children', [])
    if not children:
        # It's a leaf (but ignore ROOT if it has no children, though usually ROOT has children)
        if node.get('id') != 'ROOT':
            leaves[node.get('id')] = node
        return leaves

    # If it has children, recurse
    for child in children:
        leaves.update(get_all_leaves(child))
    
    return leaves

def build_dependency_graph(leaves: Dict[str, dict]):
    """Build adjacency list and in-degree for topological sort"""
    adj = {node_id: [] for node_id in leaves}
    in_degree = {node_id: 0 for node_id in leaves}
    
    for node_id, node in leaves.items():
        deps = node.get('dependencies', [])
        for dep_id in deps:
            # Only consider dependencies that are in our leaf set
            # (If a dependency is a parent node, we might need more complex logic, 
            # but for now assume granular dependencies)
            if dep_id in leaves:
                adj[dep_id].append(node_id)
                in_degree[node_id] += 1
            else:
                # Warning: Dependency not found in leaves
                print(f"Warning: Dependency {dep_id} for {node_id} not found in leaves.")
    
    return adj, in_degree

def topological_sort(leaves: Dict[str, dict]) -> List[str]:
    adj, in_degree = build_dependency_graph(leaves)
    queue = [node_id for node_id in leaves if in_degree[node_id] == 0]
    sorted_nodes = []
    
    while queue:
        u = queue.pop(0)
        sorted_nodes.append(u)
        
        for v in adj[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)
    
    if len(sorted_nodes) != len(leaves):
        print("Error: Cycle detected or disconnected graph issues.")
        # Fallback to returning what we have + remaining (undefined order)
        remaining = set(leaves.keys()) - set(sorted_nodes)
        return sorted_nodes + list(remaining)
        
    return sorted_nodes

def update_node_status(file_path: str, node_id: str, status: str):
    """Write node status to status.json"""
    status_file = os.path.join(os.path.dirname(file_path), 'status.json')
    current_status = {}
    
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                current_status = json.load(f)
        except:
            pass
            
    current_status[node_id] = status
    
    with open(status_file, 'w', encoding='utf-8') as f:
        json.dump(current_status, f, indent=2)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'template-fullstack')

async def run_npm_install(target_dir: str, log_cb: Callable[[str], Awaitable[None]]):
    """Run npm install in specified directory asynchronously"""
    try:
        process = await asyncio.create_subprocess_shell(
            "npm install",
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            await log_cb(f"Successfully installed dependencies in {os.path.basename(target_dir)}")
        else:
            await log_cb(f"Warning: npm install failed in {os.path.basename(target_dir)}. Error:\n{stderr.decode('utf-8')}")
            
    except Exception as e:
        await log_cb(f"Error running npm install in {target_dir}: {str(e)}")

async def init_project_workspace(workspace_path: str, broadcast_cb: Callable[[dict], Awaitable[None]] = None) -> bool:
    """Copy template-fullstack to workspace_path and install dependencies"""
    
    async def _log(msg: str):
        if broadcast_cb:
            await broadcast_cb({"type": "log", "agent": "System", "message": msg})

    if not os.path.exists(TEMPLATE_DIR):
        await _log(f"Error: Template directory not found at {TEMPLATE_DIR}")
        return False

    await _log(f"Copying template from {TEMPLATE_DIR} to {workspace_path}...")
    try:
        await asyncio.to_thread(shutil.copytree, TEMPLATE_DIR, workspace_path, dirs_exist_ok=True)
        await _log("Template files copied successfully.")
    except Exception as e:
        await _log(f"Error copying template: {str(e)}")
        return False

    backend_path = os.path.join(workspace_path, 'backend')
    if os.path.exists(backend_path):
        await _log("Installing backend dependencies. This might take a moment...")
        await run_npm_install(backend_path, _log)

    frontend_path = os.path.join(workspace_path, 'frontend')
    if os.path.exists(frontend_path):
        await _log("Installing frontend dependencies. This might take a moment...")
        await run_npm_install(frontend_path, _log)

    await _log("Full-stack workspace initialized completely.")
    return True