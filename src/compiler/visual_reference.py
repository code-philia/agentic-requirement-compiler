"""Resolve and analyze requirement-owned visual references."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, Sequence
from urllib.parse import unquote

from dotenv import load_dotenv
from openai import OpenAI

from core.logging import SynchronousLog

from .frontend_ir import (
    VISUAL_ANALYSIS_SCHEMA,
    FrontendDesignErrorCode,
    FrontendDesignIssue,
)
from .trace_payload import format_payload_trace
from .model_client import (
    completion_request_kwargs,
    describe_model_error,
    response_format_unavailable,
)


DEFAULT_MAX_VISUAL_BYTES = 20 * 1024 * 1024
SUPPORTED_MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

VISUAL_ANALYSIS_INSTRUCTIONS = """You are a senior visual-design analyst.
Analyze one UI reference image and return only directly observable evidence that
can guide a production frontend implementation. Capture the whole visual system, not only controls and text.

- regions: ordered page regions and their content purpose from top to bottom.
- visible_controls: control type, visible label, placement, and important visual state.
- layout_cues: composition, container proportions, grid/columns, alignment, grouping, whitespace rhythm, density,
  hierarchy, and relationships between regions. Use relative measurements such as narrow/wide or compact/generous.
- style_cues: concrete reusable observations. Prefix each cue with the most fitting category among Color,
  Typography, Spacing, Surface, Border, Shape, Elevation, Iconography, or Imagery. Include approximate visible color
  values when reliable, font character/weight/scale relationships, corner treatment, border weight, and shadows.
- text_cues: meaningful visible copy in reading order, preserving valid Unicode only when confidently legible.

Describe what should be referenced, not everything that happens to appear in the image. The generated product must
retain its own requirement data and behavior, so do not infer hidden behavior or copy unrelated names, records, or
decorative content. Do not emit corrupted OCR text; omit uncertain text instead. Do not generate JSX, DOM, CSS,
Tailwind classes, source code, routes, API contracts, or component names. Copy reference_id exactly from the supplied
metadata. Use concise, implementation-useful strings and [] when a category has no reliable observation. Return only
the structured JSON object required by the supplied schema.
"""


class VisualStructuredModel(Protocol):
    """Separate multimodal boundary; backend semantic passes remain text-only."""

    def generate_visual_json(
        self,
        *,
        schema_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        image_data_url: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Return one structured decision derived from text metadata and an image."""


class VisualModelConfigurationError(RuntimeError):
    """Raised when neither visual nor main model configuration is usable."""


class VisualModel:
    """OpenAI-compatible multimodal structured-output client."""

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
            raise VisualModelConfigurationError(
                "VISUAL_MODEL or MODEL is required for visual-reference analysis."
            )
        if not api_key.strip():
            raise VisualModelConfigurationError(
                "VISUAL_API_KEY or OPENAI_API_KEY is required for visual-reference analysis."
            )
        self.model = model.strip()
        self._structured_output_mode = "json_schema"
        self._client = OpenAI(
            api_key=api_key.strip(),
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            max_retries=max(0, transport_retries),
        )

    @property
    def structured_output_mode(self) -> str:
        """Expose provider fallback for generation attempt diagnostics."""
        return getattr(self, "_last_output_mode", self._structured_output_mode)

    @classmethod
    def from_env(cls) -> "VisualModel":
        env_file = os.environ.get("ARC_ENV_FILE", "").strip()
        load_dotenv(env_file or ".env", override=False)
        return cls(
            model=os.environ.get("VISUAL_MODEL", "").strip()
            or os.environ.get("MODEL", "").strip(),
            api_key=os.environ.get("VISUAL_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip(),
            base_url=os.environ.get("VISUAL_BASE_URL", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
            or "https://api.openai.com/v1",
            timeout_seconds=_positive_env_float("ARC_VISUAL_TIMEOUT_SECONDS", 120.0),
            transport_retries=_nonnegative_env_int("ARC_MODEL_TRANSPORT_RETRIES", 3),
        )

    def generate_visual_json(
        self,
        *,
        schema_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        image_data_url: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        output_schema = dict(output_schema)
        force_json = output_schema.pop("x-arc-output-mode", None) == "json_object"
        self._last_output_mode = "json_object" if force_json else self._structured_output_mode
        messages = _visual_messages(instructions, input_payload, image_data_url)
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
                # Capability negotiation is cached for this compiler run. The
                # pass is synchronous, so later images can skip the known-bad
                # request without introducing shared-state races.
                self._structured_output_mode = "json_object"
                self._last_output_mode = "json_object"

        fallback_instructions = (
            f"{instructions.rstrip()}\n\n"
            "Return exactly one JSON object matching this JSON Schema; include every required "
            "property; optional properties may be omitted. Additional properties are allowed only where "
            "the schema permits them (such as literal JSON objects). Do not use Markdown:\n"
            f"{json.dumps(output_schema, ensure_ascii=False, separators=(',', ':'))}"
        )
        fallback_messages = _visual_messages(
            fallback_instructions,
            input_payload,
            image_data_url,
        )
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
                    raise VisualModelConfigurationError("Frontend protocol requires JSON-object output support; no prompt-only fallback.") from exc
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
            raise ValueError("Visual structured model response is empty.")
        value = json.loads(str(content))
        if not isinstance(value, dict):
            raise ValueError("Visual structured model response must be a JSON object.")
        return value


def _visual_messages(
    instructions: str,
    input_payload: dict[str, Any],
    image_data_url: str,
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        input_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url, "detail": "high"},
                },
            ],
        },
    ]


@dataclass(slots=True)
class ResolvedVisualReference:
    id: str
    source_path: str
    aliases: list[str]
    absolute_path: Path
    sha256: str
    media_type: str
    byte_size: int
    requirement_ids: list[str]

    def analysis_payload(self) -> dict[str, Any]:
        """Return portable metadata; absolute local paths never reach the model or IR."""

        return {
            "reference_id": self.id,
            "source_path": self.source_path,
            "aliases": list(self.aliases),
            "sha256": self.sha256,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "requirement_ids": list(self.requirement_ids),
        }

    def to_ir(self, analysis: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_path": self.source_path,
            "aliases": list(self.aliases),
            "sha256": self.sha256,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "requirement_ids": list(self.requirement_ids),
            "analysis": analysis,
        }


@dataclass(slots=True)
class VisualReferenceResolutionResult:
    references: list[ResolvedVisualReference] = field(default_factory=list)
    errors: list[FrontendDesignIssue] = field(default_factory=list)


@dataclass(slots=True)
class VisualReferenceAnalysisResult:
    references: list[dict[str, Any]] = field(default_factory=list)
    errors: list[FrontendDesignIssue] = field(default_factory=list)


class VisualReferenceResolver:
    """Validate local references and content-deduplicate them before model use."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_VISUAL_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = int(max_bytes)

    def resolve(
        self,
        requirement_path: Path,
        requirement_ir: dict[str, Any],
    ) -> VisualReferenceResolutionResult:
        document_path = requirement_path.expanduser().resolve()
        requirement_root = document_path.parent
        nodes = requirement_ir.get("nodes")
        if not isinstance(nodes, dict):
            return VisualReferenceResolutionResult(
                errors=[
                    self._issue(
                        FrontendDesignErrorCode.IR_INVALID,
                        "Requirement IR has no node registry.",
                        "<requirement-ir>",
                    )
                ]
            )

        errors: list[FrontendDesignIssue] = []
        by_digest: dict[str, ResolvedVisualReference] = {}
        digest_by_id: dict[str, str] = {}
        for requirement_id in sorted(nodes):
            node = nodes[requirement_id]
            if not isinstance(node, dict):
                continue
            raw_references = node.get("visual_references", [])
            if not isinstance(raw_references, list):
                errors.append(
                    self._issue(
                        FrontendDesignErrorCode.IR_INVALID,
                        "visual_references must be a list.",
                        requirement_id,
                    )
                )
                continue
            for raw_reference in sorted({str(value).strip() for value in raw_references if str(value).strip()}):
                resolved, issue = self._resolve_one(
                    requirement_root,
                    requirement_id,
                    raw_reference,
                )
                if issue is not None:
                    errors.append(issue)
                    continue
                assert resolved is not None
                existing = by_digest.get(resolved.sha256)
                if existing is None:
                    colliding_digest = digest_by_id.get(resolved.id)
                    if colliding_digest is not None and colliding_digest != resolved.sha256:
                        errors.append(
                            self._issue(
                                FrontendDesignErrorCode.SYMBOL_DUPLICATE,
                                f"Visual reference id collision: {resolved.id}.",
                                requirement_id,
                                reference=raw_reference,
                            )
                        )
                        continue
                    by_digest[resolved.sha256] = resolved
                    digest_by_id[resolved.id] = resolved.sha256
                    continue
                existing.requirement_ids = sorted(
                    set(existing.requirement_ids) | set(resolved.requirement_ids)
                )
                source_paths = sorted(
                    {existing.source_path, *existing.aliases, resolved.source_path, *resolved.aliases}
                )
                existing.source_path = source_paths[0]
                existing.aliases = source_paths[1:]

        references = sorted(by_digest.values(), key=lambda item: item.id)
        return VisualReferenceResolutionResult(references=references, errors=errors)

    def _resolve_one(
        self,
        requirement_root: Path,
        requirement_id: str,
        raw_reference: str,
    ) -> tuple[ResolvedVisualReference | None, FrontendDesignIssue | None]:
        normalized = _normalize_markdown_destination(raw_reference)
        if not normalized or URI_SCHEME_PATTERN.match(normalized):
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_PATH_INVALID,
                f"Visual reference must be a local relative path: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
            )

        relative = Path(normalized.replace("/", os.sep))
        if relative.is_absolute():
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_PATH_INVALID,
                f"Visual reference must not be absolute: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
            )

        candidate = (requirement_root / relative).resolve()
        if candidate != requirement_root and requirement_root not in candidate.parents:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_PATH_ESCAPE,
                f"Visual reference escapes the requirement directory: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
            )
        if not candidate.is_file():
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_NOT_FOUND,
                f"Visual reference does not exist: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
            )

        try:
            byte_size = candidate.stat().st_size
        except OSError as exc:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_READ_FAILED,
                f"Cannot inspect visual reference {raw_reference!r}: {exc}",
                requirement_id,
                reference=raw_reference,
            )
        if byte_size <= 0:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_READ_FAILED,
                f"Visual reference is empty: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
            )
        if byte_size > self.max_bytes:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_TOO_LARGE,
                f"Visual reference is {byte_size} bytes; maximum is {self.max_bytes}: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
                byte_size=byte_size,
                max_bytes=self.max_bytes,
            )

        try:
            content = candidate.read_bytes()
        except OSError as exc:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_READ_FAILED,
                f"Cannot read visual reference {raw_reference!r}: {exc}",
                requirement_id,
                reference=raw_reference,
            )
        media_type = _detect_media_type(content)
        if media_type not in SUPPORTED_MEDIA_TYPES:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_MEDIA_UNSUPPORTED,
                f"Visual reference is not a supported PNG, JPEG, GIF, or WebP image: {raw_reference!r}.",
                requirement_id,
                reference=raw_reference,
            )

        digest = hashlib.sha256(content).hexdigest()
        source_path = PurePosixPath(candidate.relative_to(requirement_root)).as_posix()
        return ResolvedVisualReference(
            id=f"VISUAL.{digest[:16]}",
            source_path=source_path,
            aliases=[],
            absolute_path=candidate,
            sha256=digest,
            media_type=media_type,
            byte_size=byte_size,
            requirement_ids=[requirement_id],
        ), None

    @staticmethod
    def _issue(
        code: FrontendDesignErrorCode,
        message: str,
        blame_symbol: str,
        **context: Any,
    ) -> FrontendDesignIssue:
        return FrontendDesignIssue(
            code=code,
            message=message,
            phase="VISUAL_REFERENCE_RESOLUTION",
            blame_symbol=blame_symbol,
            severity="WARNING",
            context=context,
        )


class VisualReferenceAnalyzer:
    """Analyze each content-unique resolved image synchronously and serially."""

    def __init__(
        self,
        model: VisualStructuredModel | None,
        *,
        retry_count: int | None = None,
        configuration_error: str | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self._model = model
        self._configuration_error = configuration_error
        self._retry_count = (
            _bounded_env_int("ARC_STRUCTURED_OUTPUT_RETRY_COUNT", 2, 0, 10)
            if retry_count is None
            else max(0, int(retry_count))
        )
        self._cache: dict[str, dict[str, Any]] = {}
        workspace_root: Path | None = None
        if artifact_root is not None:
            arc_root = artifact_root.expanduser().resolve()
            workspace_root = arc_root.parent
        self._log = SynchronousLog(
            "VisualReferenceAnalyzer",
            workspace_root=workspace_root,
        )
        self._trace_enabled = _env_flag("ARC_FRONTEND_DESIGN_TRACE", True)

    @classmethod
    def from_env(cls, artifact_root: Path | None = None) -> "VisualReferenceAnalyzer":
        try:
            return cls(VisualModel.from_env(), artifact_root=artifact_root)
        except VisualModelConfigurationError as exc:
            return cls(
                None,
                configuration_error=str(exc),
                artifact_root=artifact_root,
            )

    def analyze(
        self,
        references: Sequence[ResolvedVisualReference],
    ) -> VisualReferenceAnalysisResult:
        records: list[dict[str, Any]] = []
        errors: list[FrontendDesignIssue] = []
        for reference in sorted(references, key=lambda item: item.id):
            analysis, issue = self._analyze_one(reference)
            if issue is not None:
                errors.append(issue)
                analysis = {
                    "reference_id": reference.id,
                    "regions": [],
                    "visible_controls": [],
                    "layout_cues": [],
                    "style_cues": [],
                    "text_cues": [],
                }
            records.append(reference.to_ir(analysis))
        return VisualReferenceAnalysisResult(references=records, errors=errors)

    def _analyze_one(
        self,
        reference: ResolvedVisualReference,
    ) -> tuple[dict[str, Any] | None, FrontendDesignIssue | None]:
        if self._model is None:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_MODEL_UNAVAILABLE,
                reference,
                self._configuration_error or "Visual model is not configured.",
            )
        cached = self._cache.get(reference.sha256)
        if cached is not None:
            return copy_json(cached), None

        try:
            content = reference.absolute_path.read_bytes()
        except OSError as exc:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_READ_FAILED,
                reference,
                f"Cannot read resolved visual reference: {exc}",
            )
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != reference.sha256:
            return None, self._issue(
                FrontendDesignErrorCode.VISUAL_READ_FAILED,
                reference,
                "Visual reference changed after resolution; resolve it again before analysis.",
                expected_sha256=reference.sha256,
                actual_sha256=actual_digest,
            )

        image_data_url = (
            f"data:{reference.media_type};base64,"
            f"{base64.b64encode(content).decode('ascii')}"
        )
        last_error = "Visual model did not return a valid analysis."
        validation_feedback: str | None = None
        for attempt in range(self._retry_count + 1):
            input_payload = reference.analysis_payload()
            if validation_feedback:
                input_payload["validation_feedback"] = validation_feedback
            self._trace(
                f"MODEL_REQUEST phase=visual_reference_analysis unit={reference.id} "
                f"attempt={attempt + 1}/{self._retry_count + 1}"
            )
            request_payload = {
                "schema_name": "arc_visual_reference_analysis",
                "instructions": VISUAL_ANALYSIS_INSTRUCTIONS,
                "input_payload": input_payload,
                "image": {
                    "source_path": reference.source_path,
                    "sha256": reference.sha256,
                    "media_type": reference.media_type,
                    "byte_size": reference.byte_size,
                    "detail": "high",
                },
                "output_schema": VISUAL_ANALYSIS_SCHEMA,
                "local_validation_schema": VISUAL_ANALYSIS_SCHEMA,
            }
            self._trace(
                _context_audit(
                    unit_id=reference.id,
                    attempt=attempt + 1,
                    request_payload=request_payload,
                    image_data_url=image_data_url,
                )
            )
            self._trace_json(
                "MODEL_INPUT",
                reference.id,
                request_payload,
            )
            started = time.perf_counter()
            try:
                value = self._model.generate_visual_json(
                    schema_name="arc_visual_reference_analysis",
                    instructions=VISUAL_ANALYSIS_INSTRUCTIONS,
                    input_payload=input_payload,
                    image_data_url=image_data_url,
                    output_schema=VISUAL_ANALYSIS_SCHEMA,
                )
            except VisualModelConfigurationError as exc:
                return None, self._issue(
                    FrontendDesignErrorCode.VISUAL_MODEL_UNAVAILABLE,
                    reference,
                    str(exc),
                )
            except Exception as exc:
                last_error = describe_model_error(exc)
                duration = int((time.perf_counter() - started) * 1000)
                self._trace(
                    f"MODEL_ERROR phase=visual_reference_analysis unit={reference.id} "
                    f"attempt={attempt + 1} duration_ms={duration} error={last_error}"
                )
                continue

            duration = int((time.perf_counter() - started) * 1000)
            self._trace_json("MODEL_OUTPUT", reference.id, value, duration)
            validation_error = _visual_analysis_error(value, reference.id)
            if validation_error is None:
                normalized = {
                    key: value[key]
                    for key in (
                        "reference_id",
                        "regions",
                        "visible_controls",
                        "layout_cues",
                        "style_cues",
                        "text_cues",
                    )
                }
                self._cache[reference.sha256] = copy_json(normalized)
                self._trace(
                    f"MODEL_ACCEPTED phase=visual_reference_analysis unit={reference.id} "
                    f"attempt={attempt + 1} duration_ms={duration}"
                )
                return normalized, None
            last_error = validation_error
            validation_feedback = validation_error
            self._trace(
                f"MODEL_REJECTED phase=visual_reference_analysis unit={reference.id} "
                f"errors={validation_error}"
            )

        code = (
            FrontendDesignErrorCode.VISUAL_ANALYSIS_INVALID
            if last_error.startswith("Invalid visual analysis")
            else FrontendDesignErrorCode.VISUAL_ANALYSIS_FAILED
        )
        return None, self._issue(code, reference, last_error)

    def _trace(self, message: str) -> None:
        if self._trace_enabled:
            self._log.info(message)

    def _trace_json(
        self,
        marker: str,
        unit_id: str,
        payload: Any,
        duration: int | None = None,
    ) -> None:
        suffix = f" duration_ms={duration}" if duration is not None else ""
        self._trace(
            f"{marker} phase=visual_reference_analysis unit={unit_id}{suffix}\n"
            f"{format_payload_trace(payload)}"
        )

    @staticmethod
    def _issue(
        code: FrontendDesignErrorCode,
        reference: ResolvedVisualReference,
        message: str,
        **context: Any,
    ) -> FrontendDesignIssue:
        return FrontendDesignIssue(
            code=code,
            message=message,
            phase="VISUAL_REFERENCE_ANALYSIS",
            blame_symbol=reference.id,
            severity="WARNING",
            context={"source_path": reference.source_path, **context},
        )


def _context_audit(
    *,
    unit_id: str,
    attempt: int,
    request_payload: dict[str, Any],
    image_data_url: str,
) -> str:
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    input_payload = request_payload.get("input_payload", {})
    section_sizes = sorted(
        ((str(key), size(value)) for key, value in input_payload.items()),
        key=lambda item: item[1],
        reverse=True,
    ) if isinstance(input_payload, dict) else []
    total_chars = size(
        {
            **request_payload,
            "image_data_url": image_data_url,
        }
    )
    return (
        f"CONTEXT_AUDIT phase=visual_reference_analysis unit={unit_id} "
        f"attempt={attempt} context_total_chars={total_chars} "
        f"instructions_chars={size(request_payload.get('instructions', ''))} "
        f"input_payload_chars={size(input_payload)} "
        f"image_data_chars={size(image_data_url)} "
        f"output_schema_chars={size(request_payload.get('output_schema', {}))} "
        f"section_chars="
        + ",".join(f"{key}:{value}" for key, value in section_sizes)
    )


def _normalize_markdown_destination(value: str) -> str:
    value = unquote(value.strip())
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")].strip()
    # Markdown permits an optional title after a whitespace-delimited destination.
    return value.split(maxsplit=1)[0].strip() if value else ""


def _detect_media_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _visual_analysis_error(value: Any, reference_id: str) -> str | None:
    required = {
        "reference_id",
        "regions",
        "visible_controls",
        "layout_cues",
        "style_cues",
        "text_cues",
    }
    if not isinstance(value, dict) or set(value) != required:
        return "Invalid visual analysis: output fields do not match the frozen schema."
    if value.get("reference_id") != reference_id:
        return "Invalid visual analysis: reference_id does not match the resolved image."
    for key in required - {"reference_id"}:
        items = value.get(key)
        if not isinstance(items, list) or len(items) > 64:
            return f"Invalid visual analysis: {key} must be a list with at most 64 items."
        if any(not isinstance(item, str) or not item.strip() for item in items):
            return f"Invalid visual analysis: {key} contains an invalid observation."
    return None


def copy_json(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


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


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}
