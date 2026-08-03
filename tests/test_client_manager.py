from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.client import KalturaClientManager, KalturaSession
from kaltura_backup.config import (
    Configuration,
    ConnectionConfig,
    PathConfig,
    DownloadConfig,
    ExportConfig,
    MetadataConfig,
    LoggingConfig,
)


class _FakeBaseEntry:
    def __init__(self) -> None:
        self.calls = []

    def list(self, filter_object, pager):
        self.calls.append((filter_object, pager))
        return [{"id": "1", "name": "demo"}]


class _FakeClient:
    def __init__(self) -> None:
        self.baseEntry = _FakeBaseEntry()


class _NullLogger:
    def debug(self, *args, **kwargs) -> None:
        return None

    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None

    def error(self, *args, **kwargs) -> None:
        return None

    def exception(self, *args, **kwargs) -> None:
        return None

    def retry(self, *args, **kwargs) -> None:
        return None


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
        metadata=MetadataConfig(profile_fields={}),
        logging=LoggingConfig(level="INFO", keep_logs=1),
    )


def test_list_entries_accepts_default_arguments(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())
    fake_client = _FakeClient()
    manager._pool = [KalturaSession(fake_client, "ks")]
    manager._connected = True

    result = manager.list_entries()

    assert result == [{"id": "1", "name": "demo"}]
    assert fake_client.baseEntry.calls == [(None, None)]
