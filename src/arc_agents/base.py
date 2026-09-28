from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


CONTEXT_SEGMENTS_KEY = "context_segments"
"""Payload key holding ordered ``{"name", "payload"}`` context segments.

A segmented payload is rendered as one user message per segment so a stable
prefix can be reused by the provider cache across iterations. The constant is
declared here rather than imported so bounded agents keep no dependency on a
concrete model client.
"""


class JsonModel(Protocol):
    """Minimal model interface required by a bounded structured agent."""

    def generate_json(
        self,
        *,
        schema_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        output_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class AgentInvocationResult:
    output: dict[str, Any] | None
    attempts: int
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output is not None and not self.errors


class BaseStructuredAgent:
    """Run one schema-constrained model decision with bounded repair retries.

    This base module deliberately provides no shell, filesystem, network, test,
    or patch tools. Concrete agents receive compiler-built context and return a
    validated decision; deterministic callers own all side effects.
    """

    def __init__(
        self,
        model: JsonModel,
        *,
        schema_name: str,
        instructions: str,
        output_schema: dict[str, Any],
        retries: int = 2,
        trace: Callable[[str], None] | None = None,
        model_log: Callable[[dict[str, Any]], None] | None = None,
        agent_name: str = "StructuredAgent",
    ) -> None:
        self._model = model
        self._schema_name = schema_name
        self._instructions = instructions
        self._output_schema = copy.deepcopy(output_schema)
        self._retries = max(0, min(int(retries), 5))
        self._trace = trace
        self._model_log = model_log
        self._agent_name = str(agent_name or "StructuredAgent")

    def invoke(
        self,
        input_payload: dict[str, Any],
        *,
        validate: Callable[[dict[str, Any]], list[str]],
        initial_feedback: list[str] | None = None,
    ) -> AgentInvocationResult:
        base_feedback = list(initial_feedback or [])
        feedback: list[str] = list(base_feedback)
        last_errors: list[str] = []
        for attempt in range(1, self._retries + 2):
            payload = copy.deepcopy(input_payload)
            if feedback:
                payload["agent_validation_feedback"] = feedback
            requirement_id = _payload_value(payload, "requirement_id")
            mode = _payload_value(payload, "implementation_mode")
            iteration = _payload_value(payload, "iteration")
            self._emit(
                f"MODEL_REQUEST requirement={requirement_id} mode={mode} "
                f"iteration={iteration} attempt={attempt}/{self._retries + 1}"
            )
            self._emit(
                _context_audit(
                    requirement_id=requirement_id,
                    mode=mode,
                    iteration=iteration,
                    attempt=attempt,
                    instructions=self._instructions,
                    input_payload=payload,
                    output_schema=self._output_schema,
                )
            )
            try:
                started = time.perf_counter()
                output = self._model.generate_json(
                    schema_name=self._schema_name,
                    instructions=self._instructions,
                    input_payload=payload,
                    output_schema=self._output_schema,
                )
                if self._model_log is not None:
                    try:
                        self._model_log({
                        "agent_name": self._agent_name,
                        "schema_name": self._schema_name,
                        "instructions": self._instructions,
                        "input_payload": payload,
                        "output_schema": self._output_schema,
                        "output": output,
                        "error": None,
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                        "attempt": attempt,
                        "requirement_id": requirement_id,
                        "iteration": iteration,
                        })
                    except Exception as log_exc:
                        self._emit(f"MODEL_LOG_WRITE_FAILED: {type(log_exc).__name__}: {log_exc}")
            except Exception as exc:
                if self._model_log is not None:
                    try:
                        self._model_log({
                        "agent_name": self._agent_name,
                        "schema_name": self._schema_name,
                        "instructions": self._instructions,
                        "input_payload": payload,
                        "output_schema": self._output_schema,
                        "output": None,
                        "error": _describe_error(exc),
                        "duration_ms": round((time.perf_counter() - started) * 1000)
                        if "started" in locals() else None,
                        "attempt": attempt,
                        "requirement_id": requirement_id,
                        "iteration": iteration,
                        })
                    except Exception as log_exc:
                        self._emit(f"MODEL_LOG_WRITE_FAILED: {type(log_exc).__name__}: {log_exc}")
                last_errors = [f"AGENT_MODEL_FAILED: {_describe_error(exc)}"]
                feedback = (
                    [
                        *base_feedback,
                        "The provider response was empty or invalid JSON. Return exactly one non-empty JSON object; "
                        "do not emit Markdown, a code fence, prose, or an empty response."
                    ]
                    if isinstance(exc, json.JSONDecodeError)
                    or "empty" in str(exc).lower()
                    else [*base_feedback, *last_errors]
                )
                self._emit(last_errors[0])
                if attempt <= self._retries:
                    delay = _transport_retry_delay(attempt)
                    self._emit(
                        f"MODEL_RETRY_BACKOFF attempt={attempt} delay_seconds={delay:g}"
                    )
                    time.sleep(delay)
                continue
            validation_errors = validate(output)
            if not validation_errors:
                self._emit(f"MODEL_ACCEPTED attempt={attempt}")
                return AgentInvocationResult(output=output, attempts=attempt)
            last_errors = list(dict.fromkeys(validation_errors))
            feedback = [
                "Repair only the structured output. Do not broaden scope or change the task.",
                *base_feedback,
                *last_errors,
            ]
            self._emit(
                f"MODEL_REJECTED attempt={attempt}/{self._retries + 1} "
                + "; ".join(last_errors)
            )
        return AgentInvocationResult(
            output=None,
            attempts=self._retries + 1,
            errors=last_errors or ["AGENT_MODEL_FAILED: no valid output was produced."],
        )

    def _emit(self, message: str) -> None:
        if self._trace is not None:
            self._trace(f"[{self._agent_name}] {message}")


def _payload_value(payload: dict[str, Any], key: str) -> str:
    """Read one routing field from a flat or segmented context payload."""

    if key in payload:
        return str(payload[key])
    for segment in payload.get(CONTEXT_SEGMENTS_KEY, []):
        if not isinstance(segment, dict):
            continue
        body = segment.get("payload")
        if isinstance(body, dict) and key in body:
            return str(body[key])
    return ""


def _payload_sections(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten a flat or segmented payload into audit-visible sections."""

    sections: list[tuple[str, Any]] = []
    for key, value in payload.items():
        if key != CONTEXT_SEGMENTS_KEY:
            sections.append((str(key), value))
            continue
        for index, segment in enumerate(value if isinstance(value, list) else []):
            if not isinstance(segment, dict):
                continue
            name = str(segment.get("name", f"segment_{index}"))
            body = segment.get("payload")
            if not isinstance(body, dict):
                sections.append((name, body))
                continue
            sections.extend(
                (f"{name}.{str(inner_key)}", inner_value)
                for inner_key, inner_value in body.items()
            )
    return sections


def _describe_error(error: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {str(current).strip() or repr(current)}")
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return " <- ".join(parts)


def _transport_retry_delay(attempt: int) -> float:
    """Return the bounded 2s/8s/30s retry schedule for model transport faults."""

    schedule = (2.0, 8.0, 30.0)
    return schedule[min(max(1, int(attempt)), len(schedule)) - 1]


def _context_audit(
    *,
    requirement_id: str,
    mode: str,
    iteration: str,
    attempt: int,
    instructions: str,
    input_payload: dict[str, Any],
    output_schema: dict[str, Any],
) -> str:
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    section_sizes = sorted(
        ((key, size(value)) for key, value in _payload_sections(input_payload)),
        key=lambda item: item[1],
        reverse=True,
    )
    envelope = {
        "instructions": instructions,
        "input_payload": input_payload,
        "output_schema": output_schema,
    }
    return (
        f"CONTEXT_AUDIT requirement={requirement_id} phase=implementation mode={mode} "
        f"iteration={iteration} attempt={attempt} context_total_chars={size(envelope)} "
        f"instructions_chars={size(instructions)} input_payload_chars={size(input_payload)} "
        f"output_schema_chars={size(output_schema)} section_chars="
        + ",".join(f"{key}:{value}" for key, value in section_sizes)
    )
