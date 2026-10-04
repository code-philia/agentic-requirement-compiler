"""Whole-requirement database analysis; this agent never writes application files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agents.runtime.contracts import AgentRuntimeContext
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from core.database_plan import DatabasePlan


DATABASE_PROMPT = """You are ARC's global database analysis stage, before node design and TDD.
Read ALL supplied requirements, scenarios, dependencies, and the existing database baseline.
Return only the structured DatabasePlan JSON. Do not write files or generate schema documentation,
SQL, arbitrary code, migrations, APIs, repositories, UI, or tests. The compiler renders and executes
your records deterministically. Use read-only file tools only to inspect exact existing DB paths.

Design the smallest complete shared SQLite data model needed by the entire application:
entities, scalar columns, literal defaults, primary keys, unique constraints, foreign keys,
indexes, and concrete pre-existing rows. Types are INTEGER, REAL, TEXT, BLOB, NUMERIC.
Use stable ASCII SQL identifiers; never use identifiers starting sqlite_ or _arc_.
Composite primary keys and unique constraints are supported. Primary key columns must set nullable=false.
Use explicit stable primary keys
for seeds, including parent/child references. Order seed groups parents before children.
Every table and seed group must identify its source requirement ids.
Every seed group needs a short source explanation and conflict_columns backed by a primary
key or unique constraint; this is how the compiler avoids duplicates without replacing records.
Rows contain scalar JSON values only; secrets requiring hashing must not be seeded as plaintext
into a password-hash field. BLOB columns are for application-written data, not JSON seed values.

Distinguish product bootstrap data from user action inputs and dependency-created prerequisites.
Seed only records the requirements say the application already provides. Do not seed accounts
that scenarios create through registration, sessions created by login, or orders created by a
confirmation action. Dynamic placeholders such as <timestamp> are inputs, not literal seed data.
Screenshots describe style, never seed business data from images. Do not infer hidden fixtures.

For an existing plan, return the COMPLETE cumulative plan, retaining existing tables, columns,
keys, defaults, relationships, indexes, and seeds. Only add new tables, safe nullable columns or
non-null columns with literal defaults, indexes, and seed rows. Do not rename, drop, or redefine
existing structures. Match an existing application's actual tables and naming. A persisted
database's schema takes precedence over guessed names. Do not create parallel domain tables.
If there are no persistence requirements and no prior plan, return empty tables/seeds.
"""


class DatabaseDesigner:
    def __init__(self, *, workspace_root: str, app_type: str, requirement_path: str, log_cb=None):
        self.workspace_root = workspace_root
        self.app_type = app_type
        self.requirement_path = requirement_path
        self.log_cb = log_cb

    async def run(self, requirement_tree: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        agent = build_stage_agent(
            name="database_designer", stage="database_analysis",
            model=os.environ.get("MODEL", "openai:gpt-5.4"),
            system_prompt=DATABASE_PROMPT, response_format=DatabasePlan,
            workspace_root=self.workspace_root, writable_roots=[],
            skills=[], permitted_skill_names=[], memory=[], tools=[],
        )
        return await ainvoke_stage_agent(
            agent,
            message=json.dumps({
                "app_type": self.app_type,
                "requirements": requirement_tree,
                "record_schema": DatabasePlan.model_json_schema(),
                "baseline": baseline,
                "existing_database_paths": [
                    "backend/src/database/init_db.js", "backend/src/database/seed_db.js",
                    "backend/src/database/db_runtime.js", "app/database.py",
                    "app/__main__.py", "app/src/main/AndroidManifest.xml",
                ],
            }, ensure_ascii=False),
            context=AgentRuntimeContext(
                node_id=str(requirement_tree["id"]), phase="DATABASE_PREPARE",
                app_type=self.app_type, workspace_root=str(Path(self.workspace_root).resolve()),
                requirement_path=self.requirement_path,
            ),
            thread_id=f"{requirement_tree['id']}:DATABASE_PREPARE",
            label="DatabaseDesigner", log_cb=self.log_cb,
        )
