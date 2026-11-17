import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from requests import HTTPError

from workflow.client import Stash
from workflow.engine import Engine
from workflow.services import Whisparr


@pytest.fixture
def services(setup):
    fake, config, state, root = setup
    records = {"requests": [], "movie": None, "notify": 0, "fail_notify": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value=None, status=200):
            data = json.dumps(value).encode() if value is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            records["requests"].append(("GET", path, self.headers.get("Cookie"), None))
            if path.endswith("/screenshot.jpg"):
                data = fake.bytes(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/v3/qualityprofile":
                self.respond([{"id": 4}])
            elif path == "/api/v3/rootfolder":
                self.respond([{"path": str(root)}])
            elif path == "/api/v3/movie":
                self.respond([records["movie"]] if records["movie"] else [])
            elif path.startswith("/api/v3/movie/"):
                self.respond(records["movie"])
            else:
                self.respond({"error": "unexpected GET"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            records["requests"].append(("POST", self.path, self.headers.get("Cookie"), body))
            if self.path == "/graphql":
                query = body["query"]
                variables = body["variables"]
                if "findScene(" in query:
                    self.respond({"data": {"findScene": fake.find_scene(variables["id"])}})
                elif "moveFiles(" in query:
                    value = variables["input"]
                    fake.move(
                        value["ids"][0],
                        Path(value["destination_folder"]) / value["destination_basename"],
                    )
                    self.respond({"data": {"moveFiles": True}})
                elif "runPluginTask(" in query:
                    self.respond({"data": {"runPluginTask": "job-1"}})
                else:
                    self.respond({"errors": [{"message": "unexpected query"}]})
            elif self.path == "/jf/Library/Media/Updated":
                records["notify"] += 1
                if records["fail_notify"]:
                    records["fail_notify"] -= 1
                    self.respond({"error": "temporary failure"}, 503)
                else:
                    self.respond(None, 204)
            elif self.path == "/api/v3/movie":
                records["movie"] = {**body, "id": 99}
                self.respond(records["movie"], 201)
            else:
                self.respond({"error": "unexpected POST"}, 404)

        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            records["requests"].append(("PUT", self.path, self.headers.get("Cookie"), body))
            records["movie"] = body
            self.respond(body, 202)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    config.sections["export"].update(
        JELLYFIN_URL=origin + "/jf", JELLYFIN_API_KEY="jellyfin-test-key"
    )
    config.sections["whisparr"].update(WHISPARR_URL=origin, WHISPARR_API_KEY="whisparr-test-key")
    client = Stash(
        {
            "Scheme": "http",
            "Host": "127.0.0.1",
            "Port": server.server_port,
            "SessionCookie": {"Name": "hook", "Value": "original-context"},
        }
    )
    try:
        yield client, config, state, root, records, fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_http_stash_move_export_notification_and_whisparr(services):
    client, config, state, root, records, fake = services
    pipeline = Engine(client, config, state, [root])
    pipeline.scene("1")
    pipeline.scene("1")
    assert len(fake.moves) == 1 and records["notify"] == 1
    assert records["movie"]["monitored"] is False
    assert records["movie"]["path"] == str(Path(fake.scene["files"][0]["path"]).parent).replace(
        "\\", "/"
    )
    assert all(
        cookie == "hook=original-context"
        for method, path, cookie, body in records["requests"]
        if path == "/graphql"
    )
    assert not any(
        cookie
        for method, path, cookie, body in records["requests"]
        if path.startswith("/api/v3/") or path.startswith("/jf/")
    )
    assert (
        sum(
            method == "POST" and path == "/api/v3/movie" for method, path, *_ in records["requests"]
        )
        == 1
    )
    assert not any(method == "PUT" for method, *_ in records["requests"])


def test_real_http_failure_retries_notification_only(services):
    client, config, state, root, records, fake = services
    records["fail_notify"] = 1
    pipeline = Engine(client, config, state, [root])
    with pytest.raises(HTTPError):
        pipeline.scene("1")
    pipeline.scene("1")
    assert records["notify"] == 2 and len(fake.moves) == 1 and len(fake.downloads) == 1


def test_delete_unmonitors_but_never_deletes_media_or_whisparr_item(services):
    client, config, state, root, records, fake = services
    config.sections["whisparr"]["MONITORED"] = True
    pipeline = Engine(client, config, state, [root])
    pipeline.scene("1")
    file = Path(fake.scene["files"][0]["path"])
    pipeline.process({"kind": "scene_delete", "id": "1"})
    pipeline.process({"kind": "scene_delete", "id": "1"})
    assert records["movie"]["monitored"] is False
    assert file.exists() and state.get("whisparr_mapping", "1") is None
    assert not any(method == "DELETE" for method, *_ in records["requests"])
    assert sum(method == "PUT" for method, *_ in records["requests"]) == 1


def test_unchanged_whisparr_data_does_not_put(services):
    client, config, state, root, records, fake = services
    wh = Whisparr(config, state, [root])
    wh.sync(fake.scene)
    wh.sync(fake.scene)
    assert not any(method == "PUT" for method, *_ in records["requests"])
    fake.scene["title"] = "Changed"
    wh.sync(fake.scene)
    assert sum(method == "PUT" for method, *_ in records["requests"]) == 1


def test_missing_delete_mapping_completes_with_info_and_no_whisparr_request(services, capsys):
    client, config, state, root, records, fake = services
    state.enqueue("scene_delete", "404")
    result = Engine(client, config, state, [root]).drain(wait=False)
    assert result["processed"] == 1 and result["errors"] == []
    assert not state.db.execute("SELECT 1 FROM jobs").fetchone()
    assert not records["requests"]
    output = capsys.readouterr().err
    assert "\x01i\x02Szene ID=404: keine Whisparr-Zuordnung" in output
    assert "\x01e\x02" not in output


@pytest.mark.parametrize("status", [404, 401, 503])
def test_absent_whisparr_item_is_normal_but_real_service_errors_are_preserved(services, status):
    client, config, state, root, records, fake = services
    state.put("whisparr_mapping", "404", 99)
    calls = []

    class FailingHTTP:
        def call(self, *args, **kwargs):
            from requests import Response

            calls.append(args)
            response = Response()
            response.status_code = status
            raise HTTPError(response=response)

    service = Whisparr(config, state, [root], FailingHTTP())
    if status == 404:
        assert service.unmonitor("404") == {"skipped": "Whisparr entry absent"}
        assert state.get("whisparr_mapping", "404") is None
        assert state.get("whisparr_deleted", "404") == 99
    else:
        with pytest.raises(HTTPError):
            service.unmonitor("404")
        assert state.get("whisparr_mapping", "404") == 99
    assert len(calls) == 1 and calls[0][0] == "GET"


def test_worker_task_uses_compatible_graphql_args_and_original_cookie(services):
    client, config, state, root, records, fake = services
    client.schedule()
    method, path, cookie, body = records["requests"][-1]
    assert cookie == "hook=original-context"
    assert body["variables"]["id"] == "StashSystemSync"
    assert body["variables"]["args"][0]["value"]["str"] == "worker"


def test_real_http_notification_records_translated_paths(services):
    client, config, state, root, records, fake = services
    config.values["jellyfin_path_mappings"] = str(root) + "=/jellyfin-media"
    pipeline = Engine(client, config, state, [root])
    pipeline.scene("1")
    expected = (
        "/jellyfin-media/" + Path(fake.scene["files"][0]["path"]).relative_to(root).as_posix()
    )
    notifications = [
        body
        for method, path, cookie, body in records["requests"]
        if method == "POST" and path == "/jf/Library/Media/Updated"
    ]
    assert notifications == [{"Updates": [{"Path": expected}]}]
    assert state.get("scene:1", "notify_paths") == [expected]
    pipeline.scene("1")
    assert records["notify"] == 1
