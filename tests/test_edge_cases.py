from pathlib import Path

import pytest
import yaml
from conftest import FakeJellyfin, FakeWhisparr

from workflow.config import Config
from workflow.engine import Engine


def test_image_change_hook_invalidates_only_image_related_outputs(setup):
    stash, config, state, root = setup
    jf = FakeJellyfin()
    wh = FakeWhisparr(state)
    engine = Engine(stash, config, state, [root], jf, wh)
    engine.scene("1")
    state.enqueue("scene", "1", image_changed=True)
    engine.drain(wait=False)
    assert len(stash.moves) == 1 and len(wh.calls) == 1
    assert len(stash.downloads) == 2 and len(jf.calls) == 2


def test_multiple_files_keep_distinct_destinations_and_exports(setup):
    stash, config, state, root = setup
    second = root / "incoming" / "part2.mp4"
    second.write_bytes(b"second-file")
    stash.scene["files"].append(
        {**stash.scene["files"][0], "id": "12", "path": str(second), "basename": second.name}
    )
    engine = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state))
    engine.scene("1")
    files = [Path(f["path"]) for f in stash.scene["files"]]
    assert len(set(files)) == 2 and all(p.exists() for p in files)
    assert all((p.parent / "movie.nfo").exists() for p in files)
    engine.scene("1")
    assert len(stash.moves) == 2


def test_sidecars_do_not_overwrite_existing_files(setup):
    stash, config, state, root = setup
    destination = root / "Studio" / "VR_A & B - 2026-11-16"
    destination.mkdir(parents=True)
    sidecar = destination / "VR_A & B - 2026-11-16.funscript"
    sidecar.write_bytes(b"other")
    engine = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state))
    with pytest.raises(FileExistsError):
        engine.scene("1")
    assert sidecar.read_bytes() == b"other" and not stash.moves


def test_turning_on_dry_run_stops_pending_move_recovery(setup, monkeypatch):
    stash, config, state, root = setup
    engine = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state))
    import workflow.files as files

    monkeypatch.setattr(
        files.shutil, "move", lambda *a: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError):
        engine.scene("1")
    config.sections["rename"]["dryRun"] = True
    result = engine.scene("1")
    assert result["dry_run"] and len(stash.moves) == 1 and state.get("move_plan", "1")


def test_fresh_event_revives_failed_job_and_preserves_checkpoint(setup):
    stash, config, state, root = setup
    config.values["max_attempts"] = 1
    jf = FakeJellyfin()
    jf.fail = 1
    engine = Engine(stash, config, state, [root], jf, FakeWhisparr(state))
    state.enqueue("scene", "1")
    engine.drain(wait=False)
    assert not state.pending()
    state.enqueue("scene", "1")
    engine.drain(wait=False)
    assert len(stash.moves) == 1 and len(jf.calls) == 2


def test_private_credentials_are_not_repeated_in_queue_errors():
    config = Config({"jellyfin_api_key": "SECRET+VALUE"})
    error = "Failed http://localhost?api_key=SECRET%2BVALUE&other=x SECRET+VALUE"
    result = config.scrub(error)
    assert "SECRET" not in result and "[redacted]" in result


def test_manifest_declares_all_hooks_and_tasks():
    manifest = yaml.safe_load(Path("StashSystemSync.yml").read_text(encoding="utf-8"))
    assert set(manifest["hooks"][0]["triggeredBy"]) == {
        "Scene.Update.Post",
        "Scene.Destroy.Post",
        "Performer.Create.Post",
        "Performer.Update.Post",
    }
    assert len(manifest["tasks"]) == 9
    assert any(t["defaultArgs"].get("mode") == "progress" for t in manifest["tasks"])
    from workflow.settings import SETTING_KEYS

    expected_fields = {
        "rename_defaultDirectoryPathFormat",
        "rename_tagReplace",
        "export_ORGANIZED",
        "performer_update_image",
        "whisparr_MONITORED",
        "cleanup_DELETE_PATH",
    }
    assert {SETTING_KEYS[key] for key in expected_fields} <= set(manifest["settings"])


def test_optional_wait_for_organized_does_not_move_intermediate_scene(setup):
    stash, config, state, root = setup
    config.values["wait_for_organized"] = True
    stash.scene["organized"] = False
    jf = FakeJellyfin()
    wh = FakeWhisparr(state)
    engine = Engine(stash, config, state, [root], jf, wh)
    assert "deferred" in engine.scene("1")
    assert not stash.moves and not jf.calls and not wh.calls
    stash.scene["organized"] = True
    engine.scene("1")
    assert len(stash.moves) == 1 and len(jf.calls) == 1


def test_api_performer_sync_skips_unchanged_and_has_no_file_export(setup):
    stash, config, state, root = setup
    config.values["performer_enabled"] = True

    class Person:
        def __init__(self):
            self.calls = 0
            self.person = {"id": "7", "name": "mIxEd Name", "image_path": "/person/image"}

        def read(self, _):
            return dict(self.person)

        def sync(self, _):
            self.calls += 1

    person = Person()
    engine = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state), person)
    engine.person("7")
    engine.person("7")
    target = root.parent / "people" / "M" / "mIxEd Name" / "folder.jpg"
    assert not target.exists() and person.calls == 1 and len(stash.downloads) == 0
    person.person["details"] = "Changed biography"
    engine.person("7")
    assert person.calls == 2 and len(stash.downloads) == 0
    state.enqueue("performer", "7", image_changed=True)
    engine.drain(wait=False)
    assert person.calls == 3 and len(stash.downloads) == 0
