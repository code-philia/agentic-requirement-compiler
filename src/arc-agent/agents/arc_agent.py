import asyncio
import json
import os
from typing import List, Dict, Any, Callable, Awaitable
from openai import AsyncOpenAI
from .tools import TOOL_REGISTRY

# os.environ["OPENAI_API_KEY"] = "your_api_key_here"

class ARCAgent:
    """
    Base class for the ARC multi-agent system.
    """
    def __init__(
        self, 
        agent_name: str, 
        model: str = "gpt-4o-mini", 
        broadcast_cb: Callable[[dict], Awaitable[None]] = None
    ):
        self.agent_name = agent_name
        self.model = model
        self.broadcast_cb = broadcast_cb
        self.client = AsyncOpenAI() 
        
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

    async def run(self, user_prompt: str, node_id: str = None, max_steps: int = 10) -> str:
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
                
            response = await self.client.chat.completions.create(**api_kwargs)
            message = response.choices[0].message
            messages.append(message)

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                        
                    await self._log(f"Calling tool: `{tool_name}`", node_id=node_id)
                    
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
