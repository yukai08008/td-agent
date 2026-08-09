from __future__ import annotations

from toe_dac.environment import load_environment


def test_env_file_precedence_is_local_then_env_then_example(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\nversion='0'\n")
    (tmp_path / ".env.example").write_text("SHARED=example\nEXAMPLE_ONLY=yes\n")
    (tmp_path / ".env").write_text("SHARED=env\nENV_ONLY=yes\n")
    (tmp_path / ".env.local").write_text("SHARED=local\nLOCAL_ONLY=yes\n")

    values = load_environment(tmp_path, process_environment={})
    assert values == {
        "SHARED": "local",
        "EXAMPLE_ONLY": "yes",
        "ENV_ONLY": "yes",
        "LOCAL_ONLY": "yes",
    }


def test_process_environment_has_highest_precedence(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\nversion='0'\n")
    (tmp_path / ".env.local").write_text("SHARED=local\n")
    assert load_environment(tmp_path, process_environment={"SHARED": "process"})["SHARED"] == "process"
