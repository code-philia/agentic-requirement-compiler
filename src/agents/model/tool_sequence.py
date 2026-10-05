"""Minimal model-facing tools, translated to the existing validated records."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def tool_contract(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})

    def compact(value: Any) -> Any:
        if isinstance(value, list):
            return [compact(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return compact(definitions[value["$ref"].rsplit("/", 1)[-1]])
        return {key: compact(item) for key, item in value.items()
                if key not in {"title", "description", "default", "$defs"}}

    props = schema.get("properties", {})
    tools: dict[str, Any] = {}

    def add(name: str, parameters: dict[str, Any], required: list[str]) -> None:
        tools[name] = {"parameters": compact(parameters), "required": required}

    string = {"type": "string"}
    design = "files" in props
    layer = {"enum": ["frontend", "API", "FUNC", "DB", "shared"]}
    if "actions" in props:
        add("read_file", {"path": string}, ["path"])
    if "changes" in props:
        add("edit_file", {"path": string, "old_text": string, "new_text": string, **({"layer": layer} if design else {})},
            ["path", "old_text", "new_text"])
        add("add_file", {"path": string, "content": string, **({"layer": layer} if design else {})},
            ["path", "content", "layer"] if design else ["path", "content"])
        add("delete_file", {"path": string}, ["path"])
    if "tests" in props:
        test = compact(props["tests"]["items"])
        add("register_test", test["properties"], test["required"])
    if "identity_usage" in props:
        usage = compact(definitions["IdentityUsage"])
        add("declare_identity_usage", usage["properties"], usage["required"])
    for field, name, parameter in (("read_shared", "read_shared", "name"),
                                   ("read_shared_groups", "read_shared_group", "id")):
        if field in props and not design:
            add(name, {parameter: string}, [parameter])
    if "shared_need" in props and not design:
        add("request_shared", {"name": string, "need": string}, ["name", "need"])
    if "capability" in props:
        cap = compact(definitions["Capability"])
        add("register_shared", cap["properties"], cap["required"])
    if "database_gap" in props:
        add("report_database_gap", {"need": string}, ["need"])
    for field, name in (("entities", "define_entity"), ("facts", "record_persistence_fact"),
                        ("bindings", "bind_entity"), ("tables", "define_table"), ("seeds", "seed_rows")):
        if field in props:
            item = compact(props[field]["items"])
            add(name, item["properties"], item["required"])
    for field, name in (("replace_tables", "replace_table"), ("replace_seed_tables", "replace_seed_rows")):
        if field in props:
            add(name, {"name": string}, ["name"])
    return {"tools": tools}


def parse_tool_sequence(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Reject prose/wrappers and unknown parameters before internal validation."""
    try:
        sequence = json.loads(text.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Return only a JSON array of tool calls, without Markdown or prose") from exc
    if not isinstance(sequence, list):
        raise ValueError("Expected a tool-call array, not an object")
    tools = tool_contract(schema)["tools"]
    props = schema.get("properties", {})
    result: dict[str, Any] = {field: [] for field in ("entities", "facts", "bindings", "tables", "seeds") if field in props}
    if any(not isinstance(call, dict) or not isinstance(call.get("tool"), str) for call in sequence):
        raise ValueError("Every call must be an object with a string tool name")
    names = [call["tool"] for call in sequence]
    reads = {"read_file", "read_shared", "read_shared_group"}
    if any(name in reads for name in names) and any(name not in reads for name in names):
        raise ValueError("Return reading calls alone, without writes or registrations")
    if "request_shared" in names and len(sequence) != 1:
        raise ValueError("request_shared must be returned alone")
    if "report_database_gap" in names and len(sequence) != 1:
        raise ValueError("report_database_gap must be returned alone")
    for call in sequence:
        if not isinstance(call, dict) or call.get("tool") not in tools:
            raise ValueError("Unknown or unavailable tool")
        name = call["tool"]
        spec = tools[name]
        params = {key: value for key, value in call.items() if key != "tool"}
        if set(params) - spec["parameters"].keys() or set(spec["required"]) - params.keys():
            raise ValueError(f"Invalid parameters for {name}; expected {spec['parameters'].keys()}")
        params = deepcopy(params)
        if name in {"read_file", "add_file", "edit_file", "delete_file"}:
            layer = params.pop("layer", None)
            if layer is not None:
                if layer not in {"frontend", "API", "FUNC", "DB", "shared"}:
                    raise ValueError(f"Invalid file layer: {layer}")
                result.setdefault("files", {}).setdefault(layer, []).append(params["path"])
            if name == "delete_file":
                params["reason"] = "Model requested removal of an obsolete in-scope file"
            result.setdefault("actions", []).append({"tool": name, **params})
        elif name == "register_test":
            result.setdefault("tests", []).append(params)
        elif name == "declare_identity_usage":
            if "identity_usage" in result:
                raise ValueError("Declare identity usage exactly once")
            result["identity_usage"] = params
        elif name in {"read_shared", "read_shared_group"}:
            field, parameter = ("read_shared", "name") if name == "read_shared" else ("read_shared_groups", "id")
            result.setdefault(field, []).append(params[parameter])
        elif name in {"request_shared", "register_shared", "report_database_gap"}:
            field = {"request_shared": "shared_need", "register_shared": "capability",
                     "report_database_gap": "database_gap"}[name]
            if field in result:
                raise ValueError(f"Only one {name} call is allowed")
            result[field] = ({"name": params["name"], "reason": params["need"]} if name == "request_shared"
                             else params if name == "register_shared" else params["need"])
        elif name in {"define_entity", "record_persistence_fact", "bind_entity", "define_table", "seed_rows"}:
            result[{"define_entity": "entities", "record_persistence_fact": "facts",
                    "bind_entity": "bindings", "define_table": "tables", "seed_rows": "seeds"}[name]].append(params)
        else:
            field = "replace_tables" if name == "replace_table" else "replace_seed_tables"
            result.setdefault(field, []).append(params["name"])
    return result
