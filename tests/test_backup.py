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
from kaltura_backup.models import ArtifactType, BackupEntry, BackupStatus
from kaltura_backup.state import StateManager
from kaltura_backup.logging_utils import initialize_logger


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
    assert (backup_dir / "metadata.json").exists()
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
