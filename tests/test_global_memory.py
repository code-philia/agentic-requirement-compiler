from __future__ import annotations

from agents.context.global_memory import GlobalProjectMemory


class FakeStore:
    requirements = [
        {"req_id": "BASE", "parent_id": "ROOT"},
        {"req_id": "CHILD", "parent_id": "ROOT", "dependencies": ["BASE"]},
        {"req_id": "OTHER", "parent_id": "ROOT"},
        {"req_id": "ROOT", "children_ids": ["BASE", "CHILD", "OTHER"]},
    ]

    def list_requirements(self):
        return self.requirements

    def get_requirement(self, req_id):
        return next((row for row in self.requirements if row["req_id"] == req_id), None)


def failure(path: str = "backend/src/login.py", message: str = "expected 200, received 500") -> str:
    return f"Exit Code: 1\nFAIL tests/test_login.py\nError: {message}\n at {path}:12:4"


def test_memory_is_compact_and_does_not_duplicate_traceability(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    context = memory.refresh(node_id="CHILD")

    assert context.startswith("<global_project_memory>")
    assert '"parent":"ROOT"' in context
    assert "interfaces" not in context
    assert "tests" not in context
    assert len(context) < 1000
    assert (tmp_path / ".arc" / "global_memory.md").exists()


def test_pass_records_compact_verified_pattern_and_failure_invalidates_it(tmp_path):
    class StoreWithInterfaces(FakeStore):
        def list_interfaces(self, req_id=None):
            rows = [
                {
                    "interface_id": "API-LOGIN",
                    "type": "API",
                    "file_path": "backend/src/login.py",
                    "req_ids": ["CHILD"],
                    "callers": ["UI-LOGIN"],
                    "callees": ["FUNC-LOGIN"],
                }
            ]
            return rows if req_id in (None, "CHILD") else []

    memory = GlobalProjectMemory(str(tmp_path), StoreWithInterfaces())
    memory.record_test_result(
        requirement="CHILD",
        test_type="Integration",
        test_files=["tests/test_login.py"],
        output="Exit Code: 0",
    )

    context = memory.refresh(node_id="CHILD")
    assert '"verified_patterns":[' in context
    assert '"verified_owners":["backend/src/login.py"]' in context
    assert '"architecture":[{"interface":"API-LOGIN"' in context

    memory.record_test_result(
        requirement="CHILD",
        test_type="Integration",
        test_files=["tests/test_login.py"],
        output=failure(),
    )

    assert '"verified_patterns":[]' in memory.refresh(node_id="CHILD")


def test_verified_pattern_only_claims_interfaces_covered_by_layer_tests(tmp_path):
    class StoreWithLayerCoverage(FakeStore):
        def list_tests(self, req_id=None):
            return [
                {
                    "type": "Unit",
                    "file_path": "backend/tests/login.test.js",
                    "interface_ids": ["FUNC-LOGIN"],
                }
            ]

        def list_interfaces(self, req_id=None):
            return [
                {
                    "interface_id": "FUNC-LOGIN",
                    "file_path": "backend/src/login.js",
                    "req_ids": ["CHILD"],
                },
                {
                    "interface_id": "UI-LOGIN",
                    "file_path": "frontend/src/Login.tsx",
                    "req_ids": ["CHILD"],
                },
            ]

    memory = GlobalProjectMemory(str(tmp_path), StoreWithLayerCoverage())
    memory.record_test_result(
        requirement="CHILD",
        test_type="Unit",
        test_files=["backend/tests/login.test.js"],
        output="Exit Code: 0",
    )

    context = memory.refresh(node_id="CHILD")

    assert '"verified_owners":["backend/src/login.js"]' in context
    assert '"verified_interfaces":["FUNC-LOGIN"]' in context


def test_memory_can_be_disabled_for_controlled_ab_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ARC_GLOBAL_MEMORY_ENABLED", "off")
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())

    memory.record_test_result(
        requirement="CHILD", test_type="E2E", test_files=[], output=failure()
    )

    assert memory.refresh(node_id="CHILD") == ""
    assert not (tmp_path / ".arc" / "global_handoffs.json").exists()


def test_actionable_failure_survives_generic_budget_error(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_test_result(
        requirement="CHILD",
        test_type="E2E",
        test_files=["tests/test_login.py"],
        output=failure(),
    )
    memory.record_test_result(
        requirement="CHILD",
        test_type="E2E",
        test_files=["tests/test_login.py"],
        output="Exit Code: 1\nSTDERR:\nrun_tests budget exhausted for E2E: 10/10.",
    )

    context = memory.refresh(node_id="CHILD")
    assert "expected 200, received 500" in context
    assert "budget exhausted" not in context
    assert '"next_file":"backend/src/login.py"' in context


def test_only_matching_layer_success_clears_failure(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_test_result(
        requirement="CHILD", test_type="E2E", test_files=["tests/test_login.py"], output=failure()
    )
    memory.record_test_result(
        requirement="CHILD", test_type="Unit", test_files=[], output="Exit Code: 0"
    )
    assert "received 500" in memory.refresh(node_id="CHILD")

    memory.record_test_result(
        requirement="CHILD", test_type="E2E", test_files=[], output="Exit Code: 0"
    )
    assert "received 500" not in memory.refresh(node_id="CHILD")


def test_handoffs_are_scoped_to_current_parent_and_dependencies(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_test_result(
        requirement="BASE", test_type="Integration", test_files=[], output=failure("backend/base.py")
    )
    memory.record_test_result(
        requirement="OTHER", test_type="Integration", test_files=[], output=failure("backend/other.py")
    )

    context = memory.refresh(node_id="CHILD")
    assert "backend/base.py" in context
    assert "backend/other.py" not in context


def test_refresh_replaces_stale_audit_snapshot(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.refresh(node_id="CHILD")
    memory.path.write_text("stale", encoding="utf-8")

    memory.refresh(node_id="CHILD")

    assert memory.path.read_text(encoding="utf-8").startswith("# ARC global project memory")


def test_next_file_prefers_product_owner_over_generated_artifact():
    fingerprint = "\n".join(
        [
            "FAIL backend/tests/auth.test.js",
            "at backend/src/database/db_runtime.js:54:14",
            "Error Context: backend/test-results/auth/error-context.md",
        ]
    )

    assert GlobalProjectMemory._next_file(fingerprint) == "backend/src/database/db_runtime.js"


def test_checkpoint_handoff_can_restore_known_failure(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        fingerprint="Expected alert replacement at frontend/src/pages/LoginPage.tsx:42",
    )

    context = memory.refresh(node_id="CHILD")

    assert '"test_type":"E2E"' in context
    assert '"next_file":"frontend/src/pages/LoginPage.tsx"' in context


def test_structured_handoff_preserves_attempt_and_new_hypothesis(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        fingerprint="""LATEST_FAILURE: stale duplicate alert remains
ATTEMPTED_CHANGE: clear error state when an input changes
OBSERVED_OUTCOME: stale alert cleared, but no validation alert replaced it
DO_NOT_REPEAT: clearing stale state alone does not validate the form
NEXT_FILE: frontend/src/pages/LoginPage.tsx
NEXT_ACTION: validate malformed email before starting the API request""",
    )

    context = memory.refresh(node_id="CHILD")

    assert '"attempted_changes":["clear error state when an input changes"]' in context
    assert '"do_not_repeat":["clearing stale state alone does not validate the form"]' in context
    assert '"next_action":"validate malformed email before starting the API request"' in context


def test_later_generic_handoff_cannot_overwrite_exact_compiler_diagnosis(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        fingerprint=(
            "LATEST_FAILURE: SyntaxError: Duplicate export at "
            "backend/src/services/auth_service.js:116:1\n"
            "NEXT_FILE: backend/src/services/auth_service.js\n"
            "NEXT_ACTION: remove the duplicate export block at line 116"
        ),
    )
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        fingerprint=(
            "LATEST_FAILURE: backend startup timed out after 20 seconds\n"
            "NEXT_FILE: backend/src/server.js\n"
            "NEXT_ACTION: investigate ports and startup configuration"
        ),
    )

    context = memory.refresh(node_id="CHILD")

    assert '"next_file":"backend/src/services/auth_service.js"' in context
    assert '"next_action":"remove the duplicate export block at line 116"' in context
    assert "SyntaxError: Duplicate export" in context


def test_resolved_exact_diagnosis_advances_to_new_failure_target(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        fingerprint=(
            "LATEST_FAILURE: SyntaxError in backend/src/services/auth_service.js:116:1\n"
            "NEXT_FILE: backend/src/services/auth_service.js\n"
            "NEXT_ACTION: remove the duplicate export"
        ),
    )
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        fingerprint=(
            "LATEST_FAILURE: backend now starts, but login stays on /login\n"
            "ATTEMPTED_CHANGE: repaired backend/src/services/auth_service.js\n"
            "OBSERVED_OUTCOME: startup is resolved and the failure moved to login behavior\n"
            "DO_NOT_REPEAT: auth_service.js syntax is resolved and no longer explains the failure\n"
            "NEXT_FILE: frontend/src/features/login/LoginPage.tsx\n"
            "NEXT_ACTION: repair submit, alert, session update, and navigation"
        ),
    )

    context = memory.refresh(node_id="CHILD")

    assert '"next_file":"frontend/src/features/login/LoginPage.tsx"' in context
    assert '"next_action":"repair submit, alert, session update, and navigation"' in context
    assert "login stays on /login" in context


def test_repeated_failure_increments_repeat_count_without_losing_next_action(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    output = failure(message="expected validation alert, received none")
    memory.record_test_result(
        requirement="CHILD", test_type="E2E", test_files=["tests/test_login.py"], output=output
    )
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["tests/test_login.py"],
        fingerprint="NEXT_FILE: backend/src/login.py\nNEXT_ACTION: validate before persistence",
    )
    memory.record_test_result(
        requirement="CHILD", test_type="E2E", test_files=["tests/test_login.py"], output=output
    )

    context = memory.refresh(node_id="CHILD")

    assert '"repeat_count":2' in context
    assert '"next_action":"validate before persistence"' in context


def test_refresh_migrates_legacy_node_session_to_owner_work_packet(tmp_path):
    class StoreWithArtifacts(FakeStore):
        def list_interfaces(self, req_id=None):
            return [
                {
                    "file_path": "frontend/src/pages/LoginPage.tsx",
                    "implemented": False,
                },
                {"file_path": "backend/src/auth.py", "implemented": True},
            ]

        def list_tests(self, req_id=None):
            return [{"type": "E2E", "file_path": "backend/test-e2e/login.test.ts"}]

    session = tmp_path / ".arc" / "node_sessions" / "CHILD.json"
    session.parent.mkdir(parents=True)
    session.write_text(
        '{"recent_failure_summary":"Exit Code: 1\\nError: expected alert, received none",'
        '"tdd_handoff":{"last_test_type":"E2E"}}',
        encoding="utf-8",
    )

    context = GlobalProjectMemory(str(tmp_path), StoreWithArtifacts()).refresh(node_id="CHILD")

    assert '"next_file":"frontend/src/pages/LoginPage.tsx"' in context
    assert '"test_files":["backend/test-e2e/login.test.ts"]' in context
    assert (tmp_path / ".arc" / "global_handoffs.json").exists()


def test_long_structured_handoff_parses_before_field_truncation(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=[],
        fingerprint=(
            "LATEST_FAILURE: precise alert failure\n"
            "ATTEMPTED_CHANGE: first repair\n"
            f"OBSERVED_OUTCOME: {'x' * 1800}\n"
            "DO_NOT_REPEAT: first repair alone\n"
            "NEXT_FILE: frontend/src/pages/LoginPage.tsx\n"
            "NEXT_ACTION: validate before the request"
        ),
    )

    context = memory.refresh(node_id="CHILD")

    assert '"observation":"precise alert failure"' in context
    assert '"next_file":"frontend/src/pages/LoginPage.tsx"' in context
    assert '"next_action":"validate before the request"' in context


def test_checkpoint_handoff_retargets_protected_test_to_product_owner(tmp_path):
    memory = GlobalProjectMemory(str(tmp_path), FakeStore())
    memory.record_handoff(
        requirement="CHILD",
        test_type="E2E",
        test_files=["backend/test-e2e/login.test.ts"],
        protected_files=["backend/test-e2e/login.test.ts"],
        fingerprint=(
            "LATEST_FAILURE: session assertion failed\n"
            "NEXT_FILE: backend/test-e2e/login.test.ts\n"
            "NEXT_ACTION: align frontend/src/pages/LoginPage.tsx with the session contract"
        ),
    )

    context = memory.refresh(node_id="CHILD")

    assert '"next_file":"frontend/src/pages/LoginPage.tsx"' in context
