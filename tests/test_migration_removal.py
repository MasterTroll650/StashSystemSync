from pathlib import Path

import pytest
import yaml

from plugin import run


def test_old_identity_migration_task_and_module_are_removed():
    manifest = yaml.safe_load(Path("StashSystemSync.yml").read_text())
    assert not any(
        task["defaultArgs"].get("mode") == "migrate_legacy" for task in manifest["tasks"]
    )
    assert not Path("workflow/identity_migration.py").exists()


def test_removed_migration_cannot_modify_existing_data(setup):
    stash, config, state, root = setup
    state.put("performer_mapping", "7", "person")
    state.put("migration", "performer_imported", True)
    state.put("migration", "whisparr_imported", True)
    before = [tuple(row) for row in state.db.execute("SELECT * FROM kv ORDER BY scope,key")]
    with pytest.raises(ValueError, match="Choose a plugin task"):
        run({"args": {"mode": "migrate_legacy"}}, state.directory, stash)
    assert [tuple(row) for row in state.db.execute("SELECT * FROM kv ORDER BY scope,key")] == before
    assert not state.db.execute("SELECT 1 FROM jobs").fetchone()
