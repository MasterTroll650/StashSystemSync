import pytest
from conftest import FakeJellyfin, FakeWhisparr

from plugin import run
from workflow import engine as engine_module
from workflow.engine import Engine
from workflow.logging import overall_message
from workflow.state import State


def test_overall_progress_survives_workers_and_database_reopen(setup, monkeypatch, capsys):
    stash, config, original, root = setup
    folder = original.directory
    original.close()
    clock = [0.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])
    config.values["worker_seconds"] = 1
    identifiers = []
    fractions = []
    for batch in range(3):
        state = State(folder)
        try:
            if batch == 0:
                for sid in [1, 2, 3]:
                    state.enqueue("scene", sid)
            worker = Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state))

            def process(job):
                clock[0] += 2
                return {"scene_id": job["id"]}

            worker.process = process
            result = worker.drain(wait=False)
            progress = result["overall"]
            identifiers.append(progress["id"])
            fractions.append(progress["fraction"])
            assert progress["done"] == batch + 1 and progress["total"] == 3
            messages = capsys.readouterr().err
            assert ("\x01p\x021.0000" in messages) is (batch == 2)
        finally:
            state.close()
    assert len(set(identifiers)) == 1
    assert fractions == pytest.approx([1 / 3, 2 / 3, 1])


def test_child_jobs_increase_total_and_hooks_are_coalesced(tmp_path):
    state = State(tmp_path)
    try:
        state.enqueue("scene", "1")
        for _ in range(10):
            state.enqueue("scene", "1")
        assert state.progress()["total"] == 1
        job = state.next()
        state.enqueue("performer", "7")
        state.complete(job, active_seconds=2)
        progress = state.progress()
        assert progress["total"] == 2 and progress["done"] == 1 and progress["fraction"] == 0.5
    finally:
        state.close()


def test_obsolete_revision_is_not_counted_as_finished(tmp_path):
    state = State(tmp_path)
    try:
        state.enqueue("scene", "1")
        job = state.next()
        state.enqueue("scene", "1")
        state.complete(job, active_seconds=1)
        assert state.progress()["done"] == 0 and state.progress()["pending"] == 1
        state.complete(state.next())
        assert state.progress()["done"] == 1 and state.progress()["total"] == 1
    finally:
        state.close()


def test_failed_jobs_prevent_full_progress_and_retry_keeps_run(tmp_path):
    state = State(tmp_path)
    try:
        state.enqueue("scene", "1")
        state.enqueue("scene", "2")
        state.complete(state.next())
        job = state.next()
        state.fail(job, "problem", 1)
        progress = state.progress()
        assert progress["status"] == "blocked" and progress["fraction"] == 0.5
        assert progress["failed"] == 1 and "Fehler" in overall_message(progress)
        state.retry_failed()
        assert state.progress()["id"] == progress["id"]
        state.complete(state.next())
        assert state.progress()["percent"] == 100 and state.progress()["total"] == 2
    finally:
        state.close()


def test_scene_deletion_accounts_for_cancelled_scene_job(tmp_path):
    state = State(tmp_path)
    try:
        state.enqueue("scene", "1")
        state.enqueue("scene_delete", "1")
        progress = state.progress()
        assert progress["total"] == 2 and progress["cancelled"] == 1
        assert progress["fraction"] == 0.5
        state.complete(state.next(), {"skipped": "not in Whisparr"})
        progress = state.progress()
        assert progress["done"] == 1 and progress["skipped"] == 1 and progress["percent"] == 100
    finally:
        state.close()


def test_collection_and_partial_failure_do_not_report_completion(tmp_path):
    state = State(tmp_path)
    try:
        state.begin_collection()
        state.enqueue("scene", "1")
        state.complete(state.next())
        progress = state.progress()
        assert progress["status"] == "collecting" and progress["fraction"] < 1
        state.end_collection(False)
        assert state.progress()["status"] == "incomplete" and state.progress()["percent"] < 100
        state.begin_collection()
        state.enqueue("scene", "2")
        state.end_collection(True)
        assert state.progress()["id"] == progress["id"]
        state.complete(state.next())
        assert state.progress()["total"] == 2 and state.progress()["status"] == "complete"
    finally:
        state.close()


def test_another_finished_wave_starts_new_run_but_status_keeps_last_run(tmp_path):
    state = State(tmp_path)
    try:
        assert state.progress()["status"] == "idle"
        state.enqueue("scene", "1")
        state.complete(state.next())
        last = state.progress()
        assert state.progress()["id"] == last["id"]
        state.enqueue("performer", "2")
        progress = state.progress()
        assert progress["id"] != last["id"] and progress["done"] == 0 and progress["total"] == 1
    finally:
        state.close()


def test_existing_queue_can_be_adopted_without_replacing_jobs(tmp_path):
    state = State(tmp_path)
    try:
        state.db.execute("INSERT INTO jobs(kind,id,rev,due) VALUES('scene','old',3,0)")
        before = dict(state.next())
        progress = state.progress()
        assert progress["adopted_queue"] and progress["done"] == 0 and progress["total"] == 1
        assert dict(state.next()) == before
    finally:
        state.close()


def test_eta_uses_persisted_processing_time_and_due_delay(tmp_path):
    state = State(tmp_path, clock=lambda: 100)
    try:
        for sid in range(4):
            state.enqueue("scene", sid)
        for _ in range(3):
            state.complete(state.next(), active_seconds=10)
        assert state.progress()["remaining_seconds"] == 10
        state.fail(state.next(), "temporary", 5, retry_after=120)
        assert state.progress()["remaining_seconds"] == 130
    finally:
        state.close()
    state = State(tmp_path, clock=lambda: 160)
    try:
        assert state.progress()["remaining_seconds"] == 70
    finally:
        state.close()


def test_progress_does_not_round_to_100_with_remaining_work(tmp_path):
    state = State(tmp_path)
    try:
        state.enqueue("scene", "last")
        run_data = state.get("progress", "run")
        run_data.update(done=9999, total=10000)
        state.put("progress", "run", run_data)
        progress = state.progress()
        assert progress["percent"] == 99.9 and progress["fraction"] == 0.99
    finally:
        state.close()


def test_producer_exception_preserves_jobs_and_marks_incomplete(setup):
    stash, config, state, root = setup

    def scenes():
        yield {"id": "1"}
        raise RuntimeError("source unavailable")

    stash.scenes = scenes
    with pytest.raises(RuntimeError, match="source unavailable"):
        run({"args": {"mode": "enqueue_all"}}, state.directory, stash)
    assert state.progress()["status"] == "incomplete"
    assert state.db.execute("SELECT id FROM jobs").fetchone()[0] == "1"


def test_progress_task_works_without_server_or_media_operations(tmp_path, capsys):
    result = run({"args": {"mode": "progress"}}, tmp_path)
    assert result["status"] == "idle"
    assert "\x01i\x02Gesamt:" in capsys.readouterr().err


def test_rolling_upgrade_accounts_for_jobs_finished_by_old_worker(tmp_path):
    state = State(tmp_path)
    try:
        state.db.executemany(
            "INSERT INTO jobs(kind,id,rev,due) VALUES('scene',?,1,0)", [("1",), ("2",)]
        )
        initial = state.progress()
        state.db.execute("DELETE FROM jobs WHERE id='1'")
        progress = state.progress()
        assert progress["id"] == initial["id"] and progress["inherited"] == 1
        assert progress["fraction"] == 0.5 and progress["done"] == 0
        state.complete(state.next())
        assert state.progress()["percent"] == 100
        state.enqueue("performer", "next")
        assert state.progress()["id"] != initial["id"] and state.progress()["total"] == 1
    finally:
        state.close()
