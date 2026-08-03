from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.cli import build_parser, main


def test_build_parser_accepts_config_argument() -> None:
    parser = build_parser()
    args = parser.parse_args(["--config", "custom.ini"])
    assert args.config == "custom.ini"


def test_build_parser_includes_help_text() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert "Run the Kaltura backup workflow" in help_text
    assert "--config" in help_text


def test_build_parser_accepts_dry_run_argument() -> None:
    parser = build_parser()
    args = parser.parse_args(["--dry-run"])

    assert args.dry_run is True


def test_build_parser_accepts_retry_failed_argument() -> None:
    parser = build_parser()
    args = parser.parse_args(["--retry-failed"])

    assert args.retry_failed is True


def test_build_parser_supports_version_argument(capsys) -> None:
    parser = build_parser()

    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("Expected SystemExit for --version")

    captured = capsys.readouterr()
    assert "kaltura-backup" in captured.out


def test_main_returns_error_code_for_missing_config(tmp_path: Path, capsys) -> None:
    missing_config = tmp_path / "missing.ini"

    exit_code = main(["--config", str(missing_config)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Configuration error" in captured.err


class DummyClientManager:
    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def list_entries(self, *args, **kwargs):
        return []

    def get_entry(self, entry_id: str):
        return {"id": entry_id, "name": "demo"}


def test_main_retries_failed_entries_from_state(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.ini"
    state_path = tmp_path / "backup_state.json"

    config_path.write_text(
        "[Connection]\nPartnerId = 123\nAdminSecret = secret\nServiceUrl = https://example.invalid\n"
        "[Paths]\nBackupDir = backups\nCsvDir = csv\nLogDir = logs\nReportDir = reports\nStateFile = " + str(state_path).replace('\\', '/') + "\n"
        "[Download]\nWorkers = 1\nRetryCount = 0\nRetryDelaySeconds = 1\nTimeout = 5\nSkipOlderThanHours = 1\nResumeDownloads = false\nVerifyChecksum = false\n"
        "[Export]\nSaveMetadata = true\nSaveApiResponses = false\nSaveCaptions = false\nSaveThumbnails = false\nSaveAttachments = false\n"
        "[metadata_profiles]\n"
        "[Logging]\nLevel = INFO\nKeepLogs = 1\n",
        encoding="utf-8",
    )

    state_path.write_text(
        '{"format_version": 1, "partner_id": 123, "created_at": 0, "updated_at": 0, "statistics": {}, "entries": {"entry-1": {"entry_id": "entry-1", "name": "demo", "updated_at": 1, "created_at": 1, "status": "FAILED", "downloads": {}, "retry": {}}}}',
        encoding="utf-8",
    )

    exit_code = main(
        ["--config", str(config_path), "--retry-failed"],
        client_manager_factory=lambda configuration, logger: DummyClientManager(),
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Starting Kaltura backup" in captured.out
    assert state_path.exists()
