"""Actual provider token usage; cached input is a subset, never added twice."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from core.logging import append_debug_log, write_terminal_log, local_timestamp

_lock = threading.Lock()


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return value.model_dump() if hasattr(value, "model_dump") else {}


def record_model_usage(response: Any, *, stage: str, workspace_root: str | Path | None = None) -> None:
    payload = _dict(response)
    if isinstance(response, str):
        # Some gateways return Responses SSE even for non-streaming requests.
        for line in response.splitlines():
            if line.startswith("data:"):
                try:
                    event = json.loads(line[5:])
                except ValueError:
                    continue
                if isinstance(event, dict) and _dict(event.get("response")).get("usage"):
                    payload = event["response"]
    usage = _dict(payload.get("usage") or payload.get("usage_metadata")
                  or _dict(payload.get("response_metadata")).get("token_usage"))
    def count(*values):
        return next((v for v in values if isinstance(v, int) and not isinstance(v, bool) and v >= 0), None)
    input_tokens = count(usage.get("prompt_tokens"), usage.get("input_tokens"))
    output_tokens = count(usage.get("completion_tokens"), usage.get("output_tokens"))
    cached = count(_dict(usage.get("prompt_tokens_details")).get("cached_tokens"),
                   _dict(usage.get("input_tokens_details")).get("cached_tokens"),
                   _dict(usage.get("input_token_details")).get("cache_read"))
    root = Path(workspace_root or os.getenv("ARC_WORKSPACE_ROOT") or Path.cwd()).expanduser().resolve()
    path = root / ".arc/model_usage.jsonl"
    with _lock:
        totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0, "incomplete_calls": 0}
        if path.is_file():
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    totals["calls"] += 1
                    totals["incomplete_calls"] += int(any(row.get(k) is None for k in ("input_tokens", "output_tokens", "cached_tokens")))
                    for k in ("input_tokens", "output_tokens", "cached_tokens"):
                        totals[k] += count(row.get(k)) or 0
        row = {"timestamp": local_timestamp(), "stage": stage, "model": payload.get("model"),
               "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        totals["calls"] += 1
        totals["incomplete_calls"] += int(any(v is None for v in (input_tokens, output_tokens, cached)))
        for k in ("input_tokens", "output_tokens", "cached_tokens"):
            totals[k] += row[k] or 0
        show = lambda value: "unknown" if value is None else f"{value:,}"
        message = (f"{stage} tokens: input={show(input_tokens)}, output={show(output_tokens)}, cache_hit={show(cached)}; "
                   f"cumulative({totals['calls']} calls): input={totals['input_tokens']:,}, output={totals['output_tokens']:,}, "
                   f"cache_hit={totals['cached_tokens']:,}, total={totals['input_tokens'] + totals['output_tokens']:,}; "
                   f"incomplete_usage_calls={totals['incomplete_calls']}. Cache hits are included in input.")
        append_debug_log("ModelUsage", message, workspace_root=str(root))
        write_terminal_log("ModelUsage", message)
