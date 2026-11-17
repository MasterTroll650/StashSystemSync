import copy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import FakeJellyfin, FakeWhisparr

from workflow.engine import Engine


def engine(setup):
    stash, config, state, root = setup
    jf = FakeJellyfin()
    wh = FakeWhisparr(state)
    return Engine(stash, config, state, [root], jf, wh), jf, wh


def test_full_scene_pipeline_and_repeated_updates_are_idempotent(setup):
    stash, config, state, root = setup
    pipeline, jf, wh = engine(setup)
    stash.scene["interactive"] = True
    pipeline.scene("1")
    target = Path(stash.scene["files"][0]["path"])
    assert target == root / "Studio" / "VR_A & B - 2026-11-16" / "VR_A & B - 2026-11-16.mp4"
    assert target.with_suffix(".funscript").read_text() == '{"actions":[]}'
    document = ET.fromstring((target.parent / "movie.nfo").read_bytes())
    assert document.findtext("title") == "A & B"
    assert document.findtext("plot") == "Text <test> & more"
    assert document.findtext("year") == "2026"
    assert document.findtext("runtime") == "2"
    assert document.findtext("tag") == "Virtual Reality"
    stamp = (target.parent / "movie.nfo").stat().st_mtime_ns
    assert len(stash.moves) == 1 and stash.tag_calls == 1
    assert jf.calls == [[str(target)]]
    assert wh.calls[0]["files"][0]["path"] == str(target)
    pipeline.scene("1")
    pipeline.scene("1")
    assert len(stash.moves) == len(jf.calls) == len(wh.calls) == 1
    assert len(stash.downloads) == 1
    assert (target.parent / "movie.nfo").stat().st_mtime_ns == stamp
    assert not (root / "incoming").exists()
    assert root.exists()


def test_metadata_change_only_rewrites_nfo_and_notifies(setup):
    stash, config, state, root = setup
    pipeline, jf, wh = engine(setup)
    pipeline.scene("1")
    stash.scene["details"] = "New plot"
    pipeline.scene("1")
    assert len(stash.moves) == 1 and len(stash.downloads) == 1
    assert len(jf.calls) == 2 and len(wh.calls) == 1


def test_jellyfin_retry_does_not_repeat_move_or_download(setup):
    stash, config, state, root = setup
    pipeline, jf, wh = engine(setup)
    jf.fail = 1
    with pytest.raises(RuntimeError):
        pipeline.scene("1")
    assert state.get("scene:1", "notify") is None
    pipeline.scene("1")
    assert len(stash.moves) == 1 and len(stash.downloads) == 1
    assert len(jf.calls) == 2 and len(wh.calls) == 1


def test_missing_output_is_repaired_even_after_success(setup):
    stash, config, state, root = setup
    pipeline, jf, wh = engine(setup)
    pipeline.scene("1")
    folder = Path(stash.scene["files"][0]["path"]).parent
    (folder / "movie.nfo").unlink()
    (folder / "poster.jpg").unlink()
    pipeline.scene("1")
    assert (folder / "movie.nfo").exists() and (folder / "poster.jpg").exists()
    assert len(stash.moves) == 1 and len(jf.calls) == 2


def test_dry_run_never_mutates_stash_files_or_remote_services(setup):
    stash, config, state, root = setup
    config.values["dry_run"] = True
    pipeline, jf, wh = engine(setup)
    stash.scene["interactive"] = True
    before = copy.deepcopy(stash.scene)
    result = pipeline.scene("1")
    assert result["dry_run"]
    assert stash.scene == before
    assert (
        not stash.moves
        and not jf.calls
        and not wh.calls
        and not stash.downloads
        and not stash.tag_calls
    )
    assert (root / "incoming" / "old.funscript").exists()
    assert not list(root.rglob("movie.nfo"))


def test_unorganized_rules_are_preserved_per_module(setup):
    stash, config, state, root = setup
    stash.scene["organized"] = False
    pipeline, jf, wh = engine(setup)
    pipeline.scene("1")
    # Renamer skips metadata naming; the enabled legacy exporter still gets its basename directory.
    assert Path(stash.scene["files"][0]["path"]) == root / "incoming" / "old" / "old.mp4"


def test_ignored_suffix_prevents_export(setup):
    stash, config, state, root = setup
    config.values["rename_enabled"] = False
    config.sections["export"]["IGNORED_FILE_SUFFIXES"] = "_Transcoded, ($index$)"
    old = Path(stash.scene["files"][0]["path"])
    new = old.with_name("old (2).mp4")
    old.rename(new)
    stash.scene["files"][0].update(path=str(new), basename=new.name)
    pipeline, jf, wh = engine(setup)
    pipeline.scene("1")
    assert not list(root.rglob("movie.nfo")) and not jf.calls and not stash.downloads


def test_pending_move_recovers_sidecars_after_failure(setup, monkeypatch):
    stash, config, state, root = setup
    pipeline, jf, wh = engine(setup)
    import workflow.files as module

    real = module.shutil.move

    def fail(*_args):
        raise OSError("Simulated failure after Stash moved main file")

    monkeypatch.setattr(module.shutil, "move", fail)
    with pytest.raises(OSError):
        pipeline.scene("1")
    assert len(stash.moves) == 1 and state.get("move_plan", "1")
    monkeypatch.setattr(module.shutil, "move", real)
    pipeline.scene("1")
    assert len(stash.moves) == 1
    assert Path(stash.scene["files"][0]["path"]).with_suffix(".funscript").exists()
    assert state.get("move_plan", "1") is None


def test_occupied_target_gets_suffix_without_overwriting(setup):
    stash, config, state, root = setup
    occupied = root / "Studio" / "VR_A & B - 2026-11-16" / "VR_A & B - 2026-11-16.mp4"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"other scene")
    pipeline, jf, wh = engine(setup)
    pipeline.scene("1")
    moved = Path(stash.scene["files"][0]["path"])
    assert moved.parent.name == "VR_A & B - 2026-11-16 (1)"
    assert occupied.read_bytes() == b"other scene"
    pipeline.scene("1")
    assert len(stash.moves) == 1


def test_destination_outside_library_is_rejected(setup):
    stash, config, state, root = setup
    config.sections["rename"]["defaultDirectoryPathFormat"] = str(root.parent / "outside")
    pipeline, jf, wh = engine(setup)
    with pytest.raises(ValueError):
        pipeline.scene("1")
    assert not stash.moves and not jf.calls and not wh.calls


def test_html_image_response_is_not_written_as_poster(setup):
    stash, config, state, root = setup
    stash.bytes = lambda _: b"<html>" + b"not an image" * 200
    pipeline, jf, wh = engine(setup)
    with pytest.raises(RuntimeError, match="not an image"):
        pipeline.scene("1")
    assert not list(root.rglob("poster.jpg")) and not jf.calls


def test_deleted_source_file_stops_export(setup):
    stash, config, state, root = setup
    Path(stash.scene["files"][0]["path"]).unlink()
    pipeline, jf, wh = engine(setup)
    with pytest.raises(FileNotFoundError):
        pipeline.scene("1")
    assert not jf.calls and not wh.calls
