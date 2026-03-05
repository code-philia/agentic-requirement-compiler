import asyncio
import json
from typing import Callable, Awaitable

# ==========================================
# 1. Simulated low-level tool library (Tool Executions)
# In real projects, this should wrap real file IO, terminal command execution, etc.
# ==========================================
async def execute_tool(tool_name: str, tool_args: dict) -> str:
    """Unified tool execution router"""
    try:
        if tool_name == "read_file":
            return f"[Simulated Content of {tool_args.get('path')}]"
        elif tool_name == "write_file":
            return "File written successfully."
        elif tool_name == "run_tests":
            # Simulated test environment: assume tests pass only if the code contains a specific defensive string
            code = tool_args.get('code', '')
            if "IMPLEMENTED" in code:
                return "Tests passed: 100% coverage."
            else:
                return "Tests failed: AssertionError at line 42."
        else:
            return f"Error: Unknown tool '{tool_name}'"
    except Exception as e:
        return f"Tool execution failed: {str(e)}"


# ==========================================
# 2. Native LLM tool loop
# ==========================================
async def native_llm_tool_loop(
    agent_name: str, 
    system_prompt: str, 
    user_prompt: str, 
    broadcast_cb: Callable = None,
    max_steps: int = 5
) -> str:
    """Core loop: dialogue -> parse tool -> execute tool -> feed result back into context -> continue dialogue"""
    
    # Initialize context window (in real applications, you should strictly manage the token length)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    step = 0
    while step < max_steps:
        step += 1
        
        # -----------------------------------------------------------------
        # In a real scenario, replace this with a large model API call:
        # response = await client.chat.completions.create(model="...", messages=messages, tools=...)
        # response_message = response.choices[0].message
        # -----------------------------------------------------------------
        
        # --- Simulated LLM behavior (Mock LLM) ---
        await asyncio.sleep(1)  # Simulate network latency
        if step == 1:
            # First step: LLM decides to read a file
            llm_response = {"type": "tool_call", "tool": "read_file", "args": {"path": "src/module.py"}}
        elif step == 2:
            # Second step: LLM tries to run tests
            llm_response = {"type": "tool_call", "tool": "run_tests", "args": {"code": "def func(): return 'bug'"}}
        else:
            # Third step: LLM finishes the task and outputs final code
            llm_response = {"type": "final_answer", "content": "IMPLEMENTED: Fixed the bug and passed tests."}
        # -----------------------------------------------------------------

        # Handle the LLM response
        if llm_response["type"] == "tool_call":
            tool_name = llm_response["tool"]
            tool_args = llm_response["args"]

            if broadcast_cb:
                await broadcast_cb({
                    "type": "log", "agent": agent_name, 
                    "message": f"Thinking... calling tool: `{tool_name}`"
                })

            # 1. Intercept and execute the tool
            tool_result = await execute_tool(tool_name, tool_args)

            # 2. Append the action and result to the history for the next reasoning step
            messages.append({"role": "assistant", "content": f"Call tool {tool_name} with {json.dumps(tool_args)}"})
            messages.append({"role": "user", "content": f"Tool execution result:\n{tool_result}"})

        elif llm_response["type"] == "final_answer":
            # Exit the loop and return the final result
            return llm_response["content"]

    return "Error: Reached max iteration steps without a final answer."


# ==========================================
# 3. Top-level workflow manager
# ==========================================
class ARCWorkflowManager:
    """Manage the lifecycle and TDD state transitions for a single requirement node"""
    
    def __init__(self, node_id: str, requirement_data: dict, broadcast_cb: Callable[[dict], Awaitable[None]] = None):
        self.node_id = node_id
        self.requirement_data = requirement_data
        self.broadcast_cb = broadcast_cb
        
        # Shared workflow state
        self.state = {
            "analysis": "",
            "interfaces": "",
            "tests": "",
            "iteration": 0,
            "test_passed": False
        }

    async def _log(self, agent: str, message: str, status: str = None):
        if self.broadcast_cb:
            payload = {"type": "log", "agent": agent, "message": message, "nodeId": self.node_id}
            if status:
                payload["status"] = status
            await self.broadcast_cb(payload)

    async def step_1_analyze(self):
        await self._log("RequirementAnalyzer", f"Analyzing requirement node {self.node_id}...", "analyzing")
        await asyncio.sleep(1)
        self.state["analysis"] = "Parsed constraints and goals."

    async def step_2_design(self):
        await self._log("InterfaceDesigner", f"Designing interfaces for {self.node_id}...", "designed")
        await asyncio.sleep(1)
        self.state["interfaces"] = "Defined API schemas."

    async def step_3_generate_tests(self):
        await self._log("TestGenerator", f"Generating test suite for {self.node_id}...")
        await asyncio.sleep(1)
        self.state["tests"] = "Test cases generated."

    async def step_4_implement_tdd(self):
        max_tdd_loops = 3
        
        while self.state["iteration"] < max_tdd_loops and not self.state["test_passed"]:
            self.state["iteration"] += 1
            await self._log("CodeGenerator", f"Implementing business logic (TDD Iteration {self.state['iteration']}/{max_tdd_loops})...")

            # Call the underlying native agent loop
            system_prompt = "You are an expert developer. You have access to tools: read_file, write_file, run_tests."
            user_prompt = f"Implement node {self.node_id} based on tests: {self.state['tests']}"
            
            final_code_output = await native_llm_tool_loop(
                agent_name="CodeGenerator",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                broadcast_cb=self.broadcast_cb
            )

            # Evaluate execution result to decide whether to exit the loop
            if "IMPLEMENTED" in final_code_output:
                self.state["test_passed"] = True
                await self._log("TestRunner", "All tests passed! TDD loop completed.")
            else:
                await self._log("TestRunner", "Tests failed. Feeding errors back to CodeGenerator...")
                
        if not self.state["test_passed"]:
            await self._log("System", "Warning: Max TDD iterations reached without passing all tests.")

    async def execute(self):
        """Execute the entire workflow sequentially"""
        await self.step_1_analyze()
        await self.step_2_design()
        await self.step_3_generate_tests()
        await self.step_4_implement_tdd()
        return self.state


async def run_agent_workflow(node_id: str, requirement_data: dict, broadcast_cb=None):
    manager = ARCWorkflowManager(node_id, requirement_data, broadcast_cb)
    final_state = await manager.execute()
    return final_state
