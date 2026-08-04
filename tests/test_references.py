from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import yaml

from agents.factory import _build_filesystem_permissions
from context.context_pipeline import context_pipeline
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.middleware.filesystem import _check_fs_permission
from core.reference_documents import (
    audit_requirement_tree_references,
    available_reference_paths,
    build_reference_catalog,
)
from core.service import configure_runtime, reset_runtime_for_tests
from core.utils import load_requirements
from core.phases import WorkflowPhaseRunner
from core.workflow import ARCWorkflowManager


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURE_PATH = FIXTURE_DIR / "requirements_extended.yaml"


class ReferenceSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = configure_runtime(
            project_dir=self.temp_dir.name,
            requirements_dir=str(FIXTURE_DIR),
            app_type="cli",
        )
        self.runtime.traceability.init_store(reset=True)
        self.tree = load_requirements(FIXTURE_PATH)
        self.runtime.traceability.store_requirement_tree(self.tree)

    def tearDown(self) -> None:
        context_pipeline.cache.clear()
        reset_runtime_for_tests()
        self.temp_dir.cleanup()

    def test_catalog_inherits_root_to_current_without_inlining_content(self) -> None:
        catalog = context_pipeline.get_reference_catalog("FLOW-APPROVAL")

        self.assertEqual(
            [item["virtual_path"] for item in catalog],
            [
                "/references/reference_docs/global-standard.md",
                "/references/reference_docs/approval-policy.txt",
            ],
        )
        self.assertEqual([item["declared_on"] for item in catalog], ["SYS-REVIEW", "FLOW-APPROVAL"])
        self.assertEqual([item["relation"] for item in catalog], ["ancestor", "current"])
        self.assertEqual(catalog[1]["label"], "approval-policy.txt")
        self.assertTrue(all(item["available"] for item in catalog))

        context = context_pipeline.build_agent_context("FLOW-APPROVAL", "TestDrivenDeveloper")
        self.assertIn("<reference_catalog>", context)
        self.assertIn('"available_reference_count":2', context)
        self.assertIn('"inherited_reference_count":1', context)
        self.assertNotIn("Every approval decision must retain an auditable reviewer identity", context)

    def test_available_paths_are_deduplicated_and_permissions_are_exact_read_only(self) -> None:
        catalog = context_pipeline.get_reference_catalog("FLOW-APPROVAL")
        paths = available_reference_paths([*catalog, dict(catalog[0])])
        self.assertEqual(
            paths,
            [
                "/references/reference_docs/global-standard.md",
                "/references/reference_docs/approval-policy.txt",
            ],
        )

        permissions = _build_filesystem_permissions(
            Path(self.temp_dir.name),
            [self.temp_dir.name],
            readable_reference_paths=[*paths, "/references/../outside.txt", "/workspace/not-a-reference.md"],
        )
        reference_read_allows = [
            permission
            for permission in permissions
            if permission.mode == "allow"
            and permission.operations == ["read"]
            and any(path.startswith("/references/") for path in permission.paths)
        ]
        self.assertEqual(len(reference_read_allows), 1)
        self.assertEqual(reference_read_allows[0].paths, sorted(paths))
        self.assertNotIn("/references/**", reference_read_allows[0].paths)
        self.assertTrue(
            any(
                permission.mode == "deny"
                and permission.operations == ["write"]
                and "/references/**" in permission.paths
                for permission in permissions
            )
        )
        self.assertEqual(_check_fs_permission(permissions, "read", paths[0]), "allow")
        self.assertEqual(
            _check_fs_permission(permissions, "read", "/references/reference_docs/undeclared.md"),
            "deny",
        )
        self.assertEqual(_check_fs_permission(permissions, "write", paths[0]), "deny")

        backend = CompositeBackend(
            default=StateBackend(),
            routes={
                "/references/": FilesystemBackend(
                    root_dir=str(FIXTURE_DIR),
                    virtual_mode=True,
                )
            },
        )
        first_line = backend.read(paths[0], offset=0, limit=1)
        self.assertIsNone(first_line.error)
        self.assertEqual(first_line.file_data["content"], "# Global review standard\n")
        self.assertEqual(first_line.next_offset, 1)

    def test_invalid_references_are_unavailable_without_hiding_valid_entries(self) -> None:
        root = Path(self.temp_dir.name) / "requirements"
        root.mkdir()
        (root / "valid.md").write_text("valid reference\n", encoding="utf-8")
        (root / "bad.txt").write_bytes(b"\xff\xfe\x00")
        (root / "binary.pdf").write_bytes(b"%PDF-1.7")
        (root / "folder.md").mkdir()
        outside = Path(self.temp_dir.name) / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        symlink = root / "outside-link.md"
        try:
            symlink.symlink_to(outside)
        except OSError:
            symlink = None

        references: list[object] = [
            {"label": "Valid", "path": "./valid.md"},
            {"path": "./missing.md"},
            {"path": "./bad.txt"},
            {"path": "./binary.pdf"},
            {"path": "./folder.md"},
            {"path": "../outside.md"},
            {"path": str(outside)},
            {"path": "https://example.test/standard.md"},
            {"label": "No path"},
            "not-a-mapping",
        ]
        if symlink is not None:
            references.append({"path": "./outside-link.md"})
        tree = {"id": "ROOT", "references": references}

        issues = audit_requirement_tree_references(tree, root)
        errors = "\n".join(item["error"] for item in issues)
        for expected in (
            "file does not exist",
            "not valid UTF-8",
            "unsupported reference format",
            "not a regular file",
            "path traversal",
            "absolute paths",
            "URLs are not supported",
            "path is required",
            "must be a mapping",
        ):
            self.assertIn(expected, errors)
        if symlink is not None:
            self.assertIn("resolves outside", errors)

        catalog = build_reference_catalog(
            node_id="ROOT",
            requirement_data={"id": "ROOT", "references": [item for item in references if isinstance(item, dict)]},
            store=None,
            requirements_dir=root,
        )
        self.assertTrue(catalog[0]["available"])
        self.assertEqual(catalog[0]["virtual_path"], "/references/valid.md")
        self.assertTrue(all(not item["available"] for item in catalog[1:]))
        self.assertEqual(available_reference_paths(catalog), ["/references/valid.md"])

        self.runtime.traceability.store_requirement_tree(
            {"id": "INVALID", "references": [item for item in references if isinstance(item, dict)]}
        )
        context_pipeline.configure(requirements_dir=str(root))
        invalid_context = context_pipeline.build_agent_context("INVALID", "InterfaceDesigner")
        self.assertIn('"available":false', invalid_context)
        self.assertIn("file does not exist", invalid_context)
        self.assertNotIn('"virtual_path":"/references/missing.md"', invalid_context)

    def test_workflow_logs_reference_warnings_and_still_loads_tree(self) -> None:
        requirement_dir = Path(self.temp_dir.name) / "warning-case"
        requirement_dir.mkdir()
        requirement_path = requirement_dir / "requirements.yaml"
        requirement_path.write_text(
            yaml.safe_dump(
                {
                    "id": "ROOT",
                    "references": [
                        {"label": "Missing standard", "path": "./missing.md"},
                        {"label": "Available", "path": "./available.md"},
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (requirement_dir / "available.md").write_text("available\n", encoding="utf-8")
        logs: list[tuple[str, str, str | None, str | None]] = []

        def log_cb(agent: str, message: str, status: str | None, node_id: str | None) -> None:
            logs.append((agent, message, status, node_id))

        manager = object.__new__(ARCWorkflowManager)
        manager.requirement_path = str(requirement_path)
        manager.log_cb = log_cb
        loaded = asyncio.run(manager.load_requirement_tree())

        self.assertEqual(loaded["id"], "ROOT")
        warnings = [item for item in logs if item[2] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Missing standard", warnings[0][1])
        self.assertIn("file does not exist", warnings[0][1])
        self.assertEqual(warnings[0][3], "ROOT")

    def test_reference_only_non_leaf_still_skips_interface_design(self) -> None:
        self.runtime.traceability.update_requirement_fields(
            "MOD-REVIEW",
            references=[{"path": "./reference_docs/global-standard.md"}],
        )
        runner = object.__new__(WorkflowPhaseRunner)
        runner.workspace_path = self.temp_dir.name
        runner.requirement_path = str(FIXTURE_PATH)
        runner.log_cb = None
        runner.interface_designer = AsyncMock()
        runner._update_node_session = lambda _node_id, _patch: None

        result = asyncio.run(
            runner.run_design_phase(
                "MOD-REVIEW",
                self.runtime.traceability.get_requirement("MOD-REVIEW"),
            )
        )

        self.assertTrue(result)
        runner.interface_designer.run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
