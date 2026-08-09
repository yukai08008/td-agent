from __future__ import annotations

import pytest

from toe_dac.releases import forwarded_version_args, normalize_release_version, package_spec


def test_normalize_release_version_accepts_tag_or_plain_version():
    assert normalize_release_version("0.2.0") == "0.2.0"
    assert normalize_release_version("v0.2.0") == "0.2.0"
    assert normalize_release_version("1.0.0-rc.1") == "1.0.0-rc.1"


@pytest.mark.parametrize("value", ["main", "0.2", "0.2.0;whoami", "vnext"])
def test_normalize_release_version_rejects_non_release_refs(value):
    with pytest.raises(ValueError, match="invalid release version"):
        normalize_release_version(value)


def test_normalize_release_version_rejects_private_dependency_release():
    with pytest.raises(ValueError, match="standalone versions start at v0.2.0"):
        normalize_release_version("0.1.0")


def test_package_spec_uses_latest_or_exact_tag():
    assert package_spec().endswith("td-agent.git")
    assert package_spec("v0.2.0").endswith("td-agent.git@v0.2.0")


def test_forwarded_version_args_remove_launcher_selector():
    assert forwarded_version_args(
        ["--use-version", "0.2.0", "--data", "/tmp/v2", "new"],
    ) == ["--data", "/tmp/v2", "new"]
    assert forwarded_version_args(["--use-version=0.2.0", "--version"]) == ["--version"]
