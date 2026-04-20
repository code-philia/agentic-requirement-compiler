import json
from .database import (
    insert_requirement,
    get_requirement_by_id,
    get_interfaces_by_req_id,
    get_tests_by_req_id,
    get_all_requirements,
    get_all_interfaces,
    get_all_tests,
)

def get_requirement(req_id: str):
    """
    Retrieve requirement data by ID from the database.
    """
    return get_requirement_by_id(req_id)

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
    """
    Retrieve requirements/interfaces/tests with optional req filter and keyword fuzzy search.
    """
    if req_id:
        requirements = [get_requirement_by_id(req_id)] if get_requirement_by_id(req_id) else []
        interfaces = get_interfaces_by_req_id(req_id)
        tests = get_tests_by_req_id(req_id)
    else:
        requirements = get_all_requirements()
        interfaces = get_all_interfaces()
        tests = get_all_tests()

    if keyword:
        requirements = [r for r in requirements if r and _contains_keyword(r, keyword)]
        interfaces = [i for i in interfaces if _contains_keyword(i, keyword)]
        tests = [t for t in tests if _contains_keyword(t, keyword)]
    
    return {
        "requirements": requirements,
        "interfaces": interfaces,
        "tests": tests,
        "filters": {
            "req_id": req_id or "",
            "keyword": keyword or ""
        }
    }

def store_all_requirement(node: dict, parent_id: str = ""):
    """
    Recursively traverse the requirement DAG/tree and store all nodes into SQLite.
    """
    if not isinstance(node, dict):
        return

    # Extract current node data
    req_id = node.get('id', 'UNKNOWN_ID')
    description = node.get('description', '')
    visual_reference = node.get('visual_reference', [])
    scenario = node.get('scenario', [])
    dependencies = node.get('dependencies', [])
    
    # Extract children list
    children = node.get('children', [])
    # Collect all child IDs
    children_ids = [
        child.get('id') for child in children 
        if isinstance(child, dict) and 'id' in child
    ]
    
    # Store current node
    # Even the ROOT node can be stored as the starting point of the whole tree
    insert_requirement(
        req_id=req_id,
        description=description,
        visual_reference=visual_reference,
        scenario=scenario,
        parent_id=parent_id,
        children_ids=children_ids,
        dependencies=dependencies
    )
    
    # Recursively process all children, using the current node ID as their parent_id
    for child in children:
        store_all_requirement(child, parent_id=req_id)
