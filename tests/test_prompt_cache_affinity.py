from __future__ import annotations

from agents.model.openai_api_adapter import (
    build_openai_chat_model,
    prompt_cache_key,
    set_prompt_cache_run_id,
)
from core.workflow import ARCWorkflowManager


def test_workflow_assigns_one_run_scoped_cache_key(tmp_path) -> None:
    manager = ARCWorkflowManager(
        workspace_path=str(tmp_path / "output"),
        requirement_path=str(tmp_path / "requirements.yaml"),
    )

    assert prompt_cache_key() == f"arc-run:{manager.run_id}"
    assert len(manager.run_id) == 32


def test_model_sends_run_scoped_prompt_cache_key() -> None:
    set_prompt_cache_run_id("run-123")

    model = build_openai_chat_model("gpt-5.4", api_key="test-key")

    assert model._default_params["prompt_cache_key"] == "arc-run:run-123"
