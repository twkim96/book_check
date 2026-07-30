import decision_store


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
