"""
Backup orchestration for the Kaltura Backup application.

This module coordinates discovery, download, state updates, and reporting
for the backup workflow.
"""

from __future__ import annotations

import csv
import json
import re
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import urlparse, unquote, unquote_plus
from typing import Any

import requests

from .config import Configuration
from .exceptions import BackupCancelled, CaptionAssetNotReadyError, PermanentError, RetryableError
from .logging_utils import BackupLogger, EventId
from .models import ArtifactType, BackupEntry, BackupStatus
from .state import StateManager


SUMMARY_ARTIFACTS = (
    "Media files",
    "Metadata",
    "Captions",
    "Thumbnails",
    "Attachments",
)


def _to_snake_case(value: str) -> str:
    """Convert CamelCase to snake_case for dict-like payloads."""

    result: list[str] = []
    for index, char in enumerate(value):
        if char.isupper() and index and not value[index - 1].isupper():
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _read_value(item: Any, attr: str, fallback: Any = None) -> Any:
    """Read a field from either a dict-like payload or a Kaltura SDK object."""

    if item is None:
        return fallback

    if isinstance(item, dict):
        for key in (attr, _to_snake_case(attr), attr.lower()):
            if key in item:
                value = item[key]
                return value if value is not None else fallback
        return fallback

    getter = f"get{attr[0].upper()}{attr[1:]}"
    for candidate in (getter, attr, _to_snake_case(attr)):
        if hasattr(item, candidate):
            value = getattr(item, candidate)
            return value() if callable(value) else (value if value is not None else fallback)
    return fallback


def _normalize_xml_tag(tag: str) -> str:
    """Normalize XML element names to a comparable form."""

    if not tag:
        return ""
    name = tag.split("}")[-1]
    return "".join(ch for ch in name if ch.isalnum() or ch in {"_", "-"}).lower()


def _find_xml_value(root: ElementTree.Element, field_name: str) -> str:
    """Search XML metadata for a field, ignoring attribute naming differences."""

    normalized = _normalize_xml_tag(field_name)
    candidates = {
        normalized,
        normalized.replace("-", ""),
        normalized.replace("_", ""),
    }

    for element in root.iter():
        tag_name = _normalize_xml_tag(element.tag)
        if tag_name in candidates and element.text and element.text.strip():
            return element.text.strip()

    for element in root.iter():
        tag_name = _normalize_xml_tag(element.tag)
        if normalized in tag_name or tag_name in normalized:
            if element.text and element.text.strip():
                return element.text.strip()

    return ""


def _resolve_download_filename(response: Any, fallback_name: str, request_url: str | None = None) -> str:
    """Best-effort extraction of the actual file name from a download response."""

    headers = getattr(response, "headers", {}) or {}
    disposition = headers.get("Content-Disposition") or headers.get("content-disposition")
    url = getattr(response, "url", request_url) or request_url or ""

    if disposition:
        params = disposition.split(";")
        for part in params:
            item = part.strip()
            if item.lower().startswith("filename*="):
                raw = item.split("=", 1)[1].strip("\"'")
                if raw:
                    if "''" in raw:
                        _, filename = raw.split("''", 1)
                    else:
                        filename = raw
                    return unquote_plus(filename)
            if item.lower().startswith("filename="):
                raw = item.split("=", 1)[1].strip("\"'")
                if raw:
                    return unquote(raw)

    parsed = urlparse(url)
    candidate = unquote(parsed.path.split("/")[-1])
    if candidate and "." in candidate:
        return candidate

    return fallback_name


def _safe_download_filename(filename: str, entry_id: str) -> str:
    """Sanitize a response filename, falling back to the entry ID if unusable."""
    invalid_characters = set('<>:"/\\|?*')
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem, separator, suffix = basename.rpartition(".")
    extension = f".{suffix}" if separator and stem and re.fullmatch(r"[A-Za-z0-9]{1,16}", suffix) else ""
    stem = stem if extension else basename
    reserved_names = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }

    sanitized_stem = "".join(
        "_" if character in invalid_characters or ord(character) < 32 else character
        for character in stem
    ).rstrip(". ")
    reserved_stem = sanitized_stem.split(".", 1)[0].upper()
    sanitized_name = f"{sanitized_stem}{extension}"
    if sanitized_name and reserved_stem not in reserved_names:
        return sanitized_name

    safe_entry_id = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", entry_id).rstrip(". ")
    return f"{safe_entry_id or 'entry'}{extension}"


def _normalize_media_type(value: Any) -> str:
    getter = getattr(value, "getValue", None)
    if callable(getter):
        value = getter()
    elif hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        value = value.value

    normalized = str(value or "").strip().lower()
    return {
        "1": "video",
        "2": "image",
        "5": "audio",
    }.get(normalized, normalized)


def _is_error_media_payload(prefix: bytes, content_type: str) -> bool:
    content_type = content_type.lower().split(";", 1)[0].strip()
    if content_type in {"application/xml", "text/xml", "text/html", "application/json"}:
        return True

    beginning = prefix.lstrip().lower()
    return beginning.startswith((b"<", b"{", b"["))


class BackupManager:
    """Coordinate backup work for one batch of entries."""

    def __init__(
        self,
        configuration: Configuration,
        state_manager: StateManager,
        client_manager: Any,
        logger: BackupLogger,
        dry_run: bool = False,
        database_entry_ids: list[str] | None = None,
        database_entry_updated_at: dict[str, int | None] | None = None,
    ) -> None:
        self._configuration = configuration
        self._state_manager = state_manager
        self._client_manager = client_manager
        self._logger = logger
        self._dry_run = dry_run
        self._database_entry_ids = database_entry_ids
        self._database_entry_updated_at = database_entry_updated_at
        self._stop_requested = False
        self._stop_event = threading.Event()
        self._statistics_lock = threading.Lock()
        self._run_bytes_downloaded = 0
        self._run_started_at: datetime | None = None
        self._run_artifact_outcomes: dict[tuple[str, str], str | None] = {}

    def run(self, entries: list[BackupEntry] | None = None) -> list[BackupEntry]:
        """Process a batch of entries and persist the resulting state."""

        self._stop_requested = False
        self._stop_event.clear()
        self._run_started_at = datetime.now(UTC)
        with self._statistics_lock:
            self._run_bytes_downloaded = 0
            self._run_artifact_outcomes = {}
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
                            self._record_entry_artifacts_skipped(entry)
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
                        self._record_entry_artifacts_skipped(entry)
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

    def _record_artifact(self, entry_id: str, artifact: str, outcome: str | None = None) -> None:
        key = (entry_id, artifact)
        with self._statistics_lock:
            previous = self._run_artifact_outcomes.get(key)
            if previous == "downloaded" and outcome == "skipped":
                return
            if key not in self._run_artifact_outcomes or outcome is not None:
                self._run_artifact_outcomes[key] = outcome

    def _record_entry_artifacts_skipped(self, entry: BackupEntry) -> None:
        self._record_artifact(entry.entry_id, "Media files", "skipped")
        if self._configuration.export.save_metadata and not self._is_image_entry(entry):
            self._record_artifact(entry.entry_id, "Metadata", "skipped")
        if self._configuration.export.save_captions:
            self._record_artifact(entry.entry_id, "Captions", "skipped")
        if self._configuration.export.save_thumbnails:
            self._record_artifact(entry.entry_id, "Thumbnails", "skipped")
        if self._configuration.export.save_attachments:
            self._record_artifact(entry.entry_id, "Attachments", "skipped")

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

        entry.name = str(_read_value(result, "name", entry.name))
        entry.reference_id = str(_read_value(result, "referenceId", entry.reference_id))
        entry.owner_id = str(_read_value(result, "userId", entry.owner_id))
        entry.media_type = _normalize_media_type(
            _read_value(result, "mediaType", entry.media_type)
        )
        try:
            entry.duration_seconds = int(_read_value(result, "duration", entry.duration_seconds) or 0)
        except Exception:
            entry.duration_seconds = entry.duration_seconds
        try:
            entry.size_bytes = int(_read_value(result, "size", entry.size_bytes) or 0)
        except Exception:
            entry.size_bytes = entry.size_bytes
        try:
            entry.updated_at = int(_read_value(result, "updatedAt", entry.updated_at) or 0)
        except Exception:
            entry.updated_at = entry.updated_at
        try:
            entry.created_at = int(_read_value(result, "createdAt", entry.created_at) or 0)
        except Exception:
            entry.created_at = entry.created_at

        return result

    def _download_entry(self, entry: BackupEntry, entry_data: Any) -> None:
        """Create the entry backup artifacts and record progress using real downloads."""

        if self._dry_run:
            self._logger.info(EventId.APPLICATION_START, f"Dry run: would back up entry {entry.entry_id}")
            self._record_entry_artifacts_skipped(entry)
            entry.downloads.mark_completed(ArtifactType.MEDIA)
            self._state_manager.increment_statistic("bytes_downloaded", 0)
            self._state_manager.increment_statistic("api_calls")
            return

        backup_dir = self._configuration.paths.backup_dir / entry.entry_id
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Download media
        media_skipped = False
        self._record_artifact(entry.entry_id, "Media files")
        media_url = ""
        if self._source_media_is_stale_safe(entry) and self._media_file_exists(backup_dir, entry):
            media_skipped = True
            self._record_artifact(entry.entry_id, "Media files", "skipped")
            entry.downloads.mark_completed(ArtifactType.MEDIA)
            self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping unchanged media for old entry {entry.entry_id}")
        else:
            if self._is_image_entry(entry):
                media_url = self._source_image_url(entry_data)
                if not media_url:
                    media_skipped = True
                    self._logger.warning(
                        EventId.WARNING,
                        f"Image entry {entry.entry_id} has no direct Kaltura downloadUrl; skipping source download.",
                    )
            else:
                media_url_method = getattr(self._client_manager, "get_entry_media_url", None)
                if not callable(media_url_method):
                    media_skipped = True
                    self._logger.warning(EventId.WARNING, f"Client manager cannot provide a media URL for {entry.entry_id}")
                else:
                    try:
                        media_url_value = media_url_method(entry.entry_id)
                    except Exception as exc:
                        raise RetryableError(f"Failed to create media URL for {entry.entry_id}: {exc}") from exc

                    if not media_url_value:
                        raise RetryableError(f"Kaltura returned no media URL for {entry.entry_id}")
                    media_url = str(media_url_value)

            if not media_skipped:
                if not media_url:
                    raise RetryableError(f"No source media URL available for entry {entry.entry_id}")
                downloaded_bytes = self._download_media_from_url(entry, backup_dir, media_url)
                with self._statistics_lock:
                    self._run_bytes_downloaded += downloaded_bytes
                    self._state_manager.increment_statistic("bytes_downloaded", downloaded_bytes)
                self._record_artifact(entry.entry_id, "Media files", "downloaded")
                entry.downloads.mark_completed(ArtifactType.MEDIA)
            else:
                self._record_artifact(entry.entry_id, "Media files", "skipped")

        if self._configuration.export.save_metadata and not self._is_image_entry(entry):
            self._record_artifact(entry.entry_id, "Metadata")
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
                    except Exception as exc:
                        self._logger.warning(
                            EventId.WARNING,
                            f"Could not list metadata for entry {entry.entry_id}, profile {profile_id}: {exc}",
                        )
                        meta_objs = []

                    for meta in meta_objs:
                        # determine metadata id getter
                        meta_id = _read_value(meta, "id")

                        if not meta_id:
                            continue

                        xml = _read_value(meta, "xml")
                        if not xml:
                            try:
                                xml = self._client_manager.get_metadata_xml(meta_id)
                            except Exception as exc:
                                self._logger.warning(
                                    EventId.WARNING,
                                    f"Could not fetch metadata XML {meta_id} for entry {entry.entry_id}: {exc}",
                                )
                                continue

                        try:
                            root = ElementTree.fromstring(xml)
                        except Exception as exc:
                            self._logger.warning(
                                EventId.WARNING,
                                f"Could not parse metadata XML {meta_id} for entry {entry.entry_id}: {exc}",
                            )
                            continue

                        for field_name in fields:
                            if row.get(field_name):
                                continue
                            value = _find_xml_value(root, field_name)
                            if value:
                                row[field_name] = value

                writer.writerow(row)

            entry.downloads.mark_completed(ArtifactType.METADATA)
            self._state_manager.increment_statistic("metadata_written")
            self._record_artifact(entry.entry_id, "Metadata", "downloaded")

        if self._configuration.export.save_captions:
            self._record_artifact(entry.entry_id, "Captions")
            if self._entry_artifact_is_stale_safe(entry) and self._caption_file_exists(backup_dir):
                self._record_artifact(entry.entry_id, "Captions", "skipped")
                entry.downloads.mark_completed(ArtifactType.CAPTIONS)
                self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping unchanged captions for old entry {entry.entry_id}")
            else:
                try:
                    assets = self._client_manager.list_caption_assets(entry.entry_id)
                except Exception:
                    assets = []

                wrote_any = False
                for asset in assets:
                    asset_id = _read_value(asset, "id")

                    if not asset_id:
                        continue

                    try:
                        caption_url = self._client_manager.get_caption_url(asset_id)
                        response = requests.get(caption_url, timeout=30)
                        response.raise_for_status()
                        caption_bytes = response.content
                        if not caption_bytes:
                            continue
                        safe_asset_id = "".join(
                            character if character.isalnum() or character in {"-", "_", "."} else "_"
                            for character in str(asset_id)
                        )
                        extension = self._caption_extension(asset, caption_url)
                        caption_path = backup_dir / f"caption_{safe_asset_id}.{extension}"
                        caption_path.write_bytes(caption_bytes)
                        wrote_any = True
                    except CaptionAssetNotReadyError as exc:
                        self._logger.info(
                            EventId.ENTRY_SKIPPED,
                            f"Skipping caption asset {asset_id} for entry {entry.entry_id}: {exc}",
                        )
                        continue
                    except Exception as exc:
                        self._logger.warning(
                            EventId.WARNING,
                            f"Could not download caption asset {asset_id} for entry {entry.entry_id}: {exc}",
                        )
                        continue

                if wrote_any:
                    entry.downloads.mark_completed(ArtifactType.CAPTIONS)
                    self._state_manager.increment_statistic("captions_downloaded")
                    self._record_artifact(entry.entry_id, "Captions", "downloaded")
                else:
                    self._record_artifact(entry.entry_id, "Captions", "skipped")

        if self._configuration.export.save_thumbnails:
            self._record_artifact(entry.entry_id, "Thumbnails")
            stale_safe = self._entry_artifact_is_stale_safe(entry)
            regular_thumbnail_exists = (backup_dir / "thumbnail.jpg").exists()
            thumb_written = False
            if stale_safe and regular_thumbnail_exists:
                self._record_artifact(entry.entry_id, "Thumbnails", "skipped")
                entry.downloads.mark_completed(ArtifactType.THUMBNAILS)
                self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping unchanged thumbnail for old entry {entry.entry_id}")
            else:
                try:
                    thumbs = self._client_manager.list_thumb_assets(entry.entry_id)
                except Exception:
                    thumbs = []

                for thumb in thumbs:
                    thumb_id = _read_value(thumb, "id")

                    if not thumb_id:
                        continue

                    try:
                        url = self._client_manager.get_thumb_url(thumb_id)
                    except Exception:
                        continue

                    try:
                        resp = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        content = resp.content
                        if not content:
                            continue
                        (backup_dir / "thumbnail.jpg").write_bytes(content)
                        thumb_written = True
                        break
                    except Exception as exc:
                        self._logger.warning(
                            EventId.WARNING,
                            f"Could not download thumbnail {thumb_id} for entry {entry.entry_id}: {exc}",
                        )
                        continue

            timeline_assets = []
            list_timeline_assets = getattr(self._client_manager, "list_timeline_slide_assets", None)
            if callable(list_timeline_assets):
                try:
                    timeline_assets = list_timeline_assets(entry.entry_id)
                except Exception as exc:
                    self._logger.warning(
                        EventId.WARNING,
                        f"Could not list timeline slide images for entry {entry.entry_id}: {exc}",
                    )

            timeline_written = False
            timeline_skipped = False
            for cue_point in timeline_assets:
                asset_id = _read_value(cue_point, "assetId")
                cue_point_id = _read_value(cue_point, "id") or asset_id
                if not asset_id or asset_id == "N/A":
                    continue

                safe_id = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(cue_point_id)).rstrip(". ")
                extension = str(_read_value(cue_point, "fileExt", "jpg") or "jpg").lower().lstrip(".")
                if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
                    extension = "jpg"
                slide_path = backup_dir / f"timeline_slide_{safe_id or asset_id}.{extension}"
                if stale_safe and slide_path.is_file() and slide_path.stat().st_size > 0:
                    timeline_skipped = True
                    continue
                try:
                    image_url = self._client_manager.get_thumb_url(asset_id)
                    response = requests.get(image_url, timeout=30)
                    response.raise_for_status()
                    if not response.content:
                        continue
                    temporary_path = slide_path.with_name(f".{slide_path.name}.part")
                    temporary_path.write_bytes(response.content)
                    temporary_path.replace(slide_path)
                    timeline_written = True
                except Exception as exc:
                    self._logger.warning(
                        EventId.WARNING,
                        f"Could not download timeline slide image {asset_id} for entry {entry.entry_id}: {exc}",
                    )

            if thumb_written or timeline_written:
                entry.downloads.mark_completed(ArtifactType.THUMBNAILS)
                self._state_manager.increment_statistic("thumbnails_downloaded")
                self._record_artifact(entry.entry_id, "Thumbnails", "downloaded")
            elif regular_thumbnail_exists or timeline_skipped or stale_safe:
                self._record_artifact(entry.entry_id, "Thumbnails", "skipped")
            else:
                self._record_artifact(entry.entry_id, "Thumbnails", "skipped")

        if self._configuration.export.save_attachments:
            self._record_artifact(entry.entry_id, "Attachments")
            if self._entry_artifact_is_stale_safe(entry) and self._attachment_file_exists(backup_dir):
                self._record_artifact(entry.entry_id, "Attachments", "skipped")
                entry.downloads.mark_completed(ArtifactType.ATTACHMENTS)
                self._logger.info(EventId.ENTRY_SKIPPED, f"Skipping unchanged attachments for old entry {entry.entry_id}")
            else:
                try:
                    atts = self._client_manager.list_attachment_assets(entry.entry_id)
                except Exception as exc:
                    self._logger.warning(
                        EventId.WARNING,
                        f"Could not list attachments for entry {entry.entry_id}: {exc}",
                    )
                    atts = []

                att_written = False
                for att in atts:
                    att_id = _read_value(att, "id")
                    if not att_id:
                        continue

                    try:
                        url = self._client_manager.get_attachment_url(att_id)
                    except Exception as exc:
                        self._logger.warning(
                            EventId.WARNING,
                            f"Could not get attachment URL for {att_id} on entry {entry.entry_id}: {exc}",
                        )
                        continue

                    try:
                        response = requests.get(url, timeout=30)
                        response.raise_for_status()
                        content = response.content
                        if not content:
                            continue

                        filename = _read_value(att, "filename")
                        if filename is NotImplemented or not filename:
                            filename = _read_value(att, "title")
                        if filename is NotImplemented or not filename:
                            filename = unquote(urlparse(url).path.split("/")[-1])
                        if not filename:
                            filename = f"attachment_{att_id}"

                        extension = _read_value(att, "fileExt")
                        if extension is not NotImplemented and extension and not Path(str(filename)).suffix:
                            filename = f"{filename}.{str(extension).strip().lstrip('.')}"
                        filename = _safe_download_filename(str(filename), str(att_id))
                        attachment_path = backup_dir / filename
                        temporary_path = attachment_path.with_name(f".{attachment_path.name}.part")
                        temporary_path.write_bytes(content)
                        temporary_path.replace(attachment_path)
                        att_written = True
                    except Exception as exc:
                        self._logger.warning(
                            EventId.WARNING,
                            f"Could not download attachment {att_id} for entry {entry.entry_id}: {exc}",
                        )
                        continue

                if att_written:
                    entry.downloads.mark_completed(ArtifactType.ATTACHMENTS)
                    self._state_manager.increment_statistic("attachments_downloaded")
                    self._record_artifact(entry.entry_id, "Attachments", "downloaded")
                else:
                    self._record_artifact(entry.entry_id, "Attachments", "skipped")

        if media_skipped:
            self._logger.info(
                EventId.DOWNLOAD_SKIPPED,
                f"Source media download skipped for {entry.entry_id}.",
            )

        self._state_manager.increment_statistic("api_calls")

    def _is_image_entry(self, entry: BackupEntry) -> bool:
        return entry.media_type.lower() == "image"

    def _media_file_path(self, backup_dir: Any, entry: BackupEntry, explicit_name: str | None = None) -> Any:
        if explicit_name:
            return backup_dir / explicit_name

        name = entry.media_type.lower()

        if "video" in name:
            filename = "media.mp4"
        elif "audio" in name:
            filename = "audio.mp3"
        elif "image" in name:
            filename = "image.jpg"
        else:
            filename = "media.bin"

        matches = [
            backup_dir / filename,
            backup_dir / "media.mp4",
            backup_dir / "audio.mp3",
            backup_dir / "image.jpg",
            backup_dir / "media.bin",
        ]
        for candidate in matches:
            if candidate.exists():
                return candidate

        return backup_dir / filename

    def _media_file_exists(self, backup_dir: Any, entry: BackupEntry) -> bool:
        media_extensions = {
            "video": {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".ts", ".bin"},
            "audio": {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".wma", ".bin"},
            "image": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".bin"},
        }
        media_type = entry.media_type.lower()
        extensions = next(
            (values for kind, values in media_extensions.items() if kind in media_type),
            {".bin"},
        )

        candidates = [self._media_file_path(backup_dir, entry)]
        candidates.extend(
            path
            for path in backup_dir.iterdir()
            if path.name.lower() != "thumbnail.jpg" and path.suffix.lower() in extensions
        )
        checked: set[Any] = set()
        for media_path in candidates:
            if media_path in checked:
                continue
            checked.add(media_path)
            try:
                if not media_path.is_file() or media_path.stat().st_size <= 1024:
                    continue
                with media_path.open("rb") as media_file:
                    prefix = media_file.read(512)
                if not _is_error_media_payload(prefix, ""):
                    return True
            except OSError:
                continue
        return False

    def _source_media_is_stale_safe(self, entry: BackupEntry) -> bool:
        database_updated_at = (self._database_entry_updated_at or {}).get(entry.entry_id)
        updated_at = entry.updated_at if database_updated_at is None else database_updated_at
        if updated_at <= 0:
            return False
        return datetime.fromtimestamp(updated_at, tz=UTC) < datetime.now(UTC) - timedelta(hours=24)

    def _source_image_url(self, entry_data: Any) -> str | None:
        value = _read_value(entry_data, "downloadUrl")
        if value is None or value is NotImplemented:
            return None
        url = str(value).strip()
        if not url or url.lower() in {"notimplemented", "none"}:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return url

    def _download_media_from_url(self, entry: BackupEntry, backup_dir: Any, media_url: str) -> int:
        file_path = self._media_file_path(backup_dir, entry)
        temporary_path = file_path.with_name(f".{file_path.name}.part")
        try:
            with requests.get(media_url, stream=True, timeout=30) as response:
                response.raise_for_status()
                filename = _resolve_download_filename(response, file_path.name, media_url)
                filename = _safe_download_filename(filename, entry.entry_id)
                file_path = self._media_file_path(backup_dir, entry, filename)
                temporary_path = file_path.with_name(f".{file_path.name}.part")
                prefix = bytearray()
                byte_count = 0
                with temporary_path.open("wb") as media_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        if len(prefix) < 512:
                            prefix.extend(chunk[: 512 - len(prefix)])
                        media_file.write(chunk)
                        byte_count += len(chunk)

                content_type = (response.headers.get("Content-Type") or "").lower()
                if byte_count == 0 or _is_error_media_payload(bytes(prefix), content_type):
                    raise RetryableError(
                        f"Kaltura returned a non-media response for {entry.entry_id} "
                        f"(content-type={content_type or 'unknown'}, bytes={byte_count})"
                    )

            temporary_path.replace(file_path)
            return byte_count
        except RetryableError:
            temporary_path.unlink(missing_ok=True)
            raise
        except requests.RequestException as exc:
            temporary_path.unlink(missing_ok=True)
            raise RetryableError(f"Failed to download media for {entry.entry_id}: {exc}") from exc
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise RetryableError(f"Could not save media for {entry.entry_id}: {exc}") from exc

    @staticmethod
    def _caption_file_exists(backup_dir: Any) -> bool:
        return (backup_dir / "captions.vtt").exists() or any(
            path.is_file()
            for pattern in ("caption_*.srt", "caption_*.vtt")
            for path in backup_dir.glob(pattern)
        )

    @staticmethod
    def _caption_extension(asset: Any, caption_url: str) -> str:
        extension = str(_read_value(asset, "fileExt", "") or "").strip().lower().lstrip(".")
        if extension in {"srt", "vtt"}:
            return extension

        url_extension = Path(urlparse(caption_url).path).suffix.lower().lstrip(".")
        if url_extension in {"srt", "vtt"}:
            return url_extension
        return "vtt"

    @staticmethod
    def _attachment_file_exists(backup_dir: Any) -> bool:
        known_files = {
            "manifest.json",
            "metadata.csv",
            "api_response.json",
            "thumbnail.jpg",
            "media.mp4",
            "audio.mp3",
            "image.jpg",
            "media.bin",
            "captions.vtt",
        }
        return any(
            path.is_file()
            and path.name not in known_files
            and not path.name.startswith("caption_")
            for path in backup_dir.iterdir()
        )

    @staticmethod
    def _entry_artifact_is_stale_safe(entry: BackupEntry) -> bool:
        if entry.updated_at <= 0:
            return False
        updated_at = datetime.fromtimestamp(entry.updated_at, tz=UTC)
        return updated_at < datetime.now(UTC) - timedelta(hours=48)

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

        bytes_downloaded = self._run_bytes_downloaded
        api_calls = stats.api_calls if stats is not None else None
        elapsed_seconds = 0
        if self._run_started_at is not None:
            elapsed_seconds = max(
                0,
                int((datetime.now(UTC) - self._run_started_at).total_seconds()),
            )
        hours, remainder = divmod(elapsed_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        run_duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        summary_lines = [
            f"Backup summary: {report_payload['count']} entries processed",
            f"  Completed: {report_payload['completed']}, Failed: {report_payload['failed']}",
            f"  Duration: {run_duration}",
        ]

        with self._statistics_lock:
            artifact_outcomes = dict(self._run_artifact_outcomes)
        for artifact in SUMMARY_ARTIFACTS:
            outcomes = [
                outcome
                for (_entry_id, item), outcome in artifact_outcomes.items()
                if item == artifact
            ]
            total = len(outcomes)
            downloaded = sum(outcome == "downloaded" for outcome in outcomes)
            skipped = sum(outcome == "skipped" for outcome in outcomes)
            failed = total - downloaded - skipped
            summary_lines.append(
                f"  {artifact}: total {total}, downloaded {downloaded}, skipped {skipped}, failed {failed}"
            )

        summary_lines.append(f"  Bytes downloaded this run: {bytes_downloaded}")

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

        if self._database_entry_ids is not None:
            result = [
                self._client_manager.get_entry(entry_id)
                for entry_id in self._database_entry_ids
            ]
            self._logger.info(
                EventId.ENTRY_DISCOVERED,
                f"Loaded {len(result)} Type=1 entries selected by the database.",
            )
        else:
            result = self._client_manager.list_all_entries()
        return [
            BackupEntry(
                entry_id=str(_read_value(item, "id", "")),
                name=str(_read_value(item, "name", "")),
                updated_at=int(_read_value(item, "updatedAt", 0) or 0),
                created_at=int(_read_value(item, "createdAt", 0) or 0),
            )
            for item in result
        ]
