from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

from agents.compatible_openai import CompatibleChatOpenAI


def create_arc_chat_model(model: str | object) -> str | object:
    if not isinstance(model, str):
        return model

    provider, model_name = _split_model_name(model)
    if provider not in ("", "openai"):
        return model

    kwargs: dict[str, Any] = {
        "model": model_name,
        "disable_streaming": True,
        "stream_usage": False,
    }
    base_url = os.getenv("OPENAI_API_BASE", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    if _should_use_responses_api(base_url):
        kwargs["use_responses_api"] = True
        kwargs["output_version"] = "responses/v1"
    elif base_url:
        kwargs["use_responses_api"] = False
        kwargs["output_version"] = "v0"
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        kwargs["api_key"] = api_key
    model_class = CompatibleChatOpenAI if _should_use_sse_text_compat(base_url) else ChatOpenAI
    return model_class(**kwargs)


def _split_model_name(model: str) -> tuple[str, str]:
    if ":" not in model:
        return "", model.strip()
    provider, model_name = model.split(":", 1)
    return provider.strip().lower(), model_name.strip()


def _should_use_responses_api(base_url: str) -> bool:
    override = os.getenv("ARC_USE_RESPONSES_API", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return True


def _should_use_sse_text_compat(base_url: str) -> bool:
    override = os.getenv("ARC_OPENAI_SSE_TEXT_COMPAT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return bool(base_url and _should_use_responses_api(base_url))
