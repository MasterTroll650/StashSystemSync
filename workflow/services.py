import contextlib
import io
import json
import posixpath
import re
from pathlib import Path

import requests

from .config import boolean
from .errors import RetryLater
from .files import inside, lines
from .legacy import performer as performer_compat
from .logging import Log, entity
from .mappings import PerformerMappings


class HTTP:
    def __init__(self, timeout=20):
        self.session = requests.Session()
        self.timeout = timeout

    def call(self, method, url, headers=None, **kwargs):
        response = self.session.request(
            method, url, headers=headers or {}, timeout=self.timeout, **kwargs
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()


class Jellyfin:
    def __init__(self, config, http=None):
        self.config = config
        self.settings = config.sections["export"]
        self.http = http or HTTP(config.number("request_timeout", 1))
        self._user_cache = None

    def mapped_path(self, path):
        mappings = []
        for line in lines(self.config.values.get("jellyfin_path_mappings")):
            source, separator, target = line.partition("=")
            source = posixpath.normpath(source.strip().replace("\\", "/"))
            target = posixpath.normpath(target.strip().replace("\\", "/"))

            def absolute(value):
                return value.startswith("/") or bool(re.match(r"^[A-Za-z]:/", value))

            if not separator or not absolute(source) or not absolute(target):
                raise ValueError("Jellyfin path mapping must be absolute source=destination")
            mappings.append((source, target))
        if not mappings:
            return str(path)
        normalized = posixpath.normpath(str(path).replace("\\", "/"))
        for source, target in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True):
            prefix = source.rstrip("/") + "/"
            if normalized == source:
                return target
            if normalized.startswith(prefix):
                return posixpath.join(target, normalized[len(prefix) :])
        raise ValueError("No Jellyfin path mapping for scene file")

    def credentials(self):
        url = (self.settings.get("JELLYFIN_URL") or "").rstrip("/")
        key = self.settings.get("JELLYFIN_API_KEY") or ""
        if not url or not key:
            raise ValueError("Jellyfin URL/API key missing")
        return url, {
            "X-Emby-Token": key,
            "Authorization": 'MediaBrowser Client="StashSystemSync", Device="Stash", DeviceId="stash-workflow", Version="0.1", Token="'
            + key
            + '"',
        }

    def notify(self, paths):
        if self.config.dry("export"):
            return
        url, headers = self.credentials()
        mapped = sorted({self.mapped_path(p) for p in paths})
        self.http.call(
            "POST",
            url + "/Library/Media/Updated",
            headers,
            json={"Updates": [{"Path": p} for p in mapped]},
        )
        return mapped

    def refresh(self):
        if self.config.dry("export"):
            return
        url, headers = self.credentials()
        self.http.call("POST", url + "/Library/Refresh", headers)

    def user_id(self, required=False):
        username = str(self.settings.get("JELLYFIN_USER") or "").strip()
        if not username:
            if required:
                raise ValueError("Jellyfin username is required for favorites")
            return ""
        url, headers = self.credentials()
        cache_key = (url, headers["X-Emby-Token"], username)
        if self._user_cache and self._user_cache[0] == cache_key:
            return self._user_cache[1]
        users = self.http.call("GET", url + "/Users", headers)
        if not isinstance(users, list):
            raise ValueError("Invalid Jellyfin user list")
        exact = [user for user in users if user.get("Name") == username]
        matches = exact or [
            user for user in users if str(user.get("Name") or "").casefold() == username.casefold()
        ]
        if len(matches) != 1 or not matches[0].get("Id"):
            raise ValueError("Configured Jellyfin username not found or ambiguous")
        self._user_cache = (cache_key, str(matches[0]["Id"]))
        return self._user_cache[1]

    def favorites(self, performers):
        if self.config.dry("export"):
            return {"dry_run": True}
        url, headers = self.credentials()
        user = self.user_id(required=True)
        summary = {"checked": 0, "updated": 0, "already_current": 0, "missing": 0}
        log = Log(self.config)
        for person in performers:
            if not person.get("favorite"):
                continue
            summary["checked"] += 1
            candidates = performer_compat.jf_search_people(
                url, headers["X-Emby-Token"], person["name"], self.http.timeout, user_id=user
            )
            person_id = performer_compat.pick_person_id_with_stash_verification(
                url,
                headers["X-Emby-Token"],
                candidates,
                person["name"],
                str(person["id"]),
                self.http.timeout,
                user_id=user,
            )
            if not person_id:
                summary["missing"] += 1
                log.info(
                    f"{entity('performer', person['id'], person.get('name'))}: Favorit nicht in Jellyfin gefunden; uebersprungen"
                )
                continue
            dto = self.http.call("GET", url + f"/Users/{user}/Items/{person_id}", headers)
            if not (dto.get("UserData") or {}).get("IsFavorite"):
                self.http.call(
                    "POST",
                    url + f"/Users/{user}/Items/{person_id}/UserData",
                    headers,
                    json={"IsFavorite": True},
                )
                summary["updated"] += 1
            else:
                summary["already_current"] += 1
        log.info(
            f"Favoriten-Abgleich: geprueft={summary['checked']}, aktualisiert={summary['updated']}, bereits aktuell={summary['already_current']}, nicht in Jellyfin={summary['missing']}"
        )
        return summary


class Whisparr:
    def __init__(self, config, state, roots, http=None):
        self.config, self.state, self.roots = config, state, roots
        self.settings = config.sections["whisparr"]
        self.http = http or HTTP(config.number("request_timeout", 1))

    def request(self, method, path, **kwargs):
        base = (self.settings.get("WHISPARR_URL") or "").rstrip("/")
        key = self.settings.get("WHISPARR_API_KEY") or ""
        if not base or not key:
            raise ValueError("Whisparr URL/API key missing")
        return self.http.call(method, base + "/api/v3/" + path, {"X-Api-Key": key}, **kwargs)

    def stash_id(self, scene):
        host = self.settings.get("STASHDB_ENDPOINT_SUBSTR") or "stashdb.org"
        return next(
            (
                s["stash_id"]
                for s in scene.get("stash_ids", [])
                if host in (s.get("endpoint") or "")
            ),
            None,
        )

    def mapped_path(self, directory, root):
        mappings = []
        for line in lines(self.config.values.get("whisparr_path_mappings")):
            source, sep, target = line.partition("=")
            if not sep:
                raise ValueError("Whisparr path mapping must be source=destination")
            mappings.append((source.strip(), target.strip()))
        mappings.extend((str(source), root) for source in self.roots)
        for source, target in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True):
            if inside(directory, source):
                relative = Path(directory).resolve().relative_to(Path(source).resolve()).as_posix()
                return posixpath.join(target.replace("\\", "/"), relative)
        raise ValueError("No Whisparr path mapping for final scene directory")

    def sync(self, scene):
        stash_id = self.stash_id(scene)
        if not stash_id:
            return {"skipped": "no matching StashDB ID"}
        if self.config.dry("whisparr"):
            return {"planned": stash_id}
        matches = self.request("GET", "movie", params={"stashId": stash_id})
        if not isinstance(matches, list) or len(matches) > 1:
            raise RuntimeError("Whisparr scene lookup is ambiguous or invalid")
        existing = matches[0] if matches else None
        profiles = self.request("GET", "qualityprofile")
        folders = self.request("GET", "rootfolder")
        if not profiles or not folders:
            raise RuntimeError("Whisparr quality profile or root folder missing")
        root = (existing or {}).get("rootFolderPath") or folders[0]["path"]
        payload = {
            "title": scene.get("title") or Path(scene["files"][0]["path"]).stem,
            "qualityProfileId": (existing or {}).get("qualityProfileId") or profiles[0]["id"],
            "rootFolderPath": root,
            "monitored": boolean(self.settings.get("MONITORED"), True),
            "foreignId": stash_id,
            "stashId": stash_id,
            "path": self.mapped_path(Path(scene["files"][0]["path"]).parent, root),
        }
        if existing:
            movie_id = existing["id"]
            updates = {k: v for k, v in payload.items() if k not in {"foreignId", "stashId"}}
            # Preserve all unrelated Whisparr fields and only write actual differences.
            if any(
                existing.get(k) != v
                for k, v in payload.items()
                if k not in {"foreignId", "stashId"}
            ):
                self.request("PUT", f"movie/{movie_id}", json={**existing, **updates})
        else:
            # A matching path is a conflict, never sufficient proof of identity.
            movies = self.request("GET", "movie")
            if not isinstance(movies, list):
                raise RuntimeError("Whisparr movie list is invalid")
            target = posixpath.normpath(payload["path"].replace("\\", "/"))
            owners = [
                movie
                for movie in movies
                if posixpath.normpath(str(movie.get("path", "")).replace("\\", "/")) == target
            ]
            if owners:
                ids = ",".join(str(movie.get("id")) for movie in owners)
                Log(self.config).warning(
                    f"Whisparr-Zuordnungskonflikt: {entity('scene', scene['id'], scene.get('title'))}; "
                    f"StashDB-ID={stash_id}; Whisparr-ID={ids}; Zielpfad={target}. "
                    "Externe IDs stimmen nicht ueberein; bitte Zuordnung in Stash/Whisparr "
                    "pruefen. Whisparr-Abgleich uebersprungen; keine automatische Zuordnung "
                    "und keine Aenderung des vorhandenen Eintrags."
                )
                return {
                    "skipped": "Whisparr identity conflict",
                    "scene_id": str(scene["id"]),
                    "whisparr_ids": [movie.get("id") for movie in owners],
                }
            created = self.request(
                "POST",
                "movie",
                json={
                    **payload,
                    "addOptions": {
                        "monitor": "movieOnly" if payload["monitored"] else "none",
                        "searchForMovie": False,
                    },
                },
            )
            movie_id = created["id"]
        self.state.put("whisparr_mapping", str(scene["id"]), movie_id)
        self.state.remove("whisparr_deleted", str(scene["id"]))
        return {"movie_id": movie_id}

    def unmonitor(self, scene_id):
        if self.config.dry("whisparr"):
            return
        movie_id = self.state.get("whisparr_mapping", scene_id)
        if movie_id is None:
            if self.state.get("whisparr_deleted", scene_id) is not None:
                return {"skipped": "already handled"}
            Log(self.config).info(
                f"{entity('scene', scene_id, self.state.get('scene_title', scene_id))}: keine Whisparr-Zuordnung; keine Whisparr-Aktion erforderlich"
            )
            return {"skipped": "no Whisparr mapping"}
        missing = False
        try:
            movie = self.request("GET", f"movie/{movie_id}")
            if movie.get("monitored"):
                self.request("PUT", f"movie/{movie_id}", json={**movie, "monitored": False})
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            missing = True
            Log(self.config).info(
                f"{entity('scene', scene_id, self.state.get('scene_title', scene_id))}: Whisparr ID={movie_id} ist nicht mehr vorhanden; "
                "keine weitere Whisparr-Aktion erforderlich"
            )
        # Record completion before removing the mapping so a crash/repeated destroy event is harmless.
        self.state.put("whisparr_deleted", scene_id, movie_id)
        self.state.remove("whisparr_mapping", scene_id)
        return {"skipped": "Whisparr entry absent"} if missing else {"unmonitored": scene_id}

    def rebuild(self, scenes):
        if self.config.dry("whisparr"):
            return 0
        movies = self.request("GET", "movie")
        ids = {str(m.get("stashId") or m.get("foreignId")): m["id"] for m in movies}
        count = 0
        for scene in scenes:
            sid = self.stash_id(scene)
            if sid and str(sid) in ids:
                self.state.put("whisparr_mapping", scene["id"], ids[str(sid)])
                count += 1
        return count


class Performer:
    def __init__(self, stash, config, state, user_resolver=None):
        self.user_resolver = user_resolver or Jellyfin(config).user_id
        self.stash, self.config, self.state = stash, config, state
        self.module = performer_compat

    def read(self, performer_id):
        connection = self.stash.connection
        cookie = self.module.stash_cookie_from_server_connection(connection)
        return self.module.get_performer(
            self.stash.base,
            cookie,
            int(performer_id),
            int(self.config.number("request_timeout", 1)),
        )

    def sync(self, performer_id):
        if self.config.dry("performer"):
            return
        output = io.StringIO()
        settings = {
            **self.config.sections["performer"],
            "jellyfin_user_id": self.user_resolver(),
            "timeout_seconds": self.config.number("request_timeout", 1),
            "dry_run": False,
        }
        with contextlib.redirect_stdout(output):
            self.module.main(
                {
                    "server_connection": self.stash.connection,
                    "args": {"performer_id": str(performer_id)},
                },
                settings_override=settings,
                mapping_store=PerformerMappings(self.state),
            )
        result = json.loads(output.getvalue().strip().splitlines()[-1])
        if not result.get("ok"):
            if result.get("retry_after"):
                raise RetryLater(
                    result.get("error", "Performer sync failed"), result["retry_after"]
                )
            raise RuntimeError(result.get("error", "Performer sync failed"))
        message = (result.get("output") or {}).get("message", "")
        if "Failed to save Jellyfin URL" in message:
            raise RuntimeError(message)
        if "Uploaded automatically prepared performer image" in message:
            Log(self.config).info(
                f"{entity('performer', performer_id, (result.get('output') or {}).get('performer_name'))}: "
                "Bild automatisch aufbereitet und ueber Jellyfin-API aktualisiert"
            )
        return result
