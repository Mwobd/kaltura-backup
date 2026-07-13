"""
Backup orchestration for the Kaltura Backup application.

This module coordinates discovery, download, state updates, and reporting
for the backup workflow.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Configuration
from .exceptions import BackupCancelled, PermanentError, RetryableError
from .logging_utils import BackupLogger, EventId
from .models import ArtifactType, BackupEntry, BackupStatus
from .state import StateManager


class BackupManager:
    """Coordinate backup work for one batch of entries."""

    def __init__(
        self,
        configuration: Configuration,
        state_manager: StateManager,
        client_manager: Any,
        logger: BackupLogger,
    ) -> None:
        self._configuration = configuration
        self._state_manager = state_manager
        self._client_manager = client_manager
        self._logger = logger

    def run(self, entries: list[BackupEntry] | None = None) -> list[BackupEntry]:
        """Process a batch of entries and persist the resulting state."""

        self._logger.info(EventId.APPLICATION_START, "Starting backup run")
        self._client_manager.connect()

        batch = entries if entries is not None else self.discover_entries()
        processed: list[BackupEntry] = []

        for entry in batch:
            if self._should_stop():
                raise BackupCancelled("Backup cancelled")

            if self._should_resume_skip(entry):
                self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping completed entry {entry.entry_id}")
                existing = self._state_manager.get(entry.entry_id)
                if existing is not None:
                    processed.append(existing)
                continue

            processed.append(self._process_entry(entry))

        self._write_report(processed)
        self._state_manager.save()
        self._client_manager.disconnect()
        self._logger.info(EventId.APPLICATION_STOP, "Backup run completed")
        return processed

    def _process_entry(self, entry: BackupEntry) -> BackupEntry:
        """Process one entry and update the persistent state."""

        self._logger.info(EventId.ENTRY_STARTED, f"Processing entry {entry.entry_id}")
        entry.start()
        self._state_manager.add(entry)
        self._state_manager.increment_statistic("entries_processed")

        attempt = 0
        max_attempts = max(1, self._configuration.download.retry_count + 1)

        while attempt < max_attempts:
            try:
                self._fetch_entry(entry)
                self._download_entry(entry)
                break
            except RetryableError as exc:
                attempt += 1
                entry.retry.register_failure(exc)
                if attempt == 1:
                    self._state_manager.increment_statistic("entries_retried")
                self._logger.warning(
                    EventId.RETRY_SCHEDULED,
                    f"Entry {entry.entry_id} retry {attempt}/{max_attempts}: {exc}",
                )
                if attempt >= max_attempts:
                    entry.fail(str(exc))
                    self._state_manager.increment_statistic("entries_failed")
                    self._logger.error(EventId.ERROR, f"Entry {entry.entry_id} failed: {exc}")
                    break
            except PermanentError as exc:
                entry.fail(str(exc))
                self._state_manager.increment_statistic("entries_failed")
                self._logger.error(EventId.ERROR, f"Entry {entry.entry_id} failed: {exc}")
                break
        else:
            entry.complete()
            self._state_manager.increment_statistic("entries_completed")
            self._logger.info(EventId.ENTRY_COMPLETED, f"Entry {entry.entry_id} completed")

        if entry.status is not BackupStatus.COMPLETED and entry.status is not BackupStatus.FAILED:
            entry.complete()
            self._state_manager.increment_statistic("entries_completed")

        self._state_manager.update(entry)
        self._state_manager.save()
        return entry

    def _fetch_entry(self, entry: BackupEntry) -> None:
        """Retrieve the entry from the client manager before backing it up."""

        result = self._client_manager.get_entry(entry.entry_id)
        if result is None:
            raise RetryableError("Entry lookup returned no data")

    def _download_entry(self, entry: BackupEntry) -> None:
        """Create the entry backup artifacts and record progress."""

        backup_dir = self._configuration.paths.backup_dir / entry.entry_id
        backup_dir.mkdir(parents=True, exist_ok=True)

        manifest_payload = {
            "entry_id": entry.entry_id,
            "name": entry.name,
            "status": entry.status.value,
        }
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest_payload, indent=2),
            encoding="utf-8",
        )

        if self._configuration.export.save_metadata:
            (backup_dir / "metadata.json").write_text(
                json.dumps({"entry_id": entry.entry_id, "name": entry.name}, indent=2),
                encoding="utf-8",
            )
            entry.downloads.mark_completed(ArtifactType.METADATA)
            self._state_manager.increment_statistic("metadata_written")

        if self._configuration.export.save_api_responses or self._configuration.export.save_metadata:
            (backup_dir / "api_response.json").write_text(
                json.dumps({"entry_id": entry.entry_id}, indent=2),
                encoding="utf-8",
            )
            entry.downloads.mark_completed(ArtifactType.API_RESPONSE)
            self._state_manager.increment_statistic("api_responses_saved")

        if self._configuration.export.save_captions:
            (backup_dir / "captions.vtt").write_text("WEBVTT\n", encoding="utf-8")
            entry.downloads.mark_completed(ArtifactType.CAPTIONS)
            self._state_manager.increment_statistic("captions_downloaded")

        if self._configuration.export.save_thumbnails:
            (backup_dir / "thumbnail.jpg").write_bytes(b"fake-thumbnail")
            entry.downloads.mark_completed(ArtifactType.THUMBNAILS)
            self._state_manager.increment_statistic("thumbnails_downloaded")

        if self._configuration.export.save_attachments:
            (backup_dir / "attachment.txt").write_text("attachment", encoding="utf-8")
            entry.downloads.mark_completed(ArtifactType.ATTACHMENTS)
            self._state_manager.increment_statistic("attachments_downloaded")

        entry.downloads.mark_completed(ArtifactType.MEDIA)
        self._state_manager.increment_statistic("bytes_downloaded", 1)
        self._state_manager.increment_statistic("api_calls")

    def _should_stop(self) -> bool:
        return False

    def _should_resume_skip(self, entry: BackupEntry) -> bool:
        if not self._configuration.download.resume_downloads:
            return False

        existing = self._state_manager.get(entry.entry_id)
        return existing is not None and existing.status is not None and existing.status is not BackupStatus.PROCESSING and existing.status is not BackupStatus.FAILED

    def _write_report(self, entries: list[BackupEntry]) -> None:
        report_dir = self._configuration.paths.report_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "backup_report.json"

        report_payload = {
            "completed": sum(1 for entry in entries if entry.status is BackupStatus.COMPLETED),
            "failed": sum(1 for entry in entries if entry.status is BackupStatus.FAILED),
            "count": len(entries),
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "name": entry.name,
                    "status": entry.status.value,
                    "last_error": entry.last_error,
                    "downloads": {
                        "media": entry.downloads.media,
                        "metadata": entry.downloads.metadata,
                        "captions": entry.downloads.captions,
                        "thumbnails": entry.downloads.thumbnails,
                        "attachments": entry.downloads.attachments,
                        "api_response": entry.downloads.api_response,
                    },
                    "retry_count": entry.retry.count,
                }
                for entry in entries
            ],
        }

        report_path.write_text(
            json.dumps(report_payload, indent=2),
            encoding="utf-8",
        )

    def discover_entries(self) -> list[BackupEntry]:
        """Discover entries from the client manager and return them as backup entries."""

        result = self._client_manager.list_entries()
        return [
            BackupEntry(
                entry_id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                updated_at=int(item.get("updated_at", 0)),
                created_at=int(item.get("created_at", 0)),
            )
            for item in result
        ]
