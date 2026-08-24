from pathlib import Path


TEMPLATE_DATABASE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "arc-template"
    / "templates"
    / "web-react-express"
    / "backend"
    / "src"
    / "database"
)


def test_database_initializer_returns_database_after_cached_initialization():
    source = (TEMPLATE_DATABASE / "init_db.js").read_text(encoding="utf-8")

    assert "if (initPromise) {\n    await initPromise;\n    return database;\n  }" in source
    assert "if (initPromise) {\n    return initPromise;\n  }" not in source


def test_database_harness_uses_its_explicit_path_for_setup_and_reset():
    source = (TEMPLATE_DATABASE / "test_harness.js").read_text(encoding="utf-8")

    assert source.count("await resetDatabaseFile(dbPath);") >= 2
    assert source.count("await initializeDatabase({ dbPath });") >= 2
