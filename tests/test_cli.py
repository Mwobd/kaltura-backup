from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.cli import build_parser, main


def test_build_parser_accepts_config_argument() -> None:
    parser = build_parser()
    args = parser.parse_args(["--config", "custom.ini"])
    assert args.config == "custom.ini"


def test_main_returns_error_code_for_missing_config(tmp_path: Path, capsys) -> None:
    missing_config = tmp_path / "missing.ini"

    exit_code = main(["--config", str(missing_config)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Configuration error" in captured.err
