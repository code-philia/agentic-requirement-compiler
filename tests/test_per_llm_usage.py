from agents.runtime.runners import _format_llm_usage_line


def test_per_llm_usage_distinguishes_cold_and_warm_calls() -> None:
    cold = _format_llm_usage_line({"prompt_tokens": 3652, "cached_tokens": 0, "total_tokens": 3657}, 1)
    warm = _format_llm_usage_line({"prompt_tokens": 3652, "cached_tokens": 3456, "total_tokens": 3657}, 2)
    assert "call=1" in cold and "cache_rate=0.0%" in cold
    assert "call=2" in warm and "cache_rate=94.6%" in warm
