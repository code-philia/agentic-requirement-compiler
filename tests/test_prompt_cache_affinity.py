from __future__ import annotations

import os

from agents.model.openai_api_adapter import (
    build_openai_chat_model,
    prompt_cache_key,
)


def test_cache_key_is_stable_and_scoped_by_prompt_family() -> None:
    first = prompt_cache_key("interface_design")

    assert first == prompt_cache_key("interface_design")
    assert first.endswith(":interface-design")
    assert prompt_cache_key("implementation").endswith(":implementation")
    assert first != prompt_cache_key("implementation")


def test_model_sends_prompt_family_cache_key() -> None:
    model = build_openai_chat_model(
        "gpt-5.4",
        api_key="test-key",
        prompt_cache_scope="implementation",
    )

    assert model._default_params["prompt_cache_key"] == "arc-v1:implementation"


def test_namespace_can_isolate_cold_benchmarks() -> None:
    original = os.environ.get("ARC_PROMPT_CACHE_NAMESPACE")
    try:
        os.environ["ARC_PROMPT_CACHE_NAMESPACE"] = "benchmark-a"
        first = prompt_cache_key("implementation")
        os.environ["ARC_PROMPT_CACHE_NAMESPACE"] = "benchmark-b"
        assert prompt_cache_key("implementation") != first
        assert len(prompt_cache_key("implementation")) <= 64
    finally:
        if original is None:
            os.environ.pop("ARC_PROMPT_CACHE_NAMESPACE", None)
        else:
            os.environ["ARC_PROMPT_CACHE_NAMESPACE"] = original
