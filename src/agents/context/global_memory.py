from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
from typing import Any


class GlobalProjectMemory:
    """Persist actionable cross-agent handoffs without duplicating node context."""

    MAX_FINGERPRINT_CHARS = 1400
    MAX_OBSERVATION_CHARS = 700
    MAX_ACTION_CHARS = 500
    MAX_ATTEMPTS = 3
    _NON_ACTIONABLE_FAILURES = (
        "budget exhausted",
        "the active tdd layer is",
        "tool budget exhausted",
        "no current-node tests are registered",
        "received files from multiple test types",
    )

    def __init__(self, workspace_dir: str, store: Any) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.store = store

    @property
    def path(self) -> Path:
        return self.workspace_dir / ".arc" / "global_memory.md"

    @property
    def handoff_path(self) -> Path:
        return self.workspace_dir / ".arc" / "global_handoffs.json"

    @property
    def success_path(self) -> Path:
        return self.workspace_dir / ".arc" / "global_successes.json"

    def refresh(self, node_id: str = "", *, write_audit: bool = True) -> str:
        """Refresh the audit file and return only memory relevant to ``node_id``."""
        if not global_memory_enabled():
            return ""
        requirements = self._requirements()
        handoffs = self._load_handoffs()
        successes = self._load_successes()
        if node_id and node_id not in handoffs:
            migrated = self._session_handoff(node_id)
            if migrated:
                handoffs[node_id] = migrated
                if write_audit:
                    self._write_handoffs(handoffs)
        if write_audit:
            self._write_audit_file(
                {
                    "requirement_graph": requirements,
                    "architecture": self._architecture_digest(node_id, requirements),
                    "verified_patterns": successes,
                    "actionable_handoffs": handoffs,
                }
            )
        visible = {
            "project_relations": self._project_relations(node_id, requirements),
            "architecture": self._architecture_digest(node_id, requirements),
            "verified_patterns": self._relevant_successes(node_id, successes),
            "work_packets": self._relevant_handoffs(node_id, handoffs),
        }
        body = json.dumps(visible, ensure_ascii=False, separators=(",", ":"), default=str)
        return "<global_project_memory>\n" + body + "\n</global_project_memory>"

    def record_test_result(
        self,
        *,
        requirement: str,
        test_type: str,
        test_files: list[str],
        output: str,
    ) -> None:
        """Store a useful failure, or clear it when the same layer passes."""
        if not global_memory_enabled():
            return
        requirement = str(requirement or "").strip()
        test_type = str(test_type or "").strip()
        if not requirement or not test_type:
            return
        handoffs = self._load_handoffs()
        successes = self._load_successes()
        current = handoffs.get(requirement)
        exit_code = self._exit_code(output)
        if exit_code == 0:
            layers = dict(successes.get(requirement) or {})
            layers[test_type] = self._success_pattern(
                requirement=requirement,
                test_type=test_type,
                test_files=test_files,
            )
            successes[requirement] = layers
            self._write_successes(successes)
            if isinstance(current, dict) and current.get("test_type") == test_type:
                handoffs.pop(requirement, None)
                self._write_handoffs(handoffs)
            return
        if exit_code is None or self._is_non_actionable(output):
            return

        layers = dict(successes.get(requirement) or {})
        if test_type in layers:
            layers.pop(test_type, None)
            if layers:
                successes[requirement] = layers
            else:
                successes.pop(requirement, None)
            self._write_successes(successes)

        observation = self._failure_fingerprint(output)
        if not observation:
            return
        current = self._normalize_handoff(handoffs.get(requirement, {}))
        signature = self._failure_signature(observation)
        previous_signature = str(current.get("failure_signature", ""))
        current.update(
            {
                "test_type": test_type,
                "test_files": sorted({str(path).strip() for path in test_files if str(path).strip()}),
                "observation": observation[-self.MAX_OBSERVATION_CHARS :],
                "failure_signature": signature,
                "repeat_count": int(current.get("repeat_count", 0)) + 1 if signature == previous_signature else 1,
                "last_outcome": "failed",
            }
        )
        observed_file = self._next_file(observation)
        if observed_file and not current.get("next_file"):
            current["next_file"] = observed_file
            current["next_file_source"] = "test_output"
        handoffs[requirement] = current
        self._write_handoffs(handoffs)

    def record_handoff(
        self,
        *,
        requirement: str,
        test_type: str,
        test_files: list[str],
        fingerprint: str,
        protected_files: list[str] | None = None,
    ) -> None:
        """Persist a known failure summary, including one restored from a checkpoint."""
        if not global_memory_enabled():
            return
        requirement = str(requirement or "").strip()
        fingerprint = str(fingerprint or "").strip()
        if not requirement or not fingerprint:
            return
        handoffs = self._load_handoffs()
        current = self._normalize_handoff(handoffs.get(requirement, {}))
        parsed = self._parse_handoff_text(fingerprint)
        attempts = list(current.get("attempted_changes") or [])
        attempted = parsed.get("attempted_change", "")
        if attempted and attempted not in attempts:
            attempts.append(attempted)
        do_not_repeat = list(current.get("do_not_repeat") or [])
        rejected = parsed.get("do_not_repeat", "")
        if rejected and rejected not in do_not_repeat:
            do_not_repeat.append(rejected)
        candidate_observation = parsed.get("observation") or fingerprint
        candidate_next_file = parsed.get("next_file") or self._next_file(fingerprint)
        candidate_next_action = parsed.get("next_action") or ""
        current_priority = self._diagnostic_priority(
            observation=str(current.get("observation") or ""),
            next_file=str(current.get("next_file") or ""),
            next_action=str(current.get("next_action") or ""),
        )
        candidate_priority = self._diagnostic_priority(
            observation=candidate_observation,
            next_file=candidate_next_file,
            next_action=candidate_next_action,
        )
        same_target = bool(candidate_next_file) and candidate_next_file == current.get("next_file")
        resolves_current = self._resolves_current_hypothesis(parsed, current)
        preserve_current_diagnosis = (
            bool(current)
            and current_priority > candidate_priority
            and not same_target
            and not resolves_current
        )
        observation = (
            current.get("observation")
            if preserve_current_diagnosis or same_target
            else candidate_observation or current.get("observation")
        ) or fingerprint
        next_file = (
            current.get("next_file")
            if preserve_current_diagnosis or same_target
            else candidate_next_file or current.get("next_file")
        ) or ""
        next_action = (
            current.get("next_action")
            if preserve_current_diagnosis
            else candidate_next_action or current.get("next_action")
        ) or ""
        protected = {
            str(path or "").replace("\\", "/").removeprefix("/workspace/").lstrip("/")
            for path in (protected_files or [])
            if str(path or "").strip()
        }
        if next_file in protected:
            next_file = self._next_file(
                "\n".join(
                    part
                    for part in (
                        parsed.get("next_action", ""),
                        str(observation),
                        fingerprint,
                    )
                    if part
                ),
                excluded=protected,
            )
        current.update(
            {
                "schema_version": 2,
                "test_type": str(test_type or "unknown").strip() or "unknown",
                "test_files": sorted({str(path).strip() for path in test_files if str(path).strip()}),
                "observation": str(observation)[-self.MAX_OBSERVATION_CHARS :],
                "failure_signature": current.get("failure_signature") or self._failure_signature(str(observation)),
                "repeat_count": max(1, int(current.get("repeat_count", 0))),
                "attempted_changes": attempts[-self.MAX_ATTEMPTS :],
                "last_outcome": parsed.get("outcome") or current.get("last_outcome") or "failed",
                "do_not_repeat": do_not_repeat[-self.MAX_ATTEMPTS :],
                "next_file": next_file,
                "next_file_source": "model_handoff",
                "next_action": next_action,
                "diagnostic_priority": max(current_priority, candidate_priority),
            }
        )
        handoffs[requirement] = current
        self._write_handoffs(handoffs)

    def record_failure(self, requirement: str, output: str) -> None:
        """Compatibility wrapper for callers that do not know the test layer."""
        self.record_test_result(
            requirement=requirement,
            test_type="unknown",
            test_files=[],
            output=output,
        )
    def _requirements(self) -> list[dict[str, Any]]:
        rows = sorted(self.store.list_requirements(), key=lambda row: str(row.get("req_id", "")))
        return [
            {
                "id": row.get("req_id", ""),
                "parent": row.get("parent_id"),
                "children": row.get("children_ids") or [],
                "dependencies": row.get("dependencies") or [],
            }
            for row in rows
        ]

    def _project_relations(self, node_id: str, requirements: list[dict[str, Any]]) -> dict[str, Any]:
        if not node_id:
            return {"requirements": [row.get("id", "") for row in requirements]}
        current = next((row for row in requirements if row.get("id") == node_id), {})
        return {
            "current": node_id,
            "parent": current.get("parent"),
            "children": current.get("children") or [],
            "dependencies": current.get("dependencies") or [],
        }

    def _architecture_digest(
        self,
        node_id: str,
        requirements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return a bounded interface-link diagram, without duplicating specifications."""
        if not node_id or not hasattr(self.store, "list_interfaces"):
            return []
        current = next((row for row in requirements if row.get("id") == node_id), {})
        relevant = {
            node_id,
            str(current.get("parent") or "").strip(),
            *(str(item).strip() for item in current.get("dependencies") or []),
        }
        links: list[dict[str, Any]] = []
        for item in self.store.list_interfaces():
            req_ids = [str(value).strip() for value in item.get("req_ids") or [] if str(value).strip()]
            if req_ids and not relevant.intersection(req_ids):
                continue
            content = item.get("content") or {}
            if not isinstance(content, dict):
                try:
                    content = json.loads(str(content or "{}"))
                except json.JSONDecodeError:
                    content = {}
            interface_id = str(item.get("interface_id", "") or "").strip()
            file_path = str(item.get("file_path", "") or "").strip()
            if not interface_id and not file_path:
                continue
            callees = item.get("callees") or content.get("callees") or []
            links.append(
                {
                    "interface": interface_id,
                    "type": str(item.get("type", "") or "").strip(),
                    "owner": file_path,
                    "calls": [str(value).strip() for value in callees if str(value).strip()][:4],
                }
            )
            if len(links) >= 12:
                break
        return links

    def _success_pattern(
        self,
        *,
        requirement: str,
        test_type: str,
        test_files: list[str],
    ) -> dict[str, Any]:
        owners: list[str] = []
        interface_ids: list[str] = []
        covered_interface_ids: set[str] = set()
        normalized_test_files = {
            str(path).strip() for path in test_files if str(path).strip()
        }
        if hasattr(self.store, "list_tests"):
            for test in self.store.list_tests(req_id=requirement):
                if str(test.get("type", "") or "").strip().casefold() != test_type.casefold():
                    continue
                test_path = str(test.get("file_path", "") or "").strip()
                if normalized_test_files and test_path not in normalized_test_files:
                    continue
                covered_interface_ids.update(
                    str(value).strip()
                    for value in test.get("interface_ids") or []
                    if str(value).strip()
                )
        if hasattr(self.store, "list_interfaces"):
            for item in self.store.list_interfaces(req_id=requirement):
                path = str(item.get("file_path", "") or "").strip()
                interface_id = str(item.get("interface_id", "") or "").strip()
                if covered_interface_ids and interface_id not in covered_interface_ids:
                    continue
                if path and path not in owners:
                    owners.append(path)
                if interface_id and interface_id not in interface_ids:
                    interface_ids.append(interface_id)
        return {
            "test_type": test_type,
            "test_files": sorted(normalized_test_files)[:8],
            "verified_owners": owners[:8],
            "verified_interfaces": interface_ids[:12],
        }

    def _relevant_successes(self, node_id: str, successes: dict[str, Any]) -> list[dict[str, Any]]:
        if not node_id:
            return []
        requirement = self.store.get_requirement(node_id) or {}
        relevant = {
            node_id,
            str(requirement.get("parent_id") or "").strip(),
            *(str(item).strip() for item in requirement.get("dependencies") or []),
        }
        packets: list[dict[str, Any]] = []
        for req_id, layers in sorted(successes.items()):
            if req_id not in relevant or not isinstance(layers, dict):
                continue
            for test_type, pattern in sorted(layers.items()):
                if isinstance(pattern, dict):
                    packets.append(dict(pattern, requirement=req_id, test_type=test_type))
        return packets[:12]

    def _relevant_handoffs(self, node_id: str, handoffs: dict[str, Any]) -> list[dict[str, Any]]:
        if not node_id:
            return [
                dict(self._normalize_handoff(value), requirement=key)
                for key, value in sorted(handoffs.items())
                if isinstance(value, dict)
            ]
        requirement = self.store.get_requirement(node_id) or {}
        relevant = {
            node_id,
            str(requirement.get("parent_id") or "").strip(),
            *(str(item).strip() for item in requirement.get("dependencies") or []),
        }
        return [
            dict(self._normalize_handoff(value), requirement=key)
            for key, value in sorted(handoffs.items())
            if key in relevant and isinstance(value, dict)
        ]

    def _load_handoffs(self) -> dict[str, Any]:
        if not self.handoff_path.exists():
            return {}
        try:
            payload = json.loads(self.handoff_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_successes(self) -> dict[str, Any]:
        if not self.success_path.exists():
            return {}
        try:
            payload = json.loads(self.success_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _session_handoff(self, requirement: str) -> dict[str, Any]:
        session_path = self.workspace_dir / ".arc" / "node_sessions" / f"{requirement}.json"
        if not session_path.is_file():
            return {}
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(session, dict):
            return {}
        tdd_handoff = session.get("tdd_handoff") or {}
        checkpoint_summary = str(session.get("checkpoint_handoff_summary") or "").strip()
        summary = str(
            checkpoint_summary
            or session.get("recent_failure_summary")
            or (tdd_handoff.get("last_failed_output_summary") if isinstance(tdd_handoff, dict) else "")
            or ""
        ).strip()
        if not summary or self._is_non_actionable(summary):
            return {}
        test_type = str(
            (tdd_handoff.get("last_test_type") if isinstance(tdd_handoff, dict) else "") or "unknown"
        ).strip()
        parsed = self._parse_handoff_text(summary)
        observation = parsed.get("observation", "")
        if not checkpoint_summary or "Exit Code:" in summary or len(observation) > self.MAX_OBSERVATION_CHARS:
            observation = self._failure_fingerprint(summary)
        test_files: list[str] = []
        if hasattr(self.store, "list_tests"):
            test_files = [
                str(item.get("file_path", "") or "").strip()
                for item in self.store.list_tests(req_id=requirement)
                if str(item.get("file_path", "") or "").strip()
                and (
                    test_type == "unknown"
                    or str(item.get("type", "") or "").strip().casefold() == test_type.casefold()
                )
            ]
        parsed_file = parsed.get("next_file", "")
        owner_file = self._owner_file(requirement)
        if not checkpoint_summary and "/src/" not in parsed_file:
            next_file = owner_file or parsed_file
        else:
            next_file = parsed_file or owner_file or self._next_file(observation)
        return {
            "schema_version": 2,
            "test_type": test_type,
            "test_files": sorted(set(test_files)),
            "observation": observation[-self.MAX_OBSERVATION_CHARS :],
            "failure_signature": self._failure_signature(observation),
            "repeat_count": 1,
            "attempted_changes": [],
            "last_outcome": "failed",
            "do_not_repeat": [],
            "next_file": next_file,
            "next_file_source": "checkpoint_handoff" if checkpoint_summary else "interface_owner",
            "next_action": parsed.get("next_action") or (
                "Compare the failing assertion with this requirement-owned interface and form a new repair hypothesis."
                if next_file
                else ""
            ),
        }

    def _owner_file(self, requirement: str) -> str:
        if not hasattr(self.store, "list_interfaces"):
            return ""
        candidates = [
            item
            for item in self.store.list_interfaces(req_id=requirement)
            if str(item.get("file_path", "") or "").strip()
        ]
        candidates.sort(
            key=lambda item: (
                bool(item.get("implemented")),
                "/src/" not in str(item.get("file_path", "") or "").replace("\\", "/"),
                str(item.get("file_path", "") or ""),
            )
        )
        return str(candidates[0].get("file_path", "") or "").strip() if candidates else ""

    def _normalize_handoff(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        if int(value.get("schema_version", 0) or 0) >= 2:
            return dict(value)
        fingerprint = str(value.get("fingerprint", "") or "").strip()
        parsed = self._parse_handoff_text(fingerprint)
        observation = parsed.get("observation") or fingerprint
        return {
            "schema_version": 2,
            "test_type": value.get("test_type", "unknown"),
            "test_files": value.get("test_files") or [],
            "observation": observation[-self.MAX_OBSERVATION_CHARS :],
            "failure_signature": self._failure_signature(observation),
            "repeat_count": max(1, int(value.get("repeat_count", 0) or 0)),
            "attempted_changes": value.get("attempted_changes") or [],
            "last_outcome": value.get("last_outcome") or "failed",
            "do_not_repeat": value.get("do_not_repeat") or [],
            "next_file": value.get("next_file") or parsed.get("next_file") or self._next_file(fingerprint),
            "next_file_source": value.get("next_file_source") or "legacy_handoff",
            "next_action": value.get("next_action") or parsed.get("next_action") or "",
        }

    def _write_handoffs(self, handoffs: dict[str, Any]) -> None:
        self.handoff_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.handoff_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(dict(sorted(handoffs.items())), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.handoff_path)

    def _write_successes(self, successes: dict[str, Any]) -> None:
        self.success_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.success_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(dict(sorted(successes.items())), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.success_path)

    def _write_audit_file(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        text = (
            "# ARC global project memory\n\n"
            "Generated requirement index and actionable cross-agent handoffs. "
            "Interface and test manifests remain in traceability and are intentionally not duplicated here.\n\n"
            f"```json\n{body}\n```\n"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.read_text(encoding="utf-8", errors="replace") != text:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.path)

    @classmethod
    def _is_non_actionable(cls, output: str) -> bool:
        lowered = str(output or "").lower()
        return any(marker in lowered for marker in cls._NON_ACTIONABLE_FAILURES)

    @staticmethod
    def _exit_code(output: str) -> int | None:
        match = re.search(r"Exit Code:\s*(-?\d+)", str(output or ""), flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    @classmethod
    def _failure_fingerprint(cls, output: str) -> str:
        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", str(output or ""))
        lines = [line.rstrip() for line in cleaned.splitlines()]
        useful: list[str] = []
        markers = ("error", "fail", "exception", "expected", "received", "timeout", " at ", " ❯ ")
        for line in lines:
            stripped = line.strip()
            if not stripped or "...[truncated]" in stripped:
                continue
            lowered = f" {stripped.lower()} "
            if any(marker in lowered for marker in markers) or re.search(r"[\w./-]+\.[A-Za-z0-9]+:\d+", stripped):
                if stripped not in useful:
                    useful.append(stripped)
        if not useful:
            useful = [line.strip() for line in lines[-12:] if line.strip()]
        prioritized = sorted(
            enumerate(useful),
            key=lambda item: (
                0 if any(marker in item[1].casefold() for marker in ("error", "expected", "received")) else 1,
                item[0],
            ),
        )
        return "\n".join(line for _, line in prioritized)[: cls.MAX_FINGERPRINT_CHARS]

    @staticmethod
    def _failure_signature(observation: str) -> str:
        normalized = re.sub(r"\d+", "#", str(observation or "").casefold())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12] if normalized else ""

    @staticmethod
    def _diagnostic_priority(*, observation: str, next_file: str, next_action: str) -> int:
        """Prefer exact compiler/runtime evidence over later generic hypotheses."""
        combined = "\n".join((str(observation or ""), str(next_action or "")))
        score = 0
        if next_file:
            score += 2
        if "/src/" in str(next_file).replace("\\", "/"):
            score += 1
        if next_action:
            score += 1
        if re.search(r"(?:SyntaxError|TypeError|ReferenceError|compiler|parse error)", combined, re.IGNORECASE):
            score += 3
        if re.search(
            r"(?:duplicate export|unexpected token|module parse|malformed trailing)",
            combined,
            re.IGNORECASE,
        ):
            score += 2
        if re.search(r"[\w./-]+\.[A-Za-z0-9]+:\d+(?::\d+)?", combined):
            score += 3
        if re.search(r"\b(?:timeout|port|startup|failed to start)\b", combined, re.IGNORECASE):
            score -= 1
        return max(0, score)

    @staticmethod
    def _resolves_current_hypothesis(parsed: dict[str, str], current: dict[str, Any]) -> bool:
        """Allow memory to advance after evidence explicitly retires the old target."""
        current_file = str(current.get("next_file") or "").replace("\\", "/")
        if not current_file:
            return False
        evidence = "\n".join(
            str(parsed.get(field) or "")
            for field in ("attempted_change", "outcome", "do_not_repeat", "observation")
        )
        mentions_target = current_file in evidence or current_file.rsplit("/", 1)[-1] in evidence
        retires_hypothesis = bool(
            re.search(
                r"(?:\bresolved\b|no longer|failure moved|now starts|now passes|successfully repaired)",
                evidence,
                re.IGNORECASE,
            )
        )
        return mentions_target and retires_hypothesis

    @classmethod
    def _parse_handoff_text(cls, text: str) -> dict[str, str]:
        value = str(text or "").strip()
        labels = {
            "observation": ("LATEST_FAILURE", "LATEST FAILURE", "LATEST FAILURE FINGERPRINT"),
            "attempted_change": ("ATTEMPTED_CHANGE", "ATTEMPTED CHANGE"),
            "outcome": ("OBSERVED_OUTCOME", "OBSERVED OUTCOME"),
            "do_not_repeat": ("DO_NOT_REPEAT", "DO NOT REPEAT"),
            "next_action": ("NEXT_ACTION", "NEXT ACTION"),
            "next_file": ("NEXT_FILE", "NEXT FILE", "NEXT CONCRETE EDIT TARGET"),
        }
        result: dict[str, str] = {}
        all_labels = [label for variants in labels.values() for label in variants]
        boundary = "|".join(re.escape(label) for label in sorted(all_labels, key=len, reverse=True))
        for key, variants in labels.items():
            variant = "|".join(re.escape(label) for label in sorted(variants, key=len, reverse=True))
            match = re.search(
                rf"(?:^|\n)\s*(?:{variant})\s*:\s*(.*?)(?=\n\s*(?:{boundary})\s*:|\Z)",
                value,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if match:
                result[key] = match.group(1).strip()[: cls.MAX_ACTION_CHARS]
        if "observation" not in result:
            split = re.split(r"\n\s*Next concrete edit target\s*:", value, maxsplit=1, flags=re.IGNORECASE)
            result["observation"] = split[0].strip()[: cls.MAX_OBSERVATION_CHARS]
        result["next_file"] = cls._next_file(result.get("next_file", "") or value)
        if "next_action" not in result:
            next_match = re.search(
                r"Next concrete edit target\s*:\s*`?[^`\n]+`?\s*(?:—|-)\s*(.+)$",
                value,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if next_match:
                result["next_action"] = next_match.group(1).strip()[: cls.MAX_ACTION_CHARS]
        return result

    @staticmethod
    def _next_file(fingerprint: str, *, excluded: set[str] | None = None) -> str:
        matches = re.findall(r"(?:/workspace/)?([\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]+)(?::\d+)?", fingerprint)
        candidates: list[str] = []
        for match in matches:
            normalized = match.replace("\\", "/")
            for marker in ("backend/", "frontend/"):
                if marker in normalized:
                    normalized = marker + normalized.split(marker, 1)[1]
                    break
            if any(part in normalized for part in ("node_modules/", "test-results/", ".arc-test-db/")):
                continue
            if normalized in (excluded or set()):
                continue
            if normalized not in candidates:
                candidates.append(normalized)
        return next((path for path in candidates if "/src/" in path), candidates[0] if candidates else "")


def global_memory_enabled() -> bool:
    """Return whether model-visible global memory is enabled for this process."""
    return os.environ.get("ARC_GLOBAL_MEMORY_ENABLED", "1").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }
