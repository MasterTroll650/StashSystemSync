import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import boolean
from .legacy.rename_config import Config as RenameConfig
from .legacy.renamer import StashFile


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def lines(value):
    return [
        p.strip()
        for p in (value.splitlines() if isinstance(value, str) else value or [])
        if p.strip()
    ]


def ignored(file, value):
    for suffix in re.split(r"[,\n]+", value or ""):
        suffix = suffix.strip()
        if suffix and re.search(
            re.escape(suffix).replace(re.escape("$index$"), r"\d+") + r"$",
            Path(file["basename"]).stem,
            re.I,
        ):
            return True
    return False


def atomic_write(path, data):
    path = Path(path)
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".stashworkflow-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, (path.stat().st_mode & 0o777) if path.exists() else 0o666)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def nfo(scene, file):
    root = ET.Element("movie")
    values = {
        "title": scene.get("title") or Path(file["basename"]).stem,
        "name": scene.get("title") or Path(file["basename"]).stem,
        "plot": scene.get("details") or "",
        "runtime": str(int(float(file.get("duration") or 0) / 60)),
        "studio": (scene.get("studio") or {}).get("name", ""),
        "year": (scene.get("date") or "")[:4],
        "premiered": scene.get("date") or "",
    }
    for key, value in values.items():
        ET.SubElement(root, key).text = value
    for performer in scene.get("performers", []):
        ET.SubElement(ET.SubElement(root, "actor"), "name").text = performer["name"]
    for tag in scene.get("tags", []):
        ET.SubElement(root, "tag").text = tag["name"]
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class Files:
    def __init__(self, stash, config, state, library_roots):
        self.stash, self.config, self.state = stash, config, state
        self.roots = [Path(p).resolve() for p in library_roots]

    def exportable(self, scene, file):
        section = self.config.sections["export"]
        if boolean(section.get("ORGANIZED"), True) and not scene.get("organized"):
            return False
        if ignored(file, section.get("IGNORED_FILE_SUFFIXES", "")):
            return False
        return not any(
            inside(file["path"], p) for p in lines(section.get("EXCLUDED_PATHS", ["/people"]))
        )

    def _destination(self, scene, file, index):
        section = self.config.sections["rename"]
        rename = self.config.enabled("rename") and (
            scene.get("organized") or boolean(section.get("renameUnorganized"))
        )
        helper = StashFile(
            self.stash, RenameConfig(section), {**scene, "studio": scene.get("studio") or {}}, file
        )
        helper.duplicate_index = index
        if rename:
            folder = helper.get_new_file_folder()
            name = helper.get_new_file_name()
        else:
            folder = Path(file["path"]).parent
            name = Path(file["path"]).name
            if index:
                suffix = section.get("duplicateFileSuffix", " ($index$)").replace(
                    "$index$", str(index)
                )
                name = Path(name).stem + suffix + Path(name).suffix
        if self.config.enabled("export") and self.exportable(scene, file):
            # A duplicate keeps a sibling scene directory instead of adding another nested directory.
            original_helper = StashFile(
                self.stash,
                RenameConfig(section),
                {**scene, "studio": scene.get("studio") or {}},
                file,
            )
            original_name = (
                original_helper.get_new_file_name() if rename else Path(file["path"]).name
            )
            if folder.name == Path(original_name).stem:
                folder = folder.parent / Path(name).stem
            elif folder.name != Path(name).stem:
                folder = folder / Path(name).stem
        return folder / name, rename

    def plan(self, scene):
        reserved = set()
        result = []
        for file in sorted(scene.get("files") or [], key=lambda f: str(f["id"])):
            old = Path(file["path"]).resolve()
            if Path(file["path"]).is_symlink() or not any(inside(old, root) for root in self.roots):
                raise ValueError("Source is a symlink or outside configured Stash library roots")
            for index in range(1000):
                new, rename = self._destination(scene, file, index)
                new = new.resolve()
                if new not in reserved and (new == old or not new.exists()):
                    break
            else:
                raise FileExistsError("No free destination after 1000 duplicate suffixes")
            if not any(inside(new, root) for root in self.roots):
                raise ValueError("Destination is outside configured Stash library roots")
            reserved.add(new)
            related = []
            if old != new and boolean(
                self.config.sections["rename"].get("renameRelatedFiles"), True
            ):
                videos = [
                    p
                    for p in old.parent.iterdir()
                    if p.suffix.lower()
                    in {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".ts"}
                ]
                for path in old.parent.iterdir():
                    if not path.is_file() or path == old:
                        continue
                    if path.stem == old.stem:
                        target = new.parent / (new.stem + path.suffix)
                    elif len(videos) == 1 and path.name.lower() in {
                        "movie.nfo",
                        "poster.jpg",
                        "fanart.jpg",
                    }:
                        target = new.parent / path.name
                    else:
                        continue
                    if path != target:
                        if target.exists():
                            raise FileExistsError(
                                "Sidecar destination already exists: " + str(target)
                            )
                        related.append([str(path), str(target)])
            dry = (
                self.config.dry()
                or (rename and self.config.dry("rename"))
                or (
                    self.config.enabled("export")
                    and self.exportable(scene, file)
                    and self.config.dry("export")
                )
            )
            result.append(
                {
                    "id": str(file["id"]),
                    "old": str(old),
                    "new": str(new),
                    "related": related,
                    "dry": dry,
                }
            )
        return result

    def organize(self, scene):
        key = str(scene["id"])
        plan = self.state.get("move_plan", key)
        if plan is None:
            plan = self.plan(scene)
            if not any(item["dry"] for item in plan):
                self.state.put("move_plan", key, plan)
        elif (
            self.config.dry()
            or (self.config.enabled("rename") and self.config.dry("rename"))
            or (self.config.enabled("export") and self.config.dry("export"))
        ):
            return [{**item, "dry": True} for item in plan]
        current = {str(f["id"]): f for f in scene["files"]}
        for item in plan:
            if item["id"] not in current:
                raise RuntimeError(
                    "File list changed during a pending move; inspect saved plan before continuing"
                )
            old, new = Path(item["old"]), Path(item["new"])
            now = Path(current[item["id"]]["path"]).resolve()
            if now not in {old, new}:
                raise RuntimeError(
                    "File path changed outside this workflow while a move was pending"
                )
            if item["dry"]:
                continue
            if now != new:
                if not old.exists():
                    raise FileNotFoundError("Source file is missing: " + str(old))
                if new.exists():
                    raise FileExistsError("Destination became occupied: " + str(new))
                self.stash.move(item["id"], new)
                current[item["id"]]["path"] = str(new)
                current[item["id"]]["basename"] = new.name
            for source, target in item["related"]:
                source, target = Path(source), Path(target)
                if not source.exists():
                    if target.exists():
                        continue
                    raise FileNotFoundError("Sidecar disappeared during move")
                if target.exists():
                    raise FileExistsError("Sidecar destination became occupied")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        if not any(item["dry"] for item in plan):
            self.state.remove("move_plan", key)
        return plan

    def clean_old_parents(self, plan):
        if not self.config.enabled("auto_cleanup") or self.config.dry():
            return
        for item in plan:
            if item["dry"] or item["old"] == item["new"]:
                continue
            directory = Path(item["old"]).parent
            while directory not in self.roots and any(
                inside(directory, root) for root in self.roots
            ):
                try:
                    directory.rmdir()
                except (OSError, FileNotFoundError):
                    break
                directory = directory.parent
