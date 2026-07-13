from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.config import (
    Configuration,
    ConnectionConfig,
    PathConfig,
    DownloadConfig,
    ExportConfig,
    LoggingConfig,
)
from kaltura_backup.state import StateManager


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
            save_metadata=False,
            save_api_responses=False,
            save_captions=False,
            save_thumbnails=False,
            save_attachments=False,
        ),
        logging=LoggingConfig(level="INFO", keep_logs=1),
    )


def test_state_manager_loads_finished_timestamp_from_disk(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)

    state_manager.increment_statistic("entries_completed")
    state_manager.statistics.finish()
    state_manager.save()

    reloaded = StateManager(configuration)
    reloaded.load()

    assert reloaded.statistics.entries_completed == 1
    assert reloaded.statistics.finished is not None
