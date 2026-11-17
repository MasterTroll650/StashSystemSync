"""Install runtime dependencies privately; keep raw plugin stdout as JSON."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

MODULES = ("requests", "yaml", "stashapi.stashapp", "PIL.Image", "resvg_py")


def _ready():
    try:
        for name in MODULES:
            importlib.import_module(name)
        return True
    except (ImportError, OSError):
        return False


def ensure_dependencies(root):
    root = Path(root)
    target = root / "data" / "python"
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    if _ready():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / "dependency-install.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            importlib.invalidate_caches()
            if _ready():
                return
            wheel = root / "vendor" / "pip.whl"
            if wheel.is_file():
                command = [
                    sys.executable,
                    "-c",
                    "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('pip',run_name='__main__')",
                    str(wheel),
                ]
            else:
                command = [sys.executable, "-m", "pip"]
            print(
                "\x01i\x02Stash System Sync: preparing private Python dependencies (first use only)",
                file=sys.stderr,
                flush=True,
            )
            result = subprocess.run(
                [
                    *command,
                    "install",
                    "--isolated",
                    "--no-input",
                    "--disable-pip-version-check",
                    "--no-warn-script-location",
                    "--upgrade",
                    "--target",
                    str(target),
                    "-r",
                    str(root / "requirements.txt"),
                ],
                capture_output=True,
                timeout=300,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    "Dependency installation failed; check network access to PyPI and the Python installation"
                )
            prefixes = {name.split(".")[0] for name in MODULES}
            for name in list(sys.modules):
                if name.split(".")[0] in prefixes:
                    sys.modules.pop(name, None)
            importlib.invalidate_caches()
            if not _ready():
                raise RuntimeError("Installed Python dependencies could not be loaded")
            print(
                "\x01i\x02Stash System Sync: private dependencies ready",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
