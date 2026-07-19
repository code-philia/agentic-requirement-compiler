from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAIError
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agents.compatible_openai import CompatibleChatOpenAI


OpenAIAPIMode = Literal["responses", "chat_completions"]
_TRUTHY = {"1", "true", "yes", "on", "responses", "response", "responses_api"}
_FALSY = {"0", "false", "no", "off", "chat", "chat_completion", "chat_completions", "chat/completions"}


@dataclass(frozen=True)
class OpenAIAdapterConfig:
    model_name: str
    api_mode: OpenAIAPIMode
    base_url: str = ""
    api_key: str = ""
    sse_text_compat: bool = False


class ARCModelAPIError(RuntimeError):
    """Normalized exception for OpenAI-compatible model API failures."""

    def __init__(
        self,
        message: str,
        *,
        api_mode: OpenAIAPIMode,
        model: str,
        status_code: int | None = None,
        error_type: str = "",
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.api_mode = api_mode
        self.model = model
        self.status_code = status_code
        self.error_type = error_type
        self.original = original


class ARCChatOpenAI(ChatOpenAI):
    """ChatOpenAI with ARC-level API error normalization."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="chat_completions")
    _arc_model_name: str = PrivateAttr(default="")

    def __init__(self, *args: Any, arc_api_mode: OpenAIAPIMode, arc_model_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            _raise_model_api_exception(exc, api_mode=self._arc_api_mode, model=self._arc_model_name)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            _raise_model_api_exception(exc, api_mode=self._arc_api_mode, model=self._arc_model_name)


class ARCCompatibleChatOpenAI(CompatibleChatOpenAI):
    """Responses-compatible ChatOpenAI with ARC-level API error normalization."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="responses")
    _arc_model_name: str = PrivateAttr(default="")

    def __init__(self, *args: Any, arc_api_mode: OpenAIAPIMode, arc_model_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            _raise_model_api_exception(exc, api_mode=self._arc_api_mode, model=self._arc_model_name)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            _raise_model_api_exception(exc, api_mode=self._arc_api_mode, model=self._arc_model_name)


def build_openai_chat_model(
    model_name: str,
    *,
    api_mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    config = resolve_openai_adapter_config(
        model_name=model_name,
        api_mode=api_mode,
        base_url=base_url,
        api_key=api_key,
    )
    kwargs: dict[str, Any] = {
        "model": config.model_name,
        "disable_streaming": True,
        "stream_usage": False,
        "use_responses_api": config.api_mode == "responses",
        "output_version": "responses/v1" if config.api_mode == "responses" else "v0",
        "arc_api_mode": config.api_mode,
        "arc_model_name": config.model_name,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.api_key:
        kwargs["api_key"] = config.api_key

    model_class = ARCCompatibleChatOpenAI if config.sse_text_compat else ARCChatOpenAI
    return model_class(**kwargs)


def resolve_openai_adapter_config(
    *,
    model_name: str,
    api_mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> OpenAIAdapterConfig:
    resolved_base_url = (base_url if base_url is not None else _get_openai_base_url()).strip()
    resolved_api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
    resolved_mode = resolve_openai_api_mode(api_mode)
    return OpenAIAdapterConfig(
        model_name=model_name,
        api_mode=resolved_mode,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        sse_text_compat=_should_use_sse_text_compat(resolved_base_url, resolved_mode),
    )


def resolve_openai_api_mode(api_mode: str | None = None) -> OpenAIAPIMode:
    requested = str(api_mode or os.getenv("ARC_OPENAI_API_MODE", "")).strip().lower()
    if not requested:
        legacy = os.getenv("ARC_USE_RESPONSES_API", "").strip().lower()
        if legacy in _TRUTHY:
            return "responses"
        if legacy in _FALSY:
            return "chat_completions"
        return "responses"
    if requested in _TRUTHY:
        return "responses"
    if requested in _FALSY:
        return "chat_completions"
    raise ValueError(
        "Invalid OpenAI API mode. Use `responses` or `chat_completions` "
        "(aliases: `chat`, `chat/completions`)."
    )


def should_disable_streaming_for_openai_mode(api_mode: str | None = None) -> bool:
    if resolve_openai_api_mode(api_mode) != "responses":
        return False
    force = os.environ.get("ARC_AGENT_FORCE_RESPONSES_STREAM", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return False
    base_url = _get_openai_base_url()
    if not base_url:
        return False
    return not _is_official_openai_base_url(base_url)


def normalize_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    """Return ARC's normalized API exception when `exc` is model-provider related."""

    return _wrap_model_api_exception(exc, api_mode=api_mode, model=model)


def _raise_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> NoReturn:
    wrapped = _wrap_model_api_exception(exc, api_mode=api_mode, model=model)
    if wrapped is exc:
        raise exc
    raise wrapped from exc


def _wrap_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    if isinstance(exc, ARCModelAPIError):
        return exc
    if not _is_model_api_exception(exc):
        return exc
    status_code = getattr(exc, "status_code", None)
    error_type = _extract_error_type(exc)
    message = _format_model_api_error_message(
        exc,
        api_mode=api_mode,
        model=model,
        status_code=status_code,
        error_type=error_type,
    )
    return ARCModelAPIError(
        message,
        api_mode=api_mode,
        model=model,
        status_code=status_code if isinstance(status_code, int) else None,
        error_type=error_type,
        original=exc,
    )


def _is_model_api_exception(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            OpenAIError,
            APIError,
            APIStatusError,
            APIConnectionError,
            APITimeoutError,
            httpx.HTTPError,
        ),
    )


def _format_model_api_error_message(
    exc: Exception,
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    status_code: Any,
    error_type: str,
) -> str:
    parts = [
        f"Model API request failed using `{api_mode}` mode",
        f"model={model or '<unknown>'}",
    ]
    if status_code:
        parts.append(f"status={status_code}")
    if error_type:
        parts.append(f"type={error_type}")
    parts.append(f"error={_short_error_text(exc)}")
    if api_mode == "responses":
        parts.append("If the provider does not support Responses API, set ARC_OPENAI_API_MODE=chat_completions.")
    return "; ".join(parts)


def _extract_error_type(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("type") or error.get("code") or "").strip()
        return str(body.get("type") or body.get("code") or "").strip()
    return type(exc).__name__


def _short_error_text(exc: Exception, limit: int = 800) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... [truncated]"


def _should_use_sse_text_compat(base_url: str, api_mode: OpenAIAPIMode) -> bool:
    override = os.getenv("ARC_OPENAI_SSE_TEXT_COMPAT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return bool(base_url and api_mode == "responses" and not _is_official_openai_base_url(base_url))


def _get_openai_base_url() -> str:
    return os.getenv("OPENAI_API_BASE", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()


def _is_official_openai_base_url(base_url: str) -> bool:
    try:
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname or ""
    except Exception:
        host = ""
    return host == "api.openai.com" or host.endswith(".openai.com")
