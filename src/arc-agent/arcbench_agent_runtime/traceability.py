from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import RuntimePaths
from .events import EventClient
from .jsonio import write_json_atomic


def _json_list(value: Any) -> str:
    return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    return None


@dataclass(frozen=True)
class RequirementRecord:
    req_id: str
    name: str = ""
    description: str = ""
    visual_reference: list[str] | None = None
    scenarios: list[dict[str, Any]] | None = None
    parent_id: str | None = None
    children_ids: list[str] | None = None
    dependencies: list[str] | None = None


@dataclass(frozen=True)
class ScenarioRecord:
    scenario_id: str
    req_id: str
    name: str
    steps: list[dict[str, str]]


@dataclass(frozen=True)
class InterfaceRecord:
    interface_id: str
    req_ids: list[str]
    type: str
    content: str
    file_path: str | None = None
    first_line: str | None = None
    implemented: bool = False
    callers: list[str] | None = None
    callees: list[str] | None = None


@dataclass(frozen=True)
class TestRecord:
    test_id: str
    req_id: str
    type: str
    file_path: str | None = None
    first_line: str | None = None
    interface_ids: list[str] | None = None
    passed: bool | None = None


class TraceabilityStore:
    def __init__(self, paths: RuntimePaths, events: EventClient) -> None:
        self.paths = paths
        self.events = events

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _build_snapshot_payload(self, connection: sqlite3.Connection) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requirements": [],
            "scenarios": [],
            "interfaces": [],
            "tests": [],
            "call_edges": [],
            "node_states": [],
            "node_contracts": [],
        }
        if self._table_exists(connection, "requirements"):
            rows = connection.execute("SELECT * FROM requirements ORDER BY req_id").fetchall()
            payload["requirements"] = [self._row_to_requirement(row) for row in rows]
        if self._table_exists(connection, "scenarios"):
            rows = connection.execute("SELECT * FROM scenarios ORDER BY scenario_id").fetchall()
            payload["scenarios"] = [
                {
                    "scenario_id": str(row["scenario_id"] or "").strip(),
                    "name": str(row["name"] or "").strip(),
                    "req_id": str(row["req_id"] or "").strip(),
                    "steps": _parse_json_list(row["steps"]),
                }
                for row in rows
            ]
        if self._table_exists(connection, "interfaces"):
            rows = connection.execute("SELECT * FROM interfaces ORDER BY interface_id").fetchall()
            payload["interfaces"] = [
                {
                    "interface_id": str(row["interface_id"] or "").strip(),
                    "req_ids": _parse_json_list(row["req_ids"]),
                    "type": str(row["type"] or "").strip(),
                    "content": str(row["content"] or "").strip(),
                    "file_path": str(row["file_path"] or "").strip() or None,
                    "first_line": str(row["first_line"] or "").strip() or None,
                    "implemented": bool(row["implemented"]),
                    "callers": _parse_json_list(row["callers"]),
                    "callees": _parse_json_list(row["callees"]),
                }
                for row in rows
            ]
        if self._table_exists(connection, "tests"):
            rows = connection.execute("SELECT * FROM tests ORDER BY test_id").fetchall()
            payload["tests"] = [
                {
                    "test_id": str(row["test_id"] or "").strip(),
                    "req_id": str(row["req_id"] or "").strip(),
                    "interface_ids": _parse_json_list(row["interface_ids"]),
                    "type": str(row["type"] or "").strip(),
                    "file_path": str(row["file_path"] or "").strip() or None,
                    "passed": _parse_bool(row["passed"]),
                    "first_line": str(row["first_line"] or "").strip() or None,
                }
                for row in rows
            ]
        if self._table_exists(connection, "call_edges"):
            rows = connection.execute(
                "SELECT * FROM call_edges ORDER BY source_req_id, target_req_id, from_interface_id, to_interface_id"
            ).fetchall()
            payload["call_edges"] = [dict(row) for row in rows]
        if self._table_exists(connection, "node_states"):
            rows = connection.execute("SELECT * FROM node_states ORDER BY req_id").fetchall()
            payload["node_states"] = [dict(row) for row in rows]
        if self._table_exists(connection, "node_contracts"):
            rows = connection.execute("SELECT * FROM node_contracts ORDER BY req_id").fetchall()
            payload["node_contracts"] = []
            for row in rows:
                content = str(row["content"] or "").strip()
                try:
                    parsed_content = json.loads(content) if content else {}
                except json.JSONDecodeError:
                    parsed_content = {}
                payload["node_contracts"].append(
                    {
                        "req_id": str(row["req_id"] or "").strip(),
                        "content": parsed_content,
                        "updated_at": str(row["updated_at"] or "").strip() or None,
                    }
                )
        return payload

    def _write_snapshot(self, connection: sqlite3.Connection) -> None:
        write_json_atomic(
            self.paths.traceability_snapshot_path,
            self._build_snapshot_payload(connection),
        )

    def connect(self) -> sqlite3.Connection:
        self.paths.traceability_db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.paths.traceability_db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connection(self) -> sqlite3.Connection:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def init_db(self, *, reset: bool = False) -> None:
        with self.connection() as connection:
            cursor = connection.cursor()
            if reset:
                cursor.execute("DROP TABLE IF EXISTS node_contracts")
                cursor.execute("DROP TABLE IF EXISTS node_states")
                cursor.execute("DROP TABLE IF EXISTS call_edges")
                cursor.execute("DROP TABLE IF EXISTS tests")
                cursor.execute("DROP TABLE IF EXISTS interfaces")
                cursor.execute("DROP TABLE IF EXISTS scenarios")
                cursor.execute("DROP TABLE IF EXISTS requirements")

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS requirements (
                    req_id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    visual_reference TEXT,
                    scenarios TEXT,
                    parent_id TEXT,
                    children_ids TEXT,
                    dependencies TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS scenarios (
                    scenario_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    req_id TEXT NOT NULL,
                    steps TEXT NOT NULL,
                    FOREIGN KEY(req_id) REFERENCES requirements(req_id)
                        ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS interfaces (
                    interface_id TEXT PRIMARY KEY,
                    req_ids TEXT,
                    type TEXT,
                    content TEXT,
                    file_path TEXT,
                    first_line TEXT,
                    implemented INTEGER,
                    callers TEXT,
                    callees TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tests (
                    test_id TEXT PRIMARY KEY,
                    req_id TEXT,
                    interface_ids TEXT,
                    type TEXT,
                    file_path TEXT,
                    passed INTEGER,
                    first_line TEXT,
                    FOREIGN KEY(req_id) REFERENCES requirements(req_id)
                        ON DELETE CASCADE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS call_edges (
                    source_req_id TEXT,
                    target_req_id TEXT,
                    from_interface_id TEXT,
                    to_interface_id TEXT,
                    edge_type TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source_req_id, target_req_id, from_interface_id, to_interface_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS node_states (
                    req_id TEXT PRIMARY KEY,
                    state TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(req_id) REFERENCES requirements(req_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS node_contracts (
                    req_id TEXT PRIMARY KEY,
                    content TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(req_id) REFERENCES requirements(req_id)
                )
                """
            )
            connection.commit()
            self._write_snapshot(connection)

    def _row_to_requirement(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "req_id": str(row["req_id"] or "").strip(),
            "name": str(row["name"] or "").strip(),
            "description": str(row["description"] or "").strip(),
            "visual_reference": _parse_json_list(row["visual_reference"]),
            "scenarios": _parse_json_list(row["scenarios"]),
            "parent_id": str(row["parent_id"] or "").strip() or None,
            "children_ids": _parse_json_list(row["children_ids"]),
            "dependencies": _parse_json_list(row["dependencies"]),
        }

    def get_requirement(self, req_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM requirements WHERE req_id = ?", (req_id,)).fetchone()
        return self._row_to_requirement(row) if row else None

    def list_requirements(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM requirements ORDER BY req_id").fetchall()
        return [self._row_to_requirement(row) for row in rows]

    def upsert_requirement(
        self,
        *,
        req_id: str,
        name: str = "",
        description: str = "",
        visual_reference: list[str] | None = None,
        scenarios: list[dict[str, Any]] | None = None,
        parent_id: str | None = None,
        children_ids: list[str] | None = None,
        dependencies: list[str] | None = None,
    ) -> None:
        normalized_req_id = str(req_id or "").strip()
        if not normalized_req_id:
            raise ValueError("req_id is required")
        normalized_scenarios = scenarios or []
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO requirements (
                    req_id, name, description, visual_reference, scenarios, parent_id, children_ids, dependencies
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_req_id,
                    str(name or "").strip(),
                    str(description or "").strip(),
                    _json_list(visual_reference or []),
                    _json_list(normalized_scenarios),
                    str(parent_id or "").strip() or None,
                    _json_list(children_ids or []),
                    _json_list(dependencies or []),
                    ),
                )
            connection.execute("DELETE FROM scenarios WHERE req_id = ?", (normalized_req_id,))
            for scenario in normalized_scenarios:
                scenario_id = str(scenario.get("id") or scenario.get("scenario_id") or "").strip()
                if not scenario_id:
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO scenarios (scenario_id, name, req_id, steps)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        scenario_id,
                        str(scenario.get("name") or "").strip() or scenario_id,
                        normalized_req_id,
                        _json_list(scenario.get("steps") or []),
                    ),
                )
            connection.commit()
            self._write_snapshot(connection)
        self.events.notify_traceability_changed("requirements_updated")

    def update_requirement_fields(self, req_id: str, **fields: Any) -> None:
        current = self.get_requirement(req_id)
        if current is None:
            raise ValueError(f"Requirement not found: {req_id}")
        merged = {**current, **fields}
        self.upsert_requirement(
            req_id=req_id,
            name=str(merged.get("name") or "").strip(),
            description=str(merged.get("description") or "").strip(),
            visual_reference=merged.get("visual_reference") or [],
            scenarios=merged.get("scenarios") or [],
            parent_id=merged.get("parent_id"),
            children_ids=merged.get("children_ids") or [],
            dependencies=merged.get("dependencies") or [],
        )

    def delete_requirement(self, req_id: str) -> None:
        test_ids = [item["test_id"] for item in self.list_tests(req_id=req_id)]
        with self.connection() as connection:
            connection.execute("DELETE FROM requirements WHERE req_id = ?", (req_id,))
            connection.execute("DELETE FROM call_edges WHERE source_req_id = ? OR target_req_id = ?", (req_id, req_id))
            connection.execute("DELETE FROM node_states WHERE req_id = ?", (req_id,))
            connection.execute("DELETE FROM node_contracts WHERE req_id = ?", (req_id,))
            connection.commit()
            self._write_snapshot(connection)
        self.events.clear_demo_test_statuses(test_ids)
        self.events.set_demo_requirement_status(req_id, None)
        self.events.notify_traceability_changed("requirement_deleted")

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scenarios WHERE scenario_id = ?", (scenario_id,)).fetchone()
        if not row:
            return None
        return {
            "scenario_id": str(row["scenario_id"] or "").strip(),
            "name": str(row["name"] or "").strip(),
            "req_id": str(row["req_id"] or "").strip(),
            "steps": _parse_json_list(row["steps"]),
        }

    def list_scenarios(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if req_id:
                rows = connection.execute(
                    "SELECT * FROM scenarios WHERE req_id = ? ORDER BY scenario_id",
                    (req_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM scenarios ORDER BY scenario_id").fetchall()
        return [
            {
                "scenario_id": str(row["scenario_id"] or "").strip(),
                "name": str(row["name"] or "").strip(),
                "req_id": str(row["req_id"] or "").strip(),
                "steps": _parse_json_list(row["steps"]),
            }
            for row in rows
        ]

    def upsert_scenario(self, *, scenario_id: str, req_id: str, name: str, steps: list[dict[str, str]]) -> None:
        normalized_scenario_id = str(scenario_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_scenario_id or not normalized_req_id:
            raise ValueError("scenario_id and req_id are required")
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO scenarios (scenario_id, name, req_id, steps)
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized_scenario_id,
                    str(name or "").strip(),
                    normalized_req_id,
                    _json_list(steps),
                ),
            )
            requirement = connection.execute(
                "SELECT scenarios FROM requirements WHERE req_id = ?",
                (normalized_req_id,),
            ).fetchone()
            if requirement:
                scenarios = _parse_json_list(requirement["scenarios"])
                scenario_payload = {
                    "id": normalized_scenario_id,
                    "name": str(name or "").strip(),
                    "steps": steps or [],
                }
                updated = [item for item in scenarios if str(item.get("id") or item.get("scenario_id") or "").strip() != normalized_scenario_id]
                updated.append(scenario_payload)
                connection.execute(
                    "UPDATE requirements SET scenarios = ? WHERE req_id = ?",
                    (_json_list(updated), normalized_req_id),
                )
            connection.commit()
            self._write_snapshot(connection)
        self.events.notify_traceability_changed("scenarios_updated")

    def delete_scenario(self, scenario_id: str) -> None:
        scenario = self.get_scenario(scenario_id)
        if scenario is None:
            return
        with self.connection() as connection:
            connection.execute("DELETE FROM scenarios WHERE scenario_id = ?", (scenario_id,))
            requirement = connection.execute(
                "SELECT scenarios FROM requirements WHERE req_id = ?",
                (scenario["req_id"],),
            ).fetchone()
            if requirement:
                scenarios = [
                    item
                    for item in _parse_json_list(requirement["scenarios"])
                    if str(item.get("id") or item.get("scenario_id") or "").strip() != scenario_id
                ]
                connection.execute(
                    "UPDATE requirements SET scenarios = ? WHERE req_id = ?",
                    (_json_list(scenarios), scenario["req_id"]),
                )
            connection.commit()
            self._write_snapshot(connection)
        self.events.notify_traceability_changed("scenario_deleted")

    def get_interface(self, interface_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM interfaces WHERE interface_id = ?", (interface_id,)).fetchone()
        if not row:
            return None
        return {
            "interface_id": str(row["interface_id"] or "").strip(),
            "req_ids": _parse_json_list(row["req_ids"]),
            "type": str(row["type"] or "").strip(),
            "content": str(row["content"] or "").strip(),
            "file_path": str(row["file_path"] or "").strip() or None,
            "first_line": str(row["first_line"] or "").strip() or None,
            "implemented": bool(row["implemented"]),
            "callers": _parse_json_list(row["callers"]),
            "callees": _parse_json_list(row["callees"]),
        }

    def list_interfaces(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM interfaces ORDER BY interface_id").fetchall()
        interfaces = [self.get_interface(str(row["interface_id"])) for row in rows]
        normalized = [item for item in interfaces if item is not None]
        if not req_id:
            return normalized
        return [item for item in normalized if req_id in item["req_ids"]]

    def upsert_interface(
        self,
        *,
        interface_id: str,
        req_ids: list[str],
        type: str,
        content: str,
        file_path: str | None = None,
        first_line: str | None = None,
        implemented: bool = False,
        callers: list[str] | None = None,
        callees: list[str] | None = None,
        emit_event: bool = True,
    ) -> None:
        normalized_interface_id = str(interface_id or "").strip()
        if not normalized_interface_id:
            raise ValueError("interface_id is required")
        payload = {
            "type": "interface_upsert",
            "interface_id": normalized_interface_id,
            "req_ids": [str(item).strip() for item in req_ids if str(item).strip()],
            "interface_type": str(type or "").strip(),
            "content": str(content or "").strip(),
            "file_path": str(file_path or "").strip() or None,
            "first_line": str(first_line or "").strip() or None,
            "implemented": bool(implemented),
            "callers": [str(item).strip() for item in (callers or []) if str(item).strip()],
            "callees": [str(item).strip() for item in (callees or []) if str(item).strip()],
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO interfaces (
                    interface_id, req_ids, type, content, file_path, first_line, implemented, callers, callees
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["interface_id"],
                    _json_list(payload["req_ids"]),
                    payload["interface_type"],
                    payload["content"],
                    payload["file_path"],
                    payload["first_line"],
                    1 if payload["implemented"] else 0,
                    _json_list(payload["callers"]),
                    _json_list(payload["callees"]),
                ),
            )
            connection.commit()
            self._write_snapshot(connection)
        if emit_event:
            self.events._emit_traceability_event(payload)

    def update_interface_fields(self, interface_id: str, **fields: Any) -> None:
        current = self.get_interface(interface_id)
        if current is None:
            raise ValueError(f"Interface not found: {interface_id}")
        merged = {**current, **fields}
        self.upsert_interface(
            interface_id=interface_id,
            req_ids=merged.get("req_ids") or [],
            type=str(merged.get("type") or "").strip(),
            content=str(merged.get("content") or "").strip(),
            file_path=merged.get("file_path"),
            first_line=merged.get("first_line"),
            implemented=bool(merged.get("implemented")),
            callers=merged.get("callers") or [],
            callees=merged.get("callees") or [],
        )

    def set_interface_implemented(self, interface_id: str, implemented: bool, message: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE interfaces SET implemented = ? WHERE interface_id = ?",
                (1 if implemented else 0, interface_id),
            )
            connection.commit()
            self._write_snapshot(connection)
        self.events._emit_traceability_event(
            {
                "type": "interface_status",
                "interface_id": str(interface_id or "").strip(),
                "implemented": bool(implemented),
                "message": message,
            }
        )

    def delete_interface(self, interface_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM interfaces WHERE interface_id = ?", (interface_id,))
            connection.commit()
            self._write_snapshot(connection)
        self.events.notify_traceability_changed("interface_deleted")

    def get_test(self, test_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tests WHERE test_id = ?", (test_id,)).fetchone()
        if not row:
            return None
        return {
            "test_id": str(row["test_id"] or "").strip(),
            "req_id": str(row["req_id"] or "").strip(),
            "interface_ids": _parse_json_list(row["interface_ids"]),
            "type": str(row["type"] or "").strip(),
            "file_path": str(row["file_path"] or "").strip() or None,
            "passed": _parse_bool(row["passed"]),
            "first_line": str(row["first_line"] or "").strip() or None,
        }

    def list_tests(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if req_id:
                rows = connection.execute("SELECT * FROM tests WHERE req_id = ? ORDER BY test_id", (req_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM tests ORDER BY test_id").fetchall()
        return [self.get_test(str(row["test_id"])) for row in rows if row is not None]

    def upsert_test(
        self,
        *,
        test_id: str,
        req_id: str,
        type: str,
        file_path: str | None = None,
        first_line: str | None = None,
        interface_ids: list[str] | None = None,
        passed: bool | None = None,
        scenario_id: str | None = None,
        emit_event: bool = True,
    ) -> None:
        normalized_test_id = str(test_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_test_id or not normalized_req_id:
            raise ValueError("test_id and req_id are required")
        existing = self.get_test(normalized_test_id)
        if existing and existing["req_id"] != normalized_req_id:
            raise ValueError(
                f"Test id collision detected for `{normalized_test_id}`: "
                f"existing req_id=`{existing['req_id']}`, new req_id=`{normalized_req_id}`."
            )
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO tests (
                    test_id, req_id, interface_ids, type, file_path, passed, first_line
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_test_id,
                    normalized_req_id,
                    _json_list(interface_ids or []),
                    str(type or "").strip(),
                    str(file_path or "").strip() or None,
                    None if passed is None else (1 if passed else 0),
                    str(first_line or "").strip() or None,
                ),
            )
            connection.commit()
            self._write_snapshot(connection)
        self.events.set_demo_test_status(
            normalized_test_id,
            "passed" if passed is True else "failed" if passed is False else None,
        )
        if emit_event:
            self.events._emit_traceability_event(
                {
                    "type": "test_upsert",
                    "test_id": normalized_test_id,
                    "req_id": normalized_req_id,
                    "scenario_id": str(scenario_id or "").strip() or None,
                    "test_type": str(type or "").strip(),
                    "file_path": str(file_path or "").strip() or None,
                    "first_line": str(first_line or "").strip() or None,
                    "interface_ids": [str(item).strip() for item in (interface_ids or []) if str(item).strip()],
                }
            )

    def update_test_fields(self, test_id: str, **fields: Any) -> None:
        current = self.get_test(test_id)
        if current is None:
            raise ValueError(f"Test not found: {test_id}")
        merged = {**current, **fields}
        self.upsert_test(
            test_id=test_id,
            req_id=str(merged.get("req_id") or "").strip(),
            type=str(merged.get("type") or "").strip(),
            file_path=merged.get("file_path"),
            first_line=merged.get("first_line"),
            interface_ids=merged.get("interface_ids") or [],
            passed=merged.get("passed"),
        )

    def set_test_pass_status(self, test_id: str, passed: bool | None) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE tests SET passed = ? WHERE test_id = ?",
                (None if passed is None else (1 if passed else 0), test_id),
            )
            connection.commit()
            self._write_snapshot(connection)
        self.events.set_demo_test_status(
            test_id,
            "passed" if passed is True else "failed" if passed is False else None,
        )
        self.events.notify_traceability_changed("test_status_updated")

    def set_test_pass_statuses(self, status_by_test_id: dict[str, bool | None]) -> None:
        if not status_by_test_id:
            return
        with self.connection() as connection:
            for test_id, passed in status_by_test_id.items():
                connection.execute(
                    "UPDATE tests SET passed = ? WHERE test_id = ?",
                    (None if passed is None else (1 if passed else 0), test_id),
                )
            connection.commit()
            self._write_snapshot(connection)
        self.events.set_demo_test_statuses(
            {
                test_id: "passed" if passed is True else "failed" if passed is False else None
                for test_id, passed in status_by_test_id.items()
            }
        )
        self.events.notify_traceability_changed("test_statuses_updated")

    def reset_test_pass_statuses_for_requirement(self, req_id: str) -> None:
        tests = self.list_tests(req_id=req_id)
        test_ids = [item["test_id"] for item in tests]
        with self.connection() as connection:
            connection.execute("UPDATE tests SET passed = NULL WHERE req_id = ?", (req_id,))
            connection.commit()
            self._write_snapshot(connection)
        self.events.clear_demo_test_statuses(test_ids)
        self.events.set_demo_requirement_status(req_id, None)
        self.events.notify_traceability_changed("test_statuses_reset")

    def delete_test(self, test_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM tests WHERE test_id = ?", (test_id,))
            connection.commit()
            self._write_snapshot(connection)
        self.events.set_demo_test_status(test_id, None)
        self.events.notify_traceability_changed("test_deleted")

    def insert_call_edge(
        self,
        *,
        source_req_id: str,
        target_req_id: str,
        from_interface_id: str,
        to_interface_id: str,
        edge_type: str = "parent_child",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO call_edges (
                    source_req_id, target_req_id, from_interface_id, to_interface_id, edge_type
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (source_req_id, target_req_id, from_interface_id, to_interface_id, edge_type),
            )
            connection.commit()
            self._write_snapshot(connection)
        self.events.notify_traceability_changed("call_edges_updated")

    def list_call_edges(self, *, req_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if req_id:
                rows = connection.execute(
                    """
                    SELECT * FROM call_edges
                    WHERE source_req_id = ? OR target_req_id = ?
                    ORDER BY source_req_id, target_req_id, from_interface_id, to_interface_id
                    """,
                    (req_id, req_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM call_edges ORDER BY source_req_id, target_req_id, from_interface_id, to_interface_id"
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_call_edge(
        self,
        *,
        source_req_id: str,
        target_req_id: str,
        from_interface_id: str,
        to_interface_id: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                DELETE FROM call_edges
                WHERE source_req_id = ? AND target_req_id = ? AND from_interface_id = ? AND to_interface_id = ?
                """,
                (source_req_id, target_req_id, from_interface_id, to_interface_id),
            )
            connection.commit()
            self._write_snapshot(connection)
        self.events.notify_traceability_changed("call_edge_deleted")

    def upsert_node_state(self, req_id: str, state: str) -> None:
        normalized_req_id = str(req_id or "").strip()
        normalized_state = str(state or "").strip()
        if not normalized_req_id:
            raise ValueError("req_id is required")
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO node_states (req_id, state, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(req_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (normalized_req_id, normalized_state),
            )
            connection.commit()
            self._write_snapshot(connection)
        upper_state = normalized_state.upper()
        if upper_state in {"PASSED", "CONVERGED", "CONVERGED_WITH_FAILED_CHILDREN"}:
            self.events.set_demo_requirement_status(normalized_req_id, "passed")
        elif upper_state == "FAILED":
            self.events.set_demo_requirement_status(normalized_req_id, "failed")
        else:
            self.events.set_demo_requirement_status(normalized_req_id, None)
        self.events.notify_traceability_changed("node_state_updated")

    def get_node_state(self, req_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM node_states WHERE req_id = ?", (req_id,)).fetchone()
        return dict(row) if row else None

    def list_node_states(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM node_states ORDER BY req_id").fetchall()
        return [dict(row) for row in rows]

    def delete_node_state(self, req_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM node_states WHERE req_id = ?", (req_id,))
            connection.commit()
            self._write_snapshot(connection)
        self.events.set_demo_requirement_status(req_id, None)
        self.events.notify_traceability_changed("node_state_deleted")

    def upsert_node_contract(self, req_id: str, content: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO node_contracts (req_id, content, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(req_id) DO UPDATE SET
                    content=excluded.content,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (req_id, json.dumps(content, ensure_ascii=False)),
            )
            connection.commit()
            self._write_snapshot(connection)

    def get_node_contract(self, req_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM node_contracts WHERE req_id = ?", (req_id,)).fetchone()
        if not row:
            return None
        payload = dict(row)
        try:
            payload["content"] = json.loads(payload["content"]) if payload.get("content") else {}
        except json.JSONDecodeError:
            payload["content"] = {}
        return payload

    def delete_node_contract(self, req_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM node_contracts WHERE req_id = ?", (req_id,))
            connection.commit()
            self._write_snapshot(connection)

    def clear_node_design_artifacts(self, req_id: str) -> None:
        interfaces = self.list_interfaces(req_id=req_id)
        with self.connection() as connection:
            for interface in interfaces:
                remaining_req_ids = [value for value in interface["req_ids"] if value != req_id]
                if remaining_req_ids:
                    connection.execute(
                        "UPDATE interfaces SET req_ids = ? WHERE interface_id = ?",
                        (_json_list(remaining_req_ids), interface["interface_id"]),
                    )
                else:
                    connection.execute("DELETE FROM interfaces WHERE interface_id = ?", (interface["interface_id"],))
            test_ids = [item["test_id"] for item in self.list_tests(req_id=req_id)]
            connection.execute("DELETE FROM tests WHERE req_id = ?", (req_id,))
            connection.execute("DELETE FROM call_edges WHERE source_req_id = ? OR target_req_id = ?", (req_id, req_id))
            connection.commit()
            self._write_snapshot(connection)
        self.events.clear_demo_test_statuses(test_ids)
        self.events.set_demo_requirement_status(req_id, None)
        self.events.notify_traceability_changed("design_artifacts_cleared")
