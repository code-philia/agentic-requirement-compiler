import asyncio
import json
import os
import yaml
from typing import List, Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

from utils import *
from agent_workflow import run_agent_workflow
from traceability import store_all_requirement

app = FastAPI(title="ARC Multi-Agent Backend")

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

"""
For a single requirement node, conduct the 4-step process:
1. Analyze
2. Interface Design
3. Generate Tests
4. Implement
"""
async def process_requirement_node(node_id: str, requirement_data: dict = None):
    current_file = ACTIVE_REQ_FILE
    
    update_node_status(current_file, node_id, "analyzing")
    await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "analyzing"})

    await run_agent_workflow(
        node_id=node_id, 
        requirement_data=requirement_data or {},
        broadcast_cb=manager.broadcast
    )

    update_node_status(current_file, node_id, "completed")
    await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "completed"})

"""
Compilation Workflow:
1. Load and Parse Requirements
2. Build DAG
3. Process Nodes in Topological Order
"""
async def run_compilation():
    """full compilation workflow based on the requirement DAG"""
    
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
    
    store_all_requirement(data)
    leaves = get_all_leaves(data)
    process_queue = topological_sort(leaves)
    
    await manager.broadcast({"type": "log", "agent": "DependencyManager", "message": f"Calculated processing order: {', '.join(process_queue)}"})
    await asyncio.sleep(1)

    # 3. Process
    for node_id in process_queue:
        await process_requirement_node(node_id)
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
                target_file = os.path.join(project_path, 'requirements', 'requirements.yaml')
                
                # TODO:Update global or pass to function
                # For this simple script, we can just pass it or set a global (less ideal but works for prototype)
                global ACTIVE_REQ_FILE, WORKSPACE_ROOT
                ACTIVE_REQ_FILE = target_file
                WORKSPACE_ROOT = project_path
                
                asyncio.create_task(run_compilation())
            elif message.get("command") == "restartCompilation":
                 # Restart logic if needed (handled by frontend re-sending start)
                 pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    print("ARC Backend listening on ws://127.0.0.1:8000/ws/compiler")
    uvicorn.run(app, host="127.0.0.1", port=8000)
