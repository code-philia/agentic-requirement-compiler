import asyncio
from collections import defaultdict

from agents.context.global_memory import GlobalProjectMemory
from core.phases import WorkflowPhaseRunner


class FakeTraceability:
    def __init__(self):
        self.status_updates = []

    def set_test_pass_statuses(self, statuses):
        self.status_updates.append(dict(statuses))


class ReplayThenRecoverHandler:
    def __init__(self):
        self.calls = []
        self.counts = defaultdict(int)

    async def run_test_group(self, test_type, test_files):
        self.calls.append(test_type)
        self.counts[test_type] += 1
        # Integration passes in the implementation loop, regresses in the first
        # zero-token replay, then passes after the resumed agent repair.
        if test_type == "Integration" and self.counts[test_type] == 2:
            return "Exit Code: 1\nSTDERR:\nexpected session cookie to remain present\n"
        return f"Runner: Fake\nBatch Test Type: {test_type}\nExit Code: 0\n"


class RepairingDeveloper:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        result = await kwargs["run_tests_executor"](kwargs["test_type"], None)
        return "IMPLEMENTED" if "Exit Code: 0" in result else "LATEST_FAILURE"

    def get_last_verifier_report(self):
        return ""


def test_zero_token_regression_reopens_failed_layer_and_replays_all(monkeypatch, tmp_path):
    traceability = FakeTraceability()
    handler = ReplayThenRecoverHandler()
    developer = RepairingDeveloper()
    logs = []

    runner = WorkflowPhaseRunner.__new__(WorkflowPhaseRunner)
    runner.workspace_path = str(tmp_path)
    runner.test_driven_developer = developer
    runner.app_handler = handler
    runner.log_cb = lambda agent, message, status, node: logs.append((agent, message, status, node))

    monkeypatch.setattr(
        WorkflowPhaseRunner,
        "traceability",
        property(lambda self: traceability),
    )
    monkeypatch.setattr("core.phases.sessions.load_node_session", lambda node_id: {})
    monkeypatch.setattr("core.phases.sessions.merge_node_session", lambda node_id, patch: patch)
    monkeypatch.setattr("core.phases.context_pipeline.cache.invalidate_db_layers", lambda node_id: None)
    monkeypatch.setattr("core.phases.context_pipeline.cache.invalidate_file_layers", lambda node_id: None)
    monkeypatch.setattr(GlobalProjectMemory, "record_test_result", lambda *args, **kwargs: None)

    tests = [
        {"test_id": "U", "type": "Unit", "file_path": "tests/unit.test.js"},
        {"test_id": "I", "type": "Integration", "file_path": "tests/integration.test.js"},
        {"test_id": "E", "type": "E2E", "file_path": "tests/e2e.test.js"},
    ]

    ok = asyncio.run(
        runner._run_tdd_for_node(
            node_id="REQ-1.1",
            tests=tests,
            # Every layer consumes its normal budget before replay. Recovery
            # must therefore use the separately reserved replay allowance.
            run_tests_budget=1,
            max_agent_sessions=4,
        )
    )

    assert ok is True
    assert handler.calls == [
        "Unit",
        "Integration",
        "E2E",
        "Unit",
        "Integration",
        "Integration",
        "Unit",
        "Integration",
        "E2E",
    ]
    recovery_calls = [
        call for call in developer.calls
        if "REGRESSION_REPLAY_FAILURE" in call["previous_failure_summary"]
    ]
    assert len(recovery_calls) == 1
    assert recovery_calls[0]["test_type"] == "Integration"
    assert recovery_calls[0]["continue_across_layers"] is False
    assert any("Resuming TDD regression recovery cycle 1" in message for _, message, _, _ in logs)
    assert any("after regression recovery cycle 1" in message for _, message, _, _ in logs)
