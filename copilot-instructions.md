# Copilot instructions for kaltura-backup

## Working rules (strict)

- This project backs up Kaltura media and metadata, and optionally syncs Kaltura base entries to MySQL.
- Do not treat deleted or missing Kaltura entries as retryable failures. They must be skipped immediately.
- If a Kaltura `get` call returns not-found / does-not-exist conditions, mark the entry as `SKIPPED` and do not retry it.
- If a row is not refreshed in the current DB sync, reconcile it as deleted: `IsDeleted = 1` and set `IsDeletedDate`.
- Prefer platform truth over stale DB state when the two disagree.
- Keep `EntryUpdated` / `IsDeleted` logic in the database sync layer and not as an ad hoc backup workaround.
- Do not create `api_response.json` or `manifest.json` artifacts unless explicitly requested.
- Keep backup orchestration in `backup.py`, Kaltura client logic in `client.py`, and DB sync logic in `database.py`.
- Reuse pooled Kaltura sessions via `KalturaClientManager.session()` / `acquire()` / `release()`; do not create ad hoc client sessions per request.
- Keep session renewal and expiry handling centralized in the client manager and never bypass the session pool.
- Use the `BackupLogger` singleton and `EventId` codes for all application logging; do not call `logging.basicConfig()` directly.
- Log sensitive values such as KS only in file-only modes and only when explicitly requested.
- When fixing bugs, reproduce with a focused test first and validate the relevant subset before finishing.
- Preserve state/statistics consistency when entries are skipped or failed.

## Project purpose

This repository backs up media and metadata from Kaltura and optionally mirrors Kaltura base-entry metadata to MySQL. The project is intentionally layered: client/API access, backup orchestration, database sync, state tracking, and CLI entry points are separated into distinct modules.

## Important project facts

- The project lives under `src/kaltura_backup`.
- The backup workflow is implemented in `backup.py`.
- The Kaltura SDK wrapper and service access are in `client.py`.
- Database schema and sync logic are in `database.py`.
- Runtime config is in `config.py`.
- Persistent run state is in `state.py`.
- Custom exceptions are in `exceptions.py`.
- CLI entry points are in `cli.py`.

## Session management and pooling

The Kaltura client manager uses a thread-safe session pool in `KalturaClientManager` in `src/kaltura_backup/client.py`.

Rules:
- the pool is intentionally limited and reused across worker threads
- each request should acquire a pooled session through the context manager `with self.session() as session:`
- sessions are tracked with `KalturaSession`, including use count and last-used timestamps
- expired or invalid sessions must be renewed centrally through `_renew_session()`
- session creation and teardown must stay inside the client manager, not in backup logic
- sensitive session data such as the KS should only be logged to file when explicitly enabled for dry-run troubleshooting

## Logging principles

Application logging is centralized in `src/kaltura_backup/logging_utils.py`.

Rules:
- use the singleton `BackupLogger` and `EventId` values for all logs
- keep console and rotating file logging consistent; do not configure logging ad hoc in individual modules
- log retry/sync/session lifecycle events with the existing event IDs
- use DEBUG logging for SDK/session details but avoid printing sensitive credentials or KS values to the console
- use file-only logging for sensitive debug output, especially KS values and request metadata on dry-run troubleshooting
- only log the exception traceback when there is a real failure case worth diagnosing

## Key architecture

### 1) Entry discovery and listing

The Kaltura entry listing logic is in `KalturaClientManager.list_all_entries()` in `src/kaltura_backup/client.py`.

This method:
- uses the base-entry service
- applies paging
- returns every discovered base entry for the sync or backup workflow

The backup discovery flow is in `BackupManager.discover_entries()` in `src/kaltura_backup/backup.py`.

If MySQL is configured, the backup flow may select entry IDs from the `kaltura_entries` table instead of performing a raw platform-wide discovery. Otherwise it falls back to `list_all_entries()`.

### 2) Database sync and MySQL mirror

The database sync layer is in `DatabaseManager` in `src/kaltura_backup/database.py`.

Key methods:
- `ensure_schema()` creates the required tables and migrates schema when needed
- `upsert_entry()` writes or updates a single Kaltura entry row
- `sync_from_api()` retrieves all Kaltura entries and writes them to the DB
- `reconcile_deleted_entries()` marks stale rows as deleted
- `mark_deleted()` marks a single entry as deleted
- `get_backup_entry_updated_at()` selects active, non-deleted `Type = 1` entries

The project keeps these important database columns:
- `EntryUpdated`: last sync day used to establish freshness
- `IsDeleted`: deletion marker
- `IsDeletedDate`: date when a row was marked deleted

This is the critical stale-row rule:

- after retrieving all entries and updating the database
- if an entry was not refreshed in the current run and its `EntryUpdated` is not today
- mark it as deleted with `IsDeleted = 1`

This protects against deleted or stale Kaltura entries being treated as active records.

### 3) Backup processing

The backup workflow is orchestrated by `BackupManager` in `src/kaltura_backup/backup.py`.

Important responsibilities:
- process one entry at a time
- fetch the Kaltura entry
- download media, metadata, captions, thumbnails, attachments
- track retry state
- mark entries as completed, failed, or skipped

The backup loop uses retries for transient failures, but deleted Kaltura entries must not be retried.

## Deleted-entry handling rules

This is a required rule for future fixes:

- If Kaltura says the entry no longer exists, treat it as a skipped entry, not a failed retryable task.
- Do not loop through the normal retry count for entry-not-found / deleted-entry conditions.
- Detect and handle stale DB rows that appear active even though the platform has already removed them.
- When DB status and Kaltura status disagree, prefer the platform truth and the stale-row reconciliation rule.

The safe behavior is:

- `EntryNotFoundError` or equivalent not-found condition => `entry.skip()` and mark as `BackupStatus.SKIPPED`
- no retry loop for missing-deleted entries
- no final conversion from skipped back to completed

## Important implementation constraints

### No JSON artifact files

The project no longer writes the following artifacts as part of normal backup flow:
- `api_response.json`
- `manifest.json`

If code is added that creates these files for debugging or output, it should be treated as a regression unless explicitly requested.

### Retry behavior

- transient Kaltura/API failures may retry
- missing/deleted entries are permanent skip conditions
- retry logic is centralized around the manager logic in `backup.py`

The backup run should not crash or abort because one deleted entry is missing from the platform.

## Runtime behavior and configuration

### CLI

The application entry point is `python -m kaltura_backup --config config.ini`.

Common CLI actions:
- `--dry-run` to preview without writing artifacts
- `--retry-failed` to retry only previously failed entries
- `--db-sync` to sync base entries to MySQL
- `--force-db-sync` to force a daily sync again

### Configuration file

Use `config.ini.example` as the template. If no config exists, the app can bootstrap a default `config.ini`.

The MySQL sync is optional, and no database sync is required for the local backup workflow to run.

## Test expectations

The repository includes tests for:
- backup retries
- stale-entry handling
- database sync behavior
- Kaltura SDK conversion and schema logic

When fixing bugs or adding features:
- reproduce with a focused test first
- prefer the smallest possible fix
- validate the relevant test subset before claiming success

## Safe coding guidance for future work

- Keep the client manager wrapper isolated from backup logic.
- Keep DB sync logic in `database.py`; avoid embedding DB specifics into `backup.py`.
- Preserve the `EntryUpdated` / `IsDeleted` stale-entry lifecycle.
- When the platform reports “not found”, skip that entry without retries.
- Prefer root-cause fixes, not ad hoc retry filters.
- Do not reintroduce generated JSON artifacts unless clearly requested.
- Keep backup state and statistics consistent when an entry is skipped.

## Quick map of files

- [src/kaltura_backup/backup.py](src/kaltura_backup/backup.py): orchestration, retries, skip logic, backup run
- [src/kaltura_backup/client.py](src/kaltura_backup/client.py): Kaltura SDK integration and entry listing
- [src/kaltura_backup/database.py](src/kaltura_backup/database.py): schema and MySQL sync logic
- [src/kaltura_backup/config.py](src/kaltura_backup/config.py): all configuration structures and loading
- [src/kaltura_backup/state.py](src/kaltura_backup/state.py): persistent entry/state management
- [src/kaltura_backup/models.py](src/kaltura_backup/models.py): BackupEntry and retry/status dataclasses
- [src/kaltura_backup/cli.py](src/kaltura_backup/cli.py): CLI commands and behavior
- [tests/test_backup_retry.py](tests/test_backup_retry.py): retry and deleted-entry regression tests
- [tests/test_database.py](tests/test_database.py): DB sync and stale-entry logic tests

## Summary

The most important rule for future maintenance is this:

Deleted or non-existent Kaltura entries are not retryable failures. They are skipped entries, and stale database rows must be reconciled as deleted after a refresh.
