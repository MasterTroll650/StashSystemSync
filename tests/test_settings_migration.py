from pathlib import Path

import pytest
import yaml

from workflow.config import Config
from workflow.state import State, fingerprint


def test_all_manifest_fields_have_explicit_defaults():
    schema = yaml.safe_load(Path("StashSystemSync.yml").read_text())["settings"]
    settings = Config().as_settings()
    assert len(settings) == 44
    assert set(schema) == set(settings)
    for key, spec in schema.items():
        assert isinstance(
            settings[key], {"STRING": str, "NUMBER": (int, float), "BOOLEAN": bool}[spec["type"]]
        )


def test_legacy_files_and_old_plugin_settings_are_ignored(tmp_path):
    (tmp_path / "legacy-settings.json").write_text("invalid json")
    configuration = {
        "plugins": {
            "stashJellyfinExport": {"JELLYFIN_API_KEY": "old-secret"},
            "StashSystemSync": {"dry_run": False, "jellyfin_api_key": "current-secret"},
        }
    }
    config = Config.load(tmp_path, configuration)
    assert not config.dry()
    assert config.sections["export"]["JELLYFIN_API_KEY"] == "current-secret"
    assert (
        Config.load(
            {"plugins": {"stashJellyfinExport": {"JELLYFIN_API_KEY": "old-secret"}}}
        ).sections["export"]["JELLYFIN_API_KEY"]
        == ""
    )


def test_layout_preserves_hashes_but_ui_edits_invalidate_them(tmp_path):
    config = Config({"export_EXCLUDED_PATHS": "/people\n/excluded"})
    state = State(tmp_path)
    original = {
        "export": {"EXCLUDED_PATHS": ["/people", "/excluded"]},
        "performer": {"update_image": True},
        "whisparr": {"MONITORED": True},
    }
    layouts = {
        section: {
            "effective_sha256": fingerprint(config.sections[section]),
            "keys": list(values),
            "list_keys": [key for key, value in values.items() if isinstance(value, list)],
        }
        for section, values in original.items()
    }
    config.adopt_layouts(layouts, state)
    for section, values in original.items():
        assert fingerprint(config.hash_section(section, state)) == fingerprint(values)
    config.sections["export"]["JELLYFIN_URL"] = "http://changed"
    assert config.hash_section("export", state) == config.sections["export"]
    with pytest.raises(ValueError):
        config.adopt_layouts(layouts, state)
    state.close()


def test_retired_people_path_only_preserves_old_checkpoint_hash(tmp_path):
    config = Config()
    state = State(tmp_path)
    old = {"PEOPLE_DIR": "/old-mounted-people", "ORGANIZED": True}
    layouts = {
        section: {
            "effective_sha256": fingerprint(config.sections[section]),
            "keys": [],
            "list_keys": [],
        }
        for section in ["export", "performer", "whisparr"]
    }
    layouts["export"].update(keys=list(old), retired={"PEOPLE_DIR": old["PEOPLE_DIR"]})
    config.adopt_layouts(layouts, state)
    assert config.hash_section("export", state) == old
    assert "PEOPLE_DIR" not in config.sections["export"]
    assert "export_PEOPLE_DIR" not in config.as_settings()
    config.sections["export"]["ORGANIZED"] = False
    assert config.hash_section("export", state) == config.sections["export"]
    state.close()
