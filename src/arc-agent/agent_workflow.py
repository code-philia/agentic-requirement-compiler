import asyncio
from typing import Callable, Awaitable

from agents.requirement_analyzer import RequirementAnalyzer
from agents.interface_designer import InterfaceDesigner
from agents.test_generator import TestGenerator
from agents.test_driven_developer import TestDrivenDeveloper

class ARCWorkflowManager:
    """Manage the lifecycle of a single requirement node and multi-agent TDD state transitions"""
    
    def __init__(self, node_id: str, requirement_data: dict, broadcast_cb: Callable[[dict], Awaitable[None]] = None):
        self.node_id = node_id
        self.requirement_data = requirement_data
        self.broadcast_cb = broadcast_cb
        
        # Maintain shared workflow context state
        self.state = {
            "analysis": "",
            "interfaces": "",
            "tests": "",
            "iteration": 0,
            "test_passed": False
        }
        
        # Instantiate all participating agents and pass the WebSocket broadcast callback to them
        self.requirement_analyzer = RequirementAnalyzer(broadcast_cb)
        self.interface_designer = InterfaceDesigner(broadcast_cb)
        self.test_generator = TestGenerator(broadcast_cb)
        self.test_driven_developer = TestDrivenDeveloper(broadcast_cb)

    async def _log(self, agent: str, message: str, status: str = None):
        if self.broadcast_cb:
            payload = {"type": "log", "agent": agent, "message": message, "nodeId": self.node_id}
            if status:
                payload["status"] = status
            await self.broadcast_cb(payload)
        else:
            # Default to console print if no callback provided
            prefix = f"[{self.node_id}] " if self.node_id else ""
            print(f"{prefix}[{agent}] {message}")
            if status:
                print(f"{prefix}[Status Update] {status}")

    async def step_1_analyze(self):
        await self._log("RequirementAnalyzer", f"Starting analysis for node {self.node_id}...", "analyzing")
        # Actually call the LLM to perform requirement analysis
        self.state["analysis"] = await self.requirement_analyzer.analyze(self.node_id, self.requirement_data)

    async def step_2_design(self):
        await self._log("InterfaceDesigner", f"Starting interface design for node {self.node_id}...", "designed")
        # Pass the analysis result to the designer
        self.state["interfaces"] = await self.interface_designer.design(self.node_id, self.state["analysis"])

    async def step_3_generate_tests(self):
        await self._log("TestGenerator", f"Generating test suite for node {self.node_id}...")
        # Pass the interface design to the test generator
        self.state["tests"] = await self.test_generator.generate_tests(self.node_id, self.state["interfaces"])

    async def step_4_implement_tdd(self):
        max_tdd_loops = 3
        
        while self.state["iteration"] < max_tdd_loops and not self.state["test_passed"]:
            self.state["iteration"] += 1
            await self._log("TestDrivenDeveloper", f"Starting implementation (TDD Iteration {self.state['iteration']}/{max_tdd_loops})...")

            # Call the code generator, which runs its own tool loop internally (including invoking the run_tests tool)
            final_output = await self.test_driven_developer.implement(
                node_id=self.node_id, 
                tests_summary=self.state["tests"],
                iteration=self.state["iteration"]
            )
            
            # Evaluate the final output of this iteration to determine whether the task is truly completed
            if "IMPLEMENTED" in final_output:
                self.state["test_passed"] = True
                await self._log("System", "All tests passed! TDD loop completed successfully.")
            else:
                await self._log("System", "Agent finished reasoning but tests might not be fully passing. Retrying...")
                
        if not self.state["test_passed"]:
            await self._log("System", "Warning: Max TDD iterations reached without a definitive 'IMPLEMENTED' signal.")

    async def execute(self):
        """Run the entire workflow sequentially"""
        try:
            await self.step_1_analyze()
            await self.step_2_design()
            await self.step_3_generate_tests()
            await self.step_4_implement_tdd()
            return self.state
        except Exception as e:
            await self._log("System", f"Workflow failed due to an error: {str(e)}")
            return self.state


async def run_agent_workflow(node_id: str, requirement_data: dict, broadcast_cb=None):
    """Unified entry point for main.py"""
    manager = ARCWorkflowManager(node_id, requirement_data, broadcast_cb)
    final_state = await manager.execute()
    return final_state
