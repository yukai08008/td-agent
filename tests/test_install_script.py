from __future__ import annotations

import subprocess
from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def test_install_script_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n", str(INSTALLER)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_rejects_invalid_version_before_installing():
    result = subprocess.run(
        ["bash", str(INSTALLER), "install", "0.2;whoami"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Invalid version" in result.stderr


def test_install_script_explains_unsupported_initial_release():
    result = subprocess.run(
        ["bash", str(INSTALLER), "install", "v0.1.0"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "public standalone versions start at v0.2.0" in result.stderr


def test_installer_latest_version_matches_package_and_uses_release_wheel():
    script = INSTALLER.read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]

    assert f'LATEST_VERSION="{version}"' in script
    assert "/releases/download/v${version}/toe_dac-${version}-py3-none-any.whl" in script


def test_installer_network_calls_are_bounded_and_visible():
    script = INSTALLER.read_text(encoding="utf-8")
    assert "--connect-timeout 10" in script
    assert "--max-time 120" in script
    assert "--retry 3" in script
    assert "--progress-bar" in script
