from __future__ import annotations

from agents.runtime.runners import _format_llm_usage_line


def test_format_llm_usage_line_preserves_cold_start() -> None:
    line = _format_llm_usage_line(
        {
            "prompt_tokens": 3652,
            "completion_tokens": 5,
            "total_tokens": 3657,
            "cached_tokens": 0,
            "model": "gpt-5.4",
        },
        1,
    )

    assert line == (
        "llm usage: call=1 prompt_tokens=3652 completion_tokens=5 "
        "total_tokens=3657 cached_tokens=0 cache_rate=0.0% model=gpt-5.4"
    )


def test_format_llm_usage_line_reports_warm_cache_rate() -> None:
    line = _format_llm_usage_line(
        {
            "prompt_tokens": 3652,
            "completion_tokens": 5,
            "total_tokens": 3657,
            "cached_tokens": 3456,
        },
        2,
    )

    assert "call=2" in line
    assert "cached_tokens=3456" in line
    assert "cache_rate=94.6%" in line
