"""Compiler-owned IDs, atomic local edits and lightweight frontend graph diagnostics."""

from __future__ import annotations

import copy
import json
from typing import Any

from .frontend_generation_contracts import ENTITY_FIELDS, TABLES


DEFAULTS: dict[str, dict[str, Any]] = {
    "components": {"ui_root_id": None},
    "data": {"owner_id": None, "direction": None, "type": None, "required": None, "default": None},
    "properties": {"kind": None, "type": None, "initial": None, "derive": None},
    "ui": {"kind": None, "element": None, "component_ref": None, "slot_data_id": None,
           "attributes": [], "text": None, "children": [], "condition": None, "repeat": None,
           "arguments": [], "callbacks": [], "presentation": ""},
    "events": {"kind": None, "ui_id": None, "event_name": None, "handler_id": None, "arguments": []},
    "handlers": {"reads": [], "writes": [], "invokes": [], "emits": []},
    "effects": {"kind": None, "activation": None, "dependencies": [], "arguments": [],
                "reads": [], "writes": [], "target": None, "async_policy": None, "cleanup": None},
}
REF_FIELDS = {
    "owner_id": "CEHF", "component_id": "C", "ui_root_id": "U", "component_ref": "C",
    "slot_data_id": "D", "ui_id": "U", "handler_id": "H", "event_id": "E",
    "effect_id": "F", "parameter_id": "D", "ref_id": "DP",
    "children": "U", "reads": "DP", "writes": "P", "dependencies": "DP",
}
KINDS = {
    "properties": {"STATE", "DERIVED", "REF"},
    "ui": {"ELEMENT", "TEXT", "FRAGMENT", "COMPONENT", "SLOT"},
    "events": {"UI", "CUSTOM"},
    "effects": {"REQUEST", "SUBSCRIPTION", "STORAGE", "DOM", "NAVIGATION", "TIMER", "OTHER"},
}


def references(value: Any):
    """Walk only IR reference positions, never literal payloads or prose."""
    if isinstance(value, dict):
        if value.get("kind") == "LITERAL":
            return
        for key, child in value.items():
            if key in REF_FIELDS:
                for target in child if isinstance(child, list) else [child]:
                    if isinstance(target, str):
                        yield key, target
            else:
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def decode(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [decode(child, aliases) for child in value]
    if not isinstance(value, dict):
        return value
    if value.get("kind") == "LITERAL":
        literal = json.loads(value["value_json"], parse_constant=_invalid_constant)
        json.dumps(literal, allow_nan=False)
        return {"kind": "LITERAL", "value": literal}
    result = {}
    for key, child in value.items():
        if key in REF_FIELDS:
            result[key] = [resolve(v, aliases) for v in child] if isinstance(child, list) else resolve(child, aliases)
        else:
            result[key] = decode(child, aliases)
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON literal constant: {value}")


def resolve(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("@"):
        if value not in aliases:
            raise ValueError(f"Unresolved local reference {value}")
        return aliases[value]
    return value


class FrontendWorkspace:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in TABLES}
        self.counters = dict.fromkeys(TABLES, 0)
        self.root_component_id: str | None = None
        self.observation_components: dict[str, list[str]] = {}
        self.regions: dict[str, dict[str, str]] = {}

    def allocate(self, table: str) -> str:
        self.counters[table] += 1
        return f"{TABLES[table]}.{self.counters[table]:06d}"

    def new(self, table: str, fields: dict[str, Any], requirements: list[str],
            component_id: str | None = None, entity_id: str | None = None) -> dict[str, Any]:
        row = {"id": entity_id or self.allocate(table), "name": "", "spec": "",
               "requirement_ids": list(dict.fromkeys(requirements)), **copy.deepcopy(DEFAULTS[table]), **fields}
        if table not in {"components", "data"}:
            row["component_id"] = component_id
        self.tables[table][row["id"]] = row
        return row

    def component(self, name: str, spec: str, requirements: list[str]) -> dict[str, Any]:
        row = self.new("components", {"name": name, "spec": spec}, requirements)
        root = self.new("ui", {"name": f"{name} root", "spec": "Component render root.",
                               "kind": "FRAGMENT"}, requirements, row["id"])
        row["ui_root_id"] = root["id"]
        return row

    def ensure_root(self, requirements: list[str]) -> None:
        if self.root_component_id is None:
            self.root_component_id = self.component("App", "Application composition root.", requirements)["id"]

    def find(self, entity_id: str) -> tuple[str, dict[str, Any]]:
        for table, rows in self.tables.items():
            if entity_id in rows:
                return table, rows[entity_id]
        raise ValueError(f"Unknown entity {entity_id}")

    def owner(self, entity_id: str) -> str | None:
        table, row = self.find(entity_id)
        if table == "components":
            return entity_id
        if table == "data":
            owner_id = row.get("owner_id")
            if owner_id and owner_id != entity_id:
                owner_table, owner = self.find(owner_id)
                if owner_table == "components":
                    return owner_id
                if owner_table in {"events", "handlers", "effects"}:
                    return owner.get("component_id")
            return None
        return row.get("component_id")

    def apply_assembly(self, batch: dict[str, Any], requirements: list[str],
                       observation_ids: set[str]) -> dict[str, str]:
        candidate = copy.deepcopy(self)
        aliases: dict[str, str] = {}
        assignments: set[str] = set()
        for item in batch["components"]:
            key = "@" + item["key"]
            if not item["key"] or key in aliases:
                raise ValueError("Assembly keys must be nonempty and unique")
            cid = item["existing_component_id"]
            if cid:
                if cid not in candidate.tables["components"]:
                    raise ValueError(f"Unknown component {cid}")
                row = candidate.tables["components"][cid]
                row.update(name=item["name"], spec=item["spec"])
                row["requirement_ids"] = list(dict.fromkeys(row["requirement_ids"] + requirements))
            else:
                row = candidate.component(item["name"], item["spec"], requirements)
                cid = row["id"]
            aliases[key] = cid
            for oid in item["observation_ids"]:
                if oid not in observation_ids:
                    raise ValueError(f"Observation {oid} is outside this assembly task")
                assignments.add(oid)
                owners = candidate.observation_components.setdefault(oid, [])
                if cid not in owners:
                    owners.append(cid)
        for item in batch["components"]:
            for table, label in (("data", "inputs"), ("properties", "properties")):
                for field in item[label]:
                    key = "@" + field["key"]
                    if not field["key"] or key in aliases:
                        raise ValueError(f"Duplicate or empty local key {key}")
                    aliases[key] = candidate.allocate(table)
        for item in batch["components"]:
            cid = aliases["@" + item["key"]]
            for table, label in (("data", "inputs"), ("properties", "properties")):
                for field in item[label]:
                    values = decode({k: v for k, v in field.items() if k != "key"}, aliases)
                    if table == "data":
                        values.update(owner_id=cid, direction="INPUT")
                    candidate.new(table, values, requirements, cid, aliases["@" + field["key"]])
        for placement in batch["placements"]:
            parent = resolve(placement["parent_ref"], aliases)
            child = resolve(placement["child_ref"], aliases)
            if parent not in candidate.tables["components"] or child not in candidate.tables["components"]:
                raise ValueError("Placement must reference known components")
            if parent == child or child == candidate.root_component_id:
                raise ValueError("A placement cannot contain itself or the application root")
            use = candidate.new("ui", {"name": candidate.tables["components"][child]["name"],
                "spec": placement["spec"], "kind": "COMPONENT", "component_ref": child}, requirements, parent)
            root = candidate.tables["components"][parent]["ui_root_id"]
            candidate.tables["ui"][root]["children"].append(use["id"])
        if assignments != observation_ids:
            raise ValueError(f"Assign remaining observations: {sorted(observation_ids - assignments)}")
        self.__dict__.update(candidate.__dict__)
        return aliases

    def add_regions(self, observations: dict[str, dict[str, Any]]) -> None:
        for oid, owners in self.observation_components.items():
            for cid in owners:
                observation = observations[oid]
                region = self.new("ui", {"name": observation["name"], "spec": observation["spec"],
                    "kind": "FRAGMENT"}, observation["requirement_ids"], cid)
                self.regions.setdefault(cid, {})[oid] = region["id"]
                root = self.tables["components"][cid]["ui_root_id"]
                self.tables["ui"][root]["children"].append(region["id"])

    def apply_patch(self, batch: dict[str, Any], component_id: str, requirements: list[str],
                    editable_ids: set[str], stage: str) -> dict[str, str]:
        candidate = copy.deepcopy(self)
        aliases: dict[str, str] = {}
        allowed = {"data", "properties", "ui"}
        if stage != "ui":
            allowed |= {"events", "handlers", "effects"}
        for item in batch["creates"]:
            if item["table"] not in allowed:
                raise ValueError(f"Cannot create {item['table']} in this pass")
            key = "@" + item["key"]
            if not item["key"] or key in aliases:
                raise ValueError("Create keys must be nonempty and unique")
            aliases[key] = candidate.allocate(item["table"])
        touched = []
        for item in batch["creates"]:
            table = item["table"]
            fields = candidate._fields(table, item["fields"], aliases)
            row = candidate.new(table, fields, requirements, component_id, aliases["@" + item["key"]])
            touched.append((table, row))
        for item in batch["updates"]:
            if item["id"] not in editable_ids:
                raise ValueError(f"Update outside supplied scope: {item['id']}")
            table, row = candidate.find(item["id"])
            if table not in allowed | {"components"}:
                raise ValueError(f"Cannot update {table} in this pass")
            fields = candidate._fields(table, item["fields"], aliases)
            if "owner_id" in fields and fields["owner_id"] != row.get("owner_id"):
                raise ValueError("Data ownership is immutable")
            row.update(fields)
            row["requirement_ids"] = list(dict.fromkeys(row["requirement_ids"] + requirements))
            touched.append((table, row))
        for table, row in touched:
            if candidate.owner(row["id"]) != component_id:
                raise ValueError(f"{row['id']} must belong to the current component")
            if table in KINDS and row.get("kind") not in KINDS[table]:
                raise ValueError(f"{row['id']}: invalid or missing kind")
            if table == "data" and row.get("direction") not in {"INPUT", "OUTPUT"}:
                raise ValueError(f"{row['id']}: missing INPUT/OUTPUT direction")
            if table == "data":
                owner_table, _ = candidate.find(row["owner_id"])
                if owner_table == "components" and row["direction"] != "INPUT":
                    raise ValueError("Component Data must be INPUT")
                if owner_table == "events" and row["direction"] != "OUTPUT":
                    raise ValueError("Event Data must be OUTPUT")
            # Required local reference integrity is cheap; leave full type/behavior checks for later.
            for field, target in references(row):
                target_table, target_row = candidate.find(target)
                if TABLES[target_table] not in REF_FIELDS[field]:
                    raise ValueError(f"{row['id']}.{field}: wrong reference type {target}")
                if field == "writes" and (candidate.owner(target) != component_id or target_row.get("kind") == "DERIVED"):
                    raise ValueError(f"Cannot write {target} from {component_id}")
                if field == "children" and candidate.owner(target) != component_id:
                    raise ValueError("UI children must belong to the same component")
                if table == "events" and field in {"ui_id", "handler_id"} and candidate.owner(target) != component_id:
                    raise ValueError("UI Event and its source/handler must belong to the same component")
            if table == "ui" and row["id"] == candidate.tables["components"][component_id]["ui_root_id"]:
                if row["kind"] != "FRAGMENT":
                    raise ValueError("Keep the compiler-owned root as FRAGMENT")
        self.__dict__.update(candidate.__dict__)
        return aliases

    @staticmethod
    def _fields(table: str, edits: list[dict[str, Any]], aliases: dict[str, str]) -> dict[str, Any]:
        fields = {}
        for edit in edits:
            name = edit["field"]
            if name not in ENTITY_FIELDS[table] or name in fields:
                raise ValueError(f"Unknown or repeated field {table}.{name}")
            fields[name] = edit["value"]
        return decode(fields, aliases)

    def export(self) -> dict[str, Any]:
        return {"root_component_id": self.root_component_id, **{
            table: [copy.deepcopy(rows[key]) for key in sorted(rows)] for table, rows in self.tables.items()
        }}

    def parent_first_components(self) -> list[str]:
        remaining = set(self.tables["components"])
        edges = [(u["component_id"], u["component_ref"]) for u in self.tables["ui"].values()
                 if u["kind"] == "COMPONENT"]
        ordered = []
        while remaining:
            ready = sorted(c for c in remaining if not any(b == c and a in remaining for a, b in edges))
            if not ready:
                ordered.extend(sorted(remaining))
                break
            ordered.extend(ready)
            remaining.difference_update(ready)
        return ordered

    def inspect(self, api_ids: set[str]) -> list[str]:
        warnings = []
        if self.root_component_id not in self.tables["components"]:
            warnings.append("Application root is missing.")
        all_rows = {key: row for rows in self.tables.values() for key, row in rows.items()}
        for table, rows in self.tables.items():
            for row in rows.values():
                for field, target in references(row):
                    if target not in all_rows:
                        warnings.append(f"{row['id']}.{field}: unresolved {target}")
                required = {"data": ["type", "direction", "required"], "properties": ["type", "kind"],
                            "events": ["kind", "event_name"], "effects": ["kind", "activation", "target", "async_policy"]}.get(table, [])
                if table == "properties":
                    required += ["derive" if row["kind"] == "DERIVED" else "initial"]
                if table == "ui":
                    required += {"ELEMENT": ["element"], "TEXT": ["text"], "COMPONENT": ["component_ref"],
                                 "SLOT": ["slot_data_id"]}.get(row["kind"], [])
                if table == "events" and row["kind"] == "UI":
                    required += ["ui_id", "handler_id"]
                for name in ["name", "spec", *required]:
                    if row.get(name) is None or row.get(name) == "":
                        warnings.append(f"{row['id']}: missing {name}")
                if table == "effects" and row["kind"] == "REQUEST" and row["target"] not in api_ids:
                    warnings.append(f"{row['id']}: unknown API {row['target']}")
        graph: dict[str, list[str]] = {}
        for cid, component in self.tables["components"].items():
            graph[cid] = [component["ui_root_id"]]
        for uid, ui in self.tables["ui"].items():
            graph[uid] = list(ui["children"])
            if ui["kind"] == "COMPONENT" and ui["component_ref"]:
                graph[uid].append(ui["component_ref"])
            graph[uid].extend(target for field, target in references(ui.get("arguments", [])) if field == "ui_id")
        visited, active = set(), set()
        stack = [(self.root_component_id, False)] if self.root_component_id else []
        while stack:
            node, leaving = stack.pop()
            if leaving:
                active.discard(node)
                continue
            if node in active:
                warnings.append(f"Render graph cycle at {node}")
                continue
            if node in visited:
                continue
            visited.add(node)
            active.add(node)
            stack.append((node, True))
            stack.extend((child, False) for child in graph.get(node, []))
        for node in sorted(set(graph) - visited):
            warnings.append(f"Unconnected render node {node}")
        return list(dict.fromkeys(warnings))
