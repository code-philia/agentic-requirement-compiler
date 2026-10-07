from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAIError


OpenAIAPIMode = Literal["responses", "chat_completions"]
_TRUTHY = {"1", "true", "yes", "on", "responses", "response", "responses_api"}
_FALSY = {"0", "false", "no", "off", "chat", "chat_completion", "chat_completions", "chat/completions"}


@dataclass(frozen=True)
class OpenAIAdapterConfig:
    model_name: str
    api_mode: OpenAIAPIMode
    base_url: str = ""
    api_key: str = ""


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
    )


def resolve_openai_api_mode(api_mode: str | None = None) -> OpenAIAPIMode:
    requested = str(api_mode or os.getenv("ARC_OPENAI_API_MODE", "")).strip().lower()
    if not requested:
        return "chat_completions"
    if requested in _TRUTHY or requested in {"responses"}:
        return "responses"
    if requested in _FALSY or requested in {"chat_completions", "chat"}:
        return "chat_completions"
    raise ValueError(
        "Invalid OpenAI API mode. Use `responses` or `chat_completions` "
        "(aliases: true/false for responses, chat for chat_completions)."
    )


def normalize_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    """Return ARC's normalized API exception when `exc` is model-provider related."""

    return _wrap_model_api_exception(exc, api_mode=api_mode, model=model)


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


def _get_openai_base_url() -> str:
    return os.getenv("OPENAI_API_BASE", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()
