"""Publish the generated index after the release asset exists."""

import base64
import json
import os
from pathlib import Path

import requests


def main():
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
        }
    )
    url = "https://api.github.com/repos/MasterTroll650/StashSystemSync/contents/index.yml"
    response = session.get(url, timeout=30)
    if response.status_code != 404:
        response.raise_for_status()
    version = json.loads(Path("release.json").read_text())["version"]
    body = {
        "message": "Publish Stash source index for v" + version,
        "content": base64.b64encode(Path("dist/index.yml").read_bytes()).decode(),
        "branch": "main",
    }
    if response.status_code == 200:
        body["sha"] = response.json()["sha"]
    session.put(url, json=body, timeout=30).raise_for_status()
    print("Stash source index published")


if __name__ == "__main__":
    main()
