from __future__ import annotations

import io

import pytest

from toe_dac import changelog


SAMPLE = """# Changelog

## [Unreleased]

- Next change.

## [0.2.0] - 2026-08-09

### Added

- Changelog command.

## [0.1.0] - 2026-08-09

### Added

- Initial release.

[Unreleased]: https://example.test/compare
[0.2.0]: https://example.test/0.2.0
[0.1.0]: https://example.test/0.1.0
"""


def test_extract_version_accepts_plain_and_tag_versions():
    expected = "## [0.2.0] - 2026-08-09\n\n### Added\n\n- Changelog command."
    assert changelog.extract_version(SAMPLE, "0.2.0") == expected
    assert changelog.extract_version(SAMPLE, "v0.2.0") == expected


def test_extract_last_version_omits_reference_links():
    section = changelog.extract_version(SAMPLE, "0.1.0")
    assert "Initial release" in section
    assert "[0.1.0]:" not in section


def test_extract_version_rejects_unknown_version():
    with pytest.raises(ValueError, match="version not found"):
        changelog.extract_version(SAMPLE, "9.9.9")


def test_load_changelog_prefers_worktree_file(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        changelog.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network should not be used")),
    )

    assert changelog.load_changelog() == SAMPLE


def test_load_changelog_caches_remote_and_falls_back_offline(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        changelog.urllib.request,
        "urlopen",
        lambda *args, **kwargs: Response(SAMPLE.encode()),
    )
    assert changelog.load_changelog() == SAMPLE

    monkeypatch.setattr(
        changelog.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    assert changelog.load_changelog() == SAMPLE
