from __future__ import annotations

import subprocess
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import CompilerArtifactStore
from .backend_lowering import render_database_client
from .file_planning import GlobalFilePlanner
from .fixture_lowering import FixtureLowerer, fixture_source_paths
from .skeleton_lowering import DatabaseSchemaLowerer, TypeLowerer
from .symbol_planning import GlobalSymbolPlanner
from .process_utils import resolve_executable


@dataclass(slots=True)
class DatabaseLoweringResult:
    manifest: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def lower_database(
    output_root: Path,
    store: CompilerArtifactStore,
    schema: dict[str, Any],
    fixtures: dict[str, Any],
    project_manifest: dict[str, Any],
) -> DatabaseLoweringResult:
    """Lower the schema without depending on the later backend module design."""

    result = DatabaseLoweringResult()
    database_only_design = {"requirements": [], "modules": []}
    symbols = GlobalSymbolPlanner().plan(database_only_design, schema, project_manifest)
    if not symbols.ok:
        result.errors.extend(symbols.errors)
        return result
    files = GlobalFilePlanner(output_root).plan(
        database_only_design, symbols.registry, project_manifest,
        fixture_paths=fixture_source_paths(fixtures),
    )
    if not files.ok:
        result.errors.extend(files.errors)
        return result
    types = TypeLowerer().lower(symbols.registry, files.registry)
    if not types.ok:
        result.errors.extend(types.errors)
        return result
    database = DatabaseSchemaLowerer().lower(schema, symbols.registry, files.registry)
    result.warnings.extend(database.warnings)
    if not database.ok:
        result.errors.extend(database.errors)
        return result
    fixture = FixtureLowerer().lower(fixtures, database.manifest)
    if not fixture.ok:
        result.errors.extend(fixture.errors)
        return result

    sources = {**types.sources, **database.sources, **fixture.sources}
    sources["backend/src/db/client.ts"] = render_database_client(
        database.manifest["initialization"]["statements"]
    )
    schema_paths = sorted(path for path in database.sources | fixture.sources if path.startswith("backend/src/"))
    database.manifest["generated_files"] = schema_paths
    result.manifest = database.manifest
    result.artifacts.update(store.write_generated_sources(sources))
    node = resolve_executable("node", os.environ)
    if node is None:
        result.errors.append("DATABASE_LOWERING_FAILED: Required command is unavailable: node")
        return result
    try:
        seeded = subprocess.run(
            [node, "init-db.mjs"], cwd=output_root / "backend",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.errors.append(f"DATABASE_LOWERING_FAILED: {exc}")
    else:
        if seeded.returncode != 0:
            result.errors.append(f"DATABASE_LOWERING_FAILED: {seeded.stderr or seeded.stdout}")
    return result
