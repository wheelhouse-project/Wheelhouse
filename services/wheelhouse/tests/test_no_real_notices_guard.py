"""Prove that no test in this suite can deliver a real Windows notice.

On 2026-09-19 a test on branch worktree-floating-button-offscreen reached the
real ``gui.send_notice``, and a mutation gate then showed the developer a real
Windows notice 23 times. The guard in ``tests/conftest.py`` replaced only the
copy of ``send_notice`` held by ``utils.speech_notifier``, and six other
modules import that function by name, each keeping its own copy.

The widened guard stands at the last step instead of at ``send_notice``: it
replaces the ``notification`` attribute on the ``plyer`` package. These tests
check it from the outside, by calling through each kind of importer and
reading where the call landed, rather than by reading the fixture's code.

THE EIGHT IMPORTS BELOW ARE LOAD-BEARING and must stay at module scope. pytest
runs them at collection time, BEFORE any fixture, so each module binds the REAL
``send_notice``. ``test_a_module_level_importer_cannot_deliver`` then calls that
real function. Move an import inside a test body and the module would bind
whatever ``utils.notice_text`` held at that moment, which is a weaker thing to
prove.

WHAT PROTECTS THE LIST OF SENDERS. This guard needs no list, because
``utils.notice_text.send_notice`` is the one place in WheelHouse that reaches
plyer. That property is not assumed here; it is held by
``tests/test_utils/test_notice_sender_is_the_only_plyer_caller.py``, which
scans every module for a plyer import or a notify call and allows only
``utils/notice_text.py``, and by two mutations in
``tests/mutation_gate_notice_length.py``
("a-call-site-calls-plyer-directly-again" and
"a-call-site-reaches-plyer-under-an-alias") that break that scan on purpose and
require it to fail. A new module that starts sending notices is covered without
any edit here; a new module that walks around ``send_notice`` and calls plyer
itself is caught by that scan.
"""

import pytest
from unittest.mock import Mock

import plyer
import plyer.utils

import gui
import ui.ui_action_handler  # noqa: F401
import utils.code_telemetry  # noqa: F401
import utils.error_notifier
import utils.monitors
import utils.notice_text
import utils.notifier_worker  # noqa: F401
import utils.speech_notifier

# The modules that run ``from utils.notice_text import send_notice`` at import
# time, so each holds its own copy of the real function. Produced from
# services/wheelhouse by::
#
#     grep -rnE "from (utils\\.notice_text|\\.notice_text) import" \\
#         --include=*.py . | grep -v "/\\.venv/" | grep -v "^\\./tests/"
#
# on 2026-09-19: gui.py:70, utils/error_notifier.py:23, utils/monitors.py:53
# and utils/speech_notifier.py:11. The same grep also found three modules that
# import inside a function body -- ui/ui_action_handler.py:5918 (aliased to
# send_measured_notice), utils/code_telemetry.py:85 and
# utils/notifier_worker.py:162 -- which hold no module attribute at all; the
# alias test below stands for those. This list is test data, not guard input:
# the guard reads no list, so a module missing from here is still covered.
MODULE_LEVEL_IMPORTERS = {
    "gui": gui,
    "utils.error_notifier": utils.error_notifier,
    "utils.monitors": utils.monitors,
    "utils.speech_notifier": utils.speech_notifier,
}


def test_the_plyer_backend_is_a_stand_in_during_a_test_body():
    """Nothing in a test body may reach plyer's real notify."""
    assert isinstance(plyer.notification.notify, Mock), (
        "plyer.notification.notify is not a stand-in during a test body, so "
        "any sender in this suite can put a real Windows notice on the "
        "developer's screen. The autouse guard in tests/conftest.py is what "
        "replaces it."
    )


def test_plyers_lazy_proxy_is_never_resolved():
    """The guard must replace the package attribute, not reach through it.

    ``plyer.notification`` is a ``plyer.utils.Proxy``. Reading ANY attribute of
    it imports and builds the real Windows backend, which starts plyer's own
    notification thread. A guard written as
    ``monkeypatch.setattr(plyer.notification, "notify", ...)`` reads that
    attribute and so resolves the proxy for every test in the suite; a guard
    written as ``monkeypatch.setattr(plyer, "notification", ...)`` replaces the
    proxy without touching it. This test is what tells the two apart.
    """
    assert not isinstance(plyer.notification, plyer.utils.Proxy), (
        "plyer.notification is still the lazy proxy, so the guard reached "
        "through it instead of replacing it, and the real Windows backend has "
        "been built. Patch the attribute on the plyer package itself."
    )


def test_the_sender_itself_cannot_deliver():
    """A direct call to ``utils.notice_text.send_notice`` lands in the stub."""
    utils.notice_text.send_notice("guard title", "guard message")

    plyer.notification.notify.assert_called_once_with(
        title="guard title", message="guard message"
    )


@pytest.mark.parametrize("module_name", sorted(MODULE_LEVEL_IMPORTERS))
def test_a_module_level_importer_cannot_deliver(module_name):
    """Each module-level importer's own copy of the real function is safe.

    The module bound the real ``send_notice`` at collection time, before any
    fixture ran, and nothing replaces that copy. Calling it here is the
    strongest available proof that the guard covers it anyway, because the
    call runs the same real function the module would run in a test body.
    """
    module = MODULE_LEVEL_IMPORTERS[module_name]

    module.send_notice(f"from {module_name}", "guard message")

    plyer.notification.notify.assert_called_once_with(
        title=f"from {module_name}", message="guard message"
    )


def test_the_function_local_alias_cannot_deliver():
    """Repeat the exact import that ``ui/ui_action_handler.py:5918`` runs.

    That line reads ``from utils.notice_text import send_notice as
    send_measured_notice`` inside a method, so ``send_measured_notice`` is a
    local name and never a module attribute. Nothing can replace it by name.
    It is covered because the function it names ends at plyer.
    """
    from utils.notice_text import send_notice as send_measured_notice

    send_measured_notice("from the alias", "guard message")

    plyer.notification.notify.assert_called_once_with(
        title="from the alias", message="guard message"
    )


def test_a_module_imported_during_a_test_body_cannot_deliver():
    """A module first imported inside a test body is covered with no edit.

    The guard keeps no list of senders, so a module that appears after it runs
    needs nothing added anywhere. This test stands for that module.
    """
    import textwrap
    import types

    late_module = types.ModuleType("late_importer_under_test")
    exec(  # noqa: S102 - a one-line module written here, not external input
        textwrap.dedent(
            """
            from utils.notice_text import send_notice
            """
        ),
        late_module.__dict__,
    )

    late_module.send_notice("from a late import", "guard message")

    plyer.notification.notify.assert_called_once_with(
        title="from a late import", message="guard message"
    )
