from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI


def create_arc_chat_model(model: str | object) -> str | object:
    if not isinstance(model, str):
        return model

    provider, model_name = _split_model_name(model)
    if provider not in ("", "openai"):
        return model

    kwargs: dict[str, Any] = {
        "model": model_name,
        "use_responses_api": True,
        "output_version": "responses/v1",
        "disable_streaming": True,
    }
    base_url = os.getenv("OPENAI_API_BASE", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def _split_model_name(model: str) -> tuple[str, str]:
    if ":" not in model:
        return "", model.strip()
    provider, model_name = model.split(":", 1)
    return provider.strip().lower(), model_name.strip()
