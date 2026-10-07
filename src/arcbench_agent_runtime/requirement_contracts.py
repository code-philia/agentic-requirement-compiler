"""Deterministic data references; no model-produced UI or data-world graph."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def resolve_requirement_contracts(tree: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(tree)
    nodes: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        nodes.append(node)
        for child in node.get("children") or []:
            walk(child)

    walk(result)
    catalog: dict[str, dict[str, Any]] = {}
    for node in nodes:
        data = node.get("data")
        if not isinstance(data, list):
            continue
        for entry in data:
            # Legacy data without lifecycle retains its original meaning.
            if not isinstance(entry, dict) or "lifecycle" not in entry:
                continue
            identity = entry.get("id")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError(f"Data declaration needs an id: {node.get('id')}")
            if identity in catalog:
                raise ValueError(f"Duplicate data id: {identity}")
            if entry["lifecycle"] not in {"SEED", "CREATED", "DERIVED"}:
                raise ValueError(f"Invalid data lifecycle: {identity}")
            properties = entry.get("properties", {})
            if not (isinstance(properties, dict) or
                    isinstance(properties, list) and all(isinstance(item, dict) for item in properties)):
                raise ValueError(f"Data properties must be a mapping or a list of mappings: {identity}")
            catalog[identity] = entry
    for node in nodes:
        references: list[str] = []
        data = node.get("data")
        if isinstance(data, list):
            references.extend(entry["id"] for entry in data
                              if isinstance(entry, dict) and "lifecycle" in entry)
        for owner in [node, *(node.get("scenarios") or [])]:
            interactions = owner.get("interactions")
            if interactions is not None:
                if not isinstance(interactions, list) or any(not isinstance(item, dict) for item in interactions):
                    raise ValueError(f"interactions must be a list of mappings: {node.get('id')}")
                seen: set[str] = set()
                for item in interactions:
                    for field in ("id", "role", "accessible_name"):
                        if not isinstance(item.get(field), str) or not item[field].strip():
                            raise ValueError(f"Interaction needs {field}: {node.get('id')}")
                    if item["id"] in seen:
                        raise ValueError(f"Duplicate interaction id in {node.get('id')}: {item['id']}")
                    seen.add(item["id"])
            usage = owner.get("data")
            if isinstance(usage, dict) and "requires" in usage:
                required = usage["requires"]
                if not isinstance(required, list) or any(not isinstance(ref, str) for ref in required):
                    raise ValueError(f"data.requires must be a list of IDs: {node.get('id')}")
                references.extend(required)
        unknown = set(references) - catalog.keys()
        if unknown:
            raise ValueError(f"Unknown data references in {node.get('id')}: {sorted(unknown)}")
        node["resolved_data"] = [deepcopy(catalog[ref]) for ref in dict.fromkeys(references)]
    return result


def validate_seed_data_sources(seeds: list[dict[str, Any]], requirements: list[dict[str, Any]]) -> None:
    by_id = {str(node.get("req_id") or node.get("id")): node.get("resolved_data", [])
             for node in requirements}
    catalog = {entry["id"]: entry for entries in by_id.values() for entry in entries}
    for seed in seeds:
        relevant = {entry["id"] for req_id in seed["req_ids"] for entry in by_id.get(req_id, [])}
        references = seed.get("data_ids", [])
        if relevant and not references:
            raise ValueError("Seeds from structured data requirements must cite their SEED data_ids")
        for data_id in references:
            if data_id not in catalog or catalog[data_id]["lifecycle"] != "SEED":
                raise ValueError(f"Seed data_ids must reference declared SEED data: {data_id}")
            if data_id not in relevant:
                raise ValueError(f"Seed source {data_id} is not declared/referenced by its req_ids")
