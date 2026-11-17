import re
import time
from pathlib import Path

from .config import boolean
from .files import Files, atomic_write, lines, nfo
from .logging import VERSION, Log, entity, overall_message
from .services import Jellyfin, Performer, Whisparr
from .state import fingerprint


class Engine:
    def __init__(self, stash, config, state, roots, jellyfin=None, whisparr=None, performer=None):
        self.stash, self.config, self.state = stash, config, state
        self.files = Files(stash, config, state, roots)
        self.jellyfin = jellyfin or Jellyfin(config)
        self.whisparr = whisparr or Whisparr(config, state, roots)
        self.performer = performer or Performer(
            stash, config, state, getattr(self.jellyfin, "user_id", None)
        )

    def entity_label(self, kind, entity_id):
        scope = {
            "performer": "performer_name",
            "scene": "scene_title",
            "scene_delete": "scene_title",
        }.get(kind)
        name = self.state.get(scope, entity_id) if scope else None
        return entity(kind, entity_id, name)

    def step(self, scope, name, value, action, valid=None):
        digest = fingerprint(value)
        if self.state.get(scope, name) == digest and (valid is None or valid()):
            return False
        action()
        self.state.put(scope, name, digest)
        return True

    def scene(self, scene_id):
        scene = self.stash.find_scene(scene_id)
        if not scene:
            return {"skipped": "scene no longer exists"}
        self.state.put("scene_title", scene_id, scene.get("title") or "")
        if not scene.get("files"):
            return {"skipped": "scene has no files"}
        if boolean(self.config.values.get("wait_for_organized")) and not scene.get("organized"):
            return {"deferred": "waiting for a subsequent organized update"}
        scope = "scene:" + str(scene_id)
        generation = self.state.get("image_generation", scope, 0)
        if self.config.dry():
            return {"dry_run": True, "files": self.files.plan(scene)}
        # Metadata first, using the original Stash hook cookie to preserve hook context.
        if (
            self.config.enabled("export")
            and not self.config.dry("export")
            and scene.get("interactive")
        ):
            self.stash.add_tag(scene, "Funscript")
            scene = self.stash.find_scene(scene_id)
        plan = self.files.organize(scene)
        if any(item["dry"] for item in plan):
            return {"dry_run": True, "files": plan}
        scene = self.stash.find_scene(scene_id)  # Never export the pre-move path snapshot.
        if not scene or not scene.get("files"):
            raise RuntimeError("Scene disappeared after move")
        export_files = [f for f in scene["files"] if self.files.exportable(scene, f)]
        exported_changed = False
        # The filesystem is checked even when a previous run completed; deleted output is recreated.
        if self.config.enabled("export") and not self.config.dry("export"):
            for file in export_files:
                exported_changed |= atomic_write(
                    Path(file["path"]).parent / "movie.nfo", nfo(scene, file)
                )
            screenshot = (scene.get("paths") or {}).get("screenshot")
            image_value = {
                "source": screenshot,
                "generation": generation,
                "paths": [str(Path(f["path"]).parent) for f in export_files],
            }

            def valid_images():
                return all(
                    (Path(f["path"]).parent / name).exists()
                    and (Path(f["path"]).parent / name).stat().st_size >= 1024
                    for f in export_files
                    for name in ["poster.jpg", "fanart.jpg"]
                )

            def images():
                if not export_files:
                    return
                if not screenshot:
                    raise RuntimeError("Screenshot is not yet available")
                url = (
                    screenshot.rstrip("/") + "/screenshot.jpg"
                    if not re.search(r"\.(?:jpg|png|webp)(?:\?|$)", screenshot)
                    else screenshot
                )
                data = self.stash.bytes(url)
                if len(data) < 1024:
                    raise RuntimeError("Screenshot is too small or incomplete")
                from .legacy.performer import detect_image_mime

                if not detect_image_mime(data, ""):
                    raise RuntimeError("Screenshot response is not an image")
                for file in export_files:
                    atomic_write(Path(file["path"]).parent / "poster.jpg", data)
                    atomic_write(Path(file["path"]).parent / "fanart.jpg", data)

            exported_changed |= self.step(scope, "images", image_value, images, valid_images)
            # A failed notification never marks this step complete, even when all local output exists.
            notify_value = {
                "scene": {
                    k: scene.get(k)
                    for k in ["title", "details", "date", "studio", "performers", "tags"]
                },
                "paths": [f["path"] for f in export_files],
                "images": image_value,
                "credentials": self.config.hash_section("export", self.state),
                "path_mappings": self.config.values["jellyfin_path_mappings"],
            }
            if export_files:
                if exported_changed:
                    self.state.remove(scope, "notify")

                def notify_jellyfin():
                    paths = self.jellyfin.notify([f["path"] for f in export_files])
                    if paths is not None:
                        self.state.put(scope, "notify_paths", paths)

                notified = self.step(
                    scope,
                    "notify",
                    notify_value,
                    notify_jellyfin,
                )
                if notified and self.config.enabled("performer"):
                    for person in scene.get("performers", []):
                        self.state.enqueue("performer", person["id"], 2)
        if self.config.enabled("whisparr"):
            value = {
                "title": scene.get("title"),
                "files": [f["path"] for f in scene["files"]],
                "stash_ids": scene.get("stash_ids"),
                "settings": self.config.hash_section("whisparr", self.state),
                "mappings": self.config.values["whisparr_path_mappings"],
            }
            if not self.config.dry("whisparr"):
                self.step(
                    scope,
                    "whisparr",
                    value,
                    lambda: self.whisparr.sync(scene),
                    lambda: (
                        self.state.get("whisparr_mapping", scene_id) is not None
                        or not self.whisparr.stash_id(scene)
                    ),
                )
        self.files.clean_old_parents(plan)
        return {"scene_id": str(scene_id), "files": len(scene["files"])}

    def person(self, performer_id):
        if not self.config.enabled("performer") or self.config.dry("performer"):
            return {"dry_run": True}
        person = self.performer.read(performer_id)
        if not person:
            return {"skipped": "performer no longer exists"}
        self.state.put("performer_name", performer_id, person.get("name") or "")
        scope = "performer:" + str(performer_id)
        value = {
            k: v
            for k, v in person.items()
            if k not in {"updated_at", "created_at", "o_counter", "scene_count", "play_count"}
        }
        generation = self.state.get("image_generation", scope, 0)
        digest = fingerprint([value, generation, self.config.hash_section("performer", self.state)])
        if self.state.get(scope, "sync") == digest:
            return {"skipped": "unchanged performer"}
        self.performer.sync(performer_id)
        # Saving our own Jellyfin profile link can emit one more performer event.
        current = self.performer.read(performer_id)
        value = {
            k: v
            for k, v in current.items()
            if k not in {"updated_at", "created_at", "o_counter", "scene_count", "play_count"}
        }
        self.state.put(
            scope,
            "sync",
            fingerprint([value, generation, self.config.hash_section("performer", self.state)]),
        )
        return {"performer_id": str(performer_id)}

    def process(self, job):
        if job["kind"] == "scene":
            return self.scene(job["id"])
        if job["kind"] == "performer":
            return self.person(job["id"])
        if job["kind"] == "scene_delete":
            if self.config.enabled("whisparr"):
                if self.config.dry("whisparr"):
                    return {"dry_run": True, "would_unmonitor": job["id"]}
                result = self.whisparr.unmonitor(job["id"])
                if result is not None:
                    return result
            return {"unmonitored": job["id"]}
        if job["kind"] == "favorites":
            people = self.stash.gql("query {allPerformers {id name favorite}}")["allPerformers"]
            result = self.jellyfin.favorites(people)
            return {"favorites": result}
        if job["kind"] == "rebuild":
            return {"mappings": self.whisparr.rebuild(self.stash.scenes())}
        if job["kind"] == "cleanup":
            from .legacy import cleanup

            settings = self.config.sections["cleanup"]
            destination = settings.get("DELETE_PATH")
            if not destination or not Path(destination).is_dir():
                raise ValueError("Cleanup DELETE_PATH must be an existing directory")
            dry = self.config.dry("cleanup")
            total = 0
            while True:
                count = cleanup.find_and_move_empty_folders(
                    destination,
                    [{"path": str(p)} for p in self.files.roots],
                    dry,
                    settings.get("SIZE_LIMIT_MB", 50),
                    settings.get("DELETE_FOLDER_NAME") or "delete",
                    boolean(settings.get("INCLUDE_FILE_FOLDERS"), True),
                    lines(settings.get("EXCLUDED_PATHS")),
                )
                total += count
                if dry or count == 0:
                    break
            return {"cleanup_candidates" if dry else "cleanup_moved": total}
        raise ValueError("Unknown job kind")

    def drain(self, wait=True, sleep=time.sleep):
        processed = 0
        errors = []
        deferred = 0
        started = time.monotonic()
        budget = self.config.number("worker_seconds", 1)
        deadline = started + budget
        log = Log(self.config)
        interval = self.config.number("log_progress_seconds", 5, 300)
        last_report = started
        last_processed = 0
        last_entity = "keine"

        last_overall_message = None

        def report(label):
            nonlocal last_overall_message
            counts = self.state.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(failed),0), "
                "COALESCE(SUM(kind='scene' AND failed=0),0), "
                "COALESCE(SUM(kind='performer' AND failed=0),0) FROM jobs"
            ).fetchone()
            log.info(
                f"{label}: Abschnitt erledigt={processed}, zurueckgestellt={deferred}, "
                f"Fehlversuche={len(errors)}; offen={counts[0] - counts[1]} "
                f"(Szenen={counts[2]}, Darsteller={counts[3]}), "
                f"fehlgeschlagen={counts[1]}; zuletzt {last_entity}"
            )
            overall = self.state.progress()
            overall_text = overall_message(overall)
            if overall_text != last_overall_message:
                log.info(overall_text)
                log.progress(overall["fraction"])
                last_overall_message = overall_text

        with self.state.worker_lock() as acquired:
            if not acquired:
                log.debug("Arbeitsabschnitt nicht gestartet: Ein anderer Arbeiter ist aktiv")
                return {"busy": True}
            log.info(
                f"StashSystemSync {VERSION}: Arbeitsabschnitt gestartet; "
                f"Modus={'Testlauf' if self.config.dry() else 'Produktiv'}, "
                f"Organized-Abwarten={boolean(self.config.values['wait_for_organized'])}"
            )
            report("Start")
            existing = self.state.db.execute(
                "SELECT kind,id,attempts,failed,error FROM jobs WHERE error IS NOT NULL "
                "ORDER BY kind,id LIMIT 10"
            ).fetchall()
            for job in existing:
                log.warning(
                    f"Gespeicherter Fehler: {self.entity_label(job['kind'], job['id'])}; "
                    f"Versuche={job['attempts']}; {job['error']}"
                )
            while time.monotonic() < deadline:
                job = self.state.next()
                if job is None and (not wait or not self.state.pending()):
                    break
                now = time.monotonic()
                if now - last_report >= interval or processed - last_processed >= 100:
                    report("Fortschritt")
                    last_report, last_processed = now, processed
                if job is None:
                    sleep(0.25)
                    continue
                try:
                    job_started = time.monotonic()
                    last_entity = self.entity_label(job["kind"], job["id"])
                    result = self.process(job)
                    last_entity = self.entity_label(job["kind"], job["id"])
                    self.state.complete(job, result, time.monotonic() - job_started)
                    processed += 1
                    if isinstance(result, dict) and result.get("deferred"):
                        deferred += 1
                    log.debug(f"Abgearbeitet: {last_entity}")
                except Exception as exc:
                    last_entity = self.entity_label(job["kind"], job["id"])
                    error = self.config.scrub(exc)
                    self.state.fail(
                        job,
                        error,
                        int(self.config.number("max_attempts", 1, 20)),
                        retry_after=getattr(exc, "retry_after", 0),
                        active_seconds=time.monotonic() - job_started,
                    )
                    errors.append({"kind": job["kind"], "id": job["id"], "error": error})
                    attempts = job["attempts"] + 1
                    limit = int(self.config.number("max_attempts", 1, 20))
                    disposition = (
                        "endgueltig fehlgeschlagen"
                        if attempts >= limit
                        else "Wiederholung vorgesehen"
                    )
                    log.error(
                        f"Fehler: {last_entity}; Versuch={attempts}/{limit}; {disposition}; {error}"
                    )
            pending = self.state.worker_finished()
            report("Arbeitsabschnitt beendet")
        return {
            "processed": processed,
            "errors": errors,
            "pending": pending,
            "overall": self.state.progress(),
        }
