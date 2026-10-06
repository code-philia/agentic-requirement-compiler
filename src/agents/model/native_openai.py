"""Tool-free model requests using the native OpenAI SDK."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from agents.model.retries import MODEL_MAX_RETRIES, retry_model_call, without_sdk_retries

from agents.model.openai_api_adapter import (
    normalize_model_api_exception, resolve_openai_adapter_config,
)


class NativeOpenAIModel:
    def __init__(self, model_name: str, *, api_mode: str | None = None):
        self.config = resolve_openai_adapter_config(model_name=model_name, api_mode=api_mode)

    async def generate(self, messages: list[dict[str, Any]], *, stage: str = "MODEL",
                       workspace_root: str | Path | None = None) -> str:
        config = self.config
        kwargs: dict[str, Any] = {"max_retries": MODEL_MAX_RETRIES}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        # Use the SDK transport directly, including its HTTPX2 implementation
        # when installed; never let LangChain construct another transport.
        transport = getattr(openai, "DefaultAsyncHttpx2Client", openai.DefaultAsyncHttpxClient)
        kwargs["http_client"] = transport()
        try:
            async with openai.AsyncOpenAI(**kwargs) as client:
                effort = os.getenv("ARC_OPENAI_REASONING_EFFORT", "").strip()
                if config.api_mode == "chat_completions":
                    options: dict[str, Any] = {}
                    if effort:
                        options["reasoning_effort"] = effort
                    if os.getenv("ARC_OPENAI_DISABLE_THINKING", "").lower() in {"1", "true", "yes"}:
                        options["extra_body"] = {"thinking": {"type": "disabled"}}
                    request = dict(model=config.model_name, messages=messages, stream=False, **options)
                    log_path = _log_input(stage, workspace_root, request, config.api_mode, config.base_url)
                    response = await client.chat.completions.create(**request)
                    _log_output(log_path, response)
                    if not response.choices:
                        raise ValueError("Model returned no chat completion choices")
                    message = response.choices[0].message
                    if message.refusal:
                        raise ValueError(f"Model refused the request: {message.refusal}")
                    if response.choices[0].finish_reason == "length":
                        raise ValueError(f"Model output truncated (finish_reason=length); inspect {log_path}")
                    if not message.content:
                        raise ValueError(f"Model returned empty content; inspect {log_path}")
                    return message.content
                options = {"reasoning": {"effort": effort}} if effort else {}
                request = dict(model=config.model_name, input=messages, stream=False, **options)
                log_path = _log_input(stage, workspace_root, request, config.api_mode, config.base_url)
                response = await client.responses.create(**request)
                _log_output(log_path, response)
                if isinstance(response, str):
                    return _sse_text(response)
                if response.status == "incomplete":
                    raise ValueError(f"Model output incomplete: {response.incomplete_details}; inspect {log_path}")
                if not response.output_text:
                    raise ValueError(f"Model returned empty output; inspect {log_path}")
                return response.output_text
        except Exception as exc:
            wrapped = normalize_model_api_exception(exc, api_mode=config.api_mode, model=config.model_name)
            if wrapped is exc:
                raise
            raise wrapped from exc


def _sse_text(payload: str) -> str:
    """Compatibility for gateways returning SSE despite stream=False."""
    completed: dict[int, str] = {}
    deltas: dict[int, str] = {}
    for line in payload.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        index = event.get("output_index", 0)
        if kind == "response.output_text.done":
            completed[index] = event.get("text", "")
        elif kind == "response.output_text.delta":
            deltas[index] = deltas.get(index, "") + event.get("delta", "")
        elif kind in {"error", "response.failed"}:
            raise ValueError(f"Responses API failed: {event.get('error') or event.get('response', {}).get('error')}")
    return "".join(completed.get(index, deltas.get(index, "")) for index in sorted(completed.keys() | deltas.keys()))


def _log_input(stage: str, workspace_root: str | Path | None,
               request: dict[str, Any], api_mode: str, base_url: str = "") -> Path:
    """Persist the complete final input before dispatch, including failed requests."""
    root = Path(workspace_root or os.getenv("ARC_WORKSPACE_ROOT") or Path.cwd()).expanduser().resolve()
    directory = root / ".arc" / "model_inputs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    label = re.sub(r"[^\w.-]+", "_", stage).strip("._")[:120] or "MODEL"
    # Exclusive creation also prevents overwrites on concurrent calls.
    suffix = 0
    while True:
        path = directory / f"{stamp}_{label}{'_' + str(suffix) if suffix else ''}.log"
        try:
            stream = path.open("x", encoding="utf-8")
            break
        except FileExistsError:
            suffix += 1
    with stream:
        metadata = {"stage": stage, "timestamp_utc": stamp, "api_mode": api_mode,
                    "base_url": base_url,
                    "parameters": {key: value for key, value in request.items() if key not in {"messages", "input"}}}
        stream.write(json.dumps(metadata, ensure_ascii=False, indent=2, default=str) + "\n")
        for index, message in enumerate(request.get("messages", request.get("input", [])), 1):
            stream.write(f"\n===== MESSAGE {index}: {message.get('role', 'unknown')} =====\n")
            content = message.get("content", "")
            stream.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2))
            stream.write("\n")
    return path


def _log_output(path: Path, response: Any) -> None:
    """Keep raw output, finish reason and usage next to its exact input."""
    from agents.model.usage import record_model_usage
    record_model_usage(response, stage=path.stem, workspace_root=path.parent.parent.parent)
    payload = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n===== MODEL OUTPUT =====\n")
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


async def generate_text(model: Any, messages: list[dict[str, Any]], *, stage: str = "MODEL",
                        workspace_root: str | Path | None = None) -> str:
    if isinstance(model, NativeOpenAIModel):
        return await model.generate(messages, stage=stage, workspace_root=workspace_root)
    # Preserve explicitly injected model objects (e.g. application integrations).
    log_path = _log_input(stage, workspace_root, {"model": getattr(model, "model_name", type(model).__name__),
                                     "messages": messages}, "injected")
    model = without_sdk_retries(model)
    response = await retry_model_call(lambda: model.ainvoke(messages))
    _log_output(log_path, response)
    content = response.content
    if isinstance(content, str):
        return content
    return "\n".join(block if isinstance(block, str) else block.get("text", "")
                     for block in content if isinstance(block, (str, dict)))
