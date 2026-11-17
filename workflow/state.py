import contextlib
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class State:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(
            self.directory / "workflow.sqlite", timeout=30, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs(kind TEXT,id TEXT,rev INTEGER NOT NULL,due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0,error TEXT,PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS kv(scope TEXT,key TEXT,value TEXT,PRIMARY KEY(scope,key));
        """)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, scope, key, default=None):
        row = self.db.execute(
            "SELECT value FROM kv WHERE scope=? AND key=?", (scope, str(key))
        ).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, scope, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO kv VALUES(?,?,?)",
            (scope, str(key), json.dumps(value, default=str)),
        )

    def remove(self, scope, key):
        self.db.execute("DELETE FROM kv WHERE scope=? AND key=?", (scope, str(key)))

    def _progress_run(self, new_work=False):
        """Called inside the queue transaction; no job schema changes are needed."""
        run = self.get("progress", "run")
        count = self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if run is None or (
            new_work and not count and not run["collectors"] and not run["collection_failed"]
        ):
            run = {
                "id": uuid.uuid4().hex,
                "started_at": self.clock(),
                "total": count,
                "done": 0,
                "inherited": 0,
                "cancelled": 0,
                "deferred": 0,
                "skipped": 0,
                "planned": 0,
                "active_seconds": 0.0,
                "collectors": 0,
                "collection_failed": False,
                "adopted_queue": bool(count),
            }
        else:
            # During a rolling upgrade the already running old worker can still
            # finish captured jobs. Account for removed rows without inventing
            # processing times or losing their contribution to overall progress.
            observed = run["done"] + run["inherited"] + run["cancelled"] + count
            if observed < run["total"]:
                run["inherited"] += run["total"] - observed
            else:
                run["total"] = observed
        return run

    def begin_collection(self):
        with self.transaction():
            run = self._progress_run(new_work=True)
            if not run["collectors"]:
                run["collection_failed"] = False
            run["collectors"] += 1
            self.put("progress", "run", run)

    def end_collection(self, success=True):
        with self.transaction():
            run = self._progress_run()
            run["collectors"] = max(0, run["collectors"] - 1)
            run["collection_failed"] |= not success
            self.put("progress", "run", run)

    def progress(self):
        with self.transaction():
            counts = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(failed),0), "
                "COALESCE(SUM(kind='scene' AND failed=0),0), "
                "COALESCE(SUM(kind='performer' AND failed=0),0), "
                "MIN(CASE WHEN failed=0 THEN due END) FROM jobs"
            ).fetchone()
            run = self.get("progress", "run")
            if run is None and not counts[0]:
                return {
                    "id": None,
                    "status": "idle",
                    "total": 0,
                    "done": 0,
                    "cancelled": 0,
                    "pending": 0,
                    "failed": 0,
                    "fraction": 0,
                    "percent": None,
                    "remaining_seconds": None,
                }
            run = self._progress_run()
            run["total"] = max(
                run["total"], run["done"] + run["inherited"] + run["cancelled"] + counts[0]
            )
            self.put("progress", "run", run)
            pending = counts[0] - counts[1]
            complete = not counts[0] and not run["collectors"] and not run["collection_failed"]
            settled = run["done"] + run["inherited"] + run["cancelled"]
            fraction = settled / run["total"] if run["total"] else 0
            if complete:
                fraction = 1.0
            remaining = None
            if (
                pending
                and run["done"] >= 3
                and run["active_seconds"] >= 1
                and not run["collectors"]
            ):
                waiting = max(0, (counts[4] or self.clock()) - self.clock())
                remaining = round(run["active_seconds"] / run["done"] * pending + waiting)
            status = (
                "collecting"
                if run["collectors"]
                else "incomplete"
                if run["collection_failed"]
                else "complete"
                if complete
                else "running"
                if pending
                else "blocked"
            )
            return {
                **run,
                "status": status,
                "pending": pending,
                "failed": counts[1],
                "scene_pending": counts[2],
                "performer_pending": counts[3],
                "fraction": min(1.0 if complete else 0.99, fraction),
                "percent": 100.0 if complete else min(99.9, round(fraction * 100, 1)),
                "remaining_seconds": remaining,
            }

    def enqueue(self, kind, entity_id, delay=0, image_changed=False, force=False):
        entity_id = str(entity_id)
        with self.transaction():
            run = self._progress_run(new_work=True)
            if kind == "scene_delete":
                run["cancelled"] += self.db.execute(
                    "DELETE FROM jobs WHERE kind='scene' AND id=?", (entity_id,)
                ).rowcount
            elif (
                kind == "scene"
                and self.db.execute(
                    "SELECT 1 FROM jobs WHERE kind='scene_delete' AND id=?", (entity_id,)
                ).fetchone()
            ):
                return
            present = self.db.execute(
                "SELECT 1 FROM jobs WHERE kind=? AND id=?", (kind, entity_id)
            ).fetchone()
            self.db.execute(
                """INSERT INTO jobs(kind,id,rev,due) VALUES(?,?,1,?)
                ON CONFLICT(kind,id) DO UPDATE SET rev=rev+1,due=excluded.due,failed=0,attempts=0,error=NULL""",
                (kind, entity_id, self.clock() + delay),
            )
            if not present:
                run["total"] += 1
            self.put("progress", "run", run)
            if image_changed or force:
                key = kind + ":" + entity_id
                self.put("image_generation", key, self.get("image_generation", key, 0) + 1)
            if force:
                self.db.execute("DELETE FROM kv WHERE scope=?", (kind + ":" + entity_id,))

    def next(self):
        return self.db.execute(
            'SELECT * FROM jobs WHERE failed=0 AND due<=? ORDER BY CASE kind WHEN "scene_delete" THEN 0 ELSE 1 END,due LIMIT 1',
            (self.clock(),),
        ).fetchone()

    def complete(self, job, result=None, active_seconds=0):
        with self.transaction():
            run = self._progress_run()
            removed = self.db.execute(
                "DELETE FROM jobs WHERE kind=? AND id=? AND rev=?",
                (job["kind"], job["id"], job["rev"]),
            ).rowcount
            if removed:
                run["done"] += 1
                for key in ["deferred", "skipped", "planned"]:
                    field = "dry_run" if key == "planned" else key
                    if isinstance(result, dict) and result.get(field):
                        run[key] += 1
            run["active_seconds"] += max(0, active_seconds)
            self.put("progress", "run", run)

    def fail(self, job, error, max_attempts, retry_after=0, active_seconds=0):
        with self.transaction():
            run = self._progress_run()
            self._fail(job, error, max_attempts, retry_after)
            run["active_seconds"] += max(0, active_seconds)
            self.put("progress", "run", run)

    def _fail(self, job, error, max_attempts, retry_after):
        attempts = job["attempts"] + 1
        self.db.execute(
            "UPDATE jobs SET attempts=?,failed=?,error=?,due=? WHERE kind=? AND id=? AND rev=?",
            (
                attempts,
                int(attempts >= max_attempts),
                str(error)[:1000],
                self.clock() + max(min(30, 2**attempts), min(600, max(0, retry_after))),
                job["kind"],
                job["id"],
                job["rev"],
            ),
        )

    def request_worker(self):
        with self.transaction():
            until = self.get("worker", "scheduled_until", 0)
            if until > self.clock():
                return False
            self.put("worker", "scheduled_until", self.clock() + 120)
            return True

    def worker_finished(self):
        with self.transaction():
            self.remove("worker", "scheduled_until")
            return bool(self.db.execute("SELECT 1 FROM jobs WHERE failed=0 LIMIT 1").fetchone())

    def retry_failed(self):
        self.db.execute(
            "UPDATE jobs SET failed=0,attempts=0,due=?,error=NULL WHERE failed=1", (self.clock(),)
        )

    def pending(self):
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE failed=0").fetchone()[0]

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def worker_lock(self):
        path = self.directory / "worker.lock"
        with path.open("a+b") as stream:
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
            try:
                yield acquired
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
