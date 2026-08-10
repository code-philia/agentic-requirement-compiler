from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api


class CompatibleChatOpenAI(ChatOpenAI):
    """ChatOpenAI with a fallback for providers returning SSE text for Responses."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if self._use_responses_api(payload):
            return await self._agenerate_from_sse_text(payload)
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except AttributeError as exc:
            if not _is_responses_sse_attribute_error(exc):
                raise
            return await self._agenerate_from_sse_text(payload)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if self._use_responses_api(payload):
            return self._generate_from_sse_text(payload)
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except AttributeError as exc:
            if not _is_responses_sse_attribute_error(exc):
                raise
            return self._generate_from_sse_text(payload)

    async def _agenerate_from_sse_text(self, payload: dict[str, Any]) -> ChatResult:
        raw_response = await self.root_async_client.responses.with_raw_response.create(**payload)
        parsed = raw_response.parse()
        if not isinstance(parsed, str):
            return _construct_lc_result_from_responses_api(parsed, output_version=self.output_version)
        return _chat_result_from_sse_text(parsed)

    def _generate_from_sse_text(self, payload: dict[str, Any]) -> ChatResult:
        raw_response = self.root_client.responses.with_raw_response.create(**payload)
        parsed = raw_response.parse()
        if not isinstance(parsed, str):
            return _construct_lc_result_from_responses_api(parsed, output_version=self.output_version)
        return _chat_result_from_sse_text(parsed)


def _is_responses_sse_attribute_error(exc: AttributeError) -> bool:
    return "'str' object has no attribute 'error'" in str(exc)


def _chat_result_from_sse_text(payload: str) -> ChatResult:
    parsed = _parse_responses_sse(payload)
    message = AIMessage(
        content=parsed["content"],
        tool_calls=parsed["tool_calls"],
        invalid_tool_calls=parsed["invalid_tool_calls"],
        response_metadata={"model_provider": "openai", "sse_text_fallback": True},
    )
    return ChatResult(generations=[ChatGeneration(message=message)])


def _parse_responses_sse(payload: str) -> dict[str, Any]:
    text_by_output_index: dict[int, list[str]] = {}
    tool_calls: list[dict[str, Any]] = []
    invalid_tool_calls: list[dict[str, Any]] = []
    current_event = ""

    for raw_line in str(payload or "").splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = _loads_json_line(line.split(":", 1)[1].strip())
        if not isinstance(data, dict):
            continue
        if current_event == "response.output_text.done":
            text = str(data.get("text", "") or "")
            if text:
                output_index = data.get("output_index")
                if not isinstance(output_index, int):
                    output_index = 0
                text_by_output_index.setdefault(output_index, []).append(text)
            continue
        if current_event != "response.output_item.done":
            continue
        item = data.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        _append_tool_call(item, tool_calls, invalid_tool_calls)

    content = ""
    if text_by_output_index:
        content = "\n".join(text_by_output_index[max(text_by_output_index)])
    return {
        "content": content,
        "tool_calls": tool_calls,
        "invalid_tool_calls": invalid_tool_calls,
    }


def _append_tool_call(
    item: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    invalid_tool_calls: list[dict[str, Any]],
) -> None:
    name = str(item.get("name", "") or "")
    call_id = str(item.get("call_id") or item.get("id") or "")
    arguments = item.get("arguments", "{}")
    if isinstance(arguments, dict):
        args = arguments
    else:
        args = None
    if args is not None:
        tool_calls.append(
            {
                "type": "tool_call",
                "name": name,
                "args": args,
                "id": call_id,
            }
        )
        return
    try:
        args = json.loads(str(arguments or "{}"), strict=False)
        if not isinstance(args, dict):
            args = {"__arg1": args}
    except JSONDecodeError as exc:
        invalid_tool_calls.append(
            {
                "type": "invalid_tool_call",
                "name": name,
                "args": str(arguments or ""),
                "id": call_id,
                "error": str(exc),
            }
        )
        return
    tool_calls.append(
        {
            "type": "tool_call",
            "name": name,
            "args": args,
            "id": call_id,
        }
    )


def _loads_json_line(value: str) -> Any:
    if not value or value == "[DONE]":
        return None
    try:
        return json.loads(value)
    except JSONDecodeError:
        return None
