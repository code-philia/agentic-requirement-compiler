import asyncio
import json
import os
import yaml
from typing import List, Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI(title="ARC Multi-Agent Backend")

# Path to requirements.yaml
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Assuming the structure is project/src/arc-agent/main.py
# and project/requirements/requirements.yaml
REQ_FILE_PATH = os.path.abspath(os.path.join(BASE_DIR, '..', '..', 'requirements', 'requirements.yaml'))

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                pass

manager = ConnectionManager()

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

async def simulate_processing(node_id: str):
    """Simulate the 4-step process for a single leaf node"""
    
    current_file = ACTIVE_REQ_FILE
    
    # Step 1: Analyze
    update_node_status(current_file, node_id, "analyzing")
    await manager.broadcast({
        "type": "log", 
        "agent": "RequirementAnalyzer", 
        "message": f"Analyzing requirement node {node_id}...", 
        "nodeId": node_id,
        "status": "analyzing"
    })
    await manager.broadcast({
        "type": "node_update", 
        "nodeId": node_id, 
        "status": "analyzing"
    })
    await asyncio.sleep(1.5)

    # Step 2: Interface Design
    await manager.broadcast({
        "type": "log", 
        "agent": "InterfaceDesigner", 
        "message": f"Designing interfaces (UI/API/DB) for {node_id}...",
        "nodeId": node_id
    })
    await asyncio.sleep(1.5)
    
    # Mark as Designed (Orange)
    update_node_status(current_file, node_id, "designed")
    await manager.broadcast({
        "type": "node_update", 
        "nodeId": node_id, 
        "status": "designed",
        "agent": "InterfaceDesigner",
        "message": f"Interface design completed: {node_id}"
    })
    
    # Step 3: Generate Tests
    await manager.broadcast({
        "type": "log", 
        "agent": "TestGenerator", 
        "message": f"Generating test cases (E2E/Unit) for {node_id}...",
        "nodeId": node_id
    })
    await asyncio.sleep(1.5)

    # Step 4: Implement
    await manager.broadcast({
        "type": "log", 
        "agent": "CodeGenerator", 
        "message": f"Implementing business logic for {node_id}...",
        "nodeId": node_id
    })
    await asyncio.sleep(2.0)

    # Mark as Completed (Green)
    update_node_status(current_file, node_id, "completed")
    await manager.broadcast({
        "type": "node_update", 
        "nodeId": node_id, 
        "status": "completed",
        "agent": "System",
        "message": f"Requirement {node_id} processing completed."
    })

async def simulate_agent_workflow():
    """Simulate the full workflow based on the requirement DAG"""
    
    await manager.broadcast({"type": "log", "agent": "System", "message": "ARC compilation system started..."})
    await asyncio.sleep(1)

    # 1. Load and Parse Requirements
    current_file = ACTIVE_REQ_FILE
    await manager.broadcast({"type": "log", "agent": "RequirementLoader", "message": f"Reading requirements file: {current_file}"})
    try:
        data = load_requirements(current_file)
        if not data:
            await manager.broadcast({"type": "error-event", "agent": "System", "message": "Failed to read requirements file or file is empty."})
            return
    except Exception as e:
        await manager.broadcast({"type": "error-event", "agent": "System", "message": f"Error while reading requirements file: {str(e)}"})
        return

    # 2. Build DAG
    await manager.broadcast({"type": "log", "agent": "DependencyManager", "message": "Building requirement dependency DAG..."})
    await asyncio.sleep(1)
    
    leaves = get_all_leaves(data)
    process_queue = topological_sort(leaves)
    
    await manager.broadcast({"type": "log", "agent": "DependencyManager", "message": f"Calculated processing order: {', '.join(process_queue)}"})
    await asyncio.sleep(1)

    # 3. Process
    for node_id in process_queue:
        await simulate_processing(node_id)
        await asyncio.sleep(0.5)

    await manager.broadcast({"type": "log", "agent": "System", "message": "All requirements processed successfully. Project compiled!"})

@app.websocket("/ws/compiler")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("command") == "start":
                project_path = message.get("project_path")
                
                # Dynamic Path Resolution
                if project_path == "CURRENT_WORKSPACE":
                    # Fallback for dev environment or when path not provided
                    target_file = REQ_FILE_PATH
                else:
                    # Use the provided workspace path
                    # Handle windows paths properly
                    workspace_root = project_path
                    target_file = os.path.join(workspace_root, 'requirements', 'requirements.yaml')
                
                # Update global or pass to function
                # For this simple script, we can just pass it or set a global (less ideal but works for prototype)
                global ACTIVE_REQ_FILE
                ACTIVE_REQ_FILE = target_file
                
                asyncio.create_task(simulate_agent_workflow())
            elif message.get("command") == "restartCompilation":
                 # Restart logic if needed (handled by frontend re-sending start)
                 pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    print("ARC Backend listening on ws://127.0.0.1:8000/ws/compiler")
    uvicorn.run(app, host="127.0.0.1", port=8000)
