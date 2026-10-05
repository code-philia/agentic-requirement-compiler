"""One tool-free model call, followed by deterministic, scoped file edits."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from agents.model.factory import create_arc_chat_model
from agents.model.native_openai import generate_text
from agents.runtime.runners import parse_json_payload


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Replacement(Record):
    path: str
    old_text: str
    new_text: str


class NewFile(Record):
    path: str
    content: str


class CodeEdits(Record):
    changes: list[Replacement] = Field(default_factory=list)
    new_files: list[NewFile] = Field(default_factory=list)


EDIT_POLICY = """You have no tools. Return only JSON matching response_schema.
changes replaces one unique exact old_text in a supplied source file with new_text.
Use separate, non-overlapping replacements. new_files creates missing files only.
Never replace an existing file via new_files, delete files, or edit unseen source.
Paths are workspace-relative. Preserve unrelated behavior and database bootstrap hooks.
Do not change the prepared database schema/seeds/runtime or compiler control files.
The system applies edits and runs validation; do not claim builds/tests passed.
If necessary source is excluded, report it by returning no edits rather than inventing code.
"""

EXCLUDED = {".git", ".arc", ".agents", ".codex", ".aws", "requirements",
            "node_modules", "dist", "build", ".gradle", ".venv", "venv", "__pycache__"}
EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json",
              ".py", ".java", ".xml", ".gradle", ".toml"}


def is_test_asset(path: str) -> bool:
    normalized = "/" + path.lower()
    name = normalized.rsplit("/", 1)[-1]
    return any(part in normalized for part in ("/test/", "/tests/", "/__tests__/", "/e2e/", "/__mocks__/")) or any(
        marker in name for marker in (".test.", ".spec.", "playwright.config.", "vitest.config.",
                                     "jest.config.", "setup-tests.", "setuptests."))


def safe_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Expected a workspace-relative path: {value}")
    if any(part in EXCLUDED or part.startswith(".env") for part in path.parts):
        raise ValueError(f"Excluded code path: {value}")
    target = root / path
    # Reject symlink aliases as well as escapes so ownership cannot be bypassed.
    if target.resolve() != target or not target.resolve().is_relative_to(root):
        raise ValueError(f"Unsafe code path: {value}")
    return target


def source_bundle(root: Path, required: list[str], *, frontend_roots: list[str],
                  design: bool = False) -> dict[str, Any]:
    """Bounded frontend index; backend reads use exact targets and local imports."""
    index: list[str] = []
    search_roots = frontend_roots + (["backend", "app"] if design else [])
    for relative in dict.fromkeys(search_roots):
        folder = safe_path(root, relative)
        for directory, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDED
                             and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix in EXTENSIONS and not path.is_symlink() and not name.startswith(".env"):
                    index.append(path.relative_to(root).as_posix())
    index = sorted(set(index))
    entry_names = {"app", "main", "index", "server", "router", "routes", "client", "api", "__main__"}
    entries = [path for path in index if Path(path).stem.lower() in entry_names]
    frontend = [path for path in index if any(path.startswith(folder + "/") for folder in frontend_roots)]
    queue = list(dict.fromkeys(required + entries + frontend))
    sources: dict[str, str] = {}
    excluded: list[str] = []
    total = 0
    for relative in queue:
        target = safe_path(root, relative)
        if not target.is_file():
            if relative in required:
                raise ValueError(f"Required source file is missing: {relative}")
            continue
        if target.stat().st_size > 60000 or total + target.stat().st_size > 300000:
            if relative in required:
                raise ValueError(f"Required source exceeds context budget: {relative}")
            excluded.append(relative)
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeError:
            excluded.append(relative)
            continue
        sources[relative] = content
        total += len(content)
        # Follow direct JS/TS relative imports, without searching backend owners.
        for specifier in re.findall(r"(?:from\s*|require\(\s*|import\s*)['\"](\.[^'\"]+)['\"]", content):
            base = (target.parent / specifier).resolve()
            candidates = [base] + [Path(str(base) + ext) for ext in (".js", ".ts", ".tsx", ".jsx", ".json")]
            candidates += [base / ("index" + ext) for ext in (".js", ".ts", ".tsx")]
            for candidate in candidates:
                if candidate.is_file() and candidate.is_relative_to(root):
                    dependency = candidate.relative_to(root).as_posix()
                    try:
                        safe_path(root, dependency)
                    except ValueError:
                        break
                    if dependency not in queue:
                        queue.append(dependency)
                    break
    return {"sources": sources, "file_index": index[:2000],
            "index_truncated": len(index) > 2000, "excluded_sources": excluded}


async def ask_for_edits(model: str | object, system: str, task: dict[str, Any],
                        response_type: type[CodeEdits], *, workspace_root: Path | None = None) -> CodeEdits:
    resolved = create_arc_chat_model(model)
    from agents.model.prompt_input import format_task_input
    content = await generate_text(resolved, [
        {"role": "system", "content": system + "\n\n" + EDIT_POLICY},
        {"role": "user", "content": format_task_input(task, response_type.model_json_schema())},
    ], stage=str(task.get("model_stage") or response_type.__name__) +
       ("_" + str(task["node_id"]) if task.get("node_id") else ""), workspace_root=workspace_root)
    payload = parse_json_payload(content)
    if payload is None:
        raise ValueError("Expected JSON code edits; inspect MODEL OUTPUT in .arc/model_inputs. "
                         "Return one complete JSON object matching response_schema, without prose.")
    return response_type.model_validate(payload)


def apply_edits(root: Path, edits: CodeEdits, sources: dict[str, str],
                allowed: Callable[[str], bool], validate: Callable[[], Any] | None = None) -> list[str]:
    """Validate the complete batch before writing; roll back failed manifests."""
    pending: dict[str, str] = {}
    originals: dict[str, str | None] = {}
    for edit in edits.changes:
        if edit.path not in sources:
            raise ValueError(f"Cannot edit unseen source: {edit.path}")
        old = sources[edit.path]
        current = pending.get(edit.path, old)
        if not edit.old_text or old.count(edit.old_text) != 1 or current.count(edit.old_text) != 1:
            raise ValueError(f"old_text must match once without overlapping earlier edits: {edit.path}")
        pending[edit.path] = current.replace(edit.old_text, edit.new_text, 1)
        originals[edit.path] = old
    for file in edits.new_files:
        target = safe_path(root, file.path)
        if file.path in pending or target.exists():
            raise ValueError(f"new_files path already exists/duplicated: {file.path}")
        pending[file.path] = file.content
        originals[file.path] = None
    for relative in pending:
        target = safe_path(root, relative)
        if not allowed(relative):
            raise ValueError(f"Write outside node scope: {relative}")
        old = originals[relative]
        if old is not None and (not target.is_file() or target.read_text(encoding="utf-8") != old):
            raise ValueError(f"Source changed since context was collected: {relative}")
    written: list[str] = []
    try:
        for relative, content in pending.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            written.append(relative)
            target.write_text(content, encoding="utf-8")
        if validate:
            validate()
    except Exception:
        for relative in reversed(written):
            target = root / relative
            old = originals[relative]
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(old, encoding="utf-8")
        raise
    return written


def protected_paths(root: Path, node_id: str, records: list[dict[str, Any]]) -> set[str]:
    paths = {str(record.get("file_path", "")) for record in records
             if record.get("type") in {"API", "FUNC", "DB"}
             and (node_id not in record.get("req_ids", [])
                  or str(record.get("interface_id", "")).startswith("GLOBAL:DB:"))}
    state = root / ".arc/database/state.json"
    if state.exists():
        paths.update(json.loads(state.read_text(encoding="utf-8")).get("generated_files", []))
    return paths
