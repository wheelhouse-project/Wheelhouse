# Wheelhouse Action, Notice, and Configuration Reference

This is the automatically generated reference for every action a pattern step can perform, for the notices and windows that Wheelhouse shows, and for every user-facing setting that the shipped config.toml contains. Optional settings that the file leaves commented out are described in the help document instead. It is built from the same sources the application uses, so it stays in step with what Wheelhouse actually does. Every voice command is listed in the Voice Commands section of the help document (wheelhouse_help.md), which is also the guided introduction to using Wheelhouse; for installation, see the installation guide (wheelhouse_install.md).

## Action Reference

Every action a pattern step can perform, generated from the action catalog that the Pattern Manager's action list and its help page read. Each entry gives the label the Pattern Manager shows, the action name that a pattern file uses, what the action does, its parameters in the order a pattern passes them, and a worked example. The word in parentheses after a parameter is its kind, which sets the field the Pattern Manager's editor shows for it: choice is a fixed list, group_ref is a list of the pattern's capture groups (g1, g2, and so on) that also accepts typed text, and every other kind is a text field.

### AI

#### Ask AI

Action name: `ask_ai`. The Pattern Manager lists it under Advanced actions, AI.

Sends one prompt to the configured AI and stores its reply for a later step. Capture groups and earlier step results substitute into the prompt; failures stop the pattern.

Parameters, in the order a pattern passes them:

- `prompt` (text) -- Question or instruction for the AI. It may include a capture group or an earlier step's result name.

It stores the text it produces for the steps after it: a later step's parameter that is exactly `ask_ai`, or exactly the result name set on this step, receives that text.

Example: `Trigger "^ask (.+)$" with params ["Answer in one short sentence, no preamble: g1"] and result "answer", then use params ["answer"] in a following insert_text step.`

#### Cancel AI fix

Action name: `cancel_fix`. The Pattern Manager lists it under Advanced actions, AI.

Cancels an in-progress AI text correction before it pastes anything back.

Parameters: none.

Example: `Trigger "^cancel fix$" with no params: saying "cancel fix" stops the running correction.`

#### Fix text with AI

Action name: `fix_text_ai`. The Pattern Manager lists it under Advanced actions, AI.

Captures the selected text, or all the text in the focused field when nothing is selected, sends it to the configured AI for correction, and pastes the corrected version back.

Parameters: none.

Example: `Trigger "^fix" with no params: saying "x-ray fix" corrects the selected text, or all the text in the focused field when nothing is selected.`

#### Rewrite text with AI

Action name: `rewrite_text_ai`. The Pattern Manager lists it under Advanced actions, AI.

Captures the selected text, or all the text in the focused field when nothing is selected, sends it to the configured AI to be rewritten in the style you describe, and pastes the rewritten version back. You write the style sentence and nothing else: Wheelhouse adds the wording that keeps the layout intact and the wording that stops the highlighted text from redirecting the AI.

Parameters, in the order a pattern passes them:

- `instruction` (text) -- One sentence describing the style, addressed to the AI, ending with "Return only the rewritten text." For example: "Rewrite this text in plain language. Keep every fact. Return only the rewritten text."

Example: `Trigger "^simplify$" with params ["Rewrite this text in plain language. Keep every fact. Return only the rewritten text."]: saying "simplify" by itself rewrites the highlighted text in plain language.`

### Clicking

#### Click a control by name

Action name: `click_element`. The Pattern Manager lists it under Advanced actions, Clicking.

Finds a control in the focused window by its spoken name (optionally with a role word like button) and clicks it.

Parameters, in the order a pattern passes them:

- `target` (group_ref) -- Capture group holding the spoken control name, usually g1.

Example: `Trigger "^(?:click|clicks|tap)\s+(.+)$" with params ["g1"]: saying "click submit button" clicks the button labeled Submit. This one needs no safety word.`

#### Hide numbered click overlay

Action name: `hide_overlay_command`. The Pattern Manager lists it under Advanced actions, Clicking.

Removes the numbered badges painted by the show-numbers command.

Parameters: none.

Example: `Trigger "^(?:hide|dismiss) numbers$" with no params: saying "hide numbers" hides the badges. "dismiss numbers" does the same.`

#### Right- or double-click a control by name

Action name: `click_element_command`. The Pattern Manager lists it under Advanced actions, Clicking.

Parses a full gesture click command (right click X, double click X) and clicks the named control with that mouse gesture.

Parameters, in the order a pattern passes them:

- `utterance` (group_ref) -- Capture group holding the whole spoken command including the gesture words, usually g1.

Example: `Trigger "^((?:right|double)[\s-]+click\s+.+)$" with params ["g1"]: saying "right click recycle bin" opens the context menu of the Recycle Bin icon.`

#### Show numbered click overlay

Action name: `show_overlay_command`. The Pattern Manager lists it under Advanced actions, Clicking.

Paints a number badge on every clickable control on screen so you can say "click" plus a number.

Parameters: none.

Example: `Trigger "^(?:show numbers|apply numbers|show numbers here)$" with no params: saying "show numbers" shows the badges; then "click 4" clicks control number 4. "apply numbers" and "show numbers here" do the same.`

### Clipboard

#### Capture clipboard

Action name: `capture_clipboard`. The Pattern Manager's action list does not offer this action.

Reads the current clipboard text and stores it under the name capture_clipboard for a later step to use.

Parameters: none.

It stores the text it produces for the steps after it: a later step's parameter that is exactly `capture_clipboard`, or exactly the result name set on this step, receives that text.

Example: `Trigger "^search$" copies the selection, calls capture_clipboard, then gs uses the stored value as the search query.`

#### Skip clipboard restore

Action name: `skip_clipboard_restore`. The Pattern Manager's action list does not offer this action.

Marks this utterance so the original clipboard content is not restored afterward; used as the first step of copy and cut commands.

Parameters: none.

Example: `First step of "^copy$": [skip_clipboard_restore, then hk ctrl+c] so the copied text stays on the clipboard.`

### Date and pauses

#### Format the current date

Action name: `date`. The Pattern Manager lists it under Advanced actions, Date and pauses.

Formats the current date and time and stores the result under the name date for a later step to insert.

Parameters, in the order a pattern passes them:

- `format` (text) -- Python strftime format string (for example %Y-%m-%d); defaults to the ISO date.

It stores the text it produces for the steps after it: a later step's parameter that is exactly `date`, or exactly the result name set on this step, receives that text.

Example: `Steps [{date, params ["%Y-%m-%d"]}, {insert_text, params ["date"]}]: inserts today's date, like 2026-07-09.`

#### Pause between steps

Action name: `sleep`. The Pattern Manager lists it under Advanced actions, Date and pauses.

Waits the given number of seconds before the next action step runs.

Parameters, in the order a pattern passes them:

- `seconds` (number) -- How long to wait, in seconds; fractions like 0.5 are allowed.

Example: `Steps [{run, params ["notepad.exe"]}, {sleep, params ["1.5"]}, {type_text, params ["hello"]}]: waits 1.5 seconds for Notepad to open before typing.`

### Keyboard

#### Move the cursor by voice

Action name: `cursor_navigate`. The Pattern Manager lists it under Advanced actions, Keyboard.

Parses a spoken navigation phrase like "go right two words" and executes it as keystrokes; unrecognized phrases fall through to dictation.

Parameters, in the order a pattern passes them:

- `utterance` (text) -- The navigation phrase, normally the template "go g1" so the words spoken after "go" are parsed.

Example: `Trigger "^go (.+)" with params ["go g1"]: saying "go home then grab to end" moves to line start then selects to the end of the line.`

#### Press a hotkey

Action name: `hk`. The Pattern Manager lists it under Basic actions.

Presses several keys together as one combination, optionally repeated a number of times.

Parameters, in the order a pattern passes them:

- `keys` (keys) -- Key names held down together, listed in order (for example ctrl, z).
- `repeat` (number) -- Optional last value: how many times to press the combination (capped at 30); often a capture group like g1 so the spoken number is used.

Example: `Trigger "^undo\s*(\d+)?$" with params ["ctrl", "z", "g1"]: saying "undo 3" presses Ctrl+Z three times.`

#### Press a spoken key sequence

Action name: `press_keys`. The Pattern Manager lists it under Advanced actions, Keyboard.

Turns spoken key names into a key combination and presses it; if any name is unrecognized the whole phrase falls through to dictation.

Parameters, in the order a pattern passes them:

- `key_sequence` (keys) -- Space-separated spoken key names in any order (for example control alt delete); usually the capture group g1.
- `repeat` (number) -- Optional repeat count as digits or a number word (capped at 30); the capture group g2 in "press tab 3 times".

Example: `Trigger "^press\s*(.+?)(?:\s+(\d+)\s+times?)?$" with params ["g1", "g2"]: saying "press control alt delete" presses Ctrl+Alt+Delete, and "press tab 3 times" presses Tab three times.`

#### Press one key

Action name: `press`. The Pattern Manager lists it under Advanced actions, Keyboard.

Presses a single key, optionally repeated a number of times.

Parameters, in the order a pattern passes them:

- `key` (key) -- The key to press (for example del or backspace).
- `repeat` (number) -- Optional repeat count as digits or a number word (capped at 50); often a capture group like g1.

Example: `Trigger "^delete\s*(\d+)?$" with params ["del", "g1"]: saying "delete 3" presses the Delete key three times.`

### Mouse grid

#### Dismiss the mouse grid

Action name: `grid_dismiss_command`. The Pattern Manager lists it under Advanced actions, Mouse grid.

Closes the mouse grid without clicking anything.

Parameters: none.

Example: `Trigger "^(?:hide|dismiss) grid$" with no params: saying "hide grid" removes the grid. "dismiss grid" does the same.`

#### Mouse-grid action word

Action name: `grid_action_command`. The Pattern Manager lists it under Advanced actions, Mouse grid.

Runs one grid-only action word (mark, drag, or move_here) at the grid's current cell; with the grid closed the word types as normal dictation.

Parameters, in the order a pattern passes them:

- `action` (text) -- Fixed action name: mark, drag, or move_here.
- `utterance` (group_ref) -- Capture group holding the spoken word for the dictation fallback, usually g1.

Example: `Trigger "^(mark[.!?]?)$" with params ["mark", "g1"]: with the grid open, saying "mark" pins the drag start point; with it closed, "mark" just types the word.`

#### Mouse-grid bare click

Action name: `grid_click_command`. The Pattern Manager lists it under Advanced actions, Mouse grid.

Clicks at the open mouse grid's current cell center (click, tap, right click, double click, or triple click); with the grid closed it clicks at the current pointer position. The words type as dictation only when voice clicking is turned off; say "literal click" to type the word.

Parameters, in the order a pattern passes them:

- `utterance` (group_ref) -- Capture group holding the spoken gesture words, usually g1.

Example: `With the grid narrowed to the target, saying "right click" opens the context menu at the cell center; with no grid open, saying "triple click" selects the line under the pointer.`

#### Mouse-grid number

Action name: `grid_number_command`. The Pattern Manager lists it under Advanced actions, Mouse grid.

Narrows the open mouse grid to the spoken cell (1-9). With the grid closed and the numbered overlay showing, the number clicks the control with that label; with neither showing, the number types as normal dictation.

Parameters, in the order a pattern passes them:

- `utterance` (group_ref) -- Capture group holding the whole spoken text for the dictation fallback, usually g1.
- `number_word` (group_ref) -- Capture group holding just the number word or digit, usually g2.

Example: `Saying "five" with the grid open zooms the grid into cell 5; with the grid closed and the numbered overlay showing it clicks the control labeled 5; with neither showing it types "five".`

#### Move the mouse grid to the next monitor

Action name: `grid_next_screen_command`. The Pattern Manager lists it under Advanced actions, Mouse grid.

Moves the open mouse grid to the next monitor, resetting it to cover that whole screen.

Parameters: none.

Example: `Trigger "^grid next screen$" with no params: saying "grid next screen" jumps the grid to the other monitor.`

#### Show the mouse grid

Action name: `grid_show_command`. The Pattern Manager lists it under Advanced actions, Mouse grid.

Opens the 3x3 mouse grid over the focused monitor; spoken numbers then narrow it to a point you can click, drag, or move to.

Parameters: none.

Example: `Trigger "^(?:show|apply) grid$" with no params: saying "show grid" or "apply grid" paints the grid; "5" then "click" clicks the center.`

### Programs and web

#### Google search

Action name: `gs`. The Pattern Manager lists it under Advanced actions, Programs and web.

Opens a Google search for the given query in the default browser.

Parameters, in the order a pattern passes them:

- `query` (text) -- Search text; may be a capture group, or the name of an earlier step whose stored result to search for (for example capture_clipboard).

Example: `Trigger "^search$" copies the selection, captures the clipboard, then calls gs with params ["capture_clipboard"]: saying "search" by itself Googles the selected text.`

#### Open a web address

Action name: `open_url`. The Pattern Manager lists it under Advanced actions, Programs and web.

Opens a web address in the default browser. Spoken words and earlier step results substitute into the address URL-encoded, so the address itself must start with http:// or https:// -- anything else fails the step and stops the pattern. The site part of the address (everything from http:// or https:// up to the first slash, question mark, or number sign) must be written out in the pattern: a capture group or result name there fails the step, and so does a capture group that did not match any spoken words. The address must use the normal form -- http:// or https:// followed directly by the site name, with no extra slashes and no control characters.

Parameters, in the order a pattern passes them:

- `url_template` (text) -- The full web address, starting with http:// or https://. Capture groups (g1) or an earlier step's result name embedded in it are replaced with their text, URL-encoded so the spoken words arrive as data.

Example: `Trigger "^jira (.+)$" with params ["https://mycompany.atlassian.net/issues/?jql=text~%22g1%22"] searches Jira for the spoken words.`

#### Run a program

Action name: `run`. The Pattern Manager lists it under Basic actions.

Launches a program or command line on the computer.

Parameters, in the order a pattern passes them:

- `command` (path) -- Program path or command line to run, including any arguments (for example explorer.exe ms-settings:).

Example: `Trigger "^Windows? settings$" with params ["explorer.exe ms-settings:"]: saying "windows settings" opens the Windows Settings app.`

#### Run a program and capture its text

Action name: `run_capture`. The Pattern Manager lists it under Advanced actions, Programs and web.

Runs a program without a shell and stores the text it prints for a later step. An optional leading, unquoted TOML number sets the timeout in seconds and is clamped from 0.1 to 60; failures, including output over the configured cap, stop the pattern.

Parameters, in the order a pattern passes them:

- `timeout` (number) -- Optional leading timeout in seconds. It must be an unquoted TOML number and is clamped from 0.1 to 60 seconds. A quoted number is not a timeout; in the leading position it is used as the program path.
- `program` (path) -- Program path to run. It is run directly, without a shell.
- `argument` (text) -- Repeatable program argument. Add one separate field for each argument; no shell parsing is performed.

It stores the text it produces for the steps after it: a later step's parameter that is exactly `run_capture`, or exactly the result name set on this step, receives that text.

Example: `Trigger "^look up (.+)$" with params [0.5, "C:\\Tools\\lookup.exe", "g1"] and result "answer", then use params ["answer"] in a following insert_text step.`

#### Switch to a window

Action name: `activate`. The Pattern Manager lists it under Basic actions.

Brings a window to the front, found by program name (a target ending in .exe) or by the spoken name matching whole words in the window title; the reserved target default_browser resolves to your default browser.

Parameters, in the order a pattern passes them:

- `target` (exe_or_title) -- Program executable name (notepad.exe), the words to find in a window title (whole words, plain text), or the reserved word default_browser.

Example: `Trigger "^notepad$" with params ["notepad.exe"]: saying "notepad", and nothing else in that utterance, focuses the Notepad window.`

### Scrolling

#### Scroll the mouse wheel

Action name: `scroll`. The Pattern Manager lists it under Advanced actions, Scrolling.

Turns the mouse wheel a number of notches, up, down, left or right, without moving or pressing the mouse.

Parameters, in the order a pattern passes them:

- `direction` (choice: `up`, `down`, `left`, `right`) -- Which way to turn the wheel.
- `clicks` (number) -- Optional last value: how many wheel notches to send (capped at 50); often a capture group like g1 so the spoken number is used.

Example: `Trigger "^scroll down\s*(\d+)?$" with params ["down", "g1"]: saying "scroll down 3" turns the wheel three notches down.`

#### Start scrolling and keep going

Action name: `start_continuous_scroll`. The Pattern Manager lists it under Advanced actions, Scrolling.

Starts turning the mouse wheel over and over, in one direction, until a stop command arrives or the time limit is reached.

Parameters, in the order a pattern passes them:

- `direction` (choice: `up`, `down`, `left`, `right`) -- Which way to keep turning the wheel.

Example: `Trigger "^start scrolling down$" with params ["down"]: saying "start scrolling down" scrolls down until you say "stop scrolling".`

#### Stop the scrolling

Action name: `stop_continuous_scroll`. The Pattern Manager lists it under Advanced actions, Scrolling.

Stops a scroll that was started with the continuous scroll command. It takes no direction and does nothing when no scroll is running.

Parameters: none.

Example: `Trigger "^stop scrolling$" with no params: saying "stop scrolling" ends the scroll that is running.`

### Text

#### Insert blank lines

Action name: `insert_newlines`. The Pattern Manager lists it under Advanced actions, Text.

Inserts the given number of newline characters (up to 50).

Parameters, in the order a pattern passes them:

- `count` (number) -- How many newlines to insert, as digits or a number word; capped at 50.

Example: `Trigger "^blank lines (\d+)$" with params ["g1"]: saying "blank lines 3" inserts three newlines.`

#### Insert raw text

Action name: `insert_raw`. The Pattern Manager lists it under Advanced actions, Text.

Inserts exact text at the cursor via paste, with no added space, capitalization, or cleanup.

Parameters, in the order a pattern passes them:

- `text` (text) -- The exact characters to insert; may be a capture group like g1.

Example: `Trigger "^insert\s*(.+)$" with params ["g1"]: saying "insert TODO:" inserts "TODO:" exactly, no leading space.`

#### Insert text

Action name: `insert_text`. The Pattern Manager lists it under Basic actions.

Inserts text at the cursor with intelligent spacing and capitalization.

Parameters, in the order a pattern passes them:

- `text` (text) -- The text to insert; may be a capture group like g1 to insert the spoken words.

Example: `Trigger "literal (.+)$" with params ["g1"]: saying "literal submit" inserts the word "submit" as text instead of firing the submit command.`

#### Insert text with no spaces

Action name: `insert_raw_no_spaces`. The Pattern Manager lists it under Advanced actions, Text.

Inserts text at the cursor via paste with every space removed, and no added space, capitalization, or cleanup.

Parameters, in the order a pattern passes them:

- `text` (text) -- The words to join; may be a capture group like g1.

Example: `Trigger "^no space (.+)$" with params ["g1"]: saying "no space hello world" inserts "helloworld".`

#### Numbered list item

Action name: `number_point`. The Pattern Manager lists it under Advanced actions, Text.

Converts a spoken number to the digit followed by a period, for numbered lists.

Parameters, in the order a pattern passes them:

- `number` (number) -- The number as a word (one) or digits (1); usually the capture group g1.

Example: `Trigger "^item (\d+)$" with params ["g1"]: saying "item 5" inserts "5.".`

#### Replace matched words with text

Action name: `text`. The Pattern Manager lists it under Advanced actions, Text.

The standard replacement action: inserts the given text with intelligent spacing, and an empty value silently discards the matched words.

Parameters, in the order a pattern passes them:

- `template` (text) -- Replacement text; may reference capture groups; an empty string swallows the match (used to filter phrases like "okay Google").

Example: `Trigger "\bperiod\b" with params ["."]: saying "period" during dictation inserts "." instead of the word.`

#### Select a spoken phrase

Action name: `select_phrase`. The Pattern Manager lists it under Advanced actions, Text.

Finds the first match of the spoken words in the focused text control and selects it. The match is exact apart from letter case.

Parameters, in the order a pattern passes them:

- `phrase` (text) -- The words to find; may be a capture group like g1.

Example: `Trigger "^select (.+)$" with params ["g1"]: saying "x-ray select brown fox" selects the first "brown fox" in the document. This one needs the safety word first.`

#### Transform selected text

Action name: `transform_selection`. The Pattern Manager lists it under Advanced actions, Text.

Changes the currently selected text: case conversion (snake case, title case, and so on) or wrapping in quotes or brackets.

Parameters, in the order a pattern passes them:

- `transformation` (choice: `quote`, `single_quote`, `bracket`, `parenthesis`, `angle_bracket`, `curly_bracket`, `uppercase`, `lowercase`, `capitalize`, `title_case`, `snake_case`, `camel_case`, `pascal_case`, `kebab_case`, `compress`) -- Which transformation to apply to the selection.

Example: `Trigger "^snake case$" with params ["snake_case"]: with "hello world" selected, saying "snake case" replaces it with "hello_world".`

#### Type captured words literally

Action name: `literal`. The Pattern Manager lists it under Advanced actions, Text.

Types the captured words exactly as spoken, bypassing all pattern processing and smart spacing.

Parameters, in the order a pattern passes them:

- `text` (group_ref) -- The words to type, usually the capture group g1.

Example: `Trigger "^say (.+)$" with params ["g1"]: saying "say hello" types "hello" with no command matching or cleanup.`

#### Type text exactly

Action name: `type_text`. The Pattern Manager lists it under Advanced actions, Text.

Types text character for character with no smart spacing or capitalization.

Parameters, in the order a pattern passes them:

- `text` (text) -- The exact text to type; may be a capture group like g1.

Example: `Trigger "^find\s*(.*)$" presses Ctrl+F then calls type_text with params ["g1"]: saying "x-ray find hello" opens Find and types "hello".`

#### Wrap in delimiters

Action name: `wrap_or_insert`. The Pattern Manager lists it under Advanced actions, Text.

Wraps the selection or the captured words in the given delimiters, or inserts an empty delimiter pair with the cursor between when there is nothing to wrap.

Parameters, in the order a pattern passes them:

- `left_fence` (text) -- Opening delimiter (for example ( or ").
- `right_fence` (text) -- Closing delimiter (for example ) or ").
- `text` (group_ref) -- Capture group holding the words to wrap (usually g1); may be empty.

Example: `Trigger "\bparentheses(.*)$" with params ["(", ")", "g1"]: saying "parentheses hello" inserts "(hello)"; saying just "parentheses" wraps the selection or inserts "()".`

### Wheelhouse

#### Open online help

Action name: `wheelhouse_help_online`. The Pattern Manager lists it under Advanced actions, Wheelhouse.

Opens the configured online help page in the default browser.

Parameters: none.

Example: `Trigger "^help$" with no params: saying "help" opens the help page.`

#### Open the Pattern Manager

Action name: `open_pattern_manager`. The Pattern Manager lists it under Advanced actions, Wheelhouse.

Opens the Pattern Manager window for viewing and editing voice patterns.

Parameters: none.

Example: `Trigger "^patterns?$" with no params: saying "patterns" opens the Pattern Manager.`

#### Set speech interaction mode

Action name: `set_speech_interaction_mode`. The Pattern Manager's action list does not offer this action.

Switches how the microphone is engaged: click to talk (toggle) or push to talk.

Parameters, in the order a pattern passes them:

- `mode` (choice: `toggle`, `push_to_talk`) -- The interaction mode to switch to.

Example: `Trigger "^push to talk mode$" with params ["push_to_talk"]: saying it switches the microphone to push-to-talk.`

#### Stop listening

Action name: `stop_listening`. The Pattern Manager lists it under Advanced actions, Wheelhouse.

Switches listening off, as clicking the floating button or the tray icon does while it listens. In toggle mode, say the wake word to switch it back on; in push-to-talk mode the wake word ends only the idle pause, so hold the floating button again.

Parameters: none.

Example: `Trigger "^stop listening$": saying it by itself switches listening off.`

#### Teach WheelHouse your voice

Action name: `open_calibration`. The Pattern Manager lists it under Advanced actions, Wheelhouse.

Opens the voice-teaching window, where WheelHouse learns how you sound so it stops missing short words. Only the Distil-Whisper speech engine uses it.

Parameters: none.

Example: `Trigger "^learn my voice$" with no params: saying "learn my voice" opens the voice-teaching window.`

#### Teach word to speech engine

Action name: `add_hint_to_stt`. The Pattern Manager's action list does not offer this action.

Sends the clipboard text to the speech-recognition server as a vocabulary hint so it is transcribed correctly.

Parameters: none.

Example: `Trigger "^boost$": select a word, say "boost" by itself, and the word is copied and sent to the speech engine as a hint.`

## Notice Reference

The titles of the notices, message boxes, and windows that Wheelhouse shows, each written exactly as it appears on screen. Each row says when the title appears, what it means, and what to do. The words in parentheses after a title say what kind of notice or window it is.

| Title | When it appears | What it means | What to do |
|---|---|---|---|
| **Open a program** (choice in the new pattern list) | You click Add Pattern in the Pattern Manager. The New Pattern window opens on the list "What do you want to happen?". This is the first choice. | This choice starts a voice command that opens a program, such as Notepad. | Click it, or select it with the arrow keys and press Enter. The editor opens with "Launch a program" selected. Type the words you will say. In the Program box, type the program name, or click Browse... to pick the file. Then click Create. |
| **Switch to an app** (choice in the new pattern list) | You click Add Pattern in the Pattern Manager. This is the second choice in the list "What do you want to happen?". | This choice starts a voice command that brings a window that is already open to the front. | Click it, or select it and press Enter. The editor opens with "Activate a window" selected. Type the words you will say. In the Window/process box, type the window title or program name, such as brave.exe. Then click Create. |
| **Press a keyboard shortcut** (choice in the new pattern list) | You click Add Pattern in the Pattern Manager. This is the third choice in the list "What do you want to happen?". | This choice starts a voice command that presses keys for you, such as Ctrl+S to save. | Click it, or select it and press Enter. The editor opens with "Press a key combination" selected. Type the words you will say. In the Keys box, type the keys joined with +, such as ctrl+s. Or click Record and press the shortcut; Escape cancels the recording. Then click Create. |
| **Type a phrase you say often** (choice in the new pattern list) | You click Add Pattern in the Pattern Manager. This is the fourth choice in the list "What do you want to happen?". | This choice makes a shortcut phrase. When you dictate the phrase, Wheelhouse types your saved text in its place, such as your email address. | Click it, or select it and press Enter. The editor opens with "Insert text" selected. Type the phrase you will say, such as my email. In the "Text to type:" box, type the exact text for Wheelhouse to type. Then click Create. |
| **Correct a word the microphone keeps getting wrong** (choice in the new pattern list) | You click Add Pattern in the Pattern Manager. This is the fifth choice in the list "What do you want to happen?". | This choice fixes a word that speech recognition keeps getting wrong. When dictation produces the wrong word, Wheelhouse types the right one. | Click it, or select it and press Enter. In "What does the microphone type by mistake?", type the wrong word, such as jason. In "What should it type instead?", type the right word, such as JSON. Then click Create. |
| **Create an advanced command** (choice in the new pattern list) | You click Add Pattern in the Pattern Manager. This is the last choice in the list "What do you want to happen?". | This choice opens the full editor with the Advanced check box already ticked. You write your own expression and can add more than one action. | Click it, or select it and press Enter. The editor opens with Advanced ticked. Type the expression in the "Regular expression:" box. Pick the first action, and click "Add step" for each further action. Then click Create. The Advanced check box usually stays ticked here, because the simple editor cannot show a hand-written expression. To use the simple editor instead, click Cancel, click Add Pattern again, and pick another choice. Cancel discards what you typed. |
| **Select Program** (file picker) | You click Browse... next to the Program box in the pattern editor. The Program box shows when "Launch a program" is selected. | This picker lets you choose the program file instead of typing its path. | Find the program's .exe file and click Open. To see other files, pick All Files in the file type list. Wheelhouse puts the full path in the Program box. |
| **Pattern Manager Help** (help window) | You click the "? Help" button in the Pattern Manager. | This window explains patterns, commands and replacements, the safety word, the editor, and Advanced mode. It also lists every action the editor offers. | Read or scroll the page. A link to a part of the page jumps there. A web link opens in your browser. Click Close when you are done. |
| **Change Safety Word** (input box) | You click Change... next to "Safety word:" in the Pattern Manager. | This box sets the safety word. You say the safety word before commands marked [safety word]. | Type one word with no spaces, and click OK. Click Cancel to keep the current word. For an empty entry or more than one word, Wheelhouse shows an error under the safety word row. |
| **Error Creating Pattern** (message box) | You clicked Create in the pattern editor, then closed the editor before Wheelhouse answered. Wheelhouse then reported that it could not create the pattern. While the editor stays open, it shows such an error inside the editor instead. | The new pattern was not saved. The message text is the reason Wheelhouse gave. | Click OK. Open the editor again the same way and enter the pattern again. If the same message comes back, read its reason and fix that problem first. |
| **Error Updating Pattern** (message box) | You clicked Save while you edited one of your patterns, then closed the editor before Wheelhouse answered. Wheelhouse then reported that it could not save the change. | Your change to the pattern was not saved. The message text is the reason Wheelhouse gave. | Click OK. Select the pattern, click Edit..., and make the change again. |
| **Error Deleting Pattern** (message box) | You clicked Delete Pattern or Remove customization, then clicked Yes. Wheelhouse could not delete the pattern. | Wheelhouse did not delete the pattern. The message gives the reason, for example that the pattern was not found. | Click OK. Close and reopen the Pattern Manager to refresh the list. If the pattern is still in the list, try again. |
| **WheelHouse settings** (notice) | You moved or resized the floating button, turned Show Floating Button on or off, or changed the Pattern Manager font size, and Wheelhouse could not save the change. | Wheelhouse shows no notice while it saves a setting or when the save succeeds. "Couldn't save settings" and "Couldn't send settings" mean the change was not saved. Wheelhouse then puts back the last saved values. "Couldn't confirm the settings" means Wheelhouse got no answer, even after it checked the saved settings, so the change may not be saved. | After a message that starts with "Couldn't", make the change again. For "Couldn't change button visibility", select Show Floating Button in the menu again. |
| **Wheelhouse** (notice) | You selected Assistant in the "Ask the Wheelhouse Assistant" window, and Wheelhouse could not save your choice about the "Do not show this again" box. | The notice reads "Wheelhouse could not save your choice from the Assistant window. The window can appear again when you choose Help." Your choice was not kept, so the next Help shows the window again. Other short notices also use the title Wheelhouse. | Choose Help again. When the window appears, set the check box the way you want and select Assistant again. |
| **Terminal Dictation** (window) | You dictate while a terminal window is in front and waiting at its prompt. Windows Terminal, Command Prompt, and PowerShell count as terminals. Wheelhouse opens this window and types your words into it. The terminal inside Visual Studio Code does not open this window. A terminal that runs as administrator does not open this window either. | Wheelhouse collects your dictated command here, so you can check it before it reaches the terminal. | Check or edit the text. Press Enter or click Submit to paste the text into the terminal and run it. Press Shift+Enter for a new line. Press Escape or click Cancel to close the window and discard the text. |
| **Terminal paste failed** (Windows notification) | You pressed Enter or clicked Submit in the Terminal Dictation window. Wheelhouse could not paste the text into the terminal. | Wheelhouse did not press Enter in the terminal, so the command did not run. The Terminal Dictation window closed and its text was cleared. In some cases the text already reached the terminal prompt. | Click the terminal and check its prompt. Then dictate the command again. Wheelhouse does not paste into a terminal that runs as administrator. Type the command there with the keyboard. |
| **Wheelhouse: Speech setup** (Windows notification) | Wheelhouse checks each speech engine's installed files once, when it first starts a speech engine after launch. It shows this notice when an engine's files are out of date. It also shows it when Windows lacks a new enough Microsoft Visual C++ runtime. | Speech can fail to start, or the speech engine can crash. Wheelhouse shows one notice for each problem it finds. | If the message says "Speech environment is out of date", run the Wheelhouse installer again. For the Visual C++ runtime message, run the installer again and select Yes when Windows asks for permission. If you already did that, restart the computer. |
| **Wheelhouse: STT Choice Not Saved** (Windows notification) | You picked a speech engine from STT Provider in the right-click menu. The settings file could not record the choice. | Wheelhouse already stopped the old engine and started the new one, so the new engine is running now. The settings file was not changed. It has a plain value where its stt section belongs, so it cannot store the choice. The next time Wheelhouse starts, it will not remember this choice. | Select Exit in the menu to close Wheelhouse. Open the settings file, %LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\config.toml, in Notepad. Find the line that sets stt to one value, such as stt = "whisper", and delete it. Or copy config.toml.example over config.toml to restore every default. Then start Wheelhouse and pick the engine again from STT Provider. |

## Configuration Reference

### General

**SPEECH_WEBSOCKET_HOST** *(default: `"127.0.0.1"`)* -- The network address on which Wheelhouse's speech connection server listens; the default 127.0.0.1 accepts connections from this computer only. Change only for the advanced setup where speech recognition runs on a second computer on your home network. That setup also needs ws_host under [stt], the address the speech engine connects to, which the shipped file does not contain.

**REPLACEMENT_TIMEOUT_MS** *(default: `700`)* -- How long Wheelhouse waits after you stop speaking, in milliseconds, before deciding a correction phrase is complete. Raise to 900-1000 if corrections fire before you finish (common on slower machines); lower slightly if responses feel sluggish.

**COMMAND_TIMEOUT_MS** *(default: `700`)* -- How long Wheelhouse waits after you stop speaking, in milliseconds, before deciding a command phrase is complete. Raise to 900-1000 if commands fire before you finish (common on slower machines); lower slightly if responses feel sluggish.

**GREEDY_TIMEOUT_MS** *(default: `5000`)* -- A longer wait, in milliseconds, for commands that intentionally keep listening for more words. Rarely needs changing.

**COMMAND_COMPLETION_WAIT_MS** *(default: `1000`)* -- Has no effect. No part of Wheelhouse reads this setting. Changing it changes nothing. To give commands more time, change COMMAND_TIMEOUT_MS.

**ENABLE_AUDIO_SUPPRESSION** *(default: `"auto"`)* -- Pause listening while this computer plays sound, when the microphone has no echo canceller. Valid values: "auto", true, false. "auto" asks Windows once at startup whether the default microphone has an echo canceller, and pauses only when Windows reports none or cannot answer. Set true to pause whenever sound plays, or false to never pause; with false and no echo canceller, expect more misrecognitions, because the microphone picks up the sound. On a machine where Windows reports no echo canceller, sound from a screen reader also pauses listening.

**ENABLE_SONOS_SUPPRESSION** *(default: `true`)* -- Pause listening while Sonos music is playing. Turn off only if you want Wheelhouse listening during playback; expect more misrecognitions, because the microphone picks up the audio.

**ENABLE_IDLE_SUPPRESSION** *(default: `true`)* -- Pause listening after the computer sits idle. Turn off only if you never want idle pauses; the Idle Monitor plugin controls the timing.

**LOG_FILE** *(default: `""`)* -- Has no effect. No part of Wheelhouse reads this setting. Wheelhouse always writes its activity log to wheelhouse.log in its install folder, %LOCALAPPDATA%\Wheelhouse\app.

**LOG_LEVEL** *(default: `"INFO"`)* -- How detailed the activity log is. Change only when a support conversation asks you to.

**LOG_TRANSCRIPTS** *(default: `false`)* -- A privacy setting: false keeps the words you dictate and your clipboard contents out of the log files (only text lengths are noted). Set true only while troubleshooting recognition, then turn it back off; while on, everything you dictate, including passwords, accumulates in the logs.

**SIDE_OFFSET** *(default: `10`)* -- Width in pixels of the left-edge screen zone where the mouse thumb wheel adjusts brightness instead of volume. Raise it if the brightness zone is hard to hit.

**BRIGHTNESS_INCREMENT** *(default: `1.0`)* -- The size of each thumb-wheel brightness adjustment step. Raise for faster, coarser changes; lower for finer control.

**VOLUME_INCREMENT** *(default: `0.5`)* -- The size of each thumb-wheel volume adjustment step. Raise for faster, coarser changes; lower for finer control.

**THUMB_WHEEL_DEAD_ZONE_TICKS** *(default: `3`)* -- How many thumb-wheel ticks a gesture must reach before the brightness or volume zone acts. Ticks below this are held; the batch that reaches it releases them, and every later tick in the same gesture acts at once. Raise if the wheel still reacts to an accidental touch; set to 1 to make every tick act at once.

**THUMB_WHEEL_GESTURE_GAP_MS** *(default: `500`)* -- The pause, in milliseconds, that ends a thumb-wheel gesture. After a longer pause the next touch starts in the dead zone again and any held ticks are forgotten. Raise if a slow, deliberate roll keeps stopping early; lower if a stray tick still adds up with the next touch.

**THUMB_WHEEL_MAX_TICKS_PER_BATCH** *(default: `3`)* -- The most thumb-wheel ticks one 50 ms batch may apply to brightness or volume. Ticks above this in a single batch are discarded, so a fast flick moves at most this many ticks per batch. Raise for larger changes from a fast roll; lower if a flick still jumps too far.

**BRIGHTNESS_STEPS_PER_COMMAND** *(default: `2`)* -- The most native TV brightness steps one thumb-wheel command may carry. The TV answers one command at a time, about 400 ms each for a Samsung, so this is how far brightness moves per round trip while the wheel turns. Ticks beyond it are discarded, so the TV never keeps moving after the wheel stops. Set 1 for the smoothest one-step-at-a-time change; raise it if brightness moves too slowly. A value below 1 counts as 1.

**FLOATING_BUTTON_SIZE** *(default: `50`)* -- Size in pixels of the small on-screen status button. Dragging the button's outer edge, or holding Ctrl and rolling the mouse wheel over it, writes this setting.

**FLOATING_BUTTON_POS** *(default: `[100, 100]`)* -- Screen position of the small on-screen status button, as [x, y] pixels from the top-left of the desktop. Dragging the button writes this setting, and so does resizing it, because the button grows and shrinks around its own centre.

**FLOATING_BUTTON_VISIBLE** *(default: `true`)* -- Whether the small on-screen status button is shown. Set true for an always-visible microphone click target. In push-to-talk mode the floating button is the only way to listen, because the hold works on it alone.

**SPEECH_ENABLED_ON_STARTUP** *(default: `true`)* -- Whether Wheelhouse starts listening as soon as it launches. Set false to turn the microphone on manually each session.

**SHOW_SPEECH_PULSE** *(default: `true`)* -- Pulse the floating button while Wheelhouse hears you, as a signal that your speech is reaching it. The tray icon does not pulse. With this off, the button still flashes green when an utterance has been processed. Turn off only if the animation distracts.

**SPATIAL_SOUND_EXEC** *(default: `""`)* -- Path to the small free NirSoft helper tool used for voice switching of Dolby Atmos spatial sound; empty means the feature is off. Fill in the tool path only if you use Dolby Atmos and have that tool installed; everyone else can ignore it.

**SPATIAL_SOUND_FORMAT** *(default: `"Dolby Atmos for home theater"`)* -- The spatial sound format name passed to the helper tool. Only matters if SPATIAL_SOUND_EXEC is set.

### [brightness_coordinator]

**software_dimmer** *(default: `"gamma_dimmer"`)* -- The software dimming method used once hardware brightness is as low as it goes. Valid values: "gamma_dimmer" (darkens through the graphics card), "overlay" (a translucent overlay window), or "software_dimmer" (the same overlay window as "overlay"). Any other value falls back to "gamma_dimmer" with a warning in the log. Change only if dimming misbehaves with your monitor setup.

**unwinding_threshold** *(default: `10`)* -- Currently has no effect -- Wheelhouse hands control back to the hardware only once software dimming is fully undone, whatever this is set to.

### [plugins.internal_panel]

**enabled** *(default: `true`)* -- Turns the Internal Panel plugin on or off; it controls a laptop's built-in screen brightness from the brightness scroll zone. On a desktop PC with no built-in panel it does nothing and is safe to leave enabled.

### [plugins.sonos]

**enabled** *(default: `false`)* -- Turns the Sonos plugin on or off; it adjusts Sonos speaker volume from the volume scroll zone and pauses listening while music plays. Turn it on only if you own Sonos speakers.

**polling_interval** *(default: `2`)* -- How often, in seconds, to check whether music is playing.

### [plugins.system_volume]

**enabled** *(default: `true`)* -- Turns the System Volume plugin on or off; it controls the normal Windows volume from the volume scroll zone and quiets system audio during push-to-talk holds.

**device_type** *(default: `"default"`)* -- Which audio device to control. Valid values: "default" (the usual choice) or "communications".

**volume_step_db** *(default: `1.5`)* -- Loudness change per wheel step, in decibels.

**min_volume_db** *(default: `-96.0`)* -- The volume floor, in decibels.

**max_volume_db** *(default: `0.0`)* -- The volume ceiling, in decibels.

### [plugins.bravia]

**enabled** *(default: `false`)* -- Turns the Bravia plugin on or off; it brings a Sony Bravia TV used as a monitor into Wheelhouse's brightness control. Turn it on only if a Sony Bravia TV is your monitor.

**ip_address** *(default: `""`)* -- Your TV's address on the home network; leave it blank and Wheelhouse searches the network for the TV automatically. Set it if you have more than one TV or discovery fails.

**psk** *(default: `""`)* -- The pre-shared key you set on the TV under Settings -> Network -> Home Network -> IP Control; the plugin will not start with it blank.

**device_name** *(default: `"SONY TV"`)* -- The TV's audio device name exactly as Windows shows it under Sound settings -> Output; a device lookup key for spatial-sound handling, not a free-form label.

### [plugins.idle_monitor]

**enabled** *(default: `true`)* -- Turns the Idle Monitor plugin on or off; it pauses listening when you step away, and listening resumes when you return or say the wake word. Almost everyone should leave this on.

**idle_timeout_minutes** *(default: `10`)* -- Minutes of no keyboard or mouse activity before listening pauses.

**polling_interval_seconds** *(default: `4`)* -- How often, in seconds, the plugin checks for idleness.

### [plugins.window_positioning]

**enabled** *(default: `true`)* -- Turns the Window Positioning plugin on or off; it moves the Windows On-Screen Keyboard out of the way when it would cover your working window.

**target_window_names** *(default: `["On-Screen Keyboard", "osk"]`)* -- Which windows the plugin moves; the default is the On-Screen Keyboard.

**move_cooldown_seconds** *(default: `0.5`)* -- Minimum seconds between moves, preventing jitter.

**clearance_gap_pixels** *(default: `5`)* -- Gap in pixels left between the moved window and the window it was covering.

**ignore_window_titles** *(default: `["Program Manager", "Task Switching", "SoftwareDimmerOverlay_AlphaBlend_v12", "MainWindow"]`)* -- Window titles that should never trigger a move.

**ignore_window_classes** *(default: `["Shell_TrayWnd", "Progman"]`)* -- Window classes that should never trigger a move.

### [wake_word]

**enabled** *(default: `true`)* -- Turns wake-word listening on or off; in toggle mode, whenever listening is off, for any reason, saying the wake word out loud turns it back on; in push-to-talk mode it ends only the idle pause.

**keyword** *(default: `"computer"`)* -- The wake word.

**sensitivity** *(default: `0.5`)* -- Wake-word detection sensitivity, range 0-1. Lower it if saying the wake word often fails to wake Wheelhouse; raise it if ordinary conversation keeps waking it by accident.

**mode** *(default: `"idle_recovery"`)* -- What the wake word is used for -- in toggle mode, turning listening back on whenever it is off; in push-to-talk mode, ending only the idle pause, after which the next hold of the floating button is the way back. Valid values: "idle_recovery" or "push_to_talk"; any other value turns the wake word off for every pause, and the log gives no warning about it.

**model_dir** *(default: `"../shared/data/wake_words"`)* -- Where the wake-word listening model lives on disk; set by the installer, do not change it.

### [ui_actions.timing]

**clipboard_verification_timeout_ms** *(default: `250`)* -- How long, in milliseconds, to wait for the clipboard to verify during text insertion. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**clipboard_operation_delay_ms** *(default: `50`)* -- Delay, in milliseconds, between clipboard operations during text insertion. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**selection_clear_delay_ms** *(default: `20`)* -- Delay, in milliseconds, after clearing a selection during text insertion. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**context_gather_delay_ms** *(default: `10`)* -- Delay, in milliseconds, before gathering the text context around the caret. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**post_paste_delay_ms** *(default: `30`)* -- Delay, in milliseconds, after pasting text into the target application. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**utterance_clipboard_timeout_seconds** *(default: `60.0`)* -- A safety limit, in seconds. Wheelhouse puts dictated text on the clipboard to paste it, and restores your own clipboard contents when the utterance ends. If the signal that the utterance has ended never arrives, Wheelhouse ends it after this long and restores your clipboard.

### [ui_actions.verified_unicode]

**max_chars** *(default: `50`)* -- Dictations up to this length are typed directly, character by character, avoiding your clipboard; longer ones go through the clipboard. Lower it if a particular app mishandles direct typing; raise it to have more dictations bypass the clipboard.

### [ui_actions.foreground_check]

**same_process_browser_names** *(default: `["brave.exe", "brave_beta.exe", "chrome.exe", "chromium.exe", "msedge.exe", "edge.exe", "vivaldi.exe", "opera.exe", "operagx.exe", "arc.exe"]`)* -- The web browsers Wheelhouse recognizes (browsers manage their windows in an unusual way); all the mainstream ones are already listed.

**same_process_browser_names_extend** *(default: `[]`)* -- Adds an unusual browser to the recognized list without retyping the built-ins.

### [ui_actions.text_target]

**allow_class_names_extend** *(default: `[]`)* -- Extends the built-in list of window classes allowed to receive dictation. Add a window here when you want your short phrases typed key by key into it. Wheelhouse pastes your words into a box it does not recognize, and it asks you nothing. In some apps your words are never typed key by key, whatever you add here, and long text is never typed either.

**deny_control_types_extend** *(default: `[]`)* -- Extends the built-in list of control types denied dictation. Add a control here when Wheelhouse types into something that is not a text box. Wheelhouse then pastes your words there instead of typing them, so the words still arrive.

**deny_class_names_extend** *(default: `[]`)* -- Extends the built-in list of window classes denied dictation. Add a window here when Wheelhouse types into something that is not a text box. Wheelhouse then pastes your words there instead of typing them, so the words still arrive.

**browser_process_names_extend** *(default: `[]`)* -- Extends the built-in list of browser process names used by the dictation safety check. Add a browser here when Wheelhouse types into the page itself, which scrolls the page. Wheelhouse then pastes your words instead of typing them.

### [speech]

**interaction_mode** *(default: `"toggle"`)* -- The microphone interaction mode: toggle keeps the microphone on until you turn it off; push_to_talk listens only while you hold the floating button, muting system audio during the hold. Valid values: "toggle" or "push_to_talk". You can also switch by voice (push to talk mode / click to talk mode) without editing anything.

**ptt_safety_timeout_seconds** *(default: `30`)* -- In push-to-talk mode, automatically releases the microphone if a hold gets stuck. Raise it if you routinely dictate longer than 30 seconds in one hold.

**notify_on_revision** *(default: `false`)* -- Show a small notice when the speech engine revises its guess at what you said.

### [stt]

**last_provider** *(default: `"parakeet_tdt"`)* -- Which speech-to-text engine Wheelhouse uses; you normally switch engines from the menu on the floating button or the tray icon, and Wheelhouse writes your choice here for you, which is why it is called the last provider. Valid values: "parakeet_tdt" (local, offline, no account), "distil_medium_en" (local, runs on an NVIDIA graphics card), or "google_stt" (Google Cloud; needs an account, sends audio to Google).

### [stt.google]

**credentials_file** *(default: `""`)* -- Full path to the Google service-account key file (the JSON file downloaded during Google Cloud setup). Type the path here yourself, doubling each backslash, and restart Wheelhouse; when this is empty, the GOOGLE_APPLICATION_CREDENTIALS environment variable is used instead.

### [ai]

**enabled** *(default: `true`)* -- The master switch for all AI features: dictation text correction, and also the rewrite commands and Ask AI, which it switches on and off together (it also gates the in-app help chat, which is currently disabled). New installs leave it off unless you chose the AI helper during setup.

**knowledge_base** *(default: `"knowledge/wheelhouse_help.md"`)* -- The document the in-app help assistant would consult; because the in-app help chat is currently disabled, this setting has no effect today.

### [ai.server]

**base_url** *(default: `"http://127.0.0.1:8781/v1"`)* -- The address of the AI server Wheelhouse talks to, using the standard OpenAI-style interface; empty leaves AI off. Any OpenAI-compatible address works, local or hosted. The installer's local AI choice writes http://127.0.0.1:8781/v1, and only its cloud choice fills in Google's Gemini address.

**model** *(default: `"gemma-4-e4b"`)* -- The model name to request from the AI server. Change it to whatever model your server has installed.

**kind** *(default: `"local"`)* -- Whether the AI server is on your own machine or out on the internet, which frames the privacy tradeoff: with a local server, the text being corrected never leaves your computer. Valid values: "local" or "cloud". This setting does not decide where your text is sent -- base_url above does that. Capitals and stray spaces are forgiven; anything else falls back to local and says so in the log.

**timeout_s** *(default: `30`)* -- Seconds Wheelhouse waits for the AI server before giving up on a request. Raise it if a slow local model keeps timing out.

### [ai.runtime]

**enabled** *(default: `false`)* -- Whether Wheelhouse starts its own model server. False means you start one yourself and point base_url at it. Valid values: true or false. When true, base_url above must name 127.0.0.1 or localhost and include a port -- the port Wheelhouse starts its server on comes from that address, so the two cannot drift apart. The installer sets this to true when it has downloaded a model for you. Set it to false if you would rather run your own server, or point Wheelhouse at a hosted one.

**model_path** *(default: `""`)* -- The full path to the model file Wheelhouse loads. The installer writes this. Point it at a different model file to change which model answers. Nothing else has to change.

**binary_dir** *(default: `""`)* -- The folder holding llama-server.exe, the program that runs the model. The installer writes this. Change it if you moved the llama.cpp build, or to use a build made for different graphics hardware.

**context_size** *(default: `8192`)* -- How much text the model can consider at once, counted in tokens. Raise it if you correct or rewrite long passages and the reply comes back cut short. A larger value uses more memory.

**gpu_layers** *(default: `99`)* -- How much of the model to place on the graphics card. Valid values: 99 places the whole model on the graphics card; 0 runs it entirely on the processor, which the installer chooses for a machine with enough system memory but no suitable graphics card. Values in between split it. Lower it if the server fails to start because the graphics card is out of memory.

**startup_timeout_seconds** *(default: `90`)* -- How long Wheelhouse waits for the model server it starts to report itself ready before it stops that attempt. The AI features stay off until a later attempt succeeds: Wheelhouse checks the server every 60 seconds and when an AI command needs it, and the first check after the timeout starts the server again. After each failed start, the wait before the next start doubles, from 120 seconds to at most 600 seconds. After a start that succeeds, the next start waits at least 60 seconds. Raise it if a large model on a slow disk is still loading when Wheelhouse stops waiting.

### [ai.help]

**assistant_url** *(default: `"https://notebook.google.com/notebook/da51a404-67ec-4804-9ebe-83605df3e9cf/preview"`)* -- The web address that Help on the menu, and the spoken command "help", open in your browser; if you blank it out, both show a notice that online help is not configured. Release 1.2.1 and earlier named this setting gem_url; updating with the installer renames it to assistant_url and replaces an old assistant address with the current one.

**explain_before_open** *(default: `true`)* -- Whether a window explaining the Wheelhouse Assistant appears before your browser opens; the window's "Do not show this again" check box sets this to false, and setting it back to true brings the window back. While it is false and the file help_explainer_notebook_shown.toml is missing from the data folder next to the settings file, the window appears at every Help with the box already ticked. Only the Assistant button writes that file, so after Cancel the window appears again at the next Help; select Assistant with the box still ticked to keep the window off.

**max_response_tokens** *(default: `800`)* -- Caps the length of an answer from the in-app help chat; because that chat is currently disabled, this setting has no effect today.

### [click]

**enabled** *(default: `true`)* -- The master switch for voice clicking -- the click-something-by-name commands and the numbered overlay.

**min_confidence** *(default: `0.4`)* -- How sure Wheelhouse must be before clicking something by name. Raise it if it clicks the wrong thing; lower it if it too often finds no match.

**clear_winner_margin** *(default: `0.15`)* -- How clearly one candidate must beat the runner-up before Wheelhouse clicks it by name; with no clear winner it shows the numbered overlay instead of guessing.

**notice_max_names** *(default: `3`)* -- How many candidate names appear in the did-you-mean style notice.

**overlay_badge_font_pt** *(default: `8`)* -- The size of the painted overlay numbers. Raise it if the numbers are hard to read.

**overlay_badge_shadow** *(default: `false`)* -- Whether each overlay number casts a drop shadow. Off by default; the bubble's border already separates it from the background. Turn it on if the numbers blend into busy screen content.

**overlay_badge_theme** *(default: `"auto"`)* -- The color of the numbered overlay's speech bubbles. The value names the bubble's own color, not the system theme: light is a white bubble with a black number, dark is a near-black bubble with a white number, and auto picks the opposite of the Windows theme so the bubbles stand out. Valid values: "auto" (follow the Windows theme, inverted for contrast), "light" (white bubble), or "dark" (near-black bubble). Pin it to light or dark if the automatic choice blends into the apps you use most.

**response_timeout_ms** *(default: `3000`)* -- How long, in milliseconds, Wheelhouse waits for a click command's search before giving up. Raise it on a slow machine if clicks time out in complex windows.

**walk_deadline_ms** *(default: `2500`)* -- How long, in milliseconds, Wheelhouse searches a window for the control a spoken click names before giving up. Raise it on a slow machine if clicks time out in complex windows. Keep it more than 250 below response_timeout_ms: a value that is not switches voice clicking off, and the log names this key.

**screen_read_timeout_ms** *(default: `10000`)* -- How long, in milliseconds, Wheelhouse waits for a read of the window's clickable things (show numbers, the refresh after a focus change, and the re-read after a click) before giving up. Separate from response_timeout_ms, which limits a click reply. Raise it if the numbered overlay reports that it could not draw the numbers in windows that take several seconds to answer.

**snapshot_ttl_seconds** *(default: `30`)* -- How long the numbered overlay's snapshot stays valid, in seconds.

**browser_processes** *(default: `["brave.exe", "chrome.exe", "msedge.exe", "vivaldi.exe", "slack.exe", "discord.exe", "code.exe", "ms-teams.exe", "Teams.exe", "spotify.exe", "notion.exe", "obsidian.exe", "ChatGPT.exe"]`)* -- The browser-like apps (browsers, Slack, Discord, and similar) that need a deeper search for clickable elements.

**browser_processes_extend** *(default: `[]`)* -- Adds a browser-like app to the deeper-search list without retyping the built-ins. Add an app here if voice clicking cannot see controls inside it.

**enable_screen_reader_flag** *(default: `false`)* -- Tells apps a screen reader is present, which makes some expose more clickable elements. Try true if an app hides its buttons; note some apps change their appearance when this is on.

**grid_min_cell_px** *(default: `24`)* -- The smallest a mouse-grid cell may become, in pixels; spoken numbers stop narrowing the grid past that size. Lower it for finer pointer placement on a very high resolution screen; a cell this small already places the pointer within a click's precision.

**drag_duration_ms** *(default: `250`)* -- How long, in milliseconds, a mouse-grid drag takes to move from the marked point to the destination. Raise it if an application does not register the drag; many ignore a pointer that moves too fast.

### [actions]

**output_cap_chars** *(default: `10000`)* -- The most characters a pattern step's captured text may hold -- the output a run_capture program prints or the reply an ask_ai step receives; longer output makes the step fail rather than insert an incomplete piece of it. Raise it (up to the 15360 ceiling) if a legitimate script prints more; lower it for a tighter guard against a runaway program filling your document.

**run_capture_timeout_default_s** *(default: `10`)* -- How many seconds a run_capture pattern step waits for its program to finish when the pattern gives no timeout number of its own. Raise it if your scripts legitimately run longer; 60 seconds is the most any waiting step will wait, because voice commands wait their turn while a step runs.

---

Generated: 2026-10-01 for the v1.2.2 release
