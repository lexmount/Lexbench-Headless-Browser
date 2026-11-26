"""Only fixed layout modes are exposed; mixed-layout legacy evidence is rejected."""
import pytest
from runner import layout, run


@pytest.mark.parametrize('mode', ['off', 'on', 'realtime'])
def test_cli_accepts_fixed_modes(mode):
    args = run.build_parser().parse_args(['run', '--moli-layout', mode, '--k', '3'])
    assert args.moli_layout == mode
    assert args.k == 3
    assert not hasattr(args, 'try_layout')
    assert layout.policy(mode) == {'policy_id': 'fixed_layout_v1', 'layout': mode}


def test_cli_defaults_realtime_and_rejects_removed_switch():
    parser = run.build_parser()
    assert parser.parse_args(['run']).moli_layout == 'realtime'
    for args in [['run', '--try-layout'], ['run', '--moli-layout', 'auto']]:
        with pytest.raises(SystemExit):
            parser.parse_args(args)


@pytest.mark.parametrize("help_text, debugger_args", [
    ("      --debugger\n          Enable debugger pauses.", ("--debugger",)),
    ("      --layout\n          Enable on-demand layout.", ()),
])
def test_moli_launch_enables_debugger_when_supported(monkeypatch, help_text, debugger_args):
    def check_launch_args(_args):
        assert run.engine_serve_args("moli", "all_resources") == (
            "--realtime", *debugger_args, "--resource",
        )
        return 0

    monkeypatch.setattr(run, "moli_serve_help", lambda: help_text)
    monkeypatch.setattr(run, "command_run", check_launch_args)
    assert run.main(["run"]) == 0
    assert run.engine_serve_args("moli") == ("--realtime",)


def test_moli_browser_command_line_opt_in_is_scoped_to_automation_profile(monkeypatch):
    def check_launch_args(_args):
        assert run.engine_serve_args("moli", "browser_automation") == (
            "--realtime", "--enable-automation",
        )
        assert run.engine_serve_args("moli") == ("--realtime",)
        return 0

    monkeypatch.setattr(run, "moli_serve_help", lambda: "      --enable-automation\n")
    monkeypatch.setattr(run, "command_run", check_launch_args)
    assert run.main(["run", "--engines", "moli"]) == 0
    assert run.engine_serve_args("moli", "browser_automation") == ("--realtime",)


@pytest.mark.parametrize('manifest', [
    {'layout_retry': {'recovered_cases': ['one']}},
    {'moli_layout_policy': {'try_layout': True}},
    {'moli_layout_policy': {'retry_layout': 'on'}},
])
def test_mixed_layout_evidence_cannot_be_reported_as_fixed(manifest):
    with pytest.raises(ValueError, match='must not contain layout retries'):
        layout.require_fixed(manifest)


@pytest.mark.parametrize('mode', ['off', 'on', 'realtime'])
def test_fixed_evidence_is_accepted(mode):
    layout.require_fixed({'moli_layout_policy': layout.policy(mode)})
