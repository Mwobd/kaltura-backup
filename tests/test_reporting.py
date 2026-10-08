from pathlib import Path
from datetime import UTC, datetime, timedelta
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.backup import BackupManager
from kaltura_backup.config import (
    Configuration,
    ConnectionConfig,
    PathConfig,
    DownloadConfig,
    ExportConfig,
    MetadataConfig,
    LoggingConfig,
)
from kaltura_backup.logging_utils import initialize_logger
from kaltura_backup.models import BackupEntry
from kaltura_backup.state import StateManager


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
            save_api_responses=True,
            save_captions=False,
            save_thumbnails=False,
            save_attachments=False,
        ),
        metadata=MetadataConfig(profile_fields={}),
        logging=LoggingConfig(level="INFO", keep_logs=1),
    )


def test_backup_manager_writes_detailed_report(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
    )

    entry = BackupEntry(entry_id="entry-9", name="demo", updated_at=1, created_at=1)
    manager.run([entry])

    report_path = configuration.paths.report_dir / "backup_report.json"
    assert report_path.exists()

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["completed"] == 1
    assert payload["failed"] == 0
    assert payload["entries"][0]["entry_id"] == "entry-9"


def test_summary_reports_bytes_downloaded_this_run(tmp_path: Path, capsys) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    state_manager.increment_statistic("bytes_downloaded", 10_966_136_606)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=initialize_logger(configuration),
    )
    manager._run_bytes_downloaded = 835_252_329
    manager._run_started_at = datetime.now(UTC) - timedelta(seconds=65)

    manager._write_report([])

    summary = capsys.readouterr().out
    assert "Bytes downloaded this run: 835252329" in summary
    assert "Duration: 00:01:05" in summary


def test_skipped_media_run_reports_zero_not_persisted_byte_total(tmp_path: Path, capsys) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    state_manager.increment_statistic("bytes_downloaded", 179_000_000)
    logger = initialize_logger(configuration)
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=DummyClientManager(),
        logger=logger,
        database_entry_updated_at={
            "entry-old": int((datetime.now(UTC) - timedelta(hours=25)).timestamp())
        },
    )
    entry = BackupEntry(
        entry_id="entry-old",
        name="old video",
        updated_at=int(datetime.now(UTC).timestamp()),
        created_at=1,
        media_type="video",
    )
    backup_dir = configuration.paths.backup_dir / entry.entry_id
    backup_dir.mkdir(parents=True)
    (backup_dir / "old video.mp4").write_bytes(b"cached-video" * 200)

    manager.run([entry])

    assert "Bytes downloaded this run: 0" in capsys.readouterr().out
    assert state_manager.statistics.bytes_downloaded == 179_000_000


def test_summary_splits_artifact_totals_downloads_and_skips(tmp_path: Path, capsys) -> None:
    configuration = _make_configuration(tmp_path)
    manager = BackupManager(
        configuration=configuration,
        state_manager=StateManager(configuration),
        client_manager=DummyClientManager(),
        logger=initialize_logger(configuration),
    )
    manager._record_artifact("video-1", "Media files", "downloaded")
    manager._record_artifact("video-2", "Media files", "skipped")
    manager._record_artifact("video-3", "Media files")
    manager._record_artifact("caption-1", "Captions", "downloaded")
    manager._record_artifact("caption-2", "Captions", "skipped")

    manager._write_report([])

    summary = capsys.readouterr().out
    assert "Media files: total 3, downloaded 1, skipped 1, failed 1" in summary
    assert "Captions: total 2, downloaded 1, skipped 1, failed 0" in summary
