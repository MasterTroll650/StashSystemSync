import base64
import copy
import io
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from PIL import Image

from workflow.engine import Engine
from workflow.errors import JellyfinPersonNotFound, RetryLater
from workflow.legacy import performer as legacy
from workflow.services import Performer, Whisparr

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 120"><rect width="80" height="120" fill="red"/></svg>'


def bmp_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(stream, format="BMP")
    return stream.getvalue()


def response(status, text=""):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode()
    return result


def test_legacy_fetch_normalizes_bmp(monkeypatch):
    image = response(200)
    image._content = bmp_bytes()
    image.headers["Content-Type"] = "image/bmp"
    monkeypatch.setattr(legacy.requests, "get", lambda *a, **k: image)
    data, content_type = legacy.fetch_stash_image("http://localhost", "", "/image", 20)
    assert content_type == "image/png"
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.format == "PNG"
        decoded.verify()


def test_svg_performer_sync_renders_png_and_updates_metadata(setup, monkeypatch):
    stash, config, state, root = setup
    stash.connection = {"Host": "localhost"}
    stash.base = "http://localhost:9999"
    config.sections["performer"].update(jellyfin_url="http://localhost", jellyfin_api_key="key")
    updates, uploads = [], []
    monkeypatch.setattr(
        legacy,
        "get_performer",
        lambda *a: {"id": "7", "name": "Person", "image_path": "/svg", "details": "Bio"},
    )
    state.put("performer_mapping", "7", "person")
    monkeypatch.setattr(legacy, "jf_person_id_verified", lambda *a, **k: (True, ""))
    monkeypatch.setattr(legacy, "jf_update_person_metadata", lambda *a, **k: updates.append(k))
    image_response = response(200)
    image_response._content = SVG
    image_response.headers["Content-Type"] = "image/svg+xml"
    monkeypatch.setattr(legacy.requests, "get", lambda *a, **k: image_response)
    monkeypatch.setattr(legacy, "jf_post_binary", lambda *a, **k: uploads.append(a))
    result = Performer(stash, config, state).sync("7")
    assert result["ok"] and updates and len(uploads) == 1
    assert uploads[0][4] == "image/png"
    with Image.open(io.BytesIO(uploads[0][3])) as image:
        assert image.size == (80, 120)
        assert image.convert("RGB").getpixel((40, 60)) == (255, 0, 0)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_cache_is_not_discarded_for_auth_or_server_error(monkeypatch, status):
    mapping = {"1790": "old", "other": "keep"}
    saved = []

    def verify(*args, **kwargs):
        raise requests.HTTPError(response=response(status))

    monkeypatch.setattr(legacy, "jf_person_id_verified", verify)
    monkeypatch.setattr(legacy, "save_map", lambda data: saved.append(dict(data)))
    with pytest.raises(requests.HTTPError):
        legacy.resolve_person_id("http://localhost", "key", "Person", "1790", mapping, 20, "user")
    assert mapping == {"1790": "old", "other": "keep"} and not saved


def test_deleted_cached_person_is_searched_and_verified_again(monkeypatch):
    mapping = {"1790": "old", "other": "keep"}
    saved = []

    def verify(base, key, pid, *args, **kwargs):
        if pid == "old":
            raise JellyfinPersonNotFound("gone")
        return True, "1790"

    monkeypatch.setattr(legacy, "jf_person_id_verified", verify)
    monkeypatch.setattr(legacy, "save_map", lambda data: saved.append(dict(data)))
    monkeypatch.setattr(
        legacy, "jf_search_people", lambda *a, **k: [{"Id": "new", "Name": "Person"}]
    )
    assert (
        legacy.resolve_person_id("http://localhost", "key", "Person", "1790", mapping, 20, "user")
        == "new"
    )
    assert mapping == {"1790": "new", "other": "keep"} and len(saved) == 2


@pytest.mark.parametrize("user_exists", [True, False])
def test_item_404_is_distinguished_from_missing_user(monkeypatch, user_exists):
    calls = []

    def get(*args, **kwargs):
        calls.append(args[2])
        raise requests.HTTPError(response=response(404))

    monkeypatch.setattr(legacy, "jf_get", get)
    monkeypatch.setattr(
        legacy, "jf_get_users", lambda *a, **k: [{"Id": "user"}] if user_exists else []
    )
    with pytest.raises(JellyfinPersonNotFound if user_exists else requests.HTTPError):
        legacy.jf_get_item_dto("http://localhost", "key", "old", 20, "user")
    assert calls == ["/Users/user/Items/old"]


def test_upload_retains_first_server_error_and_makes_only_one_attempt(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return response(500, "file is locked")

    monkeypatch.setattr(legacy.requests, "post", post)
    with pytest.raises(RetryLater, match="HTTP 500") as caught:
        legacy.jf_post_binary(
            "http://localhost", "key", "/Items/person/Images/Primary", b"image", "image/png"
        )
    assert caught.value.retry_after == 60 and len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["data"] == base64.b64encode(b"image") and kwargs["timeout"][1] >= 60
    assert "Authorization" in kwargs["headers"] and "params" not in kwargs


def test_upload_timeout_is_retried_later_without_an_immediate_second_upload(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout("still uploading")

    monkeypatch.setattr(legacy.requests, "post", post)
    with pytest.raises(RetryLater) as caught:
        legacy.jf_post_binary("http://localhost", "key", "/image", b"image", "image/png")
    assert calls == [1] and caught.value.retry_after == 120


def test_upload_success_uses_one_authenticated_base64_request(monkeypatch):
    calls = []
    monkeypatch.setattr(legacy.requests, "post", lambda *a, **k: calls.append(k) or response(204))
    legacy.jf_post_binary("http://localhost", "key", "/image", b"image", "image/png")
    assert len(calls) == 1 and calls[0]["data"] == base64.b64encode(b"image")


def test_delayed_retry_is_persisted_without_losing_other_jobs(setup):
    stash, config, state, root = setup
    clock = [100.0]
    state.clock = lambda: clock[0]
    state.enqueue("performer", "344")
    state.enqueue("performer", "other")
    calls = []

    def process(job):
        calls.append(job["id"])
        if job["id"] == "344" and calls.count("344") == 1:
            raise RetryLater("busy", 120)
        return {}

    worker = Engine(stash, config, state, [root])
    worker.process = process
    worker.drain(wait=False)
    row = state.db.execute("SELECT * FROM jobs").fetchone()
    assert row["id"] == "344" and row["due"] == 220 and row["attempts"] == 1 and "other" in calls
    clock[0] = 130
    worker.drain(wait=False)
    assert calls.count("344") == 1
    clock[0] = 221
    worker.drain(wait=False)
    assert not state.pending()


class WhisparrHTTP:
    def __init__(self, scene, root):
        self.owner = {
            "id": 99,
            "title": scene["title"],
            "path": str(Path(scene["files"][0]["path"]).parent).replace("\\", "/"),
            "rootFolderPath": str(root),
            "qualityProfileId": 4,
            "monitored": True,
            "foreignId": "original-external",
            "stashId": "original-external",
        }
        self.root = root
        self.calls = []
        self.lookup = []

    def call(self, method, url, headers=None, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, kwargs))
        if method == "GET":
            if path == "/api/v3/movie":
                return copy.deepcopy(self.lookup if kwargs.get("params") else [self.owner])
            if path == "/api/v3/movie/99":
                return copy.deepcopy(self.owner)
            if path == "/api/v3/qualityprofile":
                return [{"id": 4}]
            if path == "/api/v3/rootfolder":
                return [{"path": str(self.root)}]
        if method == "PUT":
            return kwargs["json"]
        raise AssertionError("No POST or DELETE is allowed for confirmed existing movie")


def conflicting_whisparr(setup):
    stash, config, state, root = setup
    config.sections["whisparr"].update(
        WHISPARR_URL="http://localhost", WHISPARR_API_KEY="key", MONITORED=True
    )
    http = WhisparrHTTP(stash.scene, root)
    return Whisparr(config, state, [root], http), http


def test_path_conflict_is_logged_without_manual_mapping_or_writes(setup, capsys):
    stash, config, state, root = setup
    service, http = conflicting_whisparr(setup)
    result = service.sync(stash.scene)
    assert result["skipped"] == "Whisparr identity conflict"
    assert result["whisparr_ids"] == [99]
    assert not any(call[0] in {"PUT", "POST", "DELETE"} for call in http.calls)
    assert state.get("whisparr_mapping", "1") is None
    log = capsys.readouterr().err
    assert "Zuordnungskonflikt" in log and 'Szene "A & B" (ID=1)' in log and "Whisparr-ID=99" in log
    assert "StashDB-ID=" in log and "pruefen" in log


def test_matching_external_id_still_updates_existing_entry(setup):
    stash, config, state, root = setup
    service, http = conflicting_whisparr(setup)
    http.lookup = [copy.deepcopy(http.owner)]
    http.owner["monitored"] = False
    http.lookup[0]["monitored"] = False
    assert service.sync(stash.scene)["movie_id"] == 99
    assert state.get("whisparr_mapping", "1") == 99
    assert not any(call[0] in {"POST", "DELETE"} for call in http.calls)


def test_raster_conversion_uses_existing_ffmpeg_when_pillow_is_unavailable(monkeypatch):
    from types import SimpleNamespace

    from workflow import images

    def missing_pillow(*args):
        raise ImportError("Pillow not installed in this container")

    calls = []
    monkeypatch.setattr(images, "_pillow_raster", missing_pillow)
    monkeypatch.setattr(
        images.subprocess,
        "run",
        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(stdout=b"\xff\xd8converted"),
    )
    assert images.convert_raster(bmp_bytes()) == b"\xff\xd8converted"
    assert calls[0][1]["timeout"] == 60 and calls[0][1]["input"] == bmp_bytes()


def test_large_upload_is_reencoded_below_body_limit_without_changing_source(monkeypatch):
    stream = io.BytesIO()
    Image.effect_noise((1024, 1024), 100).save(stream, format="PNG")
    source = stream.getvalue()
    original = bytes(source)
    calls = []
    monkeypatch.setattr(legacy, "MAX_IMAGE_UPLOAD_BYTES", 300000)
    monkeypatch.setattr(legacy.requests, "post", lambda *a, **k: calls.append(k) or response(204))
    legacy.jf_post_binary("http://localhost", "key", "/image", source, "image/png")
    assert len(calls) == 1 and len(calls[0]["data"]) <= 400000
    reduced = base64.b64decode(calls[0]["data"])
    assert calls[0]["headers"]["Content-Type"] == "image/jpeg"
    with Image.open(io.BytesIO(reduced)) as image:
        assert image.format == "JPEG" and max(image.size) <= 512
    assert source == original
