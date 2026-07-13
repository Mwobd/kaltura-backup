from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.backup import BackupManager
from kaltura_backup.config import (
    Configuration,
    ConnectionConfig,
    PathConfig,
    DownloadConfig,
    ExportConfig,
    LoggingConfig,
)
from kaltura_backup.models import BackupEntry
from kaltura_backup.state import StateManager
from kaltura_backup.logging_utils import initialize_logger
from kaltura_backup.exceptions import RetryableError


class FlakyClientManager:
    def __init__(self) -> None:
        self.calls = 0

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def list_entries(self, filter_object=None, pager=None):
        return []

    def get_entry(self, entry_id: str):
        self.calls += 1
        if self.calls < 3:
            raise RetryableError("transient failure")
        return {"id": entry_id, "name": "ok"}


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
            retry_count=2,
            retry_delay_seconds=0,
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
        logging=LoggingConfig(level="INFO", keep_logs=1),
    )


def test_backup_manager_retries_transient_failures(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    state_manager = StateManager(configuration)
    logger = initialize_logger(configuration)
    client_manager = FlakyClientManager()
    manager = BackupManager(
        configuration=configuration,
        state_manager=state_manager,
        client_manager=client_manager,
        logger=logger,
    )

    entry = BackupEntry(entry_id="entry-3", name="demo", updated_at=1, created_at=1)
    processed = manager.run([entry])

    assert processed[0].status.value == "COMPLETED"
    assert client_manager.calls == 3
    assert state_manager.statistics.entries_retried == 1
