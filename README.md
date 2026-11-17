# Stash System Sync

One coordinated workflow for Stash file organization, Jellyfin and Whisparr.
Scene updates are coalesced, work is stored in SQLite, and completed stages are
remembered so repeated hooks do not repeat every operation.

## Install from the Stash plugin list

In **Settings → Plugins → Available Plugins → Add Source**, enter:

- Name: `Stash System Sync`
- Source URL: `https://raw.githubusercontent.com/MasterTroll650/StashSystemSync/main/index.yml`
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

## Existing StashWorkflow installation

Install the new plugin without changing its settings, then run **Migrate from
Stash Workflow** under the new plugin tasks. The old installation must already
use the current grouped settings and SQLite performer mappings (0.1.13).

The task requires idle workers, creates a private ZIP backup, copies the SQLite
database using SQLite's backup API, transfers all settings unchanged and disables
the old `StashWorkflow` plugin. Productive/dry-run mode is preserved. Existing
pending work is resumed under the new ID; no library scan is started. The old
folder and migration backup remain available. Do not uninstall the old plugin
until you have checked the new status and logs.

The task detects the old `StashWorkflow/data` folder next to the new plugin or
its source directory. It refuses to overwrite an already configured new plugin
or an existing new mapping database. If the copy or settings transfer fails,
the destination database, settings and old enable state are rolled back.

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
library-wide operation; installation and migration do not start it.

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
