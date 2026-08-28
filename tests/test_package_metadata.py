import subprocess
import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_RESOURCE_WHEEL_PATHS = {
    "tau_coding/data/docs/README.md",
    "tau_coding/data/docs/extensions.md",
    "tau_coding/data/examples/extensions/hello_tool.py",
}


def test_python_version_floor_matches_package_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["requires-python"] == ">=3.12"
    assert pyproject["tool"]["ruff"]["target-version"] == "py312"
    assert pyproject["tool"]["mypy"]["python_version"] == "3.12"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"


def test_wheel_includes_builtin_resources_package_data(tmp_path: Path) -> None:
    """Regression: docs and example resources must be included in installed wheels."""
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, result.stdout + result.stderr
    with ZipFile(wheels[0]) as wheel:
        wheel_files = set(wheel.namelist())

    assert wheel_files >= BUILTIN_RESOURCE_WHEEL_PATHS
    assert not any(path.startswith("tau_coding/data/skills/") for path in wheel_files)
