"""Unit tests for the SendInput-backed mouse wheel primitive
(wh-voice-access-parity.2.3, part one -- discrete scrolling only).

``utils.win_input_sender`` gains :func:`scroll_wheel`, which sends one or more
wheel notches at the pointer's current position. It is the primitive behind the
spoken "scroll up / down / left / right" commands. Before this bead Wheelhouse
had no way to scroll by voice at all, which stops a user from reading a long
page.

Shape notes these tests pin down:

* A wheel event carries its distance in ``mouseData``, not in dx/dy. dx and dy
  are ignored by Windows for a wheel event, so they stay 0 and neither
  ``MOUSEEVENTF_MOVE`` nor ``MOUSEEVENTF_ABSOLUTE`` is set. The pointer does not
  move; the wheel goes where the pointer already is.
* ``mouseData`` is a ``DWORD``, which is UNSIGNED, and a scroll down or left
  carries a NEGATIVE distance. Measured on this Python: ctypes accepts a plain
  -120 in that field and wraps it, reading back as 4294967176. So the tests
  compare the field against ``(-120) & 0xFFFFFFFF`` because that is what any
  correct implementation reads back, NOT because an unmasked assignment would
  fail. What they do catch is a wrong SIGN: a scroll down that sends +120.
* No ``BlockInput``. The click and drag primitives suppress physical input so a
  hand resting on the mouse cannot override an injected absolute MOVE. A wheel
  event has no coordinate to be overridden, so suppressing the user's own input
  would cost more than it protects.

The tests fake ``user32`` so they run headless with no real input synthesis.
"""

from __future__ import annotations

import ast
import builtins
import logging
from pathlib import Path
from typing import Any, Optional

import pytest

from utils import win_input_sender as wis


def unsigned(value: int) -> int:
    """The 32-bit unsigned form of ``value``, as a DWORD field reads back."""
    return value & 0xFFFFFFFF


class FakeUser32:
    """Records SendInput batches, including ``mouseData``.

    ``send_returns`` scripts the accepted-event count of each batch in order;
    once exhausted every event of a batch is accepted. ``raise_on_batch`` is the
    0-based index of the batch that raises at the platform boundary.
    """

    def __init__(
        self,
        *,
        send_returns: Optional[list[Optional[int]]] = None,
        raise_on_batch: Optional[int] = None,
    ) -> None:
        self._send_returns = send_returns or []
        self._raise_on_batch = raise_on_batch
        self.sendinput_batches: list[list[dict[str, int]]] = []
        self.blockinput_calls: list[int] = []

    def BlockInput(self, flag: int) -> int:
        self.blockinput_calls.append(int(flag))
        return 1

    def SendInput(self, num: int, lp_array: Any, _size: int) -> int:
        index = len(self.sendinput_batches)
        array = lp_array._obj  # type: ignore[attr-defined]
        batch: list[dict[str, int]] = []
        for i in range(num):
            ev = array[i]
            batch.append(
                {
                    "type": int(ev.type),
                    "dx": int(ev.ii.mi.dx),
                    "dy": int(ev.ii.mi.dy),
                    "mouseData": int(ev.ii.mi.mouseData),
                    "flags": int(ev.ii.mi.dwFlags),
                }
            )
        self.sendinput_batches.append(batch)

        if self._raise_on_batch is not None and index == self._raise_on_batch:
            raise OSError("SendInput failed at the platform boundary")

        if index < len(self._send_returns):
            scripted = self._send_returns[index]
            if scripted is not None:
                return scripted
        return num

    def events(self) -> list[dict[str, int]]:
        """Every captured event, flattened across batches."""
        return [event for batch in self.sendinput_batches for event in batch]


@pytest.fixture
def patch_win32(monkeypatch):
    """Install a FakeUser32 onto the module; return a setter."""

    def _install(fake: FakeUser32) -> FakeUser32:
        monkeypatch.setattr(wis, "user32", fake)
        monkeypatch.setattr(
            wis,
            "kernel32",
            type("K", (), {"GetLastError": staticmethod(lambda: 0)})(),
        )
        return fake

    return _install


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_wheel_flags_and_delta_have_the_documented_win32_values():
    """The flag and notch constants must match the Win32 header values."""
    assert wis.MOUSEEVENTF_WHEEL == 0x0800
    assert wis.MOUSEEVENTF_HWHEEL == 0x01000
    assert wis.WHEEL_DELTA == 120


# ---------------------------------------------------------------------------
# direction and distance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction,expected_flag,expected_data",
    [
        ("up", 0x0800, 120),
        ("down", 0x0800, unsigned(-120)),
        ("right", 0x01000, 120),
        ("left", 0x01000, unsigned(-120)),
    ],
)
def test_scroll_wheel_sends_one_notch_in_each_direction(
    patch_win32, direction, expected_flag, expected_data
):
    """One notch per direction, with the right flag and signed distance."""
    fake = patch_win32(FakeUser32())

    succeeded, reason = wis.scroll_wheel(direction)

    assert (succeeded, reason) == (True, None)
    events = fake.events()
    assert len(events) == 1
    assert events[0]["type"] == wis.INPUT_MOUSE
    assert events[0]["flags"] == expected_flag
    assert events[0]["mouseData"] == expected_data


def test_scroll_wheel_repeats_the_notch_for_a_click_count(patch_win32):
    """A count of 3 sends three identical wheel events."""
    fake = patch_win32(FakeUser32())

    succeeded, reason = wis.scroll_wheel("down", 3)

    assert (succeeded, reason) == (True, None)
    events = fake.events()
    assert len(events) == 3
    for event in events:
        assert event["flags"] == wis.MOUSEEVENTF_WHEEL
        assert event["mouseData"] == unsigned(-wis.WHEEL_DELTA)


def test_scroll_wheel_never_moves_the_pointer(patch_win32):
    """No MOVE flag, no ABSOLUTE flag, and dx/dy stay 0."""
    fake = patch_win32(FakeUser32())

    wis.scroll_wheel("up", 2)

    for event in fake.events():
        assert event["dx"] == 0
        assert event["dy"] == 0
        assert not event["flags"] & wis.MOUSEEVENTF_MOVE
        assert not event["flags"] & wis.MOUSEEVENTF_ABSOLUTE


def test_scroll_wheel_does_not_suppress_physical_input(patch_win32):
    """A wheel event has no coordinate to defend, so BlockInput is not used."""
    fake = patch_win32(FakeUser32())

    wis.scroll_wheel("down")

    assert fake.blockinput_calls == []


def test_scroll_wheel_sends_every_notch_in_one_batch(patch_win32):
    """All notches travel in a single SendInput call, like the click batch."""
    fake = patch_win32(FakeUser32())

    wis.scroll_wheel("up", 4)

    assert len(fake.sendinput_batches) == 1
    assert len(fake.sendinput_batches[0]) == 4


# ---------------------------------------------------------------------------
# argument validation -- fails closed, sends nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["", "sideways", "UP ", None, 3, "middle"])
def test_scroll_wheel_refuses_an_unknown_direction(patch_win32, direction):
    fake = patch_win32(FakeUser32())

    assert wis.scroll_wheel(direction) == (False, "invalid_direction")
    assert fake.sendinput_batches == []


@pytest.mark.parametrize("clicks", [0, -1, "3", 1.0, None])
def test_scroll_wheel_refuses_a_non_positive_or_non_integer_count(
    patch_win32, clicks
):
    fake = patch_win32(FakeUser32())

    assert wis.scroll_wheel("down", clicks) == (False, "invalid_clicks")
    assert fake.sendinput_batches == []


def test_scroll_wheel_refuses_a_bool_count(patch_win32):
    """``bool`` is an int subclass; True must not pass as a count of 1."""
    fake = patch_win32(FakeUser32())

    assert wis.scroll_wheel("down", True) == (False, "invalid_clicks")
    assert fake.sendinput_batches == []


def test_scroll_wheel_refuses_a_count_above_the_cap(patch_win32):
    """One malformed message must not flood the input queue."""
    fake = patch_win32(FakeUser32())

    over = wis.MAX_SCROLL_CLICKS + 1
    assert wis.scroll_wheel("down", over) == (False, "invalid_clicks")
    assert fake.sendinput_batches == []


def test_scroll_wheel_accepts_the_count_at_the_cap(patch_win32):
    fake = patch_win32(FakeUser32())

    succeeded, reason = wis.scroll_wheel("down", wis.MAX_SCROLL_CLICKS)

    assert (succeeded, reason) == (True, None)
    assert len(fake.events()) == wis.MAX_SCROLL_CLICKS


# ---------------------------------------------------------------------------
# platform failures -- reported, never raised
# ---------------------------------------------------------------------------


def test_scroll_wheel_reports_a_short_send(patch_win32):
    """Windows accepted fewer events than were offered."""
    patch_win32(FakeUser32(send_returns=[1]))

    assert wis.scroll_wheel("down", 3) == (False, "sendinput_short")


def test_scroll_wheel_reports_a_raising_send(patch_win32):
    """A raise at the ctypes boundary becomes a reason, not an exception."""
    patch_win32(FakeUser32(raise_on_batch=0))

    assert wis.scroll_wheel("up") == (False, "sendinput_error")


# ---------------------------------------------------------------------------
# wh-wheel-refusal-notice -- which refusals show the user a box
# ---------------------------------------------------------------------------


def _levels_for(caplog, needle: str) -> set[str]:
    """The levels of every win_input_sender record whose text holds needle."""
    return {
        record.levelname
        for record in caplog.records
        if record.name == "utils.win_input_sender"
        and needle in record.getMessage()
    }


class TestTheWheelRefusalDoesNotPopItsOwnBox:
    """wh-wheel-refusal-notice.

    ``ErrorNotificationHandler`` is attached to the ROOT logger and shows a
    generic ``[ERROR] utils.win_input_sender`` box for EVERY ERROR record,
    with no opt-out. Both wheel callers write their own notice now -- the
    continuous scroll always did, and the discrete spoken scroll gained one in
    step 1 -- so an ERROR record here is the duplicate box the user complained
    about.

    A suppression attribute on the record cannot be used instead: both wheel
    callers reach this one call, so nothing set here can tell them apart.

    NO WHEEL REFUSAL LOGS AN ERROR ANY MORE
    (wh-wheel-refusal-notice.1.7). An earlier version of this
    docstring said the two argument refusals kept their ERROR on
    purpose, and that the generic box stayed their one and only
    report. Both statements stopped being true when .1.7 demoted
    every wheel refusal to WARNING and emptied
    ``WHEEL_REFUSALS_THAT_LOG_ERROR``. The tests below assert
    WARNING, and each caller's own written notice is the report
    for every refusal that caller reports at all.

    That last clause is exact on purpose, and the product owner has
    now ruled on what it leaves out (wh-wheel-refusal-notice.1.18,
    QUESTIONS-2026-08-30.md item 23, answered "number one"). The
    continuous scroll writes its notice only once consecutive
    refusals reach ``REFUSALS_BEFORE_STOPPING``; a refused tick
    followed by a successful one resets the count and reports
    nothing. That silence is DELIBERATE, not a gap: the scroll
    corrected itself, and a box for every hiccup would interrupt
    voice work for nothing. So the guarantee this bead makes is the
    narrower one -- exactly one notice for a refusal a caller
    reports, and no notice for a tick that recovers within two
    tries.
    """

    def test_a_short_send_logs_a_warning_and_no_error(self, patch_win32, caplog):
        patch_win32(FakeUser32(send_returns=[1]))

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            assert wis.scroll_wheel("down", 3) == (False, "sendinput_short")

        assert _levels_for(caplog, "short SendInput") == {"WARNING"}

    def test_a_raising_send_logs_a_warning_and_no_error(
        self, patch_win32, caplog
    ):
        patch_win32(FakeUser32(raise_on_batch=0))

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            assert wis.scroll_wheel("up") == (False, "sendinput_error")

        assert _levels_for(caplog, "unexpected error") == {"WARNING"}

    def test_the_refused_wheel_call_still_writes_a_line(
        self, patch_win32, caplog
    ):
        """Demoting the level must not delete the diagnosis.

        The line still has to carry the accepted count and the Win32 error, or
        an operator reading the log cannot tell a blocked foreground window
        from a full input queue.
        """
        patch_win32(FakeUser32(send_returns=[1]))

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            wis.scroll_wheel("down", 3)

        lines = [
            record.getMessage() for record in caplog.records
            if record.name == "utils.win_input_sender"
        ]
        assert any("sent 1/3" in line and "Win32 error" in line
                   for line in lines)

    def test_an_unknown_direction_logs_a_warning_not_an_error(
        self, patch_win32, caplog
    ):
        """WARNING since wh-wheel-refusal-notice.1.7; see the class docstring."""
        patch_win32(FakeUser32())

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            assert wis.scroll_wheel("sideways") == (
                False, "invalid_direction",
            )

        assert _levels_for(caplog, "unsupported direction") == {"WARNING"}

    def test_an_unsupported_notch_count_logs_a_warning_not_an_error(
        self, patch_win32, caplog
    ):
        """WARNING since wh-wheel-refusal-notice.1.7; see the class docstring."""
        patch_win32(FakeUser32())

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            assert wis.scroll_wheel("down", 0) == (False, "invalid_clicks")

        assert _levels_for(caplog, "unsupported notch count") == {"WARNING"}


class TestTheOtherInputPrimitivesKeepTheirError:
    """The blast radius of the demotion, pinned so it cannot spread.

    ``utils/win_input_sender.py`` is shared by click, drag and typing. The
    caller survey found that ``press_keys`` and ``type_string`` both return
    ``None``, so their callers cannot see a failure at all -- the ERROR record
    here is the ONLY user-visible signal those two paths have. Demoting them
    with the wheel would make a dropped hotkey and a partly typed phrase
    completely silent.
    """

    def test_press_keys_still_logs_an_error_on_a_short_send(
        self, patch_win32, caplog
    ):
        patch_win32(FakeUser32(send_returns=[1]))

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            wis.press_keys("ctrl", "c")

        assert _levels_for(caplog, "SendInput failed") == {"ERROR"}

    def test_type_string_still_logs_an_error_on_a_short_send(
        self, patch_win32, caplog
    ):
        patch_win32(FakeUser32(send_returns=[1]))

        with caplog.at_level(logging.DEBUG, logger="utils.win_input_sender"):
            wis.type_string("hello", chunk_delay=0)

        assert _levels_for(caplog, "SendInput failed for a chunk") == {"ERROR"}


class TestTheRefusalLevelContractIsMachineReadable:
    """wh-wheel-refusal-notice.1.1 -- the reviewer's finding.

    ``ErrorNotificationHandler`` is attached to the ROOT logger and turns
    an ERROR record into a generic notice box with no opt-out. A caller
    that writes its own notice for a reason that also logs an ERROR gives
    the user two boxes -- the defect this bead exists to remove.

    It does NOT follow that a listed ERROR is a report the user received
    (wh-wheel-refusal-notice.1.7). ``emit`` drops a repeat of the same
    (logger, level, message) inside ``rate_limit_seconds``, which
    ``setup_logging`` sets to 10, so a caller silenced by the list can
    leave a repeated refusal with NO notice at all. That is why the list
    is empty and stays empty, and why this test now measures an empty set
    on both sides rather than a matching pair.

    Before this test the contract lived in two modules: the ERROR records
    here, and a hand-copied exclusion list in ``ui/ui_action_handler.py``. A
    refusal reason added later could log at ERROR and never reach that list,
    and nothing would notice. ``WHEEL_REFUSALS_THAT_LOG_ERROR`` publishes the
    list beside the records that choose the level, and this test walks every
    reason ``scroll_wheel`` can return and checks the two halves against each
    other.
    """

    def test_every_error_logged_refusal_is_listed_and_no_other_is(
        self, patch_win32, caplog
    ):
        provocations = (
            (FakeUser32(), ("sideways", 1)),
            (FakeUser32(), ("down", 0)),
            (FakeUser32(send_returns=[1]), ("down", 3)),
            (FakeUser32(raise_on_batch=0), ("up", 1)),
        )

        logged_an_error = {}
        for fake, call in provocations:
            patch_win32(fake)
            caplog.clear()
            with caplog.at_level(
                logging.DEBUG, logger="utils.win_input_sender",
            ):
                succeeded, reason = wis.scroll_wheel(*call)
            assert not succeeded
            logged_an_error[reason] = any(
                record.levelname == "ERROR"
                and record.name == "utils.win_input_sender"
                for record in caplog.records
            )

        assert set(logged_an_error) == {
            "invalid_direction",
            "invalid_clicks",
            "sendinput_short",
            "sendinput_error",
        }
        assert set(wis.WHEEL_REFUSALS_THAT_LOG_ERROR) == {
            reason
            for reason, logged in logged_an_error.items()
            if logged
        }


# The service root, so the walk below can reach a second file without
# importing it. ui/ui_action_handler.py pulls in the GUI import graph, and
# this test only needs its text.
_SERVICE_ROOT = Path(wis.__file__).parent.parent

# Returned by ``_reason_returned`` for a pass-through return that hands the
# whole answer to another function. Distinct from ``None``, which means the
# success return.
_DELEGATES = object()

# Every function that produces a wheel refusal reason the callers act on, and
# the ``(module, name)`` import each may hand its whole answer to.
# wh-wheel-refusal-notice.1.3: the primitive was the only one the walk
# covered, and the wrapper -- a second producer, of "sendinput_error" --
# logged that refusal at ERROR unchecked for two review rounds. ``None``
# means the function may not delegate at all.
_WHEEL_REFUSAL_PRODUCERS = (
    (Path("utils") / "win_input_sender.py", "scroll_wheel", None),
    (
        Path("ui") / "ui_action_handler.py",
        "_win32_scroll_wheel",
        ("utils.win_input_sender", "scroll_wheel"),
    ),
)


class TestEveryRefusalBranchDeclaresItsLevelInTheSource:
    """The published list is checked against the SOURCE, not fixed calls.

    ``test_every_error_logged_refusal_is_listed_and_no_other_is`` above makes
    four calls, one for each refusal ``scroll_wheel`` has today. It cannot
    reach a branch nobody has written yet, so it stays green when a FIFTH
    refusal is added that logs an ERROR and is left out of
    ``WHEEL_REFUSALS_THAT_LOG_ERROR`` -- which is the duplicate-box bug
    returning. That gap was measured, not assumed: adding such a branch by
    hand left all 119 tests in the four wheel test files passing
    (wh-wheel-refusal-notice.1.2).

    This test reads the source instead of calling it. It walks every
    ``return`` in each producer, reads the log level of the record in the
    same block, and rebuilds the published list from what it finds. A branch
    added later is covered whether or not any test calls it.

    ONE RULE, TWO FUNCTIONS (wh-wheel-refusal-notice.1.3). A refusal branch
    logs an ERROR if and only if the reason it returns is in
    ``WHEEL_REFUSALS_THAT_LOG_ERROR``. That is the rule the callers actually
    depend on, so both producers are held to it.

    PER PRODUCER, NOT AS A UNION (wh-wheel-refusal-notice.1.6). Comparing
    only the union of both producers' ERROR reasons against the published
    tuple was not the rule this docstring claimed. Both producers can return
    ``sendinput_error``. A wrapper branch that logs it at ERROR, with the
    reason added to the tuple, keeps the union equal to the tuple while the
    primitive's own ``sendinput_error`` still logs a WARNING -- and the
    discrete caller then suppresses its written notice for that WARNING
    outcome, because the tuple says the reason already reported itself. ZERO
    notices, which is the worse half of this bead's invariant. So producers
    that share a reason must agree on whether it logs an ERROR -- the only
    thing the tuple can say, and the only disagreement a caller can feel --
    and disagreement fails here
    before the published list is derived at all.

    NO WHEEL REFUSAL LOGS AN ERROR (wh-wheel-refusal-notice.1.7, .1.12).
    The published tuple is empty, and this test holds it empty rather than
    merely equal to the reasons it finds logging one. Equality could be
    satisfied by adding a new ERROR branch AND its reason to the tuple in
    the same edit, which is the arrangement .1.7 measured as showing the
    user NOTHING: the caller reads the reason out of the tuple and
    suppresses its written notice, and ErrorNotificationHandler drops the
    generic box for a repeat inside its ten-second window. What counts as
    an ERROR record is read from the method name alone -- error,
    exception, critical, fatal and log -- at any receiver, in any
    statement that always runs, because the handler is installed at
    logging.ERROR and sees every one of them.

    NO LOUD METHOD IS REACHABLE AT ALL (wh-wheel-refusal-notice.1.15,
    .1.16). Attributing a loud record to the refusal it belongs to means
    being right about reachability, and the walk below is not. It reads
    a logging call only where the call's own callee is an attribute, and
    it reads records only from the STATEMENTS ABOVE the return in the
    return's own block. So ``getattr(logger, "error")(...)``, a call
    through an alias of the bound method, a record written in the ``if``
    header that guards the return, and a record in a ``finally`` that
    runs on the way out all read as no record at all, while every one of
    them puts an ERROR record in front of ErrorNotificationHandler.

    Since the rule above forbids all of them anyway, this test stopped
    trying to attribute them and refuses them outright: inside either
    producer, a reference to error, exception, critical, fatal or log, a
    dynamic lookup (``getattr``, ``eval``, ``exec``), the bare name of
    one of those methods written as a string, or a call to a
    module-level name bound to one of them, each fails on its own. That
    check needs no reachability argument, which is what makes it the
    reliable half. The level machinery below is what remains, and while
    the published tuple is empty nothing can reach its ERROR side --
    every mutation that adds a loud record now fails on this check
    first.

    THE RECORD CAN ALSO SIT IN SOMETHING THE PRODUCER CALLS
    (wh-wheel-refusal-notice.1.17). A module-level
    ``def _helper(): logger.error(...)`` puts nothing loud inside the
    producer at all: the branch calls a bare name, and the alias scan
    above reads assignments, not definitions. So the scan follows the
    call. Every call in a producer must resolve to one of three
    things -- a Python builtin, a definition inside the producer
    itself, or a ``def`` or ``class`` at module level in the same
    file, whose body is then scanned the same way, recursively.
    Anything else FAILS: an imported helper, a callable parameter,
    a name pulled out of a container. The walk cannot read those,
    and a callable it cannot read is where the next indirection
    will live.

    THAT SENTENCE WAS TRUE OF NAMES ONLY UNTIL ROUND 10
    (wh-wheel-refusal-notice.1.20). The resolver looked at a call
    whose callee was a bare NAME and ignored every other callee, so
    ``[_helper][0]()`` reached a module-level helper the walk never
    read, while the paragraph above already promised that shape
    failed. Two rules close the gap. A computed callee now fails,
    with ONE exception: ``(<Name> * <count>)(...)``, the ctypes
    array constructor the primitive needs to send anything at all,
    whose left name is followed and read like any other definition.
    And a definition is followed only while nothing else that runs
    at module level binds its name, because ``_quiet = _loud``
    after the ``def`` leaves the body read here describing
    something the call no longer runs.

    ROUND 11 FOUND THAT SENTENCE TOO WIDE AS WELL, which is twice
    in a row (wh-wheel-refusal-notice.1.21). The round-10 fence
    skipped every module-level ``def``, ``async def`` and
    ``class`` statement whole, so four bindings that really do run
    at import went unseen: a walrus in a default argument, a
    ``match`` case capture, a class suite that declares ``global``
    and then assigns, and a star import, which the fence recorded
    as the literal name ``*`` and therefore matched nothing.
    _module_level_rebindings now reads the declaration-time parts
    of a ``def`` or ``class`` without treating its body as module
    level, descends into ``match`` cases, and refuses every
    followed definition while a star import is present. In the
    same round the walk began reading a CLASS HEADER, because a
    metaclass could answer the multiplication the computed-callee
    exception allows.

    ROUND 12 MADE IT THREE IN A ROW
    (wh-wheel-refusal-notice.1.24). The sentence round 11 wrote --
    that a class suite declaring ``global`` and then assigning
    binds at module level -- described a walk that read only the
    suite's OUTERMOST statements, and only an assignment among
    the binding forms. A ``global`` inside an ``if``, a ``for``
    target, a ``match`` capture, a ``with ... as``, an ``import``
    alias and a nested class body each rebound a helper while the
    guard reported nothing. Two shared helpers now decide both
    scopes: _statements_in_scope says which statements run in a
    scope, and _names_bound_by_statement says what one binds.
    _globals_rebound_in and _module_level_rebindings both use
    them, so the module scope and a class suite cannot cover
    different sets.

    ROUND 13 MADE IT FOUR (wh-wheel-refusal-notice.1.25), and it
    found the gap by reading round 12's own sentence rather than
    by inventing a new attack. "What one binds" covered every
    BINDING form and no DEFINITION form: a ``def``, ``async def``
    or ``class`` statement binds its own name when it runs, and
    both scans skipped straight past that to the walruses in its
    header. _definitions_in reads module.body alone, so a ``def
    _quiet`` inside an ``if`` left the guard following the first,
    quiet body while the loud one was what ran. Both scans now
    record the name. Module level records it only for a
    definition reached through a compound suite, since a direct
    one IS what _definitions_in points at; a class suite records
    every one, because the intersection with its ``global``
    declarations already decides which of them leave the class.

    ROUND 14 MADE IT FIVE (wh-wheel-refusal-notice.1.26), and it
    is the first of the five to point somewhere else. Rounds 10
    to 13 all taught the walk what MODULE LEVEL and a CLASS SUITE
    bind. The producer's own scope was never read at all:
    _defined_inside recorded definitions and nothing else, saying
    outright that a parameter and an assignment do not count as
    local -- true of a DEFINITION, and nothing else recorded them
    either, so a same-named parameter, assignment, ``for``
    target, ``with ... as`` target or walrus inside the producer
    left the walk following a module definition of a name the
    producer had already rebound. The same method also read
    ``ast.walk``, so a definition in a NESTED scope, which cannot
    bind the producer's call, stopped the walk resolving that
    call at all. _defined_inside now reads the scope's own
    statements, _shadowing_bindings_in reads its parameters and
    every other binding it makes, and a shadowed name fails
    closed ahead of the local check.

    ROUND 15 MADE IT SIX (wh-wheel-refusal-notice.1.27), by
    asking round 14's own question of round 14's own fix. Round
    14 taught the walk to read the PRODUCER's scope. It did not
    ask how many scopes there are. _loud_record_sites computed
    one local and one shadowed set per pending scope and then
    crossed every child scope with ``ast.walk``, so a name
    written inside a nested function, an async function, a class
    suite, a lambda or any of the four comprehensions was
    resolved against the producer's bindings and then against a
    module definition -- never against the bindings that actually
    govern it. Ten shapes were seen to escape. The shipped
    scroll_wheel already holds a list comprehension whose target
    is not in any shadowed set; it is safe only because that
    target is named ``_``.

    ``ast.walk`` IS GONE FROM THE CALL-SITE SCAN. _nodes_in_scope
    yields what one scope evaluates and queues each child with
    its own chain, _nearest_bindings resolves a name in the
    nearest scope that binds it, and a class scope is dropped
    from what its children inherit, because a function written in
    a class suite cannot see what that suite binds. The parts of
    a child that run where it is WRITTEN -- decorators, defaults,
    annotations, class bases and keywords, and a comprehension's
    first iterable -- stay in the enclosing stream, so a call
    written there is still resolved there.

    THE LESSON, worth more than any of the six fixes: for six
    rounds this docstring was narrowed while the code was
    narrowed separately, and then SIX rounds running found the
    prose promising more than the code delivered -- each time in
    a sentence the previous round had written to correct exactly
    that. Read the two against each other in both directions, not
    just the code against the attack, and read the sentence the
    last fix added first. Round 13 is the clearest case: the
    words "what one binds" were the whole defect, and nothing
    outside this docstring had to be read to see it. Round 14
    adds the other half of the same discipline: ask which SCOPES
    a claim was ever tested against, not only which forms. Round
    15 is what that question found when it was asked once more --
    four rounds of scope work had covered module level and a
    class suite, round 14 added the producer, and NONE of the
    five had asked how many scopes a producer contains. When a
    fix answers "which X", the next question is always "how many
    X are there".

    ROUND 16 MADE IT SEVEN (wh-wheel-refusal-notice.1.28 and
    .1.29), and it answers the round-15 question in the two
    places round 15 did not look. Round 15 counted the scopes a
    producer contains and read each with its own BINDINGS. It
    counted eight kinds and there are nine: a PEP 695 type
    parameter list opens an annotation scope, its bounds are
    executable code on the supported 3.12 runtime, and nothing
    yielded them -- neither _declaration_expressions, which lists
    decorators, arguments, returns, bases and keywords, nor
    _evaluated_inside, which lists a body. That ninth scope is
    REFUSED rather than modelled; see _type_parameter_site for
    why saying no is the smaller and safer answer.

    AND A BINDING IS NOT THE ONLY THING THAT DECIDES A NAME. Two
    declaration forms decide one without binding it. ``global``
    makes a bare name skip every enclosing function and mean the
    module name, so a child scope reached the loud module helper
    while the producer's own quiet definition of that name sat
    beside it. ``nonlocal`` does the reverse: it lets a nested
    scope rebind THIS scope's name, which this scope's own reader
    cannot see because it stops at a ``def``. Thirteen shapes
    were seen to escape. Both are now read, in
    _bindings_and_escapes_in, so a scope answers with three sets
    rather than two.

    THE SECOND LESSON, and it is about the review rather than the
    code: .1.28 stated that a ``nonlocal`` rebinding already
    failed closed. It does when the call is written INSIDE the
    rebinding scope, which is where a reviewer naturally looks.
    It does not when the call is written in the scope that was
    rebound. A claim about a symmetric construct has two
    directions and a probe that runs only one has tested half of
    it, whoever wrote the claim.

    ROUND 17 MADE IT EIGHT (wh-wheel-refusal-notice.1.30 and
    .1.31), and neither finding is a new kind of hiding place. Both
    are the round-16 answers applied at the wrong set of ENTRY
    POINTS. _globals_rebound_in learned to read a ``global``, but it
    queued only a CLASS suite for later reading and dropped a
    function body, so the declaration round 16 taught it to honour
    was invisible in the scope where one is usually written. And the
    ninth scope round 16 REFUSED is refused only where the walk
    arrives: a MODULE-level ``type`` statement is visited by nothing
    at all, because _definitions_in returns def, async def and class,
    and ast.TypeAlias was not a binding form. Both halves of an alias
    run lazily, on the first read of ``__value__``, ``__bound__``
    or ``__constraints__``, so a refusal branch reaches them with
    an attribute reference and no call in sight; _LAZY_MEMBERS
    refuses that reference for the same reason
    _type_parameter_site refuses a bound. Round 18 added
    ``__constraints__`` (wh-wheel-refusal-notice.1.32): a type
    parameter carries a bound or a set of constraints, never both,
    so naming one of the two alternatives left the other open.

    WHY THE REBINDING SCOPE STAYED QUIET, which round 16 got wrong in
    this docstring's own words: a name in a scope's ``shadowed`` set
    produces a site only when a CALL to that name is resolved in that
    scope, and a scope that only performs the rebinding contains no
    such call. The reasoning that called this shape fail-closed was
    tested by running it: it logged an ERROR while the guard reported
    nothing.

    THE THIRD LESSON: ask a new fix where it is READ FROM, not only
    what it reads. Thirteen shapes escaped where the two findings
    named four between them, and every one of them is unreachable
    from a probe that writes the interesting shape inside the
    producer -- which is exactly where a reviewer, and round 16,
    naturally look.

    ROUND 18 MADE IT NINE (wh-wheel-refusal-notice.1.32 and
    .1.33), and the first of the two is the round-17 fix caught one
    member short. A PEP 695 type parameter carries a bound OR a set
    of constraints, never both. _LAZY_MEMBERS named ``__bound__``
    and stopped, so a branch that stored a constrained parameter in
    a module name and read ``.__constraints__`` ran the constraint
    expressions with nothing reported -- and it never had to write
    ``__type_params__``, which was the only other member that would
    have refused it.

    THE MEMBER LIST IS NOW MEASURED RATHER THAN RECALLED. Every
    dunder member of a bounded parameter, a constrained one, a
    class's parameter, a plain alias and a parameterised alias was
    read on the pinned 3.12 runtime with the logger captured.
    Exactly three reads run author-written code: ``__bound__``,
    ``__constraints__`` and ``__value__``. ``__type_params__`` runs
    none, and stays in the set because reaching a parameter through
    it is how a branch reaches the other two.

    THE FOURTH LESSON, and .1.33 is the whole of it: a paragraph
    that describes a defect outlives the defect. _module_level_
    rebindings still told a reader that a ``global`` written in a
    module-level function body was out of reach and would need
    call-graph reasoning. Round 17 closed that, and the sentence
    stayed, which is worse than never writing it -- a future
    maintainer would have believed an open bypass remained. When a
    fix closes a gap, find the prose that named the gap.

    THE EXCEPTION HAS A TEST OF ITS OWN NOW. It did not until
    round 11, and the claim here said it needed none, on the
    argument that this test passing on unmodified source already
    proves the exception still permits the real shape. That
    argument holds, but it proves nothing about the exception
    being load-bearing rather than dead permission, so the gate
    carries ``ctypes-callee-exception-removed``: deleting the
    exception must FAIL this test on unmodified shipped source.

    That is a real cost, and it is the deliberate direction. A future
    refusal branch that wants to call an imported helper has to either
    move the helper into the module or teach this walk to follow it. The
    alternative is a guard that reads an unreadable callable as silent,
    which is what rounds 6, 7 and 8 each found in turn.

    What is still out of reach, and cannot be closed by reading one
    file: a method called on an object -- ``user32.SendInput(...)``,
    ``kernel32.GetLastError()``. Refusing those would forbid the
    primitive from calling Windows at all. Only their ATTRIBUTE NAME is
    checked, so ``anything.error(...)`` fails while
    ``anything.helper(...)`` does not.
    ``test_every_error_logged_refusal_is_listed_and_no_other_is`` above
    measures the four refusals that exist at run time.

    A DECORATOR CAN REPLACE THE CALLABLE ENTIRELY
    (wh-wheel-refusal-notice.1.19). ``@_watch`` above a ``def``
    is an ``ast.Name`` in ``decorator_list`` -- not a call, not an
    attribute -- so no branch of the walk reacted to it, and a
    decorator that answers a refusal with an ERROR record in place
    of the function it wraps read as nothing at all. The same held
    for a decorated nested ``def``: its name was collected as
    defined inside the producer, so a later call to it was skipped
    as already covered -- true of the body written there, false of
    what ran. Both are refused now. A decorator on any scope this
    walk reads fails the test, whatever that decorator does,
    because one file cannot tell.

    WHAT THIS TEST CLAIMS, EXACTLY (wh-wheel-refusal-notice.1.19).
    It is a static check on the SYNTAX of two functions and of what
    they call in the same file. It is NOT a proof about what those
    functions log at run time, and no reader should take it for
    one. The narrower thing it does is still worth having: every
    shape it cannot resolve -- an imported callable, a dynamic
    lookup, a decorator, a name pulled out of a container -- fails
    rather than passing quietly, so a record can only reach
    ErrorNotificationHandler through a shape this file has never
    seen. Rounds 6, 7, 8 and 9 each found one more such shape,
    which is why the claim is written this way instead of
    promising the class is closed. Proving the run-time behaviour
    would need a different instrument -- capturing records from a
    live call -- and that is a larger change than this bead.

    The walk is deliberately strict about shape, in three ways.

    A ``return`` that is neither ``return (True, None)`` nor
    ``return (False, "<literal reason>")`` fails this test rather than being
    skipped, because a reason this walk cannot read is a reason it cannot
    check. The ONE exception is narrow and named per producer in
    ``_WHEEL_REFUSAL_PRODUCERS``: a wrapper whose success path is
    ``return <the one named import>(...)`` hands its whole answer to the
    function it names, so that return produces no reason of its own. The
    delegated name must be bound by ``from <module> import <name>`` INSIDE
    the function and never rebound there, so a shadowed or reassigned helper
    cannot pose as the primitive (wh-wheel-refusal-notice.1.6). "Never
    rebound" includes from a NESTED scope (wh-wheel-refusal-notice.1.13):
    a ``nonlocal`` inside a nested def reaches the wrapper's own binding,
    and a walrus in that def's decorator, default or annotation is
    evaluated in the wrapper's scope. The primitive names nothing and may
    not delegate.

    A statement that can write a record and then FALL THROUGH to a refusal
    return below it fails this test too (wh-wheel-refusal-notice.1.6).
    ``if bad: logger.error(...)`` followed by
    ``return (False, "bad_thing")`` in the enclosing block logs an ERROR for
    an unlisted reason at run time, while a walk that reads only bare
    ``logger`` calls in the same block records no level at all. A guard whose
    every block ends in a ``return`` cannot reach the later return, so the
    ordinary ``if ...: logger.warning(...); return (...)`` shape is left
    alone.

    If either function ever needs a COMPUTED reason, that is the moment to
    move the level choice into a lookup the producers and the callers share,
    and to replace this test.
    """

    @staticmethod
    def _function(relative, name):
        """The whole module and the named function in it, parsed from disk.

        The module comes back as well because a loud logging method can
        be bound to a module-level name and only CALLED inside the
        producer (wh-wheel-refusal-notice.1.15), which nothing inside
        the function body shows.
        """
        path = _SERVICE_ROOT / relative
        assert path.is_file(), (
            f"{relative} is not where this test expects it; the walk reads "
            f"the file rather than importing it, so a moved module must be "
            f"followed here (looked in {path})"
        )
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source)
        for node in module.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return module, node
        raise AssertionError(
            f"no module-level {name} in {relative}; this test walks the "
            f"source, so a moved or renamed function must be followed here"
        )

    # A nested function, lambda or class opens a scope of its own. The
    # boundary node still belongs to the enclosing scope -- ``def helper():``
    # binds the name ``helper`` there -- but nothing inside its body does, so
    # the walk yields the boundary node and stops rather than descending.
    _SCOPES = (
        ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    )

    @classmethod
    def _own_scope(cls, node, top=True):
        """``node`` and every descendant that shares its lexical scope."""
        yield node
        if not top and isinstance(node, cls._SCOPES):
            return
        for child in ast.iter_child_nodes(node):
            yield from cls._own_scope(child, top=False)

    @classmethod
    def _statement_lists(cls, node):
        """Every block in ``node``'s own scope, ``node`` itself included.

        A nested function's body is a different scope answering a different
        caller (wh-wheel-refusal-notice.1.10), so its blocks are left out
        rather than read as this producer's own refusal branches.
        """
        for scoped in cls._own_scope(node):
            if scoped is not node and isinstance(scoped, cls._SCOPES):
                continue
            for field in ("body", "orelse", "finalbody"):
                block = getattr(scoped, field, None)
                if (
                    isinstance(block, list)
                    and block
                    and isinstance(block[0], ast.stmt)
                ):
                    yield block

    # Every logging method name, split by what ErrorNotificationHandler
    # does with the record (wh-wheel-refusal-notice.1.12). The handler is
    # installed at logging.ERROR in utils/logging_setup.py, so CRITICAL
    # reaches it exactly as ERROR does; reading only the literal name
    # "error" left critical, fatal, exception and ``logger.log`` invisible
    # here while the user still got the generic box.
    #
    # ``log`` is LOUD because its level is an argument this walk does not
    # evaluate. That is the fail-closed direction: a refusal that wants a
    # quiet record writes ``logger.debug(...)``, which costs one line,
    # and the alternative is reading ``logger.log(level, ...)`` as silent
    # when ``level`` came from a variable.
    _QUIET_METHODS = frozenset({"debug", "info", "warning", "warn"})
    _LOUD_METHODS = frozenset({
        "error", "exception", "critical", "fatal", "log",
    })

    @classmethod
    def _logging_methods(cls, node):
        """Every logging method name called anywhere inside ``node``.

        The RECEIVER is not read. ``logger.error(...)``,
        ``_log.error(...)`` after an alias, ``self._log.error(...)`` in a
        method and ``logging.getLogger(__name__).error(...)`` written
        inline all put one ERROR record in front of the handler, so a
        walk that insisted on the name ``logger`` was reading the
        variable rather than the record (wh-wheel-refusal-notice.1.12).
        """
        return [
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and (
                child.func.attr in cls._QUIET_METHODS
                or child.func.attr in cls._LOUD_METHODS
            )
        ]

    @classmethod
    def _record_written_by(cls, statement):
        """``"loud"`` / ``"quiet"`` for a statement that always logs, else None.

        A COMPOUND statement is not read here even when it contains a
        logging call: its records belong to one branch of it, and
        ``_falls_through_with_a_record`` decides whether they can reach
        the return below. Every other statement is read whole rather than
        only as a bare expression statement, because binding the return
        value -- ``_reported = logger.error(...)`` -- changes nothing
        about the record (wh-wheel-refusal-notice.1.12).
        """
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            block = getattr(statement, field, None)
            if isinstance(block, list) and block:
                return None
        methods = cls._logging_methods(statement)
        if not methods:
            return None
        if any(method in cls._LOUD_METHODS for method in methods):
            return "loud"
        return "quiet"

    @classmethod
    def _writes_a_record(cls, node):
        """True when a logging call sits anywhere inside ``node``."""
        return bool(cls._logging_methods(node))

    # Builtins that fetch an attribute or run source this walk cannot
    # follow (wh-wheel-refusal-notice.1.15). Neither producer needs any
    # of them, so refusing all three costs nothing and closes the
    # computed-name route in one line.
    _DYNAMIC_LOOKUPS = frozenset({"getattr", "eval", "exec"})

    # Members whose READ runs an expression written somewhere else.
    # A ``type`` statement defers its value, and a type parameter
    # defers its bound OR its constraints -- the two forms are
    # alternatives, so a set that names only one leaves the other
    # open (wh-wheel-refusal-notice.1.32). Touching any of these
    # evaluates it, and the expression can sit at module level
    # where this walk, which starts at a producer, never goes
    # (wh-wheel-refusal-notice.1.31).
    #
    # MEASURED, NOT ASSUMED. On the pinned 3.12 runtime every
    # dunder member of a bounded type parameter, a constrained
    # one, a class's type parameter, a plain type alias and a
    # parameterised one was read with the logger captured. Exactly
    # three reads ran author-written code: __bound__,
    # __constraints__ and __value__. __type_params__ ran none of
    # them and is kept anyway, because reaching a parameter
    # through it is how a branch reaches the other two.
    _LAZY_MEMBERS = frozenset({
        "__value__", "__type_params__", "__bound__",
        "__constraints__",
    })

    @classmethod
    def _loud_aliases_in(cls, module):
        """Module-level names bound to something that can log loudly.

        ``_shout = logger.error`` at module level leaves nothing inside
        the producer to see: the call there is a plain name
        (wh-wheel-refusal-notice.1.15). Any assignment whose VALUE
        mentions a loud method binds a name this walk then treats as
        loud wherever it is called.
        """
        aliases = set()
        for node in ast.walk(module):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            if node.value is None:
                continue
            if not any(
                isinstance(child, ast.Attribute)
                and child.attr in cls._LOUD_METHODS
                for child in ast.walk(node.value)
            ):
                continue
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for target in targets:
                aliases |= cls._names_bound_by(target)
        return aliases

    @staticmethod
    def _definitions_in(node):
        """Name -> node for every ``def`` or ``class`` directly in ``node``."""
        return {
            child.name: child
            for child in node.body
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
        }

    @classmethod
    def _defined_inside(cls, scope):
        """Names a ``def`` or ``class`` binds in ``scope`` itself.

        Their bodies are part of the subtree already being read, so a
        call to one needs no separate resolution
        (wh-wheel-refusal-notice.1.17).

        ONLY THIS SCOPE'S OWN STATEMENTS COUNT
        (wh-wheel-refusal-notice.1.26). This read ``ast.walk``
        until round 14, which collected definitions from every
        NESTED scope as well. A ``def _quiet`` written inside a
        function nested in the producer cannot bind the
        producer's own ``_quiet()`` call, and counting it there
        stopped the walk resolving that call at all -- so a loud
        module ``_quiet`` went unreported. A definition in a
        compound suite of this scope DOES count, because that
        suite runs here.

        A LAMBDA AND A COMPREHENSION DEFINE NOTHING
        (wh-wheel-refusal-notice.1.27). Both are scopes the walk
        now reads, and neither holds a statement, so neither can
        hold a ``def`` or a ``class``.

        NOTHING ELSE IS LOCAL, and until round 14 nothing else
        was recorded at all. A parameter holds whatever the
        caller passed and an assignment holds a value this walk
        cannot read, so neither is the module definition of that
        name either. _shadowing_bindings_in collects them, and a
        call to one FAILS rather than falling through to a
        module definition.
        """
        if not isinstance(
            scope,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            return set()
        return {
            statement.name
            for statement in cls._statements_in_scope(scope.body)
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
        }

    @classmethod
    def _shadowing_bindings_in(cls, scope):
        """Names ``scope`` binds to a value this walk cannot read.

        A parameter holds whatever the caller passed. An
        assignment, a loop target, a ``with ... as``, an
        ``except ... as``, an import alias, a ``match`` capture
        and a walrus all hold a value only running code decides.
        None of them is the module definition of the same name,
        so following that definition reads a body the call does
        not run (wh-wheel-refusal-notice.1.26).

        The binding forms come from _names_bound_by_statement,
        the same reader module level and a class suite use, so a
        form added there is covered here without a second list.
        A nested definition contributes only its declaration-time
        walruses: its own name is a definition, which
        _defined_inside decides.

        A LAMBDA AND A COMPREHENSION ARE SCOPES TOO
        (wh-wheel-refusal-notice.1.27). A lambda binds its
        parameters and holds one expression, not statements. A
        comprehension binds its ``for`` targets, which is the
        only binding form it has. Neither holds a statement, so
        the statement loop below reads nothing for them.
        """
        bound = set()
        if isinstance(scope, cls._COMPREHENSIONS):
            for generator in scope.generators:
                bound |= cls._names_bound_by(generator.target)
            return bound
        arguments = getattr(scope, "args", None)
        if arguments is not None:
            for argument in (
                list(arguments.posonlyargs)
                + list(arguments.args)
                + list(arguments.kwonlyargs)
            ):
                bound.add(argument.arg)
            for extra in (arguments.vararg, arguments.kwarg):
                if extra is not None:
                    bound.add(extra.arg)
        if not isinstance(
            scope,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            # A lambda: its parameters are already collected and
            # its body is one expression, not a statement list.
            return bound
        for statement in cls._statements_in_scope(scope.body):
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                bound |= cls._declared_walrus_bindings(statement)
                continue
            bound |= cls._names_bound_by_statement(statement)
        return bound

    @classmethod
    def _globals_declared_in(cls, scope):
        """Names ``scope`` declares ``global``.

        A ``global`` declaration is not a binding, it is a
        RESOLUTION RULE: inside the scope that writes it, the
        bare name skips every enclosing function scope and means
        the module-level name, whatever any enclosing scope binds
        (wh-wheel-refusal-notice.1.28). A nested ``def`` that
        declares a helper global therefore reaches the module
        helper even while the producer defines a quiet one of the
        same name, and until round 16 the walk resolved that call
        against the producer's set and returned early.

        ONLY THIS SCOPE'S OWN STATEMENTS COUNT. A declaration
        governs the scope that writes it and no other, so a
        nested ``def``'s ``global`` is read on that scope's own
        turn. _statements_in_scope gives exactly that: every
        compound block of this scope, and no deferred body.

        A LAMBDA AND A COMPREHENSION DECLARE NOTHING. Neither
        holds a statement, so neither can hold a ``global``.
        """
        if not isinstance(
            scope,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            return set()
        return {
            name
            for statement in cls._statements_in_scope(scope.body)
            if isinstance(statement, ast.Global)
            for name in statement.names
        }

    @classmethod
    def _nonlocals_declared_below(cls, scope):
        """Names a scope NESTED in ``scope`` can rebind here.

        An assignment inside a nested ``def`` is local to that
        ``def`` and cannot touch this scope -- unless the nested
        scope declares the name ``nonlocal``, which makes the
        assignment land in the nearest enclosing function that
        binds it. This scope's own reader cannot see it:
        _shadowing_bindings_in reads _statements_in_scope, which
        stops at a ``def`` (wh-wheel-refusal-notice.1.28).

        THE ESCAPE IS SEEN FROM OUT HERE, NOT FROM IN THERE, and
        that is why round 16's finding called this case
        fail-closed when it is not. A nested scope that declares
        ``nonlocal _helper`` and assigns to it has that
        assignment in its OWN shadowed set, so a call written
        inside it is reported. A call written in THIS scope after
        the nested one runs was resolved against a definition
        this scope still appeared to hold. Measured, not
        reasoned: the shape logged an ERROR record while the
        guard reported nothing.

        TWO DELIBERATE IMPRECISIONS, both fail-closed. A
        ``nonlocal`` names the nearest enclosing function that
        binds it, which may be a scope BETWEEN this one and the
        declaring one; every enclosing scope collects it anyway.
        And a declaration with no assignment after it is
        collected too, though it rebinds nothing.
        """
        if not isinstance(
            scope,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            return set()
        bound = set()
        for statement in cls._statements_in_scope(scope.body):
            if not isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            for child in ast.walk(statement):
                if isinstance(child, ast.Nonlocal):
                    bound |= set(child.names)
        return bound

    @classmethod
    def _bindings_and_escapes_in(cls, scope):
        """One scope's local set, shadowed set, and module escapes.

        The three answers are decided together because a
        ``global`` declaration changes what the other two mean
        (wh-wheel-refusal-notice.1.28).

        A NAME DECLARED GLOBAL AND ALSO BOUND HERE REBINDS THE
        MODULE NAME, so it is shadowed rather than escaped: what
        the module definition of that name holds after this scope
        runs is a value only running code decides. A name merely
        declared global escapes, and _nearest_bindings then lets
        it skip every enclosing scope and resolve at module
        level, which is what Python does.
        """
        local = cls._defined_inside(scope)
        shadowed = (
            cls._shadowing_bindings_in(scope)
            | cls._nonlocals_declared_below(scope)
        )
        declared = cls._globals_declared_in(scope)
        rebinds_module = declared & (local | shadowed)
        return (
            local - rebinds_module,
            shadowed | rebinds_module,
            declared - rebinds_module,
        )

    @classmethod
    def _callee_names(cls, func):
        """The bare names a call's callee resolves to, or None.

        A bare name gives itself. The ONE computed shape allowed
        is a multiplication whose left operand is a bare name,
        ``(<Name> * <count>)(...)``: it gives the LEFT name, so
        whatever that name defines is followed and read rather
        than trusted. Every other computed callee gives None and
        fails (wh-wheel-refusal-notice.1.20). An attribute callee
        is handled by the caller, not here.

        THE SHAPE IS WHAT IS CHECKED, NOT THE TYPE
        (wh-wheel-refusal-notice.1.22). This is the shape the
        primitive needs -- ``(Input * count)(*events)`` inside
        _send_mouse_events, which scroll_wheel calls -- but
        nothing here proves the left name is a ctypes type, and
        an earlier version of this docstring implied it did.
        What makes the exception safe is not the name: it is
        that the caller follows the left name INCLUDING its
        class header, so a multiplication answered by a
        metaclass or an inherited operator is read like any
        other definition instead of being trusted.
        """
        if isinstance(func, ast.Name):
            return [func.id]
        if (
            isinstance(func, ast.BinOp)
            and isinstance(func.op, ast.Mult)
            and isinstance(func.left, ast.Name)
        ):
            return [func.left.id]
        return None

    # A star import can bind any name the supplying module exports,
    # and nothing here can read that module. It stands in the rebinding
    # set for "every name", and the call site reports rather than
    # follows while it is present (wh-wheel-refusal-notice.1.21).
    _ANY_NAME = "*"

    # Every executable scope Python opens inside a function body.
    # A name written in one of these resolves against ITS bindings
    # first, so the walk must read each with its own sets rather
    # than with the producer's (wh-wheel-refusal-notice.1.27).
    _CHILD_SCOPES = (
        ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
        ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
        ast.GeneratorExp,
    )
    _COMPREHENSIONS = (
        ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    )

    @classmethod
    def _declaration_expressions(cls, statement):
        """The parts of a ``def`` or ``class`` that run at once.

        A ``def`` or ``class`` statement binds its own name and
        defers its body, but its decorators, default arguments,
        annotations, class bases and class keywords are all
        evaluated where the statement sits. A walrus in any of
        them therefore binds in the enclosing namespace
        (wh-wheel-refusal-notice.1.21).
        """
        parts = list(statement.decorator_list)
        if isinstance(statement, ast.ClassDef):
            parts.extend(statement.bases)
            parts.extend(
                keyword.value for keyword in statement.keywords
            )
            return parts
        parts.extend(cls._argument_expressions(statement.args))
        if statement.returns is not None:
            parts.append(statement.returns)
        return parts

    @staticmethod
    def _argument_expressions(arguments):
        """Defaults and annotations, which run where they are written.

        A lambda has the same ``arguments`` node as a ``def`` and
        no decorators or return annotation, so one reader serves
        both (wh-wheel-refusal-notice.1.27).
        """
        parts = [
            default for default in arguments.defaults
            if default is not None
        ]
        parts.extend(
            default for default in arguments.kw_defaults
            if default is not None
        )
        for group in (
            arguments.posonlyargs, arguments.args, arguments.kwonlyargs,
        ):
            parts.extend(
                argument.annotation for argument in group
                if argument.annotation is not None
            )
        for argument in (arguments.vararg, arguments.kwarg):
            if argument is not None and argument.annotation is not None:
                parts.append(argument.annotation)
        return parts

    @classmethod
    def _statements_in_scope(cls, suite):
        """Every statement that runs in one scope.

        A compound block -- ``if``, ``for``, ``while``, ``try``,
        ``with``, ``match`` -- belongs to the scope containing it,
        so its statements run in that scope and are returned here
        (wh-wheel-refusal-notice.1.24). A ``def``, ``async def``
        or ``class`` statement is returned but NOT descended
        into: a function body is deferred until something calls
        it, and a class body opens a scope of its own. Each
        caller decides what a returned definition means to it.
        """
        seen = []
        pending = list(suite)
        while pending:
            statement = pending.pop()
            seen.append(statement)
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef,
                 ast.ClassDef),
            ):
                continue
            for field in ("body", "orelse", "finalbody",
                          "handlers"):
                pending.extend(getattr(statement, field, None) or [])
            if isinstance(statement, ast.Match):
                for case in statement.cases:
                    pending.extend(case.body)
        return seen

    @classmethod
    def _names_bound_by_statement(cls, statement):
        """Names one statement binds in the scope that runs it.

        Every binding form the walk reads lives in this one
        place, so the module scope and a class suite cannot
        cover different sets (wh-wheel-refusal-notice.1.24).
        The forms read here are: ``=``, an annotated assignment,
        an augmented assignment, ``del``, a ``for`` target, a
        ``with ... as``, an ``except ... as``, an ``import``
        alias, a ``match`` capture, and a walrus.

        WHAT IT DOES NOT READ, stated rather than implied. A
        name bound by running code -- ``globals()[...] = ...``,
        ``setattr``, an ``exec`` of built source -- is invisible
        to any walk over syntax. So is a name bound inside a
        function body, which binds only once something calls
        that function.

        THAT SECOND EXCLUSION IS ABOUT THIS METHOD, NOT ABOUT
        THE WALK. This method is handed one statement and never
        descends into a ``def``, so a function body is out of
        reach here whichever caller asks. Two of the three
        callers want exactly that, because they are answering
        what a module or a class suite binds AT IMPORT. The
        third, _shadowing_bindings_in, hands this method the
        statements of a producer's own body ON PURPOSE: it is
        not asking what bound at import, it is asking what the
        producer rebinds before its call runs, and a name that
        body binds is the proof that a module definition of the
        same name is not what the call reaches
        (wh-wheel-refusal-notice.1.26).

        TWO DELIBERATE IMPRECISIONS, both fail-closed. A star
        import binds ``_ANY_NAME``, because which names it
        really binds cannot be read from this module alone. And
        a walrus is collected from anywhere inside the
        statement, nested bodies included, so one buried in a
        deferred body over-reports.
        """
        bound = set()
        targets = []
        if isinstance(statement, (ast.Assign, ast.Delete)):
            targets = statement.targets
        elif isinstance(
            statement,
            (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor),
        ):
            targets = [statement.target]
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            targets = [
                item.optional_vars
                for item in statement.items
                if item.optional_vars is not None
            ]
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                if alias.name == "*":
                    bound.add(cls._ANY_NAME)
                else:
                    bound.add(
                        (alias.asname or alias.name).split(".")[0]
                    )
        elif isinstance(statement, ast.TypeAlias):
            # A ``type`` statement binds its name to a lazy alias
            # object, which is not the definition of the same name
            # this walk would otherwise follow
            # (wh-wheel-refusal-notice.1.31).
            targets = [statement.name]
        elif isinstance(statement, ast.ExceptHandler):
            if statement.name:
                bound.add(statement.name)
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                for child in ast.walk(case.pattern):
                    if isinstance(
                        child, (ast.MatchAs, ast.MatchStar)
                    ) and child.name:
                        bound.add(child.name)
                    elif (
                        isinstance(child, ast.MatchMapping)
                        and child.rest
                    ):
                        bound.add(child.rest)
        for target in targets:
            bound |= cls._names_bound_by(target)
        for child in ast.walk(statement):
            if isinstance(child, ast.NamedExpr):
                bound |= cls._names_bound_by(child.target)
        return bound

    @classmethod
    def _declared_walrus_bindings(cls, statement):
        """Walrus names in the parts of a definition that run now.

        A ``def`` or ``class`` defers its body, but its
        decorators, defaults, annotations, bases and keywords all
        evaluate where the statement sits
        (wh-wheel-refusal-notice.1.21).
        """
        bound = set()
        for part in cls._declaration_expressions(statement):
            for child in ast.walk(part):
                if isinstance(child, ast.NamedExpr):
                    bound |= cls._names_bound_by(child.target)
        return bound

    @classmethod
    def _globals_rebound_in(cls, suite):
        """Module names any scope under ``suite`` rebinds through ``global``.

        A class body runs at import, in a namespace of its own,
        so an ordinary binding there is class-local. One whose
        name the suite has declared ``global`` is not: it binds
        at module level (wh-wheel-refusal-notice.1.21).

        A FUNCTION BODY IS DESCENDED INTO TOO, and until round 17
        it was not (wh-wheel-refusal-notice.1.30). The paragraph
        below headed THE KNOWN GAP said that a ``def`` whose body
        declares ``global`` and binds rebinds only when something
        calls it, and that seeing that needs call-graph reasoning
        this walk does not do. Being unable to say WHEN is not a
        reason to say NEVER: a name any scope in this file can
        rebind at module level is a name no call to it can be
        resolved against a module definition. Every such name is
        collected here and _module_level_rebindings reports it,
        which is fail-closed and needs no call graph at all.

        THE SHAPE THAT FORCED IT is one a scope-local answer
        cannot see. A nested ``def`` that declares a helper
        global and assigns to it writes no CALL to that name, so
        the round-16 rule -- which puts a declared-and-bound name
        in that scope's shadowed set -- reports nothing there.
        The producer then calls the name afterwards and was
        resolved against the module definition the write had
        already replaced. Measured, not reasoned: the shape
        logged an ERROR record while the guard reported nothing.

        BOTH HALVES READ THE WHOLE SUITE, not only its outermost
        statements (wh-wheel-refusal-notice.1.24). Round 11 read
        neither: it collected ``global`` only from a direct
        statement and a binding only from a direct assignment,
        so a ``global`` inside an ``if``, a ``for`` target, a
        ``match`` capture, a ``with ... as``, an ``import``
        alias and a nested class body all rebound a helper while
        this returned nothing. The scope walk and the binding
        reader shared with module level now decide both halves.

        A DEFINITION BINDS ITS OWN NAME, which the shared binding
        reader does not say because it reads statements the scope
        walk descends into, and it hands a ``def``, ``async def``
        or ``class`` back whole (wh-wheel-refusal-notice.1.25).
        Every one of them is added here. No test of nesting is
        needed: the intersection with this scope's ``global``
        declarations is what decides whether the name leaves the
        class, and a name never declared here cannot.

        A NESTED CLASS IS ITS OWN SCOPE and is resolved
        separately. ``global`` names the module in either case,
        but a declaration written in the inner suite governs the
        inner suite alone, so its names must not be matched
        against this one's bindings.

        THE GAP THAT IS LEFT is narrower than the one round 11
        recorded, and it is about WHEN rather than WHETHER. A
        rebinding written in a body that nothing ever calls is
        collected all the same, so a call written ABOVE the
        rebinding is refused along with one written below it.
        That over-reports, which is the safe direction, and
        neither target module contains a ``global`` statement at
        all.
        """
        bound = set()
        pending = [list(suite)]
        while pending:
            statements = cls._statements_in_scope(pending.pop())
            declared = {
                name
                for statement in statements
                if isinstance(statement, ast.Global)
                for name in statement.names
            }
            scope = set()
            for statement in statements:
                if isinstance(
                    statement,
                    (ast.FunctionDef, ast.AsyncFunctionDef,
                     ast.ClassDef),
                ):
                    # The statement itself runs and binds its own
                    # name, whatever its body does later
                    # (wh-wheel-refusal-notice.1.25).
                    scope.add(statement.name)
                    scope |= cls._declared_walrus_bindings(statement)
                    # A function body is queued as well as a class
                    # suite: a ``global`` written in either one
                    # rebinds a module name
                    # (wh-wheel-refusal-notice.1.30).
                    pending.append(statement.body)
                    continue
                scope |= cls._names_bound_by_statement(statement)
            bound |= scope & declared
        return bound

    @classmethod
    def _module_level_rebindings(cls, module):
        """Names that module level binds outside a deferred body.

        Following a definition by name is sound only while that
        name still refers to it once the module has finished
        importing (wh-wheel-refusal-notice.1.20). Any other
        module-level binding of the same name can leave the body
        read here describing something the call no longer runs,
        so such a call fails instead of being followed. The alias
        scan does not cover this: it records an assignment only
        when the value holds a loud ATTRIBUTE, and ``_quiet =
        _loud`` holds a plain name.

        WHAT COUNTS AS MODULE LEVEL is the whole question, and
        three rounds answered it too narrowly. Round 10 skipped
        every ``def``, ``async def`` and ``class`` statement
        outright, though only the BODY of one is deferred
        (wh-wheel-refusal-notice.1.21). Round 11 then read a
        class suite only one statement deep
        (wh-wheel-refusal-notice.1.24). Round 12 read every
        binding form and no definition form, so a ``def`` that
        replaced a helper inside an ``if`` was not a binding at
        all (wh-wheel-refusal-notice.1.25). What runs at module
        level is now decided in two shared places rather than
        written out here: _statements_in_scope says which
        statements run in a scope, and _names_bound_by_statement
        says what each one binds. Both are used again for a
        class suite, so the two scopes cannot drift apart.

        A definition statement is the one case this walk decides
        for itself, and round 13 found it deciding too little
        (wh-wheel-refusal-notice.1.25). Its declaration-time
        parts can bind through a walrus, a class suite can rebind
        through ``global``, and the statement ALSO binds its own
        name when it runs -- which is the whole defect if that
        name is one _definitions_in already maps. Only a
        definition reached through a compound suite counts here.
        A direct one is what _definitions_in points at, so
        recording it would refuse every ordinary helper in both
        target modules. Its body stays deferred either way, read
        only when a call follows it.

        WHAT IS LEFT IS TIMING, NOT DISCOVERY
        (wh-wheel-refusal-notice.1.33). Until round 17 this
        paragraph said a module-level ``def`` whose BODY declares
        ``global`` and binds was out of reach, because seeing it
        would need call-graph reasoning. That stopped being true
        when _globals_rebound_in began queueing every definition
        body: the rebinding is found whether or not anything
        calls the function. The paragraph outlived the defect it
        described, which is worse than never writing it, so it is
        replaced rather than trimmed. The remaining limit is the
        one _globals_rebound_in states: this walk answers WHETHER
        a name is rebound and never WHEN, so a call written
        before the rebinding runs is reported as loud.
        """
        # Every ``global`` rebinding anywhere in the file, deferred
        # bodies included, in ONE call rather than per class suite
        # (wh-wheel-refusal-notice.1.30).
        bound = cls._globals_rebound_in(module.body)
        direct = {id(statement) for statement in module.body}
        for statement in cls._statements_in_scope(module.body):
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef,
                 ast.ClassDef),
            ):
                # The body is deferred; the declaration is not.
                bound |= cls._declared_walrus_bindings(statement)
                if id(statement) not in direct:
                    # Reached through a compound suite that RUNS,
                    # so this definition binds the name again and
                    # _definitions_in, which reads module.body
                    # alone, still points at the earlier one
                    # (wh-wheel-refusal-notice.1.25).
                    bound.add(statement.name)
                continue
            bound |= cls._names_bound_by_statement(statement)
        return bound

    @classmethod
    def _evaluated_where_written(cls, scope):
        """The parts of ``scope`` that run in the scope holding it.

        A ``def`` or ``class`` evaluates its decorators, defaults,
        annotations, bases and keywords where the statement sits;
        _declaration_expressions already lists those. A lambda
        evaluates its defaults and annotations there too. A
        comprehension evaluates its FIRST iterable there, and
        nothing else: every later iterable, every ``if``, every
        target and the element expression all run inside the
        comprehension (wh-wheel-refusal-notice.1.27).
        """
        if isinstance(scope, cls._COMPREHENSIONS):
            return [scope.generators[0].iter] if scope.generators else []
        if isinstance(scope, ast.Lambda):
            # A lambda has no decorators and no return annotation,
            # so its arguments are the whole of it.
            return cls._argument_expressions(scope.args)
        return cls._declaration_expressions(scope)

    @classmethod
    def _evaluated_inside(cls, scope):
        """The parts of ``scope`` that run in ``scope`` itself."""
        if isinstance(scope, ast.Lambda):
            return [scope.body]
        if isinstance(scope, cls._COMPREHENSIONS):
            parts = (
                [scope.key, scope.value]
                if isinstance(scope, ast.DictComp) else [scope.elt]
            )
            for index, generator in enumerate(scope.generators):
                parts.append(generator.target)
                if index:
                    # The first iterable runs outside; every later
                    # one is evaluated over a target this scope
                    # already binds.
                    parts.append(generator.iter)
                parts.extend(generator.ifs)
            return parts
        return list(scope.body)

    @staticmethod
    def _scope_label(scope, where):
        """A name for ``scope`` that says where it sits."""
        name = getattr(scope, "name", None)
        if name is None:
            name = {
                ast.Lambda: "<lambda>",
                ast.ListComp: "<listcomp>",
                ast.SetComp: "<setcomp>",
                ast.DictComp: "<dictcomp>",
                ast.GeneratorExp: "<genexpr>",
            }[type(scope)]
        return f"{where}.{name}"

    @classmethod
    def _nodes_in_scope(cls, scope, where, header_read, inherited, pending):
        """Yield what ``scope`` evaluates, queueing its child scopes.

        This replaces ``ast.walk(scope)``, which crossed into
        every child scope while the caller kept ONE local and
        shadowed set (wh-wheel-refusal-notice.1.27). A name
        written inside a nested function, class suite, lambda or
        comprehension resolves against that scope's bindings
        first, so each child is queued with its own chain and
        read on its own turn.

        ``header_read`` says whether some enclosing scope already
        read this one's declaration. A child queued below was
        yielded there, decorators and all. A module definition
        that _verdict_for followed was not, so its own node and
        the parts that run where it is written are read here.
        """
        stack: list[ast.AST] = list(cls._evaluated_inside(scope))
        if not header_read:
            stack.extend(cls._evaluated_where_written(scope))
            yield scope
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, cls._CHILD_SCOPES):
                pending.append((
                    node, cls._scope_label(node, where), inherited, True,
                ))
                # Its declaration runs HERE even though its body
                # does not, so those parts stay in this stream.
                stack.extend(cls._evaluated_where_written(node))
                continue
            stack.extend(ast.iter_child_nodes(node))

    @staticmethod
    def _nearest_bindings(chain):
        """Resolve one name-to-meaning answer over a scope chain.

        ``chain`` runs innermost first. Python resolves a bare
        name in the nearest enclosing scope that binds it, so the
        first entry to bind a name decides it and no outer entry
        can change that answer (wh-wheel-refusal-notice.1.27).
        Within ONE scope a shadow still beats a definition, which
        is round 14's fail-closed order kept intact.

        A THIRD ANSWER IS "NEITHER", and it is what a ``global``
        declaration asks for (wh-wheel-refusal-notice.1.28). Such
        a name is decided at the scope that declares it -- no
        enclosing entry may put it in either set -- and it is
        returned in neither, so the caller falls through to the
        builtins, definitions and rebinding checks, which are the
        module-level resolution Python performs.
        """
        local = set()
        shadowed = set()
        escaped = set()
        for scope_local, scope_shadowed, scope_escaped in chain:
            decided = local | shadowed | escaped
            escaped |= scope_escaped - decided
            decided |= escaped
            fresh_shadow = scope_shadowed - decided
            shadowed |= fresh_shadow
            local |= scope_local - decided - fresh_shadow
        return local, shadowed

    @staticmethod
    def _type_parameter_site(node, where):
        """Refuse a PEP 695 type parameter list, or return None.

        ``def helper[T: _loud()]()`` puts an executable expression
        in a scope this walk does not model
        (wh-wheel-refusal-notice.1.29). Python 3.12 is the
        supported runtime -- pyproject.toml pins
        ``>=3.12,<3.13`` -- so a bound is not future syntax: it
        is code that runs the moment something reads
        ``__type_params__[0].__bound__``, and a refusal branch
        can read it before returning.

        THE WALK NEVER YIELDED THE NODE AT ALL, which is why no
        existing branch could have caught it.
        _declaration_expressions lists decorators, arguments,
        returns, bases and keywords, and _evaluated_inside lists
        a body; ``type_params`` is in neither, so the bound was
        not in the stream for anything to judge.

        REFUSED RATHER THAN MODELLED, deliberately. A type
        parameter list opens an ANNOTATION SCOPE that binds the
        parameter names, so its expressions cannot simply be
        added to the enclosing stream -- they would be resolved
        against the wrong bindings, which is the mistake round 15
        removed. Modelling a fifth kind of scope to describe
        syntax neither target module uses would be more code and
        more ways to be wrong than saying no. Both target modules
        were checked: neither writes a type parameter list.

        A ``type`` STATEMENT WITH NO PARAMETERS IS LEFT ALONE.
        Its value is lazily evaluated but is traversed inline by
        the ordinary walk, so a loud call there is already
        reported -- verified by running both forms. One that
        DOES carry parameters is refused here with the rest,
        because its annotation scope binds those names.
        """
        parameters = getattr(node, "type_params", None)
        if not parameters:
            return None
        return (
            where, node.lineno,
            f"a type parameter list on "
            f"{getattr(node, 'name', 'a type alias')}, whose bounds "
            "run in an annotation scope this walk does not read",
        )

    @classmethod
    def _verdict_for(
        cls, named, where, lineno, aliases, definitions, rebound,
        local, shadowed, exempt, pending,
    ):
        """Report one name, follow what it defines, or pass it.

        One place decides what a name means, so a call and a class
        header cannot drift apart (wh-wheel-refusal-notice.1.22).
        Following appends to ``pending``; everything else comes
        back as sites to report.

        A PRODUCER-LOCAL BINDING IS CHECKED BEFORE A LOCAL
        DEFINITION (wh-wheel-refusal-notice.1.26), so a scope
        that both defines a name and later binds it to something
        else fails rather than being read as its own definition.
        """
        if named in cls._DYNAMIC_LOOKUPS:
            return [(where, lineno, f"a call to {named}()")]
        if named in aliases:
            return [(
                where, lineno,
                f"a call to {named}(), bound to a logging method "
                "at module level",
            )]
        if named == exempt:
            return []
        if named in shadowed:
            return [(
                where, lineno,
                f"a call to {named}(), which {where} binds itself to a "
                "value this walk cannot read, so no definition of that "
                "name describes what runs",
            )]
        if named in local:
            return []
        if hasattr(builtins, named):
            return []
        if named in definitions:
            if cls._ANY_NAME in rebound:
                return [(
                    where, lineno,
                    f"a call to {named}(), in a module carrying a "
                    "star import, which can bind any name over the "
                    "definition read here",
                )]
            if named in rebound:
                return [(
                    where, lineno,
                    f"a call to {named}(), which module level binds "
                    "again outside its definition, so the body read "
                    "here may not be what runs",
                )]
            # A module definition's enclosing scope is the module,
            # so it inherits no producer bindings, and nobody has
            # read its declaration yet (wh-wheel-refusal-notice.1.27).
            pending.append((definitions[named], named, (), False))
            return []
        return [(
            where, lineno,
            f"a call to {named}(), which is neither a builtin nor "
            "defined in this module, so what it logs cannot be read "
            "here",
        )]

    @classmethod
    def _loud_record_sites(cls, function, module, exempt):
        """Every place an ERROR-or-above record can start, from ``function``.

        Reachability inside a body is not consulted, and that is the
        point (wh-wheel-refusal-notice.1.15, .1.16): each body is read
        whole, nested scopes included, so a record in an ``if`` header,
        in a ``finally``, or inside a helper defined in the producer
        counts exactly like one written above the return.

        WHAT THE PRODUCER CALLS is followed as well
        (wh-wheel-refusal-notice.1.17), because a module-level
        ``def _helper(): logger.error(...)`` leaves nothing loud inside
        the producer to find. A bare-name call resolves to a builtin, to
        a definition inside the scope being read, or to a ``def`` or
        ``class`` at module level in the same file -- which is then read
        the same way. Anything else is reported rather than assumed
        silent. ``exempt`` carries the wrapper's one delegate, which
        _delegation_target has already checked and which this test reads
        as a producer in its own right.

        A COMPUTED CALLEE IS REFUSED, AND SO IS A REBOUND
        DEFINITION (wh-wheel-refusal-notice.1.20). Resolving only
        bare-name calls meant ``[_helper][0]()`` reached a helper
        this walk never read, and following a definition by name
        meant a later module-level rebinding of that name went
        unnoticed. See _callee_names and _module_level_rebindings
        for the two rules and for the single computed-callee
        exception.

        A CLASS HEADER IS READ, NOT ONLY A CLASS BODY
        (wh-wheel-refusal-notice.1.22). A base or a
        ``metaclass=`` can answer an operator the class appears
        to support, so a class the walk follows had a route into
        code the walk never opened. Bare names in the header are
        resolved exactly as a call is; attribute bases such as
        ``ctypes.Structure`` are checked by attribute name like
        every other attribute.

        A DECORATOR IS REFUSED OUTRIGHT
        (wh-wheel-refusal-notice.1.19). A plain ``@name`` is an
        ``ast.Name`` in ``decorator_list`` -- not a call and not
        an attribute -- so no branch below reacts to it and the
        decorator's own body is never read, while at run time it
        may return any callable at all in place of the one it
        decorates. Every ``def``, ``async def`` and ``class``
        reached here, the producer itself included, fails if it
        carries a decorator of any kind.
        """
        aliases = cls._loud_aliases_in(module)
        definitions = cls._definitions_in(module)
        rebound = cls._module_level_rebindings(module)
        sites = []
        seen = set()
        pending = [(function, function.name, (), False)]
        while pending:
            scope, where, outer, header_read = pending.pop()
            if id(scope) in seen:
                continue
            seen.add(id(scope))
            own = cls._bindings_and_escapes_in(scope)
            local, shadowed = cls._nearest_bindings((own,) + outer)
            # A class body does not close over: a function written
            # inside a class suite cannot see what that suite binds,
            # so its children inherit the chain WITHOUT this scope
            # (wh-wheel-refusal-notice.1.27).
            inherited = (
                outer if isinstance(scope, ast.ClassDef)
                else (own,) + outer
            )
            for node in cls._nodes_in_scope(
                scope, where, header_read, inherited, pending
            ):
                site = cls._type_parameter_site(node, where)
                if site is not None:
                    sites.append(site)
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef,
                     ast.ClassDef),
                ):
                    if node.decorator_list:
                        sites.append((
                            where, node.lineno,
                            f"a decorator on {node.name}, which "
                            "can replace it with a callable "
                            "written where this walk never looks",
                        ))
                    if isinstance(node, ast.ClassDef):
                        # The header runs where the class is
                        # written, and a base or a metaclass can
                        # answer any operator the class appears
                        # to support (.1.22).
                        header = [
                            base.id for base in node.bases
                            if isinstance(base, ast.Name)
                        ] + [
                            keyword.value.id
                            for keyword in node.keywords
                            if isinstance(keyword.value, ast.Name)
                        ]
                        for named in header:
                            sites.extend(cls._verdict_for(
                                named, where, node.lineno, aliases,
                                definitions, rebound, local, shadowed,
                                exempt, pending,
                            ))
                elif isinstance(node, ast.Attribute):
                    if node.attr in cls._LOUD_METHODS:
                        sites.append(
                            (where, node.lineno,
                             f"a reference to .{node.attr}")
                        )
                    elif node.attr in cls._LAZY_MEMBERS:
                        # The expression this READ runs can sit at
                        # module level, which the walk never visits
                        # (wh-wheel-refusal-notice.1.31).
                        sites.append((
                            where, node.lineno,
                            f"a reference to .{node.attr}, which runs a "
                            "deferred expression that may be written "
                            "where this walk never looks",
                        ))
                elif isinstance(node, ast.Constant):
                    if (
                        isinstance(node.value, str)
                        and node.value in cls._LOUD_METHODS
                    ):
                        sites.append(
                            (where, node.lineno,
                             f"the method name {node.value!r}")
                        )
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        # Checked by its attribute NAME above; the
                        # object it hangs off is foreign code with
                        # no source to read in this file.
                        continue
                    names = cls._callee_names(node.func)
                    if names is None:
                        sites.append((
                            where, node.lineno,
                            "a call through a computed "
                            f"{type(node.func).__name__}, so what it "
                            "runs cannot be read here",
                        ))
                        continue
                    for called in names:
                        sites.extend(cls._verdict_for(
                            called, where, node.lineno, aliases,
                            definitions, rebound, local, shadowed,
                            exempt, pending,
                        ))
        return sorted(sites)

    @classmethod
    def _falls_through_with_a_record(cls, statement):
        """True when ``statement`` can log AND continue to the next sibling.

        A block whose last statement is a ``return`` cannot reach anything
        below the compound statement it belongs to, so its records belong to
        a different path and are ignored -- that is the ordinary
        ``if ...: logger.warning(...); return (False, "...")`` guard. A block
        that logs and then falls out is the shape the walk cannot attribute.
        """
        blocks = []
        for field in ("body", "orelse", "finalbody"):
            block = getattr(statement, field, None)
            if isinstance(block, list) and block:
                blocks.append(block)
        # ``handlers`` for try / except and ``cases`` for match
        # (wh-wheel-refusal-notice.1.8). Both keep their blocks on a list of
        # clause nodes rather than on the statement, so neither is reachable
        # through the field loop above, and a match case that logs and falls
        # through read as no record at all.
        for group in ("handlers", "cases"):
            for clause in getattr(statement, group, None) or []:
                if getattr(clause, "body", None):
                    blocks.append(clause.body)
        return any(
            any(cls._writes_a_record(s) for s in block)
            and not isinstance(block[-1], ast.Return)
            for block in blocks
        )

    @classmethod
    def _an_error_is_written_before(cls, block, index, name):
        """True when ANY ERROR-or-above record sits above ``block[index]``.

        Every record above the return is read, not only the nearest one
        (wh-wheel-refusal-notice.1.9). All of them run before the return, so
        all of them reach ErrorNotificationHandler; a branch that logs an
        ERROR and then a WARNING has still shown the user the generic box,
        and stopping at the WARNING declared that branch silent.

        Only the same block counts. A record written in an enclosing block
        belongs to a different path through the function, so reading it here
        would credit a refusal with a report it never makes. A preceding
        sibling that can log and then fall through to this return is neither
        case, and fails rather than reading as no record at all.
        """
        found_an_error = False
        for statement in reversed(block[:index]):
            level = cls._record_written_by(statement)
            if level is not None:
                found_an_error = found_an_error or level == "loud"
                continue
            if cls._falls_through_with_a_record(statement):
                raise AssertionError(
                    f"{name} line {statement.lineno}: this statement can "
                    "write a log record and then fall through to the refusal "
                    "return below it, so nothing here can say whether that "
                    "return already reported itself. Put the record in the "
                    "same block as the return it belongs to."
                )
        return found_an_error

    @staticmethod
    def _names_bound_by(target):
        """Every plain name an assignment target binds."""
        return {
            sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name)
        }

    @classmethod
    def _reaches_out_of(cls, scope):
        """Names a nested scope can bind in the scope that HOLDS it.

        ``_own_scope`` stops at a nested def, lambda or class, which is
        right for its ordinary local body and wrong for two shapes that
        still move the enclosing binding (wh-wheel-refusal-notice.1.13).
        A ``nonlocal`` declaration anywhere inside reaches back out. A
        walrus in a decorator, a default, an annotation or a class base is
        evaluated in the enclosing scope at definition time, so it binds
        there rather than inside.

        Every ``nonlocal``, ``global`` and walrus inside the nested scope
        counts, wherever it sits. That is stricter than Python -- a walrus
        in a nested BODY binds only in that body -- and stricter is the
        safe direction for a guard: the cost is renaming a variable inside
        a helper, and the alternative is deciding, per expression, which
        scope evaluates it. The comprehension target below takes the same
        trade for the same reason.
        """
        names = set()
        for child in ast.walk(scope):
            if isinstance(child, (ast.Global, ast.Nonlocal)):
                names |= set(child.names)
            elif isinstance(child, ast.NamedExpr):
                names |= cls._names_bound_by(child.target)
        return names

    @classmethod
    def _delegation_target(cls, function, delegates_to, name):
        """The one local name this function may delegate its answer to.

        None when the producer may not delegate. Otherwise the name bound by
        ``from <module> import <attribute>`` inside the function, which must
        exist exactly once and must never be rebound there.
        """
        if delegates_to is None:
            return None
        module, attribute = delegates_to
        bound = set()
        rebound = set()
        # The function's own parameters, and only its own: a nested
        # function's parameters bind in ITS scope, and _own_scope stops
        # before them.
        rebound |= {
            arg.arg for arg in ast.walk(function.args)
            if isinstance(arg, ast.arg)
        }
        for node in cls._own_scope(function):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name != "*", (
                        f"{name} may not use `from ... import *`; a star "
                        "import can bind any name at all, including the one "
                        "this test is about to trust as the delegate"
                    )
                    if node.module == module and alias.name == attribute:
                        bound.add(alias.asname or alias.name)
                    else:
                        rebound.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    rebound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    rebound |= cls._names_bound_by(target)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
                # NamedExpr is the walrus (wh-wheel-refusal-notice.1.10): it
                # binds in this scope like any assignment, and reading only
                # the three ordinary assignment forms let it through.
                rebound |= cls._names_bound_by(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                # A comprehension target really binds in the comprehension's
                # own scope, so counting it here is stricter than Python is.
                # Stricter is the safe direction for a guard: the cost is
                # renaming a loop variable, and the alternative is reasoning
                # about which comprehension a name escapes.
                rebound |= cls._names_bound_by(node.target)
            elif isinstance(node, ast.withitem):
                if node.optional_vars is not None:
                    rebound |= cls._names_bound_by(node.optional_vars)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
                if node.name:
                    rebound.add(node.name)
            elif isinstance(node, ast.MatchMapping):
                if node.rest:
                    rebound.add(node.rest)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                rebound |= set(node.names)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if node is not function:
                    rebound.add(node.name)
                    rebound |= cls._reaches_out_of(node)
            elif isinstance(node, ast.Lambda):
                rebound |= cls._reaches_out_of(node)
        usable = bound - rebound
        assert len(usable) == 1, (
            f"{name} may delegate its whole answer to "
            f"{module}.{attribute}, so that name must be imported inside the "
            f"function exactly once and never rebound there. Imported as "
            f"{sorted(bound)}; rebound in the function: "
            f"{sorted(bound & rebound)}. A rebound name could be any "
            "function at all, and delegating to it would hide whatever "
            "refusal it reports."
        )
        return usable.pop()

    @staticmethod
    def _reason_returned(statement, name, delegate):
        """``"<reason>"``, None for the success return, or ``_DELEGATES``."""
        shape = (
            f"{name} line {statement.lineno}: every return must be written "
            f"as `return (True, None)` or `return (False, \"<reason>\")`"
            + (f", or `return {delegate}(...)`" if delegate else "")
            + ", so this test can read the refusal reason out of the source"
        )
        value = statement.value
        if (
            delegate is not None
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == delegate
        ):
            return _DELEGATES
        assert isinstance(value, ast.Tuple) and len(value.elts) == 2, shape
        succeeded, reason = value.elts
        assert isinstance(succeeded, ast.Constant), shape
        if succeeded.value is True:
            assert isinstance(reason, ast.Constant) and reason.value is None, shape
            return None
        assert succeeded.value is False, shape
        assert isinstance(reason, ast.Constant), shape
        assert isinstance(reason.value, str), shape
        return reason.value

    def test_the_published_list_matches_every_refusal_branch_in_the_source(self):
        # reason -> producer -> the set of answers its branches gave. A SET,
        # not one answer (wh-wheel-refusal-notice.1.9): one producer can
        # refuse with the same reason from more than one branch, and keeping
        # a single value let whichever branch the walk reached last overwrite
        # the others. Two branches that disagree is exactly the defect this
        # test exists to catch, so the disagreement must survive the walk.
        states_of = {}

        for relative, name, delegates_to in _WHEEL_REFUSAL_PRODUCERS:
            module, function = self._function(relative, name)
            delegate = self._delegation_target(function, delegates_to, name)
            loud = self._loud_record_sites(function, module, delegate)
            assert not loud, (
                f"{name} may not reach a logging method at ERROR or above "
                f"at all, in its own body or in anything it calls. No wheel "
                f"refusal reports itself through the generic "
                f"ErrorNotificationHandler box, so neither producer has any "
                f"use for one -- and deciding WHICH refusal a loud record "
                f"belongs to means being right about reachability, which "
                f"the level walk below is not: a record in an `if` header, "
                f"in a `finally` that runs on the way out, behind getattr, "
                f"through an alias, or inside a helper the producer calls "
                f"all read there as no record at all "
                f"(wh-wheel-refusal-notice.1.15, .1.16, .1.17). Write the "
                f"record at debug, info or warning instead, and let the "
                f"caller's own written notice be the report. A call this "
                f"walk cannot follow is listed here too, for the same "
                f"reason: it is where the next hidden record would sit. "
                f"Found in {relative}, as (function, line, what): {loud}"
            )
            found_a_non_refusal_return = False
            states = {}
            for block in self._statement_lists(function):
                for index, statement in enumerate(block):
                    if not isinstance(statement, ast.Return):
                        continue
                    reason = self._reason_returned(statement, name, delegate)
                    if reason is None or reason is _DELEGATES:
                        found_a_non_refusal_return = True
                        continue
                    states.setdefault(reason, set()).add(
                        self._an_error_is_written_before(block, index, name)
                    )

            assert found_a_non_refusal_return, (
                f"the walk found no success return in {name}, so it is not "
                f"reading the whole function"
            )
            assert states, (
                f"the walk found no refusal returns in {name}, so it is not "
                f"reading the whole function"
            )
            states_of[name] = states

        by_reason = {}
        for producer, states in states_of.items():
            for reason, flags in states.items():
                by_reason.setdefault(reason, {}).setdefault(producer, set())
                by_reason[reason][producer] |= flags

        # ERROR or not, rather than the raw level. That is exactly what the
        # published tuple encodes and all the callers can read, so it is the
        # only disagreement that can hurt anyone. Holding the raw levels
        # equal would also fail a producer that logs a shared reason at
        # WARNING while the other writes no record at all -- identical as
        # far as every caller is concerned.
        disagreed = {
            reason: producers for reason, producers in by_reason.items()
            if len({
                flag for flags in producers.values() for flag in flags
            }) > 1
        }
        assert not disagreed, (
            "producers that return the same refusal reason must agree on "
            "whether it logs an ERROR. WHEEL_REFUSALS_THAT_LOG_ERROR names "
            "reasons, "
            "not branches, so the callers cannot tell two producers apart: a "
            "reason listed because ONE producer logs an ERROR silences the "
            "caller's own notice for the other producer too, and that path "
            "then reports nothing at all. Disagreeing reasons: "
            f"{ {r: sorted((q, sorted(f)) for q, f in p.items()) for r, p in disagreed.items()} }"
        )

        logs_an_error = {
            reason for reason, producers in by_reason.items()
            if any(True in flags for flags in producers.values())
        }
        # THE UNIFORM RULE (wh-wheel-refusal-notice.1.7, .1.12). Round 5
        # held these two SETS EQUAL, which is a weaker claim than the rule
        # the callers depend on. Equality was satisfiable by adding a new
        # ERROR branch AND its reason to the published tuple in one edit,
        # and that is the zero-notice arrangement .1.7 measured: the
        # caller reads the reason out of the tuple and stays quiet, while
        # ErrorNotificationHandler drops the generic box for a repeat
        # inside its ten-second window. Holding both sides EMPTY implies
        # the old equality and cannot be satisfied that way.
        assert not logs_an_error, (
            "no wheel refusal may write a record at ERROR or above. The "
            "generic ErrorNotificationHandler box is rate-limited per "
            "(logger, level, message) for ten seconds "
            "(utils/error_notifier.py), so it is not a report anyone can "
            "count on; every wheel refusal is reported by its caller's own "
            "written notice instead. Reasons whose branch logs at ERROR or "
            f"above: {sorted(logs_an_error)}. Levels read from the source: "
            f"{ {n: sorted((r, sorted(f)) for r, f in v.items()) for n, v in states_of.items()} }"
        )
        assert not wis.WHEEL_REFUSALS_THAT_LOG_ERROR, (
            "WHEEL_REFUSALS_THAT_LOG_ERROR must stay EMPTY. It is what the "
            "callers read to decide whether a refusal already reported "
            "itself, and since .1.7 none does. A reason listed here "
            "silences that caller's written notice and leaves the user with "
            "the rate-limited generic box, or with nothing at all. The list "
            f"says {sorted(wis.WHEEL_REFUSALS_THAT_LOG_ERROR)}."
        )
