import asyncio
import json
import os
from typing import List, Dict, Any, Callable, Awaitable
from openai import AsyncOpenAI
from dotenv import load_dotenv
from .tools import TOOL_REGISTRY

# Load environment variables from .env
load_dotenv()

# Global Debug Flag
DEBUG_MODE = int(os.environ.get("ARC_DEBUG", "1"))

class ARCAgent:
    """
    Base class for the ARC multi-agent system.
    """
    def __init__(
        self, 
        agent_name: str, 
        broadcast_cb: Callable[[dict], Awaitable[None]] = None
    ):
        self.agent_name = agent_name
        self.model = os.environ.get("MODEL", "GLM-5")
        self.broadcast_cb = broadcast_cb
        self.client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_API_BASE_URL"),
        )
        
    async def _log(self, message: str, status: str = None, node_id: str = None):
        if self.broadcast_cb:
            payload = {
                "type": "log", 
                "agent": self.agent_name, 
                "message": message
            }
            if status: payload["status"] = status
            if node_id: payload["nodeId"] = node_id
            await self.broadcast_cb(payload)
        else:
            # Default to console print if no callback provided
            prefix = f"[{node_id}] " if node_id else ""
            print(f"{prefix}[{self.agent_name}] {message}")
            if status:
                print(f"{prefix}[Status Update] {status}")

    def get_system_prompt(self) -> str:
        raise NotImplementedError

    def get_tool_names(self) -> List[str]:
        """
        Subclasses only need to return the list of tool names they require.
        For example: ["read_file", "write_file"]
        """
        return []

    async def run(self, user_prompt: str, node_id: str = None, max_steps: int = 30) -> str:
        messages = [
            {"role": "system", "content": self.get_system_prompt()},
            {"role": "user", "content": user_prompt}
        ]
        
        # Dynamically assemble the tool schemas this agent is allowed to use from the registry
        allowed_tool_names = self.get_tool_names()
        tools = [
            TOOL_REGISTRY[name]["schema"] 
            for name in allowed_tool_names 
            if name in TOOL_REGISTRY
        ]

        for step in range(max_steps):
            await self._log(f"Thinking... (Step {step + 1}/{max_steps})", node_id=node_id)
            
            api_kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.2
            }
            if tools:
                api_kwargs["tools"] = tools
                api_kwargs["tool_choice"] = "auto"
                
            if DEBUG_MODE:
                # Log last message sent to model
                last_msg = messages[-1]["content"]
                await self._log(f"[DEBUG] Input to model:\n{last_msg}", node_id=node_id)
                
            response = await self.client.chat.completions.create(**api_kwargs)
            message = response.choices[0].message
            messages.append(message)
            
            if DEBUG_MODE:
                reply_content = message.content or ""
                await self._log(f"[DEBUG] Model reply:\n{reply_content}", node_id=node_id)

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                        
                    await self._log(f"Calling tool: `{tool_name}` with args: {json.dumps(tool_args, indent=2)}", node_id=node_id)
                    
                    # Look up and execute the actual tool function from the registry
                    if tool_name in TOOL_REGISTRY and tool_name in allowed_tool_names:
                        tool_func = TOOL_REGISTRY[tool_name]["func"]
                        try:
                            # Dynamically call the corresponding async function
                            tool_result = await tool_func(**tool_args)
                        except Exception as e:
                            tool_result = f"Tool execution error: {str(e)}"
                    else:
                        tool_result = f"Error: Tool '{tool_name}' not permitted or not found."
                    
                    if DEBUG_MODE:
                        await self._log(f"Tool `{tool_name}` result: {tool_result}", node_id=node_id)
                    
                    # Return the result back to the LLM
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": str(tool_result)
                    })
                    
                # After tools are executed, continue to the next dialogue step
                continue
            else:
                await self._log("Task completed.", node_id=node_id)
                return message.content

        return "Error: Agent reached maximum reasoning steps without a final conclusion."
