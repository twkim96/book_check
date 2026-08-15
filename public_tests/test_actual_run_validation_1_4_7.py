from types import SimpleNamespace

import decision_store
import duplicate_auditor


def _approved_run(tmp_path, *, prevalidated=False):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-1.4.7.sqlite3"
        )
        if prevalidated:
            run_id, receipt = decision_store.issue_prevalidated_actual_run_token(
                conn, str(backup), house_dir=house, temp_dir=temp
            )
        else:
            run_id = decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house, temp_dir=temp
            )
            receipt = None
    finally:
        conn.close()
    return state_db, house, temp, backup, run_id, receipt


def test_backup_evidence_receipt_reuses_only_unchanged_file(tmp_path, monkeypatch):
    decision_store._clear_backup_evidence_cache_for_tests()
    calls = []
    original = decision_store.sha256_file

    def tracked(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(decision_store, "sha256_file", tracked)
    state_db, _house, _temp, backup, run_id, _receipt = _approved_run(tmp_path)

    ready, reason = decision_store.verify_state_db_ready(state_db)
    assert (ready, reason) == (True, "ok")
    assert calls == [str(backup)]

    with backup.open("ab") as stream:
        stream.write(b"changed")
    ready, reason = decision_store.verify_state_db_ready(state_db)
    assert ready is False
    assert "SHA-256 mismatch" in reason
    assert calls == [str(backup), str(backup)]


def test_prevalidated_receipt_skips_only_matching_readiness_doctor(
    tmp_path, monkeypatch,
):
    decision_store._clear_backup_evidence_cache_for_tests()
    state_db, house, temp, _backup, run_id, receipt = _approved_run(
        tmp_path, prevalidated=True
    )
    calls = []

    def tracked_doctor(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(decision_store, "doctor_issues", tracked_doctor)

    assert decision_store.verify_state_db_ready(
        state_db, preflight_receipt=receipt
    ) == (True, "ok")
    assert calls == []
    assert decision_store.verify_state_db_ready(state_db) == (True, "ok")
    assert calls == [1]
    try:
        decision_store.prepare_actual_run(
            state_db, house, temp, preflight_receipt=object()
        )
    except RuntimeError as exc:
        assert "invalid preflight validation receipt" in str(exc)
    else:
        raise AssertionError("forged preflight receipt must fail closed")
    assert calls == [1]


def test_receipt_bound_auditor_initialization_uses_structural_validation(
    tmp_path, monkeypatch,
):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    calls = []
    original = decision_store.validate_schema

    def tracked_validate(conn, *, check_integrity=True):
        calls.append(check_integrity)
        return original(conn, check_integrity=check_integrity)

    monkeypatch.setattr(decision_store, "validate_schema", tracked_validate)
    cache = duplicate_auditor.PersistentAuditCache(
        state_db,
        [],
        "pair-config",
        "analysis-config",
        trust_entry_identity=True,
    )
    cache.close()

    assert calls == [False]


def test_auditor_marks_actual_run_before_non_cache_review_mutation(
    tmp_path, monkeypatch,
):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    markers = []
    cache = duplicate_auditor.PersistentAuditCache(
        state_db,
        [],
        "pair-config",
        "analysis-config",
        before_non_cache_mutation=lambda: markers.append("marked"),
    )
    cache.file_ids = {"left": 1, "right": 2}

    def fake_supersede(*_args, **_kwargs):
        assert markers == ["marked"]
        return 1

    monkeypatch.setattr(
        duplicate_auditor, "supersede_open_pair_reviews", fake_supersede
    )
    candidate = SimpleNamespace(
        left=SimpleNamespace(path="left"),
        right=SimpleNamespace(path="right"),
    )
    result = SimpleNamespace(
        classification="lossless_identity_mismatch",
        evidence={},
    )
    cache._store_review_item(candidate, result)
    assert cache.stats["lossless_mismatch_reviews_superseded"] == 1
    cache.close()


def test_standalone_auditor_initialization_keeps_full_integrity_validation(
    tmp_path, monkeypatch,
):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    calls = []
    original = decision_store.validate_schema

    def tracked_validate(conn, *, check_integrity=True):
        calls.append(check_integrity)
        return original(conn, check_integrity=check_integrity)

    monkeypatch.setattr(decision_store, "validate_schema", tracked_validate)
    cache = duplicate_auditor.PersistentAuditCache(
        state_db,
        [],
        "pair-config",
        "analysis-config",
        trust_entry_identity=False,
    )
    cache.close()

    assert calls == [True]


def test_state_integrity_receipt_requires_unchanged_main_and_wal_storage(tmp_path):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    receipt = decision_store.issue_state_integrity_receipt(
        state_db, run_id="run-1"
    )

    assert decision_store.state_integrity_receipt_is_current(
        receipt, state_db, run_id="run-1"
    )
    assert not decision_store.state_integrity_receipt_is_current(
        receipt, state_db, run_id="different-run"
    )

    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('receipt-change', '1')"
        )
        conn.commit()
    finally:
        conn.close()

    assert not decision_store.state_integrity_receipt_is_current(
        receipt, state_db, run_id="run-1"
    )
