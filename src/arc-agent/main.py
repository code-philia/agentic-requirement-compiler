import asyncio
import json
import os
import yaml
from typing import List, Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

from utils import *
from agent_workflow import ARCWorkflowManager, run_agent_workflow
from traceability import store_all_requirement, get_traceability_data

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
async def run_compilation(project_path: str, requirement_path: str, clear_all: bool = False):
    """full compilation workflow based on the requirement DAG"""
    
    await manager.broadcast({"type": "clear-logs"})
    await manager.broadcast({"type": "log", "agent": "System", "message": "ARC compilation system started..."})
    await asyncio.sleep(1)

    if clear_all:
        await manager.broadcast({"type": "log", "agent": "System", "message": "Clear and re-compile requested. Cleaning workspace..."})
        try:
            import shutil
            # Delete everything in project_path EXCEPT the 'requirements' folder
            for item in os.listdir(project_path):
                if item == "requirements":
                    continue
                item_path = os.path.join(project_path, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.remove(item_path)
            
            # Delete status.json inside requirements folder
            status_file = os.path.join(project_path, ".arc", "status.json")
            if os.path.exists(status_file):
                os.remove(status_file)
                
            await manager.broadcast({"type": "log", "agent": "System", "message": "Workspace cleaned successfully."})
        except Exception as e:
            await manager.broadcast({"type": "error-event", "agent": "System", "message": f"Failed to clean workspace: {str(e)}"})
            return

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

    # 2. Initialize Project Environment & Database
    workflow_manager = ARCWorkflowManager(
        workspace_path=project_path,
        requirement_path=requirement_path,
        broadcast_cb=manager.broadcast
    )
    await workflow_manager.initialize_project()

    # 3. Build DAG & Store Requirements
    await manager.broadcast({"type": "log", "agent": "DependencyManager", "message": "Building requirement dependency DAG..."})
    await asyncio.sleep(1)
    
    # Store requirements into the initialized database
    store_all_requirement(data)
    
    leaves = get_all_leaves(data)
    process_queue = topological_sort(leaves)
    
    await manager.broadcast({"type": "log", "agent": "DependencyManager", "message": f"Calculated processing order: {', '.join(process_queue)}"})
    await asyncio.sleep(1)

    # 4. Process Nodes
    for node_id in process_queue:
        update_node_status(workflow_manager.workspace_path, node_id, "analyzing")
        await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "analyzing"})

        ok = await workflow_manager.process_node(node_id)
        if ok:
            update_node_status(workflow_manager.workspace_path, node_id, "completed")
            await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "completed"})
        else:
            update_node_status(workflow_manager.workspace_path, node_id, "error")
            await manager.broadcast({"type": "node_update", "nodeId": node_id, "status": "error"})
            await manager.broadcast({"type": "error-event", "agent": "System", "message": f"Node {node_id} failed. Continue with next node."})
        

    await manager.broadcast({"type": "log", "agent": "System", "message": "All requirements processed successfully. Project compiled!"})

from traceability.database import set_db_path

@app.websocket("/ws/compiler")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("command") == "start":
                project_path = message.get("projectPath")
                requirement_path = message.get("requirementPath")
                if project_path and requirement_path:
                    # Run compilation in background task to not block websocket loop
                    asyncio.create_task(run_compilation(project_path, requirement_path, clear_all=False))

            elif message.get("command") == "restart":
                project_path = message.get("projectPath")
                requirement_path = message.get("requirementPath")
                if project_path and requirement_path:
                    asyncio.create_task(run_compilation(project_path, requirement_path, clear_all=True))
            
            elif message.get("command") == "traceabilityData":
                node_id = message.get("nodeId")
                keyword = message.get("keyword", "")
                project_path = message.get("projectPath")
                
                print(project_path)
                
                if project_path:
                     db_path = os.path.join(project_path, '.arc', 'database.db')
                     set_db_path(db_path)
                
                if node_id:
                    result = get_traceability_data(node_id, keyword=keyword)
                else:
                    result = get_traceability_data("", keyword=keyword)
                await websocket.send_text(json.dumps({
                    "type": "traceabilityData", 
                    "nodeId": node_id, 
                    "data": result
                }))

    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    print("ARC Backend listening on ws://127.0.0.1:8000/ws/compiler")
    uvicorn.run(app, host="127.0.0.1", port=8000)
