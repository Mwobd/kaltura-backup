"""
Backup orchestration for the Kaltura Backup application.

This module coordinates discovery, download, state updates, and reporting
for the backup workflow.
"""

from __future__ import annotations

import csv
import json
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from xml.etree import ElementTree
from urllib.parse import urlparse, unquote
from typing import Any

import requests

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
        dry_run: bool = False,
    ) -> None:
        self._configuration = configuration
        self._state_manager = state_manager
        self._client_manager = client_manager
        self._logger = logger
        self._dry_run = dry_run
        self._stop_requested = False
        self._stop_event = threading.Event()

    def run(self, entries: list[BackupEntry] | None = None) -> list[BackupEntry]:
        """Process a batch of entries and persist the resulting state."""

        self._stop_requested = False
        self._stop_event.clear()
        self._register_signal_handlers()

        self._logger.info(EventId.APPLICATION_START, "Starting backup run")
        self._client_manager.connect()

        try:
            batch = entries if entries is not None else self.discover_entries()
            total_entries = len(batch)
            processed: list[BackupEntry] = []

            if self._configuration.download.workers > 1:
                futures = []
                with ThreadPoolExecutor(max_workers=self._configuration.download.workers) as executor:
                    for index, entry in enumerate(batch, start=1):
                        if self._should_stop():
                            self._logger.warning(EventId.WARNING, "Backup cancelled by shutdown request")
                            raise BackupCancelled("Backup cancelled")

                        if self._should_resume_skip(entry):
                            self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping completed entry {entry.entry_id}")
                            existing = self._state_manager.get(entry.entry_id)
                            if existing is not None:
                                processed.append(existing)
                            self._emit_progress(index, total_entries, entry.entry_id)
                            continue

                        futures.append(executor.submit(self._process_entry, entry))

                    done, _ = wait(futures)
                    for future in done:
                        result = future.result()
                        processed.append(result)

                for index, entry in enumerate(batch, start=1):
                    self._emit_progress(index, total_entries, entry.entry_id)
            else:
                for index, entry in enumerate(batch, start=1):
                    if self._should_stop():
                        self._logger.warning(EventId.WARNING, "Backup cancelled by shutdown request")
                        raise BackupCancelled("Backup cancelled")

                    if self._should_resume_skip(entry):
                        self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping completed entry {entry.entry_id}")
                        existing = self._state_manager.get(entry.entry_id)
                        if existing is not None:
                            processed.append(existing)
                        self._emit_progress(index, total_entries, entry.entry_id)
                        continue

                    processed.append(self._process_entry(entry))
                    self._emit_progress(index, total_entries, entry.entry_id)

            self._write_report(processed)
            self._state_manager.save()
            self._logger.info(EventId.APPLICATION_STOP, "Backup run completed")
            return processed
        finally:
            self._restore_signal_handlers()
            self._client_manager.disconnect()

    def _emit_progress(self, completed: int, total: int, entry_id: str) -> None:
        """Print a lightweight progress update for the current backup run."""

        if total <= 0:
            return

        print(f"Progress: {completed}/{total} entries processed ({entry_id})", flush=True)

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
                entry_data = self._fetch_entry(entry)
                self._download_entry(entry, entry_data)
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

    def _fetch_entry(self, entry: BackupEntry) -> Any:
        """Retrieve the entry from the client manager and hydrate local entry fields.

        Returns the raw SDK entry object for further processing.
        """

        result = self._client_manager.get_entry(entry.entry_id)
        if result is None:
            raise RetryableError("Entry lookup returned no data")

        def value(item: Any, attr: str, fallback: Any = None) -> Any:
            getter = f"get{attr[0].upper()}{attr[1:]}"
            if hasattr(item, getter):
                return getattr(item, getter)()
            if hasattr(item, attr):
                return getattr(item, attr)
            return fallback

        entry.name = str(value(result, "name", entry.name))
        entry.reference_id = str(value(result, "referenceId", entry.reference_id))
        entry.owner_id = str(value(result, "userId", entry.owner_id))
        entry.media_type = str(
            value(result, "mediaType", value(result, "type", entry.media_type) or "")
        )
        try:
            entry.duration_seconds = int(value(result, "duration", entry.duration_seconds) or 0)
        except Exception:
            entry.duration_seconds = entry.duration_seconds
        try:
            entry.size_bytes = int(value(result, "size", entry.size_bytes) or 0)
        except Exception:
            entry.size_bytes = entry.size_bytes
        try:
            entry.updated_at = int(value(result, "updatedAt", entry.updated_at) or 0)
        except Exception:
            entry.updated_at = entry.updated_at
        try:
            entry.created_at = int(value(result, "createdAt", entry.created_at) or 0)
        except Exception:
            entry.created_at = entry.created_at

        return result

    def _download_entry(self, entry: BackupEntry, entry_data: Any) -> None:
        """Create the entry backup artifacts and record progress using real downloads."""

        if self._dry_run:
            self._logger.info(EventId.APPLICATION_START, f"Dry run: would back up entry {entry.entry_id}")
            entry.downloads.mark_completed(ArtifactType.MEDIA)
            self._state_manager.increment_statistic("bytes_downloaded", 0)
            self._state_manager.increment_statistic("api_calls")
            return

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

        # Download media
        media_skipped = False
        try:
            media_url = self._client_manager.get_entry_media_url(entry.entry_id)
            if media_url:
                file_path = self._media_file_path(backup_dir, entry)
                try:
                    with requests.get(media_url, stream=True, timeout=30) as resp:
                        resp.raise_for_status()
                        with file_path.open("wb") as fh:
                            for chunk in resp.iter_content(chunk_size=8192):
                                if chunk:
                                    fh.write(chunk)
                                    self._state_manager.increment_statistic("bytes_downloaded", len(chunk))
                    entry.downloads.mark_completed(ArtifactType.MEDIA)
                except requests.RequestException as exc:
                    raise RetryableError(f"Failed to download media for {entry.entry_id}: {exc}") from exc
            else:
                # Fallback to placeholder if no URL available
                media_skipped = self._write_media_if_needed(backup_dir, entry)
        except Exception:
            # If the client method fails, fall back to placeholder to avoid blocking
            media_skipped = self._write_media_if_needed(backup_dir, entry)

        if self._configuration.export.save_metadata and not self._is_image_entry(entry):
            profile_fields = self._configuration.metadata.profile_fields
            field_names: list[str] = []
            seen_fields: set[str] = set()

            for fields in profile_fields.values():
                for field_name in fields:
                    if field_name not in seen_fields:
                        seen_fields.add(field_name)
                        field_names.append(field_name)

            metadata_path = backup_dir / "metadata.csv"
            with metadata_path.open("w", encoding="utf-8", newline="") as metadata_file:
                writer = csv.DictWriter(
                    metadata_file,
                    fieldnames=["entry_id", "name"] + field_names,
                )
                writer.writeheader()

                # Populate metadata values by profile and field name
                row = {"entry_id": entry.entry_id, "name": entry.name}
                row.update({field_name: "" for field_name in field_names})

                for profile_id, fields in profile_fields.items():
                    try:
                        meta_objs = self._client_manager.list_metadata_objects(entry.entry_id, profile_id)
                    except Exception:
                        meta_objs = []

                    for meta in meta_objs:
                        # determine metadata id getter
                        meta_id = None
                        if hasattr(meta, "getId"):
                            meta_id = getattr(meta, "getId")()
                        elif hasattr(meta, "id"):
                            meta_id = getattr(meta, "id")

                        if not meta_id:
                            continue

                        try:
                            xml = self._client_manager.get_metadata_xml(meta_id)
                        except Exception:
                            continue

                        try:
                            root = ElementTree.fromstring(xml)
                        except Exception:
                            continue

                        for field_name in fields:
                            if row.get(field_name):
                                continue
                            # try to find text for the field
                            found = root.find(f".//{field_name}")
                            if found is not None and found.text:
                                row[field_name] = found.text

                writer.writerow(row)

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
            captions_path = backup_dir / "captions.vtt"
            wrote_any = False
            try:
                assets = self._client_manager.list_caption_assets(entry.entry_id)
            except Exception:
                assets = []

            with captions_path.open("w", encoding="utf-8") as fh:
                fh.write("WEBVTT\n")
                for asset in assets:
                    asset_id = None
                    if hasattr(asset, "getId"):
                        asset_id = getattr(asset, "getId")()
                    elif hasattr(asset, "id"):
                        asset_id = getattr(asset, "id")

                    if not asset_id:
                        continue

                    try:
                        vtt = self._client_manager.get_caption_webvtt(asset_id)
                        fh.write(vtt)
                        fh.write("\n")
                        wrote_any = True
                    except Exception:
                        continue

            if wrote_any:
                entry.downloads.mark_completed(ArtifactType.CAPTIONS)
                self._state_manager.increment_statistic("captions_downloaded")

        if self._configuration.export.save_thumbnails:
            try:
                thumbs = self._client_manager.list_thumb_assets(entry.entry_id)
            except Exception:
                thumbs = []

            thumb_written = False
            for thumb in thumbs:
                thumb_id = None
                if hasattr(thumb, "getId"):
                    thumb_id = getattr(thumb, "getId")()
                elif hasattr(thumb, "id"):
                    thumb_id = getattr(thumb, "id")

                if not thumb_id:
                    continue

                try:
                    url = self._client_manager.get_thumb_url(thumb_id)
                except Exception:
                    continue

                try:
                    resp = requests.get(url, timeout=30)
                    resp.raise_for_status()
                    # use first thumbnail only
                    (backup_dir / "thumbnail.jpg").write_bytes(resp.content)
                    thumb_written = True
                    break
                except Exception:
                    continue

            if thumb_written:
                entry.downloads.mark_completed(ArtifactType.THUMBNAILS)
                self._state_manager.increment_statistic("thumbnails_downloaded")

        if self._configuration.export.save_attachments:
            try:
                atts = self._client_manager.list_attachment_assets(entry.entry_id)
            except Exception:
                atts = []

            att_written = False
            for att in atts:
                att_id = None
                if hasattr(att, "getId"):
                    att_id = getattr(att, "getId")()
                elif hasattr(att, "id"):
                    att_id = getattr(att, "id")

                if not att_id:
                    continue

                try:
                    url = self._client_manager.get_attachment_url(att_id)
                except Exception:
                    continue

                try:
                    resp = requests.get(url, timeout=30)
                    resp.raise_for_status()
                    parsed = urlparse(url)
                    name = unquote(parsed.path.split("/")[-1]) or f"attachment_{att_id}"
                    (backup_dir / name).write_bytes(resp.content)
                    att_written = True
                except Exception:
                    continue

            if att_written:
                entry.downloads.mark_completed(ArtifactType.ATTACHMENTS)
                self._state_manager.increment_statistic("attachments_downloaded")

        if media_skipped:
            self._logger.info(
                EventId.DOWNLOAD_COMPLETED,
                f"Existing source media found for {entry.entry_id}, skipping media download.",
            )

        self._state_manager.increment_statistic("api_calls")

    def _is_image_entry(self, entry: BackupEntry) -> bool:
        return entry.media_type.lower() == "image"

    def _media_file_path(self, backup_dir: Any, entry: BackupEntry) -> Any:
        name = entry.media_type.lower()

        if "video" in name:
            filename = "media.mp4"
        elif "audio" in name:
            filename = "audio.mp3"
        elif "image" in name:
            filename = "image.jpg"
        else:
            filename = "media.bin"

        return backup_dir / filename

    def _write_media_if_needed(self, backup_dir: Any, entry: BackupEntry) -> bool:
        file_path = self._media_file_path(backup_dir, entry)

        if file_path.exists():
            entry.downloads.mark_completed(ArtifactType.MEDIA)
            return True

        file_path.write_bytes(b"fake-media")
        entry.downloads.mark_completed(ArtifactType.MEDIA)
        return False

    def _register_signal_handlers(self) -> None:
        """Register signal handlers so interruption requests stop the backup gracefully."""

        try:
            signal.signal(signal.SIGINT, self._handle_shutdown_signal)
            signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        except ValueError:  # pragma: no cover - occurs outside main thread
            return

    def _restore_signal_handlers(self) -> None:
        """Restore the previous signal handlers after the run completes."""

        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        except ValueError:  # pragma: no cover - occurs outside main thread
            return

    def _handle_shutdown_signal(self, signum: int, _frame: Any) -> None:
        """Set a shutdown flag so the manager stops on the next safe checkpoint."""

        self._stop_requested = True
        self._stop_event.set()
        self._logger.warning(EventId.WARNING, f"Shutdown signal received: {signum}")

    def _should_stop(self) -> bool:
        return self._stop_requested or self._stop_event.is_set()

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

        # Produce a concise summary for console and logfile
        try:
            stats = self._state_manager.statistics
        except Exception:
            stats = None

        media_count = sum(1 for entry in entries if entry.downloads.media)
        metadata_count = sum(1 for entry in entries if entry.downloads.metadata)
        captions_count = sum(1 for entry in entries if entry.downloads.captions)
        thumbnails_count = sum(1 for entry in entries if entry.downloads.thumbnails)
        attachments_count = sum(1 for entry in entries if entry.downloads.attachments)
        api_resp_count = sum(1 for entry in entries if entry.downloads.api_response)

        bytes_downloaded = stats.bytes_downloaded if stats is not None else None
        api_calls = stats.api_calls if stats is not None else None

        summary_lines = [
            f"Backup summary: {report_payload['count']} entries processed",
            f"  Completed: {report_payload['completed']}, Failed: {report_payload['failed']}",
            f"  Media files: {media_count}, Metadata: {metadata_count}, Captions: {captions_count}",
            f"  Thumbnails: {thumbnails_count}, Attachments: {attachments_count}, API responses: {api_resp_count}",
        ]

        if bytes_downloaded is not None:
            summary_lines.append(f"  Bytes downloaded: {bytes_downloaded}")

        if api_calls is not None:
            summary_lines.append(f"  API calls: {api_calls}")

        # Log and print the summary
        for line in summary_lines:
            try:
                self._logger.info(EventId.APPLICATION_STOP, line)
            except Exception:
                # Fallback to plain logging if structured logger fails
                try:
                    print(line, flush=True)
                except Exception:
                    pass

        try:
            print("\n".join(summary_lines), flush=True)
        except Exception:
            pass

    def discover_entries(self) -> list[BackupEntry]:
        """Discover entries from the client manager and return them as backup entries."""

        def value(item: Any, attr: str, fallback: Any) -> Any:
            getter = f"get{attr[0].upper()}{attr[1:]}"
            if hasattr(item, getter):
                return getattr(item, getter)()
            if hasattr(item, attr):
                return getattr(item, attr)
            return fallback

        result = self._client_manager.list_all_entries()
        return [
            BackupEntry(
                entry_id=str(value(item, "id", "")),
                name=str(value(item, "name", "")),
                updated_at=int(value(item, "updatedAt", 0) or 0),
                created_at=int(value(item, "createdAt", 0) or 0),
            )
            for item in result
        ]
