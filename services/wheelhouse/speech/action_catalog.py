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

Params describe what patterns.toml actually passes (ground truth checked
against the shipped file), not the Python signature: e.g. ``hk`` takes a
flat variadic list where a trailing number is peeled off as a repeat
count, and ``gs`` accepts the name of an earlier step (capture_clipboard)
whose stored return value becomes the query.
"""

# Allowed values, exported for consumers (the editor's field generator and
# validators). Kept in sync with spec section 5.
AUDIENCES = ("basic", "advanced", "internal")
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
            '"x-ray notepad" focuses the Notepad window.'
        ),
        "audience": "basic",
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
            'Trigger "^click\\s+(.+)$" with params ["g1"]: saying "click '
            'submit button" clicks the button labeled Submit.'
        ),
        "audience": "advanced",
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
            'Trigger "^apply numbers$" with no params: saying "apply '
            "numbers\" shows the badges; then \"click 4\" clicks control "
            "number 4."
        ),
        "audience": "advanced",
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
            'Trigger "^dismiss numbers$" with no params: saying "dismiss '
            'numbers" hides the badges.'
        ),
        "audience": "advanced",
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
            'saying "x-ray search" Googles the selected text.'
        ),
        "audience": "advanced",
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
    },
    {
        "name": "wheelhouse_help",
        "label": "Open help chat",
        "summary": (
            "Opens the local WheelHouse help chat window, optionally "
            "submitting a spoken question immediately."
        ),
        "params": [
            {
                "name": "question",
                "summary": (
                    "Optional question to submit as the window opens; "
                    "usually the capture group g1."
                ),
                "kind": "text",
            },
        ],
        "example": (
            'Trigger "^wheelhouse help (.+)$" with params ["g1"]: opens '
            "the help window and asks the spoken question."
        ),
        "audience": "advanced",
    },
    {
        "name": "wheelhouse_help_online",
        "label": "Open online help",
        "summary": (
            "Opens the configured online help page in the default browser."
        ),
        "params": [],
        "example": (
            'Trigger "^wheelhouse help online$" with no params: saying '
            '"x-ray wheelhouse help online" opens the help page.'
        ),
        "audience": "advanced",
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
            'Trigger "^boost$": select a word, say "x-ray boost", and '
            "the word is copied and sent to the speech engine as a hint."
        ),
        "audience": "internal",
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
    },
)

# Name -> entry index for O(1) lookup by consumers (picker, explainer,
# help generator). Built once at import; entries are shared, not copied.
CATALOG_BY_NAME = {entry["name"]: entry for entry in ACTION_CATALOG}
