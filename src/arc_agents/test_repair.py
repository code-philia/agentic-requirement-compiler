"""Independent read-only test analysis and cross-layer repair agents."""
from __future__ import annotations

import hashlib
import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import AgentInvocationResult, BaseStructuredAgent, JsonModel
from .contracts import ProposedEdit, ProposedPatch
from .implementation import IMPLEMENTATION_OUTPUT_SCHEMA


ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "reason", "suspected_files"],
    "properties": {
        "verdict": {"type": "string", "enum": ["IMPLEMENTATION", "TEST", "MIXED", "PRECONDITION", "DESIGN", "UNKNOWN"]},
        "reason": {"type": "string"},
        "suspected_files": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "evidence", "correction"],
            "properties": {key: {"type": "string"} for key in ("file", "evidence", "correction")},
        }},
    },
}

ANALYSIS_INSTRUCTIONS = """Analyze this test execution without editing anything.
Use requirement, test_files, test_result, source_files and the chronological history.
There is no editable scope at this stage. Requirements determine expected behavior;
source signatures and routes determine public contracts, not expected test results.
Trace the failure across frontend, API, business logic and persistence as needed.
Compare prior analyses, applied/rejected patches and subsequent results: do not repeat
a disproven diagnosis or treat a rejected patch as installed code.
Return a verdict, reason and suspected_files identifying the minimal files that
actually require changes. Files must appear in source_files or test_files and be
marked repairable in file_catalog. For every file, quote an exact current source
excerpt as evidence and describe the correction. Include the required caller/callee
files for a coordinated change, not every dependency. For TEST/MIXED, show why the
test contradicts a requirement or public contract; an unimplemented behavior or
failing assertion is not itself a test defect. Never weaken assertions to accept bugs.
Use PRECONDITION when execution is blocked by unavailable fixture data, an external
service, environment setup, or an unimplemented prerequisite outside the behavior
under test. Identify the missing prerequisite and evidence in reason; do not propose
business-code workarounds or repeat prior ineffective setup repairs. Database reset
automatically restores the designed fixture baseline before each test. A defect in
the behavior under test is IMPLEMENTATION, not PRECONDITION. Incorrect test setup
that contradicts the supplied contracts is TEST, not PRECONDITION.
Use DESIGN for missing design contracts and UNKNOWN for insufficient evidence;
PRECONDITION, DESIGN and UNKNOWN stop repair and
these verdicts must have no suspected_files. Output only the analysis JSON.
"""

REPAIR_INSTRUCTIONS = """Repair this requirement according to test_analysis.
This is a cross-layer repair task: fix diagnosed frontend, backend and test defects
together, regardless of whether the failing layer is UNIT, INTEGRATION or E2E.
Use the requirement, current test_files, current editable_files/related_files,
test_result and chronological history. History records old file states and patches;
only the current editable_files values are valid search text. Preserve unrelated
behavior and public contracts. Do not repeat a rejected or ineffective repair.
Only keys of editable_files may be edited. This set is rebuilt from each analysis;
previously editable files do not retain permission. Test files are editable only
when the analysis identifies a concrete test defect. Never remove scenarios, skip
tests, weaken valid assertions or mock away the behavior under test.
Complete coordinated changes in one patch, with exact unique search fragments.
Preserve UI styling and accessibility when changing frontend logic. Visual analyses
in the requirement guide layout; an E2E pass alone is not visual completion.
Return only {"edits":[{"file":"...","search":"...","replacement":"..."}]}.
Preserve compiler-injected imports from ../support/e2e.js and ../support/runtime.js.
Never remove their .js extensions or replace the E2E test fixture with Playwright's base test.
"""

DIRECT_REPAIR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "reason", "edits"],
    "properties": {
        "verdict": {"type": "string", "enum": ["CURRENT_IMPLEMENTATION", "NEEDS_DIAGNOSIS"]},
        "reason": {"type": "string"},
        "edits": {**IMPLEMENTATION_OUTPUT_SCHEMA["properties"]["edits"], "minItems": 0},
    },
}

DIRECT_REPAIR_INSTRUCTIONS = """Inspect the failing test, requirement, current source and recent patches.
If the failure is clearly in the current requirement's editable_files, return
CURRENT_IMPLEMENTATION with a complete exact-search patch in edits. Only those
files may be changed; tests and related_files are read-only. Preserve valid
assertions, public contracts and unrelated behavior. If a test, dependency,
design contract or environment might be at fault, return NEEDS_DIAGNOSIS with
edits:[]; a separate analysis will determine the broader repair scope.
Do not guess a current-file repair when evidence is insufficient. Return JSON only.
"""


class TestFailureAnalysisAgent:
    def __init__(self, model: JsonModel, *, trace=None, model_log=None) -> None:
        self._agent = BaseStructuredAgent(
            model, schema_name="arc_test_failure_analysis", instructions=ANALYSIS_INSTRUCTIONS,
            output_schema=ANALYSIS_SCHEMA, retries=2, trace=trace, model_log=model_log,
            agent_name="TestFailureAnalysisAgent",
        )

    def analyze(self, context: dict[str, Any]) -> AgentInvocationResult:
        return self._agent.invoke(context, validate=lambda output: self._validate(output, context))

    @staticmethod
    def _validate(output: dict[str, Any], context: dict[str, Any]) -> list[str]:
        if not isinstance(output, dict) or set(output) != {"verdict", "reason", "suspected_files"}:
            return ["Return verdict, reason and suspected_files."]
        verdict = output["verdict"]
        if not isinstance(verdict, str) or verdict not in ANALYSIS_SCHEMA["properties"]["verdict"]["enum"]:
            return ["Unknown analysis verdict."]
        if not isinstance(output["reason"], str) or not output["reason"].strip():
            return ["Give a reason supported by the supplied evidence."]
        defects = output["suspected_files"]
        if not isinstance(defects, list):
            return ["suspected_files must be an array."]
        if bool(defects) != (verdict in {"IMPLEMENTATION", "TEST", "MIXED"}):
            return ["Repairable verdicts require suspected files; PRECONDITION/DESIGN/UNKNOWN require an empty list."]
        sources = {**context["source_files"], **context["test_files"]}
        allowed = {row["file"] for row in context["file_catalog"] if row["repairable"]}
        seen: set[str] = set()
        errors = []
        for defect in defects:
            if not isinstance(defect, dict) or set(defect) != {"file", "evidence", "correction"} or not all(
                isinstance(value, str) and value.strip() for value in defect.values()
            ):
                errors.append("Each suspect needs file, exact evidence and correction.")
                continue
            path = defect["file"]
            if path not in allowed or path not in sources or path in seen:
                errors.append(f"Not a unique repairable source: {path}")
            elif defect["evidence"] not in sources[path]:
                errors.append(f"Evidence does not match current source: {path}")
            seen.add(path)
        tests = seen & context["test_files"].keys()
        application = seen - context["test_files"].keys()
        if verdict == "TEST" and application or verdict == "IMPLEMENTATION" and tests:
            errors.append("Verdict does not match the kinds of suspected files.")
        if verdict == "MIXED" and (not tests or not application):
            errors.append("MIXED must identify both implementation and test defects.")
        return errors


@dataclass
class RepairResult:
    status: str
    patch: ProposedPatch | None = None
    errors: list[str] = field(default_factory=list)
    proposal: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "PATCH_PROPOSED" and self.patch is not None and not self.errors


class TestRepairAgent:
    def __init__(self, model: JsonModel, *, trace=None, model_log=None) -> None:
        # One proposal per repair iteration. A rejected format/build becomes
        # history for the next iteration, so the five-call budget is explicit.
        self._agent = BaseStructuredAgent(
            model, schema_name="arc_test_repair_patch", instructions=REPAIR_INSTRUCTIONS,
            output_schema=IMPLEMENTATION_OUTPUT_SCHEMA, retries=0, trace=trace,
            model_log=model_log, agent_name="TestRepairAgent",
        )
        self._direct_agent = BaseStructuredAgent(
            model, schema_name="arc_direct_test_repair", instructions=DIRECT_REPAIR_INSTRUCTIONS,
            output_schema=DIRECT_REPAIR_SCHEMA, retries=0, trace=trace,
            model_log=model_log, agent_name="DirectTestRepairAgent",
        )

    def repair_direct(self, context: dict[str, Any], *,
                      accept_patch: Callable[[ProposedPatch], list[str]]) -> RepairResult:
        hashes = {path: hashlib.sha256(source.encode("utf-8")).hexdigest()
                  for path, source in context["editable_files"].items()}
        proposed: ProposedPatch | None = None
        proposal: Any = None

        def validate(output: dict[str, Any]) -> list[str]:
            nonlocal proposed, proposal
            proposal = copy.deepcopy(output)
            if not isinstance(output, dict) or set(output) != {"verdict", "reason", "edits"}:
                return ["Return verdict, reason and edits."]
            if not isinstance(output["reason"], str) or not output["reason"].strip():
                return ["Explain the verdict using the supplied failure evidence."]
            edits = output["edits"]
            if output["verdict"] == "NEEDS_DIAGNOSIS":
                return [] if edits == [] else ["NEEDS_DIAGNOSIS requires edits:[]."]
            if output["verdict"] != "CURRENT_IMPLEMENTATION" or not isinstance(edits, list) or not edits:
                return ["CURRENT_IMPLEMENTATION requires a non-empty edits array."]
            for row in edits:
                if not isinstance(row, dict) or set(row) != {"file", "search", "replacement"} or not all(
                    isinstance(value, str) for value in row.values()
                ) or row["file"] not in hashes or not row["search"]:
                    return ["Edit only current editable_files with non-empty exact search text."]
            proposed = ProposedPatch(requirement_id=context["requirement_id"], edits=tuple(
                ProposedEdit(file=row["file"], expected_sha256=hashes[row["file"]],
                             search=row["search"], replacement=row["replacement"])
                for row in edits
            ))
            return accept_patch(proposed)

        result = self._direct_agent.invoke(context, validate=validate)
        if result.ok and result.output is not None and result.output["verdict"] == "NEEDS_DIAGNOSIS":
            return RepairResult("DEFERRED", proposal=result.output)
        return RepairResult(
            "PATCH_PROPOSED" if result.ok else "REPAIR_REJECTED",
            proposed if result.ok else None, result.errors, proposal,
        )

    def repair(self, context: dict[str, Any], *,
               accept_patch: Callable[[ProposedPatch], list[str]]) -> RepairResult:
        hashes = {path: hashlib.sha256(source.encode("utf-8")).hexdigest()
                  for path, source in context["editable_files"].items()}
        proposed: ProposedPatch | None = None
        proposal: Any = None

        def validate(output: dict[str, Any]) -> list[str]:
            nonlocal proposed, proposal
            proposal = copy.deepcopy(output)
            if not isinstance(output, dict) or set(output) != {"edits"} or not isinstance(output["edits"], list) or not output["edits"]:
                return ["Return a non-empty edits array."]
            errors = []
            for row in output["edits"]:
                if not isinstance(row, dict) or set(row) != {"file", "search", "replacement"}:
                    errors.append("Each edit needs file/search/replacement.")
                elif not all(isinstance(value, str) for value in row.values()) or row["file"] not in hashes or not row["search"]:
                    errors.append("Use only current editable_files and non-empty exact search text.")
            if errors:
                return errors
            proposed = ProposedPatch(requirement_id=context["requirement_id"], edits=tuple(
                ProposedEdit(file=row["file"], expected_sha256=hashes[row["file"]],
                             search=row["search"], replacement=row["replacement"])
                for row in output["edits"]
            ))
            return accept_patch(proposed)

        result = self._agent.invoke(context, validate=validate)
        return RepairResult("PATCH_PROPOSED" if result.ok else "REPAIR_REJECTED", proposed, result.errors, proposal)
