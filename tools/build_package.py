"""Build a Stash source index and a package from an explicit public-file allowlist."""

import argparse
import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

import yaml

REPOSITORY = "MasterTroll650/StashSystemSync"


def pip_wheel(root):
    version = json.loads((root / "release.json").read_text())["pip_version"]
    cache = root / "build" / ("pip-" + version + ".whl")
    with urllib.request.urlopen(
        "https://pypi.org/pypi/pip/" + version + "/json", timeout=30
    ) as response:
        metadata = json.load(response)
    wheel = next(
        file for file in metadata["urls"] if file["filename"].endswith("-py3-none-any.whl")
    )
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(wheel["url"], timeout=60) as response:
            cache.write_bytes(response.read())
    if hashlib.sha256(cache.read_bytes()).hexdigest() != wheel["digests"]["sha256"]:
        raise ValueError("pip wheel checksum mismatch")
    return cache


def build(root, output, wheel=None):
    root, output = Path(root), Path(output)
    metadata = json.loads((root / "release.json").read_text())
    manifest = yaml.safe_load((root / "StashSystemSync.yml").read_text())
    if manifest["version"] != metadata["version"]:
        raise ValueError("Release and manifest versions differ")
    wheel = Path(wheel) if wheel else pip_wheel(root)
    files = {
        Path(name)
        for name in (
            "plugin.py",
            "StashSystemSync.yml",
            "requirements.txt",
            "README.md",
            "THIRD_PARTY.md",
            "LICENSE",
        )
    }
    files.update(path.relative_to(root) for path in (root / "workflow").rglob("*.py"))
    files.update(path.relative_to(root) for path in (root / "LICENSES").glob("*"))
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "StashSystemSync.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(files):
            entry = zipfile.ZipInfo(path.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            package.writestr(entry, (root / path).read_bytes())
        package.writestr("vendor/pip.whl", wheel.read_bytes())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    index = [
        {
            "id": "StashSystemSync",
            "name": "Stash System Sync",
            "version": metadata["version"],
            "date": metadata["date"] + " 00:00:00",
            "path": "https://github.com/"
            + REPOSITORY
            + "/releases/download/v"
            + metadata["version"]
            + "/StashSystemSync.zip",
            "sha256": checksum,
            "metadata": {"description": manifest["description"]},
        }
    ]
    (output / "index.yml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    return archive, checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    archive, checksum = build(Path(__file__).resolve().parents[1], args.output)
    print(str(archive) + " SHA256=" + checksum)


if __name__ == "__main__":
    main()
