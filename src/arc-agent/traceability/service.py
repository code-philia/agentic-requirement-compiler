from .database import insert_requirement

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
        parent_id=parent_id,
        children_ids=children_ids,
        dependencies=dependencies
    )
    
    # Recursively process all children, using the current node ID as their parent_id
    for child in children:
        store_all_requirement(child, parent_id=req_id)
