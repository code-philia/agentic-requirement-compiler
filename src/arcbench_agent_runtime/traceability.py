from __future__ import annotations

from pathlib import Path
from typing import Any

from .context import RuntimePaths
from .events import EventClient
from .jsonio import read_json, write_json_atomic


TABLE_NAMES = ("requirements",)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


class TraceabilityStore:
    """Persist requirement-owned traceability in one canonical table."""

    def __init__(self, paths: RuntimePaths, events: EventClient) -> None:
        self.paths = paths
        self.events = events

    @property
    def root(self) -> Path:
        return self.paths.traceability_dir

    def table_path(self, table_name: str) -> Path:
        if table_name not in TABLE_NAMES:
            raise ValueError(f"Unknown traceability table: {table_name}")
        return self.root / f"{table_name}.json"

    def _read_requirements(self) -> dict[str, Any]:
        payload = read_json(self.table_path("requirements"), {})
        return payload if isinstance(payload, dict) else {}

    def _write_requirements(self, rows: dict[str, Any]) -> None:
        write_json_atomic(self.table_path("requirements"), dict(sorted(rows.items())))

    def init_store(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.table_path("requirements")
        if not path.exists():
            write_json_atomic(path, {})
        self.events.notify_traceability_changed("traceability_store_initialized")

    def merge_database_schema_links(
        self,
        links: dict[str, dict[str, list[str]]],
    ) -> None:
        requirements = self._read_requirements()
        for requirement_id, entity_fields in sorted(links.items()):
            row = requirements.get(requirement_id)
            if not isinstance(row, dict):
                row = {}
            row["database"] = {
                str(entity): sorted({str(field) for field in fields})
                for entity, fields in sorted(entity_fields.items())
                if isinstance(fields, list)
            }
            requirements[requirement_id] = row
        self._write_requirements(requirements)
        self.events.notify_traceability_changed("database_schema_links_merged")

    def read_database_schema_links_from_requirements(self) -> dict[str, dict[str, list[str]]]:
        result: dict[str, dict[str, list[str]]] = {}
        for requirement_id, row in self._read_requirements().items():
            if not isinstance(row, dict) or not isinstance(row.get("database"), dict):
                continue
            result[str(requirement_id)] = {
                str(entity): [str(field) for field in fields if str(field).strip()]
                for entity, fields in row["database"].items()
                if isinstance(fields, list)
            }
        return result

    def merge_design_links(self, links: dict[str, dict[str, list[str]]]) -> None:
        requirements = self._read_requirements()
        for requirement_id, design_links in sorted(links.items()):
            row = requirements.get(requirement_id)
            if not isinstance(row, dict):
                row = {}
            row["design"] = {
                key: sorted({str(value) for value in values if str(value).strip()})
                for key, values in sorted(design_links.items())
                if key in {"api_ids", "module_ids"} and isinstance(values, list)
            }
            requirements[requirement_id] = row
        self._write_requirements(requirements)
        self.events.notify_traceability_changed("design_links_merged")

    def merge_frontend_design_links(self, links: dict[str, dict[str, Any]]) -> None:
        requirements = self._read_requirements()
        allowed_keys = {
            "layout_ids",
            "page_ids",
            "component_ids",
            "store_ids",
            "visual_reference_ids",
        }
        for requirement_id, frontend_links in sorted(links.items()):
            row = requirements.get(requirement_id)
            if not isinstance(row, dict):
                row = {}
            frontend_design = {
                key: sorted({str(value) for value in values if str(value).strip()})
                for key, values in sorted(frontend_links.items())
                if key in allowed_keys and isinstance(values, list)
            }
            frontend_design["ui_scope"] = str(
                frontend_links.get("ui_scope", "NO_UI")
            ).strip().upper()
            row["frontend_design"] = frontend_design
            requirements[requirement_id] = row
        self._write_requirements(requirements)
        self.events.notify_traceability_changed("frontend_design_links_merged")

    def merge_test_links(self, test_manifest: dict[str, Any]) -> None:
        requirements = self._read_requirements()
        for item in _as_list(test_manifest.get("files")):
            if not isinstance(item, dict):
                continue
            requirement_id = str(item.get("requirement_id", ""))
            if not requirement_id:
                continue
            row = requirements.get(requirement_id)
            if not isinstance(row, dict):
                row = {}
            row.setdefault("tests", [])
            path = str(item.get("test_file", ""))
            if path and path not in row["tests"]:
                row["tests"].append(path)
            requirements[requirement_id] = row
        self._write_requirements(requirements)
        self.events.notify_traceability_changed("test_links_merged")
    def read_frontend_design_links_from_requirements(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for requirement_id, row in sorted(self._read_requirements().items()):
            if not isinstance(row, dict):
                continue
            frontend = row.get("frontend_design")
            if not isinstance(frontend, dict):
                continue
            result.append({
                "requirement_id": str(requirement_id),
                "ui_scope": str(frontend.get("ui_scope", "NO_UI")).strip().upper(),
                "screen_ids": sorted(set(_as_str_list(frontend.get("page_ids")))),
                "shared_state_ids": sorted(set(_as_str_list(frontend.get("store_ids")))),
                "visual_reference_ids": sorted(set(_as_str_list(frontend.get("visual_reference_ids")))),
            })
        return result

    def store_requirement_ids(self, requirement_ids: list[str]) -> None:
        """Register IDs; the full requirement tree lives in preprocessing IR."""

        self._write_requirements({requirement_id: {} for requirement_id in sorted(set(requirement_ids))})
        self.events.notify_traceability_changed("requirements_registered")
