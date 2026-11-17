import os
import shutil
import json
import sys
from stashapi.stashapp import StashInterface
from stashapi import log

DEFAULT_SIZE_LIMIT_MB = 50
DEFAULT_DELETE_FOLDER_NAME = "delete"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".3gp", ".ts", ".qt", ".m4v"
}


def normalize_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def is_same_path(path_a, path_b):
    return normalize_path(path_a) == normalize_path(path_b)


def is_inside_path(path, parent_path):
    try:
        return os.path.commonpath([normalize_path(path), normalize_path(parent_path)]) == normalize_path(parent_path)
    except ValueError:
        return False


def create_delete_folder(delete_path, delete_folder_name=DEFAULT_DELETE_FOLDER_NAME, dry_run=False):
    delete_folder = os.path.join(delete_path, delete_folder_name)
    if not os.path.exists(delete_folder) and not dry_run:
        os.makedirs(delete_folder, exist_ok=True)
    elif dry_run:
        log.info(f"[Dry Run] Delete folder would be created: {delete_folder}")

    delete_empty_folder = os.path.join(delete_folder, "empty")
    if not os.path.exists(delete_empty_folder) and not dry_run:
        os.makedirs(delete_empty_folder, exist_ok=True)
    elif dry_run:
        log.info(f"[Dry Run] Delete empty folder would be created: {delete_empty_folder}")

    delete_files_folder = os.path.join(delete_folder, "files")
    if not os.path.exists(delete_files_folder) and not dry_run:
        os.makedirs(delete_files_folder, exist_ok=True)
    elif dry_run:
        log.info(f"[Dry Run] Delete files folder would be created: {delete_files_folder}")

    return delete_folder, delete_empty_folder, delete_files_folder


def get_folder_size(folder):
    total_size = 0
    for dirpath, _, filenames in os.walk(folder, onerror=lambda e: log.error(f"Cannot scan {folder}: {e}")):
        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            try:
                if os.path.exists(file_path):
                    total_size += os.path.getsize(file_path)
            except OSError as e:
                log.error(f"Cannot read file size for {file_path}: {e}")
    return total_size


def contains_video(folder):
    try:
        for entry in os.scandir(folder):
            if entry.is_file():
                _, ext = os.path.splitext(entry.name.lower())
                if ext in VIDEO_EXTENSIONS:
                    return True
    except OSError as e:
        log.error(f"Cannot scan folder for video files {folder}: {e}")
    return False


def safe_relative_path(path, base_path):
    try:
        relative_path = os.path.relpath(path, base_path)
    except ValueError:
        return os.path.basename(path)
    if relative_path in (".", ""):
        return os.path.basename(path)
    return relative_path


def unique_target_path(target_path):
    if not os.path.exists(target_path):
        return target_path

    parent = os.path.dirname(target_path)
    name = os.path.basename(target_path)
    for index in range(1, 1000):
        candidate = os.path.join(parent, f"{name}_{index}")
        if not os.path.exists(candidate):
            return candidate

    raise FileExistsError(f"Could not find unique target path for {target_path}")


def build_target_path(root, library_root, target_root, dry_run=False):
    relative_path = safe_relative_path(root, library_root)
    target_path = os.path.join(target_root, relative_path)

    if dry_run:
        return target_path if not os.path.exists(target_path) else f"{target_path}_<next-number>"

    target_parent = os.path.dirname(target_path)
    os.makedirs(target_parent, exist_ok=True)
    return unique_target_path(target_path)


def should_skip_library_path(path):
    if not path:
        log.error("Library path is empty, skipping")
        return True
    if not os.path.exists(path):
        log.error(f"Library path does not exist, skipping: {path}")
        return True
    if not os.path.isdir(path):
        log.error(f"Library path is not a folder, skipping: {path}")
        return True
    return False


def parse_bool(raw_value, default=False):
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw_value)


def parse_size_limit_mb(raw_value):
    if raw_value in (None, ""):
        return DEFAULT_SIZE_LIMIT_MB
    try:
        size_limit_mb = int(raw_value)
    except (TypeError, ValueError):
        log.error(f"Invalid SIZE_LIMIT_MB value, using default {DEFAULT_SIZE_LIMIT_MB}: {raw_value}")
        return DEFAULT_SIZE_LIMIT_MB
    if size_limit_mb < 0:
        log.error(f"Negative SIZE_LIMIT_MB value, using default {DEFAULT_SIZE_LIMIT_MB}: {raw_value}")
        return DEFAULT_SIZE_LIMIT_MB
    return size_limit_mb


def normalize_excluded_paths(raw_value):
    if not raw_value:
        return []
    if isinstance(raw_value, str):
        return [path.strip() for path in raw_value.splitlines() if path.strip()]
    if isinstance(raw_value, list):
        return [path for path in raw_value if path]
    log.error(f"Unsupported EXCLUDED_PATHS value, ignoring: {raw_value}")
    return []


def find_and_move_empty_folders(
    delete_path,
    library_paths,
    dry_run=False,
    size_limit_mb=DEFAULT_SIZE_LIMIT_MB,
    delete_folder_name=DEFAULT_DELETE_FOLDER_NAME,
    include_file_folders=True,
    excluded_paths=None,
):
    delete_folder, delete_empty_folder, delete_files_folder = create_delete_folder(delete_path, delete_folder_name, dry_run)
    size_limit = parse_size_limit_mb(size_limit_mb) * 1024 * 1024
    excluded_paths = excluded_paths or []
    skipped_paths = [delete_folder] + excluded_paths
    moved_count = 0
    found_count = 0

    for lib_path in library_paths:
        library_root = lib_path.get("path")
        if should_skip_library_path(library_root):
            continue

        library_root = os.path.abspath(library_root)
        log.info(f"Search library: {library_root}")

        for root, dirs, files in os.walk(
            library_root,
            topdown=False,
            onerror=lambda e: log.error(f"Cannot scan library path {library_root}: {e}"),
        ):
            if is_same_path(root, library_root):
                continue
            if any(is_inside_path(root, skipped_path) for skipped_path in skipped_paths):
                continue

            should_move = False
            target_path = ""

            if not dirs:
                if not files:
                    target_path = build_target_path(root, library_root, delete_empty_folder, dry_run)
                    should_move = True
                elif include_file_folders:
                    if contains_video(root):
                        continue
                    folder_size = get_folder_size(root)
                    if folder_size < size_limit:
                        target_path = build_target_path(root, library_root, delete_files_folder, dry_run)
                        should_move = True

                if should_move:
                    found_count += 1
                    if dry_run:
                        log.info(f"[Dry Run] Folder found: {root} -> would be moved to {target_path}")
                    else:
                        try:
                            shutil.move(root, target_path)
                            log.info(f"Folder moved: {root} -> {target_path}")
                            moved_count += 1
                        except Exception as e:
                            log.error(f"Error moving {root}: {e}")

    return found_count if dry_run else moved_count


def main():
    STASH_DATA = json.loads(sys.stdin.read())
    stash = StashInterface(STASH_DATA["server_connection"])

    library_paths = stash.get_configuration().get("general", {}).get("stashes", [])
    plugin_cfg = stash.get_configuration().get("plugins", {}).get("stashEmptyFolderMover", {})
    delete_path = plugin_cfg.get("DELETE_PATH")
    dry_run = parse_bool(plugin_cfg.get("DRY_RUN"), default=False)
    size_limit_mb = plugin_cfg.get("SIZE_LIMIT_MB", DEFAULT_SIZE_LIMIT_MB)
    delete_folder_name = plugin_cfg.get("DELETE_FOLDER_NAME") or DEFAULT_DELETE_FOLDER_NAME
    include_file_folders = parse_bool(plugin_cfg.get("INCLUDE_FILE_FOLDERS"), default=True)
    excluded_paths = normalize_excluded_paths(plugin_cfg.get("EXCLUDED_PATHS"))

    if not delete_path or not os.path.exists(delete_path):
        log.error(f"Delete path is invalid or does not exist: {delete_path}")
        return

    log.info(
        f"Delete path: {delete_path}, dry run: {dry_run}, size limit: {size_limit_mb} MB, "
        f"include file folders: {include_file_folders}"
    )

    total_moved = 0
    while True:
        moved_count = find_and_move_empty_folders(
            delete_path,
            library_paths,
            dry_run=dry_run,
            size_limit_mb=size_limit_mb,
            delete_folder_name=delete_folder_name,
            include_file_folders=include_file_folders,
            excluded_paths=excluded_paths,
        )
        total_moved += moved_count
        if moved_count == 0 or dry_run:
            action = "found" if dry_run else "moved"
            log.info(f"Done! Number of folders {action}: {total_moved}")
            break


if __name__ == "__main__":
    main()

