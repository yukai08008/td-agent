from __future__ import annotations

import subprocess
from pathlib import Path


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
