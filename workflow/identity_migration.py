"""Explicit migration task; old installation and a rollback backup are retained."""

import json
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path

from .config import Config
from .state import State

OLD_ID = "StashWorkflow"
NEW_ID = "StashSystemSync"


def legacy_data_dir(destination):
    plugin_dir = Path(destination).resolve().parent
    candidates = [
        parent / OLD_ID / "data" for parent in (plugin_dir.parent, plugin_dir.parent.parent)
    ]
    matches = [path for path in candidates if (path / "workflow.sqlite").is_file()]
    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one old StashWorkflow installation next to the plugin or its source directory"
        )
    return matches[0]


def migrate(stash, destination):
    source = legacy_data_dir(destination.directory)
    configuration = stash.configuration()
    plugins = configuration.get("plugins") or {}
    settings = plugins.get("settings", plugins)
    if settings.get(NEW_ID):
        raise ValueError("New plugin already has settings; migration will not overwrite them")
    if not isinstance(settings.get(OLD_ID), dict) or not settings[OLD_ID]:
        raise ValueError("Old StashWorkflow settings not found")
    if destination.db.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
        raise ValueError("New plugin has queued work; wait until its dry-run worker finishes")
    flags = {
        plugin["id"]: plugin["enabled"]
        for plugin in stash.gql("query{plugins{id enabled}}")["plugins"]
    }
    if Config(settings[OLD_ID]).as_settings() != settings[OLD_ID]:
        raise ValueError(
            "Upgrade the old installation settings to the current format before migrating"
        )
    original = State(source)
    if not original.get("migration", "performer_imported"):
        original.close()
        raise ValueError(
            "Upgrade the old installation to SQLite performer mappings before migrating"
        )
    backups = destination.directory / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    archive = backups / ("migration-" + uuid.uuid4().hex[:12] + ".zip")

    def enabled(value):
        stash.gql(
            "mutation($map:BoolMap!){setPluginsEnabled(enabledMap:$map)}", {"map": {OLD_ID: value}}
        )

    def configure(value):
        stash.gql(
            'mutation($settings:Map!){configurePlugin(plugin_id:"StashSystemSync",input:$settings)}',
            {"settings": value},
        )

    try:
        with destination.worker_lock() as new_lock, original.worker_lock() as old_lock:
            if not new_lock or not old_lock:
                raise ValueError("A worker is active; retry after it finishes")
            if destination.db.execute(
                "SELECT 1 FROM kv WHERE scope IN ('performer_mapping','whisparr_mapping') LIMIT 1"
            ).fetchone():
                raise ValueError("New plugin already has mappings; refusing to replace them")
            with tempfile.TemporaryDirectory(dir=backups) as temporary:
                old_copy = Path(temporary) / "old.sqlite"
                new_copy = Path(temporary) / "new.sqlite"
                for state, path in ((original, old_copy), (destination, new_copy)):
                    connection = sqlite3.connect(path)
                    state.db.backup(connection)
                    connection.close()
                with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as backup:
                    backup.write(old_copy, "old-workflow.sqlite")
                    backup.write(new_copy, "new-before.sqlite")
                    backup.writestr(
                        "settings.private.json",
                        json.dumps(
                            {"old": settings[OLD_ID], "old_enabled": flags.get(OLD_ID, False)}
                        ),
                    )
                    for name in ("performer-map.json", "whisparr-cache.import.json"):
                        path = source / name
                        if path.is_file():
                            backup.write(path, name)
                with zipfile.ZipFile(archive) as backup:
                    if backup.testzip() is not None:
                        raise RuntimeError("Migration backup verification failed")
                try:
                    if flags.get(OLD_ID):
                        enabled(False)
                    original.db.backup(destination.db)
                    if destination.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Copied database is invalid")
                    destination.remove("worker", "scheduled_until")
                    configure(settings[OLD_ID])
                except Exception:
                    rollback = sqlite3.connect(new_copy)
                    rollback.backup(destination.db)
                    rollback.close()
                    rollback_errors = []
                    try:
                        configure({})
                    except Exception:
                        rollback_errors.append("new settings")
                    if flags.get(OLD_ID):
                        try:
                            enabled(True)
                        except Exception:
                            rollback_errors.append("old plugin enable state")
                    if rollback_errors:
                        raise RuntimeError(
                            "Migration failed; rollback needs review for "
                            + ", ".join(rollback_errors)
                            + "; backup: "
                            + str(archive)
                        ) from None
                    raise
            return {
                "migrated": True,
                "backup": str(archive),
                "old_plugin_disabled": True,
                "productive_settings_preserved": True,
            }
    finally:
        original.close()
