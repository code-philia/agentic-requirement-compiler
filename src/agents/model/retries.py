"""One retry policy for model requests, independent of code/protocol repairs."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

from openai import APIConnectionError, APIStatusError

MODEL_MAX_RETRIES = 10
Result = TypeVar("Result")


def retryable_model_error(exc: BaseException) -> bool:
    original = getattr(exc, "original", None) or exc.__cause__ or exc
    return isinstance(original, (APIConnectionError, TimeoutError)) or (
        isinstance(original, APIStatusError)
        and (original.status_code in {408, 409, 429} or original.status_code >= 500))


async def retry_model_call(call: Callable[[], Awaitable[Result]]) -> Result:
    """For injected transports; native SDK clients implement the same policy."""
    for attempt in range(MODEL_MAX_RETRIES + 1):
        try:
            return await call()
        except Exception as exc:
            if not retryable_model_error(exc) or attempt == MODEL_MAX_RETRIES:
                raise
            logging.getLogger(__name__).warning(
                "Model request failed; retry %s/%s: %s", attempt + 1, MODEL_MAX_RETRIES, exc)
            await asyncio.sleep(min(0.5 * 2 ** attempt, 8))
    raise AssertionError("Unreachable")


def without_sdk_retries(model: Any) -> Any:
    """Disable retries on injected clients to avoid multiplying retry attempts."""
    if not hasattr(model, "model_copy") or not hasattr(model, "max_retries"):
        return model
    updates: dict[str, Any] = {"max_retries": 0}
    for name in ("root_client", "root_async_client"):
        client = getattr(model, name, None)
        if client is not None and hasattr(client, "with_options"):
            client = client.with_options(max_retries=0)
            updates[name] = client
            updates["async_client" if name == "root_async_client" else "client"] = client.chat.completions
    return model.model_copy(update=updates)
