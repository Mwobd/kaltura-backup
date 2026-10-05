import csv
from pathlib import Path
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.backup import BackupManager, _safe_download_filename
from kaltura_backup import backup as backup_module
from kaltura_backup.config import (
    Configuration,
    ConnectionConfig,
    PathConfig,
    DownloadConfig,
    ExportConfig,
    MetadataConfig,
    LoggingConfig,
)
from kaltura_backup.models import ArtifactType, BackupEntry, BackupStatus
from kaltura_backup.state import StateManager
from kaltura_backup.logging_utils import initialize_logger
from kaltura_backup.exceptions import RetryableError


class DummyClientManager:
    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def list_entries(self, filter_object=None, pager=None):
        return []

    def get_entry(self, entry_id: str):
        return {"id": entry_id, "name": "demo"}


def _make_configuration(tmp_path: Path) -> Configuration:
    return Configuration(
        connection=ConnectionConfig(
            partner_id=123,
            admin_secret="secret",
            service_url="https://example.invalid",
        ),
        paths=PathConfig(
            backup_dir=tmp_path / "backup",
            csv_dir=tmp_path / "csv",
            log_dir=tmp_path / "logs",
            report_dir=tmp_path / "reports",
            state_file=tmp_path / "backup_state.json",
        ),
        download=DownloadConfig(
            workers=1,
            retry_count=0,
            retry_delay_seconds=1,
            timeout=5,
            skip_older_than_hours=0,
            resume_downloads=False,
            verify_checksum=False,
        ),
        export=ExportConfig(
            save_metadata=True,
            save_api_responses=False,
            save_captions=False,
            save_thumbnails=False,
            save_attachments=False,
        ),
        metadata=MetadataConfig(profile_fields={}),
        logging=LoggingConfig(level="INFO", keep_logs=1),
    )


def test_backup_manager_processes_entries_and_updates_state(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    entry = BackupEntry(
        entry_id="entry-1",
        name="demo",
        updated_at=1,
        created_at=1,
    )

    processed = manager.run([entry])

    assert len(processed) == 1
    assert processed[0].status is BackupStatus.COMPLETED
    assert state_manager.get("entry-1").status is BackupStatus.COMPLETED
    assert state_manager.statistics.entries_completed == 1


def test_backup_manager_writes_artifacts_and_supports_resume(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    entry = BackupEntry(entry_id="entry-2", name="demo", updated_at=1, created_at=1)
    entry.downloads.mark_completed(ArtifactType.MEDIA)
    entry.downloads.media_completed = None

    manager.run([entry])

    backup_dir = configuration.paths.backup_dir / entry.entry_id
    assert (backup_dir / "manifest.json").exists()
    assert (backup_dir / "metadata.csv").exists()
    assert (backup_dir / "api_response.json").exists()


def test_backup_manager_emits_progress_updates(tmp_path: Path, capsys) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    entry = BackupEntry(entry_id="entry-4", name="demo", updated_at=1, created_at=1)
    manager.run([entry])

    captured = capsys.readouterr()
    assert "progress" in captured.out.lower()


def test_backup_manager_skips_existing_media_but_writes_artifacts(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    entry = BackupEntry(
        entry_id="entry-5",
        name="demo",
        updated_at=1,
        created_at=1,
        media_type="video",
    )
    backup_dir = configuration.paths.backup_dir / entry.entry_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "media.mp4").write_bytes(b"existing-media" * 200)

    manager.run([entry])

    assert (backup_dir / "media.mp4").read_bytes() == b"existing-media" * 200
    assert (backup_dir / "metadata.csv").exists()
    assert (backup_dir / "api_response.json").exists()


def test_backup_manager_uses_configured_worker_count(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    configuration = Configuration(
        connection=configuration.connection,
        paths=configuration.paths,
        download=DownloadConfig(
            workers=2,
            retry_count=configuration.download.retry_count,
            retry_delay_seconds=configuration.download.retry_delay_seconds,
            timeout=configuration.download.timeout,
            skip_older_than_hours=configuration.download.skip_older_than_hours,
            resume_downloads=configuration.download.resume_downloads,
            verify_checksum=configuration.download.verify_checksum,
        ),
        export=configuration.export,
        metadata=configuration.metadata,
        logging=configuration.logging,
    )
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    entries = [
        BackupEntry(entry_id=f"entry-{index}", name="demo", updated_at=1, created_at=1)
        for index in range(3)
    ]

    processed = manager.run(entries)

    assert len(processed) == 3
    assert all(entry.status is BackupStatus.COMPLETED for entry in processed)


def test_backup_manager_respects_shutdown_signal(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    manager._handle_shutdown_signal(15, None)

    assert manager._should_stop() is True


def test_backup_manager_supports_dict_like_entry_payloads(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)

    class DictClientManager(DummyClientManager):
        def list_all_entries(self):
            return [{"id": "entry-dict", "name": "demo", "updatedAt": 9, "createdAt": 1}]

        def get_entry(self, entry_id: str):
            return {
                "id": entry_id,
                "name": "demo",
                "referenceId": "ref-1",
                "userId": "owner-1",
                "mediaType": "video",
                "duration": 42,
                "size": 99,
                "updatedAt": 9,
                "createdAt": 1,
            }

    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DictClientManager(),
        logger=logger,
    )

    entries = manager.discover_entries()
    assert entries[0].entry_id == "entry-dict"
    assert entries[0].name == "demo"
    assert entries[0].media_type == ""

    processed = manager.run(entries)
    assert processed[0].status is BackupStatus.COMPLETED


def test_backup_metadata_uses_xml_on_listed_metadata_object(tmp_path: Path) -> None:
    base_configuration = _make_configuration(tmp_path)
    configuration = Configuration(
        connection=base_configuration.connection,
        paths=base_configuration.paths,
        download=base_configuration.download,
        export=base_configuration.export,
        metadata=MetadataConfig(profile_fields={"4696": ("Attributie",)}),
        logging=base_configuration.logging,
    )
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)

    class MetadataClient(DummyClientManager):
        def list_metadata_objects(self, entry_id, profile_id):
            assert (entry_id, profile_id) == ("entry-meta", "4696")
            return [SimpleNamespace(id=42, xml="<metadata><Attributie>Example value</Attributie></metadata>")]

        def get_metadata_xml(self, _metadata_id):
            raise AssertionError("Use the XML returned by metadata.list")

    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=MetadataClient(),
        logger=logger,
    )
    entry = BackupEntry("entry-meta", "metadata demo", updated_at=1, created_at=1, media_type="video")
    backup_dir = configuration.paths.backup_dir / entry.entry_id
    backup_dir.mkdir(parents=True)
    (backup_dir / "media.mp4").write_bytes(b"cached-media" * 200)

    manager._download_entry(entry, {})

    with (backup_dir / "metadata.csv").open(encoding="utf-8", newline="") as metadata_file:
        row = next(csv.DictReader(metadata_file))
    assert row["Attributie"] == "Example value"


def test_backup_manager_uses_database_entry_ids(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)

    class DatabaseClientManager(DummyClientManager):
        def list_all_entries(self):
            raise AssertionError("API-wide discovery must not run for database-backed backups")

    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DatabaseClientManager(),
        logger=logger,
        database_entry_ids=["entry-type-one"],
    )

    entries = manager.discover_entries()

    assert [entry.entry_id for entry in entries] == ["entry-type-one"]


def test_old_entry_with_existing_artifacts_is_safe_to_skip(tmp_path: Path) -> None:
    entry = BackupEntry(
        entry_id="entry-old",
        name="old",
        updated_at=int((datetime.now(UTC) - timedelta(hours=49)).timestamp()),
        created_at=1,
    )
    backup_dir = tmp_path / "entry-old"
    backup_dir.mkdir()
    (backup_dir / "media.mp4").write_bytes(b"existing-media" * 200)
    (backup_dir / "caption_caption-1.json").write_text("{}", encoding="utf-8")

    assert BackupManager._entry_artifact_is_stale_safe(entry) is True
    manager = object.__new__(BackupManager)
    assert manager._media_file_exists(backup_dir, entry) is True
    assert manager._caption_file_exists(backup_dir) is True


def test_small_error_body_is_not_considered_existing_media(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = BackupManager(
        configuration=configuration,
        state_manager=StateManager(configuration),
        client_manager=DummyClientManager(),
        logger=initialize_logger(configuration),
    )
    backup_dir = tmp_path / "entry-error"
    backup_dir.mkdir()
    (backup_dir / "media.mp4").write_bytes(b"<html>" + b"x" * 2048)
    entry = BackupEntry("entry-error", "old", updated_at=1, created_at=1, media_type="video")

    assert manager._media_file_exists(backup_dir, entry) is False


def test_recent_entry_is_not_safe_to_skip(tmp_path: Path) -> None:
    entry = BackupEntry(
        entry_id="entry-recent",
        name="recent",
        updated_at=int((datetime.now(UTC) - timedelta(hours=47)).timestamp()),
        created_at=1,
    )

    assert BackupManager._entry_artifact_is_stale_safe(entry) is False


def test_kaltura_numeric_media_type_is_normalized_to_video(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = BackupManager(
        configuration=configuration,
        state_manager=StateManager(configuration),
        client_manager=DummyClientManager(),
        logger=initialize_logger(configuration),
    )

    class MediaType:
        value = 1

    class KalturaEntry:
        mediaType = MediaType()
        updatedAt = 2_000_000_000
        createdAt = 1

    entry = BackupEntry("entry-capture", "Capture", updated_at=1, created_at=1)
    manager._client_manager.get_entry = lambda _entry_id: KalturaEntry()

    manager._fetch_entry(entry)

    assert entry.media_type == "video"
    assert manager._media_file_path(configuration.paths.backup_dir, entry).name == "media.mp4"


def test_invalid_media_filename_falls_back_to_entry_id_and_keeps_extension() -> None:
    assert _safe_download_filename("A recording: Final.mp4", "0_6ka2rf8b") == "A recording_ Final.mp4"
    assert _safe_download_filename("capture.webm", "0_6ka2rf8b") == "capture.webm"


def test_windows_reserved_media_filename_falls_back_to_entry_id() -> None:
    assert _safe_download_filename("CON.mov", "entry-123") == "entry-123.mov"


def test_http_200_xml_error_is_not_saved_as_media(tmp_path: Path, monkeypatch) -> None:
    configuration = _make_configuration(tmp_path)

    class MediaClient(DummyClientManager):
        def get_entry_media_url(self, _entry_id: str) -> str:
            return "https://example.invalid/playManifest"

    class Response:
        headers = {"Content-Type": "application/xml"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int):
            return iter([b"<xml>" + b"x" * 1024 + b"</xml>"])

    monkeypatch.setattr(backup_module.requests, "get", lambda *args, **kwargs: Response())
    backup_dir = configuration.paths.backup_dir / "entry-capture-error"
    manager = BackupManager(
        configuration=configuration,
        state_manager=StateManager(configuration),
        client_manager=MediaClient(),
        logger=initialize_logger(configuration),
    )
    entry = BackupEntry("entry-capture-error", "Capture", updated_at=2_000_000_000, created_at=1, media_type="video")

    try:
        manager._download_entry(entry, {})
    except RetryableError:
        pass
    else:
        raise AssertionError("Expected an XML error response to fail media download")

    assert not (backup_dir / "media.mp4").exists()
    assert not (backup_dir / "media.bin").exists()
    assert not list(backup_dir.glob("*.part"))


def test_image_entry_downloads_from_direct_entry_url(tmp_path: Path, monkeypatch) -> None:
    configuration = _make_configuration(tmp_path)

    class Client(DummyClientManager):
        def get_entry_media_url(self, _entry_id: str) -> str:
            raise AssertionError("Image entries must not use the PlayManifest URL")

    class Response:
        headers = {
            "Content-Type": "image/png",
            "Content-Disposition": 'attachment; filename="Capture: image.png"',
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int):
            return iter([b"\x89PNG\r\n\x1a\n" + b"x" * 2048])

    requested_urls = []
    monkeypatch.setattr(
        backup_module.requests,
        "get",
        lambda url, **kwargs: (requested_urls.append(url) or Response()),
    )
    manager = BackupManager(
        configuration=configuration,
        state_manager=StateManager(configuration),
        client_manager=Client(),
        logger=initialize_logger(configuration),
    )
    entry = BackupEntry("image-entry", "Capture", updated_at=2_000_000_000, created_at=1, media_type="image")

    manager._download_entry(entry, {"downloadUrl": "https://cdn.example/image-entry.png"})

    image_path = configuration.paths.backup_dir / entry.entry_id / "Capture_ image.png"
    assert requested_urls == ["https://cdn.example/image-entry.png"]
    assert image_path.read_bytes().startswith(b"\x89PNG")
    assert entry.downloads.media is True


def test_image_without_direct_url_is_skipped_without_retrying(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)

    class Client(DummyClientManager):
        def get_entry_media_url(self, _entry_id: str) -> str:
            raise AssertionError("Image entries must not use the PlayManifest URL")

    manager = BackupManager(
        configuration=configuration,
        state_manager=StateManager(configuration),
        client_manager=Client(),
        logger=initialize_logger(configuration),
    )
    entry = BackupEntry("image-no-url", "Capture", updated_at=2_000_000_000, created_at=1, media_type="image")

    manager._download_entry(entry, {"downloadUrl": NotImplemented})

    assert entry.downloads.media is False
    assert not (configuration.paths.backup_dir / entry.entry_id / "image.jpg").exists()


def test_old_entry_detects_all_existing_non_metadata_artifacts(tmp_path: Path) -> None:
    backup_dir = tmp_path / "entry-old"
    backup_dir.mkdir()
    (backup_dir / "api_response.json").write_text("{}", encoding="utf-8")
    (backup_dir / "thumbnail.jpg").write_bytes(b"image")
    (backup_dir / "attachment.pdf").write_bytes(b"attachment")

    assert (backup_dir / "api_response.json").exists()
    assert BackupManager._caption_file_exists(backup_dir) is False
    assert BackupManager._attachment_file_exists(backup_dir) is True


def test_backup_manager_disconnects_client_on_failure(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)

    class FailingClientManager(DummyClientManager):
        def __init__(self) -> None:
            self.disconnect_calls = 0

        def connect(self) -> None:
            return None

        def disconnect(self) -> None:
            self.disconnect_calls += 1

        def get_entry(self, entry_id: str):
            raise RuntimeError("boom")

    client_manager = FailingClientManager()
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=client_manager,
        logger=logger,
    )

    entry = BackupEntry(entry_id="entry-3", name="demo", updated_at=1, created_at=1)

    try:
        manager.run([entry])
    except RuntimeError:
        pass

    assert client_manager.disconnect_calls == 1
