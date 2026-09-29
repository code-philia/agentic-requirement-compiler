"""Read-only evidence selection and per-round repair permissions."""
from __future__ import annotations

import copy
import posixpath
import re
from pathlib import Path
from typing import Any

from .code_binding import CodeTargetResolver
from .file_context import direct_dependencies, requirement_dependencies
from .frontend_context import implementation_requirement


class RepairContextBuilder:
    def __init__(self, root: Path, registry: dict[str, Any], frontend_ir=None) -> None:
        self.root = root.resolve()
        self.registry = registry
        self.frontend_ir = frontend_ir

    def read(self, relative: str) -> str:
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise ValueError(f"Missing or unsafe evidence file: {relative}")
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()

    def imports(self, relative: str, source: str) -> set[str]:
        paths = set()
        for specifier in re.findall(r'''(?:from\s+|import\s+)["']([^"']+)["']''', source):
            if specifier == "@arc/shared":
                paths.add("shared/src/index.ts")
                continue
            if not specifier.startswith("."):
                continue
            stem = posixpath.normpath(posixpath.join(posixpath.dirname(relative), specifier))
            stem = stem[:-3] if stem.endswith(".js") else stem
            for candidate in (stem, stem + ".ts", stem + ".tsx", stem + "/index.ts", stem + "/index.tsx"):
                path = (self.root / candidate).resolve()
                if self.root in path.parents and path.is_file():
                    paths.add(candidate)
                    break
        return paths

    def analysis(self, requirement_id: str, requirement: dict[str, Any], *,
                 layer: str, iteration: int, test_result: dict[str, Any],
                 history: list[dict[str, Any]]) -> dict[str, Any]:
        resolved = CodeTargetResolver(self.registry).resolve_requirement_targets(requirement_id)
        bindings = self.registry.get("code_bindings", [])
        tests = {path: self.read(path) for path in test_result["selected_files"]}
        rejection_feedback = [message for event in history if event.get("status") == "REJECTED"
                              for message in event.get("implementation_feedback", [])]
        raw_output = [command.get(key) or "" for command in test_result.get("commands", [])
                      for key in ("stdout", "stderr", "error")]
        evidence = "\n".join([
            *raw_output, *test_result.get("errors", []), *rejection_feedback, *tests.values(),
        ]).replace(chr(92), "/")
        imported = {path for relative, source in tests.items() for path in self.imports(relative, source)}
        changed = {path for event in history for path in event.get("changed_files", [])}
        seeds = list(resolved["owned_targets"])
        # Error locations, test imports/symbols and actual changes may cross layers.
        seeds.extend(row for row in bindings if row.get("file") and (
            row["file"] in evidence or row["file"] in changed or row["file"] in imported
            or row["file"].partition("/")[2] in evidence
            or (row.get("symbol") and re.search(r"\b" + re.escape(row["symbol"]) + r"\b", evidence))
            or ((row.get("route") or {}).get("path") not in {None, "", "/"}
                and row["route"]["path"] in evidence)
        ))
        selected = [*seeds, *requirement_dependencies(
            seeds, bindings, requirement_id, self.frontend_ir)]
        if not seeds:
            # Aggregate requirements can own no functions themselves.
            selected.extend(resolved["dependency_targets"])
        selected.extend(direct_dependencies(
            [row for row in selected if row.get("kind") in {"API", "FUNC", "API_CLIENT", "STORE"}], bindings))
        type_ids = {ref["type_id"] for row in selected
                    for ref in [row.get("input_type"), row.get("output_type"), row.get("props_type"),
                                *row.get("store_types", [])]
                    if isinstance(ref, dict) and ref.get("type_id")}
        selected.extend(row for row in self.registry.get("type_bindings", []) if row["type_id"] in type_ids)
        paths = {row["file"] for row in selected if row.get("file")} | imported
        module_paths = {row["file"] for row in bindings if row.get("file")}
        for path in list(paths):
            # Supply imported helpers/types, without expanding shared App into
            # every unrelated component. Explicit evidence can select those.
            paths.update(self.imports(path, self.read(path)) - module_paths)
        for path in ("backend/src/generated/router.ts", "backend/src/app.ts",
                     "shared/src/index.ts", "frontend/src/index.css"):
            if (self.root / path).is_file():
                paths.add(path)
        sources = {path: self.read(path) for path in sorted(paths - tests.keys())}
        repairable = {row["file"] for row in bindings if row.get("file", "").startswith(
            ("backend/src/", "frontend/src/")) and row.get("kind") in
            {"DB", "FUNC", "API", "API_CLIENT", "STORE", "COMPONENT", "PAGE", "LAYOUT"}}
        requirement, _ = implementation_requirement(requirement, self.root, frontend=True)
        return {
            "requirement_id": requirement_id, "requirement": requirement,
            "test_layer": layer, "iteration": iteration,
            "test_files": tests, "test_result": test_result, "source_files": sources,
            "file_catalog": [{"file": path, "repairable": path in repairable or path in tests}
                             for path in sorted(sources.keys() | tests.keys())],
            "history": copy.deepcopy(history),
        }

    @staticmethod
    def repair(context: dict[str, Any], decision: dict[str, Any],
               history: list[dict[str, Any]]) -> dict[str, Any]:
        suspects = {row["file"] for row in decision["suspected_files"]}
        sources = {**context["source_files"], **context["test_files"]}
        return {
            key: value for key, value in context.items()
            if key not in {"source_files", "file_catalog", "history", "test_files"}
        } | {
            "test_analysis": decision,
            "test_files": {path: source for path, source in context["test_files"].items() if path not in suspects},
            "editable_files": {path: sources[path] for path in sorted(suspects)},
            "related_files": {path: source for path, source in context["source_files"].items() if path not in suspects},
            "history": copy.deepcopy(history),
        }
