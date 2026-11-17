import subprocess
import sys

from conftest import FakeJellyfin, FakeWhisparr

from plugin import run
from workflow.config import Config
from workflow.engine import Engine
from workflow.state import State


def test_repeated_hooks_are_coalesced_and_one_worker_is_scheduled(setup):
    stash, config, state, root = setup
    for _ in range(6):
        run(
            {"args": {"hookContext": {"type": "Scene.Update.Post", "id": "1"}}},
            state.directory,
            stash,
        )
    assert state.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert state.db.execute("SELECT rev FROM jobs").fetchone()[0] == 6
    assert stash.scheduled == 1


def test_update_during_processing_is_not_lost(setup):
    stash, config, state, root = setup
    jf = FakeJellyfin()
    original = jf.notify

    def notify(paths):
        original(paths)
        state.enqueue("scene", "1")

    jf.notify = notify
    state.enqueue("scene", "1")
    result = Engine(stash, config, state, [root], jf, FakeWhisparr(state)).drain(wait=False)
    assert result["processed"] == 2 and not state.pending()
    assert len(jf.calls) == 1 and len(stash.moves) == 1


def test_retry_is_bounded_and_successful_steps_are_retained(setup):
    stash, config, state, root = setup
    clock = [100.0]
    state.clock = lambda: clock[0]
    config.values["max_attempts"] = 2
    jf = FakeJellyfin()
    jf.fail = 2
    pipeline = Engine(stash, config, state, [root], jf, FakeWhisparr(state))
    state.enqueue("scene", "1")
    pipeline.drain(wait=False)
    assert state.pending() == 1
    clock[0] += 3
    pipeline.drain(wait=False)
    row = state.db.execute("SELECT * FROM jobs").fetchone()
    assert row["failed"] == 1 and row["attempts"] == 2
    assert not state.pending() and len(stash.moves) == 1 and len(stash.downloads) == 1
    state.retry_failed()
    pipeline.drain(wait=False)
    assert not state.db.execute("SELECT 1 FROM jobs").fetchone()
    assert len(stash.moves) == 1


def test_destroy_event_cancels_pending_update(setup):
    stash, config, state, root = setup
    state.enqueue("scene", "1")
    state.enqueue("scene_delete", "1")
    state.enqueue("scene", "1")
    assert [row["kind"] for row in state.db.execute("SELECT * FROM jobs")] == ["scene_delete"]


def test_worker_lock_is_cross_process(setup):
    stash, config, state, root = setup
    script = "from workflow.state import State; import sys; s=State(sys.argv[1]); c=s.worker_lock(); print(c.__enter__()); c.__exit__(None,None,None); s.close()"
    with state.worker_lock() as acquired:
        assert acquired
        result = subprocess.run(
            [sys.executable, "-c", script, str(state.directory)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"


def test_queue_survives_process_restart(tmp_path):
    a = State(tmp_path)
    a.enqueue("performer", "42")
    a.close()
    b = State(tmp_path)
    try:
        assert b.next()["id"] == "42"
    finally:
        b.close()


def test_settings_preserve_false_values_and_dry_run_defaults():
    config = Config(
        {
            "whisparr_MONITORED": False,
            "cleanup_SIZE_LIMIT_MB": "20",
            "cleanup_DRY_RUN": False,
        }
    )
    assert config.sections["whisparr"]["MONITORED"] is False
    assert config.sections["cleanup"]["SIZE_LIMIT_MB"] == "20"
    assert config.dry("cleanup")
    changed = Config({"dry_run": False, "whisparr_MONITORED": False})
    assert changed.sections["whisparr"]["MONITORED"] is False


def test_status_works_without_network_or_server_connection(tmp_path):
    assert run({"args": {"mode": "status"}}, tmp_path)["jobs"] == []


def test_manual_retry_recovers_stale_worker_reservation(setup):
    stash, config, state, root = setup
    assert state.request_worker()
    state.enqueue("scene", "1")
    state.db.execute("UPDATE jobs SET failed=1")
    run({"args": {"mode": "retry"}}, state.directory, stash)
    assert stash.scheduled == 1 and state.pending() == 1


def test_empty_numeric_setting_falls_back_to_default():
    config = Config({"debounce_seconds": "", "request_timeout": None})
    assert config.number("debounce_seconds") == 3
    assert config.number("request_timeout") == 20
