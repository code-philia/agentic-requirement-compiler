from __future__ import annotations

import inspect
import json
import os
import re
import time
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from agents.context import AgentRuntimeContext
from tools.logging import format_json_for_log, log_to_logger


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
DEFAULT_RECURSION_LIMIT = 5000


async def ainvoke_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    logger: Any | None = None,
    label: str = "",
    log_cb: LogCallback | None = None,
    stream: bool | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    run_label = label or getattr(agent, "name", "") or context.phase or "stage-agent"
    log_to_logger(logger, "AGENT_CALL_START", label=run_label, thread_id=thread_id, body=message)
    await _emit_log(
        log_cb,
        run_label,
        f"deep-agent call start: thread_id={thread_id}",
        node_id=context.node_id,
    )

    if _should_stream(stream):
        stream_payload = await _try_astream_stage_agent(
            agent,
            message=message,
            context=context,
            thread_id=thread_id,
            run_label=run_label,
            log_cb=log_cb,
            logger=logger,
        )
        if stream_payload is not None:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            log_to_logger(
                logger,
                "AGENT_CALL_END",
                label=run_label,
                thread_id=thread_id,
                body=f"duration_ms={duration_ms:.1f}\n{format_json_for_log(stream_payload)}",
            )
            await _emit_log(
                log_cb,
                run_label,
                f"deep-agent call end: duration_ms={duration_ms:.1f}, payload={format_json_for_log(stream_payload)}",
                node_id=context.node_id,
            )
            return stream_payload

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": message}]},
        context=context,
        config=build_agent_config(thread_id),
    )
    await _log_agent_trace(log_cb, result, label=run_label, thread_id=thread_id, node_id=context.node_id)
    payload = extract_payload(result)
    duration_ms = (time.perf_counter() - started_at) * 1000.0
    log_to_logger(
        logger,
        "AGENT_CALL_END",
        label=run_label,
        thread_id=thread_id,
        body=f"duration_ms={duration_ms:.1f}\n{format_json_for_log(payload)}",
    )
    await _emit_log(
        log_cb,
        run_label,
        f"deep-agent call end: duration_ms={duration_ms:.1f}, payload={format_json_for_log(payload)}",
        node_id=context.node_id,
    )
    return payload


def extract_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structured_response") if isinstance(result, dict) else None
    normalized = _normalize_payload_value(structured)
    if normalized is not None:
        return normalized

    final_text = _extract_final_message_text(result)
    parsed = parse_json_payload(final_text)
    if parsed is not None:
        return parsed
    return {"summary": final_text, "_raw_final_message": _stringify_final_message(result)}


def parse_json_payload(text: str) -> dict[str, Any] | None:
    current = (text or "").strip()
    for _ in range(3):
        if not current:
            return None
        next_string: str | None = None
        for candidate in _json_candidates(current):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_payload_value(payload)
            if normalized is not None:
                return normalized
            if isinstance(payload, str) and payload.strip():
                next_string = payload.strip()
        if next_string is None:
            return None
        current = next_string
    return None


def _normalize_payload_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump()
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {"items": dumped}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return None


def _extract_final_message_text(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        role = _message_role(message)
        if role in {"human", "tool"}:
            continue
        text = _message_content_text(message)
        if text:
            return text
    return ""


def _stringify_final_message(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return ""
    for message in reversed(messages):
        if _message_role(message) in {"human", "tool"}:
            continue
        return _truncate_text(_stringify_tool_args(_message_to_debug_payload(message)), max_chars=4000)
    return ""


def _message_to_debug_payload(message: Any) -> Any:
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if isinstance(message, dict):
        return message
    return {
        "type": _message_role(message),
        "content": getattr(message, "content", None),
        "tool_calls": getattr(message, "tool_calls", None),
    }


async def _try_astream_stage_agent(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    run_label: str,
    log_cb: LogCallback | None,
    logger: Any | None,
) -> dict[str, Any] | None:
    if _should_use_sync_stream_v3():
        sync_payload = _try_stream_stage_agent_sync(
            agent,
            message=message,
            context=context,
            thread_id=thread_id,
            run_label=run_label,
            log_cb=log_cb,
            logger=logger,
        )
        if sync_payload is not None:
            return sync_payload

    if not hasattr(agent, "astream_events"):
        await _emit_log(log_cb, run_label, "deep-agent streaming is unavailable; falling back to ainvoke.", node_id=context.node_id)
        return None

    await _emit_log(log_cb, run_label, "deep-agent stream start.", node_id=context.node_id)
    final_state: dict[str, Any] | None = None
    try:
        event_stream = agent.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=build_agent_config(thread_id),
            version=os.environ.get("ARC_AGENT_STREAM_VERSION", "v2"),
        )
        if inspect.isawaitable(event_stream):
            event_stream = await event_stream
        async for event in event_stream:
            maybe_state = await _log_stream_event(
                log_cb,
                event,
                label=run_label,
                node_id=context.node_id,
            )
            if isinstance(maybe_state, dict):
                final_state = maybe_state
    except Exception as exc:
        await _emit_log(
            log_cb,
            run_label,
            f"deep-agent stream failed; falling back to ainvoke. error={exc}",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_FALLBACK", label=run_label, thread_id=thread_id, body=str(exc))
        return None

    if final_state is None:
        await _emit_log(
            log_cb,
            run_label,
            "deep-agent stream ended without final state; falling back to ainvoke.",
            status="warning",
            node_id=context.node_id,
        )
        return None

    await _log_agent_trace(log_cb, final_state, label=run_label, thread_id=thread_id, node_id=context.node_id)
    return extract_payload(final_state)


def _try_stream_stage_agent_sync(
    agent: Any,
    *,
    message: str,
    context: AgentRuntimeContext,
    thread_id: str,
    run_label: str,
    log_cb: LogCallback | None,
    logger: Any | None,
) -> dict[str, Any] | None:
    if not hasattr(agent, "stream_events"):
        return None
    try:
        stream = agent.stream_events(
            {"messages": [{"role": "user", "content": message}]},
            context=context,
            config=build_agent_config(thread_id),
            version=os.environ.get("ARC_AGENT_STREAM_VERSION", "v3"),
        )
        extensions = getattr(stream, "extensions", {}) or {}
        stream_names = [name for name in ("messages", "values", "subagents") if name in extensions]
        if not stream_names or not hasattr(stream, "interleave"):
            return None

        _emit_log_sync(log_cb, run_label, "deep-agent stream start.", node_id=context.node_id)
        final_state: dict[str, Any] | None = None
        seen_message_count = 0
        for stream_name, item in stream.interleave(*stream_names):
            if stream_name == "messages":
                text = _typed_stream_item_text(item)
                if text:
                    _emit_log_sync(
                        log_cb,
                        run_label,
                        f"model> {_truncate_text(text, max_chars=1200)}",
                        node_id=context.node_id,
                    )
                continue

            if stream_name == "subagents":
                _log_subagent_stream_item_sync(log_cb, item, label=run_label, node_id=context.node_id)
                continue

            if stream_name == "values" and isinstance(item, dict):
                messages = item.get("messages")
                if isinstance(messages, list) and len(messages) > seen_message_count:
                    new_messages = messages[seen_message_count:]
                    seen_message_count = len(messages)
                    formatted = _format_message_trace(new_messages)
                    if formatted:
                        _emit_log_sync(
                            log_cb,
                            run_label,
                            f"stream messages:\n{formatted}",
                            node_id=context.node_id,
                        )
                if _is_agent_state_with_payload(item):
                    final_state = item

        latest = getattr(stream, "output", None)
        if isinstance(latest, dict) and _is_agent_state_with_payload(latest):
            final_state = latest
        if final_state is None:
            _emit_log_sync(
                log_cb,
                run_label,
                "deep-agent stream ended without final state; falling back to ainvoke.",
                status="warning",
                node_id=context.node_id,
            )
            return None
        _log_agent_trace_sync(log_cb, final_state, label=run_label, thread_id=thread_id, node_id=context.node_id)
        return extract_payload(final_state)
    except Exception as exc:
        _emit_log_sync(
            log_cb,
            run_label,
            f"deep-agent stream failed; falling back to ainvoke. error={exc}",
            status="warning",
            node_id=context.node_id,
        )
        log_to_logger(logger, "AGENT_STREAM_FALLBACK", label=run_label, thread_id=thread_id, body=str(exc))
        return None


async def _log_stream_event(
    log_cb: LogCallback | None,
    event: Any,
    *,
    label: str,
    node_id: str,
) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        text = _typed_stream_item_text(event)
        if text:
            await _emit_log(log_cb, label, f"stream: {text}", node_id=node_id)
        return None

    event_name = str(event.get("event", "") or "")
    name = str(event.get("name", "") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if event_name in {"on_chat_model_stream", "on_llm_stream"}:
        chunk_text = _message_content_text(data.get("chunk"))
        if chunk_text:
            await _emit_log(log_cb, label, f"model> {_truncate_text(chunk_text, max_chars=1200)}", node_id=node_id)
        return None

    if event_name in {"on_chat_model_end", "on_llm_end"}:
        output = data.get("output")
        message_text = _message_content_text(output)
        if message_text:
            await _emit_log(log_cb, label, f"model-final> {_truncate_text(message_text, max_chars=2000)}", node_id=node_id)
        return None

    if event_name == "on_tool_start":
        await _emit_log(
            log_cb,
            label,
            f"tool-call> {name or 'unknown'} args={_truncate_text(_stringify_tool_args(data.get('input')), max_chars=1000)}",
            node_id=node_id,
        )
        return None

    if event_name == "on_tool_end":
        await _emit_log(
            log_cb,
            label,
            f"tool-result> {name or 'unknown'} result={_truncate_text(_stringify_tool_args(data.get('output')), max_chars=2000)}",
            node_id=node_id,
        )
        return None

    if event_name in {"on_chain_end", "on_graph_end"}:
        output = data.get("output")
        if isinstance(output, dict) and _is_agent_state_with_payload(output):
            return output
    return None


def _is_agent_state_with_payload(state: dict[str, Any]) -> bool:
    structured = state.get("structured_response")
    if structured is not None:
        return True
    messages = state.get("messages")
    return isinstance(messages, list) and any(_message_role(message) not in {"human", "tool"} for message in messages)


async def _log_agent_trace(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    thread_id: str,
    node_id: str,
) -> None:
    if not _should_log_full_agent_trace():
        return
    formatted = _format_message_trace(result.get("messages", []) if isinstance(result, dict) else [])
    if not formatted:
        await _emit_log(log_cb, label, f"agent trace is empty: thread_id={thread_id}", node_id=node_id)
        return
    await _emit_log(log_cb, label, f"agent trace: thread_id={thread_id}\n{formatted}", node_id=node_id)


def _log_agent_trace_sync(
    log_cb: LogCallback | None,
    result: dict[str, Any],
    *,
    label: str,
    thread_id: str,
    node_id: str,
) -> None:
    if not _should_log_full_agent_trace():
        return
    formatted = _format_message_trace(result.get("messages", []) if isinstance(result, dict) else [])
    if not formatted:
        return
    _emit_log_sync(log_cb, label, f"agent trace: thread_id={thread_id}\n{formatted}", node_id=node_id)


def _should_log_full_agent_trace() -> bool:
    return str(os.environ.get("ARC_DEBUG_AGENT_TRACE", "")).strip().lower() in {"1", "true", "yes", "on"}


def _format_message_trace(messages: list[Any]) -> str:
    if not isinstance(messages, list) or not messages:
        return ""
    blocks: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = _message_role(message)
        if role == "human":
            continue
        lines = [f"[{index}] {role.upper()}"]
        tool_calls = _extract_tool_calls(message)
        if tool_calls:
            lines.append("tool_calls:")
            for tool_index, call in enumerate(tool_calls, start=1):
                tool_name = str(call.get("name", "") or call.get("tool_name", "") or "unknown")
                tool_id = str(call.get("id", "") or call.get("tool_call_id", "") or "-")
                tool_args = _truncate_text(_stringify_tool_args(call.get("args")), max_chars=1000)
                lines.append(f"  {tool_index}. {tool_name} id={tool_id}")
                if tool_args:
                    lines.append(f"     args: {tool_args}")
        tool_result_meta = _extract_tool_result_meta(message)
        if tool_result_meta:
            lines.append(tool_result_meta)
        content = _message_content_text(message)
        if content:
            lines.append("content:")
            lines.append(_indent_block(_truncate_text(content, max_chars=2400)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _message_role(message: Any) -> str:
    role = getattr(message, "type", None) or getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("type") or message.get("role")
    if isinstance(role, str) and role:
        return role
    cls_name = message.__class__.__name__.lower()
    if "tool" in cls_name:
        return "tool"
    if "human" in cls_name:
        return "human"
    if "ai" in cls_name or "assistant" in cls_name:
        return "assistant"
    return cls_name or "message"


def _extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if isinstance(call, dict):
            normalized.append(call)
            continue
        normalized.append(
            {
                "name": getattr(call, "name", ""),
                "id": getattr(call, "id", ""),
                "args": getattr(call, "args", {}),
            }
        )
    return normalized


def _extract_tool_result_meta(message: Any) -> str:
    if _message_role(message) != "tool":
        return ""
    tool_name = getattr(message, "name", None)
    if tool_name is None and isinstance(message, dict):
        tool_name = message.get("name")
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id is None and isinstance(message, dict):
        tool_call_id = message.get("tool_call_id")
    parts = ["tool_result:"]
    if tool_name:
        parts.append(f"name={tool_name}")
    if tool_call_id:
        parts.append(f"id={tool_call_id}")
    return " ".join(parts)


def _message_content_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return _content_text(content)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [_content_text(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        for key in ("text", "content", "output_text"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        parsed = content.get("parsed")
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
        if isinstance(parsed, str) and parsed.strip():
            return parsed.strip()
        nested_parts = [_content_text(value) for value in content.values()]
        return "\n".join(part for part in nested_parts if part).strip()
    if content is None:
        return ""
    text = getattr(content, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    nested = getattr(content, "content", None)
    if nested is not None:
        return _content_text(nested)
    return str(content).strip()


def _typed_stream_item_text(item: Any) -> str:
    if isinstance(item, tuple) and item:
        parts = [_typed_stream_item_text(part) for part in item]
        return " ".join(part for part in parts if part).strip()
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = _message_content_text(item)
    if content:
        return content
    name = getattr(item, "name", None)
    status = getattr(item, "status", None)
    if name or status:
        return f"name={name or '-'} status={status or '-'}"
    return ""


def _log_subagent_stream_item_sync(
    log_cb: LogCallback | None,
    item: Any,
    *,
    label: str,
    node_id: str,
) -> None:
    name = getattr(item, "name", "") or "subagent"
    status = getattr(item, "status", "") or ""
    _emit_log_sync(log_cb, label, f"subagent> {name} status={status or '-'}", node_id=node_id)
    messages = getattr(item, "messages", None)
    if messages:
        formatted = _format_message_trace(list(messages))
        if formatted:
            _emit_log_sync(log_cb, label, f"subagent messages ({name}):\n{formatted}", node_id=node_id)
    output = getattr(item, "output", None)
    if output:
        _emit_log_sync(
            log_cb,
            label,
            f"subagent output ({name}): {_truncate_text(_stringify_tool_args(output), max_chars=2000)}",
            node_id=node_id,
        )


def _stringify_tool_args(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return repr(value)


def _truncate_text(text: str, *, max_chars: int) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 18].rstrip() + "\n...[truncated]"


def _indent_block(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) if text else prefix


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result


def _emit_log_sync(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if not inspect.isawaitable(result):
        return
    try:
        loop = None
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(result)
            return
        close = getattr(result, "close", None)
        if callable(close):
            close()
    except Exception:
        return


def _should_stream(stream: bool | None) -> bool:
    if stream is not None:
        return stream
    value = os.environ.get("ARC_AGENT_STREAM", "").strip().lower()
    if _should_disable_streaming_for_responses_api():
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return True


def _should_disable_streaming_for_responses_api() -> bool:
    force = os.environ.get("ARC_AGENT_FORCE_RESPONSES_STREAM", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return False
    override = os.environ.get("ARC_USE_RESPONSES_API", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    base_url = os.environ.get("OPENAI_API_BASE", "").strip() or os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base_url:
        return False
    host = urlparse(base_url).hostname or ""
    if host == "api.openai.com" or host.endswith(".openai.com"):
        return False
    return True


def _should_use_sync_stream_v3() -> bool:
    value = os.environ.get("ARC_AGENT_SYNC_STREAM_V3", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def build_agent_config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []

    def add(candidate: str) -> None:
        stripped = candidate.strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)

    add(text)
    for match in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        add(match)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            add(text[start : end + 1])
    return candidates
