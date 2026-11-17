from operator import itemgetter
from stashapi.stashapp import StashInterface
from stashapi import log
import os
import pathlib
import re


MOVE_FILE_MUTATION = """
mutation MoveFiles($input: MoveFilesInput!) {
    moveFiles(input: $input)
}
"""

TAG_REPLACE_PLACEHOLDER = "$tag_replace$"


def create_target_folder(path: pathlib.Path) -> None:
    """Create only missing target directories with Unraid-standard permissions."""
    directory_mode = 0o777
    missing_directories = []
    current = path
    while not current.exists():
        missing_directories.append(current)
        if current.parent == current:
            break
        current = current.parent

    for directory in reversed(missing_directories):
        try:
            directory.mkdir(mode=directory_mode)
        except FileExistsError:
            continue
        os.chmod(directory, directory_mode)


def get_parent_studio_chain(stash, scene):
    current_studio = scene.get("studio", {})
    parent_chain = [current_studio.get("name", "")]

    while current_studio.get("parent_studio"):
        current_studio = stash.find_studio(current_studio.get("parent_studio"))

        parent_chain.append(current_studio.get("name"))

    return "/".join(reversed(parent_chain))

def key_getter(key):
    return lambda _, data: data.get(key, "")

FILE_VARIABLES = {
    "audio_codec": key_getter("audio_codec"),
    "ext": lambda _, file: file.get("basename", "").split(".")[-1],
    "format": key_getter("format"),
    "height": key_getter("height"),
    "index": key_getter("index"),
    "video_codec": key_getter("video_codec"),
    "width": key_getter("width"),
}

SCENE_VARIABLES = {
    "scene_id": key_getter("id"),
    "title": key_getter("title"),
    "date": key_getter("date"),
    "director": key_getter("director"),
    "month": lambda _, scene: scene.get("date", "").split("-")[1] if scene.get("date") else "",
    "parent_studio_chain": get_parent_studio_chain,
    "studio_code": key_getter("code"),
    "studio_name": lambda _, scene: scene.get("studio", {}).get("name", ""),
    "year": lambda _, scene: scene.get("date", "").split("-")[0] if scene.get("date") else "",
}

def find_variables(format_template) -> list[str]:
    variables = []

    for variable in FILE_VARIABLES.keys():
        if f"${variable}$" in format_template:
            variables.append(variable)

    for variable in SCENE_VARIABLES.keys():
        if f"${variable}$" in format_template:
            variables.append(variable)

    return variables

#CUSTOM ADDED
#CLEANS STRING FROM GARBAGE

def sanitize_filename(filename: str, replace_with: str = '') -> str:
    """
    Entfernt alle Windows-Illegal-Characters aus einem Dateinamen.
    Optional können sie durch ein Ersatzzeichen ersetzt werden (z.B. '-').
    
    Args:
        filename (str): Original-Dateiname
        replace_with (str): Zeichen, das illegal ersetzt; Standard '' (löschen)
    
    Returns:
        str: Bereinigter Dateiname
    """
    # Liste der illegalen Windows-Zeichen
    illegal_chars = '<>:"/\\|?*'
    
    sanitized = ''.join(replace_with if c in illegal_chars else c for c in filename)
    
    # Optional: führende/trailing Leerzeichen entfernen
    sanitized = sanitized.strip()
    
    return sanitized


def clean_optional_from_format(formatted_string: str) -> str:
    # Erase entire optional section if there is an unused variable
    formatted_string = re.sub(r"\{.*\$\w+\$.*\}", "", formatted_string)

    # Remove any remaining curly braces
    formatted_string = formatted_string.replace(r"{", "").replace(r"}", "")
    return formatted_string



def apply_format(format_template: str, stash: StashInterface, scene_data, file_data)-> str:
    # Only template groups containing placeholders are optional. Literal braces
    # in actual directory names must survive (including Windows sandbox paths).
    def value(variable):
        if variable in FILE_VARIABLES:
            result = FILE_VARIABLES[variable](stash, file_data)
        elif variable in SCENE_VARIABLES:
            result = SCENE_VARIABLES[variable](stash, scene_data)
        else:
            raise ValueError(f"Unknown filename variable: {variable}")
        if result is None or result == "":
            raise ValueError(f"Missing filename variable: {variable}")
        return sanitize_filename(str(result)) if variable in {"title", "studio_name"} else str(result)

    def render(text):
        return re.sub(r"\$(\w+)\$", lambda m: value(m.group(1)), text)

    def optional(match):
        try:
            return render(match.group(1))
        except ValueError:
            return ""

    result = re.sub(r"\{([^{}]*\$\w+\$[^{}]*)\}", optional, format_template)
    return render(result)


class StashFile:
    def __init__(self, stash: StashInterface, config, scene_data, file_data):
        self.stash = stash
        self.config = config
        self.scene_data = scene_data
        self.file_data = file_data
        self.duplicate_index = 0

    def get_old_file_path(self) -> pathlib.Path:
        return pathlib.Path(self.file_data["path"]).absolute()

    def get_new_file_folder(self) -> pathlib.Path:
        if self.config.default_directory_path_format:
            format_template = self._replace_tag_replace_placeholder(
                self.config.default_directory_path_format
            )
            directory_path = apply_format(
                format_template,
                self.stash,
                self.scene_data,
                self.file_data
            )
            return pathlib.Path(directory_path).absolute()
        return pathlib.Path(self.file_data["path"]).parent.absolute()

    def get_new_file_name(self) -> str:
        if not self.config.default_file_name_format:
            return self.file_data["basename"]

        file_data = {**self.file_data, "index": self.duplicate_index}
        format_template = self._replace_tag_replace_placeholder(
            self.config.default_file_name_format
        )
        file_name = apply_format(format_template, self.stash, self.scene_data, file_data)

        if self.duplicate_index:
            duplicate_suffix = apply_format(self.config.duplicate_file_suffix, self.stash, self.scene_data, file_data)
            base, ext = file_name.rsplit(".", 1)
            file_name = f"{base}{duplicate_suffix}.{ext}"

        if not self.config.allow_unsafe_characters:
            file_name = re.sub(r"[<>:\"/\\|?*]", "", file_name)

        if self.config.remove_extra_spaces_from_file_name:
            file_name = re.sub(r"\s+", " ", file_name)

        return file_name

    def _replace_tag_replace_placeholder(self, format_template: str) -> str:
        if TAG_REPLACE_PLACEHOLDER not in format_template:
            return format_template
        return format_template.replace(
            TAG_REPLACE_PLACEHOLDER,
            self._get_tag_replace_prefix()
        )

    def _get_tag_replace_prefix(self) -> str:
        configured_pairs = self.config.tag_replace or ""
        scene_tag_names = {
            tag.get("name", "").casefold()
            for tag in self.scene_data.get("tags", [])
            if tag.get("name")
        }
        replacements = []

        for configured_pair in re.split(r"[;,]", configured_pairs):
            if not configured_pair.strip():
                continue

            tag_name, separator, replacement = configured_pair.partition("==")
            if not separator or not tag_name.strip() or not replacement.strip():
                log.warning(f"Invalid tag replacement setting, expected \"tag\"==\"replacement\": {configured_pair}")
                continue

            tag_name = tag_name.strip()
            replacement = replacement.strip()
            if tag_name[0] == tag_name[-1] and tag_name[0] in ("\"", "'"):
                tag_name = tag_name[1:-1]
            if replacement[0] == replacement[-1] and replacement[0] in ("\"", "'"):
                replacement = replacement[1:-1]

            if tag_name.casefold() in scene_tag_names and replacement:
                replacements.append(replacement)

        return "".join(replacements)

    def get_new_file_path(self) -> pathlib.Path:
        return self.get_new_file_folder() / self.get_new_file_name()

    def rename_related_files(self, old_path: pathlib.Path, new_path: pathlib.Path, dry_run: bool):
        if not self.config.rename_related_files:
            return
        old_directory = old_path.parent
        new_directory = new_path.parent

        # Include files with same stem + screenshot.jpg
        related_files = [
            path for path in old_directory.iterdir()
            if path != old_path and (path.stem == old_path.stem or path.name.lower().startswith("fanart") or path.name.lower().startswith("poster") or path.name.lower().startswith("movie"))
        ]

        if not related_files:
            return

        for related_file in related_files:
            # Preserve extension of related file
            target_path = new_directory / f"{new_path.stem}{related_file.suffix}" \
                if related_file.stem == old_path.stem else new_directory / related_file.name

            if related_file == target_path:
                continue

            if target_path.exists():
                log.warning(f"Related file already exists at {target_path}, skipping rename for {related_file}")
                continue

            log.info(f"Renaming related file from {related_file} to {target_path}")

            if dry_run:
                continue  # don't actually move files in dry run

            create_target_folder(target_path.parent)

            # Safe move with copy + delete fallback
            try:
                related_file.rename(target_path)
            except OSError:
                import shutil
                log.warning(f"Cross-device rename failed, copying instead: {related_file} -> {target_path}")
                shutil.copy2(related_file, target_path)
                if target_path.exists():
                    related_file.unlink()
                    
    def delete_empty_folders_upwards(self, folder_path: pathlib.Path, dry_run: bool, stop_at: pathlib.Path | None = None):
        """
        Löscht einen Ordner und alle übergeordneten Ordner, falls sie leer sind.
        
        :param folder_path: Startordner, der überprüft werden soll
        :param dry_run: Wenn True, wird nur angezeigt, was passieren würde
        :param stop_at: Optionaler Pfad, bei dem das Löschen stoppen soll
        """
        current = folder_path.parent.resolve()

        # Gehe rekursiv nach oben
        while True:
            if not current.is_dir():
                log.info(f"Delete empty folder: {current} is no folder!")
                break
                
            # Stop-Bedingung
            if stop_at and current == stop_at.resolve():
                log.info(f"Delete empty folder: stop folder reached: {current}")
                break

            # Ordner prüfen
            if any(current.iterdir()):
                # Ordner nicht leer → stoppen
                log.warning(f"Delete empty folder: {current} is not empty!")
                break

            if dry_run:
                log.info(f"Delete empty folder: Folder deleted: {current}")
                log.info(f"Delete empty folder: Dry run enabled, not actually deleting folder")
            else:
                current.rmdir()
                log.info(f"Delete empty folder: Folder deleted: {current}")

            # Gehe einen Ordner nach oben
            current = current.parent


    def rename_file(self):
        old_path = self.get_old_file_path()
        new_path = self.get_new_file_path()

        if not old_path.exists():
            log.warning(f"File for scene does not exist on disk: {old_path}")
            return

        if old_path == new_path:
            log.info("File paths are the same, no renaming needed.")
            return

        # Handle duplicates
        while new_path.exists():
            self.duplicate_index += 1
            log.warning(f"File already exists at {new_path}, adding duplicate suffix: {self.duplicate_index}")
            new_path = self.get_new_file_path()
            if old_path == new_path:
                log.info("File paths are the same after adding duplicate suffix, no renaming needed.")
                return

        log.info(f"Renaming file from {old_path} to {new_path}")

        if self.config.dry_run:
            log.info("Dry run enabled, not actually renaming the file.")
            self.rename_related_files(old_path, new_path, dry_run=True)
            self.delete_empty_folders_upwards(old_path, dry_run=True, stop_at=pathlib.Path("/downloads/media/whisparr"))
            return

        # Move the main file via Stash GraphQL
        create_target_folder(self.get_new_file_folder())
        moved_file = self.stash.call_GQL(
            MOVE_FILE_MUTATION,
            {
                "input": {
                    "ids": [self.file_data["id"]],
                    "destination_folder": self.get_new_file_folder(),
                    "destination_basename": self.get_new_file_name(),
                }
            }
        )
        log.info(f"File renamed successfully: {moved_file}")

        # Rename related files manually
        self.rename_related_files(old_path, new_path, dry_run=False)
        self.delete_empty_folders_upwards(old_path, dry_run=False, stop_at=pathlib.Path("/downloads/media/whisparr"))
