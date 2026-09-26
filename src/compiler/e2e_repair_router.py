from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .failure_analysis import TestFailureReport


FRONTEND_KINDS = {"PAGE", "COMPONENT", "LAYOUT", "STORE"}
BACKEND_KINDS = {"DB", "FUNC", "API"}


@dataclass(frozen=True, slots=True)
class E2ERepairRoute:
    """Deterministic routing decision for one E2E failure cluster."""

    route: str
    backend_targets: tuple[str, ...] = ()
    frontend_targets: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "backend_targets": list(self.backend_targets),
            "frontend_targets": list(self.frontend_targets),
            "reasons": list(self.reasons),
        }


def classify_e2e_route(
    reports: Iterable[TestFailureReport],
    *,
    code_binding_registry: dict[str, Any],
) -> E2ERepairRoute:
    """Route E2E repairs from observed evidence, not model judgement.

    A report can contain target module ids, stack frames, and diagnostic text.
    Binding kinds are authoritative when available; textual evidence is only
    used to detect server-side failures that were not localized to a module.
    """

    rows = list(reports)
    bindings = {
        str(row.get("module_id", "")): row
        for row in code_binding_registry.get("code_bindings", [])
        if isinstance(row, dict) and str(row.get("module_id", ""))
    }
    backend: set[str] = set()
    frontend: set[str] = set()
    reasons: list[str] = []
    server_evidence = False
    browser_evidence = False

    for report in rows:
        candidates = set(str(value) for value in report.target_modules if str(value))
        candidates.update(
            str(value.get("module_id", ""))
            for value in report.writable_targets
            if isinstance(value, dict) and str(value.get("module_id", ""))
        )
        for module_id in candidates:
            kind = str(bindings.get(module_id, {}).get("kind", "")).upper()
            module_suffix = module_id.split("::")[-1]
            if kind in BACKEND_KINDS or module_suffix.startswith(("DB.", "FUNC.", "API.")):
                backend.add(module_id)
            elif kind in FRONTEND_KINDS or module_suffix.startswith(
                ("PAGE.", "COMPONENT.", "LAYOUT.", "STORE.")
            ):
                frontend.add(module_id)

        text = "\n".join(
            str(value)
            for value in (report.message, report.diagnostic_output)
            if value
        ).lower()
        if any(token in text for token in ("http 5", "status 5", "server error", "exception", "stack trace", "api response")):
            server_evidence = True
        if any(token in text for token in ("locator", "visible", "page.", "browser", "console", "navigation", "route")):
            browser_evidence = True
        for event in report.e2e_progress:
            kind = event.get("event")
            if kind == "api_response" and str(event.get("status", "")).startswith("5"):
                server_evidence = True
            if kind in {"page_error", "console_error"}:
                browser_evidence = True
        if report.stack_frames:
            browser_evidence = browser_evidence or any(
                str(frame.file).replace("\\", "/").startswith("frontend/")
                for frame in report.stack_frames
            )

    if (backend and frontend) or (server_evidence and frontend):
        reasons.append("server-side evidence and frontend targets both occur in this failure")
        route = "CROSS_LAYER"
    elif backend or server_evidence:
        if server_evidence and not backend:
            reasons.append("server-side HTTP/runtime evidence is present without a localized target")
        route = "BACKEND_ONLY"
    elif frontend or browser_evidence:
        if browser_evidence and not frontend:
            reasons.append("browser/locator evidence is present without a localized target")
        route = "FRONTEND_ONLY"
    else:
        route = "AMBIGUOUS"
        reasons.append("E2E evidence is insufficient to distinguish client and server cause")

    return E2ERepairRoute(
        route=route,
        backend_targets=tuple(sorted(backend)),
        frontend_targets=tuple(sorted(frontend)),
        reasons=tuple(dict.fromkeys(reasons)),
    )
