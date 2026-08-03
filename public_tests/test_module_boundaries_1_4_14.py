import ast
from pathlib import Path

import decision_store
import file_analysis_repository
import state_repository
import state_schema
import volume_policy


EXTRACTED_MODULES = (
    state_schema,
    state_repository,
    volume_policy,
    file_analysis_repository,
)


def test_decision_store_preserves_the_public_compatibility_facade():
    assert not hasattr(decision_store, "__all__")
    assert decision_store.SCHEMA_VERSION == state_schema.SCHEMA_VERSION == 15
    assert decision_store.connect_state_db is state_repository.connect_state_db
    assert decision_store.transaction is state_repository.transaction
    assert (
        decision_store.coordinate_fields_from_name
        is volume_policy.coordinate_fields_from_name
    )
    assert (
        decision_store.resolve_current_file_analysis
        is file_analysis_repository.resolve_current_file_analysis
    )
    assert decision_store.initialize_state_db.__module__ == "decision_store"
    assert (
        decision_store.sync_contextual_bare_volume_metadata.__module__
        == "decision_store"
    )


def test_extracted_modules_do_not_import_the_compatibility_facade():
    for module in EXTRACTED_MODULES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert "decision_store" not in imported, module.__name__


def test_mutation_state_machine_stays_in_one_module():
    assert decision_store.prepare_actual_run.__module__ == "decision_store"
    assert decision_store.create_operation.__module__ == "decision_store"
    assert decision_store.recover_interrupted_operation.__module__ == "decision_store"
    assert decision_store.doctor_issues.__module__ == "decision_store"


def test_repository_schema_roundtrip_through_the_facade(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        decision_store.validate_schema(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 15
    finally:
        conn.close()
