"""Build a private settings map locally. Does not contact or modify Stash."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow.settings_migration import migrate_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", type=Path, help="Private JSON object containing current plugin settings"
    )
    parser.add_argument("output", type=Path, help="New private JSON file; must not exist")
    parser.add_argument(
        "--shared-config", type=Path, help="Reviewed shared credentials for conflicting old values"
    )
    args = parser.parse_args()
    settings = json.loads(args.input.read_text(encoding="utf-8-sig"))
    shared = (
        json.loads(args.shared_config.read_text(encoding="utf-8-sig"))
        if args.shared_config
        else None
    )
    result = migrate_settings(settings, shared)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(f"Prepared {len(result)} settings locally; no server changes.")


if __name__ == "__main__":
    main()
