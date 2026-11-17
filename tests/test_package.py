import hashlib
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import yaml

from tools.build_package import build


def test_package_allowlist_excludes_private_state_and_has_matching_index(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    shutil.copytree(
        root,
        source,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache"
        ),
    )
    (source / "data").mkdir(exist_ok=True)
    (source / "data" / "workflow.sqlite").write_text("private database")
    (source / ".env").write_text("secret=private")
    wheel = tmp_path / "pip.whl"
    wheel.write_bytes(b"test wheel")
    archive, checksum = build(source, tmp_path / "output", wheel=wheel)
    assert checksum == hashlib.sha256(archive.read_bytes()).hexdigest()
    index = yaml.safe_load((archive.parent / "index.yml").read_text())
    assert index[0]["sha256"] == checksum and index[0]["id"] == "StashSystemSync"
    datetime.strptime(index[0]["date"], "%Y-%m-%d %H:%M:%S")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert "StashSystemSync.yml" in names and "vendor/pip.whl" in names
        assert not any(
            name.startswith(("data/", "tests/", ".git/")) or name == ".env" for name in names
        )
