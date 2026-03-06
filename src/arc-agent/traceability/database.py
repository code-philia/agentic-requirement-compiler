import sqlite3
import json
import os

# Ensure database.db is stored under the traceability directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')

def init_db():
    """Initialize the database and create the requirements table (more tables such as Interface/Test can be added in the future)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create the Requirements table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS requirements (
        req_id TEXT PRIMARY KEY,
        description TEXT,
        visual_reference TEXT,
        parent_id TEXT,
        children_ids TEXT,
        dependencies TEXT
    )
    ''')
    
    conn.commit()
    conn.close()
    
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
