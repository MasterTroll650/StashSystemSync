import copy
import uuid
import zipfile

import pytest

from workflow.config import Config
from workflow.identity_migration import migrate
from workflow.state import State


class FakeStash:
    def __init__(self):
        self.settings = {
            "StashWorkflow": Config(
                {"dry_run": False, "jellyfin_username": "TestUser"}
            ).as_settings()
        }
        self.flags = {"StashWorkflow": True, "StashSystemSync": True}
        self.fail_transfer = False

    def configuration(self):
        return {"plugins": copy.deepcopy(self.settings)}

    def gql(self, query, variables=None):
        if query.startswith("query"):
            return {"plugins": [{"id": key, "enabled": value} for key, value in self.flags.items()]}
        if "setPluginsEnabled" in query:
            self.flags.update(variables["map"])
            return {"setPluginsEnabled": True}
        if self.fail_transfer and variables["settings"]:
            self.fail_transfer = False
            raise RuntimeError("API unavailable")
        self.settings["StashSystemSync"] = variables["settings"]
        return {"configurePlugin": variables["settings"]}


@pytest.fixture
def installs(tmp_path):
    tmp_path = tmp_path.parent / ("m" + uuid.uuid4().hex[:6])
    old = State(tmp_path / "plugins" / "StashWorkflow" / "data")
    old.put("migration", "performer_imported", True)
    old.put("performer_mapping", "7", "jellyfin-person")
    old.put("whisparr_mapping", "1", 99)
    old.put("worker", "scheduled_until", 9999999999)
    new = State(tmp_path / "plugins" / "my-source" / "StashSystemSync" / "data")
    yield old, new, FakeStash()
    old.close()
    new.close()


def test_migration_copies_settings_sqlite_and_disables_old_plugin(installs):
    old, new, stash = installs
    old.enqueue("scene", "1")
    result = migrate(stash, new)
    assert result["migrated"]
    assert stash.settings["StashSystemSync"] == stash.settings["StashWorkflow"]
    assert not Config(stash.settings["StashSystemSync"]).dry()
    assert not stash.flags["StashWorkflow"] and stash.flags["StashSystemSync"]
    assert new.get("performer_mapping", "7") == "jellyfin-person"
    assert new.get("whisparr_mapping", "1") == 99
    assert new.pending() == 1
    assert new.get("worker", "scheduled_until") is None
    assert old.get("performer_mapping", "7") == "jellyfin-person"
    with zipfile.ZipFile(result["backup"]) as backup:
        assert backup.testzip() is None
        assert "old-workflow.sqlite" in backup.namelist()


def test_failed_settings_transfer_rolls_back_database_and_enable_state(installs):
    old, new, stash = installs
    new.put("scene_title", "123", "Dry run preview")
    stash.fail_transfer = True
    with pytest.raises(RuntimeError, match="API unavailable"):
        migrate(stash, new)
    assert new.get("scene_title", "123") == "Dry run preview"
    assert new.get("performer_mapping", "7") is None
    assert stash.flags["StashWorkflow"]
    assert stash.settings["StashSystemSync"] == {}


def test_migration_refuses_configured_destination(installs):
    old, new, stash = installs
    stash.settings["StashSystemSync"] = {"configured": True}
    with pytest.raises(ValueError, match="already has settings"):
        migrate(stash, new)
    assert stash.flags["StashWorkflow"]


def test_migration_refuses_existing_destination_mappings(installs):
    old, new, stash = installs
    new.put("performer_mapping", "8", "keep")
    with pytest.raises(ValueError, match="already has mappings"):
        migrate(stash, new)
    assert new.get("performer_mapping", "8") == "keep"


def test_settings_rollback_failure_still_attempts_to_reenable_old_plugin(installs):
    old, new, stash = installs
    original = stash.gql

    def fail_settings(query, variables=None):
        if "configurePlugin" in query:
            raise RuntimeError("API unavailable")
        return original(query, variables)

    stash.gql = fail_settings
    with pytest.raises(RuntimeError, match="rollback needs review"):
        migrate(stash, new)
    assert stash.flags["StashWorkflow"]
    assert new.get("performer_mapping", "7") is None
