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
    cursor.execute('DROP TABLE IF EXISTS tests')
    cursor.execute('DROP TABLE IF EXISTS interfaces')
    cursor.execute('DROP TABLE IF EXISTS requirements')
    
    # 1. Create the Requirements table
    cursor.execute('''
    CREATE TABLE requirements (
        req_id TEXT PRIMARY KEY,
        description TEXT,
        visual_reference TEXT,
        parent_id TEXT,
        children_ids TEXT,
        dependencies TEXT
    )
    ''')
    
    # 2. Create the Interfaces table
    cursor.execute('''
    CREATE TABLE interfaces (
        interface_id TEXT PRIMARY KEY,
        req_id TEXT,
        type TEXT,           -- UI, API, FUNC, DB
        content TEXT,
        file_path TEXT,
        first_line TEXT,
        implemented INTEGER, -- 0 for False, 1 for True
        callers TEXT,        -- JSON list of interface_ids
        callees TEXT,        -- JSON list of interface_ids
        FOREIGN KEY(req_id) REFERENCES requirements(req_id)
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
            data['children_ids'] = json.loads(data['children_ids']) if data['children_ids'] else []
            data['dependencies'] = json.loads(data['dependencies']) if data['dependencies'] else []
        except json.JSONDecodeError:
            pass
        return data
    return None


"""
Requirement Record
"""
def insert_requirement(req_id: str, description: str, visual_reference: list, 
                       parent_id: str, children_ids: list, dependencies: list):
    """Insert or update a single requirement record in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT OR REPLACE INTO requirements 
    (req_id, description, visual_reference, parent_id, children_ids, dependencies)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        req_id, 
        description or "", 
        json.dumps(visual_reference) if visual_reference else '[]',
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
def insert_interface(interface_id: str, req_id: str, type: str, content: str, 
                     file_path: str, first_line: str, implemented: bool, 
                     callers: list, callees: list):
    """Insert or update an interface record in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT OR REPLACE INTO interfaces 
    (interface_id, req_id, type, content, file_path, first_line, implemented, callers, callees)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        interface_id,
        req_id,
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

def update_interface_implemented_status(req_id: str, implemented: bool = True):
    """Update implemented status for all interfaces associated with a requirement."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    UPDATE interfaces 
    SET implemented = ?
    WHERE req_id = ?
    ''', (1 if implemented else 0, req_id))
    
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
    
    cursor.execute('SELECT * FROM interfaces WHERE req_id = ?', (req_id,))
    rows = cursor.fetchall()
    
    conn.close()
    
    interfaces = []
    for row in rows:
        data = dict(row)
        try:
            data['callers'] = json.loads(data['callers']) if data['callers'] else []
            data['callees'] = json.loads(data['callees']) if data['callees'] else []
        except json.JSONDecodeError:
            pass
        interfaces.append(data)
    return interfaces

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
