"""Open and close a scratch Notepad or Word window, for the probe scripts.

MEASURED 2026-08-19, and it corrects an earlier wrong claim in these
probes. An earlier comment said that ``notepad.exe`` attaches to the
running Notepad and does not open a second window. On this machine every
launch opened a NEW window: one session of probe runs left 35 Notepad
windows open, and David had to report it.

The earlier belief came from one real event. On 2026-08-19 a feasibility
script launched ``notepad.exe`` and typed into a document David was
already using. So a launch CAN land on an existing document. Which of the
two happens was not established, and this module does not depend on the
answer. It records the window handles that exist before the launch, and
treats any handle that appears afterwards as its own.

The caller must close what it opens. ``close_window`` answers the save
prompt. Notepad shows that prompt as a Popup INSIDE the Notepad window.
Word shows it as a separate dialog window. ``close_window`` looks in both
places.

WORD DOCUMENTS. ``write_docx`` builds the smallest valid .docx file that
Word will open: three parts in a zip container. The probes use it rather
than typing into Word, because the key path drops characters (see
notepad_typing_trials.py) and a document built on disk is exact.
"""

from __future__ import annotations

import os
import subprocess
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import uiautomation as auto
import win32con
import win32gui

# Top-level window class names.
NOTEPAD_WINDOW_CLASS = "Notepad"
WORD_WINDOW_CLASS = "OpusApp"

# Document control class names, for a caller that checks what it got.
NOTEPAD_DOCUMENT_CLASS = "RichEditD2DPT"
WORD_DOCUMENT_CLASS = "_WwG"

# Seconds to wait for a new window to appear and take focus. Word starts
# far more slowly than Notepad.
NOTEPAD_SETTLE_SECONDS = 2.0
WORD_SETTLE_SECONDS = 12.0

# How many times to look for the save prompt before giving up.
PROMPT_ATTEMPTS = 20

# Every label a save prompt may use for the discard button.
DISCARD_LABELS = ("Don't save", "Don't Save", "Do&n't Save")

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCUMENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{paragraphs}</w:body>
</w:document>"""


def write_docx(path: Path, paragraphs: list[str]) -> Path:
    """Write the smallest valid .docx that Word will open."""
    body = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t>"
        f"</w:r></w:p>"
        for text in paragraphs
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr(
            "word/document.xml",
            DOCUMENT_TEMPLATE.format(paragraphs=body),
        )
    return path


def list_handles(window_class: str) -> set[int]:
    """Every top-level window of this class open right now.

    A window can close while this walk runs, and the class-name read then
    raises a COM error. Skip that window rather than fail the whole walk.
    """
    handles = set()
    root = auto.GetRootControl()
    for child in root.GetChildren():
        try:
            if child.ClassName == window_class:
                handles.add(child.NativeWindowHandle)
        except Exception:
            continue
    return handles


def _wait_for_new_window(
    window_class: str, before: set[int], settle_seconds: float
) -> bool:
    """Wait until a window of this class appears. True when one did.

    Word replaces its own window handle during startup, so the caller
    must not keep the first handle it sees. It keeps ``before`` instead
    and closes every handle that is not in it.
    """
    deadline = time.time() + settle_seconds
    while time.time() < deadline:
        if list_handles(window_class) - before:
            # Give the window time to finish drawing and take focus.
            time.sleep(2.0)
            return True
        time.sleep(0.4)
    return False


def open_notepad(before: set[int]) -> bool:
    """Launch Notepad. True when a new window appeared.

    False means the launch attached to a window that was already open.
    The caller must then treat the focused document as somebody else's
    and refuse to type.
    """
    subprocess.Popen(["notepad.exe"])
    return _wait_for_new_window(
        NOTEPAD_WINDOW_CLASS, before, NOTEPAD_SETTLE_SECONDS
    )


def open_word(document: Path, before: set[int]) -> bool:
    """Open one .docx file in Word. True when a new window appeared."""
    os.startfile(str(document))
    return _wait_for_new_window(
        WORD_WINDOW_CLASS, before, WORD_SETTLE_SECONDS
    )


def close_new_windows(window_class: str, before: set[int]) -> tuple[int, int]:
    """Close every window of this class that was not open before.

    Returns the number closed and the number that refused to close.
    """
    closed = 0
    refused = 0
    for handle in sorted(list_handles(window_class) - before):
        if close_window(handle):
            closed += 1
        else:
            refused += 1
    return closed, refused


def _click_discard(handle: int) -> bool:
    """Click the discard button of a save prompt, wherever it is."""
    window = auto.ControlFromHandle(handle)
    if window:
        for label in DISCARD_LABELS:
            button = window.ButtonControl(Name=label, searchDepth=8)
            if button.Exists(0, 0):
                button.Click(waitTime=0)
                return True
    root = auto.GetRootControl()
    for child in root.GetChildren():
        for label in DISCARD_LABELS:
            button = child.ButtonControl(Name=label, searchDepth=6)
            if button.Exists(0, 0):
                button.Click(waitTime=0)
                return True
    return False


def _ask_to_close(handle: int) -> None:
    """Ask the window to close, by both mechanisms.

    Notepad closes on a posted WM_CLOSE. Word ignores it, and closes only
    through the UI Automation window pattern. Measured 2026-08-19.
    """
    window = auto.ControlFromHandle(handle)
    if window:
        try:
            pattern = window.GetPattern(auto.PatternId.WindowPattern)
            if pattern:
                pattern.Close()
                return
        except Exception:
            pass
    win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)


def close_window(handle: int) -> bool:
    """Close one window, discarding unsaved changes."""
    if not win32gui.IsWindow(handle):
        return True
    _ask_to_close(handle)
    time.sleep(0.6)
    for _ in range(PROMPT_ATTEMPTS):
        if not win32gui.IsWindow(handle):
            return True
        if _click_discard(handle):
            time.sleep(0.8)
        else:
            time.sleep(0.4)
    return not win32gui.IsWindow(handle)
