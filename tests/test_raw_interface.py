import json
import os
import subprocess
import sys


def test_raw_status_outputs_single_json_and_never_needs_server(tmp_path):
    result = subprocess.run(
        [sys.executable, "plugin.py"],
        input=json.dumps({"args": {"mode": "status"}}),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "STASH_SYSTEM_SYNC_DATA": str(tmp_path)},
    )
    output = json.loads(result.stdout)
    assert output["output"]["jobs"] == []
    assert result.stderr.startswith("\x01i\x02Status:")
    assert result.stdout.count("\n") == 1


def test_imported_cache_is_available_without_contacting_whisparr(tmp_path):
    from plugin import run
    from workflow.state import State

    (tmp_path / "whisparr-cache.import.json").write_text('{"123":456}')
    run({"args": {"mode": "status"}}, tmp_path)
    state = State(tmp_path)
    try:
        assert state.get("whisparr_mapping", "123") == 456
    finally:
        state.close()
