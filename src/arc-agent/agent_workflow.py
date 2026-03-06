import asyncio
from typing import Callable, Awaitable

from agents.requirement_analyzer import RequirementAnalyzer
from agents.interface_designer import InterfaceDesigner
from agents.test_generator import TestGenerator
from agents.test_driven_developer import TestDrivenDeveloper

class ARCWorkflowManager:
    """Manage the lifecycle of a single requirement node and multi-agent TDD state transitions"""
    
    def __init__(self, workspace_path: str, broadcast_cb: Callable[[dict], Awaitable[None]] = None):
        self.workspace_path = workspace_path
        self.broadcast_cb = broadcast_cb
        
        # Instantiate all participating agents and pass the WebSocket broadcast callback to them
        self.requirement_analyzer = RequirementAnalyzer(broadcast_cb)
        self.interface_designer = InterfaceDesigner(broadcast_cb)
        self.test_generator = TestGenerator(broadcast_cb)
        self.test_driven_developer = TestDrivenDeveloper(broadcast_cb)

        # Global state context for the entire workflow (optional)
        # self.global_context = {} 

    async def _log(self, agent: str, message: str, status: str = None, node_id: str = None):
        if self.broadcast_cb:
            payload = {"type": "log", "agent": agent, "message": message}
            if node_id:
                payload["nodeId"] = node_id
            if status:
                payload["status"] = status
            await self.broadcast_cb(payload)
        else:
            # Default to console print if no callback provided
            prefix = f"[{node_id}] " if node_id else ""
            print(f"{prefix}[{agent}] {message}")
            if status:
                print(f"{prefix}[Status Update] {status}")

    async def process_node(self, node_id: str, requirement_data: dict) -> dict:
        """Process a single requirement node through the 4-step workflow"""
        
        # Maintain shared workflow context state for this specific node
        node_state = {
            "analysis": "",
            "interfaces": "",
            "tests": "",
            "iteration": 0,
            "test_passed": False
        }
        
        try:
            # Step 1: Analyze
            await self._log("RequirementAnalyzer", f"Starting analysis for node {node_id}...", "analyzing", node_id)
            node_state["analysis"] = await self.requirement_analyzer.analyze(node_id, requirement_data)

            # Step 2: Design
            await self._log("InterfaceDesigner", f"Starting interface design for node {node_id}...", "designed", node_id)
            node_state["interfaces"] = await self.interface_designer.design(node_id, node_state["analysis"])

            # Step 3: Generate Tests
            await self._log("TestGenerator", f"Generating test suite for node {node_id}...", node_id=node_id)
            node_state["tests"] = await self.test_generator.generate_tests(node_id, node_state["interfaces"])

            # Step 4: Implement (TDD Loop)
            max_tdd_loops = 3
            while node_state["iteration"] < max_tdd_loops and not node_state["test_passed"]:
                node_state["iteration"] += 1
                await self._log("TestDrivenDeveloper", f"Starting implementation (TDD Iteration {node_state['iteration']}/{max_tdd_loops})...", node_id=node_id)

                final_output = await self.test_driven_developer.implement(
                    node_id=node_id, 
                    tests_summary=node_state["tests"],
                    iteration=node_state["iteration"]
                )
                
                if "IMPLEMENTED" in final_output:
                    node_state["test_passed"] = True
                    await self._log("System", "All tests passed! TDD loop completed successfully.", node_id=node_id)
                else:
                    await self._log("System", "Agent finished reasoning but tests might not be fully passing. Retrying...", node_id=node_id)
                    
            if not node_state["test_passed"]:
                await self._log("System", "Warning: Max TDD iterations reached without a definitive 'IMPLEMENTED' signal.", node_id=node_id)

            return node_state

        except Exception as e:
            await self._log("System", f"Workflow failed due to an error: {str(e)}", node_id=node_id)
            return node_state



async def run_agent_workflow(manager: ARCWorkflowManager, node_id: str, requirement_data: dict):
    """Unified entry point for processing a node using an existing manager"""
    final_state = await manager.process_node(node_id, requirement_data)
    return final_state
