"""Tests for the rewritten Pattern Manager help page (wh-pattern-editor-help).

Spec: docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 12.
Pins the section-12 requirements: the function reference is generated from
speech/action_catalog.py and is complete over every non-internal entry with
every internal entry excluded; basic entries render before advanced ones;
the old "edit patterns.toml by hand" advice is gone; the online regex
checker link appears exactly once and preselects the Python flavor; and the
table-of-contents anchors resolve to real anchor targets in the rendered
QTextBrowser document, with external links routed to the system browser
instead of navigating the QTextBrowser away from the page.
"""
from __future__ import annotations

import html
import re
from unittest.mock import patch

import pytest

from speech.action_catalog import ACTION_CATALOG
from pattern_help_dialog import (
    REGEX_CHECKER_URL,
    PatternHelpDialog,
    build_function_reference_html,
    build_help_html,
    link_is_external,
)

# Same constraint as test_pattern_manager_dialog.py (wh-pytest-flaky-segfault):
# the dialog tests below build real Qt widgets; without a QApplication Qt
# aborts the whole interpreter. The session-scoped qapp fixture guarantees
# one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")

NON_INTERNAL = [e for e in ACTION_CATALOG if e["audience"] != "internal"]
INTERNAL = [e for e in ACTION_CATALOG if e["audience"] == "internal"]


def _entry_marker(name: str) -> str:
    """The structural heading marker one reference entry renders for a
    function name. Distinct from incidental mentions of the name in other
    entries' prose (e.g. gs's example legitimately mentions
    capture_clipboard as plain escaped text, never as ``(<code>...</code>)``)."""
    return f"(<code>{html.escape(name)}</code>)"


# ---------------------------------------------------------------------------
# Generated function reference (pure, no Qt instantiation needed)
# ---------------------------------------------------------------------------


def test_reference_includes_every_non_internal_entry():
    ref = build_function_reference_html(ACTION_CATALOG)
    assert NON_INTERNAL, "catalog unexpectedly empty"
    for entry in NON_INTERNAL:
        assert _entry_marker(entry["name"]) in ref, entry["name"]
        assert html.escape(entry["label"]) in ref, entry["name"]
        assert html.escape(entry["summary"]) in ref, entry["name"]
        assert html.escape(entry["example"]) in ref, entry["name"]
        for param in entry["params"]:
            assert html.escape(param["summary"]) in ref, (
                entry["name"], param["name"],
            )


def test_reference_excludes_every_internal_entry():
    ref = build_function_reference_html(ACTION_CATALOG)
    assert INTERNAL, "catalog lost its internal entries"
    for entry in INTERNAL:
        assert _entry_marker(entry["name"]) not in ref, entry["name"]
        assert html.escape(entry["label"]) not in ref, entry["name"]


def test_basic_entries_render_before_advanced_ones():
    ref = build_function_reference_html(ACTION_CATALOG)
    basic = [
        ref.index(_entry_marker(e["name"]))
        for e in NON_INTERNAL
        if e["audience"] == "basic"
    ]
    advanced = [
        ref.index(_entry_marker(e["name"]))
        for e in NON_INTERNAL
        if e["audience"] == "advanced"
    ]
    assert basic and advanced
    assert max(basic) < min(advanced)


# ---------------------------------------------------------------------------
# Full help page content
# ---------------------------------------------------------------------------


def test_patterns_toml_advice_is_gone():
    assert "patterns.toml" not in build_help_html()


def test_help_page_embeds_the_full_reference():
    page = build_help_html()
    for entry in NON_INTERNAL:
        assert _entry_marker(entry["name"]) in page, entry["name"]


def test_regex_checker_is_the_only_external_link_and_names_python_flavor():
    page = build_help_html()
    hrefs = re.findall(r'href="([^"]+)"', page)
    external = [h for h in hrefs if not h.startswith("#")]
    assert external == [REGEX_CHECKER_URL]
    assert "regex101.com" in REGEX_CHECKER_URL
    assert "flavor=python" in REGEX_CHECKER_URL
    assert page.count(REGEX_CHECKER_URL) == 1


def test_toc_anchor_links_have_matching_targets():
    page = build_help_html()
    fragments = [h[1:] for h in re.findall(r'href="(#[^"]+)"', page)]
    # The page is long now; a real table of contents exists.
    assert len(fragments) >= 5
    for frag in fragments:
        assert f'name="{frag}"' in page, frag


# ---------------------------------------------------------------------------
# Link routing decision (pure function)
# ---------------------------------------------------------------------------


def test_link_routing_classification():
    assert link_is_external(REGEX_CHECKER_URL)
    assert link_is_external("http://example.com/page")
    assert not link_is_external("#wake-word")
    assert not link_is_external("")


# ---------------------------------------------------------------------------
# Dialog behavior (real Qt widgets; qapp fixture)
# ---------------------------------------------------------------------------


def test_dialog_browser_owns_link_navigation():
    # openLinks must be off: with it on, QTextBrowser would try to *load*
    # an http URL as a document on click and blank the help page.
    dlg = PatternHelpDialog(parent=None)
    assert dlg._browser.openLinks() is False
    assert dlg._browser.openExternalLinks() is False


def test_dialog_routes_external_links_to_system_browser():
    from PySide6.QtCore import QUrl

    dlg = PatternHelpDialog(parent=None)
    with patch("pattern_help_dialog.QDesktopServices.openUrl") as open_url:
        dlg._on_anchor_clicked(QUrl(REGEX_CHECKER_URL))
    open_url.assert_called_once()
    assert open_url.call_args[0][0].toString() == REGEX_CHECKER_URL


def test_dialog_keeps_internal_anchors_internal():
    from PySide6.QtCore import QUrl

    dlg = PatternHelpDialog(parent=None)
    scrolled = []
    # Shadow the C++ slot with a recorder; PySide6 allows instance
    # attribute assignment over bound methods.
    dlg._browser.scrollToAnchor = scrolled.append
    with patch("pattern_help_dialog.QDesktopServices.openUrl") as open_url:
        dlg._on_anchor_clicked(QUrl("#function-reference"))
    open_url.assert_not_called()
    assert scrolled == ["function-reference"]


def test_rendered_document_carries_every_toc_anchor():
    # Qt drops anchor names that wrap zero characters; this walks the real
    # rendered document and proves every TOC fragment survived HTML
    # parsing, i.e. scrollToAnchor has something to scroll to.
    dlg = PatternHelpDialog(parent=None)
    doc = dlg._browser.document()
    names = set()
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                names.update(fragment.charFormat().anchorNames() or [])
            it += 1
        block = block.next()
    page = build_help_html()
    fragments = {h[1:] for h in re.findall(r'href="(#[^"]+)"', page)}
    assert fragments, "no internal TOC links found"
    missing = fragments - names
    assert not missing, f"anchors lost in Qt rendering: {sorted(missing)}"
