import copy

import pytest
from conftest import FakeJellyfin, FakeWhisparr

from workflow.engine import Engine
from workflow.legacy import performer as legacy
from workflow.services import Jellyfin, Performer


def test_performer_metadata_mapping_preserves_other_jellyfin_fields(monkeypatch):
    posted = []
    monkeypatch.setattr(
        legacy,
        "jf_get_item_dto",
        lambda *a, **k: {
            "Id": "p1",
            "Overview": "old",
            "EndDate": "2020-01-01",
            "CustomField": "keep",
        },
    )
    monkeypatch.setattr(legacy, "jf_post_json", lambda *a, **k: posted.append(a[3]))
    legacy.jf_update_person_metadata(
        "http://localhost",
        "key",
        "p1",
        20,
        overview="new",
        original_title="Alias",
        premiere_date="1990-01-01T00:00:00.000Z",
        end_date="",
        production_year=1990,
        production_locations=["Germany"],
    )
    assert posted[0]["CustomField"] == "keep"
    assert posted[0]["EndDate"] is None and posted[0]["OriginalTitle"] == "Alias"
    assert posted[0]["ProductionYear"] == 1990


def test_performer_adapter_uses_imported_settings_and_original_hook_cookie(
    setup, monkeypatch, capsys
):
    stash, config, state, root = setup
    stash.connection = {
        "Scheme": "http",
        "Host": "localhost",
        "Port": 9999,
        "SessionCookie": {"Name": "hook", "Value": "context"},
    }
    stash.base = "http://localhost:9999"
    config.sections["performer"].update(
        jellyfin_url="http://localhost/jf",
        jellyfin_api_key="key",
        update_image=True,
        update_overview=True,
        add_jellyfin_profile_url=True,
        reencode_images=False,
    )
    person = {
        "id": "7",
        "name": "Exact Name",
        "image_path": "/image",
        "aliases": ["Alias"],
        "birthdate": "1990-01-02",
        "details": "Bio",
        "country": "Germany",
        "favorite": True,
    }
    reads = []
    updates = []
    uploads = []
    links = []
    monkeypatch.setattr(
        legacy, "get_performer", lambda base, cookie, *a: reads.append(cookie) or person
    )
    state.put("performer_mapping", "7", "person-7")
    monkeypatch.setattr(legacy, "jf_person_id_verified", lambda *a, **k: (True, True))
    monkeypatch.setattr(legacy, "jf_update_person_metadata", lambda *a, **k: updates.append(k))
    monkeypatch.setattr(
        legacy, "stash_add_jellyfin_profile_url_to_performer", lambda *a: links.append(a) or True
    )
    monkeypatch.setattr(
        legacy, "fetch_stash_image", lambda *a: (b"\xff\xd8\xffimage", "image/jpeg")
    )
    monkeypatch.setattr(legacy, "jf_post_binary", lambda *a, **k: uploads.append(a))
    result = Performer(stash, config, state).sync("7")
    assert result["ok"] and reads == ["hook=context"]
    assert updates and uploads and links
    assert 'Darsteller "Exact Name" (ID=7): Bild automatisch aufbereitet' in capsys.readouterr().err
    assert updates[0]["production_year"] == 1990
    assert updates[0]["end_date"] == ""


def test_performer_not_yet_in_jellyfin_remains_retryable(setup, monkeypatch):
    stash, config, state, root = setup
    stash.connection = {"Host": "localhost"}
    stash.base = "http://localhost:9999"
    config.sections["performer"].update(jellyfin_url="http://localhost/jf", jellyfin_api_key="key")
    monkeypatch.setattr(
        legacy, "get_performer", lambda *a: {"id": "7", "name": "Name", "image_path": ""}
    )
    monkeypatch.setattr(legacy, "load_map", lambda: {})
    monkeypatch.setattr(legacy, "jf_search_people", lambda *a, **k: [])
    monkeypatch.setattr(legacy, "pick_person_id_with_stash_verification", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="not found"):
        Performer(stash, config, state).sync("7")


def test_own_profile_link_event_does_not_repeat_upload(setup):
    stash, config, state, root = setup
    config.values["performer_enabled"] = True

    class FakePerformer:
        def __init__(self):
            self.person = {"id": "7", "name": "Name", "urls": []}
            self.calls = 0

        def read(self, _):
            return copy.deepcopy(self.person)

        def sync(self, _):
            self.calls += 1
            self.person["urls"].append("http://localhost/jf/person-7")

    service = FakePerformer()
    engine = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state), service)
    engine.person("7")
    engine.person("7")
    assert service.calls == 1


def test_manual_cleanup_preserves_videos_library_roots_and_exclusions(setup, tmp_path):
    stash, config, state, root = setup
    trash = tmp_path / "trash"
    trash.mkdir()
    (root / "empty").mkdir()
    (root / "small").mkdir()
    (root / "small" / "readme.txt").write_text("small")
    (root / "excluded").mkdir()
    config.sections["cleanup"].update(
        DELETE_PATH=str(trash),
        DELETE_FOLDER_NAME="delete",
        DRY_RUN=False,
        INCLUDE_FILE_FOLDERS=True,
        SIZE_LIMIT_MB="20",
        EXCLUDED_PATHS=[str(root / "excluded")],
    )
    engine = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state))
    result = engine.process({"kind": "cleanup", "id": "all"})
    assert result["cleanup_moved"] == 2
    assert (
        (root / "incoming" / "old.mp4").exists() and root.exists() and (root / "excluded").exists()
    )
    assert list(trash.rglob("readme.txt"))


def test_favorites_only_write_when_not_already_favorite(setup, monkeypatch):
    stash, config, state, root = setup
    config.sections["export"].update(
        JELLYFIN_URL="http://localhost/jf", JELLYFIN_API_KEY="key", JELLYFIN_USER="TestUser"
    )
    monkeypatch.setattr(legacy, "jf_search_people", lambda *a, **k: [{"Id": "p1"}])
    monkeypatch.setattr(legacy, "pick_person_id_with_stash_verification", lambda *a, **k: "p1")

    class FakeHTTP:
        timeout = 20

        def __init__(self):
            self.favorite = False
            self.posts = []

        def call(self, method, url, headers, **kwargs):
            if url.endswith("/Users"):
                return [{"Id": "a" * 32, "Name": "TestUser"}]
            if method == "GET":
                return {"UserData": {"IsFavorite": self.favorite}}
            self.favorite = True
            self.posts.append(kwargs)

    http = FakeHTTP()
    client = Jellyfin(config, http)
    persons = [
        {"id": "7", "name": "Name", "favorite": True},
        {"id": "8", "name": "Other", "favorite": False},
    ]
    client.favorites(persons)
    client.favorites(persons)
    assert len(http.posts) == 1
