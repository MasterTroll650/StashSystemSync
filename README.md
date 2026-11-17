# Stash System Sync

One coordinated workflow for Stash file organization, Jellyfin and Whisparr.
Scene updates are coalesced, work is stored in SQLite, and completed stages are
remembered so repeated hooks do not repeat every operation.

## Install from the Stash plugin list

In **Settings → Plugins → Available Plugins → Add Source**, enter:

- Name: `Stash System Sync`
- Source URL: `https://raw.githubusercontent.com/MasterTroll650/StashSystemSync/refs/heads/main/index.yml`
- Local Path: `StashSystemSync`

Open the source, select **Stash System Sync**, and install it. Reload plugins.
Python 3.10 or newer must be available to Stash; Python 3.13 is recommended.
The first task installs missing Python dependencies privately in `data/python/`.
It needs HTTPS access to PyPI and a writable plugin data directory. No global
Python installation is modified. The package includes a verified pip wheel,
so system pip is not required.

**Dry run is enabled by default.** Configure your paths and services and inspect
the logs before enabling productive operation. Do not run the old individual
plugins alongside their replacements.

## Updates and existing data

Use **Settings → Plugins → Installed Plugins → Update** to install new versions.
Settings and `data/workflow.sqlite` remain in place. Version 0.2.1 removes the
completed one-time StashWorkflow migration task; it does not delete settings,
mappings, migration receipts or private backups. No task needs to be run after
updating. Migration from the older StashWorkflow name is available only in the
previous 0.2.0 release for installations that still need it.

## Features

- Rename and move video files and matching sidecars using Stash's native API.
- Export scene NFO, poster and fanart files and notify Jellyfin with translated paths.
- Sync performer details and images through Jellyfin's API; SVG is rendered to PNG.
- Sync favorites, skipping people absent from Jellyfin with an Info message.
- Sync Whisparr records and monitoring; conflicting identities are logged without automatic reassignment.
- Clean affected empty source directories and optionally run manual folder cleanup.
- Durable queue, bounded retries, status/progress logging and per-stage checkpoints.

There is no direct export into Jellyfin's People folder and no People mount is
needed. All Stash→Whisparr and Stash-performer→Jellyfin mappings are in
`data/workflow.sqlite`. Back up this database and your Stash configuration.

## Settings

Settings labels are English and ordered as General, Rename, Jellyfin connection,
Jellyfin export, Performer sync, Whisparr bridge and Folder cleanup.
Jellyfin URL, API key and username are entered once. The internal user ID is
resolved automatically. See [all settings](docs/SETTINGS.md).

Tasks operate only on what you request. **Queue all scenes** is an explicit
library-wide operation; installation and updates do not start it.

## Development and releases

Install `requirements-dev.txt`, then run:

```text
python -m ruff check workflow tests plugin.py tools
python -m pytest -q
python tools/build_package.py
```

The builder uses an explicit file allowlist: credentials, databases, private
mappings, backups, logs, virtual environments and test artifacts are excluded.
Release packages have a SHA-256 checksum in the Stash source index.
Push a matching `v<version>` tag after updating `release.json`, the manifest and
`workflow/logging.py` to run tests, publish the package and update `index.yml`.

See [THIRD_PARTY.md](THIRD_PARTY.md) for origins and license notices.
