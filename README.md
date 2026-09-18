# kaltura-backup

A small Python utility for creating Kaltura backups from a local configuration.

## Quick start

1. Create and activate a virtual environment:
   - `py -3.11 -m venv .venv`
   - `.venv\Scripts\activate`
2. Install the required dependencies:
   - `pip install -r requirements.txt`
   - or, for editable local development: `pip install -e .`
3. Copy the example configuration and adjust it:
   - `copy config.ini.example config.ini`
4. Run the tool:
   - `python -m kaltura_backup --config config.ini`

To preview what would happen without writing backup artifacts, use:

```bat
python -m kaltura_backup --config config.ini --dry-run
```

To retry only entries that were previously marked as failed in the saved state, use:

```bat
python -m kaltura_backup --config config.ini --retry-failed
```

To sync Kaltura base entries into MySQL, use the database sync command:

```bat
python -m kaltura_backup --config config.ini --db-sync
```

By default, the database sync only runs once each day. To force a second sync on the same day, use:

```bat
python -m kaltura_backup --config config.ini --db-sync --force-db-sync
```

The MySQL sync is optional. If the `[mysql]` section is absent, the backup workflow still runs locally without database syncing.

When MySQL is configured, the normal backup workflow uses `kaltura_entries` as its entry selection source. It backs up only rows where `Type = 1` and `IsDeleted = 0`, and fetches those selected entry IDs from Kaltura. Other entry types and deleted rows are ignored. Without MySQL configuration, the workflow retains API-wide entry discovery.

Database sync progress is written to the console and to `logs/kaltura_backup.log`. The log records the MySQL connection target, schema checks, daily-sync skips, Kaltura entry counts, progress while entries are stored, and completion. The configured `[Download] Timeout` value is also applied to Kaltura SDK API requests. If the sync fails or the API does not respond before that timeout, the CLI prints the error and writes the full traceback to the log file. A sync run that appears to repeat API requests should be stopped and its log checked; the sync uses the Kaltura client's all-pages method once and then processes the returned entries.

The database-sync command forces `DEBUG` logging at runtime, including when an existing `config.ini` still specifies `INFO`. The generated configuration and `config.ini.example` also use `Level = DEBUG`, which includes Kaltura SDK request and retry messages. For other commands, set the value manually in an existing `config.ini`:

```ini
[Logging]
Level = DEBUG
```

The Kaltura SDK has its own retry cycle. A request timeout is per attempt, so a failed request may take longer than the configured timeout while the SDK performs its built-in retries. The DEBUG log records each SDK retry and its delay.

## MySQL sync and database schema

The database-backed sync is designed to keep a mirrored view of Kaltura base entries in MySQL. The schema created by the app includes:

- `kaltura_entries`
- `kaltura_caption_assets`
- `kaltura_thumb_assets`
- `kaltura_attachment_assets`
- `db_sync_state`

The relevant timestamp-related columns are:

- `CreatedAt` and `UpdatedAt` for raw UNIX epoch values
- `CreatedAtHR` and `UpdatedAtHR` for converted human-readable timestamps
- `EntryUpdated` for the last sync day used to determine stale entries

This is the schema used by the app when the sync is enabled:

```sql
CREATE TABLE IF NOT EXISTS kaltura_entries (
   EntryId VARCHAR(255) NOT NULL PRIMARY KEY,
   Name VARCHAR(255) NULL,
   Description TEXT NULL,
   PartnerId BIGINT NULL,
   UserId VARCHAR(255) NULL,
   CreatorId VARCHAR(255) NULL,
   Tags TEXT NULL,
   AdminTags TEXT NULL,
   Status INT NULL,
   Type INT NULL,
   CreatedAt BIGINT NULL,
   UpdatedAt BIGINT NULL,
   CreatedAtHR VARCHAR(50) NULL,
   UpdatedAtHR VARCHAR(50) NULL,
   DownloadUrl TEXT NULL,
   ThumbnailUrl TEXT NULL,
   DataUrl TEXT NULL,
   ReferenceId TEXT NULL,
   MediaType INT NULL,
   Duration INT NULL,
   Width INT NULL,
   Height INT NULL,
   EntryUpdated VARCHAR(20) NULL,
   IsDeleted TINYINT(1) NOT NULL DEFAULT 0,
   IsDeletedDate VARCHAR(20) NULL,
   RawXml LONGTEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

If you are upgrading an existing database, confirm those columns exist before relying on the stale-entry checks. The app will create the table on first run when the table does not exist, but it does not automatically add missing columns to an already-created table.

During import, every XML leaf tag is also mapped to a safe MySQL column name. Known Kaltura tags use the existing typed columns; previously unknown tags are added as nullable `LONGTEXT` columns automatically. The `<id>` tag is mapped to `EntryId` and is not duplicated as an `ID` column. Existing databases are migrated by removing the obsolete `ID` column when the schema is checked.

The first caption request, `caption_captionAsset.list`, is stored in `kaltura_caption_assets`. Its XML tags become columns, with `CaptionAssetId` as the primary key. The later `caption_captionAsset.serveAsJson` response is written to the per-caption JSON file only and is not stored in MySQL.

The `thumbAsset.list` and `attachmentAsset.list` responses are stored separately in `kaltura_thumb_assets` and `kaltura_attachment_assets`. Their IDs are the primary keys, and both tables include `CreatedAt`, `UpdatedAt`, `CreatedAtHR`, and `UpdatedAtHR` with UTC conversions. URL/download responses are not stored in these tables.

## MySQL configuration

The example file contains an optional `[mysql]` block. Set your host, database name, and credentials there to enable database sync:

```ini
[mysql]
host = localhost
database = backup_10206
user = backupuser10206
password = <dbuserpassword>
```
## XML Response Directory

The application saves each raw XML response from the Kaltura API to the configured directory. Configure it under the `[Paths]` section:

```ini
[Paths]
XmlDir = xml
```

Files use the API service and action plus a UTC timestamp, for example `baseEntry_list_20260918_151850_123456.xml`.

The code also supports `root_user` and `root_password` for bootstrap scenarios, but the normal runtime connection uses the non-root `user` and `password` values.

## Windows Task Scheduler

The repository includes a batch launcher at [run_backup.bat](run_backup.bat) that activates the local virtual environment and starts the backup CLI from the repository root.

To run it from Task Scheduler:

1. Open Task Scheduler and create a new Basic Task.
2. Set the action to Start a program.
3. Use these values:
   - Program/script: `cmd.exe`
   - Add arguments: `/c "C:\path\to\kaltura-backup\run_backup.bat"`
   - Start in: `C:\path\to\kaltura-backup`
4. Configure the task to run with the account that has access to the backup paths and the Kaltura credentials.
5. Ensure that account can read and write to the backup destination, the log directory, and the repository folder that contains the configuration file.
6. If the task runs without a user session, make sure the account has access to the local Python installation and that the virtual environment is created for that same account.
7. Optionally enable Run whether user is logged on or not and set a suitable trigger such as a daily schedule.

If you prefer to invoke the CLI directly, use:

```bat
python -m kaltura_backup --config config.ini
```

## Configuration

The main configuration file is [config.ini.example](config.ini.example). If no config file is present, the application will create a default [config.ini](config.ini) for you on first run so you can adjust the connection and path settings before the next execution.

The example config now includes a `[metadata_profiles]` section. Each profile is configured on its own line using the Kaltura metadata profile ID and a comma-separated list of field names:

```ini
[metadata_profiles]
4696 = Attributie, LinkNaarBron, LinkNaarLicentievoorwaarden
4694 = VrijwaringsverklaringPortretrecht
```

When metadata backup is enabled, custom metadata is written to `metadata.csv` for each entry. The file includes a header line with `entry_id`, `name`, and the configured metadata field names.

You can also bootstrap it explicitly from Python:

```python
from kaltura_backup import ensure_default_config

ensure_default_config("config.ini")
```

## Backup output structure and file naming

The backup tool creates a folder structure under your configured backup destination. Each entry gets its own folder, and various backup artifacts are stored within.

### Folder structure

```
backup_destination/
??? entry_1_id/
?   ??? media.mp4
?   ??? metadata.csv
?   ??? caption_caption-asset-1.json
?   ??? thumbnail.jpg
?   ??? attachment.txt
?   ??? api_response.json
??? entry_2_id/
?   ??? audio.mp3
?   ??? metadata.csv
?   ??? ...
??? report.json
```

The root of the backup destination contains a `report.json` file with overall backup statistics.

### Downloaded artifacts and file naming

For each Kaltura entry, the following artifacts are downloaded **if present** and **if enabled** in the configuration:

| Artifact | Filename | Enabled by | Description |
|----------|----------|-----------|-------------|
| **Media** | `media.mp4`, `audio.mp3`, `image.jpg`, or `media.bin` | `Export.SaveMedia` | The main media file. Format depends on media type: video ? `.mp4`, audio ? `.mp3`, image ? `.jpg`, other ? `.bin` |
| **Custom Metadata** | `metadata.csv` | `Export.SaveMetadata` | Custom metadata in CSV format with header row containing `entry_id`, `name`, and configured metadata field names (from `[metadata_profiles]` section). Only includes profiles specified in the configuration. |
| **Captions** | `caption_<caption_asset_id>.json` | `Export.SaveCaptions` | JSON caption response for each caption asset, retrieved through `captionAsset.serveAsJson`. |
| **Thumbnails** | `thumbnail.jpg` | `Export.SaveThumbnails` | Entry thumbnail image. Includes all available thumbnail assets for the entry. |
| **Attachments** | `attachment.txt` | `Export.SaveAttachments` | Text file listing attachment URLs. One URL per line. Empty file if no attachments are present. |
| **API Response** | `api_response.json` | `Export.SaveApiResponses` or `Export.SaveMetadata` | JSON response containing basic entry metadata (`entry_id` and `name`). Written whenever metadata or API responses are saved. |

### Resume behavior

When running with `ResumeDownloads = true` in the configuration:
- Media files are **skipped** if they already exist in the backup folder
- Metadata, captions, attachments, images, and thumbnails are **always downloaded** and overwritten
- This allows you to refresh supplementary data without re-downloading large media files

Independently of `ResumeDownloads`, media, captions, thumbnails, attachments, and saved API responses are skipped when an entry's positive `UpdatedAt` UNIX timestamp is more than 48 hours old and the corresponding artifact already exists. The timestamp comparison uses UTC. `metadata.csv` is the only downloadable artifact that is always refreshed.

## Project roadmap status

The implementation has moved beyond the initial scaffold and is now in the later operational phases.

- Phase 1 — ? Complete: project structure, packaging metadata, configuration example, and base documentation.
- Phase 2 — ? Complete: configuration loading, validation, logging, domain models, and state management.
- Phase 3 — ? Complete: the Kaltura client abstraction is in place, with a stubbed fallback for environments without the SDK; live SDK integration still awaits broader real-world validation.
- Phase 4 — ? Complete: backup orchestration, state persistence, resume-aware skipping, and manifest generation are implemented.
- Phase 5 — ? Complete: backup artifacts for metadata, API responses, captions, thumbnails, and attachments are produced during runs.
- Phase 6 — ? Complete: retry handling, reporting, worker-pool concurrency, and graceful shutdown are implemented.
- Phase 7 — ?? In progress: CLI entrypoints, user-friendly failure handling, dry-run, and retrying previously failed entries are implemented; state rebuild remains a future enhancement.
- Phase 8 — ? Complete: regression tests cover the core workflow, CLI behavior, packaging, configuration, and reporting.
