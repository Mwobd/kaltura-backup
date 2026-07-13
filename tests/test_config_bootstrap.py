from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.config import ensure_default_config


def test_ensure_default_config_creates_example_file(tmp_path: Path) -> None:
    target = tmp_path / "config.ini"

    created = ensure_default_config(target)

    assert created is True
    assert target.exists()
    assert "[Connection]" in target.read_text(encoding="utf-8")
