from agents.tools.traceability import _requirement_context_payload


class Store:
    def get_requirement(self, req_id):
        return {
            "REQ": {
                "req_id": "REQ",
                "name": "Child",
                "parent_id": "ROOT",
                "children_ids": [],
                "dependencies": ["BASE"],
            },
            "ROOT": {"req_id": "ROOT", "name": "Root"},
            "BASE": {"req_id": "BASE", "name": "Foundation"},
        }.get(req_id)

    def list_interfaces(self, req_id=None):
        return [
            {
                "interface_id": "UI-1",
                "type": "UI",
                "file_path": "frontend/src/App.tsx",
                "implemented": True,
                "content": {"responsibility": "Render the owned view"},
            }
        ]

    def list_tests(self, req_id=None):
        return [{"test_id": "T-1", "type": "E2E", "file_path": "backend/test.ts", "passed": False}]


def test_requirement_context_payload_is_compact_and_traceable():
    payload = _requirement_context_payload(Store(), "REQ")

    assert payload["relations"] == [
        {"relation": "parent", "req_id": "ROOT", "name": "Root"},
        {"relation": "dependency", "req_id": "BASE", "name": "Foundation"},
    ]
    assert payload["interfaces"][0]["file_path"] == "frontend/src/App.tsx"
    assert payload["tests"][0]["passed"] is False
