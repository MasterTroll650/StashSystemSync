import pytest
from conftest import FakeJellyfin, FakeWhisparr

from workflow.engine import Engine
from workflow.legacy import performer
from workflow.services import Jellyfin


@pytest.mark.parametrize(
    "item, origin, expected",
    [
        (
            {"Overview": "https://theporndb.net/performers/7c832f9d-38f6-4f7f-9732-123456789abc"},
            "http://localhost:9999",
            "",
        ),
        ({"Overview": "https://theporndb.net/performers/1833"}, "http://localhost:9999", ""),
        ({"Overview": "/performers/7c832f9d-38f6-4f7f-9732-123456789abc"}, "", ""),
        ({"ProviderIds": {"Stash": "7c832f9d-38f6-4f7f-9732-123456789abc"}}, "", ""),
        ({"ProviderIds": {"Stash": "1833"}}, "", "1833"),
        ({"ProviderIds": {"StashPerformerId": "42"}}, "", "42"),
        ({"Tags": ["stash:1833"]}, "", "1833"),
        ({"Tags": ["stash:1833-uuid"]}, "", ""),
        ({"Tags": ["stash:1833abc"]}, "", ""),
        ({"Overview": "Stash performer ID: 1833"}, "", "1833"),
        ({"Overview": "Stash ID: 7c832f9d-38f6-4f7f-9732-123456789abc"}, "", ""),
        ({"Overview": '<a href="/performers/1833?sort=name">Stash</a>'}, "", "1833"),
        ({"Overview": "http://localhost:9999/performers/1833"}, "http://localhost:9999", "1833"),
        ({"Overview": "http://localhost:9998/performers/1833"}, "http://localhost:9999", ""),
        (
            {
                "OriginalTitle": "https://theporndb.net/performers/7c832f9d-38f6-4f7f-9732-123456789abc /performers/1833"
            },
            "",
            "1833",
        ),
    ],
)
def test_only_actual_local_stash_ids_are_recognized(item, origin, expected):
    assert performer.jf_extract_stash_id_from_item(item, origin) == expected


def test_real_mismatching_local_marker_still_rejects_person(monkeypatch):
    monkeypatch.setattr(performer, "jf_get_item_dto", lambda *a, **k: {"Tags": ["stash:42"]})
    assert performer.jf_person_id_verified("http://localhost", "key", "person", "1833", 20) == (
        False,
        "42",
    )


class RecordingHTTP:
    def __init__(self):
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_jellyfin_notification_translates_only_complete_longest_prefix(setup):
    stash, config, state, root = setup
    config.values["jellyfin_path_mappings"] = (
        "/downloads=/other\n/downloads/media/whisparr/=/downloads/"
    )
    config.sections["export"].update(JELLYFIN_URL="http://localhost", JELLYFIN_API_KEY="key")
    http = RecordingHTTP()
    service = Jellyfin(config, http)
    source = "/downloads/media/whisparr/scenes/Studio/Special & Ü/video.mp4"
    service.notify([source, source])
    assert http.calls[0][1]["json"] == {
        "Updates": [{"Path": "/downloads/scenes/Studio/Special & Ü/video.mp4"}]
    }
    assert (
        service.mapped_path("/downloads/media/whisparr-extra/x.mp4")
        == "/other/media/whisparr-extra/x.mp4"
    )


@pytest.mark.parametrize(
    "mapping, path",
    [
        ("/downloads/media/whisparr=/downloads", "/downloads/media/whisparr-extra/x.mp4"),
        ("relative=/downloads", "/downloads/x.mp4"),
        ("/downloads=relative", "/downloads/x.mp4"),
        ("/downloads", "/downloads/x.mp4"),
    ],
)
def test_unmapped_or_invalid_path_never_sends_notification(setup, mapping, path):
    stash, config, state, root = setup
    config.values["jellyfin_path_mappings"] = mapping
    config.sections["export"].update(JELLYFIN_URL="http://localhost", JELLYFIN_API_KEY="key")
    http = RecordingHTTP()
    with pytest.raises(ValueError, match="mapping"):
        Jellyfin(config, http).notify([path])
    assert not http.calls


def test_mapping_change_invalidates_only_notification_checkpoint(setup):
    stash, config, state, root = setup
    jf = FakeJellyfin()
    pipeline = Engine(stash, config, state, [root], jf, FakeWhisparr(state))
    pipeline.scene("1")
    initial_downloads = len(stash.downloads)
    pipeline.scene("1")
    assert len(jf.calls) == 1
    config.values["jellyfin_path_mappings"] = "/downloads/media/whisparr=/downloads"
    pipeline.scene("1")
    assert len(jf.calls) == 2
    assert len(stash.moves) == 1 and len(stash.downloads) == initial_downloads
    pipeline.scene("1")
    assert len(jf.calls) == 2


def test_dry_run_never_sends_mapped_notification(setup):
    stash, config, state, root = setup
    config.values.update(dry_run=True, jellyfin_path_mappings="/downloads=/other")
    config.sections["export"].update(JELLYFIN_URL="http://localhost", JELLYFIN_API_KEY="key")
    http = RecordingHTTP()
    Jellyfin(config, http).notify(["/downloads/x.mp4"])
    assert not http.calls
