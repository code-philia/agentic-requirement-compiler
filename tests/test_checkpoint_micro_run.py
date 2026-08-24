from __future__ import annotations

import json
import subprocess

from core.checkpoint_micro_run import (
    _latest_model_handoff,
    evaluate_memory_workspace,
    parse_micro_run_log,
    prepare_checkpoint,
)


def git(*args: str, cwd) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def test_prepare_checkpoint_uses_requested_ref_and_restores_node_session(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--quiet", cwd=source)
    git("config", "user.email", "arc@example.test", cwd=source)
    git("config", "user.name", "ARC Test", cwd=source)
    product = source / "product.txt"
    product.write_text("checkpoint", encoding="utf-8")
    git("add", "product.txt", cwd=source)
    git("commit", "--quiet", "-m", "checkpoint", cwd=source)
    checkpoint = git("rev-parse", "HEAD", cwd=source)
    product.write_text("later", encoding="utf-8")
    git("commit", "--quiet", "-am", "later", cwd=source)
    session = source / ".arc" / "node_sessions" / "REQ-1.json"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"recent_failure_summary": "known failure"}), encoding="utf-8")
    debug_log = source / ".arc" / "debug.log"
    debug_log.write_text(
        "[TestDrivenDeveloper][REQ-1] model-final> Latest failure: stale alert.\n"
        "[TestDrivenDeveloper][REQ-1] \n"
        "[TestDrivenDeveloper][REQ-1] Next concrete edit target: frontend/src/App.tsx\n"
        "[TestDrivenDeveloper][REQ-1] agent call end: done\n",
        encoding="utf-8",
    )
    dependencies = source / "frontend" / "node_modules"
    dependencies.mkdir(parents=True)

    output = tmp_path / "micro"
    prepare_checkpoint(
        source_workspace=str(source),
        output_workspace=str(output),
        git_ref=checkpoint,
        node_id="REQ-1",
    )

    assert (output / "product.txt").read_text(encoding="utf-8") == "checkpoint"
    restored = json.loads((output / ".arc" / "node_sessions" / "REQ-1.json").read_text())
    assert restored["recent_failure_summary"] == "known failure"
    assert restored["checkpoint_handoff_summary"].endswith("frontend/src/App.tsx")
    assert (output / "frontend" / "node_modules").is_symlink()


def test_latest_model_handoff_keeps_full_structured_packet(tmp_path):
    log = tmp_path / "debug.log"
    prefix = "[TestDrivenDeveloper][REQ-1] "
    log.write_text(
        "\n".join(
            [
                prefix + "model-final> LATEST_FAILURE:",
                prefix + "precise failure",
                prefix,
                prefix + "ATTEMPTED_CHANGE:",
                prefix + "first fix",
                prefix,
                prefix + "NEXT_FILE:",
                prefix + "`/workspace/backend/src/routes/auth_routes.js`",
                prefix,
                prefix + "NEXT_ACTION:",
                prefix + "align the cookie contract",
                prefix + "agent call end: done",
            ]
        ),
        encoding="utf-8",
    )

    handoff = _latest_model_handoff(log, "REQ-1")

    assert "ATTEMPTED_CHANGE:" in handoff
    assert "NEXT_FILE:" in handoff
    assert "backend/src/routes/auth_routes.js" in handoff
    assert handoff.endswith("align the cookie contract")


def test_latest_model_handoff_uses_checkpoint_time_and_best_diagnosis(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text(
        "\n".join(
            [
                "[2026-08-24T02:57:46+07:00] [TestDrivenDeveloper][REQ-1] model-final> LATEST_FAILURE: SyntaxError in backend/src/auth.js:116:1",
                "[2026-08-24T02:57:46+07:00] [TestDrivenDeveloper][REQ-1] NEXT_FILE: backend/src/auth.js",
                "[2026-08-24T02:57:46+07:00] [TestDrivenDeveloper][REQ-1] NEXT_ACTION: remove the duplicate export",
                "[2026-08-24T02:57:46+07:00] [TestDrivenDeveloper][REQ-1] agent call end: done",
                "[2026-08-24T03:03:00+07:00] [TestDrivenDeveloper][REQ-1] model-final> LATEST_FAILURE: startup timeout",
                "[2026-08-24T03:03:00+07:00] [TestDrivenDeveloper][REQ-1] NEXT_FILE: backend/src/server.js",
                "[2026-08-24T03:03:00+07:00] [TestDrivenDeveloper][REQ-1] NEXT_ACTION: investigate ports",
                "[2026-08-24T03:03:00+07:00] [TestDrivenDeveloper][REQ-1] agent call end: done",
                "[2026-08-24T03:16:39+07:00] [TestDrivenDeveloper][REQ-1] model-final> LATEST_FAILURE: later 401",
                "[2026-08-24T03:16:39+07:00] [TestDrivenDeveloper][REQ-1] NEXT_FILE: backend/src/seed.js",
                "[2026-08-24T03:16:39+07:00] [TestDrivenDeveloper][REQ-1] agent call end: done",
                "[2026-08-24T03:16:40+07:00] [TestDrivenDeveloper][REQ-1] model-final> IMPLEMENTED",
            ]
        ),
        encoding="utf-8",
    )

    handoff = _latest_model_handoff(
        log,
        "REQ-1",
        cutoff_timestamp="2026-08-24T03:04:41+07:00",
    )

    assert "SyntaxError" in handoff
    assert "backend/src/auth.js" in handoff
    assert "server.js" not in handoff
    assert "seed.js" not in handoff


def test_prepare_fresh_implement_removes_future_failure_state(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--quiet", cwd=source)
    git("config", "user.email", "arc@example.test", cwd=source)
    git("config", "user.name", "ARC Test", cwd=source)
    (source / "product.txt").write_text("checkpoint", encoding="utf-8")
    git("add", "product.txt", cwd=source)
    git("commit", "--quiet", "-m", "checkpoint", cwd=source)
    checkpoint = git("rev-parse", "HEAD", cwd=source)
    session = source / ".arc" / "node_sessions" / "REQ-1.json"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "recent_failure_summary": "future failure",
                "checkpoint_handoff_summary": "future handoff",
                "tdd_handoff": {"last_test_type": "E2E"},
                "phase_status": {"design": "completed", "implement": "failed"},
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "fresh"
    prepare_checkpoint(
        source_workspace=str(source),
        output_workspace=str(output),
        git_ref=checkpoint,
        node_id="REQ-1",
        restore_handoff=False,
    )

    restored = json.loads((output / ".arc" / "node_sessions" / "REQ-1.json").read_text())
    assert restored["phase_status"]["implement"] == "pending"
    assert "recent_failure_summary" not in restored
    assert "checkpoint_handoff_summary" not in restored
    assert "tdd_handoff" not in restored


def test_parse_micro_run_log_extracts_usage_and_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("ARC_INPUT_PRICE_PER_1M", "2")
    monkeypatch.setenv("ARC_OUTPUT_PRICE_PER_1M", "8")
    monkeypatch.setenv("ARC_CACHED_INPUT_PRICE_PER_1M", "0.5")
    log = tmp_path / "debug.log"
    log.write_text(
        "\n".join(
            [
                "[Agent][REQ] tool-call> read_file args={\"file_path\":\"/workspace/frontend/src/App.tsx\"}",
                "[Agent][REQ] tool-call> edit_file args={\"file_path\":\"frontend/src/App.tsx\"}",
                "[Agent][REQ] tool-call> run_tests args={\"test_type\":\"E2E\"}",
                "[TestDrivenDeveloper][REQ] `run_tests` E2E usage 1/2.",
                "[TestDrivenDeveloper][REQ] `run_tests` E2E passed with Exit Code: 0 on attempt 1/2: backend/test.ts",
                "[TestDrivenDeveloper][REQ] agent stream failed; falling back to ainvoke.",
                "[Agent][REQ] agent usage: prompt_tokens=100 completion_tokens=10 total_tokens=110 cached_tokens=80 llm_calls=2 model=gpt-test",
            ]
        ),
        encoding="utf-8",
    )

    metrics = parse_micro_run_log(log)

    assert metrics["cache_rate"] == 0.8
    assert metrics["uncached_prompt_tokens"] == 20
    assert metrics["estimated_cost_usd"] == 0.00016
    assert metrics["estimated_uncached_cost_usd"] == 0.00028
    assert metrics["estimated_cache_savings_usd"] == 0.00012
    assert metrics["llm_calls"] == 2
    assert metrics["run_tests_calls"] == 1
    assert metrics["run_tests_executions"] == 1
    assert metrics["provider_fallbacks"] == 1
    assert metrics["layer_metrics"]["E2E"] == {
        "executions": 1,
        "first_pass_attempt": 1,
        "ever_passed": True,
        "final_passed": True,
        "pass_executions": 1,
        "failed_executions": 0,
    }
    assert metrics["files_read"] == ["frontend/src/App.tsx"]
    assert metrics["files_written"] == ["frontend/src/App.tsx"]


def test_evaluate_memory_workspace_is_offline_and_scores_packet(tmp_path):
    traceability = tmp_path / ".arc" / "traceability"
    traceability.mkdir(parents=True)
    (traceability / "requirements.json").write_text(
        json.dumps({"REQ": {"req_id": "REQ", "parent_id": "ROOT"}}), encoding="utf-8"
    )
    (tmp_path / ".arc" / "global_handoffs.json").write_text(
        json.dumps(
            {
                "REQ": {
                    "schema_version": 2,
                    "test_type": "E2E",
                    "test_files": ["backend/test.ts"],
                    "observation": "expected alert",
                    "failure_signature": "abc",
                    "repeat_count": 2,
                    "attempted_changes": ["clear stale state"],
                    "last_outcome": "still failed",
                    "do_not_repeat": ["clear only"],
                    "next_file": "frontend/src/App.tsx",
                    "next_file_source": "model_handoff",
                    "next_action": "validate first",
                }
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / ".arc" / "node_sessions" / "REQ.json"
    session.parent.mkdir()
    session.write_text(
        json.dumps(
            {
                "recent_failure_summary": "duplicate raw failure",
                "checkpoint_handoff_summary": (
                    "Latest failure: old failure.\n"
                    "Next concrete edit target: frontend/src/Old.tsx — old action"
                ),
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_memory_workspace(str(tmp_path), "REQ")

    assert report["quality_score"] == 1.0
    assert report["work_packet"]["next_file"] == "frontend/src/App.tsx"
    assert report["legacy_duplicate_failure_bytes_avoided"] == 2 * len("duplicate raw failure")
