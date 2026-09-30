"""Optional model cost accounting and threshold workspace snapshots."""
from __future__ import annotations

import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def enabled() -> bool:
    return os.environ.get("ARC_COST_STACK", "0").strip().lower() in {"1", "true", "yes", "on"}


def pricing(model: str) -> tuple[str, float, float, float]:
    name = str(model or "").lower()
    if name.startswith("deepseek-flash") or name.startswith("deepseek"):
        return "CNY", 0.02, 1.0, 4.0
    if name.startswith("gpt-5.6-sol"):
        return "USD", 0.5, 5.0, 30.0
    # Unknown providers are tracked, but do not invent a price.
    return "USD", 0.0, 0.0, 0.0


def record_usage(root: Path, model: str, usage: dict[str, Any]) -> None:
    if not enabled():
        return
    path = root / ".arc" / "cost_stack.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        currency, input_hit_rate, input_miss_rate, output_rate = pricing(model)
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
        cached = max(0, min(prompt, cached))
        miss = max(0, prompt - cached)
        amount = (cached * input_hit_rate + miss * input_miss_rate + completion * output_rate) / 1_000_000
        totals = state.setdefault("totals", {})
        totals[currency] = float(totals.get(currency, 0.0)) + amount
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["last_usage"] = {"model": model, "currency": currency, "prompt_tokens": prompt,
                                "cached_tokens": cached, "completion_tokens": completion, "cost": amount}
        _write(path, state)
        usage_log = root / ".arc" / "cost_usage.jsonl"
        usage_log.parent.mkdir(parents=True, exist_ok=True)
        with usage_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": state["updated_at"], "model": model,
                                     "currency": currency, "cost": amount,
                                     "totals": totals}, ensure_ascii=False, separators=(",", ":")) + "\n")
        print("COST_STACK " + json.dumps({"currency": currency, "cost": amount,
              "total": totals[currency]}, ensure_ascii=False))
        _threshold_snapshot(root, state)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


def stage_checkpoint(root: Path, label: str) -> None:
    if enabled():
        _snapshot(root, label)


def _threshold_snapshot(root: Path, state: dict[str, Any]) -> None:
    step = float(os.environ.get("ARC_COST_SNAPSHOT_STEP", "10") or 10)
    if step <= 0:
        return
    thresholds = state.setdefault("thresholds", {})
    for currency, value in state.get("totals", {}).items():
        current = int(float(value) // step)
        previous = int(thresholds.get(currency, 0))
        for threshold in range(previous + 1, current + 1):
            _snapshot(root, f"{threshold * step:g}{currency}")
        thresholds[currency] = max(previous, current)
    _write(root / ".arc" / "cost_stack.json", state)


def _snapshot(root: Path, trigger: str) -> None:
    parent = root.parent
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(trigger)).strip("._") or "checkpoint"
    # Keep trace archives beside, not inside, the generated workspace. This
    # prevents snapshots from becoming part of the next snapshot and gives
    # each run a stable sibling trace directory.
    trace_dir = parent / f"{root.name}_trace"
    archive = trace_dir / f"{root.name}_{safe}.zip"
    if archive.exists():
        return
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in root.rglob("*"):
                if not path.is_file() or "node_modules" in path.parts:
                    continue
                output.write(path, path.relative_to(root.parent))
    except OSError:
        return


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
