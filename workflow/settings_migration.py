"""Explicit, offline migration; never read legacy files at runtime."""

from .config import Config
from .settings import CANONICAL_KEYS, SETTING_KEYS

OLD_CREDENTIALS = {
    "jellyfin_url": ("export_JELLYFIN_URL", "performer_jellyfin_url"),
    "jellyfin_api_key": ("export_JELLYFIN_API_KEY", "performer_jellyfin_api_key"),
    "jellyfin_username": (
        "export_JELLYFIN_USER",
        "performer_jellyfin_user_id",
        "jellyfin_user_id",
        "20_03_jellyfin_user_id",
    ),
}


def migrate_settings(settings, shared=None):
    shared = shared or {}
    if set(shared) - set(OLD_CREDENTIALS):
        raise ValueError("Unknown shared Jellyfin setting")
    retired = {key for values in OLD_CREDENTIALS.values() for key in values} | {
        "export_STASH_API_KEY"
    }
    canonical = {}
    for key, value in settings.items():
        target = CANONICAL_KEYS.get(key, key)
        if target in SETTING_KEYS:
            if target in canonical and canonical[target] != value:
                raise ValueError("Conflicting duplicate setting: " + target)
            canonical[target] = value
        elif key not in retired:
            raise ValueError("Unknown setting requires review: " + key)
    for target, sources in OLD_CREDENTIALS.items():
        if target in shared:
            canonical[target] = shared[target]
            continue
        if target == "jellyfin_username" and any(
            settings.get(key)
            for key in ("performer_jellyfin_user_id", "jellyfin_user_id", "20_03_jellyfin_user_id")
        ):
            raise ValueError(
                "Conflicting Jellyfin settings require review: jellyfin_username; resolve the saved user ID to its username"
            )
        candidates = [canonical.get(target), *(settings.get(key) for key in sources)]
        candidates = [str(value) for value in candidates if value not in (None, "")]
        compared = (
            [value.rstrip("/") for value in candidates] if target == "jellyfin_url" else candidates
        )
        if len(set(compared)) > 1:
            raise ValueError("Conflicting Jellyfin settings require review: " + target)
        canonical[target] = candidates[0] if candidates else ""
    return Config(canonical).as_settings()
