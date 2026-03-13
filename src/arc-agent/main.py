import asyncio
import json
import os
import yaml
from typing import List, Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

from utils import *
from agent_workflow import ARCWorkflowManager, run_agent_workflow
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
Compilation Workflow:
1. Load and Parse Requirements
2. Build DAG
3. Process Nodes in Topological Order
"""
async def run_compilation(project_path: str, requirement_path: str):
    """full compilation workflow based on the requirement DAG"""
    
    await manager.broadcast({"type": "log", "agent": "System", "message": "ARC compilation system started..."})
    await asyncio.sleep(1)

    # 1. Load and Parse Requirements
    await manager.broadcast({"type": "log", "agent": "RequirementLoader", "message": f"Reading requirements file: {requirement_path}"})
    try:
        data = load_requirements(requirement_path)
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
    # Initialize a shared ARCWorkflowManager for this compilation session
    workflow_manager = ARCWorkflowManager(
        workspace_path=project_path,
        requirement_path=requirement_path,
        broadcast_cb=manager.broadcast
    )
    
    await workflow_manager.initialize_project()

    for node_id in process_queue:
        update_node_status(workflow_manager.requirement_path, node_id, "analyzing")
        await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "analyzing"})

        await workflow_manager.process_node(node_id)
        
        update_node_status(workflow_manager.requirement_path, node_id, "completed")
        await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "completed"})
        

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
                requirement_path = os.path.join(project_path, 'requirements', 'requirements.yaml')
                
                asyncio.create_task(run_compilation(project_path, requirement_path))
            elif message.get("command") == "restartCompilation":
                 # Restart logic if needed (handled by frontend re-sending start)
                 pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    print("ARC Backend listening on ws://127.0.0.1:8000/ws/compiler")
    uvicorn.run(app, host="127.0.0.1", port=8000)
