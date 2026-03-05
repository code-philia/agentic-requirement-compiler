import json
import os
import yaml
from typing import List, Dict, Set

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
