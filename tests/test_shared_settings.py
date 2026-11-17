from pathlib import Path

import pytest
import yaml

from workflow.config import Config
from workflow.services import Jellyfin
from workflow.settings import SETTING_KEYS
from workflow.settings_migration import migrate_settings


def test_shared_credentials_feed_both_services_and_are_redacted():
    settings = {
        SETTING_KEYS["jellyfin_url"]: "http://localhost/jf",
        SETTING_KEYS["jellyfin_api_key"]: "shared-secret",
        SETTING_KEYS["jellyfin_username"]: "TestUser",
    }
    config = Config(settings)
    assert Jellyfin(config).credentials()[0] == "http://localhost/jf"
    assert (
        config.sections["export"]["JELLYFIN_API_KEY"]
        == config.sections["performer"]["jellyfin_api_key"]
        == "shared-secret"
    )
    assert (
        config.sections["export"]["JELLYFIN_USER"]
        == config.sections["performer"]["jellyfin_user_id"]
        == "TestUser"
    )
    assert "shared-secret" not in config.scrub("shared-secret")
    assert sum(key.endswith("jellyfin_api_key") for key in config.as_settings()) == 1
    assert Config(config.as_settings()).as_settings() == config.as_settings()


def test_setting_order_and_groups_work_with_stash_alphabetical_sort():
    schema = yaml.safe_load(Path("StashSystemSync.yml").read_text())["settings"]
    assert list(schema) == sorted(schema)
    assert list(schema) == list(Config().as_settings())
    groups = [spec["displayName"].split(" | ")[0] for spec in schema.values()]
    assert list(dict.fromkeys(groups)) == [
        "General",
        "Rename",
        "Jellyfin connection",
        "Jellyfin export",
        "Performer sync",
        "Whisparr bridge",
        "Folder cleanup",
    ]
    assert all("description" in spec for spec in schema.values())
    assert not any(key.startswith(("export_JELLYFIN", "performer_jellyfin")) for key in schema)


def test_old_settings_migrate_without_changing_operating_modes():
    old = {
        "dry_run": False,
        "wait_for_organized": True,
        "rename_dryRun": True,
        "performer_update_image": False,
        "export_JELLYFIN_URL": "http://localhost/",
        "performer_jellyfin_url": "http://localhost",
        "export_JELLYFIN_API_KEY": "secret",
        "performer_jellyfin_api_key": "secret",
        "export_JELLYFIN_USER": "a" * 32,
        "performer_jellyfin_user_id": "a" * 32,
        "export_STASH_API_KEY": "unused",
    }
    migrated = migrate_settings(old, {"jellyfin_username": "TestUser"})
    config = Config(migrated)
    assert config.dry() is False and config.dry("rename") is True
    assert config.values["wait_for_organized"] is True
    assert config.sections["performer"]["update_image"] is False
    assert config.values["jellyfin_api_key"] == "secret"
    assert len(migrated) == 44
    assert migrate_settings(migrated) == migrated


@pytest.mark.parametrize(
    "field,old_keys",
    [
        ("jellyfin_url", ["export_JELLYFIN_URL", "performer_jellyfin_url"]),
        ("jellyfin_api_key", ["export_JELLYFIN_API_KEY", "performer_jellyfin_api_key"]),
        ("jellyfin_username", ["export_JELLYFIN_USER", "performer_jellyfin_user_id"]),
    ],
)
def test_conflicting_credentials_require_explicit_review_without_leaking_values(field, old_keys):
    old = dict(zip(old_keys, ["old-secret", "different-secret"], strict=True))
    with pytest.raises(ValueError) as error:
        migrate_settings(old)
    assert (
        field in str(error.value)
        and "old-secret" not in str(error.value)
        and "different-secret" not in str(error.value)
    )
    assert Config(migrate_settings(old, {field: "reviewed"})).values[field] == "reviewed"


def test_unknown_settings_are_not_silently_dropped():
    with pytest.raises(ValueError, match="requires review"):
        migrate_settings({"unknown_setting": True})
