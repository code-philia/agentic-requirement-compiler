from __future__ import annotations

import json
import os
import re
from typing import Any, Awaitable, Callable

from utils import extract_json_array_from_markdown

STRUCTURED_OUTPUT_RETRY_COUNT = int(os.environ.get("ARC_STRUCTURED_OUTPUT_RETRY_COUNT", "2"))


def extract_json_object_from_markdown(raw_output: str) -> dict[str, Any] | None:
    if not raw_output:
        return None

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_output, re.DOTALL | re.IGNORECASE)
    candidates: list[str] = []
    if fenced:
        candidates.append(fenced.group(1))
    stripped = raw_output.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    span = re.search(r"(\{\s*\"[\s\S]*\})", raw_output)
    if span:
        candidates.append(span.group(1))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


async def _emit_retry_log(
    log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None] | None,
    agent_name: str,
    node_id: str,
    attempt: int,
    last_error: str,
) -> None:
    if not log_cb:
        return
    message = (
        f"Structured output invalid ({last_error}). "
        f"Retrying repair {attempt}/{STRUCTURED_OUTPUT_RETRY_COUNT}..."
    )
    result = log_cb(agent_name, message, "warning", node_id)
    if hasattr(result, "__await__"):
        await result


async def run_agent_for_json_array(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    node_id: str,
    max_steps: int,
    tools: list[Any],
    repair_prompt: str | Callable[[str], str],
    log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None] | None = None,
    log_agent: str = "",
    repair_max_steps: int = 2,
    validate: Callable[[list[dict[str, Any]]], str | None] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], str]:
    last_output = ""
    last_error = "Response did not contain a parseable JSON array."
    working_messages = list(messages)

    for attempt in range(STRUCTURED_OUTPUT_RETRY_COUNT + 1):
        if attempt == 0:
            last_output, working_messages = await agent.run_from_messages(
                working_messages,
                node_id=node_id,
                max_steps=max_steps,
                tools=tools,
            )
        else:
            await _emit_retry_log(log_cb, log_agent or getattr(agent, "agent_name", "Agent"), node_id, attempt, last_error)
            prompt = repair_prompt(last_error) if callable(repair_prompt) else repair_prompt
            working_messages.append({"role": "user", "content": prompt})
            last_output, working_messages = await agent.run_from_messages(
                working_messages,
                node_id=node_id,
                max_steps=repair_max_steps,
                tools=[],
            )

        parsed = extract_json_array_from_markdown(last_output)
        if not parsed:
            last_error = "Response did not contain a parseable JSON array in a ```json block."
            continue

        object_entries = [item for item in parsed if isinstance(item, dict)]
        if not object_entries:
            last_error = "JSON array did not contain any object entries."
            continue

        if validate:
            validation_error = validate(object_entries)
            if validation_error:
                last_error = validation_error
                continue

        return object_entries, working_messages, last_output

    return None, working_messages, last_output


async def run_agent_for_json_object(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    node_id: str,
    max_steps: int,
    tools: list[Any],
    repair_prompt: str | Callable[[str], str],
    log_cb: Callable[[str, str, str | None, str | None], Awaitable[None] | None] | None = None,
    log_agent: str = "",
    repair_max_steps: int = 2,
    validate: Callable[[dict[str, Any]], str | None] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    last_output = ""
    last_error = "Response did not contain a parseable JSON object."
    working_messages = list(messages)

    for attempt in range(STRUCTURED_OUTPUT_RETRY_COUNT + 1):
        if attempt == 0:
            last_output, working_messages = await agent.run_from_messages(
                working_messages,
                node_id=node_id,
                max_steps=max_steps,
                tools=tools,
            )
        else:
            await _emit_retry_log(log_cb, log_agent or getattr(agent, "agent_name", "Agent"), node_id, attempt, last_error)
            prompt = repair_prompt(last_error) if callable(repair_prompt) else repair_prompt
            working_messages.append({"role": "user", "content": prompt})
            last_output, working_messages = await agent.run_from_messages(
                working_messages,
                node_id=node_id,
                max_steps=repair_max_steps,
                tools=[],
            )

        parsed = extract_json_object_from_markdown(last_output)
        if not parsed:
            last_error = "Response did not contain a parseable JSON object in a ```json block."
            continue

        if validate:
            validation_error = validate(parsed)
            if validation_error:
                last_error = validation_error
                continue

        return parsed, working_messages, last_output

    return None, working_messages, last_output
