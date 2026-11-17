# Settings

Jellyfin credentials are shared across services. Dry run is enabled by default.

## General

- **Dry run** — Preview changes without modifying media, metadata or external services.
- **Wait for Organized** — Process a scene only after it is marked Organized. A later scene update starts processing.
- **Collect updates (seconds)** — Combine rapid updates for the same item before processing.
- **HTTP timeout (seconds)** — Shared request timeout for Stash, Jellyfin and Whisparr.
- **Maximum attempts** — Maximum processing attempts before a job is marked failed.
- **Worker duration (seconds)** — Maximum duration of one worker task. Pending jobs continue in the next task.
- **Log progress every (seconds)** — Minimum 5 seconds. Errors are logged immediately; the progress bar shows overall progress.
## Rename

- **Enabled** — Rename and move scene files using the configured templates.
- **Dry run** — Preview file renames and moves without changing files.
- **Directory template** — Target directory template for scene files.
- **Filename template** — Target filename template for scene files.
- **Include unorganized scenes** — Allow renaming unorganized scenes. The global Organized requirement still takes precedence.
- **Move matching sidecar files** — Rename and move files sharing the video basename, such as .funscript or .vtt files.
- **Duplicate filename suffix** — Suffix for duplicate names. Use $index$ as the counter; default:  ($index$).
- **Remove extra spaces** — Remove redundant spaces from generated filenames.
- **Tag replacements** — Mappings: "Tag"=="Text", separated by commas or semicolons. Insert with $tag_replace$ in templates.
- **Allow unsafe characters** — Allow characters that may be unsupported in file and directory names.
## Jellyfin connection

- **Server URL** — One shared Jellyfin URL for exports, favorites and performer synchronization.
- **API key** — One shared Jellyfin API key for all Jellyfin operations.
- **Username** — Jellyfin account name. The internal user ID is resolved automatically and reused within each worker.
## Jellyfin export

- **Enabled** — Write scene NFO files, posters and fanart and notify Jellyfin.
- **Dry run** — Preview scene export without writing files or notifying Jellyfin.
- **Export organized scenes only** — Restrict scene exports to scenes marked Organized.
- **Path mappings** — One Stash-root=Jellyfin-root mapping per line. The longest matching root wins. This does not move files.
- **Ignored video suffixes** — Skip matching video basename suffixes. Separate by lines or commas; $index$ matches a number.
- **Excluded paths** — Do not export scenes from these paths. One path per line.
## Performer sync

- **Enabled** — Synchronize performer details and images with existing Jellyfin people.
- **Dry run** — Preview performer synchronization without updating Jellyfin or Stash.
- **Update images** — Prepare images automatically and upload them through the Jellyfin API. SVG is rendered as PNG.
- **Update performer details** — Update biography, original title, birth/death dates, birth year and birthplace.
- **Save Jellyfin profile link** — Add the matched Jellyfin person link to the performer URLs in Stash.
## Whisparr bridge

- **Enabled** — Synchronize scene information and monitoring with Whisparr.
- **Server URL** — Base URL of the Whisparr server.
- **API key** — Whisparr API key.
- **StashDB host match** — Match scene references from this StashDB host; default: stashdb.org.
- **Path mappings** — Optional source=destination mappings, one per line. Otherwise map the library root to the Whisparr root.
- **Monitor synchronized scenes** — Mark synchronized scenes as monitored in Whisparr.
## Folder cleanup

- **Remove emptied source folders** — After moving videos, remove only affected source directories that are empty.
- **Dry run for manual cleanup** — Log manual folder cleanup without moving folders.
- **Cleanup destination** — Base path where manual cleanup creates its destination folder.
- **Cleanup folder name** — Destination folder name for manual cleanup.
- **Include small non-video folders** — Manual cleanup also moves folders with no videos if their total size is below the limit.
- **Small folder limit (MB)** — Maximum total size for small non-video folders during manual cleanup.
- **Excluded paths** — Never move these paths during manual cleanup. One path per line.
