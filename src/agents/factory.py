from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from deepagents import FilesystemPermission, HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StateBackend
from deepagents._models import get_model_provider
from langchain.agents.middleware.types import AgentMiddleware

from agents.context import AgentRuntimeContext
from agents.model_factory import create_arc_chat_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ResponseT
    from langchain_core.tools import BaseTool


WORKSPACE_PREFIX = "/workspace"
SKILLS_PREFIX = "/skills"
DISABLED_BUILTIN_TOOLS = frozenset({"write_todos"})


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
        return [tool for tool in tools if _tool_name(tool) not in self._disabled]


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
