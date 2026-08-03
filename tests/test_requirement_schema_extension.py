from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from context.context_pipeline import context_pipeline
from context.prompts.interface_designer import get_system_prompt as get_interface_system_prompt
from context.prompts.test_driven_developer import (
    get_system_prompt as get_tdd_system_prompt,
    get_user_prompt as get_tdd_user_prompt,
)
from context.prompts.test_generator import get_system_prompt as get_test_system_prompt
from core.service import configure_runtime, reset_runtime_for_tests
from core.utils import load_requirements
from core.visual_analysis import (
    _build_visual_cache_key,
    _collect_visual_candidates,
    _request_visual_analysis,
    analyze_and_attach_visual_references,
)
from core.workflow import ARCWorkflowManager


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURE_PATH = FIXTURE_DIR / "requirements_extended.yaml"
OLD_DEMO_PATH = REPO_ROOT / "example" / "ticketbooking-demo" / "requirements.yaml"


class RequirementSchemaExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = configure_runtime(project_dir=self.temp_dir.name, app_type="cli")
        self.runtime.traceability.init_store(reset=True)
        self.requirement_tree = load_requirements(FIXTURE_PATH)
        self.runtime.traceability.store_requirement_tree(self.requirement_tree)

    def tearDown(self) -> None:
        context_pipeline.cache.clear()
        reset_runtime_for_tests()
        self.temp_dir.cleanup()

    def test_new_fields_and_actor_round_trip_and_survive_updates(self) -> None:
        root = self.runtime.traceability.get_requirement("SYS-REVIEW")
        module = self.runtime.traceability.get_requirement("MOD-REVIEW")
        workflow = self.runtime.traceability.get_requirement("FLOW-APPROVAL")

        self.assertEqual(root["type"], "MODULE")
        self.assertEqual(
            root["roles"],
            [
                {"id": "USER", "name": "User"},
                {"id": "REVIEWER", "name": "Reviewer"},
                {"id": "ADMIN", "name": "Administrator"},
            ],
        )
        self.assertEqual(root["images"], [{"label": "System overview", "path": "./reference/workflow.svg"}])
        self.assertEqual(module["permissions"], "ALL")
        self.assertEqual(workflow["type"], "WORKFLOW")
        self.assertEqual(workflow["permissions"], ["REVIEWER", "ADMIN"])
        self.assertEqual(workflow["state_flow"], ["DRAFT", "SUBMITTED", "APPROVED"])
        self.assertEqual(workflow["images"][0]["label"], "Approval workspace")

        implicit_id = "FLOW-APPROVAL:SCENARIO:002"
        embedded = {item["scenario_id"]: item for item in workflow["scenarios"]}
        scenario = self.runtime.traceability.get_scenario(implicit_id)
        self.assertEqual(embedded[implicit_id]["id"], implicit_id)
        self.assertEqual(embedded[implicit_id]["actor"], "ADMIN")
        self.assertEqual(scenario["id"], implicit_id)
        self.assertEqual(scenario["scenario_id"], implicit_id)
        self.assertEqual(scenario["actor"], "ADMIN")
        self.assertEqual(scenario["steps"][0]["actor"], "ADMIN")
        self.assertEqual(scenario["steps"][2]["actor"], "USER")

        original_metadata = {
            key: copy.deepcopy(workflow[key])
            for key in ("type", "permissions", "images", "state_flow")
        }
        self.runtime.traceability.update_requirement_fields("FLOW-APPROVAL", description="Updated description")
        self.runtime.traceability.update_requirement_fields(
            "FLOW-APPROVAL",
            visual_reference=[
                "./reference/legacy.svg",
                {
                    "image_path": "./reference/workflow.svg",
                    "label": "Approval workspace",
                    "analysis": "Structured analysis",
                },
            ],
        )
        updated = self.runtime.traceability.get_requirement("FLOW-APPROVAL")
        for key, value in original_metadata.items():
            self.assertEqual(updated[key], value)
        self.assertIsInstance(updated["visual_reference"][0], str)
        self.assertIsInstance(updated["visual_reference"][1], dict)

        existing_steps = self.runtime.traceability.get_scenario(implicit_id)["steps"]
        self.runtime.traceability.upsert_scenario(
            scenario_id=implicit_id,
            req_id="FLOW-APPROVAL",
            name="Approve a submission (updated)",
            steps=existing_steps,
        )
        updated_scenario = self.runtime.traceability.get_scenario(implicit_id)
        updated_requirement = self.runtime.traceability.get_requirement("FLOW-APPROVAL")
        updated_embedded = {
            item["scenario_id"]: item for item in updated_requirement["scenarios"]
        }[implicit_id]
        self.assertEqual(updated_scenario["actor"], "ADMIN")
        self.assertEqual(updated_embedded["actor"], "ADMIN")
        self.assertEqual(updated_scenario["steps"][2]["actor"], "USER")
        self.assertEqual(updated_embedded["steps"][2]["actor"], "USER")
        self.assertEqual(updated_scenario["id"], updated_scenario["scenario_id"])
        self.assertEqual(updated_requirement["id"], updated_requirement["req_id"])
        for key, value in original_metadata.items():
            self.assertEqual(updated_requirement[key], value)

    def test_scenario_ids_are_stable_and_collisions_are_rejected(self) -> None:
        expected_ids = ["SCENARIO-EXPLICIT", "FLOW-APPROVAL:SCENARIO:002"]
        first_ids = [
            item["scenario_id"]
            for item in self.runtime.traceability.get_requirement("FLOW-APPROVAL")["scenarios"]
        ]
        self.runtime.traceability.store_requirement_tree(self.requirement_tree)
        second_ids = [
            item["scenario_id"]
            for item in self.runtime.traceability.get_requirement("FLOW-APPROVAL")["scenarios"]
        ]
        self.assertEqual(first_ids, expected_ids)
        self.assertEqual(second_ids, expected_ids)

        invalid_trees = {
            "conflicting aliases": {
                "id": "ROOT",
                "scenarios": [{"id": "A", "scenario_id": "B", "steps": []}],
            },
            "explicit synthetic collision": {
                "id": "REQ",
                "scenarios": [
                    {"id": "REQ:SCENARIO:002", "steps": []},
                    {"name": "implicit", "steps": []},
                ],
            },
            "cross requirement collision": {
                "id": "ROOT",
                "scenarios": [{"id": "DUPLICATE", "steps": []}],
                "children": [
                    {"id": "CHILD", "scenarios": [{"id": "DUPLICATE", "steps": []}]}
                ],
            },
        }
        for label, tree in invalid_trees.items():
            with self.subTest(label=label):
                self.runtime.traceability.init_store(reset=True)
                with self.assertRaisesRegex(ValueError, "[Cc]onflicting|[Dd]uplicate"):
                    self.runtime.traceability.store_requirement_tree(tree)

    def test_agent_context_contains_roles_permissions_actors_states_and_images(self) -> None:
        context = context_pipeline.build_agent_context("FLOW-APPROVAL", "InterfaceDesigner")
        for expected in (
            '"type":"WORKFLOW"',
            '"role_catalog"',
            '"id":"USER"',
            '"id":"REVIEWER"',
            '"declared_permissions":["REVIEWER","ADMIN"]',
            '"denied_roles":["USER"]',
            '"state_flow":["DRAFT","SUBMITTED","APPROVED"]',
            '"label":"Approval workspace"',
            '"path":"./reference/workflow.svg"',
            '"actor":"ADMIN"',
            '"actor":"USER"',
        ):
            self.assertIn(expected, context)

        tdd_prompt = get_tdd_user_prompt(
            node_id="FLOW-APPROVAL",
            dynamic_context=context,
            test_files=[],
            test_type="Integration",
            node_tests=[],
        )
        self.assertIn('"declared_permissions":["REVIEWER","ADMIN"]', tdd_prompt)
        self.assertIn('"state_flow":["DRAFT","SUBMITTED","APPROVED"]', tdd_prompt)
        self.assertIn('"label":"Approval workspace"', tdd_prompt)

        non_leaf_context = context_pipeline.build_agent_context("MOD-REVIEW", "InterfaceDesigner")
        self.assertIn('"node_scope":"non_leaf"', non_leaf_context)
        self.assertIn("non-leaf node remains UI/composition-only", non_leaf_context)
        self.assertIn("delegate server authorization to leaf children", non_leaf_context)
        self.assertNotIn("at the owned backend boundary", non_leaf_context)

    def test_visual_candidates_merge_new_and_legacy_formats(self) -> None:
        candidates = _collect_visual_candidates(
            {
                "visual_reference": [
                    "./reference/workflow.svg",
                    {
                        "image_path": "./reference/existing.svg",
                        "analysis": "Existing analysis",
                    },
                ],
                "images": [
                    {"label": "Approval workspace", "path": "reference/workflow.svg"},
                    {"label": "Existing screen", "path": "./reference/existing.svg"},
                ],
                "description": "Legacy image: ![Existing alt](reference/existing.svg)",
            }
        )
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["image_path"], "./reference/workflow.svg")
        self.assertEqual(candidates[0]["label"], "Approval workspace")
        self.assertEqual(candidates[1]["analysis"], "Existing analysis")
        self.assertEqual(candidates[1]["label"], "Existing screen")

    def test_visual_request_uses_label_and_label_is_part_of_cache_identity(self) -> None:
        image_path = FIXTURE_DIR / "reference" / "workflow.svg"
        captured: dict[str, object] = {}

        class FakeOpenAI:
            def __init__(self, *, api_key: str, base_url: str) -> None:
                captured["api_key"] = api_key
                captured["base_url"] = base_url

                def create(**kwargs):
                    captured["request"] = kwargs
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="Fake analysis"))]
                    )

                self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        with patch.dict(
            os.environ,
            {
                "VISUAL_API_KEY": "fake-key",
                "VISUAL_BASE_URL": "https://invalid.example.test/v1",
                "VISUAL_MODEL": "fake-visual-model",
            },
        ), patch("core.visual_analysis.OpenAI", FakeOpenAI):
            analysis = asyncio.run(
                _request_visual_analysis(image_path, label="Approval workspace")
            )

        self.assertEqual(analysis, "Fake analysis")
        request = captured["request"]
        user_text = request["messages"][1]["content"][0]["text"]
        self.assertIn("Reference label: Approval workspace", user_text)
        self.assertEqual(
            _build_visual_cache_key(image_path, label="Approval workspace"),
            _build_visual_cache_key(image_path, label="Approval workspace"),
        )
        self.assertNotEqual(
            _build_visual_cache_key(image_path, label="Approval workspace"),
            _build_visual_cache_key(image_path, label="System overview"),
        )

    def test_partial_visual_failure_preserves_unprocessed_reference_and_new_fields(self) -> None:
        self.runtime.traceability.update_requirement_fields(
            "FLOW-APPROVAL",
            visual_reference=[
                "./reference/workflow.svg",
                "./reference/missing.svg",
            ],
        )
        requirement = self.runtime.traceability.get_requirement("FLOW-APPROVAL")
        request = AsyncMock(return_value="Generated analysis")
        with patch("core.visual_analysis._request_visual_analysis", new=request):
            asyncio.run(
                analyze_and_attach_visual_references(
                    workspace_path=self.temp_dir.name,
                    requirements_dir=str(FIXTURE_DIR),
                    requirement_data=requirement,
                )
            )

        request.assert_awaited_once()
        self.assertEqual(request.await_args.kwargs["label"], "Approval workspace")
        stored = self.runtime.traceability.get_requirement("FLOW-APPROVAL")
        references = {item["image_path"]: item for item in stored["visual_reference"]}
        self.assertEqual(references["./reference/workflow.svg"]["label"], "Approval workspace")
        self.assertEqual(references["./reference/workflow.svg"]["analysis"], "Generated analysis")
        self.assertIn("./reference/missing.svg", references)
        self.assertNotIn("analysis", references["./reference/missing.svg"])
        self.assertEqual(stored["permissions"], ["REVIEWER", "ADMIN"])
        self.assertEqual(stored["state_flow"], ["DRAFT", "SUBMITTED", "APPROVED"])
        self.assertEqual(stored["images"][0]["label"], "Approval workspace")

        self.runtime.traceability.update_requirement_fields(
            "FLOW-APPROVAL",
            visual_reference=["./reference/workflow.svg", "./reference/missing.svg"],
        )
        cache_hit_request = AsyncMock(side_effect=AssertionError("same label should use cache"))
        with patch("core.visual_analysis._request_visual_analysis", new=cache_hit_request):
            asyncio.run(
                analyze_and_attach_visual_references(
                    workspace_path=self.temp_dir.name,
                    requirements_dir=str(FIXTURE_DIR),
                    requirement_data=self.runtime.traceability.get_requirement("FLOW-APPROVAL"),
                )
            )
        cache_hit_request.assert_not_awaited()

        self.runtime.traceability.update_requirement_fields(
            "FLOW-APPROVAL",
            images=[{"label": "Changed approval workspace", "path": "./reference/workflow.svg"}],
        )
        changed_label_request = AsyncMock(return_value="Changed-label analysis")
        with patch("core.visual_analysis._request_visual_analysis", new=changed_label_request):
            asyncio.run(
                analyze_and_attach_visual_references(
                    workspace_path=self.temp_dir.name,
                    requirements_dir=str(FIXTURE_DIR),
                    requirement_data=self.runtime.traceability.get_requirement("FLOW-APPROVAL"),
                )
            )
        changed_label_request.assert_awaited_once()
        self.assertEqual(changed_label_request.await_args.kwargs["label"], "Changed approval workspace")
        changed = self.runtime.traceability.get_requirement("FLOW-APPROVAL")["visual_reference"][0]
        self.assertEqual(changed["label"], "Changed approval workspace")
        self.assertEqual(changed["analysis"], "Changed-label analysis")

    def test_old_demo_remains_loadable_traceable_and_plannable_without_rbac_invention(self) -> None:
        old_tree = load_requirements(OLD_DEMO_PATH)
        self.runtime.traceability.store_requirement_tree(old_tree)
        requirements = self.runtime.traceability.list_requirements()
        self.assertEqual(len(requirements), 12)
        self.assertEqual({item["type"] for item in requirements}, {"FOLDER", "ATOMIC"})

        manager = object.__new__(ARCWorkflowManager)
        tasks = manager._build_processing_tasks(old_tree)
        expected_task_ids = [
            "ROOT:DESIGN",
            "REQ-1:DESIGN",
            "REQ-1.1:DESIGN",
            "REQ-1.1:IMPLEMENT",
            "REQ-1.2:DESIGN",
            "REQ-1.2:IMPLEMENT",
            "REQ-1:IMPLEMENT",
            "REQ-2:DESIGN",
            "REQ-2.1:DESIGN",
            "REQ-2.1:IMPLEMENT",
            "REQ-2.2:DESIGN",
            "REQ-2.2:IMPLEMENT",
            "REQ-2.3:DESIGN",
            "REQ-2.3:IMPLEMENT",
            "REQ-2:IMPLEMENT",
            "REQ-3:DESIGN",
            "REQ-3.1:DESIGN",
            "REQ-3.1:IMPLEMENT",
            "REQ-3.2:DESIGN",
            "REQ-3.2:IMPLEMENT",
            "REQ-3.3:DESIGN",
            "REQ-3.3:IMPLEMENT",
            "REQ-3:IMPLEMENT",
            "ROOT:IMPLEMENT",
        ]
        self.assertEqual([task["task_id"] for task in tasks], expected_task_ids)

        old_context = context_pipeline.build_agent_context("REQ-1.1", "TestGenerator")
        self.assertNotIn('"permission_contract"', old_context)
        self.assertNotIn('"role_catalog"', old_context)

        mixed_tree = {
            "id": "OLD-MIXED",
            "type": "ATOMIC",
            "visual_reference": [
                "./reference/legacy.png",
                {"image_path": "./reference/analyzed.png", "analysis": "Analysis"},
            ],
        }
        self.runtime.traceability.store_requirement_tree(mixed_tree)
        mixed = self.runtime.traceability.get_requirement("OLD-MIXED")
        self.assertIsInstance(mixed["visual_reference"][0], str)
        self.assertIsInstance(mixed["visual_reference"][1], dict)
        mixed_context = context_pipeline.build_agent_context("OLD-MIXED", "InterfaceDesigner")
        self.assertIn('"image_path":"./reference/legacy.png"', mixed_context)
        self.assertIn('"analysis":"Analysis"', mixed_context)

    def test_stage_prompts_require_backend_authorization_and_state_scope(self) -> None:
        with patch.dict(os.environ, {"ARC_APP_TYPE": "cli"}):
            interface_prompt = get_interface_system_prompt()
            test_prompt = get_test_system_prompt()
            tdd_prompt = get_tdd_system_prompt()

        self.assertIn("UI hiding is supplementary", interface_prompt)
        self.assertIn("non-leaf node remains UI/composition-only", interface_prompt)
        self.assertIn("Integration/API coverage", test_prompt)
        self.assertIn("401", test_prompt)
        self.assertIn("403", test_prompt)
        self.assertIn("never replace backend rejection coverage", test_prompt)
        self.assertIn("before returning protected data or committing state", tdd_prompt)
        self.assertIn("data scopes", tdd_prompt)
        self.assertIn("state transitions", tdd_prompt)


if __name__ == "__main__":
    unittest.main()
