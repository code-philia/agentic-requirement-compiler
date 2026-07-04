import json

from runtime_sdk import get_runtime


def _store():
    return get_runtime().traceability


def get_requirement(req_id: str):
    return _store().get_requirement(str(req_id or "").strip())


def _contains_keyword(item: dict, keyword: str) -> bool:
    if not keyword:
        return True
    kw = keyword.lower()
    try:
        blob = json.dumps(item, ensure_ascii=False).lower()
    except Exception:
        blob = str(item).lower()
    return kw in blob


def get_traceability_data(req_id: str = "", keyword: str = ""):
    store = _store()
    normalized_req_id = str(req_id or "").strip()

    if normalized_req_id:
        requirement = store.get_requirement(normalized_req_id)
        requirements = [requirement] if requirement else []
        interfaces = store.list_interfaces(req_id=normalized_req_id)
        tests = [item for item in store.list_tests(req_id=normalized_req_id) if item is not None]
        call_edges = store.list_call_edges(req_id=normalized_req_id)
        state = store.get_node_state(normalized_req_id)
        node_states = [state] if state else []
    else:
        requirements = store.list_requirements()
        interfaces = store.list_interfaces()
        tests = [item for item in store.list_tests() if item is not None]
        call_edges = store.list_call_edges()
        node_states = store.list_node_states()

    if keyword:
        requirements = [r for r in requirements if r and _contains_keyword(r, keyword)]
        interfaces = [i for i in interfaces if _contains_keyword(i, keyword)]
        tests = [t for t in tests if _contains_keyword(t, keyword)]
        call_edges = [e for e in call_edges if _contains_keyword(e, keyword)]
        node_states = [s for s in node_states if _contains_keyword(s, keyword)]

    return {
        "requirements": requirements,
        "interfaces": interfaces,
        "tests": tests,
        "call_edges": call_edges,
        "node_states": node_states,
        "filters": {
            "req_id": normalized_req_id,
            "keyword": keyword or "",
        },
    }


def store_all_requirement(node: dict, parent_id: str = ""):
    if not isinstance(node, dict):
        return

    req_id = str(node.get("id") or "UNKNOWN_ID").strip()
    if not req_id:
        return

    children = node.get("children", []) or []
    children_ids = [
        str(child.get("id")).strip()
        for child in children
        if isinstance(child, dict) and str(child.get("id", "")).strip()
    ]

    _store().upsert_requirement(
        req_id=req_id,
        name=str(node.get("name") or "").strip(),
        description=str(node.get("description") or "").strip(),
        visual_reference=node.get("visual_reference", []) or [],
        scenarios=node.get("scenarios", []) or [],
        parent_id=str(parent_id or "").strip() or None,
        children_ids=children_ids,
        dependencies=node.get("dependencies", []) or [],
    )

    for child in children:
        store_all_requirement(child, parent_id=req_id)
