import json

import pytest
import requests

from plugin import run
from workflow.config import Config
from workflow.engine import Engine
from workflow.legacy import performer as legacy
from workflow.services import Jellyfin, Performer
from workflow.settings import SETTING_KEYS
from workflow.settings_migration import migrate_settings


class UserHTTP:
    timeout = 20

    def __init__(self, users=None):
        self.users = users if users is not None else [{"Name": "TestUser", "Id": "user-guid"}]
        self.calls = []

    def call(self, method, url, headers, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/Users"):
            return self.users
        if method == "GET":
            return {"UserData": {"IsFavorite": False}}
        return None


def configured():
    return Config(
        {
            "dry_run": False,
            "jellyfin_url": "http://localhost/jf",
            "jellyfin_api_key": "key",
            "jellyfin_username": "TestUser",
        }
    )


def test_username_is_resolved_once_per_client_and_cache_tracks_changes():
    config = configured()
    http = UserHTTP()
    service = Jellyfin(config, http)
    assert service.user_id() == service.user_id() == "user-guid"
    assert len(http.calls) == 1
    config.sections["export"]["JELLYFIN_USER"] = "Other"
    http.users = [{"Name": "Other", "Id": "other-guid"}]
    assert service.user_id() == "other-guid" and len(http.calls) == 2


@pytest.mark.parametrize(
    "users", [[], [{"Name": "TestUser", "Id": "one"}, {"Name": "TestUser", "Id": "two"}]]
)
def test_missing_or_ambiguous_user_is_an_error(users):
    with pytest.raises(ValueError, match="not found or ambiguous"):
        Jellyfin(configured(), UserHTTP(users)).user_id()


def test_username_lookup_auth_failure_is_not_hidden():
    http = UserHTTP()

    def fail(*args, **kwargs):
        raise requests.HTTPError("Unauthorized")

    http.call = fail
    with pytest.raises(requests.HTTPError):
        Jellyfin(configured(), http).user_id()


def test_performer_adapter_passes_resolved_id_not_username(setup, monkeypatch):
    stash, config, state, root = setup
    config = configured()
    stash.connection = {}
    received = []

    def main(payload, settings_override, mapping_store):
        received.append(settings_override["jellyfin_user_id"])
        print(json.dumps({"ok": True, "output": {"message": ""}}))

    monkeypatch.setattr(legacy, "main", main)
    resolver = Jellyfin(config, UserHTTP())
    Performer(stash, config, state, resolver.user_id).sync("7")
    assert received == ["user-guid"]


def test_favorites_skip_missing_and_continue_with_named_info(monkeypatch, capsys):
    http = UserHTTP()
    service = Jellyfin(configured(), http)
    monkeypatch.setattr(legacy, "jf_search_people", lambda *a, **k: [])
    monkeypatch.setattr(
        legacy,
        "pick_person_id_with_stash_verification",
        lambda *a, **k: None if a[3] == "Missing" else "person-guid",
    )
    result = service.favorites(
        [
            {"id": "2644", "name": "Missing", "favorite": True},
            {"id": "7", "name": "Present", "favorite": True},
            {"id": "8", "name": "NotFavorite", "favorite": False},
        ]
    )
    assert result == {"checked": 2, "updated": 1, "already_current": 0, "missing": 1}
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
    assert (
        'Darsteller "Missing" (ID=2644): Favorit nicht in Jellyfin gefunden; uebersprungen'
        in capsys.readouterr().err
    )


def test_favorites_real_search_errors_still_fail(monkeypatch):
    def fail(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(legacy, "jf_search_people", fail)
    with pytest.raises(requests.ConnectionError):
        Jellyfin(configured(), UserHTTP()).favorites(
            [{"id": "7", "name": "Present", "favorite": True}]
        )


def test_status_prints_log_without_network_or_queue_changes(tmp_path, capsys):
    result = run({"args": {"mode": "status"}}, tmp_path)
    assert result["jobs"] == []
    output = capsys.readouterr()
    assert "Status: Warteschlange=0, offen=0, fehlgeschlagen=0" in output.err
    assert output.out == ""


def test_unchanged_overall_progress_is_not_logged_twice(setup, capsys):
    stash, config, state, root = setup
    Engine(stash, config, state, [root]).drain(wait=False)
    messages = capsys.readouterr().err
    assert messages.count("Gesamt: aktuell kein Lauf vorhanden") == 1
    assert "Arbeitsabschnitt beendet" in messages


def test_legacy_user_id_requires_verified_username_for_migration():
    old = {"20_03_jellyfin_user_id": "old-guid", SETTING_KEYS["dry_run"]: False}
    with pytest.raises(ValueError, match="resolve the saved user ID"):
        migrate_settings(old)
    migrated = migrate_settings(old, {"jellyfin_username": "TestUser"})
    assert migrated[SETTING_KEYS["jellyfin_username"]] == "TestUser"
    assert migrated[SETTING_KEYS["dry_run"]] is False
