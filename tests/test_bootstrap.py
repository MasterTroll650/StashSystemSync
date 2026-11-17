from workflow import bootstrap


def test_ready_environment_does_not_install_or_create_private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "_ready", lambda: True)
    bootstrap.ensure_dependencies(tmp_path)
    assert not (tmp_path / "data").exists()


def test_dependency_installer_uses_private_target_and_keeps_stdout_clean(
    tmp_path, monkeypatch, capsys
):
    ready = iter([False, False, True])
    monkeypatch.setattr(bootstrap, "_ready", lambda: next(ready))
    (tmp_path / "requirements.txt").write_text("requests")
    commands = []

    class Result:
        returncode = 0

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["capture_output"]
        return Result()

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    # Do not clear modules already loaded by the test runner.
    monkeypatch.setattr(bootstrap, "MODULES", ())
    bootstrap.ensure_dependencies(tmp_path)
    assert str(tmp_path / "data" / "python") in commands[0]
    assert capsys.readouterr().out == ""
