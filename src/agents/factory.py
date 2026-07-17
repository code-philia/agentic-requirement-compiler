from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from deepagents import FilesystemPermission, HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StateBackend
from deepagents._models import get_model_provider
from langchain.agents.middleware.types import AgentMiddleware
from pydantic import BaseModel, Field, create_model

from agents.context import AgentRuntimeContext
from agents.model_factory import create_arc_chat_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ResponseT
    from langchain_core.tools import BaseTool


WORKSPACE_PREFIX = "/workspace"
SKILLS_PREFIX = "/skills"
DISABLED_BUILTIN_TOOLS = frozenset({"write_todos"})


class OpenAIGlobSchema(BaseModel):
    """OpenAI-compatible schema for the deep-agents glob tool."""

    pattern: str = Field(description="Glob pattern to match files (e.g., '**/*.py', '*.txt', '/subdir/**/*.md').")
    path: str = Field(default=None, description="Base directory to search from. Defaults to the backend's default root.")


class OpenAIGrepSchema(BaseModel):
    """OpenAI-compatible schema for the deep-agents grep tool."""

    pattern: str = Field(description="Text pattern to search for (literal string, not regex).")
    path: str = Field(default=None, description="Directory to search in. Defaults to current working directory.")
    glob: str = Field(default=None, description="Glob pattern to filter which files to search (e.g., '*.py').")
    output_mode: Literal["files_with_matches", "content", "count"] = Field(
        default="files_with_matches",
        description="Output format: 'files_with_matches' (file paths only, default), 'content' (matching lines with context), 'count' (match counts per file).",
    )


class DisableToolsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Hide selected tools from model requests."""

    def __init__(self, *, disabled: frozenset[str]) -> None:
        self._disabled = disabled

    def wrap_model_call(
        self,
        request: "ModelRequest[Any]",
        handler: "Callable[[ModelRequest[Any]], ModelResponse[Any]]",
    ) -> "ModelResponse[Any]":
        return handler(request.override(tools=self._filter_tools(request.tools)))

    async def awrap_model_call(
        self,
        request: "ModelRequest[Any]",
        handler: "Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]]",
    ) -> "ModelResponse[ResponseT]":
        return await handler(request.override(tools=self._filter_tools(request.tools)))

    def _filter_tools(self, tools: list[Any]) -> list[Any]:
        return [
            _normalize_tool_schema(tool)
            for tool in tools
            if _tool_name(tool) not in self._disabled
        ]


def build_stage_agent(
    *,
    name: str,
    model: str | object,
    system_prompt: str,
    response_format: object | None,
    workspace_root: str,
    writable_roots: list[str],
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    tools: list[object] | None = None,
):
    """Create a deep-agent instance with ARC's first-batch filesystem policy."""

    root = Path(workspace_root).expanduser().resolve()
    routes = {
        f"{WORKSPACE_PREFIX}/": LocalShellBackend(
            root_dir=str(root),
            virtual_mode=True,
            inherit_env=True,
        ),
    }
    skills_root = _compiler_skills_root()
    if skills_root.exists():
        routes[f"{SKILLS_PREFIX}/"] = FilesystemBackend(
            root_dir=str(skills_root),
            virtual_mode=True,
        )
    backend = CompositeBackend(default=StateBackend(), routes=routes)

    resolved_model = create_arc_chat_model(model)
    _register_arc_tool_exclusions(model=model, resolved_model=resolved_model)

    return create_deep_agent(
        name=name,
        model=resolved_model,
        backend=backend,
        system_prompt=system_prompt,
        middleware=[DisableToolsMiddleware(disabled=DISABLED_BUILTIN_TOOLS)],
        tools=tools or [],
        skills=_resolve_source_paths(skills, root, skills_root, default=[f"{SKILLS_PREFIX}/"]),
        memory=_resolve_source_paths(memory, root, skills_root, default=[]),
        permissions=_build_filesystem_permissions(root, writable_roots),
        context_schema=AgentRuntimeContext,
        response_format=response_format,
    )


def _build_filesystem_permissions(root: Path, writable_roots: list[str]) -> list[Any]:
    permissions: list[Any] = [
        FilesystemPermission(
            operations=["read", "write"],
            paths=[
                f"{WORKSPACE_PREFIX}/.arc",
                f"{WORKSPACE_PREFIX}/.arc/**",
                f"{WORKSPACE_PREFIX}/.git",
                f"{WORKSPACE_PREFIX}/.git/**",
                f"{WORKSPACE_PREFIX}/requirements",
                f"{WORKSPACE_PREFIX}/requirements/**",
                f"{WORKSPACE_PREFIX}/**/node_modules",
                f"{WORKSPACE_PREFIX}/**/node_modules/**",
                f"{WORKSPACE_PREFIX}/**/dist",
                f"{WORKSPACE_PREFIX}/**/dist/**",
                f"{WORKSPACE_PREFIX}/**/dist-ssr",
                f"{WORKSPACE_PREFIX}/**/dist-ssr/**",
                f"{WORKSPACE_PREFIX}/**/build",
                f"{WORKSPACE_PREFIX}/**/build/**",
                f"{WORKSPACE_PREFIX}/**/coverage",
                f"{WORKSPACE_PREFIX}/**/coverage/**",
                f"{WORKSPACE_PREFIX}/**/.vite",
                f"{WORKSPACE_PREFIX}/**/.vite/**",
                f"{WORKSPACE_PREFIX}/**/package-lock.json",
                f"{WORKSPACE_PREFIX}/**/yarn.lock",
                f"{WORKSPACE_PREFIX}/**/pnpm-lock.yaml",
                f"{WORKSPACE_PREFIX}/.env",
                f"{WORKSPACE_PREFIX}/.env.*",
                f"{WORKSPACE_PREFIX}/**/.env",
                f"{WORKSPACE_PREFIX}/**/.env.*",
            ],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[WORKSPACE_PREFIX, f"{WORKSPACE_PREFIX}/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[SKILLS_PREFIX, f"{SKILLS_PREFIX}/**"],
            mode="allow",
        ),
    ]

    write_paths = [
        virtual_path
        for path in writable_roots
        if str(path or "").strip()
        for virtual_path in _expand_write_permission_paths(path, root)
    ]
    if write_paths:
        permissions.append(
            FilesystemPermission(
                operations=["write"],
                paths=write_paths,
                mode="allow",
            )
        )

    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        )
    )
    return permissions


def _expand_write_permission_paths(path: str, root: Path) -> list[str]:
    normalized = _to_virtual_workspace_path(path, root).rstrip("/") or WORKSPACE_PREFIX
    return [normalized, f"{normalized}/**"]


def _register_arc_tool_exclusions(*, model: Any, resolved_model: Any) -> None:
    """Remove built-in deep-agents tools that ARC does not want agents to use."""

    profile = HarnessProfile(excluded_tools=DISABLED_BUILTIN_TOOLS)
    registered: set[str] = set()
    if isinstance(model, str):
        provider, model_name = _split_model_name(model)
        if provider:
            for key in (provider, f"{provider}:{model_name}"):
                register_harness_profile(key, profile)
                registered.add(key)
    provider = get_model_provider(resolved_model)
    if provider and provider not in registered:
        register_harness_profile(provider, profile)


def _resolve_source_paths(paths: list[str] | None, root: Path, skills_root: Path, *, default: list[str]) -> list[str]:
    candidates = paths if paths is not None else default
    resolved: list[str] = []
    for path in candidates:
        virtual_path = _to_virtual_source_path(path, root, skills_root)
        if not _virtual_path_exists(virtual_path, root, skills_root):
            continue
        if virtual_path not in resolved:
            resolved.append(virtual_path)
    return resolved


def _virtual_path_exists(virtual_path: str, root: Path, skills_root: Path) -> bool:
    if virtual_path == SKILLS_PREFIX or virtual_path.startswith(f"{SKILLS_PREFIX}/"):
        relative = virtual_path[len(SKILLS_PREFIX) :].lstrip("/")
        return (skills_root / relative).exists()
    if virtual_path == WORKSPACE_PREFIX or virtual_path.startswith(f"{WORKSPACE_PREFIX}/"):
        relative = virtual_path[len(WORKSPACE_PREFIX) :].lstrip("/")
        return (root / relative).exists()
    return False


def _to_virtual_source_path(path: str, root: Path, skills_root: Path) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if raw == SKILLS_PREFIX or raw.startswith(f"{SKILLS_PREFIX}/"):
        return _normalize_virtual_path(raw)

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        try:
            relative = candidate.resolve().relative_to(skills_root)
        except ValueError:
            return _to_virtual_workspace_path(path, root)
        relative_text = relative.as_posix()
        if not relative_text or relative_text == ".":
            return SKILLS_PREFIX
        return _normalize_virtual_path(f"{SKILLS_PREFIX}/{relative_text}")

    return _to_virtual_workspace_path(path, root)


def _to_virtual_workspace_path(path: str, root: Path) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return WORKSPACE_PREFIX
    if raw == WORKSPACE_PREFIX or raw.startswith(f"{WORKSPACE_PREFIX}/"):
        return _normalize_virtual_path(raw)

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path `{path}` is outside workspace root `{root}`.") from exc

    relative_text = relative.as_posix()
    if not relative_text or relative_text == ".":
        return WORKSPACE_PREFIX
    return _normalize_virtual_path(f"{WORKSPACE_PREFIX}/{relative_text}")


def _compiler_skills_root() -> Path:
    return Path(__file__).resolve().parents[1] / "skills"


def _normalize_virtual_path(path: str) -> str:
    normalized = "/" + str(path).strip().replace("\\", "/").strip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized != WORKSPACE_PREFIX and normalized.endswith("/"):
        return normalized.rstrip("/")
    return normalized


def _split_model_name(model: str) -> tuple[str, str]:
    if ":" not in model:
        return "", model.strip()
    provider, model_name = model.split(":", 1)
    return provider.strip().lower(), model_name.strip()


def _tool_name(tool: "BaseTool | dict[str, Any] | Any") -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def _normalize_tool_schema(tool: "BaseTool | dict[str, Any] | Any") -> "BaseTool | dict[str, Any] | Any":
    name = _tool_name(tool)
    if isinstance(tool, dict):
        copied = dict(tool)
        parameters = copied.get("parameters")
        if isinstance(parameters, dict):
            copied["parameters"] = _sanitize_json_schema(parameters)
        function = copied.get("function")
        if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
            copied["function"] = {
                **function,
                "parameters": _sanitize_json_schema(function["parameters"]),
            }
        return copied

    schema = _openai_compatible_args_schema(tool, name)
    if schema is None:
        return tool
    if hasattr(tool, "model_copy"):
        return tool.model_copy(update={"args_schema": schema})
    return tool


def _openai_compatible_args_schema(tool: "BaseTool | Any", name: str | None) -> type[BaseModel] | None:
    if name == "glob":
        return OpenAIGlobSchema
    if name == "grep":
        return OpenAIGrepSchema

    args_schema = getattr(tool, "args_schema", None)
    if not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
        return None
    raw_schema = args_schema.model_json_schema()
    if not _schema_needs_openai_normalization(raw_schema):
        return None
    return _build_openai_schema_model(name or "Tool", raw_schema)


def _schema_needs_openai_normalization(schema: dict[str, Any]) -> bool:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    return any(isinstance(prop, dict) and "type" not in prop for prop in properties.values())


def _build_openai_schema_model(tool_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, property_schema in properties.items():
        if not isinstance(field_name, str) or not isinstance(property_schema, dict):
            continue
        annotation = _annotation_from_json_schema(property_schema)
        default = ... if field_name in required else property_schema.get("default", None)
        description = property_schema.get("description")
        title = property_schema.get("title")
        fields[field_name] = (
            annotation,
            Field(default=default, description=description, title=title),
        )

    model_name = "".join(part for part in f"OpenAI{tool_name.title()}Schema" if part.isalnum())
    return create_model(model_name or "OpenAIToolSchema", __base__=BaseModel, **fields)


def _annotation_from_json_schema(schema: dict[str, Any]) -> Any:
    concrete = _first_non_null_schema(schema)
    schema_type = concrete.get("type")
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        item_annotation = _annotation_from_json_schema(concrete.get("items") or {})
        return list[item_annotation]
    if schema_type == "object":
        return dict[str, Any]
    return Any


def _first_non_null_schema(schema: dict[str, Any]) -> dict[str, Any]:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                return candidate
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        for candidate in one_of:
            if isinstance(candidate, dict) and candidate.get("type") != "null":
                return candidate
    return schema


def _sanitize_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    copied = dict(schema)
    copied.setdefault("type", "object")
    properties = copied.get("properties")
    if isinstance(properties, dict):
        copied["properties"] = {
            key: _sanitize_property_schema(value) if isinstance(value, dict) else value
            for key, value in properties.items()
        }
    return copied


def _sanitize_property_schema(schema: dict[str, Any]) -> dict[str, Any]:
    concrete = dict(_first_non_null_schema(schema))
    for key in ("default", "description", "title"):
        if key in schema and key not in concrete:
            concrete[key] = schema[key]
    if "type" not in concrete:
        concrete["type"] = "string"
    if concrete.get("type") == "object":
        return _sanitize_json_schema(concrete)
    if concrete.get("type") == "array" and isinstance(concrete.get("items"), dict):
        concrete["items"] = _sanitize_property_schema(concrete["items"])
    return concrete
