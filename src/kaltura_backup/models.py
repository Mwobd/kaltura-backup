"""
Core data models for the Kaltura Backup application.

These dataclasses represent the application's domain objects and are
shared across all modules. They are intentionally independent of the
Kaltura client implementation.

Author:
    <your name>

Python:
    >= 3.11
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


# ============================================================================
# Enumerations
# ============================================================================


class BackupStatus(StrEnum):
    """
    Processing state of a Kaltura entry.
    """

    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"

    DOWNLOADING_MEDIA = "DOWNLOADING_MEDIA"
    DOWNLOADING_CAPTIONS = "DOWNLOADING_CAPTIONS"
    DOWNLOADING_THUMBNAILS = "DOWNLOADING_THUMBNAILS"
    DOWNLOADING_ATTACHMENTS = "DOWNLOADING_ATTACHMENTS"
    WRITING_METADATA = "WRITING_METADATA"
    WRITING_MANIFEST = "WRITING_MANIFEST"

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ArtifactType(StrEnum):
    """
    Backup artifact types.
    """

    MEDIA = "media"
    METADATA = "metadata"
    CAPTIONS = "captions"
    THUMBNAILS = "thumbnails"
    ATTACHMENTS = "attachments"
    API_RESPONSE = "api_response"

class Statistic(StrEnum):
    ENTRIES_DISCOVERED = "entries_discovered"
    ENTRIES_PROCESSED = "entries_processed"
    ENTRIES_COMPLETED = "entries_completed"
    ENTRIES_SKIPPED = "entries_skipped"
    ENTRIES_FAILED = "entries_failed"
    ENTRIES_RETRIED = "entries_retried"

    MEDIA_DOWNLOADED = "media_downloaded"
    METADATA_WRITTEN = "metadata_written"
    CAPTIONS_DOWNLOADED = "captions_downloaded"
    THUMBNAILS_DOWNLOADED = "thumbnails_downloaded"
    ATTACHMENTS_DOWNLOADED = "attachments_downloaded"

    API_RESPONSES_SAVED = "api_responses_saved"

    BYTES_DOWNLOADED = "bytes_downloaded"

    API_CALLS = "api_calls"

# ============================================================================
# Download status
# ============================================================================


@dataclass(slots=True)
class DownloadStatus:
    """
    Tracks which artifacts have been backed up for one entry.
    """

    media: bool = False
    metadata: bool = False
    captions: bool = False
    thumbnails: bool = False
    attachments: bool = False
    api_response: bool = False

    media_completed: datetime | None = None
    metadata_completed: datetime | None = None
    captions_completed: datetime | None = None
    thumbnails_completed: datetime | None = None
    attachments_completed: datetime | None = None
    api_response_completed: datetime | None = None

    def mark_completed(self, artifact: ArtifactType) -> None:
        """
        Mark an artifact as successfully backed up.
        """

        now = datetime.now(UTC)

        match artifact:

            case ArtifactType.MEDIA:
                self.media = True
                self.media_completed = now

            case ArtifactType.METADATA:
                self.metadata = True
                self.metadata_completed = now

            case ArtifactType.CAPTIONS:
                self.captions = True
                self.captions_completed = now

            case ArtifactType.THUMBNAILS:
                self.thumbnails = True
                self.thumbnails_completed = now

            case ArtifactType.ATTACHMENTS:
                self.attachments = True
                self.attachments_completed = now

            case ArtifactType.API_RESPONSE:
                self.api_response = True
                self.api_response_completed = now


# ============================================================================
# Retry information
# ============================================================================


@dataclass(slots=True)
class RetryInfo:
    """
    Retry history for a single entry.
    """

    count: int = 0
    maximum: int = 3

    last_attempt: datetime | None = None

    last_exception: str = ""
    last_message: str = ""

    def register_failure(
        self,
        exception: Exception,
    ) -> None:
        """
        Register a failed attempt.
        """

        self.count += 1
        self.last_attempt = datetime.now(UTC)
        self.last_exception = type(exception).__name__
        self.last_message = str(exception)

    @property
    def exceeded(self) -> bool:
        """
        True when the retry limit has been reached.
        """

        return self.count >= self.maximum


# ============================================================================
# Download result
# ============================================================================


@dataclass(slots=True)
class DownloadResult:
    """
    Result returned by download operations.
    """

    success: bool

    bytes_downloaded: int = 0

    duration_seconds: float = 0.0

    filename: str = ""

    checksum: str = ""

    message: str = ""


# ============================================================================
# Backup entry (part 1)
# ============================================================================


@dataclass(slots=True)
class BackupEntry:
    """
    Represents one Kaltura entry during the backup process.
    """

    entry_id: str

    name: str

    updated_at: int

    created_at: int

    status: BackupStatus = BackupStatus.DISCOVERED

    reference_id: str = ""

    owner_id: str = ""

    media_type: str = ""

    size_bytes: int = 0

    duration_seconds: int = 0

    downloads: DownloadStatus = field(
        default_factory=DownloadStatus
    )

    retry: RetryInfo = field(
        default_factory=RetryInfo
    )

    last_backup: datetime | None = None

    last_error: str = ""

    backup_version: int = 1

    def start(self) -> None:
        """
        Mark the entry as being processed.
        """

        self.status = BackupStatus.PROCESSING

    def fail(
        self,
        message: str,
    ) -> None:
        """
        Mark the entry as failed.
        """

        self.status = BackupStatus.FAILED
        self.last_error = message

    def complete(self) -> None:
        """
        Mark the entry as completed.
        """

        self.status = BackupStatus.COMPLETED
        self.last_backup = datetime.now(UTC)

    def skip(self) -> None:
        """
        Mark the entry as skipped.
        """

        self.status = BackupStatus.SKIPPED

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the entry into a JSON-serializable dictionary.
        """

        data = asdict(self)

        if data["last_backup"] is not None:
            data["last_backup"] = data["last_backup"].isoformat()

        for key in (
            "media_completed",
            "metadata_completed",
            "captions_completed",
            "thumbnails_completed",
            "attachments_completed",
            "api_response_completed",
        ):
            value = data["downloads"][key]
            if value is not None:
                data["downloads"][key] = value.isoformat()

        if data["retry"]["last_attempt"] is not None:
            data["retry"]["last_attempt"] = data["retry"]["last_attempt"].isoformat()

        return data

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
    ) -> "BackupEntry":
        """
        Restore an entry from a JSON dictionary.
        """

        entry_data = dict(data)

        if isinstance(entry_data.get("status"), str):
            entry_data["status"] = BackupStatus(entry_data["status"])

        downloads_data = entry_data.get("downloads", {})
        if isinstance(downloads_data, dict):
            entry_data["downloads"] = DownloadStatus(**downloads_data)

        retry_data = entry_data.get("retry", {})
        if isinstance(retry_data, dict):
            entry_data["retry"] = RetryInfo(**retry_data)

        entry = cls(**entry_data)

        entry.last_backup = cls._parse_datetime(entry.last_backup)
        cls._restore_download_dates(entry.downloads)
        cls._restore_retry_dates(entry.retry)

        return entry

    @staticmethod
    def _parse_datetime(
        value: Any,
    ) -> datetime | None:
        """
        Convert a datetime-like value to a datetime object.
        """

        if value in (None, ""):
            return None

        if isinstance(value, datetime):
            return value

        return datetime.fromisoformat(value)

    @classmethod
    def _restore_download_dates(
        cls,
        downloads: DownloadStatus,
    ) -> None:
        """
        Restore nested download timestamps from the serialized form.
        """

        downloads.media_completed = cls._parse_datetime(
            downloads.media_completed
        )

        downloads.metadata_completed = cls._parse_datetime(
            downloads.metadata_completed
        )

        downloads.captions_completed = cls._parse_datetime(
            downloads.captions_completed
        )

        downloads.thumbnails_completed = cls._parse_datetime(
            downloads.thumbnails_completed
        )

        downloads.attachments_completed = cls._parse_datetime(
            downloads.attachments_completed
        )

        downloads.api_response_completed = cls._parse_datetime(
            downloads.api_response_completed
        )

    @classmethod
    def _restore_retry_dates(
        cls,
        retry: RetryInfo,
    ) -> None:
        """
        Restore the retry timestamp from the serialized form.
        """

        retry.last_attempt = cls._parse_datetime(
            retry.last_attempt
        )


# ============================================================================
# Backup statistics
# ============================================================================


@dataclass(slots=True)
class BackupStatistics:
    """
    Runtime statistics collected during a backup run.
    """

    entries_discovered: int = 0

    entries_processed: int = 0

    entries_completed: int = 0

    entries_skipped: int = 0

    entries_failed: int = 0

    entries_retried: int = 0

    media_downloaded: int = 0

    metadata_written: int = 0

    captions_downloaded: int = 0

    thumbnails_downloaded: int = 0

    attachments_downloaded: int = 0

    api_responses_saved: int = 0

    bytes_downloaded: int = 0

    api_calls: int = 0

    started: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    finished: datetime | None = None

    # ------------------------------------------------------------------

    def finish(self) -> None:
        """
        Mark the backup as finished.
        """

        self.finished = datetime.now(UTC)

    # ------------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        """
        Total runtime in seconds.
        """

        end = self.finished or datetime.now(UTC)

        return (
            end - self.started
        ).total_seconds()

    # ------------------------------------------------------------------

    @property
    def average_speed(self) -> float:
        """
        Average download speed in bytes per second.
        """

        runtime = self.elapsed_seconds

        if runtime <= 0:
            return 0.0

        return self.bytes_downloaded / runtime

    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Convert statistics into a JSON-serializable dictionary.
        """

        data = asdict(self)

        data["started"] = self.started.isoformat()

        if self.finished is not None:
            data["finished"] = self.finished.isoformat()

        return data

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
    ) -> "BackupStatistics":
        """
        Restore statistics from a JSON dictionary.
        """

        statistics = cls(**data)

        if isinstance(statistics.started, (int, float)):
            statistics.started = datetime.fromtimestamp(
                statistics.started,
                UTC,
            )

        if isinstance(statistics.finished, (int, float)):
            statistics.finished = datetime.fromtimestamp(
                statistics.finished,
                UTC,
            )
        elif isinstance(statistics.finished, str):
            statistics.finished = datetime.fromisoformat(
                statistics.finished
            )

        return statistics


# ============================================================================
# Utility
# ============================================================================


def new_entry(
    entry_id: str,
    name: str,
    updated_at: int,
    created_at: int,
) -> BackupEntry:
    """
    Create a new BackupEntry with sensible defaults.
    """

    return BackupEntry(
        entry_id=entry_id,
        name=name,
        updated_at=updated_at,
        created_at=created_at,
    )