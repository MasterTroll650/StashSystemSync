import json

import pytest

from plugin import run
from workflow.legacy import performer as legacy
from workflow.mappings import PerformerMappings, import_json_mappings, mapping_status
from workflow.state import State


def test_performer_import_is_complete_idempotent_and_survives_json_removal(tmp_path):
    source = tmp_path / "performer-map.json"
    source.write_text(json.dumps({"1": "one", "2": "old"}))
    state = State(tmp_path)
    state.put("performer_mapping", "2", "newer")
    import_json_mappings(state)
    assert dict(PerformerMappings(state)) == {"1": "one", "2": "newer"}
    assert state.get("migration", "performer_imported") is True
    receipt = state.get("migration", "performer_imported_receipt")
    assert receipt["source_count"] == 2 and receipt["inserted"] == 1 and receipt["preserved"] == 1
    del PerformerMappings(state)["1"]
    import_json_mappings(state)
    assert "1" not in PerformerMappings(state)
    source.unlink()
    state.close()
    result = run({"args": {"mode": "mapping_status"}}, tmp_path)
    assert result["performer"]["import_complete"] and result["performer"]["count"] == 1
    assert result["integrity"] == "ok" and not source.exists()


@pytest.mark.parametrize("payload", [[], {"1": "valid", "2": None}, {"0": "invalid"}, {"1": 123}])
def test_invalid_performer_import_leaves_database_and_marker_untouched(tmp_path, payload):
    (tmp_path / "performer-map.json").write_text(json.dumps(payload))
    state = State(tmp_path)
    with pytest.raises(ValueError):
        import_json_mappings(state)
    assert not dict(PerformerMappings(state))
    assert not state.get("migration", "performer_imported")
    state.close()


def test_import_rolls_back_on_mid_transaction_failure(tmp_path, monkeypatch):
    (tmp_path / "performer-map.json").write_text(json.dumps({"1": "one", "2": "two"}))
    state = State(tmp_path)
    original = state.put

    def fail(scope, key, value):
        if scope == "performer_mapping" and key == "2":
            raise RuntimeError("storage error")
        original(scope, key, value)

    monkeypatch.setattr(state, "put", fail)
    with pytest.raises(RuntimeError):
        import_json_mappings(state)
    assert not dict(PerformerMappings(state)) and not state.get("migration", "performer_imported")
    monkeypatch.setattr(state, "put", original)
    import_json_mappings(state)
    assert len(PerformerMappings(state)) == 2
    state.close()


def test_completed_whisparr_import_does_not_restore_deleted_mappings(tmp_path):
    source = tmp_path / "whisparr-cache.import.json"
    source.write_text('{"1":10,"2":20}')
    state = State(tmp_path)
    state.put("migration", "whisparr_imported", True)
    state.put("whisparr_mapping", "2", 200)
    state.put("whisparr_deleted", "1", 10)
    import_json_mappings(state)
    assert state.get("whisparr_mapping", "1") is None and state.get("whisparr_mapping", "2") == 200
    source.unlink()
    assert mapping_status(state)["whisparr"]["import_complete"]
    state.close()


def test_fresh_whisparr_import_preserves_existing_values_and_tombstones(tmp_path):
    (tmp_path / "whisparr-cache.import.json").write_text('{"1":10,"2":20,"3":30}')
    state = State(tmp_path)
    state.put("whisparr_mapping", "2", 200)
    state.put("whisparr_deleted", "1", 10)
    import_json_mappings(state)
    assert state.get("whisparr_mapping", "1") is None
    assert state.get("whisparr_mapping", "2") == 200 and state.get("whisparr_mapping", "3") == 30
    assert state.get("migration", "whisparr_imported")
    state.close()


def test_separate_mapping_instances_do_not_clobber_each_others_changes(tmp_path):
    first = State(tmp_path)
    second = State(tmp_path)
    a, b = PerformerMappings(first), PerformerMappings(second)
    a["1"] = "one"
    b["2"] = "two"
    a["1"] = "updated"
    assert dict(b) == {"1": "updated", "2": "two"}
    del b["1"]
    a["3"] = "three"
    assert dict(a) == {"2": "two", "3": "three"}
    first.close()
    second.close()


def test_mapping_resolution_updates_sqlite_without_json_writes(tmp_path, monkeypatch):
    state = State(tmp_path)
    mapping = PerformerMappings(state)
    mapping["7"] = "stale"
    monkeypatch.setattr(legacy, "jf_person_id_verified", lambda *a, **k: (False, ""))
    monkeypatch.setattr(legacy, "jf_search_people", lambda *a, **k: [])
    monkeypatch.setattr(legacy, "pick_person_id_with_stash_verification", lambda *a, **k: "current")

    def forbidden(*a, **k):
        raise AssertionError("JSON must not be used")

    monkeypatch.setattr(legacy, "save_map", forbidden)
    assert (
        legacy.resolve_person_id(
            "http://localhost", "key", "Name", "7", mapping, 20, save_mapping=lambda values: None
        )
        == "current"
    )
    assert state.get("performer_mapping", "7") == "current"
    assert not list(tmp_path.glob("*.json"))
    state.close()
