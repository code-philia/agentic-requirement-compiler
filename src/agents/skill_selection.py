"""Select the small set of stage skills that ARC requires for one invocation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SKILLS_SOURCE = "/skills/"
_AUTH_TERMS = (
    "login",
    "log in",
    "sign in",
    "register",
    "registration",
    "logout",
    "log out",
    "session",
    "authenticated",
    "authentication",
    "authorization",
    "current user",
    "account state",
)


def interface_design_skills(requirement_data: dict[str, Any]) -> list[str]:
    """Return the relevant design skill and, for leaf auth work, its cross-cutting skill."""

    names = [
        "non-leaf-ui-only-design"
        if requirement_data.get("children_ids")
        else "leaf-full-design"
    ]
    if not requirement_data.get("children_ids") and has_auth_context(requirement_data):
        names.append("auth-session-consistency")
    return available_skill_names(names)


def test_generation_skills(requirement_data: dict[str, Any]) -> list[str]:
    """Return test selection guidance plus auth guidance only when the node needs it."""

    names = ["leaf-test-layer-selection"]
    if has_auth_context(requirement_data):
        names.append("auth-session-consistency")
    return available_skill_names(names)


def implementation_skills(*, interface_contract: str, previous_failure_summary: str) -> list[str]:
    """Expose repair guidance only after a real failure, plus relevant auth guidance."""

    names: list[str] = []
    if previous_failure_summary.strip():
        names.append("tdd-test-failure-repair")
    if has_auth_context(interface_contract):
        names.append("auth-session-consistency")
    return available_skill_names(names)


def has_auth_context(value: object) -> bool:
    """Detect explicit authentication/session ownership without guessing from shell-only text."""

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    normalized = text.casefold()
    return any(term in normalized for term in _AUTH_TERMS)


def available_skill_names(names: list[str]) -> list[str]:
    """Keep declared skills in order and omit missing instruction files."""

    root = Path(__file__).resolve().parents[1] / "skills"
    return [name for name in dict.fromkeys(names) if (root / name / "SKILL.md").is_file()]
