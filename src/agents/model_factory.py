from __future__ import annotations

from agents.openai_api_adapter import build_openai_chat_model


def create_arc_chat_model(model: str | object, *, api_mode: str | None = None) -> str | object:
    if not isinstance(model, str):
        return model

    provider, model_name = _split_model_name(model)
    if provider not in ("", "openai"):
        return model

    return build_openai_chat_model(model_name, api_mode=api_mode)


def _split_model_name(model: str) -> tuple[str, str]:
    if ":" not in model:
        return "", model.strip()
    provider, model_name = model.split(":", 1)
    return provider.strip().lower(), model_name.strip()
