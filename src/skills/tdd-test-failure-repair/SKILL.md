---
name: tdd-test-failure-repair
description: Use for ARC TestDrivenDeveloper work after run_tests fails or when repairing implementation against a current failing batch.
---

# tdd-test-failure-repair

Use this skill after `run_tests` reports a failing current batch.

## Instructions

1. Treat the latest `run_tests` output as the source of truth.
2. Classify the failure before editing: implementation logic, boundary wiring, selector/render state, persistence/test database, framework/config, generated test defect, or test content.
3. Re-read the failing test file and the nearest owner implementation file before making another edit.
4. Compare the failure against the current UI/API/FUNC/DB interface chain; if a downstream layer is missing or disconnected, repair the chain rather than patching only the surface assertion.
5. If the same fingerprint repeats, replace the current hypothesis or move one layer outward instead of patching neighboring files from momentum.
6. If product behavior is wrong, edit product code. If a generated test is invalid, contradictory, brittle, or incompatible with the runner, edit the test while preserving requirement intent. If runner setup is wrong, edit build/test configuration.
7. Make one minimal contract-preserving fix, then call `run_tests` again.
8. Do not stop after failing runs while the active layer's tool budget remains. Do not declare blocked, failed, impossible, or out-of-scope as a final answer.
9. Say `IMPLEMENTED` only when the system indicates the active layer passed and no scheduled layer remains blocked by the current session.
10. Each current-node test layer has an independent `run_tests` budget of 20 calls. After a failure, spend enough effort to localize and repair the cause before consuming the next call.
11. For auth/session failures, use the auth-session-consistency skill and repair the shared session path first: session persistence, current-session API, client session loader, global provider/state, shell/header consumers, and route behavior.
12. If Playwright reports an unknown fixture parameter, inspect the `test.extend` block and make the fixture name and `use(...)` value match before rerunning.
13. If Playwright or Testing Library reports multiple matches for a label, do not retry with a broader selector. Use an exact accessible role, a stable id, or a scoped locator that identifies one element.
14. If the same backend database singleton error repeats, inspect the database runtime helper and initialization lifecycle before changing unrelated tests or UI files.
