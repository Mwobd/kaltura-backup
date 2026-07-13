from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.cli import build_parser


def test_build_parser_accepts_config_argument() -> None:
    parser = build_parser()
    args = parser.parse_args(["--config", "custom.ini"])
    assert args.config == "custom.ini"
