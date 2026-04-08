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
            # 1. Auto-Compact / Auto-Summarize: Prevent context overflow
            # Calculate approximate character count of all messages
            total_chars = sum(len(str(m.get("content", ""))) for m in messages if m.get("content"))
            
            if step > 0 and (step % 10 == 0 or total_chars > 60000):
                await self._log(f"Triggering Auto-Compact (Step {step}, Chars: {total_chars})...", node_id=node_id)
                summary_prompt = (
                    "You have been working on this task for several steps. "
                    "Please provide a concise summary (max 300 words) of what you have accomplished so far, "
                    "the current blockers or errors, and your immediate next plan."
                )
                
                # Ask the model to summarize the current progress
                summary_messages = messages + [{"role": "user", "content": summary_prompt}]
                try:
                    summary_response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=summary_messages,
                        temperature=0.2
                    )
                    summary_content = summary_response.choices[0].message.content
                    
                    if DEBUG_MODE:
                        await self._log(f"[DEBUG] Auto-Compact Summary:\n{summary_content}", node_id=node_id)
                    
                    # Replace history with the summary to free up context
                    messages = [
                        messages[0],  # System prompt
                        messages[1],  # Original User prompt
                        {"role": "assistant", "content": f"[Auto-Compact Summary of previous steps]\n{summary_content}"},
                        {"role": "user", "content": "Please continue with the next steps based on the summary above."}
                    ]
                except Exception as e:
                    await self._log(f"Auto-Compact failed: {str(e)}", node_id=node_id)

            # 2. Microcompact: Fold old tool results
            # Keep the first 2 messages (system, user) and the last keep_tool_num messages intact
            keep_tool_num = 10
            if len(messages) > 2 + keep_tool_num:
                for msg in messages[2:-keep_tool_num]:
                    if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg.get("content", "")) > 1000:
                        msg["content"] = msg["content"] + "...\n\n[Old tool result content cleared to save context]"

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
                last_msg = messages[-1].get("content")
                await self._log(f"[DEBUG] Input to model:\n{last_msg}", node_id=node_id)
                
            response = await self.client.chat.completions.create(**api_kwargs)
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            
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
                    
                    # 3. Tool Output Budget: Truncate long tool outputs
                    tool_result_str = str(tool_result)
                    MAX_TOOL_OUTPUT_LENGTH = 8000
                    if len(tool_result_str) > MAX_TOOL_OUTPUT_LENGTH:
                        tool_result_str = tool_result_str[:MAX_TOOL_OUTPUT_LENGTH] + "\n... [Output truncated due to length. Please use grep or narrow your search.]"

                    if DEBUG_MODE:
                        await self._log(f"Tool `{tool_name}` result length: {len(tool_result_str)} chars", node_id=node_id)
                    
                    # Return the result back to the LLM
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_result_str
                    })
                    
                # After tools are executed, continue to the next dialogue step
                continue
            else:
                await self._log("Task completed.", node_id=node_id)
                return message.content

        return "Error: Agent reached maximum reasoning steps without a final conclusion."
