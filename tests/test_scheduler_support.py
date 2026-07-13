from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.cli import build_parser


def test_cli_parser_supports_scheduler_style_config_path() -> None:
    parser = build_parser()
    args = parser.parse_args(["--config", "config.ini"])
    assert args.config == "config.ini"
