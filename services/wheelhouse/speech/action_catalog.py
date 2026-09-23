"""Curated catalog of every registered speech action function.

This is the data source the Pattern Manager editor uses for its function
picker (grouped by audience, internal hidden), its generated parameter
fields, inline description text, hover help, the Help reference page, and
the pattern explainer (wh-pattern-editor-catalog; spec:
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 5).

Hard constraints:

- Dependency-free: stdlib only, no side effects. Both the Logic and GUI
  processes import this module, so it must never pull the Logic import
  graph (a test runs it in a bare subprocess with all non-stdlib imports
  blocked to enforce this).
- No drift: every function registered in ``ActionFunctions`` in
  speech/actions.py has exactly one entry here, and every entry names a
  registered function. tests/test_action_catalog.py walks the real
  registry both directions; adding or renaming a registration without
  updating this file fails the suite.

Entry shape (all fields required):

- ``name``     -- registry key exactly as patterns.toml calls it.
- ``label``    -- short display name for the picker.
- ``summary``  -- one plain-English sentence for inline/hover help.
- ``params``   -- ordered sequence of {name, summary, kind} dicts, one per
  parameter in the order patterns.toml passes them. ``kind`` is one of
  PARAM_KINDS; a ``choice`` param additionally carries ``choices``.
- ``example``  -- one worked example (trigger + params + what happens).
- ``audience`` -- "basic" (the four simple-mode action types), "advanced"
  (everything else user-meaningful), or "internal" (never shown in the
  picker). The internal set is FIXED by the spec at exactly four names:
  skip_clipboard_restore, capture_clipboard, add_hint_to_stt,
  set_speech_interaction_mode. Moving an entry between basic and advanced
  is a one-line audience change; do not touch the internal set.
- ``group``    -- one name from ACTION_GROUPS: the sub-heading the picker
  lists this entry under (wh-action-picker-ordering). Every entry carries
  one, including basic and internal entries, so an audience change stays
  a one-line edit. ``picker_sections`` below turns audience + group into
  the order the editor shows.

Params describe what patterns.toml actually passes (ground truth checked
against the shipped file), not the Python signature: e.g. ``hk`` takes a
flat variadic list where a trailing number is peeled off as a repeat
count, and ``gs`` accepts the name of an earlier step (capture_clipboard)
whose stored return value becomes the query.
"""

# Allowed values, exported for consumers (the editor's field generator and
# validators). Kept in sync with spec section 5.
AUDIENCES = ("basic", "advanced", "internal")

# Every group name an entry may declare (wh-action-picker-ordering). The
# picker shows these as sub-headings under "Advanced actions", sorted A-Z,
# and sorts the entries inside each one A-Z by label -- an order a user can
# read off the list. The tuple is the spelling authority: a new group must
# be added here first, so a typo cannot invent a near-duplicate heading.
# "Clipboard" holds internal-only entries today and therefore never renders.
ACTION_GROUPS = (
    "AI",
    "Clicking",
    "Clipboard",
    "Date and pauses",
    "Keyboard",
    "Mouse grid",
    "Programs and web",
    "Scrolling",
    "Text",
    "Wheelhouse",
)

# The actions that hand a value to the steps after them. The command
# engine stores a step's string return under the function name, and under
# the step's optional ``result`` name as well (speech/command_engine.py
# lines 342-347). The editor offers a result-name field for exactly these
# entries (wh-editor-step-result-name); every other action returns nothing
# to name. capture_clipboard is listed because the engine treats it the
# same way, though its internal audience keeps it out of the picker.
RESULT_PRODUCING_ACTIONS = frozenset({
    "ask_ai",
    "capture_clipboard",
    "date",
    "run_capture",
})
PARAM_KINDS = (
    "text",
    "key",
    "keys",
    "path",
    "exe_or_title",
    "number",
    "group_ref",
    "choice",
)

# The four directions utils/win_input_sender.scroll_wheel accepts
# (wh-voice-access-parity.2.3). Listed here rather than imported for the same
# dependency-freeness reason as the transformation names below.
_SCROLL_CHOICES = ("up", "down", "left", "right")

# The 15 transformation names ui/selection_transformer.py accepts
# (apply_transformation's dispatch chain). Listed here rather than imported
# because importing the UI module would break dependency-freeness.
_TRANSFORM_CHOICES = (
    "quote",
    "single_quote",
    "bracket",
    "parenthesis",
    "angle_bracket",
    "curly_bracket",
    "uppercase",
    "lowercase",
    "capitalize",
    "title_case",
    "snake_case",
    "camel_case",
    "pascal_case",
    "kebab_case",
    "compress",
)

ACTION_CATALOG = (
    # ------------------------------------------------------------------
    # basic -- the four simple-mode action types
    # ------------------------------------------------------------------
    {
        "name": "hk",
        "label": "Press a hotkey",
        "summary": (
            "Presses several keys together as one combination, optionally "
            "repeated a number of times."
        ),
        "params": [
            {
                "name": "keys",
                "summary": (
                    "Key names held down together, listed in order (for "
                    "example ctrl, z)."
                ),
                "kind": "keys",
            },
            {
                "name": "repeat",
                "summary": (
                    "Optional last value: how many times to press the "
                    "combination (capped at 50); often a capture group "
                    "like g1 so the spoken number is used."
                ),
                "kind": "number",
            },
        ],
        "example": (
            'Trigger "^undo\\s*(\\d+)?$" with params ["ctrl", "z", "g1"]: '
            'saying "undo 3" presses Ctrl+Z three times.'
        ),
        "audience": "basic",
        "group": "Keyboard",
    },
    {
        "name": "scroll",
        "label": "Scroll the mouse wheel",
        "summary": (
            "Turns the mouse wheel a number of notches, up, down, left or "
            "right, without moving or pressing the mouse."
        ),
        "params": [
            {
                "name": "direction",
                "summary": "Which way to turn the wheel.",
                "kind": "choice",
                "choices": list(_SCROLL_CHOICES),
            },
            {
                "name": "clicks",
                "summary": (
                    "Optional last value: how many wheel notches to send "
                    "(capped at 50); often a capture group like g1 so the "
                    "spoken number is used."
                ),
                "kind": "number",
            },
        ],
        "example": (
            'Trigger "^scroll down\\s*(\\d+)?$" with params ["down", "g1"]: '
            'saying "scroll down 3" turns the wheel three notches down.'
        ),
        "audience": "advanced",
        "group": "Scrolling",
    },
    {
        "name": "start_continuous_scroll",
        "label": "Start scrolling and keep going",
        "summary": (
            "Starts turning the mouse wheel over and over, in one direction, "
            "until a stop command arrives or the time limit is reached."
        ),
        "params": [
            {
                "name": "direction",
                "summary": "Which way to keep turning the wheel.",
                "kind": "choice",
                "choices": list(_SCROLL_CHOICES),
            },
        ],
        "example": (
            'Trigger "^start scrolling down$" with params ["down"]: saying '
            '"start scrolling down" scrolls down until you say "stop '
            'scrolling".'
        ),
        "audience": "advanced",
        "group": "Scrolling",
    },
    {
        "name": "stop_continuous_scroll",
        "label": "Stop the scrolling",
        "summary": (
            "Stops a scroll that was started with the continuous scroll "
            "command. It takes no direction and does nothing when no scroll "
            "is running."
        ),
        "params": [],
        "example": (
            'Trigger "^stop scrolling$" with no params: saying "stop '
            'scrolling" ends the scroll that is running.'
        ),
        "audience": "advanced",
        "group": "Scrolling",
    },
    {
        "name": "insert_text",
        "label": "Insert text",
        "summary": (
            "Inserts text at the cursor with intelligent spacing and "
            "capitalization."
        ),
        "params": [
            {
                "name": "text",
                "summary": (
                    "The text to insert; may be a capture group like g1 to "
                    "insert the spoken words."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "literal (.+)$" with params ["g1"]: saying '
            '"literal submit" inserts the word "submit" as text instead '
            "of firing the submit command."
        ),
        "audience": "basic",
        "group": "Text",
    },
    {
        "name": "run",
        "label": "Run a program",
        "summary": "Launches a program or command line on the computer.",
        "params": [
            {
                "name": "command",
                "summary": (
                    "Program path or command line to run, including any "
                    "arguments (for example explorer.exe ms-settings:)."
                ),
                "kind": "path",
            },
        ],
        "example": (
            'Trigger "^Windows? settings$" with params '
            '["explorer.exe ms-settings:"]: saying "windows settings" '
            "opens the Windows Settings app."
        ),
        "audience": "basic",
        "group": "Programs and web",
    },
    {
        "name": "activate",
        "label": "Switch to a window",
        "summary": (
            "Brings a window to the front, found by program name (a target "
            "ending in .exe) or by window-title pattern; the reserved "
            "target default_browser resolves to your default browser."
        ),
        "params": [
            {
                "name": "target",
                "summary": (
                    "Program executable name (notepad.exe), a window-title "
                    "pattern, or the reserved word default_browser."
                ),
                "kind": "exe_or_title",
            },
        ],
        "example": (
            'Trigger "^notepad$" with params ["notepad.exe"]: saying '
            '"notepad", and nothing else in that utterance, focuses the '
            "Notepad window."
        ),
        "audience": "basic",
        "group": "Programs and web",
    },
    # ------------------------------------------------------------------
    # advanced -- everything else user-meaningful
    # ------------------------------------------------------------------
    {
        "name": "press",
        "label": "Press one key",
        "summary": (
            "Presses a single key, optionally repeated a number of times."
        ),
        "params": [
            {
                "name": "key",
                "summary": "The key to press (for example del or backspace).",
                "kind": "key",
            },
            {
                "name": "repeat",
                "summary": (
                    "Optional repeat count as digits or a number word "
                    "(capped at 50); often a capture group like g1."
                ),
                "kind": "number",
            },
        ],
        "example": (
            'Trigger "^delete\\s*(\\d+)?$" with params ["del", "g1"]: '
            'saying "delete 3" presses the Delete key three times.'
        ),
        "audience": "advanced",
        "group": "Keyboard",
    },
    {
        "name": "press_keys",
        "label": "Press a spoken key sequence",
        "summary": (
            "Turns spoken key names into a key combination and presses it; "
            "if any name is unrecognized the whole phrase falls through to "
            "dictation."
        ),
        "params": [
            {
                "name": "key_sequence",
                "summary": (
                    "Space-separated spoken key names in any order (for "
                    "example control alt delete); usually the capture "
                    "group g1."
                ),
                "kind": "keys",
            },
        ],
        "example": (
            'Trigger "^press\\s*(.+)$" with params ["g1"]: saying '
            '"press control alt delete" presses Ctrl+Alt+Delete.'
        ),
        "audience": "advanced",
        "group": "Keyboard",
    },
    {
        "name": "literal",
        "label": "Type captured words literally",
        "summary": (
            "Types the captured words exactly as spoken, bypassing all "
            "pattern processing and smart spacing."
        ),
        "params": [
            {
                "name": "text",
                "summary": (
                    "The words to type, usually the capture group g1."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'Trigger "^say (.+)$" with params ["g1"]: saying "say hello" '
            'types "hello" with no command matching or cleanup.'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "type_text",
        "label": "Type text exactly",
        "summary": (
            "Types text character for character with no smart spacing or "
            "capitalization."
        ),
        "params": [
            {
                "name": "text",
                "summary": (
                    "The exact text to type; may be a capture group like g1."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^find\\s*(.*)$" presses Ctrl+F then calls type_text '
            'with params ["g1"]: saying "x-ray find hello" opens Find and '
            'types "hello".'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "insert_raw",
        "label": "Insert raw text",
        "summary": (
            "Inserts exact text at the cursor via paste, with no added "
            "space, capitalization, or cleanup."
        ),
        "params": [
            {
                "name": "text",
                "summary": (
                    "The exact characters to insert; may be a capture "
                    "group like g1."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^insert\\s*(.+)$" with params ["g1"]: saying '
            '"insert TODO:" inserts "TODO:" exactly, no leading space.'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "select_phrase",
        "label": "Select a spoken phrase",
        "summary": (
            "Finds the first match of the spoken words in the focused text "
            "control and selects it. The match is exact apart from letter "
            "case."
        ),
        "params": [
            {
                "name": "phrase",
                "summary": (
                    "The words to find; may be a capture group like g1."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^select (.+)$" with params ["g1"]: saying '
            '"x-ray select brown fox" selects the first "brown fox" in the '
            "document. This one needs the hotword first."
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "insert_newlines",
        "label": "Insert blank lines",
        "summary": (
            "Inserts the given number of newline characters (up to 50)."
        ),
        "params": [
            {
                "name": "count",
                "summary": (
                    "How many newlines to insert, as digits or a number "
                    "word; capped at 50."
                ),
                "kind": "number",
            },
        ],
        "example": (
            'Trigger "^blank lines (\\d+)$" with params ["g1"]: saying '
            '"blank lines 3" inserts three newlines.'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "transform_selection",
        "label": "Transform selected text",
        "summary": (
            "Changes the currently selected text: case conversion (snake "
            "case, title case, and so on) or wrapping in quotes or "
            "brackets."
        ),
        "params": [
            {
                "name": "transformation",
                "summary": "Which transformation to apply to the selection.",
                "kind": "choice",
                "choices": list(_TRANSFORM_CHOICES),
            },
        ],
        "example": (
            'Trigger "^snake case$" with params ["snake_case"]: with '
            '"hello world" selected, saying "snake case" replaces it '
            'with "hello_world".'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "text",
        "label": "Replace matched words with text",
        "summary": (
            "The standard replacement action: inserts the given text with "
            "intelligent spacing, and an empty value silently discards the "
            "matched words."
        ),
        "params": [
            {
                "name": "template",
                "summary": (
                    "Replacement text; may reference capture groups; an "
                    "empty string swallows the match (used to filter "
                    "phrases like \"okay Google\")."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "\\bperiod\\b" with params ["."]: saying "period" '
            'during dictation inserts "." instead of the word.'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "number_point",
        "label": "Numbered list item",
        "summary": (
            "Converts a spoken number to the digit followed by a period, "
            "for numbered lists."
        ),
        "params": [
            {
                "name": "number",
                "summary": (
                    "The number as a word (one) or digits (1); usually the "
                    "capture group g1."
                ),
                "kind": "number",
            },
        ],
        "example": (
            'Trigger "^item (\\d+)$" with params ["g1"]: saying "item 5" '
            'inserts "5.".'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "wrap_or_insert",
        "label": "Wrap in delimiters",
        "summary": (
            "Wraps the selection or the captured words in the given "
            "delimiters, or inserts an empty delimiter pair with the "
            "cursor between when there is nothing to wrap."
        ),
        "params": [
            {
                "name": "left_fence",
                "summary": 'Opening delimiter (for example ( or ").',
                "kind": "text",
            },
            {
                "name": "right_fence",
                "summary": 'Closing delimiter (for example ) or ").',
                "kind": "text",
            },
            {
                "name": "text",
                "summary": (
                    "Capture group holding the words to wrap (usually g1); "
                    "may be empty."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'Trigger "\\bparentheses(.*)$" with params ["(", ")", "g1"]: '
            'saying "parentheses hello" inserts "(hello)"; saying just '
            '"parentheses" wraps the selection or inserts "()".'
        ),
        "audience": "advanced",
        "group": "Text",
    },
    {
        "name": "cursor_navigate",
        "label": "Move the cursor by voice",
        "summary": (
            'Parses a spoken navigation phrase like "go right two words" '
            "and executes it as keystrokes; unrecognized phrases fall "
            "through to dictation."
        ),
        "params": [
            {
                "name": "utterance",
                "summary": (
                    'The navigation phrase, normally the template "go g1" '
                    'so the words spoken after "go" are parsed.'
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^go (.+)" with params ["go g1"]: saying "go home '
            'then grab to end" moves to line start then selects to the '
            "end of the line."
        ),
        "audience": "advanced",
        "group": "Keyboard",
    },
    {
        "name": "click_element",
        "label": "Click a control by name",
        "summary": (
            "Finds a control in the focused window by its spoken name "
            "(optionally with a role word like button) and clicks it."
        ),
        "params": [
            {
                "name": "target",
                "summary": (
                    "Capture group holding the spoken control name, "
                    "usually g1."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'Trigger "^(?:click|tap)\\s+(.+)$" with params ["g1"]: saying '
            '"x-ray click submit button" clicks the button labeled Submit. '
            "This one needs the hotword first."
        ),
        "audience": "advanced",
        "group": "Clicking",
    },
    {
        "name": "show_overlay_command",
        "label": "Show numbered click overlay",
        "summary": (
            "Paints a number badge on every clickable control on screen "
            'so you can say "click" plus a number.'
        ),
        "params": [],
        "example": (
            'Trigger "^(?:show|apply) numbers$" with no params: saying '
            "\"show numbers\" shows the badges; then \"x-ray click 4\" "
            "clicks control number 4. \"apply numbers\" does the same."
        ),
        "audience": "advanced",
        "group": "Clicking",
    },
    {
        "name": "hide_overlay_command",
        "label": "Hide numbered click overlay",
        "summary": (
            "Removes the numbered badges painted by the show-numbers "
            "command."
        ),
        "params": [],
        "example": (
            'Trigger "^(?:hide|dismiss) numbers$" with no params: saying '
            '"hide numbers" hides the badges. "dismiss numbers" does the '
            'same.'
        ),
        "audience": "advanced",
        "group": "Clicking",
    },
    {
        "name": "click_element_command",
        "label": "Right- or double-click a control by name",
        "summary": (
            "Parses a full gesture click command (right click X, double "
            "click X) and clicks the named control with that mouse "
            "gesture."
        ),
        "params": [
            {
                "name": "utterance",
                "summary": (
                    "Capture group holding the whole spoken command "
                    "including the gesture words, usually g1."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'Trigger "^((?:right|double)[\\s-]+click\\s+.+)$" with params '
            '["g1"]: saying "right click recycle bin" opens the context '
            "menu of the Recycle Bin icon."
        ),
        "audience": "advanced",
        "group": "Clicking",
    },
    {
        "name": "grid_show_command",
        "label": "Show the mouse grid",
        "summary": (
            "Opens the 3x3 mouse grid over the focused monitor; spoken "
            "numbers then narrow it to a point you can click, drag, or "
            "move to."
        ),
        "params": [],
        "example": (
            'Trigger "^(?:show|apply) grid$" with no params: saying '
            '"show grid" or "apply grid" paints the grid; "5" then "click" clicks the '
            "center."
        ),
        "audience": "advanced",
        "group": "Mouse grid",
    },
    {
        "name": "grid_dismiss_command",
        "label": "Dismiss the mouse grid",
        "summary": "Closes the mouse grid without clicking anything.",
        "params": [],
        "example": (
            'Trigger "^(?:hide|dismiss) grid$" with no params: saying '
            '"hide grid" removes the grid. "dismiss grid" does the same.'
        ),
        "audience": "advanced",
        "group": "Mouse grid",
    },
    {
        "name": "grid_next_screen_command",
        "label": "Move the mouse grid to the next monitor",
        "summary": (
            "Moves the open mouse grid to the next monitor, resetting it "
            "to cover that whole screen."
        ),
        "params": [],
        "example": (
            'Trigger "^grid next screen$" with no params: saying "grid '
            'next screen" jumps the grid to the other monitor.'
        ),
        "audience": "advanced",
        "group": "Mouse grid",
    },
    {
        "name": "grid_action_command",
        "label": "Mouse-grid action word",
        "summary": (
            "Runs one grid-only action word (mark, drag, or move_here) at "
            "the grid's current cell; with the grid closed the word types "
            "as normal dictation."
        ),
        "params": [
            {
                "name": "action",
                "summary": (
                    "Fixed action name: mark, drag, or move_here."
                ),
                "kind": "text",
            },
            {
                "name": "utterance",
                "summary": (
                    "Capture group holding the spoken word for the "
                    "dictation fallback, usually g1."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'Trigger "^(mark[.!?]?)$" with params ["mark", "g1"]: with '
            'the grid open, saying "mark" pins the drag start point; '
            'with it closed, "mark" just types the word.'
        ),
        "audience": "advanced",
        "group": "Mouse grid",
    },
    {
        "name": "grid_number_command",
        "label": "Mouse-grid number",
        "summary": (
            "Narrows the open mouse grid to the spoken cell (1-9); with "
            "the grid closed the number types as normal dictation."
        ),
        "params": [
            {
                "name": "utterance",
                "summary": (
                    "Capture group holding the whole spoken text for the "
                    "dictation fallback, usually g1."
                ),
                "kind": "group_ref",
            },
            {
                "name": "number_word",
                "summary": (
                    "Capture group holding just the number word or digit, "
                    "usually g2."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'Saying "five" with the grid open zooms the grid into cell '
            '5; with the grid closed it types "five".'
        ),
        "audience": "advanced",
        "group": "Mouse grid",
    },
    {
        "name": "grid_click_command",
        "label": "Mouse-grid bare click",
        "summary": (
            "Clicks at the open mouse grid's current cell center (click, "
            "right click, or double click); with the grid closed the "
            "words type as normal dictation."
        ),
        "params": [
            {
                "name": "utterance",
                "summary": (
                    "Capture group holding the spoken gesture words, "
                    "usually g1."
                ),
                "kind": "group_ref",
            },
        ],
        "example": (
            'With the grid narrowed to the target, saying "right click" '
            "opens the context menu at the cell center."
        ),
        "audience": "advanced",
        "group": "Mouse grid",
    },
    {
        "name": "open_url",
        "label": "Open a web address",
        "summary": (
            "Opens a web address in the default browser. Spoken words and "
            "earlier step results substitute into the address URL-encoded, "
            "so the address itself must start with http:// or https:// -- "
            "anything else fails the step and stops the pattern. The site "
            "part of the address (everything from http:// or https:// up "
            "to the first slash, question mark, or number sign) must be "
            "written out in the pattern: a capture group or result name "
            "there fails the step, and so does a capture group that did "
            "not match any spoken words. The address must use the normal "
            "form -- http:// or https:// followed directly by the site "
            "name, with no extra slashes and no control characters."
        ),
        "params": [
            {
                "name": "url_template",
                "summary": (
                    "The full web address, starting with http:// or "
                    "https://. Capture groups (g1) or an earlier step's "
                    "result name embedded in it are replaced with their "
                    "text, URL-encoded so the spoken words arrive as data."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^jira (.+)$" with params '
            '["https://mycompany.atlassian.net/issues/?jql=text~%22g1%22"] '
            "searches Jira for the spoken words."
        ),
        "audience": "advanced",
        "group": "Programs and web",
    },
    {
        "name": "run_capture",
        "label": "Run a program and capture its text",
        "summary": (
            "Runs a program without a shell and stores the text it prints for "
            "a later step. An optional leading, unquoted TOML number sets the "
            "timeout in seconds and is clamped from 0.1 to 60; failures, "
            "including output over the configured cap, stop the pattern."
        ),
        "params": [
            {
                "name": "timeout",
                "summary": (
                    "Optional leading timeout in seconds. It must be an "
                    "unquoted TOML number and is clamped from 0.1 to 60 "
                    "seconds. A quoted numeric string is passed to the "
                    "program as an argument."
                ),
                "kind": "number",
            },
            {
                "name": "program",
                "summary": (
                    "Program path to run. It is run directly, without a "
                    "shell."
                ),
                "kind": "path",
            },
            {
                "name": "argument",
                "summary": (
                    "Repeatable program argument. Add one separate field for "
                    "each argument; no shell parsing is performed."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^look up (.+)$" with params '
            '[0.5, "C:\\\\Tools\\\\lookup.exe", "g1"] and result "answer", then '
            'use params ["answer"] in a following insert_text step.'
        ),
        "audience": "advanced",
        "group": "Programs and web",
    },
    {
        "name": "gs",
        "label": "Google search",
        "summary": (
            "Opens a Google search for the given query in the default "
            "browser."
        ),
        "params": [
            {
                "name": "query",
                "summary": (
                    "Search text; may be a capture group, or the name of "
                    "an earlier step whose stored result to search for "
                    "(for example capture_clipboard)."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^search$" copies the selection, captures the '
            'clipboard, then calls gs with params ["capture_clipboard"]: '
            'saying "search" by itself Googles the selected text.'
        ),
        "audience": "advanced",
        "group": "Programs and web",
    },
    {
        "name": "date",
        "label": "Format the current date",
        "summary": (
            "Formats the current date and time and stores the result "
            "under the name date for a later step to insert."
        ),
        "params": [
            {
                "name": "format",
                "summary": (
                    "Python strftime format string (for example %Y-%m-%d); "
                    "defaults to the ISO date."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Steps [{date, params ["%Y-%m-%d"]}, {insert_text, params '
            '["date"]}]: inserts today\'s date, like 2026-07-09.'
        ),
        "audience": "advanced",
        "group": "Date and pauses",
    },
    {
        "name": "sleep",
        "label": "Pause between steps",
        "summary": (
            "Waits the given number of seconds before the next action "
            "step runs."
        ),
        "params": [
            {
                "name": "seconds",
                "summary": (
                    "How long to wait, in seconds; fractions like 0.5 are "
                    "allowed."
                ),
                "kind": "number",
            },
        ],
        "example": (
            'Steps [{run, params ["notepad.exe"]}, {sleep, params '
            '["1.5"]}, {type_text, params ["hello"]}]: waits 1.5 seconds '
            "for Notepad to open before typing."
        ),
        "audience": "advanced",
        "group": "Date and pauses",
    },
    {
        "name": "fix_text_ai",
        "label": "Fix text with AI",
        "summary": (
            "Captures the text in the focused field, sends it to the "
            "configured AI for correction, and pastes the corrected "
            "version back."
        ),
        "params": [],
        "example": (
            'Trigger "^fix" with no params: saying "x-ray fix" corrects '
            "the text in the focused field."
        ),
        "audience": "advanced",
        "group": "AI",
    },
    {
        "name": "rewrite_text_ai",
        "label": "Rewrite text with AI",
        "summary": (
            "Captures the text in the focused field, sends it to the "
            "configured AI to be rewritten in the style you describe, and "
            "pastes the rewritten version back. You write the style "
            "sentence and nothing else: Wheelhouse adds the wording that "
            "keeps the layout intact and the wording that stops the "
            "highlighted text from redirecting the AI."
        ),
        "params": [
            {
                "name": "instruction",
                "summary": (
                    "One sentence describing the style, addressed to the "
                    "AI, ending with \"Return only the rewritten text.\" "
                    "For example: \"Rewrite this text in plain language. "
                    "Keep every fact. Return only the rewritten text.\""
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^simplify$" with params ["Rewrite this text in plain '
            'language. Keep every fact. Return only the rewritten text."]: '
            'saying "simplify" by itself rewrites the highlighted text '
            "in plain language."
        ),
        "audience": "advanced",
        "group": "AI",
    },
    {
        "name": "ask_ai",
        "label": "Ask AI",
        "summary": (
            "Sends one prompt to the configured AI and stores its reply for "
            "a later step. Capture groups and earlier step results substitute "
            "into the prompt; failures stop the pattern."
        ),
        "params": [
            {
                "name": "prompt",
                "summary": (
                    "Question or instruction for the AI. It may include a "
                    "capture group or an earlier step's result name."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^ask (.+)$" with params ["Answer in one short '
            'sentence, no preamble: g1"] and result "answer", then use '
            'params ["answer"] in a following insert_text step.'
        ),
        "audience": "advanced",
        "group": "AI",
    },
    {
        "name": "cancel_fix",
        "label": "Cancel AI fix",
        "summary": (
            "Cancels an in-progress AI text correction before it pastes "
            "anything back."
        ),
        "params": [],
        "example": (
            'Trigger "^cancel fix$" with no params: saying "x-ray cancel '
            'fix" stops the running correction.'
        ),
        "audience": "advanced",
        "group": "AI",
    },
    # The in-app help chat action (wheelhouse_help) is deliberately absent.
    # Its registration in actions.py is commented out while the help chat is
    # disabled, so offering it here would let the Pattern Manager build a
    # pattern that names a function the router cannot call.
    {
        "name": "wheelhouse_help_online",
        "label": "Open online help",
        "summary": (
            "Opens the configured online help page in the default browser."
        ),
        "params": [],
        "example": (
            'Trigger "^help$" with no params: saying "x-ray help" opens '
            "the help page."
        ),
        "audience": "advanced",
        "group": "Wheelhouse",
    },
    {
        "name": "open_pattern_manager",
        "label": "Open the Pattern Manager",
        "summary": (
            "Opens the Pattern Manager window for viewing and editing "
            "voice patterns."
        ),
        "params": [],
        "example": (
            'Trigger "^patterns?$" with no params: saying "x-ray '
            'patterns" opens the Pattern Manager.'
        ),
        "audience": "advanced",
        "group": "Wheelhouse",
    },
    {
        "name": "open_calibration",
        "label": "Teach WheelHouse your voice",
        "summary": (
            "Opens the voice-teaching window, where WheelHouse learns "
            "how you sound so it stops missing short words. Only the "
            "Distil-Whisper speech engine uses it."
        ),
        "params": [],
        "example": (
            'Trigger "^learn my voice$" with no params: saying "learn '
            'my voice" opens the voice-teaching window.'
        ),
        "audience": "advanced",
        "group": "Wheelhouse",
    },
    # ------------------------------------------------------------------
    # internal -- never shown in the picker (set fixed by spec section 5)
    # ------------------------------------------------------------------
    {
        "name": "skip_clipboard_restore",
        "label": "Skip clipboard restore",
        "summary": (
            "Marks this utterance so the original clipboard content is "
            "not restored afterward; used as the first step of copy and "
            "cut commands."
        ),
        "params": [],
        "example": (
            'First step of "^copy$": [skip_clipboard_restore, then hk '
            "ctrl+c] so the copied text stays on the clipboard."
        ),
        "audience": "internal",
        "group": "Clipboard",
    },
    {
        "name": "capture_clipboard",
        "label": "Capture clipboard",
        "summary": (
            "Reads the current clipboard text and stores it under the "
            "name capture_clipboard for a later step to use."
        ),
        "params": [],
        "example": (
            'Trigger "^search$" copies the selection, calls '
            "capture_clipboard, then gs uses the stored value as the "
            "search query."
        ),
        "audience": "internal",
        "group": "Clipboard",
    },
    {
        "name": "add_hint_to_stt",
        "label": "Teach word to speech engine",
        "summary": (
            "Sends the clipboard text to the speech-recognition server "
            "as a vocabulary hint so it is transcribed correctly."
        ),
        "params": [],
        "example": (
            'Trigger "^boost$": select a word, say "boost" by itself, '
            "and the word is copied and sent to the speech engine as a hint."
        ),
        "audience": "internal",
        "group": "Wheelhouse",
    },
    {
        "name": "set_speech_interaction_mode",
        "label": "Set speech interaction mode",
        "summary": (
            "Switches how the microphone is engaged: click to talk "
            "(toggle) or push to talk."
        ),
        "params": [
            {
                "name": "mode",
                "summary": "The interaction mode to switch to.",
                "kind": "choice",
                "choices": ["toggle", "push_to_talk"],
            },
        ],
        "example": (
            'Trigger "^push to talk mode$" with params ["push_to_talk"]: '
            "saying it switches the microphone to push-to-talk."
        ),
        "audience": "internal",
        "group": "Wheelhouse",
    },
    {
        "name": "stop_listening",
        "label": "Stop listening",
        "summary": (
            "Switches listening off, as clicking the floating button does "
            "while it listens. In toggle mode, say the wake word to switch "
            "it back on; in push-to-talk mode the wake word ends only the "
            "idle pause, so hold the floating button again."
        ),
        "params": [],
        "example": (
            'Trigger "^stop listening$": saying it by itself switches '
            "listening off."
        ),
        "audience": "advanced",
        "group": "Wheelhouse",
    },
)

# Name -> entry index for O(1) lookup by consumers (picker, explainer,
# help generator). Built once at import; entries are shared, not copied.
CATALOG_BY_NAME = {entry["name"]: entry for entry in ACTION_CATALOG}


def _by_label(entry: dict) -> str:
    return entry["label"].casefold()


def picker_sections(audience: str) -> tuple:
    """Entries of one audience, in the order the editor's picker shows.

    Returns a tuple of ``(group, entries)`` sections. The ordering rule is
    the one a user can read off the list (wh-action-picker-ordering):

    * "advanced" -- one section per group, groups sorted A-Z by name,
      entries sorted A-Z by label inside each group.
    * "basic" -- ONE section with a group of ``None``, entries sorted A-Z
      by label. The basic tier holds four entries that spread over three
      groups, so sub-headings there would outnumber the entries they
      introduce; a plain A-Z list is the same rule with nothing to hide.
    * "internal" -- empty, because internal entries never reach a picker.

    Sorting keys use ``casefold`` so the order matches how the labels read
    on screen. Entries are the shared catalog dicts, never copies, and an
    entry never moves between audiences here: the ``audience`` field alone
    decides which heading lists it.
    """
    if audience == "internal":
        return ()
    entries = [
        entry for entry in ACTION_CATALOG if entry["audience"] == audience
    ]
    if not entries:
        return ()
    if audience == "basic":
        return ((None, tuple(sorted(entries, key=_by_label))),)
    groups = sorted({entry["group"] for entry in entries}, key=str.casefold)
    return tuple(
        (
            group,
            tuple(sorted(
                (entry for entry in entries if entry["group"] == group),
                key=_by_label,
            )),
        )
        for group in groups
    )
