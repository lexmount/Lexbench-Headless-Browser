"""Only fixed layout modes are exposed; mixed-layout legacy evidence is rejected."""
import pytest
from runner import layout, run


@pytest.mark.parametrize('mode', ['off', 'on'])
def test_cli_accepts_fixed_modes(mode):
    args = run.build_parser().parse_args(['run', '--moli-layout', mode, '--k', '3'])
    assert args.moli_layout == mode
    assert args.k == 3
    assert not hasattr(args, 'try_layout')
    assert layout.policy(mode) == {'policy_id': 'fixed_layout_v1', 'layout': mode}


def test_cli_defaults_off_and_rejects_removed_switch():
    parser = run.build_parser()
    assert parser.parse_args(['run']).moli_layout == 'off'
    for args in [['run', '--try-layout'], ['run', '--moli-layout', 'auto']]:
        with pytest.raises(SystemExit):
            parser.parse_args(args)


@pytest.mark.parametrize('manifest', [
    {'layout_retry': {'recovered_cases': ['one']}},
    {'moli_layout_policy': {'try_layout': True}},
    {'moli_layout_policy': {'retry_layout': 'on'}},
])
def test_mixed_layout_evidence_cannot_be_reported_as_fixed(manifest):
    with pytest.raises(ValueError, match='must not contain layout retries'):
        layout.require_fixed(manifest)


@pytest.mark.parametrize('mode', ['off', 'on'])
def test_fixed_evidence_is_accepted(mode):
    layout.require_fixed({'moli_layout_policy': layout.policy(mode)})
