from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ContextConfig:
    workspace_dir: str = "."
    app_type: str = "web"
    web_port: int = 3301
    android_package: str = "com.example.template"


class NodeContextCache:
    FILE_DEPENDENT_LAYERS = {"source_code", "test_code"}

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}

    def get_or_compute(self, node_id: str, layer_name: str, compute_fn: Callable[[], str]) -> str:
        key = (node_id, layer_name)
        if key not in self._cache:
            self._cache[key] = compute_fn()
        return self._cache[key]

    def invalidate(self, node_id: str, layer_name: str | None = None) -> None:
        if layer_name:
            self._cache.pop((node_id, layer_name), None)
            return
        self._cache = {key: value for key, value in self._cache.items() if key[0] != node_id}

    def invalidate_file_layers(self, node_id: str) -> None:
        stale_keys = [
            key
            for key in self._cache
            if key[0] == node_id
            and (
                key[1] in self.FILE_DEPENDENT_LAYERS
                or key[1].startswith("test_code::")
                or key[1].startswith("project_structure::")
            )
        ]
        for key in stale_keys:
            self._cache.pop(key, None)

    def invalidate_db_layers(self, node_id: str) -> None:
        self.invalidate(node_id, "node_session")
        self.invalidate(node_id, "existing_interfaces")
        self.invalidate(node_id, "recent_failure_summary")

    def clear(self) -> None:
        self._cache.clear()


class ContextPipeline:
    """Assemble static and dynamic context blocks for ARC stage agents."""

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()
        self.runtime: Any | None = None
        self.max_related_interfaces = 30
        self.cache = NodeContextCache()

    def configure(
        self,
        *,
        workspace_dir: str | None = None,
        app_type: str | None = None,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> None:
        if workspace_dir is not None:
            self.config.workspace_dir = workspace_dir
        if app_type is not None:
            self.config.app_type = app_type
        if web_port is not None:
            self.config.web_port = int(web_port)
        if android_package is not None:
            self.config.android_package = android_package
        self.cache.clear()

    def set_runtime(self, runtime: Any | None) -> None:
        self.runtime = runtime
        self.cache.clear()

    def _store(self):
        return getattr(self.runtime, "traceability", None)

    @staticmethod
    def _get_app_type_handler_class(app_type: str):
        from app_type_handler import get_app_type_handler_class

        return get_app_type_handler_class(app_type)

    @staticmethod
    def _compact_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _truncate_text(text: Any, limit: int) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[:limit].rstrip() + "... [truncated]"

    @staticmethod
    def _limit_string_list(values: list[Any], limit: int = 6, item_limit: int = 160) -> list[str]:
        items: list[str] = []
        for raw in values[:limit]:
            text = str(raw or "").strip()
            if not text:
                continue
            if len(text) > item_limit:
                text = text[:item_limit].rstrip() + "... [truncated]"
            items.append(text)
        return items

    @staticmethod
    def _normalize_step_keyword(step: dict[str, Any]) -> str:
        return str(step.get("keyword") or step.get("type") or "").strip().upper()

    def _build_scenario_digest(self, scenario: dict[str, Any]) -> dict[str, Any]:
        flow: list[str] = []
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            keyword = self._normalize_step_keyword(step)
            content = str(step.get("content", "") or "").strip()
            if keyword and content:
                flow.append(f"{keyword}: {content}")
            elif content:
                flow.append(content)
        return {
            "scenario_id": scenario.get("scenario_id") or scenario.get("id", ""),
            "name": scenario.get("name", ""),
            "flow": flow,
        }

    @staticmethod
    def _build_visual_digest(visual_reference: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "image_path": item.get("image_path", ""),
                "analysis": str(item.get("analysis", "") or ""),
            }
            for item in visual_reference
            if isinstance(item, dict)
        ]

    def _build_acceptance_gate(self, node_id: str, req_data: dict[str, Any]) -> str:
        scenarios = req_data.get("scenarios") or []
        visual_reference = req_data.get("visual_reference") or []
        gate = {
            "req_id": req_data.get("req_id", node_id),
            "primary_outcomes": [
                "Implement the current node's owned behavior, not just a renderable shell.",
                "Use real owned runtime wiring for fetched or persisted data when this node owns that chain.",
                "If runtime data is not owned here, render explicit loading, empty, or error states instead of fake records.",
            ],
            "scenario_targets": [
                str(item.get("name", "")).strip()
                for item in scenarios
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ],
            "visual_rule": (
                "Use visual reference for layout and style only; do not copy screenshot business data."
                if visual_reference
                else "No visual-reference-specific acceptance rule."
            ),
            "forbidden_shortcuts": [
                "hardcoded sample rows",
                "fallback arrays that bypass the owned path",
                "fake success messages detached from real writes",
                "placeholder-only panels presented as complete features",
            ],
        }
        return "<acceptance_gate>\n" + self._compact_json(gate) + "\n</acceptance_gate>"

    def _build_requirement_focus(self, node_id: str, req_data: dict[str, Any]) -> str:
        scenarios = req_data.get("scenarios") or []
        visual_reference = req_data.get("visual_reference") or []
        focus = {
            "req_id": req_data.get("req_id", node_id),
            "name": req_data.get("name", ""),
            "description": req_data.get("description", ""),
            "dependencies": req_data.get("dependencies", []),
            "children_ids": req_data.get("children_ids", []),
            "scenario_count": len(scenarios),
            "visual_reference_count": len(visual_reference),
        }
        parts = ["<requirement_focus>", self._compact_json(focus)]
        if scenarios:
            parts.extend(
                [
                    "<scenarios>",
                    self._compact_json([self._build_scenario_digest(item) for item in scenarios if isinstance(item, dict)]),
                    "</scenarios>",
                ]
            )
        if visual_reference:
            parts.extend(
                [
                    "<visual_reference>",
                    self._compact_json(self._build_visual_digest(visual_reference)),
                    "</visual_reference>",
                ]
            )
        parts.append("</requirement_focus>")
        return "\n".join(parts)

    def _with_scenarios_from_store(self, node_id: str, req_data: dict[str, Any]) -> dict[str, Any]:
        scenarios = [item for item in req_data.get("scenarios") or [] if isinstance(item, dict)]
        store = self._store()
        if store is None or not hasattr(store, "list_scenarios"):
            return {**req_data, "scenarios": scenarios}

        seen = {
            str(item.get("scenario_id") or item.get("id") or item.get("name") or "").strip()
            for item in scenarios
        }
        for scenario in store.list_scenarios(req_id=node_id):
            key = str(scenario.get("scenario_id") or scenario.get("id") or scenario.get("name") or "").strip()
            if key and key not in seen:
                scenarios.append(scenario)
                seen.add(key)
        return {**req_data, "scenarios": scenarios}

    def _get_tech_stack_context(self) -> str:
        app_type = (self.config.app_type or "web").strip().lower()
        handler_class = self._get_app_type_handler_class(app_type)
        content = handler_class.build_stack_block(
            web_port=self.config.web_port,
            android_package=self.config.android_package,
        )
        return f"<tech_stack_context>\n{content}\n</tech_stack_context>"

    def _get_project_structure(self, agent_type: str = "") -> str:
        app_type = (self.config.app_type or "web").strip().lower()
        handler_class = self._get_app_type_handler_class(app_type)
        lines = handler_class.project_structure_lines(
            web_port=self.config.web_port,
            android_package=self.config.android_package,
        )
        return "<project_structure>\n" + "\n".join(lines) + "\n</project_structure>"

    def _get_test_harness_context(self) -> str:
        app_type = (self.config.app_type or "web").strip().lower()
        handler_class = self._get_app_type_handler_class(app_type)
        lines = handler_class.test_harness_lines(
            web_port=self.config.web_port,
            android_package=self.config.android_package,
        )
        return "<test_harness>\n" + "\n".join(f"- {line}" for line in lines) + "\n</test_harness>"

    @staticmethod
    def _dedupe_records_by_file_path_keep_latest(
        records: list[dict[str, Any]],
        file_path_key: str = "file_path",
    ) -> list[dict[str, Any]]:
        deduped_reversed: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for record in reversed(records):
            normalized_path = str(record.get(file_path_key, "") or "").strip().replace("\\", "/")
            if not normalized_path:
                deduped_reversed.append(record)
                continue
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            deduped_reversed.append(record)
        deduped_reversed.reverse()
        return deduped_reversed

    def _decode_interface_content(self, iface: dict[str, Any]) -> dict[str, Any]:
        content = iface.get("content", {})
        if isinstance(content, dict):
            return content
        try:
            parsed = json.loads(str(content or "{}"))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _get_source_file_cards(self, node_id: str, max_files: int | None = None) -> str:
        store = self._store()
        if store is None:
            return ""
        interfaces = store.list_interfaces(req_id=node_id)
        if not interfaces:
            return ""
        interfaces = self._dedupe_records_by_file_path_keep_latest(interfaces)
        selected = interfaces if max_files is None else interfaces[:max_files]
        cards: list[dict[str, Any]] = []
        for iface in selected:
            file_path = str(iface.get("file_path", "") or "").strip()
            if not file_path:
                continue
            content = self._decode_interface_content(iface)
            cards.append(
                {
                    "file_path": file_path,
                    "first_line": str(iface.get("first_line", "") or "").strip(),
                    "interface_id": str(iface.get("interface_id", "") or "").strip(),
                    "type": str(iface.get("type", "") or "").strip(),
                    "implemented": bool(iface.get("implemented")),
                    "responsibility": self._truncate_text(content.get("responsibility", ""), 180),
                    "specification": self._truncate_text(content.get("specification", ""), 220),
                    "test_focus": self._limit_string_list(content.get("test_focus") or [], limit=4, item_limit=120),
                    "callers": self._limit_string_list(content.get("callers") or [], limit=3, item_limit=80),
                    "callees": self._limit_string_list(content.get("callees") or [], limit=3, item_limit=80),
                    "why_relevant": "Current-node owned interface file.",
                }
            )
        if not cards:
            return ""
        return "<source_file_cards>\n" + self._compact_json(cards) + "\n</source_file_cards>"

    def _get_test_file_cards(self, node_id: str, target_test_files: list[str] | None = None) -> str:
        store = self._store()
        if store is None:
            return ""
        tests = [item for item in store.list_tests(req_id=node_id) if item is not None]
        if not tests:
            return ""
        tests = self._dedupe_records_by_file_path_keep_latest(tests)
        normalized_targets = {
            str(path or "").strip().replace("\\", "/")
            for path in (target_test_files or [])
            if str(path or "").strip()
        }
        if normalized_targets:
            tests = [
                test
                for test in tests
                if str(test.get("file_path", "")).strip().replace("\\", "/") in normalized_targets
            ]
        cards = [
            {
                "file_path": str(test.get("file_path", "") or "").strip(),
                "first_line": str(test.get("first_line", "") or "").strip(),
                "type": str(test.get("type", "") or "").strip(),
                "interface_ids": self._limit_string_list(test.get("interface_ids") or [], limit=6, item_limit=80),
                "passed": test.get("passed"),
                "why_relevant": "Current-node generated test artifact.",
            }
            for test in tests
            if str(test.get("file_path", "") or "").strip()
        ]
        if not cards:
            return ""
        return "<test_file_cards>\n" + self._compact_json(cards) + "\n</test_file_cards>"

    def _node_session_path(self, node_id: str) -> Path:
        return Path(self.config.workspace_dir) / ".arc" / "node_sessions" / f"{node_id}.json"

    def _load_node_session(self, node_id: str) -> dict[str, Any]:
        path = self._node_session_path(node_id)
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _get_node_session_layers(self, node_id: str) -> str:
        session = self._load_node_session(node_id)
        interfaces = session.get("interfaces")
        if not interfaces:
            return ""
        return "<interfaces>\n" + self._compact_json(interfaces) + "\n</interfaces>"

    def _get_existing_interface_cards(self, node_id: str) -> str:
        store = self._store()
        if store is None:
            return ""
        current_req = store.get_requirement(node_id) or {}
        parent_id = str(current_req.get("parent_id") or "").strip()
        dependencies = {
            str(item or "").strip()
            for item in (current_req.get("dependencies") or [])
            if str(item or "").strip()
        }
        cards: list[dict[str, Any]] = []
        for iface in store.list_interfaces():
            req_ids = [
                str(item or "").strip()
                for item in (iface.get("req_ids") or [])
                if str(item or "").strip()
            ]
            if not req_ids:
                continue
            relation = ""
            if node_id in req_ids:
                relation = "current"
            elif parent_id and parent_id in req_ids:
                relation = "parent"
            elif dependencies.intersection(req_ids):
                relation = "dependency"
            elif len(cards) >= self.max_related_interfaces:
                continue
            else:
                relation = "existing"
            content = self._decode_interface_content(iface)
            cards.append(
                {
                    "interface_id": str(iface.get("interface_id", "") or "").strip(),
                    "req_ids": req_ids,
                    "relation": relation,
                    "type": str(iface.get("type", "") or "").strip(),
                    "file_path": str(iface.get("file_path", "") or "").strip(),
                    "implemented": bool(iface.get("implemented")),
                    "responsibility": self._truncate_text(content.get("responsibility", ""), 180),
                    "specification": self._truncate_text(content.get("specification", ""), 220),
                    "callers": self._limit_string_list(content.get("callers") or [], limit=3, item_limit=80),
                    "callees": self._limit_string_list(content.get("callees") or [], limit=3, item_limit=80),
                }
            )
            if len(cards) >= self.max_related_interfaces:
                break
        if not cards:
            return ""
        return "<existing_interfaces>\n" + self._compact_json(cards) + "\n</existing_interfaces>"

    def get_interface_contract_context(self, node_id: str) -> str:
        session = self._load_node_session(node_id)
        interfaces = session.get("interfaces") or []
        if not interfaces:
            return ""
        return (
            "<current_interface_contract>\n"
            + self._compact_json(interfaces)
            + "\n</current_interface_contract>"
        )

    def _get_recent_failure_summary(self, node_id: str) -> str:
        session = self._load_node_session(node_id)
        summary = str(session.get("recent_failure_summary", "") or "").strip()
        if not summary:
            handoff = session.get("tdd_handoff") or {}
            summary = str(handoff.get("last_failed_output_summary", "") or "").strip()
        if not summary:
            return ""
        return "<recent_failure_summary>\n" + summary + "\n</recent_failure_summary>"

    def build_agent_context(
        self,
        node_id: str,
        agent_type: str,
        preloaded_source: str | None = None,
        target_test_files: list[str] | None = None,
    ) -> str:
        store = self._store()
        if store is None:
            return "<error>Context runtime is not configured.</error>"
        req_data = store.get_requirement(node_id)
        if not req_data:
            return f"<error>Requirement node {node_id} not found in database.</error>"
        req_data = self._with_scenarios_from_store(node_id, req_data)

        context_parts = [
            self._build_requirement_focus(node_id, req_data),
            self._build_acceptance_gate(node_id, req_data),
            self.cache.get_or_compute(node_id, "tech_stack_context", self._get_tech_stack_context),
        ]
        project_structure = self.cache.get_or_compute(
            node_id,
            f"project_structure::{agent_type}",
            lambda: self._get_project_structure(agent_type),
        )
        if project_structure:
            context_parts.append(project_structure)
        if agent_type == "TestGenerator":
            context_parts.append(self.cache.get_or_compute(node_id, "test_harness_context", self._get_test_harness_context))

        node_session_layers = self.cache.get_or_compute(
            node_id,
            "node_session",
            lambda: self._get_node_session_layers(node_id),
        )
        if node_session_layers:
            context_parts.append(node_session_layers)

        existing_interfaces = self.cache.get_or_compute(
            node_id,
            "existing_interfaces",
            lambda: self._get_existing_interface_cards(node_id),
        )
        if existing_interfaces:
            context_parts.append(existing_interfaces)

        if agent_type in {"InterfaceDesigner", "TestGenerator", "TestDrivenDeveloper", "TestFailureVerifier"}:
            if preloaded_source:
                context_parts.append(preloaded_source)
            else:
                source_cards = self.cache.get_or_compute(
                    node_id,
                    "source_code",
                    lambda: self._get_source_file_cards(node_id),
                )
                if source_cards:
                    context_parts.append(source_cards)

        if agent_type in {"TestDrivenDeveloper", "TestFailureVerifier"}:
            if target_test_files:
                normalized_targets = sorted(
                    {
                        str(path or "").strip().replace("\\", "/")
                        for path in target_test_files
                        if str(path or "").strip()
                    }
                )
                cache_key = f"test_code::{json.dumps(normalized_targets, ensure_ascii=False)}"
                test_cards = self.cache.get_or_compute(
                    node_id,
                    cache_key,
                    lambda: self._get_test_file_cards(node_id, normalized_targets),
                )
            else:
                test_cards = self.cache.get_or_compute(
                    node_id,
                    "test_code",
                    lambda: self._get_test_file_cards(node_id),
                )
            if test_cards:
                context_parts.append(test_cards)

        recent_failure_summary = self.cache.get_or_compute(
            node_id,
            "recent_failure_summary",
            lambda: self._get_recent_failure_summary(node_id),
        )
        if recent_failure_summary:
            context_parts.append(recent_failure_summary)

        return "\n\n".join(part for part in context_parts if part)

    def get_static_context(self, node_id: str, agent_type: str = "") -> str:
        parts = [
            self.cache.get_or_compute(node_id, "tech_stack_context", self._get_tech_stack_context),
            self.cache.get_or_compute(
                node_id,
                f"project_structure::{agent_type}",
                lambda: self._get_project_structure(agent_type),
            ),
        ]
        if agent_type == "TestGenerator":
            parts.append(self.cache.get_or_compute(node_id, "test_harness_context", self._get_test_harness_context))
        return "\n\n".join(part for part in parts if part)

    def build_agent_context_split(
        self,
        *,
        node_id: str,
        agent_type: str,
        preloaded_source: str | None = None,
        target_test_files: list[str] | None = None,
    ) -> tuple[str, str]:
        full_context = self.build_agent_context(
            node_id=node_id,
            agent_type=agent_type,
            preloaded_source=preloaded_source,
            target_test_files=target_test_files,
        )
        static = self.get_static_context(node_id, agent_type)
        dynamic = full_context
        for part in [item.strip() for item in static.split("\n\n") if item.strip()]:
            dynamic = dynamic.replace(part, "", 1)
        dynamic = "\n\n".join(item.strip() for item in dynamic.split("\n\n") if item.strip())
        return static, dynamic

    def build_incremental_context(self, node_id: str, modified_files: list[str] | None = None) -> str:
        if not modified_files:
            return ""
        workspace = Path(self.config.workspace_dir)
        lines: list[str] = []
        total = 0
        for file_path in modified_files:
            relative = str(file_path or "").strip().replace("\\", "/")
            if not relative:
                continue
            absolute = workspace / relative
            if not absolute.exists() or not absolute.is_file():
                continue
            try:
                content = absolute.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(content) > 3000:
                content = content[:3000] + "\n// ... [truncated]"
            lines.append(f"// === {relative} (modified) ===\n{content}")
            total += len(content)
            if total > 15000:
                break
        if not lines:
            return ""
        return "<modified_files>\n" + "\n\n".join(lines) + "\n</modified_files>"

    def prewarm(self, node_id: str) -> None:
        self.cache.get_or_compute(node_id, "tech_stack_context", self._get_tech_stack_context)
        self.cache.get_or_compute(node_id, "project_structure::", lambda: self._get_project_structure(""))


context_pipeline = ContextPipeline()


def set_context_runtime(runtime: Any | None) -> None:
    context_pipeline.set_runtime(runtime)


def set_context_config(
    *,
    workspace_dir: str | None = None,
    app_type: str | None = None,
    web_port: int | None = None,
    android_package: str | None = None,
) -> None:
    context_pipeline.configure(
        workspace_dir=workspace_dir,
        app_type=app_type,
        web_port=web_port,
        android_package=android_package,
    )
