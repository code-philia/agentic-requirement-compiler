from agents.runtime.stage_discipline import StageDisciplineMiddleware


def test_checkpoint_protected_test_cannot_be_written():
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        protected_write_paths=["backend/test-e2e/req-1.e2e.test.ts"],
    )

    blocked = middleware._validate_write(
        {"file_path": "/workspace/backend/test-e2e/req-1.e2e.test.ts"}
    )

    assert blocked is not None
    assert "immutable evidence" in blocked
    assert middleware._validate_write({"file_path": "/workspace/frontend/src/App.tsx"}) is None


def test_dynamic_write_guard_stops_edits_after_passing_validation():
    passed = {"value": False}
    middleware = StageDisciplineMiddleware(
        stage="implementation",
        write_block_reason=lambda: "The test layer already passed." if passed["value"] else None,
    )

    assert middleware._validate_write({"file_path": "/workspace/frontend/src/App.tsx"}) is None
    passed["value"] = True

    blocked = middleware._validate_write({"file_path": "/workspace/frontend/src/App.tsx"})
    assert blocked == "The test layer already passed."


def test_tdd_red_and_green_modes_separate_test_and_product_writes():
    mode = {"value": "red"}
    middleware = StageDisciplineMiddleware(stage="tdd", tdd_mode=lambda: mode["value"])

    assert middleware._validate_write({"file_path": "/workspace/backend/tests/auth.test.js"}) is None
    assert "RED mode" in middleware._validate_write({"file_path": "/workspace/backend/src/auth.js"})

    mode["value"] = "green"
    assert middleware._validate_write({"file_path": "/workspace/backend/src/auth.js"}) is None
    assert "GREEN mode" in middleware._validate_write({"file_path": "/workspace/backend/tests/auth.test.js"})

    mode["value"] = "sealed"
    assert "sealed" in middleware._validate_write({"file_path": "/workspace/backend/src/auth.js"})
