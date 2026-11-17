import copy
import re

from .settings import CANONICAL_KEYS, INTERNAL_CREDENTIAL_KEYS, SETTING_KEYS
from .state import fingerprint

DEFAULTS = {
    "jellyfin_url": "",
    "jellyfin_api_key": "",
    "jellyfin_username": "",
    "dry_run": True,
    "debounce_seconds": 3,
    "request_timeout": 20,
    "max_attempts": 5,
    "worker_seconds": 90,
    "rename_enabled": True,
    "export_enabled": True,
    "performer_enabled": True,
    "whisparr_enabled": True,
    "auto_cleanup_enabled": True,
    "whisparr_path_mappings": "",
    "jellyfin_path_mappings": "",
    "wait_for_organized": False,
    "log_progress_seconds": 15,
}


def boolean(value, default=False):
    if value is None:
        return default
    return (
        value
        if isinstance(value, bool)
        else str(value).lower().strip() in {"1", "true", "yes", "on"}
    )


def plugin_settings(configuration):
    plugins = configuration.get("plugins") or {}
    return plugins.get("settings", plugins) if isinstance(plugins, dict) else {}


SECTION_DEFAULTS = {
    "rename": {
        "defaultDirectoryPathFormat": "",
        "defaultFileNameFormat": "",
        "dryRun": False,
        "renameUnorganized": False,
        "renameRelatedFiles": True,
        "removeExtraSpacesFromFileName": False,
        "duplicateFileSuffix": " ($index$)",
        "tagReplace": "",
        "allowUnsafeCharacters": False,
    },
    "export": {
        "ORGANIZED": True,
        "JELLYFIN_URL": "",
        "JELLYFIN_API_KEY": "",
        "JELLYFIN_USER": "",
        "IGNORED_FILE_SUFFIXES": "",
        "EXCLUDED_PATHS": "/people",
        "DRY_RUN": False,
    },
    "performer": {
        "jellyfin_url": "",
        "jellyfin_api_key": "",
        "jellyfin_user_id": "",
        "update_image": True,
        "update_overview": True,
        "add_jellyfin_profile_url": False,
        "dry_run": False,
    },
    "whisparr": {
        "WHISPARR_URL": "",
        "WHISPARR_API_KEY": "",
        "STASHDB_ENDPOINT_SUBSTR": "stashdb.org",
        "MONITORED": True,
    },
    "cleanup": {
        "DELETE_PATH": "",
        "DELETE_FOLDER_NAME": "delete",
        "DRY_RUN": False,
        "INCLUDE_FILE_FOLDERS": True,
        "SIZE_LIMIT_MB": "50",
        "EXCLUDED_PATHS": "",
    },
}


class Config:
    def __init__(self, settings=None):
        self.values = dict(DEFAULTS)
        self.sections = copy.deepcopy(SECTION_DEFAULTS)
        for key, value in (settings or {}).items():
            key = CANONICAL_KEYS.get(key, key)
            if key in DEFAULTS:
                self.values[key] = value
            else:
                for section, defaults in SECTION_DEFAULTS.items():
                    prefix = section + "_"
                    if key.startswith(prefix) and key[len(prefix) :] in defaults:
                        self.sections[section][key[len(prefix) :]] = value
                        break

        for section in INTERNAL_CREDENTIAL_KEYS:
            targets = (
                ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER")
                if section == "export"
                else ("jellyfin_url", "jellyfin_api_key", "jellyfin_user_id")
            )
            for source, target in zip(
                ("jellyfin_url", "jellyfin_api_key", "jellyfin_username"), targets, strict=True
            ):
                self.sections[section][target] = self.values[source]

    @classmethod
    def load(cls, configuration=None, previous_configuration=None):
        # Accept the former call signature during rolling upgrades; never read files.
        if previous_configuration is not None:
            configuration = previous_configuration
        live = plugin_settings(configuration if isinstance(configuration, dict) else {})
        return cls(live.get("StashSystemSync", {}))

    def as_settings(self):
        result = copy.deepcopy(self.values)
        for section, values in self.sections.items():
            for key, value in values.items():
                if key in INTERNAL_CREDENTIAL_KEYS.get(section, set()):
                    continue
                result[section + "_" + key] = "\n".join(value) if isinstance(value, list) else value
        return {SETTING_KEYS[key]: result[key] for key in SETTING_KEYS}

    def hash_section(self, section, state):
        values = self.sections[section]
        layout = state.get("settings_layout", section)
        if layout and layout["effective_sha256"] == fingerprint(values):
            hash_values = {**values, **layout.get("retired", {})}
            result = {key: hash_values[key] for key in layout["keys"]}
            for key in layout["list_keys"]:
                result[key] = [line.strip() for line in result[key].splitlines() if line.strip()]
            return result
        return values

    def adopt_layouts(self, layouts, state):
        # Preserve completed-stage hashes without retaining a second settings source.
        if set(layouts) != {"export", "performer", "whisparr"}:
            raise ValueError("Invalid settings layout sections")
        for section, layout in layouts.items():
            if (
                not {"effective_sha256", "keys", "list_keys"} <= set(layout)
                or not set(layout) <= {"effective_sha256", "keys", "list_keys", "retired"}
                or not set(layout.get("retired", {}))
                <= ({"PEOPLE_DIR"} if section == "export" else set())
                or not set(layout["keys"])
                <= (set(self.sections[section]) | set(layout.get("retired", {})))
                or not set(layout["list_keys"]) <= set(layout["keys"])
                or layout["effective_sha256"] != fingerprint(self.sections[section])
            ):
                raise ValueError("Settings changed during migration")
        with state.transaction():
            for section, layout in layouts.items():
                state.put("settings_layout", section, layout)

    def enabled(self, name):
        return boolean(self.values.get(name + "_enabled"), True)

    def dry(self, section=None):
        if boolean(self.values.get("dry_run"), True):
            return True
        settings = self.sections.get(section, {})
        return boolean(settings.get("dryRun", settings.get("DRY_RUN", settings.get("dry_run"))))

    def number(self, name, minimum=0, maximum=3600):
        try:
            value = float(self.values[name])
        except (ValueError, TypeError):
            value = float(DEFAULTS[name])
        return max(minimum, min(maximum, value))

    def scrub(self, message):
        result = re.sub(
            r"(?i)([?&](?:api_key|apikey|token)=)[^&\s]+", r"\1[redacted]", str(message)
        )
        for settings in self.sections.values():
            for key, value in settings.items():
                if (
                    any(word in key.lower() for word in ("key", "token", "password"))
                    and isinstance(value, str)
                    and value
                ):
                    result = result.replace(value, "[redacted]")
        return result
