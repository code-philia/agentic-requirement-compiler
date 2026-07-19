from __future__ import annotations

from pathlib import Path


def normalize_windows_extended_prefix_text(value: str | Path | None) -> str:
    r"""Strip Windows extended-length path prefixes from a path string.

    The deepagents filesystem backend can surface `\\?\`-prefixed paths on
    Windows when `Path.resolve()` is involved. Those paths refer to the same
    location as their normal counterparts, but string-based containment checks
    treat them as different roots. Normalizing them keeps path comparisons
    stable.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    if normalized.startswith("//?/UNC/"):
        return "//" + normalized[len("//?/UNC/") :]
    if normalized.startswith("//?/"):
        return normalized[len("//?/") :]
    return normalized


def normalize_windows_extended_prefix_path(value: str | Path) -> Path:
    return Path(normalize_windows_extended_prefix_text(value))
