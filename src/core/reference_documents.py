from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse


REFERENCE_PREFIX = "/references"
SUPPORTED_TEXT_EXTENSIONS = frozenset(
    {
        ".adoc",
        ".asciidoc",
        ".cfg",
        ".csv",
        ".htm",
        ".html",
        ".ini",
        ".json",
        ".jsonl",
        ".markdown",
        ".md",
        ".rst",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

_TEXT_VALIDATION_CACHE: dict[tuple[str, int, int], str | None] = {}


def build_reference_catalog(
    *,
    node_id: str,
    requirement_data: dict[str, Any],
    store: Any | None,
    requirements_dir: str | os.PathLike[str] | None,
) -> list[dict[str, Any]]:
    """Build the current node's direct and inherited reference declarations."""

    lineage: list[tuple[str, dict[str, Any]]] = []
    current_id = str(node_id or requirement_data.get("req_id") or requirement_data.get("id") or "").strip()
    current = requirement_data
    visited: set[str] = set()
    while isinstance(current, dict) and current_id and current_id not in visited:
        visited.add(current_id)
        lineage.append((current_id, current))
        parent_id = str(current.get("parent_id") or "").strip()
        if not parent_id or store is None:
            break
        current_id = parent_id
        current = store.get_requirement(parent_id) or {}

    catalog: list[dict[str, Any]] = []
    for declared_on, node in reversed(lineage):
        references = node.get("references") or []
        if not isinstance(references, list):
            continue
        for item in references:
            if not isinstance(item, dict):
                continue
            catalog.append(
                inspect_reference(
                    item,
                    declared_on=declared_on,
                    relation="current" if declared_on == node_id else "ancestor",
                    requirements_dir=requirements_dir,
                )
            )
    return catalog


def audit_requirement_tree_references(
    requirement_tree: dict[str, Any],
    requirements_dir: str | os.PathLike[str] | None,
) -> list[dict[str, str]]:
    """Return non-blocking issues for malformed or unavailable declarations."""

    issues: list[dict[str, str]] = []

    def add_issue(node_id: str, label: str, path: str, error: str) -> None:
        issues.append(
            {
                "node_id": node_id,
                "label": label,
                "path": path,
                "error": error,
            }
        )

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node_id = str(node.get("id") or node.get("req_id") or "<unknown>").strip() or "<unknown>"
        raw_references = node.get("references")
        if raw_references is not None and not isinstance(raw_references, list):
            add_issue(node_id, "", "", "references must be a list")
        elif isinstance(raw_references, list):
            for item in raw_references:
                if not isinstance(item, dict):
                    add_issue(node_id, "", str(item or ""), "reference entry must be a mapping")
                    continue
                inspected = inspect_reference(
                    item,
                    declared_on=node_id,
                    relation="current",
                    requirements_dir=requirements_dir,
                )
                if not inspected["available"]:
                    add_issue(
                        node_id,
                        str(inspected.get("label") or ""),
                        str(inspected.get("path") or ""),
                        str(inspected.get("error") or "reference is unavailable"),
                    )
        for child in node.get("children") or []:
            walk(child)

    walk(requirement_tree)
    return issues


def inspect_reference(
    reference: dict[str, Any],
    *,
    declared_on: str,
    relation: str,
    requirements_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    raw_path = str(reference.get("path") or "").strip()
    label = str(reference.get("label") or "").strip()
    payload: dict[str, Any] = {
        "label": label or _fallback_label(raw_path),
        "path": raw_path,
        "declared_on": str(declared_on or "").strip(),
        "relation": relation,
        "available": False,
    }

    normalized_path, error = _normalize_relative_reference_path(raw_path)
    if error:
        payload["error"] = error
        return payload
    if not requirements_dir:
        payload["error"] = "requirements directory is not configured"
        return payload

    try:
        root = Path(requirements_dir).expanduser().resolve()
        candidate = (root / normalized_path).resolve()
    except (OSError, RuntimeError) as exc:
        payload["error"] = f"path cannot be resolved: {exc}"
        return payload
    try:
        candidate.relative_to(root)
    except ValueError:
        payload["error"] = "path resolves outside the requirements directory"
        return payload

    suffix = candidate.suffix.lower()
    if suffix not in SUPPORTED_TEXT_EXTENSIONS:
        payload["error"] = f"unsupported reference format: {suffix or '<no extension>'}"
        return payload
    if not candidate.exists():
        payload["error"] = "file does not exist"
        return payload
    if not candidate.is_file():
        payload["error"] = "path is not a regular file"
        return payload

    text_error = _validate_utf8_text(candidate)
    if text_error:
        payload["error"] = text_error
        return payload

    payload.update(
        {
            "path": normalized_path,
            "virtual_path": f"{REFERENCE_PREFIX}/{normalized_path}",
            "available": True,
        }
    )
    return payload


def available_reference_paths(catalog: list[dict[str, Any]]) -> list[str]:
    """Return stable, deduplicated virtual paths from a reference catalog."""

    paths: list[str] = []
    seen: set[str] = set()
    for item in catalog:
        if not item.get("available"):
            continue
        path = str(item.get("virtual_path") or "").strip()
        if path and path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def _normalize_relative_reference_path(raw_path: str) -> tuple[str, str | None]:
    if not raw_path:
        return "", "path is required"
    parsed = urlparse(raw_path)
    if parsed.scheme or parsed.netloc or raw_path.startswith("//"):
        return "", "URLs are not supported"
    windows_path = PureWindowsPath(raw_path)
    if Path(raw_path).expanduser().is_absolute() or windows_path.is_absolute() or windows_path.drive:
        return "", "absolute paths are not supported"
    if ".." in PurePosixPath(raw_path.replace("\\", "/")).parts:
        return "", "path traversal segments are not allowed"

    normalized = os.path.normpath(raw_path.replace("\\", "/")).replace("\\", "/")
    if normalized in {"", "."}:
        return "", "path must point to a file"
    if normalized == ".." or normalized.startswith("../"):
        return "", "path traversal segments are not allowed"
    if re.match(r"^[A-Za-z]:", normalized):
        return "", "absolute paths are not supported"
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized, None


def _fallback_label(raw_path: str) -> str:
    normalized = str(raw_path or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def _validate_utf8_text(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError as exc:
        return f"file cannot be read: {exc}"
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if cache_key in _TEXT_VALIDATION_CACHE:
        return _TEXT_VALIDATION_CACHE[cache_key]
    error: str | None = None
    try:
        with path.open("r", encoding="utf-8") as file:
            while chunk := file.read(65536):
                if "\x00" in chunk:
                    error = "file contains binary NUL bytes"
                    break
    except UnicodeDecodeError:
        error = "file is not valid UTF-8 text"
    except OSError as exc:
        error = f"file cannot be read: {exc}"
    _TEXT_VALIDATION_CACHE[cache_key] = error
    return error
