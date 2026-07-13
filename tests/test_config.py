from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.config import ConfigLoader
from kaltura_backup.exceptions import ConfigurationError


def test_loader_raises_clear_error_for_missing_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text("[Connection]\nPartnerId = 1\n", encoding="utf-8")

    loader = ConfigLoader(config_path)

    try:
        loader.load()
    except ConfigurationError as exc:
        assert "Missing configuration section [Paths]" in str(exc)
    else:
        raise AssertionError("Expected ConfigurationError")
