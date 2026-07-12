"""Pattern Manager help dialog (wh-pattern-editor-help).

The page content is built by pure module-level functions so tests can pin
it without a QApplication: ``build_function_reference_html`` renders the
function reference straight from speech/action_catalog.py (basic entries
first, then advanced; internal-audience entries never appear), and
``build_help_html`` assembles the full page -- table of contents with
internal anchor links, the concept sections, and that generated reference.
Because the reference is generated, it cannot drift from what the editor's
function picker offers; both read the same catalog.

Link handling: QTextBrowser's default openLinks behavior would try to
*load* an http URL as a document on click (blanking the page), so the
dialog takes over routing entirely -- ``setOpenLinks(False)`` plus an
``anchorClicked`` handler that sends external URLs to the system browser
via QDesktopServices and scrolls internal ``#fragment`` anchors itself.
The decision is the pure function ``link_is_external``.
"""

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QPushButton

from speech.action_catalog import ACTION_CATALOG

# The online regular-expression checker the advanced editor links to; the
# query string preselects the Python flavor, which is the engine WheelHouse
# actually matches patterns with.
REGEX_CHECKER_URL = "https://regex101.com/?flavor=python"


def link_is_external(url: str) -> bool:
    """True when a clicked link should open in the system browser, False
    when it is an internal page anchor (``#section``) to scroll to."""
    return url.lower().startswith(("http:", "https:"))


def _render_entry(entry) -> str:
    """One catalog entry -> HTML: label heading, summary, parameter list
    with per-parameter summaries (and choices where fixed), and the worked
    example. All catalog text is escaped."""
    name = html.escape(entry["name"])
    label = html.escape(entry["label"])
    summary = html.escape(entry["summary"])
    parts = [f"<p><b>{label}</b> (<code>{name}</code>)<br/>{summary}</p>"]
    params = entry.get("params") or []
    if params:
        items = []
        for param in params:
            line = (
                f"<li><code>{html.escape(param['name'])}</code> &#8212; "
                f"{html.escape(param['summary'])}"
            )
            choices = param.get("choices")
            if choices:
                line += (
                    " One of: "
                    f"{html.escape(', '.join(str(c) for c in choices))}."
                )
            items.append(line + "</li>")
        parts.append("<ul>" + "".join(items) + "</ul>")
    parts.append(f"<p><i>Example:</i> {html.escape(entry['example'])}</p>")
    return "".join(parts)


def build_function_reference_html(catalog=ACTION_CATALOG) -> str:
    """Render the function reference from the action catalog.

    Basic-audience entries first, then advanced, each under its own
    heading; internal-audience entries are excluded entirely (spec
    section 12). Pure: no Qt, no I/O.
    """
    groups = (
        (
            "basic-actions",
            "Basic actions",
            "The four action types the simple editor offers.",
        ),
        (
            "advanced-actions",
            "Advanced actions",
            "Everything else, available as steps in advanced mode.",
        ),
    )
    parts = []
    for audience, heading, blurb in groups:
        anchor = audience  # "basic-actions" / "advanced-actions"
        title = heading
        parts.append(f'<h3><a name="{anchor}">{title}</a></h3>')
        parts.append(f"<p>{blurb}</p>")
        audience_key = "basic" if audience == "basic-actions" else "advanced"
        for entry in catalog:
            if entry["audience"] == audience_key:
                parts.append(_render_entry(entry))
    return "".join(parts)


# (anchor, table-of-contents title) for every section, in page order. The
# function reference's sub-groups get their own TOC lines because the page
# is long and they are the part users come back to.
_TOC = (
    ("what-are-patterns", "What are patterns?"),
    ("commands-vs-replacements", "Commands vs replacements"),
    ("wake-word", "The wake word"),
    ("editor", "What the editor can do"),
    ("advanced-mode", "Advanced mode"),
    ("function-reference", "Function reference"),
    ("basic-actions", "&nbsp;&nbsp;&#8226; Basic actions"),
    ("advanced-actions", "&nbsp;&nbsp;&#8226; Advanced actions"),
)


def build_help_html() -> str:
    """Assemble the full help page: TOC, concept sections, generated
    function reference. Pure: no Qt, no I/O."""
    toc_links = "<br/>".join(
        f'<a href="#{anchor}">{title}</a>' for anchor, title in _TOC
    )
    # Qt drops anchor names that wrap zero characters, so every anchor
    # wraps its heading text (verified by test_pattern_help.py against the
    # rendered QTextDocument).
    return f"""
    <h2>Pattern Manager Help</h2>
    <p>{toc_links}</p>

    <h3><a name="what-are-patterns">What are patterns?</a></h3>
    <p>Patterns map voice triggers to actions. When you say a trigger
    phrase, WheelHouse performs the associated action &#8212; pressing
    keys, inserting text, launching programs, switching windows, and
    more.</p>

    <h3><a name="commands-vs-replacements">Commands vs replacements</a></h3>
    <p><b>Commands</b> are matched against your whole utterance: the
    trigger has to account for everything you said, not just how it
    starts. Saying "save" runs the save command; saying "save the file
    please" does not (unless the pattern captures extra words on
    purpose). Many command triggers do capture trailing words or a
    number &#8212; "undo 3" &#8212; and pass them to the action.</p>
    <p><b>Replacements</b> apply while you are dictating: when the
    trigger appears anywhere in what you say, WheelHouse types the
    replacement text instead of the matched words. Saying "period"
    mid-sentence types "." &#8212; the rest of the sentence is typed
    unchanged.</p>

    <h3><a name="wake-word">The wake word</a></h3>
    <p>The wake word (for example "x-ray") is a safety prefix that
    prevents accidental triggers. When a command requires the wake word,
    you must say it immediately before the trigger phrase. The current
    wake word is shown at the top of the Pattern Manager window, where
    you can also change it.</p>
    <p>Require the wake word for:</p>
    <ul>
        <li>Destructive commands (close window, cut)</li>
        <li>Triggers that could come up in normal speech (save,
        desktop)</li>
    </ul>

    <h3><a name="editor">What the editor can do</a></h3>
    <ul>
        <li><b>Create</b> new patterns from a short list of starting
        points: press a hotkey, insert text, run a program, switch to a
        window.</li>
        <li><b>Edit</b> any pattern you created.</li>
        <li><b>Duplicate</b> any pattern as a starting point for your
        own.</li>
        <li><b>Customize</b> a built-in: saving creates your own copy
        that replaces the built-in. <b>Remove customization</b> deletes
        your copy and the built-in comes back.</li>
        <li><b>Several phrasings</b> per pattern: give one pattern
        multiple spoken phrases ("editor", "code editor", "vs code") and
        any of them triggers it.</li>
        <li><b>Try it</b>: as you edit, type what you would say and see
        which pattern would respond &#8212; before you save.</li>
        <li><b>Advanced mode</b>: write the trigger as a regular
        expression directly and chain multiple action steps.</li>
    </ul>

    <h3><a name="advanced-mode">Advanced mode</a></h3>
    <p>Advanced mode trades guardrails for power. You edit the trigger
    as a raw regular expression, and the action becomes an ordered list
    of steps that run top to bottom, drawn from the full function list
    below. Capture groups in the expression (g1, g2, ...) carry the
    words you spoke into the steps' parameters.</p>
    <p>An honest warning: regular expressions are easy to get slightly
    wrong. The editor checks that your expression compiles and shows how
    many capture groups it has, but a compiling expression can still
    match the wrong things &#8212; or nothing. Test it at
    <a href="{REGEX_CHECKER_URL}">regex101.com (Python flavor)</a>, the
    same checker the editor links to; WheelHouse matches patterns with
    Python's regular-expression engine, and the link preselects that
    flavor.</p>

    <h3><a name="function-reference">Function reference</a></h3>
    <p>Generated from the same catalog the editor's function picker
    uses, so it always matches what the editor offers.</p>
    {build_function_reference_html(ACTION_CATALOG)}
    """


class PatternHelpDialog(QDialog):
    """Help dialog explaining voice command patterns."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pattern Manager Help")
        self.setMinimumSize(640, 520)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self._browser = QTextBrowser()
        # Own the routing: with openLinks on, QTextBrowser would setSource
        # an http link and blank the page; with it off, anchorClicked
        # still fires and _on_anchor_clicked decides.
        self._browser.setOpenLinks(False)
        self._browser.setOpenExternalLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        self._browser.setHtml(build_help_html())
        layout.addWidget(self._browser)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def _on_anchor_clicked(self, url):
        """Route a clicked link: external URLs to the system browser,
        internal ``#fragment`` anchors to a scroll within the page."""
        if link_is_external(url.toString()):
            QDesktopServices.openUrl(url)
        else:
            self._browser.scrollToAnchor(url.fragment())
