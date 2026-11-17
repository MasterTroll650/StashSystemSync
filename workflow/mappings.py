"""Transactional imports and per-key SQLite performer mappings."""

import hashlib
import json
from collections.abc import MutableMapping

from .state import fingerprint


class PerformerMappings(MutableMapping):
    def __init__(self, state):
        self.state = state

    def __getitem__(self, key):
        value = self.state.get("performer_mapping", str(key))
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        if not str(key).isdigit() or not isinstance(value, str) or not value:
            raise ValueError("Invalid performer mapping")
        self.state.put("performer_mapping", str(key), value)

    def __delitem__(self, key):
        self[key]
        self.state.remove("performer_mapping", str(key))

    def __iter__(self):
        return iter(
            [
                row[0]
                for row in self.state.db.execute(
                    "SELECT key FROM kv WHERE scope='performer_mapping' ORDER BY key"
                )
            ]
        )

    def __len__(self):
        return self.state.db.execute(
            "SELECT count(*) FROM kv WHERE scope='performer_mapping'"
        ).fetchone()[0]


def import_json_mappings(state):
    for filename, scope, marker in [
        ("performer-map.json", "performer_mapping", "performer_imported"),
        ("whisparr-cache.import.json", "whisparr_mapping", "whisparr_imported"),
    ]:
        if state.get("migration", marker):
            continue
        source = state.directory / filename
        if not source.exists():
            continue
        data = source.read_bytes()
        values = json.loads(data)
        if not isinstance(values, dict):
            raise ValueError("Mapping import must be an object: " + filename)
        for key, value in values.items():
            valid_value = (
                isinstance(value, str) and bool(value)
                if scope == "performer_mapping"
                else type(value) is int and value > 0
            )
            if not str(key).isdigit() or int(key) <= 0 or not valid_value:
                raise ValueError("Invalid mapping import: " + filename)
        with state.transaction():
            if state.get("migration", marker):
                continue
            inserted = 0
            for key, value in values.items():
                if state.get(scope, key) is not None:
                    continue
                if scope == "whisparr_mapping" and state.get("whisparr_deleted", key) is not None:
                    continue
                state.put(scope, key, value)
                inserted += 1
            state.put(
                "migration",
                marker + "_receipt",
                {
                    "source_count": len(values),
                    "inserted": inserted,
                    "preserved": len(values) - inserted,
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
            state.put("migration", marker, True)


def mapping_status(state):
    result = {}
    for label, scope, marker in [
        ("performer", "performer_mapping", "performer_imported"),
        ("whisparr", "whisparr_mapping", "whisparr_imported"),
    ]:
        values = {
            row[0]: json.loads(row[1])
            for row in state.db.execute("SELECT key,value FROM kv WHERE scope=?", (scope,))
        }
        result[label] = {
            "count": len(values),
            "sha256": fingerprint(values),
            "import_complete": bool(state.get("migration", marker)),
            "receipt": state.get("migration", marker + "_receipt"),
        }
    result["integrity"] = state.db.execute("PRAGMA quick_check").fetchone()[0]
    return result
