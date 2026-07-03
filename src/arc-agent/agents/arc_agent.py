import asyncio
import inspect
import json
import os
import re
from typing import Any, Awaitable, Callable, Dict, List
from openai import AsyncOpenAI
from dotenv import load_dotenv
import tiktoken
import utils
from .tools import TOOL_REGISTRY

# Load environment variables from src/arc-agent/.env if present.
# Missing .env is allowed; the process environment remains the fallback source.
_ENV_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(dotenv_path=_ENV_FILE, override=False)

# Global Debug Flag
DEBUG_MODE = int(os.environ.get("ARC_DEBUG", "1"))
MAX_TOOL_OUTPUT_LENGTH = int(os.environ.get("ARC_MAX_TOOL_OUTPUT_CHARS", "10000"))
SOFT_CONTEXT_TOKENS = 60000
HARD_CONTEXT_TOKENS = 100000
MICROCOMPACT_MIN_TOOL_OUTPUT = int(os.environ.get("ARC_MICROCOMPACT_MIN_CHARS", "1000"))
MICROCOMPACT_KEEP_MESSAGES = int(os.environ.get("ARC_MICROCOMPACT_KEEP_MESSAGES", "10"))
MODEL_RETRY_COUNT = int(os.environ.get("ARC_MODEL_RETRY_COUNT", "2"))
EPHEMERAL_GUIDANCE_PREFIX = "<arc_ephemeral_guidance>"
EPHEMERAL_GUIDANCE_SUFFIX = "</arc_ephemeral_guidance>"
COMPACT_STATE_PREFIX = "<arc_compact_state>"
COMPACT_STATE_SUFFIX = "</arc_compact_state>"

# Required args per mutating tool, validated before dispatch to surface clean errors.
_REQUIRED_TOOL_ARGS: Dict[str, list] = {
    "write_file":    ["path", "content"],
    "edit_file":     ["path", "old_string", "new_string"],
    "delete_file":   ["path"],
}


class ARCAgent:
    """
    Base class for the ARC multi-agent system.
    """
    def __init__(
        self,
        agent_name: str,
        log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None] = None
    ):
        self.agent_name = agent_name
        self.model = os.environ.get("MODEL", "gpt-5.4")
        self.log_cb = log_cb
        self.client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
        )
        self._token_encoder = None

    async def _log(
        self,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        if not self.log_cb:
            return
        result = self.log_cb(agent_name or self.agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result

    def get_system_prompt(self) -> str:
        raise NotImplementedError

    def get_tool_names(self) -> List[str]:
        """
        Subclasses only need to return the list of tool names they require.
        For example: ["read_file", "write_file"]
        """
        return []

    async def _intercept_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        node_id: str | None = None,
    ) -> tuple[bool, Any]:
        """Allow subclasses to short-circuit a tool call with a synthetic result."""
        return False, None

    async def _get_stop_response_after_tool_call(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        node_id: str | None = None,
    ) -> str | None:
        """Allow subclasses to end the current session after a specific tool result."""
        return None

    async def _postprocess_messages_after_tool_call(
        self,
        messages: List[Dict[str, Any]],
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: str,
        node_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Allow subclasses to compact or rewrite the session after a tool call."""
        return messages

    async def _get_stop_response_before_final(
        self,
        final_response: str,
        node_id: str | None = None,
    ) -> str | None:
        """Allow subclasses to reject or replace a final assistant response before the session ends."""
        return None

    async def _on_assistant_message_before_tool_calls(
        self,
        assistant_text: str,
        node_id: str | None = None,
    ) -> None:
        """Allow subclasses to update state from assistant text before tool-call interception."""
        return None

    def _build_ephemeral_user_message(self, node_id: str | None = None) -> str | None:
        """Inject a single compact, replaceable memory note before each model call."""
        return None

    def _apply_ephemeral_user_message(
        self,
        messages: List[Dict[str, Any]],
        node_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        filtered_messages: List[Dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and content.startswith(EPHEMERAL_GUIDANCE_PREFIX)
            ):
                continue
            filtered_messages.append(message)

        ephemeral_message = self._build_ephemeral_user_message(node_id=node_id)
        if ephemeral_message:
            filtered_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{EPHEMERAL_GUIDANCE_PREFIX}\n"
                        f"{ephemeral_message}\n"
                        f"{EPHEMERAL_GUIDANCE_SUFFIX}"
                    ),
                }
            )
        return filtered_messages

    def _get_token_encoder(self):
        if self._token_encoder is not None:
            return self._token_encoder

        candidate_names: list[str] = []
        try:
            self._token_encoder = tiktoken.encoding_for_model(self.model)
            return self._token_encoder
        except Exception:
            pass

        normalized_model = str(self.model or "").lower()
        if normalized_model.startswith("gpt-5") or normalized_model.startswith("o"):
            candidate_names.append("o200k_base")
        candidate_names.extend(["cl100k_base", "o200k_base"])

        for name in candidate_names:
            try:
                self._token_encoder = tiktoken.get_encoding(name)
                return self._token_encoder
            except Exception:
                continue

        raise RuntimeError(f"Unable to resolve a tiktoken encoder for model `{self.model}`.")

    def _estimate_request_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools: List | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return len(self._get_token_encoder().encode(serialized))

    def _extract_read_files(self, messages: List[Dict[str, Any]]) -> List[str]:
        """Extract list of files that have been read via read_file tool calls."""
        read_files = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {}) if isinstance(tc, dict) else (tc.function if hasattr(tc, 'function') else {})
                    name = func.get("name", "") if isinstance(func, dict) else (func.name if hasattr(func, 'name') else "")
                    if name == "read_file":
                        try:
                            args_raw = func.get("arguments", "{}") if isinstance(func, dict) else (func.arguments if hasattr(func, 'arguments') else "{}")
                            args = json.loads(args_raw)
                            path = args.get("path", "")
                            if path and path not in read_files:
                                read_files.append(path)
                        except Exception:
                            pass
        return read_files

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any] | None:
        if not isinstance(text, str):
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidates = [fenced.group(1)] if fenced else []
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)
        brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
        if brace_match:
            candidates.append(brace_match.group(1))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    def _build_fallback_compact_state(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        read_files = self._extract_read_files(messages)
        latest_assistant = ""
        latest_tool = ""
        for message in reversed(messages):
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if not latest_tool and message.get("role") == "tool":
                latest_tool = content[:400]
            if not latest_assistant and message.get("role") == "assistant":
                latest_assistant = content[:400]
            if latest_tool and latest_assistant:
                break
        return {
            "task_state": "partial_progress_preserved",
            "validated_facts": [],
            "active_hypothesis": latest_assistant[:300],
            "target_files": read_files[:8],
            "completed_actions": [],
            "remaining_gaps": [],
            "latest_failure_signature": latest_tool[:300],
            "next_action": "Continue from the preserved state and gather only the next directly relevant evidence.",
        }

    async def _compact_messages_to_state(
        self,
        messages: List[Dict[str, Any]],
        node_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        read_files = self._extract_read_files(messages)
        read_files_context = ""
        if read_files:
            read_files_context = "\nFiles already read:\n" + "\n".join(f"- {path}" for path in read_files[:20])

        compact_prompt = (
            "Compress this session into one strict JSON object for continued work.\n"
            "Return JSON only with this schema:\n"
            "{\n"
            '  "task_state": "one short sentence",\n'
            '  "validated_facts": ["fact"],\n'
            '  "active_hypothesis": "one concrete current hypothesis",\n'
            '  "target_files": ["file path"],\n'
            '  "completed_actions": ["action already completed"],\n'
            '  "remaining_gaps": ["missing behavior or unresolved issue"],\n'
            '  "latest_failure_signature": "short failure summary or empty string",\n'
            '  "next_action": "next smallest step"\n'
            "}\n"
            "Rules:\n"
            "- Keep arrays short and concrete.\n"
            "- Preserve acceptance-critical facts and current ownership boundaries.\n"
            "- Do not include prose outside JSON.\n"
            f"{read_files_context}"
        )

        summary_messages = messages + [{"role": "user", "content": compact_prompt}]
        compact_state: Dict[str, Any]
        try:
            response = await self._create_chat_completion_with_retry(
                model=self.model,
                messages=summary_messages,
                temperature=0.1,
            )
            summary_content = response.choices[0].message.content or ""
            parsed = self._extract_json_object(summary_content)
            if not parsed:
                raise ValueError("Structured compact state was not valid JSON.")
            compact_state = parsed
        except Exception as exc:
            await self._log(f"Structured auto-compact fallback: {str(exc)}", node_id=node_id)
            compact_state = self._build_fallback_compact_state(messages)

        compact_state.setdefault("target_files", read_files[:8])
        compact_text = (
            f"{COMPACT_STATE_PREFIX}\n"
            f"{json.dumps(compact_state, indent=2, ensure_ascii=False)}\n"
            f"{COMPACT_STATE_SUFFIX}"
        )
        return [
            messages[0],
            messages[1],
            {"role": "assistant", "content": compact_text},
            {
                "role": "user",
                "content": (
                    "Continue from the compact state above. Preserve the acceptance gate, current ownership boundaries, "
                    "and the next smallest proof step."
                ),
            },
        ]

    async def _summarize_tool_error(self, tool_name: str, raw_output: str) -> str:
        """Summarize long build/test error output via a quick single-shot LLM call.
        Returns a concise diagnostic summary that replaces the raw output for the agent.
        """
        summary_prompt = f"""You are a build/test error diagnostician. Analyze the following {tool_name} output and produce a concise diagnostic summary.

Rules:
- Identify each distinct error (compilation error, test failure, runtime exception)
- For each error: what file/class, what line if available, what the error is, and a one-line suggestion for fixing it
- If there are multiple errors, list them in order
- Keep the total summary under 2000 characters
- Format as plain text, no markdown

Output from {tool_name}:
{raw_output[:100000]}"""

        try:
            response = await self._create_chat_completion_with_retry(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a concise build/test error diagnostician. Analyze error output and summarize: what failed, where, why, and how to fix it."},
                    {"role": "user", "content": summary_prompt}
                ],
                temperature=0.1,
                max_tokens=120000
            )
            summary = response.choices[0].message.content.strip()
            # Return summary with original exit code preserved
            exit_code_line = ""
            for line in raw_output.split('\n'):
                if line.strip().startswith("Exit Code:"):
                    exit_code_line = line.strip()
                    break
            header = f"{exit_code_line}\n" if exit_code_line else ""
            return f"{header}[LLM Diagnostic Summary]\n{summary}\n\n[Raw output: {len(raw_output)} chars, summarized for clarity]"
        except Exception as e:
            # If summarization fails, fall back to truncated raw output
            return raw_output[:MAX_TOOL_OUTPUT_LENGTH]

    def _microcompact_messages(self, messages: List[Dict[str, Any]]) -> None:
        # Final override: never compact read_file results independently.
        # File contents stay intact unless full-session structured compaction is triggered.
        if len(messages) <= 2 + MICROCOMPACT_KEEP_MESSAGES:
            return

        for msg in messages[2:-MICROCOMPACT_KEEP_MESSAGES]:
            content = msg.get("content")
            if (
                msg.get("role") == "tool"
                and msg.get("name") != "read_file"
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
        # Auto-switch token-limit param for OpenAI models only
        model = api_kwargs.get("model", self.model).lower()
        is_o_series = bool(re.match(r'^o\d', model))
        is_gpt_series = model.startswith("gpt-")
        if is_o_series or is_gpt_series:
            token_value = None
            for param in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                if param in api_kwargs:
                    token_value = api_kwargs.pop(param)
                    break
            if token_value is not None:
                api_kwargs["max_completion_tokens" if is_o_series else "max_tokens"] = token_value

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

    async def run_from_messages(
        self,
        messages: List[Dict[str, Any]],
        node_id: str = None,
        max_steps: int = 30,
        tools: List = None,
    ) -> tuple:
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

        step = 0
        while step < max_steps:
            self._microcompact_messages(messages)
            projected_messages = self._apply_ephemeral_user_message(messages, node_id=node_id)
            total_tokens = self._estimate_request_tokens(projected_messages, tools=tools)

            # Level 1: dedup once the soft budget is exceeded.
            if total_tokens > SOFT_CONTEXT_TOKENS:
                chars_removed = self._dedup_messages(messages)
                if chars_removed > 0:
                    projected_messages = self._apply_ephemeral_user_message(messages, node_id=node_id)
                    total_tokens = self._estimate_request_tokens(projected_messages, tools=tools)
                    await self._log(
                        f"[Dedup] Removed {chars_removed} chars of duplicate tool results. Context now: {total_tokens} tokens.",
                        node_id=node_id,
                    )

            # Level 2: replace prior history with a structured compact state at the hard budget.
            if total_tokens > HARD_CONTEXT_TOKENS:
                await self._log(
                    f"Triggering structured auto-compact (Step {step}, Tokens: {total_tokens})...",
                    node_id=node_id,
                )

                messages = await self._compact_messages_to_state(messages, node_id=node_id)

            messages = self._apply_ephemeral_user_message(messages, node_id=node_id)

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
                # Log last message sent to model (truncated)
                last_msg = messages[-1].get("content", "")
                if isinstance(last_msg, str) and len(last_msg) > 500:
                    display_msg = last_msg[:500] + f"\n... [input truncated, total {len(last_msg)} chars]"
                else:
                    display_msg = last_msg
                await self._log(f"[DEBUG] Input to model:\n{display_msg}", node_id=node_id)

            if utils.prompt_dump_logger:
                try:
                    prompt_payload = {
                        "agent_name": self.agent_name,
                        "node_id": node_id,
                        "step": step + 1,
                        "request": api_kwargs,
                    }
                    dump_path = utils.prompt_dump_logger.dump(
                        agent_name=self.agent_name,
                        node_id=node_id,
                        step=step + 1,
                        payload=prompt_payload,
                    )
                    if DEBUG_MODE:
                        await self._log(f"[DEBUG] Prompt dump saved: {dump_path}", node_id=node_id)
                except Exception as exc:
                    await self._log(f"[DEBUG] Failed to dump prompt payload: {str(exc)}", node_id=node_id)

            response = await self._create_chat_completion_with_retry(**api_kwargs)
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))

            if DEBUG_MODE:
                reply_content = message.content or ""
                await self._log(f"[DEBUG] Model reply:\n{reply_content}", node_id=node_id)
                # Full LLM reply to debug log file (never truncated)
                if utils.debug_logger:
                    utils.debug_logger.log("LLM_REPLY", reply_content)

            if message.tool_calls:
                await self._on_assistant_message_before_tool_calls(
                    assistant_text=message.content or "",
                    node_id=node_id,
                )

                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name

                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    # Log tool call with args (truncate write_file content)
                    args_str = json.dumps(tool_args, indent=2, ensure_ascii=False)
                    if tool_name == "write_file" and len(args_str) > 500:
                        args_str = args_str[:500] + f"\n... [args truncated, total {len(args_str)} chars]"
                    elif len(args_str) > 1000:
                        args_str = args_str[:1000] + f"\n... [args truncated, total {len(args_str)} chars]"
                    await self._log(f"Calling tool: `{tool_name}` with args: {args_str}", node_id=node_id)

                    # Validate required args for mutating tools before dispatch
                    _missing = [
                        p for p in _REQUIRED_TOOL_ARGS.get(tool_name, [])
                        if p not in tool_args
                    ]
                    if _missing:
                        tool_result = (
                            f"Tool call error: `{tool_name}` is missing required argument(s): "
                            f"{', '.join(_missing)}. Re-issue the call with all required arguments."
                        )
                    else:
                        intercepted, intercepted_result = await self._intercept_tool_call(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            node_id=node_id,
                        )
                        if intercepted:
                            tool_result = intercepted_result
                        else:
                            if tool_name in TOOL_REGISTRY and tool_name in allowed_tool_names:
                                tool_func = TOOL_REGISTRY[tool_name]["func"]
                                try:
                                    tool_result = await tool_func(**tool_args)
                                except Exception as e:
                                    tool_result = f"Tool execution error: {str(e)}"
                            else:
                                tool_result = f"Error: Tool '{tool_name}' not permitted or not found."

                    # 3. Tool Output Budget: Summarize long error outputs, then truncate
                    tool_result_str = str(tool_result)

                    # Summarize very long build/test outputs; otherwise keep raw output intact.
                    if tool_name in ("run_build", "run_tests") and len(tool_result_str) > MAX_TOOL_OUTPUT_LENGTH:
                        exit_code_match = re.search(r'Exit Code:\s*(\d+)', tool_result_str)
                        if exit_code_match and exit_code_match.group(1) != "0":
                            tool_result_str = await self._summarize_tool_error(tool_name, tool_result_str)

                    if len(tool_result_str) > MAX_TOOL_OUTPUT_LENGTH:
                        tool_result_str = tool_result_str[:MAX_TOOL_OUTPUT_LENGTH] + "\n... [Output truncated due to length. Please use grep or narrow your search.]"

                    if DEBUG_MODE:
                        # Log tool result: skip read_file content, truncate others
                        if tool_name == "read_file":
                            await self._log(
                                f"Tool `read_file` result: {len(tool_result_str)} chars (content not shown)",
                                node_id=node_id,
                            )
                        else:
                            display_result = tool_result_str
                            if len(display_result) > 500:
                                display_result = display_result[:500] + f"\n... [result truncated, total {len(tool_result_str)} chars]"
                            await self._log(f"Tool `{tool_name}` result:\n{display_result}", node_id=node_id)
                        # Full tool output to debug log file (never truncated)
                        if utils.debug_logger:
                            utils.debug_logger.log(f"TOOL_RESULT[{tool_name}]", tool_result_str)

                    # Return the result back to the LLM
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_result_str
                    })

                    if tool_name == "edit_file" and hasattr(self, "notify_edit_failure"):
                        try:
                            self.notify_edit_failure(str(tool_args.get("path", "")).strip(), tool_result_str)
                        except Exception:
                            pass
                    if hasattr(self, "drain_forced_followup_user_messages"):
                        try:
                            forced_messages = self.drain_forced_followup_user_messages()
                        except Exception:
                            forced_messages = []
                        if forced_messages:
                            for forced_message in forced_messages:
                                messages.append({"role": "user", "content": forced_message})
                    messages = await self._postprocess_messages_after_tool_call(
                        messages=messages,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        tool_result=tool_result_str,
                        node_id=node_id,
                    )

                    stop_response = await self._get_stop_response_after_tool_call(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        tool_result=tool_result_str,
                        node_id=node_id,
                    )
                    if stop_response is not None:
                        await self._log("Agent session stopped by tool policy.", node_id=node_id)
                        return stop_response, messages

                step += 1
                continue

            stop_response = await self._get_stop_response_before_final(
                final_response=message.content or "",
                node_id=node_id,
            )
            if stop_response is not None:
                messages.append({"role": "user", "content": stop_response})
                step += 1
                continue
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
