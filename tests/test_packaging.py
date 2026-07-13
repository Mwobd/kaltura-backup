from pathlib import Path
import os
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"


def test_module_entrypoint_runs_with_python_m(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")

    completed = subprocess.run(
        [sys.executable, "-m", "kaltura_backup", "--config", str(tmp_path / "missing.ini")],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 2
    assert "Configuration error" in completed.stderr
