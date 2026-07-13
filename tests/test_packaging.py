from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"
