from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from openai import OpenAI
from agents.model.retries import MODEL_MAX_RETRIES
from jsonschema import ValidationError, validate

from core import files
from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

VISUAL_ANALYSIS_PROMPT_VERSION = "observable-visual-json-v1"
VISUAL_OBSERVATION_FIELDS = (
    "regions", "visible_controls", "layout_cues", "style_cues", "text_cues",
)
VISUAL_ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["reference_id", *VISUAL_OBSERVATION_FIELDS],
    "properties": {
        "reference_id": {"type": "string", "pattern": r"^VISUAL\.[0-9a-f]{16}$"},
        **{field: {"type": "array", "items": {"type": "string", "minLength": 1},
                   "maxItems": 64} for field in VISUAL_OBSERVATION_FIELDS},
    },
}


def build_visual_analysis_prompt() -> str:
    return """You are a senior visual-design analyst.
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


def validate_visual_analysis(payload: Any, reference_id: str | None = None) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    validate(instance=payload, schema=VISUAL_ANALYSIS_SCHEMA)
    if reference_id is not None and payload["reference_id"] != reference_id:
        raise ValueError("Visual analysis reference_id does not match the supplied image")
    for field in VISUAL_OBSERVATION_FIELDS:
        for cue in payload[field]:
            if not cue.strip() or "\ufffd" in cue or any(0xD800 <= ord(char) <= 0xDFFF for char in cue):
                raise ValueError(f"Invalid or corrupted visual observation in {field}")
    return payload


async def analyze_and_attach_visual_references(
    *,
    workspace_path: str,
    requirements_dir: str,
    requirement_data: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> dict[str, Any]:
    req_id = str(requirement_data.get("req_id") or requirement_data.get("id") or "").strip()
    if not req_id:
        await _log(log_cb, "System", "Invalid requirement data for visual analysis.", "error", None)
        return requirement_data

    candidates = _collect_visual_candidates(requirement_data)
    if not candidates:
        await _log(log_cb, "System", "No image found in the description or visual_reference.", "info", req_id)
        return requirement_data

    cache = _load_visual_cache(workspace_path)
    cache_updated = False
    visual_references: list[dict[str, Any]] = []

    for item in candidates:
        image_path = str(item.get("image_path") or "").strip()
        if not image_path:
            continue
        full_path = _resolve_image_path(image_path, workspace_path, requirements_dir)
        if not full_path.exists():
            await _log(log_cb, "System", f"Image not found: {full_path}", "warning", req_id)
            visual_references.append({"image_path": image_path})
            continue

        try:
            reference_id = _image_reference_id(full_path)
            existing_analysis = item.get("analysis")
            if existing_analysis:
                try:
                    analysis = validate_visual_analysis(existing_analysis, reference_id)
                except (ValueError, ValidationError):
                    await _log(log_cb, "System", f"Refreshing outdated visual analysis: {image_path}", None, req_id)
                else:
                    visual_references.append(_reference_payload(image_path, analysis, str(full_path)))
                    continue
            cache_key = _build_visual_cache_key(full_path)
            cached_entry = cache.get(cache_key)
            cached_analysis = None
            if isinstance(cached_entry, dict) and cached_entry.get("prompt_version") == VISUAL_ANALYSIS_PROMPT_VERSION:
                try:
                    cached_analysis = validate_visual_analysis(cached_entry.get("analysis"), reference_id)
                except (ValueError, ValidationError):
                    pass
            if cached_analysis is not None:
                visual_references.append(_reference_payload(image_path, cached_analysis, str(full_path)))
                await _log(log_cb, "System", f"Reusing cached visual analysis: {image_path}", None, req_id)
                continue

            await _log(log_cb, "System", f"Analyzing visual element: {image_path}", None, req_id)
            analysis = await _request_visual_analysis(full_path, workspace_path)
            cache[cache_key] = {
                "image_path": image_path,
                "full_path": str(full_path),
                "prompt_version": VISUAL_ANALYSIS_PROMPT_VERSION,
                "analysis": analysis,
            }
            cache_updated = True
            visual_references.append(_reference_payload(image_path, analysis, str(full_path)))
        except Exception as exc:
            await _log(log_cb, "System", f"Failed to analyze image {image_path}: {exc}", "error", req_id)
            visual_references.append({"image_path": image_path})

    if cache_updated:
        _save_visual_cache(workspace_path, cache)

    if visual_references:
        get_runtime().traceability.update_requirement_fields(req_id, visual_reference=visual_references)
        requirement_data["visual_reference"] = visual_references
        await _log(log_cb, "System", f"Stored {len(visual_references)} visual references for {req_id}", None, req_id)

    return requirement_data


def _collect_visual_candidates(requirement_data: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    visual_reference = requirement_data.get("visual_reference") or []
    if isinstance(visual_reference, list):
        for item in visual_reference:
            if isinstance(item, str):
                item = {"image_path": item}
            if not isinstance(item, dict):
                continue
            image_path = str(item.get("image_path") or "").strip()
            if image_path and image_path not in seen:
                seen.add(image_path)
                candidates.append(dict(item))

    description = str(requirement_data.get("description") or "")
    for image_path in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", description):
        normalized = image_path.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append({"image_path": normalized})
    return candidates


def _resolve_image_path(image_path: str, workspace_path: str, requirements_dir: str) -> Path:
    normalized = Path(os.path.normpath(image_path))
    if normalized.is_absolute():
        return normalized.resolve()
    base_dir = Path(requirements_dir or workspace_path)
    return (base_dir / normalized).resolve()


def _reference_payload(image_path: str, analysis: dict[str, Any], resolved_image_path: Any = None) -> dict[str, Any]:
    payload = {"image_path": image_path, "reference_id": analysis["reference_id"], "analysis": analysis}
    if resolved_image_path:
        payload["resolved_image_path"] = str(resolved_image_path)
    return payload


def _visual_cache_path(workspace_path: str) -> Path:
    return Path(workspace_path) / ".arc" / "visual_analysis_cache.json"


def _load_visual_cache(workspace_path: str) -> dict[str, Any]:
    return files.read_json_file(_visual_cache_path(workspace_path)) or {}


def _save_visual_cache(workspace_path: str, cache: dict[str, Any]) -> None:
    files.write_json_file(_visual_cache_path(workspace_path), cache)


def _build_visual_cache_key(full_path: Path) -> str:
    stat = full_path.stat()
    raw_key = f"{full_path}::{int(stat.st_mtime_ns)}::{stat.st_size}::{VISUAL_ANALYSIS_PROMPT_VERSION}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _image_reference_id(full_path: Path) -> str:
    return "VISUAL." + hashlib.sha256(full_path.read_bytes()).hexdigest()[:16]


async def _request_visual_analysis(full_path: Path, workspace_root: str | None = None) -> dict[str, Any]:
    visual_base_url = _resolve_visual_base_url()
    visual_api_key = _resolve_visual_api_key()
    if not visual_base_url:
        raise RuntimeError("Visual API base URL is not configured.")
    if not visual_api_key:
        raise RuntimeError("Visual API key is not configured.")

    mime_type, _ = mimetypes.guess_type(str(full_path))
    if not mime_type:
        mime_type = "image/png"
    image_bytes = full_path.read_bytes()
    reference_id = "VISUAL." + hashlib.sha256(image_bytes).hexdigest()[:16]
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{base64_image}"
    client = OpenAI(api_key=visual_api_key, base_url=visual_base_url, max_retries=MODEL_MAX_RETRIES)
    messages = [
            {
                "role": "system",
                "content": build_visual_analysis_prompt(),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps({
                        "reference_id": reference_id, "image_path": full_path.name,
                        "record_schema": VISUAL_ANALYSIS_SCHEMA,
                    }, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
    try:
        for attempt in range(3):
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=_normalize_openai_model_name(os.environ.get("VISUAL_MODEL") or os.environ.get("MODEL", "")),
                messages=messages,
                response_format={"type": "json_schema", "json_schema": {
                    "name": "visual_analysis", "strict": True, "schema": VISUAL_ANALYSIS_SCHEMA,
                }},
            )
            from agents.model.usage import record_model_usage
            record_model_usage(response, stage="VISUAL_ANALYSIS", workspace_root=workspace_root)
            text = _extract_visual_chat_completion_text(response)
            try:
                return validate_visual_analysis(text, reference_id)
            except (ValueError, ValidationError) as exc:
                if attempt == 2:
                    raise
                messages.extend([
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Correct the rejected JSON: {exc}. "
                     f"Use reference_id {reference_id} and the exact supplied schema."},
                ])
    finally:
        await asyncio.to_thread(client.close)


def _resolve_visual_api_key() -> str:
    return os.environ.get("VISUAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")


def _resolve_visual_base_url() -> str:
    return (
        os.environ.get("VISUAL_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE", "")
    ).strip()


def _extract_visual_chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    raise RuntimeError(f"Visual API response did not contain chat completion text: {_short_response(response)}")


def _normalize_openai_model_name(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if normalized.startswith("openai:"):
        return normalized.split(":", 1)[1].strip()
    return normalized


def _short_response(response: Any, limit: int = 800) -> str:
    text = str(response)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


async def _log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if hasattr(result, "__await__"):
        await result
