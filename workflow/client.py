from urllib.parse import urlsplit

import requests

from .legacy.performer import stash_cookie_from_server_connection

SCENE_FIELDS = """id title details date director code organized interactive
tags {id name} studio {id name parent_studio {id name}}
stash_ids {endpoint stash_id} performers {id name image_path}
paths {screenshot} files {id path basename format width height duration video_codec audio_codec}"""


class Stash:
    def __init__(self, connection, timeout=20):
        self.connection = connection
        scheme = connection.get("Scheme", "http")
        host = connection.get("Host", "localhost")
        if host in {"0.0.0.0", "::"}:
            host = "localhost"
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        self.base = f"{scheme}://{host}:{connection.get('Port', 9999)}"
        self.timeout = timeout
        self.session = requests.Session()
        cookie = stash_cookie_from_server_connection(connection)
        if cookie:
            self.session.headers["Cookie"] = cookie
        key = connection.get("ApiKey") or connection.get("APIKey")
        if key:
            self.session.headers["ApiKey"] = key

    def gql(self, query, variables=None):
        response = self.session.post(
            self.base + "/graphql",
            json={"query": query, "variables": variables or {}},
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errors"):
            raise RuntimeError("Stash GraphQL: " + str(result["errors"]))
        return result["data"]

    def configuration(self):
        return self.gql(
            "query {configuration {plugins general {stashes {path excludeVideo excludeImage}}}}"
        )["configuration"]

    def find_scene(self, scene_id):
        return self.gql(
            "query($id:ID!){findScene(id:$id){" + SCENE_FIELDS + "}}", {"id": str(scene_id)}
        )["findScene"]

    def find_studio(self, studio_id):
        return self.gql(
            "query($id:ID!){findStudio(id:$id){id name parent_studio{id name}}}",
            {"id": str(studio_id)},
        )["findStudio"]

    def scenes(self):
        page = 1
        while True:
            rows = self.gql(
                "query($page:Int!){findScenes(filter:{page:$page,per_page:100}){scenes{"
                + SCENE_FIELDS
                + "}}}",
                {"page": page},
            )["findScenes"]["scenes"]
            yield from rows
            if len(rows) < 100:
                break
            page += 1

    def add_tag(self, scene, name):
        if any(t["name"] == name for t in scene.get("tags", [])):
            return
        tags = self.gql(
            "query($name:String!){findTags(tag_filter:{name:{value:$name,modifier:EQUALS}},filter:{per_page:1}){tags{id name}}}",
            {"name": name},
        )["findTags"]["tags"]
        if not tags:
            raise RuntimeError(f"Required tag does not exist: {name}")
        ids = [t["id"] for t in scene.get("tags", [])] + [tags[0]["id"]]
        self.gql(
            "mutation($input:SceneUpdateInput!){sceneUpdate(input:$input){id}}",
            {"input": {"id": scene["id"], "tag_ids": ids}},
        )

    def move(self, file_id, destination):
        result = self.gql(
            "mutation($input:MoveFilesInput!){moveFiles(input:$input)}",
            {
                "input": {
                    "ids": [str(file_id)],
                    "destination_folder": str(destination.parent),
                    "destination_basename": destination.name,
                }
            },
        )
        if result.get("moveFiles") is not True:
            raise RuntimeError("Stash did not confirm file move")

    def schedule(self):
        self.gql(
            'mutation($id:ID!,$args:[PluginArgInput!]){runPluginTask(plugin_id:$id,description:"Stash System Sync",args:$args)}',
            {"id": "StashSystemSync", "args": [{"key": "mode", "value": {"str": "worker"}}]},
        )

    def bytes(self, url):
        from urllib.parse import urljoin

        target = urljoin(self.base + "/", url)
        if urlsplit(target).netloc != urlsplit(self.base).netloc:
            # Do not forward Stash authentication to another image host.
            response = requests.get(target, timeout=self.timeout)
        else:
            response = self.session.get(target, timeout=self.timeout)
        response.raise_for_status()
        return response.content
