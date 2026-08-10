from toe_dac.cli import build_parser
from toe_dac import TDService
import pytest
import re
from toe_dac.context import utc_now


def test_continue_can_select_persistent_session():
    args = build_parser().parse_args([
        "continue", "--session", "ss_12345678", "--thread", "ut_12345678",
    ])
    assert args.command == "continue"
    assert args.session == "ss_12345678"
    assert args.thread == "ut_12345678"


def test_new_session_requires_distinct_command():
    args = build_parser().parse_args(["session", "new", "--thread", "ut_12345678"])
    assert args.command == "session"
    assert args.session_command == "new"
    assert args.thread == "ut_12345678"


def test_data_override_is_hidden_from_normal_help():
    help_text = build_parser().format_help()
    assert "--data" not in help_text


def test_experience_commands_have_explicit_read_and_rebuild_modes():
    show = build_parser().parse_args(["experience", "show", "exp_12345678"])
    listing = build_parser().parse_args([
        "experience", "list", "--visibility", "system", "--limit", "5",
    ])
    rebuild = build_parser().parse_args(["experience", "rebuild"])

    assert (show.experience_command, show.experience_id) == ("show", "exp_12345678")
    assert (listing.visibility, listing.limit) == ("system", 5)
    assert rebuild.experience_command == "rebuild"


def test_terminal_td_attach_error_reports_td_state(repository):
    service = TDService.create(repository, "ut_terminal_attach")
    service.cancel()

    with pytest.raises(ValueError, match="TD has ended with state cancelled"):
        repository.attach_session(service.context)


def test_new_storage_uses_timestamped_session_id_and_v2_root(repository):
    service = TDService.create(repository, "ut_storage_v2")

    assert service.context["session_id"].startswith("sess-")
    assert re.fullmatch(r"sess-[0-9a-f]{8}-\d{8}_\d{6}", service.context["session_id"])
    assert not service.context["session_id"].endswith("Z")
    assert utc_now().endswith("+08:00")
    assert (repository.root / "user-threads" / "ut_storage_v2" / "meta.json").exists()
    assert (repository.root / "user-threads" / "ut_storage_v2" / "state.json").exists()
    assert (repository.thread_dir("ut_storage_v2") / "logs" / "event.jsonl").exists()
