"""The STT loading dialog shows the whole text after the start-time limit.

David reported on 2026-11-13 at 22:35 that the dialog was not tall enough
for the text after the start-time limit, and that the text scrolled
(wh-loading-dialog-text-height). The tests here use the real
`WorkingDialog` and run the Qt event loop between the first text and the
text after the limit, as the running app does: the launcher changes the
text seconds after the dialog came up. The merged tests in
tests/test_gui.py changed the text with no event loop between the two
texts, and they passed while the real dialog did not grow.

Each test compares the height that the wrapped text needs with the
visible height of the text area, for every state of the moving dots.

Display scale: the tests cover the scale of the screen they run on. To
cover another scale, run this file with the environment variable
QT_SCALE_FACTOR set; the factor multiplies the device pixel ratio of the
screen.
"""

import tomllib
from pathlib import Path

import pytest

# The services/ directory: tests -> wheelhouse -> services.
_SERVICES_DIR = Path(__file__).resolve().parents[2]

# The text that the launcher shows after the start-time limit
# (stt/remote_stt_launcher.py). The test copies it, because the text of
# the dialog is not in the scope of this bead; a change to the text there
# does not change what these tests prove about the size of the dialog.
_AFTER_LIMIT_TEXT = (
    "Loading {name} takes longer than usual. Wheelhouse cannot hear you yet."
)


def _shipped_display_names():
    """The display name of each shipped STT provider, from its config file."""
    names = []
    for config in sorted((_SERVICES_DIR / "stt_providers").glob("*/config.toml")):
        with config.open("rb") as handle:
            data = tomllib.load(handle)
        name = data.get("provider", {}).get("display_name")
        if name:
            names.append(name)
    return names


_SHIPPED_NAMES = _shipped_display_names()
# Two names of 40 characters: one with spaces, which wraps between its
# words, and one with no space, which makes the first text wider than the
# plaque.
_NAMES_OF_40_CHARACTERS = ["Local Speech Engine With Long Name 12345", "X" * 40]
assert all(len(name) == 40 for name in _NAMES_OF_40_CHARACTERS)

_NAMES = _SHIPPED_NAMES + _NAMES_OF_40_CHARACTERS


def test_the_shipped_provider_names_are_found():
    """The tests below cover every shipped provider, so the list is not empty.

    The shipped providers on 2026-11-13: Google Cloud and
    Parakeet V3 (Local).
    """
    assert "Google Cloud" in _SHIPPED_NAMES
    assert "Parakeet V3 (Local)" in _SHIPPED_NAMES


@pytest.fixture
def dialog(qapp):
    from gui import WorkingDialog

    dlg = WorkingDialog()
    yield dlg
    dlg._dot_timer.stop()
    dlg.close()
    dlg.deleteLater()
    qapp.processEvents()


def _needed_text_height(label):
    """The height that the label's text needs when it wraps at its own width."""
    from PySide6.QtCore import QRect, Qt

    width = label.contentsRect().width()
    return label.fontMetrics().boundingRect(
        QRect(0, 0, width, 100000),
        int(Qt.TextFlag.TextWordWrap),
        label.text(),
    ).height()


def _plaque_label(dialog):
    from PySide6.QtWidgets import QLabel

    for label in dialog.findChildren(QLabel):
        pixmap = label.pixmap()
        if pixmap is not None and not pixmap.isNull():
            return label
    raise AssertionError("the dialog has no plaque image")


def _show_first_then_after_limit(qapp, dialog, name):
    """Show the first text, run the event loop, then show the text after the limit."""
    dialog._dot_timer.stop()
    dialog.show_working(f"Loading {name}", "stt:1")
    dialog._dot_timer.stop()
    qapp.processEvents()
    first_geometry = dialog.geometry()
    dialog.show_working(_AFTER_LIMIT_TEXT.format(name=name), "stt:1")
    # The tests move the dots themselves, so the timer must not move them
    # between two measurements.
    dialog._dot_timer.stop()
    qapp.processEvents()
    return first_geometry


@pytest.mark.parametrize("name", _NAMES)
def test_the_text_after_the_limit_fits_in_every_state_of_the_dots(
    qapp, dialog, name
):
    """Every line of the text and of the dots shows, with no cut line."""
    _check_the_text_fits_in_every_state_of_the_dots(qapp, dialog, name)


def _check_the_text_fits_in_every_state_of_the_dots(qapp, dialog, name):
    _show_first_then_after_limit(qapp, dialog, name)
    label = dialog._message_label

    for state in range(4):
        if state:
            dialog._animate_dots()
            qapp.processEvents()
        assert label.text().endswith("." * state)
        needed = _needed_text_height(label)
        visible = label.contentsRect().height()
        assert needed <= visible, (
            f"{name!r}, {state} dots: the wrapped text needs {needed} px, "
            f"the text area shows {visible} px"
        )
        # The text area is inside the dialog, so no part of it is below
        # the edge of the window.
        text_area = label.contentsRect().translated(label.mapTo(dialog, label.rect().topLeft()))
        assert dialog.rect().contains(text_area), (
            f"{name!r}, {state} dots: the text area {text_area.getRect()} "
            f"is not inside the dialog {dialog.rect().getRect()}"
        )


@pytest.mark.parametrize("name", _NAMES)
def test_the_dialog_keeps_its_size_between_the_states_of_the_dots(
    qapp, dialog, name
):
    """The dialog and its text area do not change size while the dots move."""
    _check_the_size_stays_between_the_states_of_the_dots(qapp, dialog, name)


def _check_the_size_stays_between_the_states_of_the_dots(qapp, dialog, name):
    _show_first_then_after_limit(qapp, dialog, name)
    label = dialog._message_label
    dialog_sizes = set()
    label_geometries = set()

    for state in range(8):
        if state:
            dialog._animate_dots()
            qapp.processEvents()
        dialog_sizes.add(dialog.size().toTuple())
        label_geometries.add(label.geometry().getRect())

    assert len(dialog_sizes) == 1, dialog_sizes
    assert len(label_geometries) == 1, label_geometries


@pytest.mark.parametrize("name", _NAMES)
def test_the_first_line_does_not_move_between_the_states_of_the_dots(
    qapp, dialog, name
):
    """The text does not move up or down when the dots add or remove a line.

    With two dots the text after the limit can wrap to one more line. A
    label that centres its text in the vertical direction then moves every
    line by half a line, and the text seems to scroll.
    """
    _check_the_first_line_stays_between_the_states_of_the_dots(qapp, dialog, name)


def _check_the_first_line_stays_between_the_states_of_the_dots(qapp, dialog, name):
    from PySide6.QtCore import QRect

    _show_first_then_after_limit(qapp, dialog, name)
    label = dialog._message_label
    contents = label.contentsRect()
    first_line = QRect(
        contents.x(), contents.y(), contents.width(), label.fontMetrics().height()
    )
    images = []
    for state in range(4):
        if state:
            dialog._animate_dots()
            qapp.processEvents()
        images.append(label.grab(first_line).toImage())

    for state, image in enumerate(images[1:], start=1):
        assert image == images[0], f"{name!r}: the first line moved at {state} dots"


def _text_height_at_label_width(label, text):
    """The height that `text` needs when it wraps at the label's width."""
    from PySide6.QtCore import QRect, Qt

    return label.fontMetrics().boundingRect(
        QRect(0, 0, label.contentsRect().width(), 100000),
        int(Qt.TextFlag.TextWordWrap),
        text,
    ).height()


@pytest.fixture
def boundary_name(qapp):
    """A display name whose text after the limit takes one more line with dots.

    For the shipped names, every state of the dots wraps to the same number
    of lines, so a dialog that measured only the text without dots, or that
    centred its text, would pass the tests above. This name makes the text
    with three dots one line taller than the text with no dot, at the scale
    the test runs on. On 2026-11-13 on Ikon, names of 12 to 14 characters
    did this at device pixel ratios 1.0, 1.25, 1.5, 1.75, 2.0, and 3.0.
    """
    from gui import WorkingDialog

    for length in range(1, 41):
        name = "N" * length
        probe = WorkingDialog()
        try:
            _show_first_then_after_limit(qapp, probe, name)
            label = probe._message_label
            base = _AFTER_LIMIT_TEXT.format(name=name)
            heights = {
                _text_height_at_label_width(label, base + "." * dots)
                for dots in range(4)
            }
        finally:
            probe._dot_timer.stop()
            probe.close()
            probe.deleteLater()
            qapp.processEvents()
        if len(heights) > 1:
            return name
    pytest.fail(
        "no display name of 1 to 40 characters changes the number of lines "
        "between the states of the dots at this scale"
    )


def test_a_name_at_a_line_boundary_fits_in_every_state_of_the_dots(
    qapp, dialog, boundary_name
):
    """The dialog measures the text with each number of dots, not only without dots."""
    _check_the_text_fits_in_every_state_of_the_dots(qapp, dialog, boundary_name)


def test_a_name_at_a_line_boundary_keeps_the_size_between_the_states_of_the_dots(
    qapp, dialog, boundary_name
):
    """The extra line of the dots does not make the dialog grow or shrink."""
    _check_the_size_stays_between_the_states_of_the_dots(qapp, dialog, boundary_name)


def test_a_name_at_a_line_boundary_keeps_the_first_line_in_place(
    qapp, dialog, boundary_name
):
    """The text does not move when the dots add a line, so it does not seem to scroll."""
    _check_the_first_line_stays_between_the_states_of_the_dots(
        qapp, dialog, boundary_name
    )


@pytest.mark.parametrize("name", _NAMES)
def test_the_plaque_keeps_its_height_after_the_text_change(qapp, dialog, name):
    """The text gets its room from a taller dialog, not from the plaque."""
    _show_first_then_after_limit(qapp, dialog, name)
    plaque = _plaque_label(dialog)
    assert plaque.height() >= plaque.pixmap().height()


_FIRST_TEXTS = ["Starting Wheelhouse...", "Asking...", "Thinking..."] + [
    f"Loading {name}" for name in _NAMES
]


@pytest.mark.parametrize("reused", [False, True], ids=["new", "reused"])
@pytest.mark.parametrize("text", _FIRST_TEXTS)
def test_the_first_text_keeps_its_present_size(qapp, dialog, text, reused):
    """A dialog that comes up with a text keeps the size it had before the fix.

    Before the fix (d0eddd6b), show_working set the text of a hidden dialog
    and called adjustSize, and the label kept the QLabel default alignment.
    The reference here does the same on a new dialog, and the dialog under
    test must have the same size. The reference sets the old alignment,
    because a label aligned to the top asks for 7 px more height on Ikon,
    and the fix gives the label a fixed height in its place. A reused
    dialog showed another text and was hidden first, as the app reuses one
    dialog for each job.
    """
    from PySide6.QtCore import Qt
    from gui import WorkingDialog

    if reused:
        dialog.show_working("Rewriting...", "ai:1")
        dialog._dot_timer.stop()
        qapp.processEvents()
        dialog.hide_working("ai:1")
        qapp.processEvents()
    dialog.show_working(text, "job:1")
    dialog._dot_timer.stop()
    qapp.processEvents()

    reference = WorkingDialog()
    try:
        reference._message_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        reference._message_label.setText(text)
        reference.adjustSize()
        expected = reference.size().toTuple()
    finally:
        reference.close()
        reference.deleteLater()
        qapp.processEvents()
    assert dialog.size().toTuple() == expected


@pytest.mark.parametrize("name", _NAMES)
def test_the_centre_stays_in_place_when_the_text_changes(qapp, dialog, name):
    """The dialog grows about its centre, so it stays where the user saw it."""
    first_geometry = _show_first_then_after_limit(qapp, dialog, name)
    centre_before = first_geometry.center()
    centre_after = dialog.geometry().center()
    assert abs(centre_after.x() - centre_before.x()) <= 1
    assert abs(centre_after.y() - centre_before.y()) <= 1
