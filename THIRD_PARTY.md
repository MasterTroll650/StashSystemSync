# Third-party origins

The workflow engine, service integration, durable queue, SQLite migration,
packaging and new changes are maintained by MasterTroll650.

The legacy helpers originated in user-maintained local Stash plugins:

| File | Origin |
| --- | --- |
| workflow/legacy/rename_config.py | Rename File on Update config parser |
| workflow/legacy/renamer.py | Rename File on Update file manager |
| workflow/legacy/performer.py | User-maintained Jellyfin performer synchronization |
| workflow/legacy/cleanup.py | User-maintained empty-folder cleanup |

Rename File on Update: https://github.com/stash-of-awesomeness/stash-plugins/tree/main/plugins/rename-file-on-update
Its MIT copyright and permission notice is retained in
[LICENSES/rename-file-on-update-MIT.txt](LICENSES/rename-file-on-update-MIT.txt).
Original headers are retained. The helpers have been adapted for the coordinated
workflow, explicit configuration, strict ID checking and SQLite storage.

Runtime dependencies are requests (Apache-2.0), stashapp-tools (MIT),
PyYAML (MIT), Pillow (HPND) and resvg_py (MIT, with resvg dependency notices).
Installed distributions retain their own license metadata. The distribution
bundles the original pip wheel (MIT); its license notices are inside the wheel.

- https://github.com/psf/requests
- https://github.com/stashapp/stashapp-tools
- https://github.com/yaml/pyyaml
- https://github.com/python-pillow/Pillow
- https://github.com/baseplate-admin/resvg-py
- https://github.com/pypa/pip
