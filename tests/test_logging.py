import io
import json
import sys

import pytest
from conftest import FakeJellyfin, FakeWhisparr

import plugin
from workflow import engine as engine_module
from workflow.engine import Engine
from workflow.logging import Log


def pipeline(setup):
    stash, config, state, root = setup
    return Engine(stash, config, state, [root], FakeJellyfin(), FakeWhisparr(state))


def test_logs_use_stash_levels_and_redact_secrets_and_control_characters(setup, capsys):
    stash, config, state, root = setup
    config.sections["export"]["JELLYFIN_API_KEY"] = "private-secret"
    log = Log(config)
    log.error("Szene ID=42 private-secret https://localhost/?api_key=other-secret\n\x01i\x02fake")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("\x01e\x02Szene ID=42")
    assert "private-secret" not in out.err and "other-secret" not in out.err
    assert "[redacted]" in out.err and out.err.count("\n") == 1
    assert out.err.count("\x01") == 1 and out.err.count("\x02") == 1


def test_errors_are_visible_with_ids_and_retry_does_not_remove_job(setup, capsys):
    stash, config, state, root = setup
    state.enqueue("performer", "1833")
    state.enqueue("scene", "42")
    worker = pipeline(setup)

    def process(job):
        if job["kind"] == "performer":
            raise RuntimeError("https://localhost/?api_key=do-not-log")
        return {"deferred": "waiting for organized"}

    worker.process = process
    result = worker.drain(wait=False)
    messages = capsys.readouterr().err
    assert "\x01e\x02Fehler: Darsteller ID=1833; Versuch=1/5" in messages
    assert "Wiederholung vorgesehen" in messages and "do-not-log" not in messages
    assert result["processed"] == 1 and result["pending"]
    assert "zurueckgestellt=1" in messages
    row = state.db.execute("SELECT * FROM jobs").fetchone()
    assert row["id"] == "1833" and row["attempts"] == 1 and row["failed"] == 0


@pytest.mark.parametrize("total, seconds_per_job", [(3, 10), (102, 0)])
def test_progress_is_reported_by_time_or_batch_size(
    setup, monkeypatch, capsys, total, seconds_per_job
):
    stash, config, state, root = setup
    config.values["worker_seconds"] = 300
    config.values["log_progress_seconds"] = 15
    clock = [0.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])
    for sid in range(total):
        state.enqueue("scene", sid)
    worker = pipeline(setup)

    def process(job):
        clock[0] += seconds_per_job
        return {"scene_id": job["id"]}

    worker.process = process
    result = worker.drain(wait=False)
    messages = capsys.readouterr().err
    assert messages.count("\x01i\x02Fortschritt:") == 1
    assert "Arbeitsabschnitt gestartet" in messages and "Arbeitsabschnitt beendet" in messages
    assert "\x01p\x021.0000" in messages
    assert result["processed"] == total and not result["pending"]


def test_saved_errors_are_logged_without_resetting_failed_jobs(setup, capsys):
    stash, config, state, root = setup
    state.enqueue("scene", "19121")
    old = state.next()
    state.fail(old, "Missing filename variable: studio_name", 1)
    before = dict(state.db.execute("SELECT * FROM jobs").fetchone())
    result = pipeline(setup).drain(wait=False)
    messages = capsys.readouterr().err
    assert "\x01w\x02Gespeicherter Fehler: Szene ID=19121" in messages
    assert "studio_name" in messages and "fehlgeschlagen=1" in messages
    assert dict(state.db.execute("SELECT * FROM jobs").fetchone()) == before
    assert result["processed"] == 0


def test_worker_raw_json_and_stderr_logging_are_separate(setup, monkeypatch, capsys):
    stash, config, state, root = setup
    state.enqueue("scene", "1")
    original = plugin.run
    monkeypatch.setattr(plugin, "run", lambda payload: original(payload, state.directory, stash))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"args":{"mode":"worker"}}'))
    assert plugin.main() == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["output"]["processed"] == 1
    assert output.out.count("\n") == 1 and "\x01i\x02" in output.err
    assert not stash.moves


def test_broken_logging_stream_cannot_fail_processing(monkeypatch):
    class BrokenStream:
        def write(self, value):
            raise BrokenPipeError("log unavailable")

        def flush(self):
            pass

    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "stderr", BrokenStream())
        Log().info("message")


def test_performer_errors_use_name_when_read_before_failure(setup, capsys):
    stash, config, state, root = setup
    config.values["performer_enabled"] = True
    worker = pipeline(setup)

    class Performer:
        def read(self, performer_id):
            return {"id": performer_id, "name": "Test Name"}

        def sync(self, performer_id):
            raise RuntimeError("Upload failed")

    worker.performer = Performer()
    state.enqueue("performer", "2388")
    worker.drain(wait=False)
    messages = capsys.readouterr().err
    assert 'Fehler: Darsteller "Test Name" (ID=2388)' in messages
    assert state.get("performer_name", "2388") == "Test Name"


def test_scene_errors_include_title_and_id(setup, capsys):
    stash, config, state, root = setup
    worker = pipeline(setup)

    def fail(scene):
        raise RuntimeError("Move failed")

    worker.files.organize = fail
    state.enqueue("scene", "1")
    worker.drain(wait=False)
    assert 'Fehler: Szene "A & B" (ID=1)' in capsys.readouterr().err
    assert state.get("scene_title", "1") == "A & B"


def test_scene_label_refreshes_title_and_survives_worker_restart(setup):
    stash, config, state, root = setup
    stash.scene["files"] = []
    worker = pipeline(setup)
    worker.scene("1")
    assert worker.entity_label("scene", "1") == 'Szene "A & B" (ID=1)'
    stash.scene["title"] = "Updated title"
    worker.scene("1")
    restarted = pipeline(setup)
    assert restarted.entity_label("scene_delete", "1") == 'Szenenloeschung "Updated title" (ID=1)'
    assert restarted.entity_label("scene", "missing") == "Szene ID=missing"
