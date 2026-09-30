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
        json.dumps(value["value"], allow_nan=False)
        return copy.deepcopy(value)
    result = {}
    for key, child in value.items():
        if key in REF_FIELDS:
            result[key] = [resolve(v, aliases) for v in child] if isinstance(child, list) else resolve(child, aliases)
        else:
            result[key] = decode(child, aliases)
    return result


def resolve(value: Any, aliases: dict[str, str]) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"id"}:
        return value["id"]
    if isinstance(value, dict) and set(value) == {"local"}:
        if value["local"] not in aliases:
            raise ValueError(f"Unresolved local reference {value['local']}")
        return aliases[value["local"]]
    raise ValueError("Expected reference {id: string} or {local: string}")


class FrontendWorkspace:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in TABLES}
        self.counters = dict.fromkeys(TABLES, 0)
        self.root_component_id: str | None = None

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

    def apply_requirement_batch(self, batch: dict[str, Any], requirements: list[str],
                                editable_ids: set[str], stage: str) -> dict[str, Any]:
        """One atomic decision may edit several components for the same requirement."""
        candidate = copy.deepcopy(self)
        aliases: dict[str, str] = {}
        touched: dict[str, str] = {}
        before = {eid: copy.deepcopy(self.find(eid)[1]) for eid in editable_ids}

        def alias(key: str, entity_id: str) -> None:
            if not key or key in aliases:
                raise ValueError(f"Duplicate or empty key {key}")
            aliases[key] = entity_id

        pending_components = list(batch.get("components", []))
        while pending_components:
            item = next((item for item in pending_components
                         if not item["existing_component_id"]
                         or "id" in item["existing_component_id"]
                         or item["existing_component_id"].get("local") in aliases), None)
            if item is None:
                raise ValueError("Cyclic or unresolved component declaration dependencies")
            pending_components.remove(item)
            cid = resolve(item["existing_component_id"], aliases)
            if cid:
                if cid not in candidate.tables["components"]:
                    raise ValueError(f"Unknown component {cid}")
                if cid not in editable_ids | set(aliases.values()):
                    raise ValueError(f"Assembly outside editable_ids: {cid}")
                component = candidate.tables["components"][cid]
                component["requirement_ids"] = list(dict.fromkeys(component["requirement_ids"] + requirements))
            else:
                component = candidate.component(item["name"], item["spec"], requirements)
            alias(item["key"], component["id"])
            alias(item["key"] + ".root", component["ui_root_id"])
            touched[component["id"]] = "components"
            touched[component["ui_root_id"]] = "ui"
        allowed = {"data", "properties", "ui"}
        if stage == "behavior":
            allowed |= {"events", "handlers", "effects"}
        for item in batch["creates"]:
            if item["table"] not in allowed:
                raise ValueError(f"Cannot create {item['table']} in {stage}")
            alias(item["key"], candidate.allocate(item["table"]))
        # Reserve use-site symbols too, so every create may forward-reference them.
        for item in batch.get("components", []):
            if item["ui_ids"]:
                alias(item["key"] + ".use", candidate.allocate("ui"))
        for item in batch.get("components", []):
            if not item["ui_ids"]:
                continue
            cid = aliases[item["key"]]
            first = resolve(item["ui_ids"][0], aliases)
            parent = candidate.owner(first) if first in candidate.tables["ui"] else None
            use = next((u for u in candidate.tables["ui"].values()
                        if item["existing_component_id"] and u["kind"] == "COMPONENT"
                        and u["component_ref"] == cid and u["component_id"] == parent), None)
            if use:
                aliases[item["key"] + ".use"] = use["id"]
        for item in batch["creates"]:
            cid = resolve(item["component_ref"], aliases) or candidate.root_component_id
            if cid not in candidate.tables["components"]:
                raise ValueError(f"Unknown owning component {cid}")
            fields = candidate._fields(item["table"], item["fields"], aliases)
            row = candidate.new(item["table"], fields, requirements, cid, aliases[item["key"]])
            touched[row["id"]] = item["table"]
        # Materialize declarations before extraction. A use-site dependency waits
        # for its producing extraction; unrelated extractions retain input order.
        pending = list(batch.get("components", []))
        while pending:
            pending_uses = {item["key"] + ".use" for item in pending}
            ready = next((item for item in pending
                          if all(v.get("local") not in pending_uses
                                 and resolve(v, aliases) in candidate.tables["ui"]
                                 for v in item["ui_ids"])), None)
            if ready is None:
                raise ValueError("Cyclic or unresolved assembly UI dependencies")
            pending.remove(ready)
            cid = aliases[ready["key"]]
            extraction = {**ready, **{name: [resolve(v, aliases) for v in ready[name]]
                                      for name in ("ui_ids", "property_ids", "data_ids")}}
            use_id, moved = candidate._extract(
                extraction, cid, requirements, editable_ids | set(aliases.values()),
                aliases.get(ready["key"] + ".use"))
            if ready["ui_ids"] and use_id is None:
                raise ValueError(f"Assembly {ready['key']} must extract UI from another component")
            touched.update(moved)
        for item in batch["updates"]:
            eid = resolve(item["id"], aliases)
            if eid not in editable_ids | set(aliases.values()):
                raise ValueError(f"Update outside supplied requirement context: {eid}")
            table, row = candidate.find(eid)
            if item.get("component_id") is not None and resolve(item["component_id"], aliases) != row.get("component_id"):
                raise ValueError("Move component ownership only through assembly declarations")
            # ui_root_id and requirement_ids are compiler-owned, never update decisions.
            if item["table"] != table:
                raise ValueError(f"Update {eid} belongs to {table}, not {item['table']}; "
                                 f"use table={table} and its allowed fields {sorted(ENTITY_FIELDS[table])}")
            if table not in allowed | {"components"}:
                raise ValueError(f"Cannot update {table} in {stage}")
            fields = candidate._fields(table, item["fields"], aliases)
            if "owner_id" in fields and fields["owner_id"] != row.get("owner_id"):
                raise ValueError("Move ownership only through assembly component declarations")
            row.update(fields)
            row["requirement_ids"] = list(dict.fromkeys(row["requirement_ids"] + requirements))
            touched[eid] = table

        # Check after ALL edits, allowing same-batch child events and parent callbacks.
        for eid, table in touched.items():
            row = candidate.tables[table][eid]
            owner = candidate.owner(eid)
            if owner not in candidate.tables["components"]:
                raise ValueError(f"{eid}: missing component owner")
            if table in KINDS and row.get("kind") not in KINDS[table]:
                raise ValueError(f"{eid}: invalid kind")
            if table == "data":
                owner_table, _ = candidate.find(row["owner_id"])
                if row["direction"] not in {"INPUT", "OUTPUT"}:
                    raise ValueError(f"{eid}: missing direction")
                if owner_table == "components" and row["direction"] != "INPUT":
                    raise ValueError("Component Data must be INPUT")
                if owner_table == "events" and row["direction"] != "OUTPUT":
                    raise ValueError("Event Data must be OUTPUT")
            for field, target in references(row):
                target_table, target_row = candidate.find(target)
                if TABLES[target_table] not in REF_FIELDS[field]:
                    raise ValueError(f"{eid}.{field}: wrong reference type {target}")
                if field == "writes" and (candidate.owner(target) != owner or target_row.get("kind") == "DERIVED"):
                    raise ValueError(f"{eid}: cannot write {target}")
                if field == "children" and candidate.owner(target) != owner:
                    raise ValueError("UI children must have the same owner")
                if table == "events" and field in {"ui_id", "handler_id"} and candidate.owner(target) != owner:
                    raise ValueError("UI Event source/handler must have the same owner")
            if table == "ui" and eid == candidate.tables["components"][owner]["ui_root_id"]:
                if row["kind"] != "FRAGMENT":
                    raise ValueError("Keep component roots as FRAGMENT")
        for reference in batch.get("associations", []):
            eid = resolve(reference, aliases)
            table, row = candidate.find(eid)
            row["requirement_ids"] = list(dict.fromkeys(row["requirement_ids"] + requirements))
            touched[eid] = table
        # Reused UI retains its concrete value contracts in requirement traceability.
        # Only direct expression references are included, not render/ownership traversal.
        for eid, table in list(touched.items()):
            if table != "ui":
                continue
            for field, target in references(candidate.tables["ui"][eid]):
                if field != "ref_id":
                    continue
                target_table, row = candidate.find(target)
                if target_table in {"data", "properties"}:
                    row["requirement_ids"] = list(dict.fromkeys(row["requirement_ids"] + requirements))
                    touched[target] = target_table
        # After inventory, newly-created render nodes must be connected.
        # Validate this on the candidate so failed batches leave no partial UI.
        render_graph: dict[str, list[str]] = {
            cid: [component["ui_root_id"]]
            for cid, component in candidate.tables["components"].items()
        }
        for uid, ui in candidate.tables["ui"].items():
            targets = list(ui.get("children", []))
            if ui.get("kind") == "COMPONENT" and ui.get("component_ref"):
                targets.append(ui["component_ref"])
            render_graph[uid] = targets
        reachable: set[str] = set()
        pending_nodes = [candidate.root_component_id]
        while pending_nodes:
            node = pending_nodes.pop()
            if not node or node in reachable:
                continue
            reachable.add(node)
            pending_nodes.extend(render_graph.get(node, []))
        newly_created_ui = {
            entity_id for key, entity_id in aliases.items()
            if "." not in key and entity_id in candidate.tables["ui"] and entity_id not in self.tables["ui"]
        }
        detached = sorted(entity_id for entity_id in newly_created_ui if entity_id not in reachable)
        # Pass 1 is an inventory pass. Its atomic UI records are deliberately
        # allowed to remain staged until assemble establishes containment and
        # component boundaries. Later passes must leave a reachable tree.
        if detached and stage != "ui":
            raise ValueError("New UI nodes are not connected to the application render root: " + ", ".join(detached))
        changes = [{"before": before[eid], "after": copy.deepcopy(candidate.find(eid)[1])}
                   for eid in touched if eid in before and before[eid] != candidate.find(eid)[1]]
        self.__dict__.update(candidate.__dict__)
        return {"aliases": aliases, "touched_ids": list(touched), "changes": changes}

    def _extract(self, item: dict[str, Any], cid: str, requirements: list[str],
                 editable_ids: set[str], planned_use_id: str | None = None) -> tuple[str | None, dict[str, str]]:
        roots = list(dict.fromkeys(item["ui_ids"]))
        if not roots:
            if item["property_ids"] or item["data_ids"]:
                raise ValueError("Extract state/inputs with an explicit UI subtree")
            return None, {}
        if any(uid not in editable_ids or uid not in self.tables["ui"] for uid in roots):
            raise ValueError("Extraction roots must be supplied UI IDs")
        owners = {self.owner(uid) for uid in roots}
        if len(owners) != 1:
            raise ValueError("Extract UI from one current owner per component declaration")
        parent = owners.pop()
        component_roots = {c["ui_root_id"] for c in self.tables["components"].values()}
        if any(uid in component_roots for uid in roots):
            raise ValueError("Do not extract a component root")
        if parent == cid:
            return None, {}
        subtree, pending = set(), list(roots)
        while pending:
            uid = pending.pop()
            if uid in subtree:
                continue
            if self.owner(uid) != parent:
                raise ValueError("Extraction crosses a UI ownership boundary")
            subtree.add(uid)
            pending.extend(self.tables["ui"][uid]["children"])
        if any(child in roots for uid in subtree for child in self.tables["ui"][uid]["children"]):
            raise ValueError("Extraction roots must not overlap")
        if any(self.tables["ui"][uid].get("component_ref") == cid for uid in subtree):
            raise ValueError("Extraction would contain the target component itself")
        # Preserve the first extracted position, not an unrelated append at the application end.
        containers = [(u, u["children"].index(roots[0])) for u in self.tables["ui"].values()
                      if roots[0] in u["children"] and u["id"] not in subtree]
        if containers:
            host, position = containers[0]
        else:
            # Staged pass-1 atoms have an owner but no layout parent yet.
            # Assemble may promote them into a component at the application
            # root; this is the first point at which containment is decided.
            host = self.tables["ui"][self.tables["components"][parent]["ui_root_id"]]
            position = len(host["children"])
        touched = {}
        for ui in self.tables["ui"].values():
            if ui["id"] not in subtree and any(uid in ui["children"] for uid in roots):
                ui["children"] = [uid for uid in ui["children"] if uid not in roots]
                touched[ui["id"]] = "ui"
        for uid in subtree:
            self.tables["ui"][uid]["component_id"] = cid
            touched[uid] = "ui"
        for table, key in (("properties", "property_ids"), ("data", "data_ids")):
            for eid in item[key]:
                if eid in editable_ids and eid in self.tables[table]:
                    row = self.tables[table][eid]
                    if row.get("owner_id" if table == "data" else "component_id") == cid:
                        continue  # Already declared directly in the target component.
                if eid not in editable_ids or eid not in self.tables[table] or self.owner(eid) != parent:
                    raise ValueError(f"Cannot extract {eid}")
                row = self.tables[table][eid]
                if table == "data" and row["owner_id"] != parent:
                    raise ValueError("Only component inputs can be extracted")
                row["owner_id" if table == "data" else "component_id"] = cid
                touched[eid] = table
        root = self.tables["components"][cid]["ui_root_id"]
        self.tables["ui"][root]["children"].extend(roots)
        touched[root] = "ui"
        use = self.tables["ui"].get(planned_use_id) if planned_use_id else next((u for u in self.tables["ui"].values()
                    if item["existing_component_id"] and u["kind"] == "COMPONENT"
                    and u["component_ref"] == cid and u["component_id"] == parent), None)
        if use is None:
            use = self.new("ui", {"name": self.tables["components"][cid]["name"], "spec": item["spec"],
                                 "kind": "COMPONENT", "component_ref": cid}, requirements, parent, planned_use_id)
            host["children"].insert(position, use["id"])
        else:
            use["requirement_ids"] = list(dict.fromkeys(use["requirement_ids"] + requirements))
        touched[host["id"]] = "ui"
        touched[use["id"]] = "ui"
        return use["id"], touched

    @staticmethod
    def _fields(table: str, edits: list[dict[str, Any]], aliases: dict[str, str]) -> dict[str, Any]:
        fields = {}
        for edit in edits:
            name = edit["field"]
            if name not in ENTITY_FIELDS[table]:
                raise ValueError(f"Unsupported field {table}.{name}; allowed: {sorted(ENTITY_FIELDS[table])}")
            if name in fields:
                raise ValueError(f"Repeated field {table}.{name}; provide the field once")
            fields[name] = edit["value"]
        return decode(fields, aliases)

    def export(self) -> dict[str, Any]:
        return {"root_component_id": self.root_component_id, **{
            table: [copy.deepcopy(rows[key]) for key in sorted(rows)] for table, rows in self.tables.items()
        }}


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
