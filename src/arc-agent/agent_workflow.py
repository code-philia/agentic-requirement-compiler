import asyncio
from typing import Callable, Awaitable

from agents.requirement_analyzer import RequirementAnalyzer, parse_and_store_interfaces
from agents.interface_designer import InterfaceDesigner
from agents.test_generator import TestGenerator
from agents.test_driven_developer import TestDrivenDeveloper

from traceability.database import update_interface_file_info

from utils import init_project_workspace

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

    async def initialize_workspace(self):
        """Initialize the project workspace by setting up directories and files."""
        await self._log("System", f"Initializing project environment in {self.workspace_path}...")

        await init_project_workspace(self.workspace_path, self.broadcast_cb)

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
            # ==========================================
            # Step 1: Requirement Analysis
            # ==========================================
            await self._log("RequirementAnalyzer", f"Starting analysis for node {self.node_id}...", "analyzing")
            
            # TODO Proper context here 
            global_map_str = "" 
            project_context = ""
            
            raw_analysis_output = await self.analyzer.analyze(
                node_id=self.node_id, 
                requirement_data=self.requirement_data,
                project_context=project_context,
                global_map=global_map_str
            )
            self.state["analysis_raw"] = raw_analysis_output

            parsed_interfaces = parse_and_store_interfaces(raw_analysis_output, self.node_id)
            self.state["interfaces_ir"] = parsed_interfaces
            
            if parsed_interfaces:
                interface_names = [i.get('interface_id') for i in parsed_interfaces]
                await self._log("System", f"Extracted and stored {len(parsed_interfaces)} interfaces: {', '.join(interface_names)}")
            else:
                await self._log("System", "Warning: No valid interface IR extracted from analysis.")

            # ==========================================
            # Step 2: Design (Physical Implementation of IR)
            # ==========================================
            await self._log("InterfaceDesigner", f"Starting interface design for node {node_id}...", "designed", node_id)
            
            # TODO: Tech Stack Context
            tech_stack = "Python 3.10, FastAPI for API, SQLAlchemy for DB, React for UI."
            interfaces_ir = self.state.get("interfaces_ir", [])
            
            if not interfaces_ir:
                await self._log("System", "No IR found. Skipping physical design.", node_id=node_id)
            else:
                raw_design_output = await self.interface_designer.design(
                    node_id=node_id, 
                    interfaces_ir=interfaces_ir,
                    tech_stack=tech_stack
                )
                self.state["interfaces"] = raw_design_output

                match = re.search(r'```json\s*(.*?)\s*```', raw_design_output, re.DOTALL | re.IGNORECASE)
                if match:
                    try:
                        file_mappings = json.loads(match.group(1))
                        for mapping in file_mappings:
                            i_id = mapping.get("interface_id")
                            f_path = mapping.get("file_path", "")
                            f_line = mapping.get("first_line", "")
                            
                            if i_id:
                                update_interface_file_info(i_id, f_path, f_line)
                                
                        await self._log("System", f"Successfully updated physical file paths for {len(file_mappings)} interfaces in DB.", node_id=node_id)
                    except json.JSONDecodeError:
                        await self._log("System", "Failed to parse file mappings JSON from InterfaceDesigner.", node_id=node_id)

            # ==========================================
            # Step 3: Generate Tests (TDD Preparations)
            # ==========================================
            await self._log("TestGenerator", f"Generating test suite for node {node_id}...", node_id=node_id)
            
            if not interfaces_ir:
                await self._log("System", "No IR found. Skipping test generation.", node_id=node_id)
            else:
                raw_test_output = await self.test_generator.generate_tests(
                    node_id=node_id, 
                    interfaces_ir=interfaces_ir,
                    tech_stack=tech_stack
                )
                self.state["tests"] = raw_test_output

                # parse test mappings
                match = re.search(r'```json\s*(.*?)\s*```', raw_test_output, re.DOTALL | re.IGNORECASE)
                if match:
                    try:
                        test_mappings = json.loads(match.group(1))
                        for mapping in test_mappings:
                            t_id = mapping.get("test_id", f"TEST_{node_id}_UNKNOWN")
                            r_id = mapping.get("req_id", node_id)
                            i_ids = mapping.get("interface_ids", [])
                            t_type = mapping.get("type", "Unit")
                            f_path = mapping.get("file_path", "")
                            f_line = mapping.get("first_line", "")
                            
                            insert_test(
                                test_id=t_id,
                                req_id=r_id,
                                interface_ids=i_ids,
                                type=t_type,
                                file_path=f_path,
                                first_line=f_line
                            )
                                
                        await self._log("System", f"Successfully registered {len(test_mappings)} tests in traceability database.", node_id=node_id)
                    except json.JSONDecodeError:
                        await self._log("System", "Failed to parse test mappings JSON from TestGenerator.", node_id=node_id)

            # ==========================================
            # Step 4: Implement (TDD Loop)
            # ==========================================
            max_tdd_loops = 3 # external retry loops
            node_state = self.state

            while node_state["iteration"] < max_tdd_loops and not node_state["test_passed"]:
                node_state["iteration"] += 1
                await self._log("TestDrivenDeveloper", f"Starting implementation (TDD Iteration {node_state['iteration']}/{max_tdd_loops})...", node_id=node_id)

                final_output = await self.test_driven_developer.implement(
                    node_id=node_id, 
                    tests_summary=node_state.get("tests", "No test summary available."),
                    iteration=node_state["iteration"]
                )
                
                # 检查是否成功
                if "IMPLEMENTED" in final_output:
                    node_state["test_passed"] = True
                    
                    # 关键闭环：回写数据库，标记接口已完成
                    try:
                        update_interface_implemented_status(node_id)
                        await self._log("System", f"Database updated: Interfaces for Node {node_id} marked as implemented.", node_id=node_id)
                    except Exception as db_err:
                        await self._log("System", f"Warning: Failed to update DB status: {str(db_err)}", node_id=node_id)
                        
                    await self._log("System", "All tests passed! TDD loop completed successfully.", node_id=node_id)
                else:
                    await self._log("System", "Agent finished reasoning but tests might not be fully passing. Retrying...", node_id=node_id)
                    
            if not node_state["test_passed"]:
                await self._log("System", "Warning: Max TDD iterations reached without a definitive 'IMPLEMENTED' signal.", node_id=node_id)

            return node_state

        except Exception as e:
            await self._log("System", f"Workflow failed due to an error: {str(e)}", node_id=node_id)
            return self.state



async def run_agent_workflow(manager: ARCWorkflowManager, node_id: str, requirement_data: dict):
    """Unified entry point for processing a node using an existing manager"""
    final_state = await manager.process_node(node_id, requirement_data)
    return final_state
