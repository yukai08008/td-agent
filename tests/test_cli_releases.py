from __future__ import annotations

from types import SimpleNamespace

from toe_dac import cli


def test_use_version_runs_tag_in_isolated_uv_environment(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "toe-dac",
            "--use-version",
            "0.2.0",
            "--data",
            str(tmp_path / "v2-data"),
            "--version",
        ],
    )

    cli.main()

    assert captured["command"][:5] == [
        "uv",
        "tool",
        "run",
        "--from",
        "git+https://github.com/yukai08008/td-agent.git@v0.2.0",
    ]
    assert "--use-version" not in captured["command"]
    assert captured["command"][-3:] == ["--data", str(tmp_path / "v2-data"), "--version"]
    assert captured["environment"]["TOE_DAC_UPDATE_CHECK"] == "false"
