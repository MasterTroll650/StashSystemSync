import copy
import socket
from pathlib import Path

import pytest

from workflow.config import Config
from workflow.state import State


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if host not in {"127.0.0.1", "localhost", "::1", None}:
            raise AssertionError("Tests must not contact real services: " + str(host))
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    folder = root / "incoming"
    folder.mkdir()
    video = folder / "old.mp4"
    video.write_bytes(b"video-data")
    (folder / "old.funscript").write_text('{"actions":[]}')
    scene = {
        "id": "1",
        "title": "A & B",
        "details": "Text <test> & more",
        "date": "2026-11-16",
        "organized": True,
        "interactive": False,
        "code": "CODE",
        "studio": {"id": "1", "name": "Studio"},
        "stash_ids": [{"endpoint": "https://stashdb.org/graphql", "stash_id": "external-1"}],
        "tags": [{"id": "vr", "name": "Virtual Reality"}],
        "performers": [],
        "paths": {"screenshot": "/scene/1/screenshot"},
        "files": [
            {
                "id": "11",
                "path": str(video),
                "basename": video.name,
                "duration": 125,
                "width": 1920,
                "height": 1080,
                "format": "mp4",
                "video_codec": "h264",
                "audio_codec": "aac",
            }
        ],
    }

    class FakeStash:
        def __init__(self):
            self.scene = scene
            self.moves = []
            self.tag_calls = 0
            self.downloads = []
            self.scheduled = 0
            self.configuration_calls = 0

        def find_scene(self, sid):
            return copy.deepcopy(self.scene) if str(sid) == "1" else None

        def move(self, fid, destination):
            f = next(f for f in self.scene["files"] if f["id"] == str(fid))
            destination.parent.mkdir(parents=True, exist_ok=True)
            Path(f["path"]).rename(destination)
            f.update(path=str(destination), basename=destination.name)
            self.moves.append((str(fid), destination))

        def add_tag(self, scene, name):
            if not any(t["name"] == name for t in self.scene["tags"]):
                self.scene["tags"].append({"id": "fun", "name": name})
                self.tag_calls += 1

        def bytes(self, url):
            self.downloads.append(url)
            return b"\xff\xd8\xff" + b"image" * 400

        def find_studio(self, sid):
            return {"id": sid, "name": "Studio"}

        def configuration(self):
            self.configuration_calls += 1
            return {"plugins": {}, "general": {"stashes": [{"path": str(root)}]}}

        def schedule(self):
            self.scheduled += 1

        def scenes(self):
            yield self.find_scene("1")

    config = Config(
        {
            "rename_defaultDirectoryPathFormat": str(
                root / "$studio_name$" / "$tag_replace$$title${ - $date$}"
            ),
            "rename_defaultFileNameFormat": "$tag_replace$$title${ - $date$}.$ext$",
            "rename_tagReplace": '"Virtual Reality"=="VR_"',
            "rename_renameRelatedFiles": True,
            "export_ORGANIZED": False,
            "export_EXCLUDED_PATHS": "",
            "whisparr_MONITORED": False,
            "dry_run": False,
            "performer_enabled": False,
        }
    )
    state = State(tmp_path / "data")
    yield FakeStash(), config, state, root
    state.close()


class FakeJellyfin:
    def __init__(self):
        self.calls = []
        self.fail = 0

    def notify(self, paths):
        self.calls.append(paths)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Temporary Jellyfin error")


class FakeWhisparr:
    def __init__(self, state):
        self.calls = []
        self.state = state

    def sync(self, scene):
        self.calls.append(copy.deepcopy(scene))
        self.state.put("whisparr_mapping", scene["id"], 99)

    def stash_id(self, scene):
        return "external-1"
