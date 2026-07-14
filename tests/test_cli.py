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
