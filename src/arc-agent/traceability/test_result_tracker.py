import os
import json
from typing import Dict, List, Optional

# Valid test statuses: direct_pass, retry_pass, relaxed_pass, final_fail
VALID_STATUSES = {"direct_pass", "retry_pass", "relaxed_pass", "final_fail"}
_LEGACY_STATUS_MAP = {
    "downgrade1_pass": "retry_pass",
    "downgrade2_pass": "relaxed_pass",
}
TEST_TYPES = {"Unit", "Integration", "E2E"}


class TestResultTracker:
    """Tracks per-test execution results in a JSON file under .arc/.
    Updated programmatically (not by LLM). Persists across the entire compilation run.
    """

    def __init__(self, arc_dir: str):
        self._path = os.path.join(arc_dir, "test_results.json")
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._normalize_legacy_statuses(data)
                    return data
            except (json.JSONDecodeError, IOError):
                pass
        return {"nodes": {}}

    def _normalize_legacy_statuses(self, data: dict):
        """Map old status names to the current vocabulary."""
        nodes = data.get("nodes", {})
        for _, node in nodes.items():
            for _, tests_of_type in node.items():
                for _, info in tests_of_type.items():
                    status = info.get("status")
                    if status in _LEGACY_STATUS_MAP:
                        info["status"] = _LEGACY_STATUS_MAP[status]

    def save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def record_test(self, node_id: str, test_type: str, test_id: str,
                    file_path: str, status: str, attempts: int):
        """Record the result of a single test."""
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

        nodes = self._data.setdefault("nodes", {})
        node = nodes.setdefault(node_id, {})
        tests_of_type = node.setdefault(test_type, {})
        tests_of_type[test_id] = {
            "status": status,
            "file_path": file_path,
            "attempts": attempts
        }
        self.save()

    def get_test_status(self, node_id: str, test_type: str, test_id: str) -> Optional[str]:
        """Get the status of a specific test, or None if not recorded."""
        return (self._data.get("nodes", {})
                .get(node_id, {})
                .get(test_type, {})
                .get(test_id, {})
                .get("status"))

    def get_node_stats(self, node_id: str) -> Dict:
        """Compute per-type pass rate stats for a node.
        Returns { "Unit": {"direct_pass": n, "retry_pass": n, ...}, ... }
        """
        result = {}
        node = self._data.get("nodes", {}).get(node_id, {})
        for test_type in TEST_TYPES:
            tests = node.get(test_type, {})
            stats = {"direct_pass": 0, "retry_pass": 0, "relaxed_pass": 0, "final_fail": 0, "total": 0}
            for test_id, info in tests.items():
                status = info.get("status", "")
                if status in stats:
                    stats[status] += 1
                stats["total"] += 1
            result[test_type] = stats
        return result

    def get_project_summary(self) -> Dict:
        """Compute aggregate pass rates across all nodes.
        Returns { "all": {...}, "Unit": {...}, "Integration": {...}, "E2E": {...} }
        """
        summary = {}
        for test_type in list(TEST_TYPES) + ["all"]:
            summary[test_type] = {"direct_pass": 0, "retry_pass": 0, "relaxed_pass": 0, "final_fail": 0, "total": 0}

        for node_id, node in self._data.get("nodes", {}).items():
            for test_type in TEST_TYPES:
                tests = node.get(test_type, {})
                for test_id, info in tests.items():
                    status = info.get("status", "")
                    if status in summary[test_type]:
                        summary[test_type][status] += 1
                    summary[test_type]["total"] += 1
                    if status in summary["all"]:
                        summary["all"][status] += 1
                    summary["all"]["total"] += 1

        return summary

    def format_summary(self) -> str:
        """Format a human-readable summary string."""
        lines = ["=== Test Results Summary ==="]

        project = self.get_project_summary()
        lines.append("")
        lines.append("Project Overall:")
        lines.append(self._format_stats(project["all"]))

        for test_type in ["Unit", "Integration", "E2E"]:
            stats = project[test_type]
            if stats["total"] > 0:
                lines.append(f"\n{test_type} Tests:")
                lines.append(self._format_stats(stats))

        # Per-node breakdown
        nodes = self._data.get("nodes", {})
        if nodes:
            lines.append("\n--- Per-Node Breakdown ---")
            for node_id in sorted(nodes.keys()):
                node_stats = self.get_node_stats(node_id)
                node_parts = []
                for test_type in ["Unit", "Integration", "E2E"]:
                    stats = node_stats.get(test_type, {})
                    total = stats.get("total", 0)
                    if total > 0:
                        dp = stats.get("direct_pass", 0)
                        rp = stats.get("retry_pass", 0)
                        lp = stats.get("relaxed_pass", 0)
                        ff = stats.get("final_fail", 0)
                        node_parts.append(
                            f"{test_type}: Direct {dp}/{total} ({dp*100//total}%), "
                            f"Retry {rp}/{total}, Relaxed {lp}/{total}, Fail {ff}/{total}"
                        )
                if node_parts:
                    lines.append(f"\nNode {node_id}:")
                    for part in node_parts:
                        lines.append(f"  {part}")

        return "\n".join(lines)

    def _format_stats(self, stats: Dict) -> str:
        total = stats.get("total", 0)
        if total == 0:
            return "  No tests recorded."
        dp = stats.get("direct_pass", 0)
        rp = stats.get("retry_pass", 0)
        lp = stats.get("relaxed_pass", 0)
        ff = stats.get("final_fail", 0)
        return (
            f"  Direct Pass:    {dp:>3}/{total} ({dp*100/total:5.1f}%)\n"
            f"  Retry Pass:     {rp:>3}/{total} ({rp*100/total:5.1f}%)\n"
            f"  Relaxed Pass:   {lp:>3}/{total} ({lp*100/total:5.1f}%)\n"
            f"  Final Fail:     {ff:>3}/{total} ({ff*100/total:5.1f}%)"
        )
