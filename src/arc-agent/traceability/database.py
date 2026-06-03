import sqlite3
import json
import os

# Ensure database.db is stored under the traceability directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')

def set_db_path(path: str):
    """Set the database path dynamically."""
    global DB_PATH
    DB_PATH = path
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_db():
    """Initialize the database and create tables for Requirements, Interfaces, and Tests. 
    Drops existing tables to ensure a clean state."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # open foreign key support
    cursor.execute('PRAGMA foreign_keys = ON;')
    
    # Drop existing tables if they exist to clear old data
    cursor.execute('DROP TABLE IF EXISTS node_states')
    cursor.execute('DROP TABLE IF EXISTS implementations')
    cursor.execute('DROP TABLE IF EXISTS call_edges')
    cursor.execute('DROP TABLE IF EXISTS tests')
    cursor.execute('DROP TABLE IF EXISTS interfaces')
    cursor.execute('DROP TABLE IF EXISTS requirements')
    
    # 1. Create the Requirements table
    cursor.execute('''
    CREATE TABLE requirements (
        req_id TEXT PRIMARY KEY,
        description TEXT,
        visual_reference TEXT,
        scenario TEXT,
        parent_id TEXT,
        children_ids TEXT,
        dependencies TEXT
    )
    ''')
    
    # 2. Create the Interfaces table
    cursor.execute('''
    CREATE TABLE interfaces (
        interface_id TEXT PRIMARY KEY,
        req_ids TEXT,        -- JSON list of req_ids this interface belongs to
        type TEXT,           -- UI, API, FUNC, DB
        content TEXT,
        file_path TEXT,
        first_line TEXT,
        implemented INTEGER, -- 0 for False, 1 for True
        callers TEXT,        -- JSON list of interface_ids
        callees TEXT,        -- JSON list of interface_ids
        FOREIGN KEY(req_ids) REFERENCES requirements(req_id)
    )
    ''')

    # 3. Create the Tests table
    cursor.execute('''
    CREATE TABLE tests (
        test_id TEXT PRIMARY KEY,
        req_id TEXT,
        interface_ids TEXT,  -- JSON list of associated interface_ids
        type TEXT,           -- Unit, Integration, E2E
        file_path TEXT,
        first_line TEXT,
        FOREIGN KEY(req_id) REFERENCES requirements(req_id)
    )
    ''')

    # 4. Create the Call Edges table (explicit parent-child interface call graph)
    cursor.execute('''
    CREATE TABLE call_edges (
        source_req_id TEXT,
        target_req_id TEXT,
        from_interface_id TEXT,
        to_interface_id TEXT,
        edge_type TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_req_id, target_req_id, from_interface_id, to_interface_id)
    )
    ''')

    # 5. Create the Implementations table (node-level implementation trace)
    cursor.execute('''
    CREATE TABLE implementations (
        req_id TEXT PRIMARY KEY,
        status TEXT,
        attempts INTEGER,
        artifact_paths TEXT,
        summary TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(req_id) REFERENCES requirements(req_id)
    )
    ''')

    # 6. Create node state table (state machine snapshots)
    cursor.execute('''
    CREATE TABLE node_states (
        req_id TEXT PRIMARY KEY,
        state TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(req_id) REFERENCES requirements(req_id)
    )
    ''')
    
    conn.commit()
    conn.close()

def get_requirement_by_id(req_id: str):
    """Retrieve a single requirement record by its ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM requirements WHERE req_id = ?', (req_id,))
    row = cursor.fetchone()
    
    conn.close()
    
    if row:
        data = dict(row)
        # Parse JSON fields back to lists
        try:
            data['visual_reference'] = json.loads(data['visual_reference']) if data['visual_reference'] else []
            data['scenario'] = json.loads(data['scenario']) if data.get('scenario') else []
            data['children_ids'] = json.loads(data['children_ids']) if data['children_ids'] else []
            data['dependencies'] = json.loads(data['dependencies']) if data['dependencies'] else []
        except json.JSONDecodeError:
            pass
        return data
    return None

def get_all_requirements():
    """Retrieve all requirements."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM requirements')
    rows = cursor.fetchall()
    conn.close()

    requirements = []
    for row in rows:
        data = dict(row)
        try:
            data['visual_reference'] = json.loads(data['visual_reference']) if data.get('visual_reference') else []
            data['scenario'] = json.loads(data['scenario']) if data.get('scenario') else []
            data['children_ids'] = json.loads(data['children_ids']) if data.get('children_ids') else []
            data['dependencies'] = json.loads(data['dependencies']) if data.get('dependencies') else []
        except json.JSONDecodeError:
            pass
        requirements.append(data)
    return requirements


"""
Requirement Record
"""
def insert_requirement(req_id: str, description: str, visual_reference: list, 
                       scenario: list, parent_id: str, children_ids: list, dependencies: list):
    """Insert or update a single requirement record in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT OR REPLACE INTO requirements 
    (req_id, description, visual_reference, scenario, parent_id, children_ids, dependencies)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        req_id, 
        description or "", 
        json.dumps(visual_reference) if visual_reference else '[]',
        json.dumps(scenario) if scenario else '[]',
        parent_id or "",
        json.dumps(children_ids) if children_ids else '[]',
        json.dumps(dependencies) if dependencies else '[]'
    ))
    
    conn.commit()
    conn.close()

def update_requirement_visuals(req_id: str, visual_reference: list):
    """Update only the visual_reference field of a requirement."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    UPDATE requirements 
    SET visual_reference = ?
    WHERE req_id = ?
    ''', (
        json.dumps(visual_reference) if visual_reference else '[]',
        req_id
    ))
    
    conn.commit()
    conn.close()


"""
Interface Record
"""
def insert_interface(interface_id: str, req_ids: list, type: str, content: str, 
                     file_path: str, first_line: str, implemented: bool, 
                     callers: list, callees: list):
    """Insert or update an interface record in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT OR REPLACE INTO interfaces 
    (interface_id, req_ids, type, content, file_path, first_line, implemented, callers, callees)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        interface_id,
        json.dumps(req_ids) if req_ids else '[]',
        type,
        content,
        file_path,
        first_line,
        1 if implemented else 0,
        json.dumps(callers) if callers else '[]',
        json.dumps(callees) if callees else '[]'
    ))
    
    conn.commit()
    conn.close()

def insert_test(test_id: str, req_id: str, interface_ids: list, type: str, 
                file_path: str, first_line: str):
    """Insert or update a test record in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT OR REPLACE INTO tests 
    (test_id, req_id, interface_ids, type, file_path, first_line)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        test_id,
        req_id,
        json.dumps(interface_ids) if interface_ids else '[]',
        type,
        file_path,
        first_line
    ))
    
    conn.commit()
    conn.close()

def update_test_implemented_status(test_ids: list, implemented: bool = True):
    """Update implemented status for the specific interfaces associated with the given tests.
    It looks up the interface_ids for each test_id, and sets those interfaces as implemented.
    """
    if not test_ids:
        return
        
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    placeholders = ','.join(['?'] * len(test_ids))
    cursor.execute(f'SELECT interface_ids FROM tests WHERE test_id IN ({placeholders})', test_ids)
    rows = cursor.fetchall()
    
    target_interface_ids = set()
    for row in rows:
        try:
            i_ids = json.loads(row['interface_ids'])
            if isinstance(i_ids, list):
                target_interface_ids.update(i_ids)
        except:
            pass
            
    if target_interface_ids:
        i_placeholders = ','.join(['?'] * len(target_interface_ids))
        cursor.execute(f'''
        UPDATE interfaces 
        SET implemented = ?
        WHERE interface_id IN ({i_placeholders})
        ''', [1 if implemented else 0] + list(target_interface_ids))
        
    conn.commit()
    conn.close()

def update_interface_implemented_status(req_id: str, implemented: bool = True):
    """Update implemented status for all interfaces associated with a requirement."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    search_term = f'%"{req_id}"%'
    cursor.execute('''
    UPDATE interfaces
    SET implemented = ?
    WHERE req_ids LIKE ?
    ''', (1 if implemented else 0, search_term))

    conn.commit()
    conn.close()

def update_interface_implemented(interface_id: str, implemented: bool = True):
    """Update implemented status for a single interface by its ID."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    UPDATE interfaces
    SET implemented = ?
    WHERE interface_id = ?
    ''', (1 if implemented else 0, interface_id))
    conn.commit()
    conn.close()

def update_interface_file_info(interface_id: str, file_path: str, first_line: str):
    """Update file path and first line information for an existing interface."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    UPDATE interfaces 
    SET file_path = ?, first_line = ?
    WHERE interface_id = ?
    ''', (file_path, first_line, interface_id))
    
    conn.commit()
    conn.close()

def get_interfaces_by_req_id(req_id: str):
    """Retrieve all interfaces associated with a specific requirement ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Since req_ids is a JSON list, we use LIKE for simple searching
    search_term = f'%"{req_id}"%'
    cursor.execute('SELECT * FROM interfaces WHERE req_ids LIKE ?', (search_term,))
    rows = cursor.fetchall()
    
    conn.close()
    
    interfaces = []
    for row in rows:
        data = dict(row)
        try:
            data['req_ids'] = json.loads(data['req_ids']) if data['req_ids'] else []
            data['callers'] = json.loads(data['callers']) if data['callers'] else []
            data['callees'] = json.loads(data['callees']) if data['callees'] else []
        except json.JSONDecodeError:
            pass
        interfaces.append(data)
    return interfaces

def get_all_interfaces():
    """Retrieve all interfaces."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM interfaces')
    rows = cursor.fetchall()
    conn.close()

    interfaces = []
    for row in rows:
        data = dict(row)
        try:
            data['req_ids'] = json.loads(data['req_ids']) if data.get('req_ids') else []
            data['callers'] = json.loads(data['callers']) if data.get('callers') else []
            data['callees'] = json.loads(data['callees']) if data.get('callees') else []
        except json.JSONDecodeError:
            pass
        interfaces.append(data)
    return interfaces

def get_interface_by_id(interface_id: str):
    """Retrieve a single interface by its ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM interfaces WHERE interface_id = ?', (interface_id,))
    row = cursor.fetchone()
    
    conn.close()
    
    if row:
        data = dict(row)
        try:
            data['req_ids'] = json.loads(data['req_ids']) if data['req_ids'] else []
            data['callers'] = json.loads(data['callers']) if data['callers'] else []
            data['callees'] = json.loads(data['callees']) if data['callees'] else []
        except json.JSONDecodeError:
            pass
        return data
    return None

def update_interface_req_ids(interface_id: str, new_req_id: str):
    """Add a new req_id to an existing interface's req_ids list (for reuse)."""
    iface = get_interface_by_id(interface_id)
    if not iface:
        return False
        
    req_ids = iface.get('req_ids', [])
    if new_req_id not in req_ids:
        req_ids.append(new_req_id)
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
        UPDATE interfaces 
        SET req_ids = ?
        WHERE interface_id = ?
        ''', (json.dumps(req_ids), interface_id))
        conn.commit()
        conn.close()
        return True
    return False

def get_tests_by_req_id(req_id: str):
    """Retrieve all tests associated with a specific requirement ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM tests WHERE req_id = ?', (req_id,))
    rows = cursor.fetchall()
    
    conn.close()
    
    tests = []
    for row in rows:
        data = dict(row)
        try:
            data['interface_ids'] = json.loads(data['interface_ids']) if data['interface_ids'] else []
        except json.JSONDecodeError:
            pass
        tests.append(data)
    return tests

def get_all_tests():
    """Retrieve all tests."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM tests')
    rows = cursor.fetchall()
    conn.close()

    tests = []
    for row in rows:
        data = dict(row)
        try:
            data['interface_ids'] = json.loads(data['interface_ids']) if data.get('interface_ids') else []
        except json.JSONDecodeError:
            pass
        tests.append(data)
    return tests


"""
Call Edge Record
"""
def insert_call_edge(source_req_id: str, target_req_id: str, from_interface_id: str,
                     to_interface_id: str, edge_type: str = "parent_child"):
    """Insert a requirement/interface-level call edge. Duplicate edges are ignored."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT OR IGNORE INTO call_edges
    (source_req_id, target_req_id, from_interface_id, to_interface_id, edge_type)
    VALUES (?, ?, ?, ?, ?)
    ''', (source_req_id, target_req_id, from_interface_id, to_interface_id, edge_type))
    conn.commit()
    conn.close()


def get_call_edges_by_req_id(req_id: str):
    """Retrieve all call edges where req_id is either source or target."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
    SELECT * FROM call_edges
    WHERE source_req_id = ? OR target_req_id = ?
    ORDER BY source_req_id, target_req_id, from_interface_id, to_interface_id
    ''', (req_id, req_id))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_call_edges():
    """Retrieve all call edges."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM call_edges ORDER BY source_req_id, target_req_id, from_interface_id, to_interface_id')
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


"""
Implementation Record
"""
def upsert_implementation(req_id: str, status: str, attempts: int = 1,
                          artifact_paths: list | None = None, summary: str = ""):
    """Insert or update one implementation trace record."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO implementations (req_id, status, attempts, artifact_paths, summary, updated_at)
    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(req_id) DO UPDATE SET
        status=excluded.status,
        attempts=excluded.attempts,
        artifact_paths=excluded.artifact_paths,
        summary=excluded.summary,
        updated_at=CURRENT_TIMESTAMP
    ''', (
        req_id,
        status,
        attempts,
        json.dumps(artifact_paths) if artifact_paths else '[]',
        summary or ""
    ))
    conn.commit()
    conn.close()


def get_implementation_by_req_id(req_id: str):
    """Retrieve implementation trace record for one requirement."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM implementations WHERE req_id = ?', (req_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    try:
        data['artifact_paths'] = json.loads(data['artifact_paths']) if data.get('artifact_paths') else []
    except json.JSONDecodeError:
        data['artifact_paths'] = []
    return data


def get_all_implementations():
    """Retrieve all implementation trace records."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM implementations ORDER BY req_id')
    rows = cursor.fetchall()
    conn.close()

    implementations = []
    for row in rows:
        data = dict(row)
        try:
            data['artifact_paths'] = json.loads(data['artifact_paths']) if data.get('artifact_paths') else []
        except json.JSONDecodeError:
            data['artifact_paths'] = []
        implementations.append(data)
    return implementations


"""
Node State Record
"""
def upsert_node_state(req_id: str, state: str):
    """Insert or update current node state."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO node_states (req_id, state, updated_at)
    VALUES (?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(req_id) DO UPDATE SET
        state=excluded.state,
        updated_at=CURRENT_TIMESTAMP
    ''', (req_id, state))
    conn.commit()
    conn.close()


def get_node_state(req_id: str):
    """Retrieve node state for one requirement."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM node_states WHERE req_id = ?', (req_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_node_states():
    """Retrieve node states for all requirements."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM node_states ORDER BY req_id')
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
