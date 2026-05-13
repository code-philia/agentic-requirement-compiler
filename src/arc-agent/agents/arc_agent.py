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
MAX_TOOL_OUTPUT_LENGTH = int(os.environ.get("ARC_MAX_TOOL_OUTPUT_CHARS", "10000"))
MAX_CONTEXT_CHARS = int(os.environ.get("ARC_MAX_CONTEXT_CHARS", "500000"))
# Two-level compact thresholds (fraction of MAX_CONTEXT_CHARS)
DEDUP_THRESHOLD = 0.70    # Level 1: remove duplicate/redundant tool results
SUMMARIZE_THRESHOLD = 0.8  # Level 2: ask LLM to summarize
MICROCOMPACT_MIN_TOOL_OUTPUT = int(os.environ.get("ARC_MICROCOMPACT_MIN_CHARS", "1000"))
MICROCOMPACT_KEEP_MESSAGES = int(os.environ.get("ARC_MICROCOMPACT_KEEP_MESSAGES", "10"))
MODEL_RETRY_COUNT = int(os.environ.get("ARC_MODEL_RETRY_COUNT", "2"))

READONLY_CACHEABLE_TOOLS = {
    "read_file",
    "list_directory",
    "grep_search",
    "search_interfaces_by_keyword",
    "search_interfaces_by_relation",
    "find_interface_impacts",
    "get_node_relations",
}

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

    def _estimate_context_chars(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                total += len(content)
            elif content is not None:
                total += len(str(content))
        return total

    def _extract_read_files(self, messages: List[Dict[str, Any]]) -> List[str]:
        """Extract list of files that have been read via read_file tool calls."""
        read_files = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.function.name == "read_file":
                        try:
                            args = json.loads(tc.function.arguments)
                            path = args.get("path", "")
                            if path and path not in read_files:
                                read_files.append(path)
                        except Exception:
                            pass
        return read_files

    def _microcompact_messages(self, messages: List[Dict[str, Any]]) -> None:
        # Keep initial system+user and latest N interactions intact.
        if len(messages) <= 2 + MICROCOMPACT_KEEP_MESSAGES:
            return

        # Proactively evict read_file tool results older than 2 steps
        # These are typically one-shot reads that the LLM already processed
        protected_tail = 4  # Don't touch the last 4 messages
        for i in range(2, len(messages) - protected_tail):
            msg = messages[i]
            content = msg.get("content")
            if (
                msg.get("role") == "tool"
                and msg.get("name") == "read_file"
                and isinstance(content, str)
                and len(content) > 500
            ):
                msg["content"] = (
                    content[:300]
                    + "\n\n[read_file result evicted — use read_file again if needed]"
                )

        # Original microcompact: fold old large tool results
        for msg in messages[2:-MICROCOMPACT_KEEP_MESSAGES]:
            content = msg.get("content")
            if (
                msg.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > MICROCOMPACT_MIN_TOOL_OUTPUT
            ):
                msg["content"] = (
                    content[:300]
                    + "\n\n[Old tool result content cleared to save context]"
                )

    def _dedup_messages(self, messages: List[Dict[str, Any]]) -> int:
        """Level 1 compact: Remove duplicate/redundant tool results.
        Keeps the latest occurrence of each unique tool result, removes older duplicates.
        Returns the number of chars removed."""
        seen_tool_results: Dict[str, int] = {}  # content hash -> index
        chars_removed = 0

        for i, msg in enumerate(messages):
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not content or len(content) < 100:
                continue

            # Use a hash of the first 200 chars as dedup key
            key = content[:200]
            if key in seen_tool_results:
                # This is a duplicate — truncate the older one
                old_idx = seen_tool_results[key]
                old_content = messages[old_idx].get("content", "")
                if len(old_content) > 200:
                    chars_removed += len(old_content) - 200
                    messages[old_idx]["content"] = (
                        old_content[:200]
                        + "\n\n[Duplicate tool result — content removed by dedup]"
                    )
            seen_tool_results[key] = i

        return chars_removed

    async def _create_chat_completion_with_retry(self, **api_kwargs):
        last_err = None
        for attempt in range(1, MODEL_RETRY_COUNT + 2):
            try:
                return await self.client.chat.completions.create(**api_kwargs)
            except Exception as e:
                last_err = e
                if attempt >= MODEL_RETRY_COUNT + 1:
                    raise
                await self._log(
                    f"Model call failed (attempt {attempt}), retrying: {str(e)}"
                )
                await asyncio.sleep(min(2 * attempt, 5))
        raise last_err

    async def run_from_messages(self, messages: List[Dict[str, Any]], node_id: str = None, max_steps: int = 30, tools: List = None) -> tuple:
        """Continue an existing session from the given messages list.
        Returns (final_text, messages) so the caller can inspect/continue the session.
        """
        if tools is None:
            allowed_tool_names = set(self.get_tool_names())
            tools = [TOOL_REGISTRY[n]["schema"] for n in allowed_tool_names if n in TOOL_REGISTRY]
        else:
            # Derive allowed tool names from the provided tools list (supports both dict and object schemas)
            allowed_tool_names = set()
            for t in tools:
                if isinstance(t, dict) and "function" in t and "name" in t["function"]:
                    allowed_tool_names.add(t["function"]["name"])
                elif hasattr(t, 'function') and hasattr(t.function, 'name'):
                    allowed_tool_names.add(t.function.name)

        tool_result_cache: Dict[str, str] = {}
        step = 0
        while step < max_steps:
            # 1. Two-level Auto-Compact: Prevent context overflow
            total_chars = self._estimate_context_chars(messages)

            # Level 1: Dedup — remove duplicate tool results (at 70% threshold)
            if total_chars > MAX_CONTEXT_CHARS * DEDUP_THRESHOLD:
                chars_removed = self._dedup_messages(messages)
                if chars_removed > 0:
                    total_chars = self._estimate_context_chars(messages)
                    await self._log(f"[Dedup] Removed {chars_removed} chars of duplicate tool results. Context now: {total_chars}", node_id=node_id)

            # Level 2: Summarize — ask LLM for a summary (at 85% threshold)
            if total_chars > MAX_CONTEXT_CHARS * SUMMARIZE_THRESHOLD:
                await self._log(f"Triggering Auto-Compact (Step {step}, Chars: {total_chars})...", node_id=node_id)

                # Extract list of files already read to preserve in summary
                read_files = self._extract_read_files(messages)
                read_files_context = ""
                if read_files:
                    read_files_context = "\n\nFiles you have already read (do NOT re-read these unless absolutely necessary):\n" + "\n".join([f"- {f}" for f in read_files[:20]])

                summary_prompt = (
                    "You have been working on this task for several steps. "
                    "Please provide a concise summary (max 300 words) of what you have accomplished so far, "
                    "the current blockers or errors, and your immediate next plan."
                    f"{read_files_context}"
                )

                # Ask the model to summarize the current progress
                summary_messages = messages + [{"role": "user", "content": summary_prompt}]
                try:
                    summary_response = await self._create_chat_completion_with_retry(
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
            self._microcompact_messages(messages)

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

            response = await self._create_chat_completion_with_retry(**api_kwargs)
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))

            if DEBUG_MODE:
                reply_content = message.content or ""
                await self._log(f"[DEBUG] Model reply:\n{reply_content}", node_id=node_id)
                # Full LLM reply to debug log file (never truncated)
                try:
                    from run_compilation_cli import debug_logger
                    if debug_logger:
                        debug_logger.log("LLM_REPLY", reply_content)
                except ImportError:
                    pass

            if message.tool_calls:
                any_cache_miss = False

                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name

                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    await self._log(f"Calling tool: `{tool_name}` with args: {json.dumps(tool_args, indent=2)}", node_id=node_id)

                    # Look up and execute the actual tool function from the registry
                    cache_key = f"{tool_name}::{json.dumps(tool_args, ensure_ascii=False, sort_keys=True)}"
                    is_cache_hit = False
                    if tool_name in TOOL_REGISTRY and tool_name in allowed_tool_names:
                        if tool_name in READONLY_CACHEABLE_TOOLS and cache_key in tool_result_cache:
                            tool_result = tool_result_cache[cache_key]
                            is_cache_hit = True
                            if DEBUG_MODE:
                                await self._log(f"[DEBUG] Reusing cached tool result for `{tool_name}`", node_id=node_id)
                        else:
                            any_cache_miss = True
                            tool_func = TOOL_REGISTRY[tool_name]["func"]
                            try:
                                # Dynamically call the corresponding async function
                                tool_result = await tool_func(**tool_args)
                                if tool_name in READONLY_CACHEABLE_TOOLS:
                                    tool_result_cache[cache_key] = str(tool_result)
                            except Exception as e:
                                tool_result = f"Tool execution error: {str(e)}"
                    else:
                        any_cache_miss = True
                        tool_result = f"Error: Tool '{tool_name}' not permitted or not found."

                    # 3. Tool Output Budget: Truncate long tool outputs
                    tool_result_str = str(tool_result)
                    if len(tool_result_str) > MAX_TOOL_OUTPUT_LENGTH:
                        tool_result_str = tool_result_str[:MAX_TOOL_OUTPUT_LENGTH] + "\n... [Output truncated due to length. Please use grep or narrow your search.]"

                    if DEBUG_MODE:
                        await self._log(f"Tool `{tool_name}` result length: {len(tool_result_str)} chars", node_id=node_id)
                        # Log tool output to debug file
                        try:
                            from run_compilation_cli import debug_logger
                            if debug_logger:
                                # For list_directory, log only first 2000 chars
                                if tool_name == "list_directory":
                                    log_content = tool_result_str[:2000]
                                    if len(tool_result_str) > 2000:
                                        log_content += f"\n... [list_directory output truncated in log, total {len(tool_result_str)} chars]"
                                    debug_logger.log(f"TOOL_RESULT[{tool_name}]", log_content)
                                else:
                                    debug_logger.log(f"TOOL_RESULT[{tool_name}]", tool_result_str)
                        except ImportError:
                            pass

                    # Return the result back to the LLM
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_result_str
                    })

                # After tools are executed, advance step only if there was a cache miss
                if any_cache_miss:
                    step += 1
                continue
            else:
                await self._log("Task completed.", node_id=node_id)
                return message.content, messages

        return "Error: Agent reached maximum reasoning steps without a final conclusion.", messages

    async def run(self, user_prompt: str, node_id: str = None, max_steps: int = 30, static_context: str = None) -> str:
        """Start a new session. Backwards-compatible wrapper around run_from_messages()."""
        system_content = self.get_system_prompt()
        # Inject static context into system prompt to reduce per-step token cost
        if static_context:
            system_content = f"{system_content}\n\n{static_context}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        # Dynamically assemble the tool schemas this agent is allowed to use from the registry
        allowed_tool_names = self.get_tool_names()
        tools = [
            TOOL_REGISTRY[name]["schema"]
            for name in allowed_tool_names
            if name in TOOL_REGISTRY
        ]
        result, _ = await self.run_from_messages(messages, node_id, max_steps, tools)
        return result
