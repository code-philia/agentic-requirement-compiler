from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .code_binding import CODE_BINDING_READY, CodeTargetResolver
from .test_generation import TESTS_FROZEN
from .model_client import StructuredModel
from .test_runner import TestCommandResult, TestRunResult


FAILURE_ANALYSIS_SCHEMA_VERSION = 2
FAILURE_CLASSES = (
    "INFRASTRUCTURE",
    "TEST_RUN_TIMEOUT",
    "TEST_MATERIALIZATION",
    "TYPE_CONTRACT",
    "IMPLEMENTATION_BEHAVIOR",
    "VISUAL_BEHAVIOR",
    "TEST_OR_CONTRACT_INCONSISTENT",
    "DEFERRED_DEPENDENCY",
)


@dataclass(frozen=True, slots=True)
class StackFrame:
    file: str
    line: int | None = None
    column: int | None = None
    symbol: str | None = None


@dataclass(slots=True)
class TestFailureReport:
    requirement_id: str
    iteration: int
    test_id: str | None
    test_ids: list[str]
    layer: str | None
    phase: str
    failure_class: str
    message: str
    stack_frames: list[StackFrame]
    target_modules: list[str]
    writable_targets: list[dict[str, Any]]
    read_only_dependencies: list[dict[str, Any]]
    changed_files: list[str]
    failure_fingerprint: str
    command: list[str] = field(default_factory=list)
    diagnostic_output: str = ""
    deferred_dependency_modules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FailureAnalysisResult:
    requirement_id: str
    reports: list[TestFailureReport]
    errors: list[str] = field(default_factory=list)
    agent_context: str = ""
    schema_version: int = FAILURE_ANALYSIS_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FailureAnalyzer:
    """Convert runner output into requirement- and binding-aware failure reports."""

    def __init__(self, output_root: Path, model: StructuredModel | None = None) -> None:
        self.output_root = output_root.expanduser().resolve()
        # Kept in the constructor for compatibility with existing callers. E2E
        # repair feedback no longer invokes a synthesis model; the raw runner
        # evidence is passed directly to the implementation agent.
        self._model = model

    def classify(
        self,
        test_run: TestRunResult,
        *,
        code_binding_registry: dict[str, Any],
        test_manifest: dict[str, Any],
        iteration: int,
        changed_files: list[str] | None = None,
    ) -> FailureAnalysisResult:
        requirement_id = str(test_run.requirement_id).strip()
        errors = self._validate_inputs(
            requirement_id=requirement_id,
            code_binding_registry=code_binding_registry,
            test_manifest=test_manifest,
            iteration=iteration,
        )
        if errors:
            return FailureAnalysisResult(
                requirement_id=requirement_id,
                reports=[],
                errors=errors,
            )
        if test_run.ok:
            return FailureAnalysisResult(requirement_id=requirement_id, reports=[])

        resolver = CodeTargetResolver(code_binding_registry)
        resolved = resolver.resolve_requirement_targets(requirement_id)
        binding_by_id = {
            str(row.get("module_id", "")): row
            for row in code_binding_registry.get("code_bindings", [])
            if isinstance(row, dict) and str(row.get("module_id", ""))
        }
        modules_by_file = {
            _normalize_relative(str(row.get("file", ""))): [
                str(value) for value in row.get("module_ids", []) if str(value)
            ]
            for row in code_binding_registry.get("file_index", [])
            if isinstance(row, dict) and str(row.get("file", ""))
        }
        test_rows = [
            row
            for row in test_manifest.get("tests", [])
            if isinstance(row, dict)
            and str(row.get("requirement_id", "")) == requirement_id
        ]
        # Every module this requirement may edit. A stub hit outside that set is a
        # dependency the schedule has not built yet, not a defect in this node.
        owned_module_ids = {
            str(value)
            for key in ("owned", "writable")
            for value in resolved.get(key, [])
            if str(value)
        }
        reports: list[TestFailureReport] = []
        failed_commands = [row for row in test_run.commands if row.status != "PASSED"]
        for command_result in failed_commands:
            reports.append(
                self._report_for_command(
                    requirement_id=requirement_id,
                    iteration=iteration,
                    command_result=command_result,
                    test_rows=test_rows,
                    resolved_targets=resolved,
                    binding_by_id=binding_by_id,
                    modules_by_file=modules_by_file,
                    changed_files=changed_files or [],
                    owned_module_ids=owned_module_ids,
                )
            )

        if not reports:
            reports.append(
                self._report_for_runner_failure(
                    requirement_id=requirement_id,
                    iteration=iteration,
                    test_run=test_run,
                    resolved_targets=resolved,
                    binding_by_id=binding_by_id,
                    changed_files=changed_files or [],
                )
            )
        return FailureAnalysisResult(
            requirement_id=requirement_id,
            reports=reports,
            agent_context=self._agent_context(
                test_run,
                reports,
                test_rows=test_rows,
                binding_by_id=binding_by_id,
            ),
        )

    def _agent_context(
        self,
        test_run: TestRunResult,
        reports: list[TestFailureReport],
        *,
        test_rows: list[dict[str, Any]],
        binding_by_id: dict[str, dict[str, Any]],
    ) -> str:
        """Turn failed Playwright tests into one readable repair context.

        Routing still uses the deterministic reports above. The implementation
        model receives only failed-test sections instead of nested report objects.
        """

        e2e_commands = [
            command
            for command in test_run.commands
            if str(command.layer or "").upper() == "E2E"
            and command.status != "PASSED"
        ]
        if not e2e_commands:
            plain_context = "\n\n".join(
                "FAILURE ANALYSIS\n"
                f"failure_class={report.failure_class}\n"
                f"phase={report.phase}\n"
                f"message={report.message}\n"
                f"target_modules={', '.join(report.target_modules) or '(not localized)'}\n"
                f"details={report.diagnostic_output or '(none)'}"
                for report in reports
            )
            warning = _failure_scope_warning(reports)
            return "\n\n".join(value for value in (warning, plain_context) if value)
        # E2E feedback is intentionally kept on the evidence path.  A second
        # model call tends to paraphrase the useful Playwright error, repeat
        # routing metadata, and hide the last successful browser action.  The
        # implementation agent gets the exact JSON error/call log instead.
        return self._direct_e2e_feedback(
            commands=e2e_commands,
            reports=reports,
        )

    def _direct_e2e_feedback(
        self,
        *,
        commands: list[TestCommandResult],
        reports: list[TestFailureReport],
    ) -> str:
        """Build a compact, evidence-first E2E repair context.

        This deliberately does not invoke the failure-analysis model.  JSON
        reporter records, pw:api progress, exact process errors, and the
        corresponding frozen test source are more useful to the repair agent
        than a second natural-language summary, especially when the runner was
        terminated before JSON was complete.
        """

        blocks: list[str] = []
        for command in commands:
            raw_stdout = command.raw_stdout or command.stdout
            raw_stderr = command.raw_stderr or command.stderr
            raw_report = "\n".join(value for value in (raw_stdout, raw_stderr) if value).strip()
            all_tests = _playwright_test_results(raw_report)
            failed_tests = [row for row in all_tests if row.get("status") not in {"passed", "skipped", "pending"}]
            pw_api_lines = [
                line
                for line in (raw_stderr + "\n" + raw_stdout).splitlines()
                if "pw:api" in line
            ]
            process_lines = [
                line.rstrip()
                for line in (raw_stderr + "\n" + raw_stdout).splitlines()
                if line.strip() and "pw:api" not in line
            ]
            last_url = _last_observed_url([*pw_api_lines, *process_lines])
            exact_errors = _e2e_exact_errors(
                command=command,
                failed_tests=failed_tests,
                raw_stderr=raw_stderr,
                raw_stdout=raw_stdout,
            )

            parts: list[str] = [
                "E2E EXECUTION FEEDBACK",
                f"status: {'TIMEOUT' if command.timed_out else command.status}",
                f"reporter_complete: {'true' if bool(all_tests) else 'false'}",
                "CAPTURE FILES",
                f"stdout: {command.capture_stdout_path or '(unavailable)'}",
                f"stderr: {command.capture_stderr_path or '(unavailable)'}",
            ]
            if exact_errors:
                parts.extend(["EXACT ERROR", exact_errors])

            if all_tests:
                progress = "\n".join(
                    _format_playwright_progress(row) for row in all_tests
                )
                parts.extend(["TEST PROGRESS", progress])
            else:
                parts.extend([
                    "TEST PROGRESS",
                    "Playwright JSON was incomplete; no per-test result was emitted.",
                ])
                if raw_stdout.strip():
                    # JSON is intentionally not summarized or discarded when
                    # the process was interrupted.  The tail often contains
                    # the last serialized error/call log even though the root
                    # object cannot be decoded as a whole.
                    parts.extend([
                        "PARTIAL REPORTER OUTPUT",
                        raw_stdout[-20_000:],
                    ])

            parts.extend([
                "BROWSER STEPS (pw:api, ordered)",
                "\n".join(pw_api_lines) if pw_api_lines else "(no pw:api lines captured)",
            ])
            if pw_api_lines:
                parts.extend(["LAST OBSERVED STEP", pw_api_lines[-1]])
            if last_url:
                parts.extend(["LAST OBSERVED URL", last_url])
            if command.timed_out:
                timeout_stage = (
                    "browser_action"
                    if pw_api_lines
                    else "test_execution"
                    if all_tests
                    else "web_server_or_browser_bootstrap"
                )
                parts.extend(["TIMEOUT STAGE", timeout_stage])
            if process_lines:
                parts.extend([
                    "SERVER / PROCESS OUTPUT TAIL",
                    "\n".join(process_lines[-200:])[-20_000:],
                ])

            if failed_tests:
                for failed in failed_tests:
                    parts.extend([
                        "FAILED TEST",
                        f"title: {failed.get('title') or '(untitled)'}",
                        f"test_file: {failed.get('file') or '(unknown)'}",
                        f"error:\n{failed.get('error') or '(no structured error)'}",
                    ])
                    source = self._read_test_source(str(failed.get("file", "")))
                    if source:
                        parts.extend(["TEST SOURCE", source])
            else:
                for test_file in command.test_files:
                    source = self._read_test_source(str(test_file))
                    if source:
                        parts.extend(["TEST SOURCE", source])

            blocks.append("\n".join(parts))

        warning = _failure_scope_warning(reports)
        return "\n\n".join(value for value in (warning, *blocks) if value)

    def _read_test_source(self, test_file: str) -> str:
        normalized = _normalize_relative(test_file)
        candidates = [normalized]
        if normalized.startswith("tests/"):
            candidates.append(normalized.removeprefix("tests/"))
        else:
            candidates.append(f"tests/{normalized}")
        for candidate in candidates:
            if not candidate:
                continue
            try:
                path = (self.output_root / candidate).resolve()
                path.relative_to(self.output_root)
                return f"test_file: {candidate}\nsource:\n{path.read_text(encoding='utf-8')}"
            except (OSError, ValueError):
                continue
        return ""

    def _report_for_command(
        self,
        *,
        requirement_id: str,
        iteration: int,
        command_result: TestCommandResult,
        test_rows: list[dict[str, Any]],
        resolved_targets: dict[str, Any],
        binding_by_id: dict[str, dict[str, Any]],
        modules_by_file: dict[str, list[str]],
        changed_files: list[str],
        owned_module_ids: set[str],
    ) -> TestFailureReport:
        output = _clean_output(
            "\n".join(
                value
                for value in (
                    command_result.error or "",
                    command_result.stderr,
                    command_result.stdout,
                )
                if value
            )
        )
        phase = _failure_phase(command_result, output)
        deferred = sorted(
            {
                str(value)
                for value in command_result.stub_hits
                if str(value) and str(value) not in owned_module_ids
            }
        )
        owned_stub_hits = {
            str(value)
            for value in command_result.stub_hits
            if str(value) in owned_module_ids
        }
        failure_class = _failure_class(
            command_result,
            phase,
            output,
            deferred_modules=deferred if not owned_stub_hits else [],
        )
        matching_tests = _matching_tests(command_result, output, test_rows)
        test_ids = sorted(
            {
                str(row.get("test_id", ""))
                for row in matching_tests
                if str(row.get("test_id", ""))
            }
        )
        stack_frames = self._stack_frames(output)
        # Runtime assertions begin with the modules exercised by the matching
        # test. A workspace typecheck is different: its source locations are
        # authoritative, and seeding it with every test target recreates the
        # cross-module pollution this analyzer is meant to remove.
        stub_modules = {
            str(value)
            for value in command_result.stub_hits
            if str(value)
        }
        target_modules = (
            set()
            if phase == "TYPECHECK"
            else set(stub_modules)
            if stub_modules
            else {
                str(value)
                for row in matching_tests
                for value in row.get("target_modules", [])
                if str(value)
            }
        )
        # Generated backend skeletons deliberately remain typecheckable. Their
        # runtime stub ledger is deterministic evidence that a DB/FUNC/API
        # module was reached but still has no implementation.
        target_modules.update(stub_modules)
        for frame in stack_frames:
            target_modules.update(modules_by_file.get(frame.file, []))
        # TypeScript often reports files as ``src/...`` while the binding
        # registry stores ``backend/src/...`` or ``frontend/src/...``.  Resolve
        # every known binding file mentioned anywhere in the diagnostic, not
        # only files that happened to produce a parsed stack frame.  This keeps
        # multi-file type contracts visible to the repair agent.
        target_modules.update(
            _diagnostic_module_ids(output, modules_by_file)
        )
        if phase == "TYPECHECK" and not target_modules:
            target_modules.update(
                _module_ids_for_files(changed_files, modules_by_file)
            )
        writable_targets, read_only_targets = _relevant_targets(
            resolved_targets,
            binding_by_id,
            target_modules,
        )
        message = _failure_message(output, command_result)
        fingerprint = _fingerprint(
            failure_class=failure_class,
            phase=phase,
            layer=command_result.layer,
            test_ids=test_ids,
            message=message,
            target_modules=target_modules,
        )
        return TestFailureReport(
            requirement_id=requirement_id,
            iteration=max(0, int(iteration)),
            test_id=test_ids[0] if len(test_ids) == 1 else None,
            test_ids=test_ids,
            layer=command_result.layer,
            phase=phase,
            failure_class=failure_class,
            message=message,
            stack_frames=stack_frames,
            target_modules=sorted(target_modules),
            writable_targets=writable_targets,
            read_only_dependencies=read_only_targets,
            changed_files=sorted({_normalize_relative(value) for value in changed_files}),
            failure_fingerprint=fingerprint,
            command=list(command_result.command),
            diagnostic_output=output,
            deferred_dependency_modules=deferred,
        )

    def _report_for_runner_failure(
        self,
        *,
        requirement_id: str,
        iteration: int,
        test_run: TestRunResult,
        resolved_targets: dict[str, Any],
        binding_by_id: dict[str, dict[str, Any]],
        changed_files: list[str],
    ) -> TestFailureReport:
        message = "\n".join(test_run.errors) or f"Test run ended with {test_run.status}."
        failure_class = (
            "TEST_OR_CONTRACT_INCONSISTENT"
            if any(
                token in message
                for token in ("TEST_INTEGRITY_FAILED", "TEST_MANIFEST_INVALID")
            )
            else "INFRASTRUCTURE"
        )
        writable_targets, read_only_targets = _relevant_targets(
            resolved_targets,
            binding_by_id,
            set(),
        )
        fingerprint = _fingerprint(
            failure_class=failure_class,
            phase="RUNNER",
            layer=None,
            test_ids=test_run.selected_test_ids,
            message=message,
            target_modules=set(),
        )
        return TestFailureReport(
            requirement_id=requirement_id,
            iteration=max(0, int(iteration)),
            test_id=(
                test_run.selected_test_ids[0]
                if len(test_run.selected_test_ids) == 1
                else None
            ),
            test_ids=sorted(set(test_run.selected_test_ids)),
            layer=None,
            phase="RUNNER",
            failure_class=failure_class,
            message=message,
            stack_frames=[],
            target_modules=[],
            writable_targets=writable_targets,
            read_only_dependencies=read_only_targets,
            changed_files=sorted({_normalize_relative(value) for value in changed_files}),
            failure_fingerprint=fingerprint,
            diagnostic_output=message,
        )

    def _stack_frames(self, output: str) -> list[StackFrame]:
        frames: list[StackFrame] = []
        seen: set[tuple[str, int | None, int | None, str | None]] = set()
        pattern = re.compile(
            r"(?:^|\s)(?:at\s+(?:(?P<symbol>[^\s(]+)\s+)?\()?"
            r"(?P<file>(?:file://)?(?:[A-Za-z]:[\\/]|/)?[^\s():]+\.(?:ts|tsx|js|jsx))"
            r":(?P<line>\d+)(?::(?P<column>\d+))?",
            re.MULTILINE,
        )
        for match in pattern.finditer(output):
            file = self._workspace_file(match.group("file"))
            if file is None or "/node_modules/" in f"/{file}":
                continue
            frame = StackFrame(
                file=file,
                line=int(match.group("line")) if match.group("line") else None,
                column=(
                    int(match.group("column")) if match.group("column") else None
                ),
                symbol=match.group("symbol"),
            )
            key = (frame.file, frame.line, frame.column, frame.symbol)
            if key not in seen:
                seen.add(key)
                frames.append(frame)
            if len(frames) >= 12:
                break
        return frames

    def _workspace_file(self, value: str) -> str | None:
        raw = value.removeprefix("file://").replace("\\", "/")
        path = Path(raw)
        if path.is_absolute():
            try:
                return path.resolve().relative_to(self.output_root).as_posix()
            except ValueError:
                return None
        normalized = _normalize_relative(raw)
        if normalized.startswith("../"):
            return None
        return normalized

    @staticmethod
    def _validate_inputs(
        *,
        requirement_id: str,
        code_binding_registry: dict[str, Any],
        test_manifest: dict[str, Any],
        iteration: int,
    ) -> list[str]:
        errors: list[str] = []
        if not requirement_id:
            errors.append("ARC4510 FAILURE_ANALYSIS_INVALID: requirement_id is required.")
        if code_binding_registry.get("status") != CODE_BINDING_READY:
            errors.append(
                "ARC4510 FAILURE_ANALYSIS_INVALID: Code Binding Registry is not ready."
            )
        if test_manifest.get("status") != TESTS_FROZEN:
            errors.append(
                "ARC4510 FAILURE_ANALYSIS_INVALID: Test Manifest is not frozen."
            )
        if not isinstance(test_manifest.get("tests"), list):
            errors.append(
                "ARC4510 FAILURE_ANALYSIS_INVALID: Test Manifest has no tests table."
            )
        if not isinstance(iteration, int) or iteration < 0:
            errors.append(
                "ARC4510 FAILURE_ANALYSIS_INVALID: iteration must be a non-negative integer."
            )
        if not errors:
            try:
                CodeTargetResolver(code_binding_registry).resolve_requirement_targets(
                    requirement_id
                )
            except KeyError as exc:
                errors.append(f"ARC4510 FAILURE_ANALYSIS_INVALID: {exc}")
        return errors


def _matching_tests(
    command: TestCommandResult,
    output: str,
    test_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    file_set = {_normalize_relative(value) for value in command.test_files}
    candidates = [
        row
        for row in test_rows
        if _normalize_relative(str(row.get("test_file", ""))) in file_set
        and (command.layer is None or str(row.get("layer", "")).upper() == command.layer)
    ]
    title_matches = [
        row
        for row in candidates
        if str(row.get("title", "")).strip()
        and str(row.get("title", "")).strip() in output
    ]
    return title_matches or candidates


def _playwright_test_results(raw_report: str) -> list[dict[str, Any]]:
    """Extract every completed Playwright test without synthesizing a diagnosis."""

    payload = _decode_playwright_json(raw_report)
    if not payload:
        return []
    records: list[dict[str, Any]] = []

    def walk_suite(suite: dict[str, Any], inherited_file: str = "") -> None:
        suite_file = str(suite.get("file") or inherited_file)
        for spec in suite.get("specs", []):
            if not isinstance(spec, dict):
                continue
            location = spec.get("location")
            location_file = location.get("file", "") if isinstance(location, dict) else ""
            spec_file = str(spec.get("file") or location_file or suite_file)
            tests = spec.get("tests", [])
            for test in tests:
                if not isinstance(test, dict):
                    continue
                results = [
                    result for result in test.get("results", [])
                    if isinstance(result, dict)
                ]
                if not results:
                    continue
                result = results[-1]
                error = _playwright_error(result.get("error"))
                errors = [
                    _playwright_error(value)
                    for value in result.get("errors", [])
                    if _playwright_error(value)
                ]
                if errors and error:
                    error = "\n".join([error, *errors])
                elif errors:
                    error = "\n".join(errors)
                records.append(
                    {
                        "file": spec_file,
                        "title": str(spec.get("title", "")),
                        "status": str(result.get("status", "unknown")).lower(),
                        "duration": result.get("duration"),
                        "error": error,
                        "call_log": _extract_call_log(error),
                        "stack": _extract_stack(error),
                    }
                )
        for child in suite.get("suites", []):
            if isinstance(child, dict):
                walk_suite(child, suite_file)

    for suite in payload.get("suites", []):
        if isinstance(suite, dict):
            walk_suite(suite)
    return records


def _format_playwright_progress(test: dict[str, Any]) -> str:
    status = str(test.get("status", "unknown")).upper()
    title = str(test.get("title", "(untitled)"))
    duration = test.get("duration")
    suffix = f" duration_ms={duration}" if duration is not None else ""
    return f"{status}: {title}{suffix}"


def _last_observed_url(lines: list[str]) -> str | None:
    """Return the last concrete browser/server URL visible in runner evidence."""

    matches: list[str] = []
    pattern = re.compile(r"https?://[^\s\]\[)'\"<>]+")
    for line in lines:
        matches.extend(pattern.findall(line))
    return matches[-1].rstrip(".,;:") if matches else None


def _e2e_exact_errors(
    *,
    command: TestCommandResult,
    failed_tests: list[dict[str, Any]],
    raw_stderr: str,
    raw_stdout: str,
) -> str:
    """Return errors in evidence order, keeping timeout text even without JSON."""

    if failed_tests:
        errors = [
            str(test.get("error", "")).strip()
            for test in failed_tests
            if str(test.get("error", "")).strip()
        ]
        if errors:
            return "\n\n".join(errors)

    lines = [
        line.rstrip()
        for line in raw_stderr.splitlines()
        if line.strip() and "pw:api" not in line
    ]
    if command.timed_out:
        timeout = next(
            (line for line in lines if "TEST_COMMAND_TIMEOUT" in line),
            None,
        )
        if timeout:
            return timeout
        return "TEST_COMMAND_TIMEOUT: the E2E process was terminated before the reporter completed."
    if not lines:
        lines = [line.rstrip() for line in raw_stdout.splitlines() if line.strip()]
    return "\n".join(lines[-200:])


def _decode_playwright_json(raw_report: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw_report):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw_report[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("suites"), list):
            return candidate
    return None


def _playwright_error(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return str(value).strip() if value is not None else ""
    message = str(value.get("message", "")).strip()
    stack = str(value.get("stack", "")).strip()
    if stack and stack != message:
        return "\n".join(value for value in (message, stack) if value)
    return message or stack


def _extract_call_log(error: str) -> str:
    marker = "Call log:"
    if marker in error:
        return error[error.index(marker) :].strip()
    return error


def _extract_stack(error: str) -> str:
    marker = "Call log:"
    if marker in error:
        return error[: error.index(marker)].strip()
    return error


def _failure_scope_warning(reports: list[TestFailureReport]) -> str:
    """Explain why a failure must be fixed by the compiler or another node."""

    classes = {report.failure_class for report in reports}
    warnings: list[str] = []
    if "TEST_MATERIALIZATION" in classes:
        warnings.append(
            "COMPILER_OWNED_WARNING [TEST_MATERIALIZATION]: test generation/materialization "
            "must be repaired by the compiler; ImplementationAgent must not edit frozen tests."
        )
    if "INFRASTRUCTURE" in classes:
        warnings.append(
            "COMPILER_OWNED_WARNING [INFRASTRUCTURE]: the test environment or process "
            "failed before business behavior could be evaluated; do not guess an application patch."
        )
    if "TEST_RUN_TIMEOUT" in classes:
        warnings.append(
            "TEST_RUN_TIMEOUT_WARNING: the browser process stopped before a complete "
            "Playwright result was emitted; use the exact pw:api steps and timeout "
            "error as evidence, and do not assume an assertion failure."
        )
    if "TEST_OR_CONTRACT_INCONSISTENT" in classes:
        warnings.append(
            "COMPILER_OWNED_WARNING [TEST_OR_CONTRACT_INCONSISTENT]: contract, generated glue, "
            "or frozen test inputs are inconsistent; keep tests, imports, routes, and glue read-only."
        )
    if "DEFERRED_DEPENDENCY" in classes:
        warnings.append(
            "DEPENDENCY_SCOPE_WARNING: the failure crossed into a dependency-owned module; "
            "repair that module under its owning requirement before retrying this node."
        )
    if any(report.read_only_dependencies for report in reports):
        warnings.append(
            "READ_ONLY_SCOPE_WARNING: read-only dependency source is provided for diagnosis only; "
            "the implementation context does not include dependency source as an edit target."
        )
    return "\n".join(dict.fromkeys(warnings))


def _failure_phase(command: TestCommandResult, output: str) -> str:
    lowered = output.lower()
    if command.phase == "TYPECHECK":
        return "TYPECHECK"
    if command.phase == "EXECUTION" and command.timed_out:
        # A killed process has not necessarily reached an assertion.  Keep the
        # timeout separate so the implementation agent does not patch business
        # code based on a runner/infrastructure failure.
        return "RUN_TIMEOUT"
    if command.error or any(
        token in lowered
        for token in (
            "econnrefused",
            "address already in use",
            "webserver process",
            "browser executable",
            "failed to launch",
        )
    ):
        return "BOOTSTRAP"
    if any(
        token in lowered
        for token in (
            "failed to resolve import",
            "cannot find module",
            "no test files found",
            "transform failed",
            "syntaxerror",
        )
    ):
        return "COLLECTION"
    if command.layer == "E2E" and any(
        token in lowered
        for token in ("tohavescreenshot", "screenshot", "pixelmatch", "visual")
    ):
        return "VISUAL_ASSERTION"
    return "ASSERTION"


def _failure_class(
    command: TestCommandResult,
    phase: str,
    output: str,
    *,
    deferred_modules: list[str] | None = None,
) -> str:
    lowered = output.lower()
    if phase == "BOOTSTRAP":
        return "INFRASTRUCTURE"
    if phase == "RUN_TIMEOUT":
        raw_evidence = "\n".join(
            value
            for value in (command.raw_stderr, command.raw_stdout, output)
            if value
        )
        if (
            str(command.layer or "").upper() == "E2E"
            and not command.stub_hits
            and "pw:api" not in raw_evidence
            and '"suites"' not in raw_evidence
        ):
            # No browser action or reporter record means the command timed out
            # during build/server/browser bootstrap. Retrying infrastructure is
            # safer than forcing an implementation patch without business evidence.
            return "INFRASTRUCTURE"
        # Once browser/reporter evidence exists, keep the timeout actionable so
        # the implementation agent can inspect the last observed step.
        return "TEST_RUN_TIMEOUT"
    if phase == "COLLECTION":
        return "TEST_MATERIALIZATION"
    if phase == "TYPECHECK" or re.search(r"\berror\s+ts\d{4}\b", lowered):
        return "TYPE_CONTRACT"
    if phase == "VISUAL_ASSERTION":
        return "VISUAL_BEHAVIOR"
    if any(
        token in output
        for token in (
            "TEST_INTEGRITY_FAILED",
            "TEST_MANIFEST_INVALID",
            "TEST_OR_CONTRACT_INCONSISTENT",
        )
    ):
        return "TEST_OR_CONTRACT_INCONSISTENT"
    # Checked last, so a real type error or a broken test still wins: a behavioral
    # failure that ran through another requirement's skeleton says nothing about this
    # requirement's code, and routing it to the implementation agent would only buy a
    # patch that works around a module the schedule builds later.
    if deferred_modules:
        return "DEFERRED_DEPENDENCY"
    return "IMPLEMENTATION_BEHAVIOR"


def _failure_message(output: str, command: TestCommandResult) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    meaningful = [
        line
        for line in lines
        if not re.fullmatch(r"[-=─━\s]+", line)
        and not line.startswith(("at node:", "npm error A complete log"))
    ]
    if meaningful:
        return "\n".join(meaningful[-12:])[-4000:]
    if command.error:
        return command.error
    return f"{command.phase} {command.layer or ''} exited with {command.returncode}.".strip()


def _relevant_targets(
    resolved: dict[str, Any],
    binding_by_id: dict[str, dict[str, Any]],
    target_modules: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    writable_ids = {str(value) for value in resolved.get("writable", []) if str(value)}
    read_only_ids = {str(value) for value in resolved.get("read_only", []) if str(value)}
    relevant = set(target_modules)
    if relevant:
        writable_ids &= relevant
        read_only_ids &= relevant
    return (
        [_target_card(binding_by_id[value]) for value in sorted(writable_ids) if value in binding_by_id],
        [_target_card(binding_by_id[value]) for value in sorted(read_only_ids) if value in binding_by_id],
    )


def _diagnostic_module_ids(
    output: str,
    modules_by_file: dict[str, list[str]],
) -> set[str]:
    """Return every bound module whose source path appears in diagnostics.

    Compiler output is not consistent about path prefixes: depending on the
    command it may print ``backend/src/x.ts``, ``src/x.ts``, or a Windows
    absolute path.  Matching against known registry paths (and their
    backend/frontend-stripped suffixes) is both safer and more complete than
    trying to parse a single stack-frame grammar.
    """

    normalized_output = str(output or "").replace("\\", "/").lower()
    matched: set[str] = set()
    for raw_file, module_ids in modules_by_file.items():
        relative = _normalize_relative(raw_file).replace("\\", "/")
        if not relative:
            continue
        candidates = {relative.lower()}
        if relative.startswith(("backend/", "frontend/")):
            candidates.add(relative.lower().split("/", 1)[1])
        if any(
            re.search(
                re.escape(candidate) + r"(?:[:(,\s]|$)",
                normalized_output,
            )
            for candidate in candidates
        ):
            matched.update(str(value) for value in module_ids if str(value))
    return matched


def _module_ids_for_files(
    files: list[str],
    modules_by_file: dict[str, list[str]],
) -> set[str]:
    """Map exact changed files to modules without requirement-wide expansion."""

    wanted = {_normalize_relative(value).lower() for value in files if str(value)}
    matched: set[str] = set()
    for raw_file, module_ids in modules_by_file.items():
        normalized = _normalize_relative(raw_file).lower()
        candidates = {normalized}
        if normalized.startswith(("backend/", "frontend/")):
            candidates.add(normalized.split("/", 1)[1])
        if wanted.intersection(candidates):
            matched.update(str(value) for value in module_ids if str(value))
    return matched


def _target_card(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: binding.get(key)
        for key in (
            "module_id",
            "kind",
            "file",
            "symbol",
        )
    }


def _fingerprint(
    *,
    failure_class: str,
    phase: str,
    layer: str | None,
    test_ids: list[str],
    message: str,
    target_modules: set[str],
) -> str:
    normalized_message = re.sub(r"\b\d+(?:\.\d+)?\s*m?s\b", "<duration>", message)
    normalized_message = re.sub(
        r"\b[0-9a-f]{16,}\b",
        "<dynamic>",
        normalized_message,
        flags=re.IGNORECASE,
    )
    normalized_message = re.sub(r":\d+:\d+\b", ":<line>:<column>", normalized_message)
    payload = {
        "failure_class": failure_class,
        "phase": phase,
        "layer": layer,
        "test_ids": sorted(test_ids),
        "message": normalized_message,
        "target_modules": sorted(target_modules),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _clean_output(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value).replace("\r\n", "\n")


def _normalize_relative(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip().strip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
