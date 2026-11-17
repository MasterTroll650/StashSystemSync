#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

from workflow.bootstrap import ensure_dependencies

ensure_dependencies(Path(__file__).resolve().parent)

from workflow.client import Stash  # noqa: E402
from workflow.config import Config, boolean  # noqa: E402
from workflow.engine import Engine  # noqa: E402
from workflow.logging import Log, entity, overall_message  # noqa: E402
from workflow.mappings import import_json_mappings, mapping_status  # noqa: E402
from workflow.state import State, fingerprint  # noqa: E402

DATA = Path(os.environ.get("STASH_SYSTEM_SYNC_DATA", Path(__file__).resolve().parent / "data"))


def run(payload, data=DATA, stash=None):
    args = payload.get("args") or {}
    mode = args.get("mode", "")
    state = State(data)
    try:
        import_json_mappings(state)
        if mode == "mapping_status":
            return mapping_status(state)
        if mode == "image_environment":
            import importlib.util
            import platform
            import sysconfig

            return {
                "python": platform.python_version(),
                "platform": sysconfig.get_platform(),
                "libc": platform.libc_ver(),
                "pillow": importlib.util.find_spec("PIL") is not None,
            }
        if mode == "status":
            jobs = [
                dict(job)
                for job in state.db.execute("SELECT kind,id,attempts,failed,error FROM jobs")
            ]
            progress = state.progress()
            log = Log(Config())
            failed = sum(bool(job["failed"]) for job in jobs)
            log.info(
                f"Status: Warteschlange={len(jobs)}, offen={len(jobs) - failed}, fehlgeschlagen={failed}; {overall_message(progress)}"
            )
            for job in jobs:
                if job["error"]:
                    scope = {
                        "performer": "performer_name",
                        "scene": "scene_title",
                        "scene_delete": "scene_title",
                    }.get(job["kind"])
                    name = state.get(scope, job["id"]) if scope else None
                    log.warning(
                        f"Status: {entity(job['kind'], job['id'], name)}; Versuche={job['attempts']}; {job['error']}"
                    )
            return {"jobs": jobs, "dry_run_default": True, "overall": progress}
        if mode == "progress":
            progress = state.progress()
            Log().info(overall_message(progress))
            return progress
        stash = stash or Stash(payload.get("server_connection") or {})
        configuration = stash.configuration()
        config = Config.load(configuration)
        if mode == "image_preview":
            import hashlib
            import io

            from workflow.images import is_svg, prepare_image
            from workflow.services import Performer

            performer_id = args.get("performer_id")
            if not performer_id:
                raise ValueError("performer_id is required")
            person = Performer(stash, config, state).read(performer_id)
            if not person or not person.get("image_path"):
                raise ValueError("Performer image missing")
            source = stash.bytes(person["image_path"])
            image_bytes = prepare_image(source)
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as image:
                dimensions = list(image.size)
                image.verify()
            return {
                "performer_id": str(performer_id),
                "source_svg": is_svg(source),
                "content_type": "image/png",
                "dimensions": dimensions,
                "bytes": len(image_bytes),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
            }
        if mode == "settings_audit":
            return {
                "settings_sha256": fingerprint(config.as_settings()),
                "checkpoint_sha256": {
                    section: fingerprint(config.hash_section(section, state))
                    for section in ("export", "performer", "whisparr")
                },
                "dry_run": config.dry(),
                "wait_for_organized": boolean(config.values["wait_for_organized"]),
            }
        if mode == "adopt_settings_layout":
            config.adopt_layouts(args.get("layouts") or {}, state)
            return {"settings_layout_adopted": True}
        stash.timeout = config.number("request_timeout", 1)
        roots = [
            p["path"] for p in configuration.get("general", {}).get("stashes", []) if p.get("path")
        ]
        hook = args.get("hookContext") or {}
        event = hook.get("type", "")
        if event:
            if event == "Scene.Update.Post":
                kind = "scene"
            elif event == "Scene.Destroy.Post":
                kind = "scene_delete"
            elif event in {"Performer.Create.Post", "Performer.Update.Post"}:
                kind = "performer"
            else:
                return {"skipped": "unsupported hook"}
            if hook.get("id") is None:
                raise ValueError("Hook ID missing")
            fields = set(hook.get("inputFields") or [])
            state.enqueue(
                kind,
                hook["id"],
                config.number("debounce_seconds", 0, 60),
                bool(fields & {"cover_image", "image", "image_path"}),
            )
        elif mode in {"enqueue_all", "rename-all", "organize_all_scenes"}:
            log = Log(config)
            log.info(
                "Bibliotheksabgleich: Szenen werden in die vorhandene Warteschlange eingereiht"
            )
            count = 0
            state.begin_collection()
            complete = False
            try:
                for scene in stash.scenes():
                    state.enqueue("scene", scene["id"])
                    count += 1
                    if count % 500 == 0:
                        log.info(f"Bibliotheksabgleich: {count} Szenen eingereiht")
                complete = True
            finally:
                state.end_collection(complete)
            log.info(f"Bibliotheksabgleich: Einreihen abgeschlossen; Szenen={count}")
        elif mode in {"favorites", "sync_favorite_performers_to_jellyfin"}:
            state.enqueue("favorites", "all")
        elif mode in {"rebuild", "rebuild_cache"}:
            state.enqueue("rebuild", "all")
        elif mode == "cleanup":
            state.enqueue("cleanup", "all")
        elif mode == "retry":
            state.retry_failed()
            # An explicitly requested retry must also recover a cancelled worker's stale reservation.
            state.remove("worker", "scheduled_until")
        elif mode == "scene":
            if not args.get("scene_id"):
                raise ValueError("scene_id is required")
            state.enqueue("scene", args["scene_id"], force=boolean(args.get("force")))
        elif mode == "performer":
            if not args.get("performer_id"):
                raise ValueError("performer_id is required")
            state.enqueue("performer", args["performer_id"], force=boolean(args.get("force")))
        elif mode != "worker":
            raise ValueError("Choose a plugin task or provide mode")
        if mode == "worker":
            result = Engine(stash, config, state, roots).drain()
            if result.get("pending") and state.request_worker():
                try:
                    stash.schedule()
                except Exception:
                    state.remove("worker", "scheduled_until")
                    raise
            return result
        if state.request_worker():
            try:
                stash.schedule()
            except Exception:
                state.remove("worker", "scheduled_until")
                raise
        return {"queued": True, "dry_run": config.dry()}
    finally:
        state.close()


def main():
    try:
        payload = json.loads(sys.stdin.read())
        result = run(payload)
        print(json.dumps({"output": result}, ensure_ascii=False))
    except Exception as exc:
        Log().error(
            f"Plugin-Aufgabe abgebrochen: {type(exc).__name__}; Warteschlange bleibt gespeichert"
        )
        # Never include HTTP exception URLs or imported secrets in the raw task output.
        print(
            json.dumps(
                {
                    "error": type(exc).__name__
                    + ": workflow task failed; inspect local queue status"
                },
                ensure_ascii=False,
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
