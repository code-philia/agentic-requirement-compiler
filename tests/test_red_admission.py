from app_type_handler.test_results import classify_red_result


def test_red_admission_accepts_behavior_failure():
    result = classify_red_result(
        "Exit Code: 1\nAssertionError: expected 501 to be 201\n"
    )

    assert result["kind"] == "valid_red"


def test_red_admission_rejects_test_collection_failure():
    result = classify_red_result(
        "Exit Code: 1\nTest Files 1 failed\nTests no tests\n"
    )

    assert result == {"kind": "invalid_test", "reason": "no_tests_collected"}


def test_red_admission_rejects_ambiguous_oracle():
    result = classify_red_result(
        "Exit Code: 1\nstrict mode violation: getByRole('link') resolved to 2 elements\n"
    )

    assert result == {"kind": "invalid_test", "reason": "ambiguous_oracle"}


def test_red_admission_routes_harness_failure_away_from_product():
    result = classify_red_result(
        "Exit Code: 1\nE2E database preparation failed before backend startup.\n"
    )

    assert result == {"kind": "invalid_harness", "reason": "e2e_preparation"}


def test_red_admission_records_redundant_baseline_green():
    result = classify_red_result("Exit Code: 0\nTests 2 passed\n")

    assert result["kind"] == "baseline_green"
