"""--list-devices runs without --ws-port; a real start still demands it.

wh-capture-winrt-required.1.2. The branch gave --list-devices its own
function and its own refusal, and wrote in that function's docstring that
"a person who typed the flag is reading the console". On this provider the
person could not reach the console message: --ws-port was required for
every invocation, so `python main.py --list-devices` exited 2 with "the
following arguments are required: --ws-port" before any device was listed
and before any refusal was printed. google_stt_server has never required
it (config_loader.py, default None), so the three providers disagreed.

Boss e7 refused the obvious repair -- making --ws-port default to None --
because a real launch that forgot the flag would then carry None into
WSForwarder and fail later and less clearly. The rule is therefore
conditional: required when --list-devices is absent, ignored when it is
present. That rule needs a seam the tests can reach, which is why the
parser and the argument reading moved out of `if __name__ == "__main__":`
and into build_parser() and parse_provider_args().
"""
from __future__ import annotations

import pytest

import main as distil_main


def test_list_devices_needs_no_ws_port():
    """The console invocation the help text invites."""
    args = distil_main.parse_provider_args(["--list-devices"])

    assert args.list_devices is True
    assert args.ws_port is None


def test_a_real_start_without_ws_port_is_refused():
    """A launch that forgot the flag still fails at once, and says which
    argument is missing -- argparse's own exit code 2."""
    with pytest.raises(SystemExit) as excinfo:
        distil_main.parse_provider_args([])

    assert excinfo.value.code == 2


def test_a_real_start_keeps_the_port_it_was_given():
    args = distil_main.parse_provider_args(["--ws-port", "5001"])

    assert args.ws_port == 5001
    assert args.list_devices is False
