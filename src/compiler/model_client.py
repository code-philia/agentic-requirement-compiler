from __future__ import annotations

import json
import os
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import OpenAI


CONTEXT_SEGMENTS_KEY = "context_segments"


class StructuredModel(Protocol):
    """Small interface used by semantic compiler passes."""

    def generate_json(
        self,
        *,
        schema_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Return one JSON object conforming to ``output_schema``."""


class ModelConfigurationError(RuntimeError):
    """Raised when a semantic pass has no usable model configuration."""


def is_deepseek_model(model: str) -> bool:
    """Return whether a model name needs DeepSeek reasoning parameters."""

    return str(model).strip().lower().startswith("deepseek")


def completion_request_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build provider request arguments, including DeepSeek-only controls."""

    request: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": messages,
    }
    if response_format is not None:
        request["response_format"] = response_format
    if is_deepseek_model(model):
        request["reasoning_effort"] = "low"
        request["extra_body"] = {"thinking": {"type": "enabled"}}
    return request


def describe_model_error(error: BaseException) -> str:
    """Render the public exception and its underlying transport cause."""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        message = str(current).strip() or repr(current)
        parts.append(f"{type(current).__name__}: {message}")
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return " <- ".join(parts)


class Model:
    """Structured-output client backed by the configured chat-completions endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 120.0,
        transport_retries: int = 3,
    ) -> None:
        if not model.strip():
            raise ModelConfigurationError("MODEL is required for semantic compiler passes.")
        if not api_key.strip():
            raise ModelConfigurationError("OPENAI_API_KEY is required for semantic compiler passes.")
        self.model = model.strip()
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._structured_output_mode = "json_schema"
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=max(0, transport_retries),
        )

    @property
    def structured_output_mode(self) -> str:
        """Actual negotiated transport mode, including provider fallback."""
        return getattr(self, "_last_output_mode", self._structured_output_mode)

    @classmethod
    def from_env(cls) -> "Model":
        env_file = os.environ.get("ARC_ENV_FILE", "").strip()
        load_dotenv(env_file or ".env", override=False)
        return cls(
            model=os.environ.get("MODEL", ""),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL", "").strip()
            or "https://api.openai.com/v1",
            timeout_seconds=_positive_env_float("ARC_MODEL_TIMEOUT_SECONDS", 120.0),
            transport_retries=_nonnegative_env_int("ARC_MODEL_TRANSPORT_RETRIES", 3),
        )

    def generate_json(
        self,
        *,
        schema_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        output_schema = dict(output_schema)
        force_json = output_schema.pop("x-arc-output-mode", None) == "json_object"
        self._last_output_mode = "json_object" if force_json else self._structured_output_mode
        user_messages = _user_messages(input_payload)
        messages = [
            {"role": "system", "content": instructions},
            *user_messages,
        ]
        response = None
        if not force_json and self._structured_output_mode == "json_schema":
            try:
                response = self._client.chat.completions.create(
                    **completion_request_kwargs(
                        model=self.model,
                        messages=messages,
                        response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": output_schema,
                        },
                        },
                    )
                )
            except Exception as exc:
                if not response_format_unavailable(exc):
                    raise
                self._structured_output_mode = "json_object"
                self._last_output_mode = "json_object"

        fallback_instructions = (
            f"{instructions.rstrip()}\n\n"
            "Return exactly one JSON object matching this JSON Schema. Include every required "
            "property; optional properties may be omitted. Additional properties are allowed only where "
            "the schema permits them (such as literal JSON objects). Do not use Markdown:\n"
            f"{json.dumps(output_schema, ensure_ascii=False, separators=(',', ':'))}"
        )
        fallback_messages = [
            {"role": "system", "content": fallback_instructions},
            *user_messages,
        ]
        if response is None and (force_json or self._structured_output_mode == "json_object"):
            try:
                response = self._client.chat.completions.create(
                    **completion_request_kwargs(
                        model=self.model,
                        messages=fallback_messages,
                        response_format={"type": "json_object"},
                    )
                )
            except Exception as exc:
                if not response_format_unavailable(exc):
                    raise
                if force_json:
                    raise ModelConfigurationError("Frontend protocol requires JSON-object output support; no prompt-only fallback.") from exc
                self._structured_output_mode = "prompt_only"
                self._last_output_mode = "prompt_only"

        if response is None:
            response = self._client.chat.completions.create(
                **completion_request_kwargs(
                    model=self.model,
                    messages=fallback_messages,
                )
            )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("Structured model response is empty.")
        text = str(content)
        parsed = _parse_json_object(text)
        if not isinstance(parsed, dict):
            raise ValueError("Structured model response must be a JSON object.")
        return parsed


def _parse_json_object(text: str) -> Any:
    """Parse provider JSON while accepting lossless Markdown wrapping.

    Compatible endpoints occasionally ignore the structured-output contract and
    wrap the otherwise valid object in a ``json`` code fence.  Removing only that
    wrapper is safe; arbitrary prose is deliberately not accepted.
    """

    candidate = text.strip().lstrip("\ufeff")
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
    return json.loads(candidate)


def _user_messages(input_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Render one user message per context segment, stable prefix first.

    A layered payload carries ``context_segments`` as an ordered list of
    ``{"name": str, "payload": dict}`` rows. Each row becomes its own user
    message so the provider can reuse the prefix cache for every segment that
    did not change between iterations. Any other payload keeps the historic
    single-message shape.
    """

    segments = input_payload.get(CONTEXT_SEGMENTS_KEY)
    rendered: list[dict[str, str]] = []
    if isinstance(segments, list) and segments:
        extra = {
            key: value
            for key, value in input_payload.items()
            if key != CONTEXT_SEGMENTS_KEY
        }
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict) or not isinstance(
                segment.get("payload"), dict
            ):
                rendered = []
                break
            payload = dict(segment["payload"])
            if extra and index == len(segments) - 1:
                payload.update(extra)
            rendered.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "context_segment": str(segment.get("name", f"segment_{index}")),
                            "context": payload,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
    if rendered:
        return rendered

    markdown_context = input_payload.get("context_markdown")
    user_input = (
        markdown_context
        if set(input_payload) == {"context_markdown"} and isinstance(markdown_context, str)
        else json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"))
    )
    return [{"role": "user", "content": user_input}]


def response_format_unavailable(error: BaseException) -> bool:
    """Recognize an explicit provider rejection of response_format capability."""

    if getattr(error, "status_code", None) != 400:
        return False
    parts = [str(error)]
    body = getattr(error, "body", None)
    if body is not None:
        parts.append(json.dumps(body, ensure_ascii=False, default=str))
    detail = " ".join(parts).lower()
    unavailable = any(
        marker in detail
        for marker in (
            "unavailable",
            "unsupported",
            "not supported",
            "does not support",
        )
    )
    return "response_format" in detail and unavailable


def _positive_env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _nonnegative_env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default
