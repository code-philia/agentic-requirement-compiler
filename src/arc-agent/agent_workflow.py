import os
import json
import asyncio
from typing import Callable, Awaitable
import shutil
import re

from agents.requirement_analyzer import RequirementAnalyzer, parse_and_store_interfaces
from agents.interface_designer import InterfaceDesigner
from agents.test_generator import TestGenerator
from agents.test_driven_developer import TestDrivenDeveloper

from traceability.database import (
    get_requirement_by_id, 
    set_db_path, 
    init_db, 
    update_interface_file_info, 
    insert_test, 
    update_test_implemented_status,
    get_interfaces_by_req_id,
    get_tests_by_req_id
)

from utils import run_npm_install, run_git_init, run_git_commit, set_workspace_root

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Global Debug Flag
DEBUG_MODE = int(os.environ.get("ARC_DEBUG", "1"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'template-fullstack')

class ARCWorkflowManager:
    """Manage the lifecycle of a single requirement node and multi-agent TDD state transitions"""
    
    def __init__(self, workspace_path: str, requirement_path: str, broadcast_cb: Callable[[dict], Awaitable[None]] = None):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
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

    async def initialize_project(self):
        """Initialize the project workspace by setting up directories and files."""
        await self._log("System", f"Initializing project environment in {self.workspace_path}...")
        
        # Configure tool context
        set_workspace_root(self.workspace_path)

        # Initialize .arc database
        arc_dir = os.path.join(self.workspace_path, '.arc')
        db_path = os.path.join(arc_dir, 'database.db')
        
        await self._log("System", f"Initializing traceability database at {db_path}...")
        set_db_path(db_path)
        init_db()

        if not os.path.exists(TEMPLATE_DIR):
            await self._log("System", f"Error: Template directory not found at {TEMPLATE_DIR}")
            return False

        await self._log("System", f"Copying template from {TEMPLATE_DIR} to {self.workspace_path}...")
        try:
            await asyncio.to_thread(shutil.copytree, TEMPLATE_DIR, self.workspace_path, dirs_exist_ok=True)
            await self._log("System", "Template files copied successfully.")
        except Exception as e:
            await self._log("System", f"Error copying template: {str(e)}")
            return False

        backend_path = os.path.join(self.workspace_path, 'backend')
        if os.path.exists(backend_path):
            await self._log("System", "Installing backend dependencies. This might take a moment...")
            await run_npm_install(backend_path, self._log)

        frontend_path = os.path.join(self.workspace_path, 'frontend')
        if os.path.exists(frontend_path):
            await self._log("System", "Installing frontend dependencies. This might take a moment...")
            await run_npm_install(frontend_path, self._log)

        await self._log("System", "Full-stack workspace initialized completely.")
        
        # Initialize Git
        await self._log("System", "Initializing Git repository...")
        await run_git_init(self.workspace_path, self._log)

    async def process_node(self, node_id: str) -> dict:
        """Process a single requirement node through the 4-step workflow"""
                
        # Get requirement data from database
        requirement_data = get_requirement_by_id(node_id)
        if not requirement_data:
            await self._log("System", f"Error: Requirement node {node_id} not found in database.", node_id=node_id)
            return False
        
        try:
            # ==========================================
            # Step 1: Requirement Analysis
            # ==========================================
            await self._log("RequirementAnalyzer", f"Starting analysis for node {node_id}\n {json.dumps(requirement_data, indent=2)}", "analyzing", node_id)
            
            dependency_context = build_dependency_context(node_id)
            
            await self.requirement_analyzer.parse_and_store_visual_elements(self.workspace_path, requirement_data)
            # Refresh requirement data after parsing visual elements
            requirement_data = get_requirement_by_id(node_id)

            raw_analysis_output = await self.requirement_analyzer.analyze(
                node_id=node_id, 
                requirement_data=requirement_data,
                project_context=project_context,
                global_map=global_map_str
            )
            node_state["analysis"] = raw_analysis_output

            parsed_interfaces = parse_and_store_interfaces(raw_analysis_output, node_id)
            
            if parsed_interfaces:
                interface_names = [i.get('interface_id') for i in parsed_interfaces]
                await self._log("System", f"Extracted and stored {len(parsed_interfaces)} interfaces: {', '.join(interface_names)}", node_id=node_id)
                
                # Git Commit for Analysis
                commit_msg = f"feat(analysis): [{node_id}] extracted interfaces: {', '.join(interface_names)}"
                await run_git_commit(self.workspace_path, commit_msg, self._log)
                
            else:
                await self._log("System", "Warning: No valid interface IR extracted from analysis.", node_id=node_id)

            # ==========================================
            # Step 2: Design (Physical Implementation of IR)
            # ==========================================
            await self._log("InterfaceDesigner", f"Starting interface design for node {node_id}...", "designed", node_id)
            
            # TODO: Tech Stack Context
            tech_stack = """
### Frontend
* **Framework**: React 18+ (Vite)
* **Language**: JavaScript (ES6+)
* **Styling**: Tailwind CSS v4
* **HTTP**: Axios (Must use Interceptors for global error handling)
* **Testing**: None in frontend directory. (Verified via E2E in backend).

### Backend
* **Runtime**: Node.js (LTS)
* **Framework**: Express.js
* **Database**: SQLite3 (`sqlite3` driver, file-based)
* **Testing**: 
    * **Vitest**: Used for Unit and Integration testing.
    * **Supertest**: Used with Vitest for API route testing.
    * **Playwright**: Used for End-to-End (E2E) testing, located in `backend/test-e2e`.            
"""
            # 1. Retrieve interfaces from DB
            interfaces_ir = get_interfaces_by_req_id(node_id)
            
            if not interfaces_ir:
                await self._log("System", "No IR found in database. Skipping physical design.", node_id=node_id)
            else:
                # 2. Batch process interfaces by type: UI -> API -> FUNC -> DB
                type_order = ["UI", "API", "FUNC", "DB"]
                
                # Group interfaces by type
                interfaces_by_type = {t: [] for t in type_order}
                for iface in interfaces_ir:
                    itype = iface.get("type", "FUNC")
                    if itype in interfaces_by_type:
                        interfaces_by_type[itype].append(iface)
                    else:
                        # Fallback for unknown types
                        if "OTHER" not in interfaces_by_type:
                            interfaces_by_type["OTHER"] = []
                        interfaces_by_type["OTHER"].append(iface)
                
                all_design_outputs = []
                
                # Iterate through types
                for itype in type_order + ["OTHER"]:
                    batch = interfaces_by_type.get(itype, [])
                    if not batch:
                        continue
                        
                    await self._log("InterfaceDesigner", f"Designing {len(batch)} interfaces of type {itype}...", node_id=node_id)
                    
                    raw_design_output = await self.interface_designer.design(
                        node_id=node_id, 
                        interfaces_ir=batch,
                        tech_stack=tech_stack
                    )
                    all_design_outputs.append(raw_design_output)

                    # 3. Update DB with file paths for this batch
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
                                    
                            await self._log("System", f"Updated physical file paths for {len(file_mappings)} {itype} interfaces.", node_id=node_id)
                            
                            # Git Commit for this batch
                            if file_mappings:
                                designed_interfaces = [m.get("interface_id", "UNKNOWN") for m in file_mappings]
                                commit_msg = f"feat(design): [{node_id}] designed {itype} interfaces: {', '.join(designed_interfaces)}"
                                await run_git_commit(self.workspace_path, commit_msg, self._log)
                                
                        except json.JSONDecodeError:
                            await self._log("System", f"Failed to parse file mappings JSON for {itype} batch.", node_id=node_id)


            # ==========================================
            # Step 3: Generate Tests (TDD Preparations)
            # ==========================================
            await self._log("TestGenerator", f"Generating test suite for node {node_id}...", node_id=node_id)
            
            interfaces_ir = get_interfaces_by_req_id(node_id)
            req_desc = requirement_data.get("description", "")
            req_scenarios = requirement_data.get("scenarios", [])
            
            if not interfaces_ir:
                await self._log("System", "No IR found. Skipping test generation.", node_id=node_id)
            else:
                # Group interfaces by type
                interfaces_by_type = {"DB": [], "FUNC": [], "API": [], "UI": []}
                for iface in interfaces_ir:
                    itype = iface.get("type", "").upper()
                    if itype in interfaces_by_type:
                        interfaces_by_type[itype].append(iface)
                    else:
                        if "OTHER" not in interfaces_by_type:
                            interfaces_by_type["OTHER"] = []
                        interfaces_by_type["OTHER"].append(iface)

                all_test_outputs = []
                
                # 1. DB & FUNC -> Unit tests
                unit_interfaces = interfaces_by_type["DB"] + interfaces_by_type["FUNC"]
                if unit_interfaces:
                    await self._log("TestGenerator", f"Generating Unit Tests for {len(unit_interfaces)} DB/FUNC interfaces...", node_id=node_id)
                    unit_test_output = await self.test_generator.generate_tests(
                        node_id=node_id,
                        interfaces_ir=unit_interfaces,
                        tech_stack=tech_stack,
                        test_type="Unit",
                        req_desc=req_desc,
                        dependency_context=dependency_context
                    )
                    all_test_outputs.append(unit_test_output)

                # 2. API -> Integration tests
                api_interfaces = interfaces_by_type["API"]
                if api_interfaces:
                    await self._log("TestGenerator", f"Generating Integration Tests for {len(api_interfaces)} API interfaces...", node_id=node_id)
                    api_test_output = await self.test_generator.generate_tests(
                        node_id=node_id,
                        interfaces_ir=api_interfaces,
                        tech_stack=tech_stack,
                        test_type="Integration",
                        req_desc=req_desc,
                        dependency_context=dependency_context
                    )
                    all_test_outputs.append(api_test_output)

                # 3. UI -> E2E tests based on scenarios
                ui_interfaces = interfaces_by_type["UI"]
                if ui_interfaces and req_scenarios:
                    await self._log("TestGenerator", f"Generating E2E Tests for {len(req_scenarios)} scenarios...", node_id=node_id)
                    for idx, scenario in enumerate(req_scenarios):
                        await self._log("TestGenerator", f"Generating E2E Test for Scenario {idx+1}/{len(req_scenarios)}: {scenario.get('name', 'Unknown')}", node_id=node_id)
                        e2e_test_output = await self.test_generator.generate_tests(
                            node_id=node_id,
                            interfaces_ir=ui_interfaces,
                            tech_stack=tech_stack,
                            test_type="E2E",
                            req_desc=req_desc,
                            scenario=scenario,
                            dependency_context=dependency_context
                        )
                        all_test_outputs.append(e2e_test_output)
                elif ui_interfaces and not req_scenarios:
                    await self._log("TestGenerator", f"Generating fallback E2E Test (no scenarios found)...", node_id=node_id)
                    e2e_test_output = await self.test_generator.generate_tests(
                        node_id=node_id,
                        interfaces_ir=ui_interfaces,
                        tech_stack=tech_stack,
                        test_type="E2E",
                        req_desc=req_desc,
                        dependency_context=dependency_context
                    )
                    all_test_outputs.append(e2e_test_output)
                    
                # Combine outputs to store in state
                combined_output = "\n\n".join(all_test_outputs)
                self.state["tests"] = combined_output

                # parse test mappings
                for raw_test_output in all_test_outputs:
                    match = re.search(r'```json\s*(.*?)\s*```', raw_test_output, re.DOTALL | re.IGNORECASE)
                    if match:
                        try:
                            test_mappings = json.loads(match.group(1))
                            if not isinstance(test_mappings, list):
                                test_mappings = [test_mappings] # fallback if they return dict
                                
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
            req_desc = requirement_data.get("description", "")
            req_scenarios = requirement_data.get("scenarios", [])

            # Get tests from DB to know what files to run
            tests = get_tests_by_req_id(node_id)
            
            # Group tests by type
            tests_by_type = {"Unit": [], "Integration": [], "E2E": []}
            for t in tests:
                t_type = t.get("type", "Unit")
                if t_type in tests_by_type:
                    tests_by_type[t_type].append(t)
            
            # Helper to run TDD loop
            current_interfaces = get_interfaces_by_req_id(node_id)
            
            async def run_tdd_loop(target_type: str, tests_batch: list, budget: int, scenario: dict = None):
                test_files = [t.get("file_path") for t in tests_batch if t.get("file_path")]
                test_ids = [t.get("test_id") for t in tests_batch if t.get("test_id")]
                
                if not test_files:
                    return True # Nothing to test

                await self._log("TestDrivenDeveloper", f"Starting {target_type} TDD loop with {len(test_files)} tests (Budget: {budget})...", node_id=node_id)
                
                for iteration in range(1, budget + 1):
                    await self._log("TestDrivenDeveloper", f"[{target_type}] Iteration {iteration}/{budget}...", node_id=node_id)
                    final_output = await self.test_driven_developer.implement(
                        node_id=node_id,
                        test_files=test_files,
                        test_type=target_type,
                        req_desc=req_desc,
                        scenario=scenario,
                        dependency_context=dependency_context,
                        current_interfaces=current_interfaces
                    )
                    
                    if "IMPLEMENTED" in final_output:
                        await self._log("System", f"[{target_type}] All tests passed! TDD loop completed successfully.", node_id=node_id)
                        
                        # Mark specifically related interfaces as implemented based on this batch
                        try:
                            update_test_implemented_status(test_ids)
                            await self._log("System", f"Database updated: Interfaces covered by {target_type} tests marked as implemented.", node_id=node_id)
                        except Exception as db_err:
                            await self._log("System", f"Warning: Failed to update DB status for {target_type} batch: {str(db_err)}", node_id=node_id)
                            
                        return True
                    else:
                        await self._log("System", f"[{target_type}] Agent finished reasoning but tests might not be fully passing. Retrying...", node_id=node_id)
                
                await self._log("System", f"Warning: [{target_type}] Max TDD iterations reached without a definitive 'IMPLEMENTED' signal.", node_id=node_id)
                return False

            # 1. DB & FUNC -> Unit tests
            unit_tests = tests_by_type["Unit"]
            if unit_tests:
                await run_tdd_loop("Unit", unit_tests, budget=5)

            # 2. API -> Integration tests
            int_tests = tests_by_type["Integration"]
            if int_tests:
                await run_tdd_loop("Integration", int_tests, budget=5)

            # 3. UI -> E2E tests (One by one per scenario)
            e2e_tests = tests_by_type["E2E"]
            if e2e_tests:
                # E2E tests are matched to scenarios. If scenarios exist, match by index.
                for idx, e2e_test in enumerate(e2e_tests):
                    file_path = e2e_test.get("file_path")
                    if not file_path:
                        continue
                    
                    # Try to pair with scenario if available
                    scenario = req_scenarios[idx] if idx < len(req_scenarios) else None
                    scenario_name = scenario.get("name", f"Scenario {idx+1}") if scenario else f"Scenario {idx+1}"
                    
                    await self._log("System", f"Running E2E for {scenario_name}...", node_id=node_id)
                    await run_tdd_loop("E2E", [e2e_test], budget=3, scenario=scenario)

            return True

        except Exception as e:
            await self._log("System", f"Workflow failed due to an error: {str(e)}", node_id=node_id)
            return False



async def run_agent_workflow(manager: ARCWorkflowManager, node_id: str, requirement_data: dict):
    """Unified entry point for processing a node using an existing manager"""
    final_state = await manager.process_node(node_id, requirement_data)
    return final_state
