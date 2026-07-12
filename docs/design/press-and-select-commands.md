# Press & Select Voice Commands

Quick reference for all press and select commands defined in `speech/config/patterns.toml`.

## Press Commands

Commands that send individual keystrokes or key combinations.

| Voice Command | Pattern | Function | Action |
|---|---|---|---|
| "delete [N]" | `^delete\s*(\d+)?$` | `press` | Del key, optional repeat count |
| "delete word" | `^delete word$` | `hk` (x3) | Ctrl+Left, Shift+Ctrl+Right, Del (selects then deletes word) |
| "backspace [N]" | `^back ?space\s*(\d+)?$` | `press` | Backspace key, optional repeat count |
| "tab/indent N" | `^(tab\|indent)\s+(\d+)$` | `press` | Tab key, repeated N times |
| "shift tab" / "outdent" | `^(shift tab\|outdent)$` | `hk` | Shift+Tab (reverse indent) |
| "press [keys]" | `^press\s*(.+)$` | `press_keys` | Generic -- any key combo by name (see capabilities below) |
| "submit" | `^submit$` | `press_keys` | Enter key |
| "escape" | `^escape$` | `press` | Esc key |

### Notes

- **"delete word"** is ordered before "delete N" in the file to prevent "word" from being captured as an invalid number.
- **"press [keys]"** is the catch-all -- key order in the spoken phrase doesn't matter ("press delete control" = "press control delete").
- **Repeat counts** on delete/backspace/tab are optional. Omitting N presses once.

### press_keys Capabilities

The generic "press [keys]" command accepts any combination of the keys below. Modifiers are
automatically pressed first regardless of spoken order. Hyphenated tokens from STT (e.g., "f-11",
"control-alt") are expanded automatically. If any word is unrecognized, the entire phrase falls
through to dictation instead.

**Modifiers:**

| Spoken | Key Sent |
|---|---|
| "control" | Ctrl |
| "ctrl" | Ctrl |
| "alt" | Alt |
| "shift" | Shift |
| "windows" / "win" | Win |

**Navigation & Editing:**

| Spoken | Key Sent |
|---|---|
| "enter" / "return" | Enter |
| "escape" | Esc |
| "tab" | Tab |
| "backspace" | Backspace |
| "delete" / "del" | Delete |
| "insert" | Insert |
| "space" | Space |
| "home" | Home |
| "end" | End |
| "page up" | Page Up |
| "page down" | Page Down |
| "up" / "down" / "left" / "right" | Arrow keys |
| "pause" | Pause |
| "caps lock" | Caps Lock |
| "print screen" | Print Screen |

**Function Keys:**

F1 through F12 (spoken as "f1", "f2", etc.; STT hyphenated forms like "f-11" are handled).

**Letters & Numbers:**

All letters (a-z) and digits (0-9) are accepted directly.

**Symbols (speakable names):**

| Spoken | Character |
|---|---|
| "backtick" | `` ` `` |
| "tilde" | `~` |
| "semicolon" | `;` |
| "colon" | `:` |
| "slash" / "forward slash" | `/` |
| "backslash" / "back slash" | `\` |
| "pipe" | `\|` |
| "question" / "question mark" | `?` |
| "comma" | `,` |
| "period" / "dot" | `.` |
| "quote" / "double quote" | `"` |
| "single quote" / "apostrophe" | `'` |
| "left bracket" / "open bracket" | `[` |
| "right bracket" / "close bracket" | `]` |
| "left brace" / "open brace" | `{` |
| "right brace" / "close brace" | `}` |
| "left parenthesis" / "left paren" / "open parenthesis" / "open paren" | `(` (Shift+9) |
| "right parenthesis" / "right paren" / "close parenthesis" / "close paren" | `)` (Shift+0) |
| "less than" | `<` |
| "greater than" | `>` |
| "equals" / "equal" | `=` |
| "plus" | `+` |
| "minus" / "hyphen" / "dash" | `-` |
| "underscore" | `_` |
| "hash" / "hashtag" / "pound" | `#` |
| "at" / "at sign" | `@` |
| "ampersand" / "and sign" | `&` |
| "asterisk" / "star" | `*` |
| "caret" / "carrot" | `^` |
| "percent" | `%` |
| "dollar" / "dollar sign" | `$` |
| "exclamation" / "bang" | `!` |

**Examples:** "press control shift t", "press f5", "press alt f4", "press left bracket"

## Select Commands

Commands that create text selections at the cursor position.

| Voice Command | Pattern | Function | Key Sequence |
|---|---|---|---|
| "select all" | `^select all$` | `hk` | Ctrl+A |
| "select word" | `^select word$` | `hk` (x2) | Ctrl+Left, then Shift+Ctrl+Right |
| "select line" | `^select line$` | `hk` (x2) | Home, then Shift+End |
| "select paragraph" | `^select paragraph$` | `hk` (x3) | Ctrl+Down, Ctrl+Up, then Shift+Ctrl+Down |

### Notes

- All select actions use `awaits_done = true` on each step so keystrokes execute sequentially.
- **"select word"** moves cursor to word start first, then shift-selects to end.
- **"select line"** moves cursor to line start first, then shift-selects to end.
- **"select paragraph"** navigates to paragraph boundaries before shift-selecting.

## Cursor Navigation (cursor_navigate)

The "go" command provides composable cursor navigation with optional selection via "grab".
Commands can be chained with "then" in a single utterance.

### Verbs

| Verb | Effect |
|---|---|
| "go" | Move cursor (no selection) |
| "grab" | Move cursor while holding Shift (extends selection) |

### Landmarks

Absolute positions -- jump directly to a location.

| Spoken | Key Sequence | Example |
|---|---|---|
| "home" | Home | "go home" |
| "end" | End | "go end" |
| "top" | Ctrl+Home | "go top" |
| "bottom" | Ctrl+End | "go bottom" |
| "start of word" / "beginning of word" | Ctrl+Left | "go start of word" |
| "end of word" | Ctrl+Right | "go end of word" |
| "start of paragraph" / "beginning of paragraph" | Ctrl+Up | "go start of paragraph" |
| "end of paragraph" | Ctrl+Down | "go end of paragraph" |

### Relative Movement

Direction + optional count + optional unit. Defaults: count=1, unit=character.

| Direction | Unit | Key Sequence | Example |
|---|---|---|---|
| "right" / "left" | character (default) | Arrow key | "go right", "go left 5" |
| "right" / "left" | word | Ctrl+Arrow | "go right 3 words" |
| "right" / "left" | paragraph | Ctrl+Up/Down | "go left 2 paragraphs" |

**Counts:** Digits ("3") or spoken words ("one" through "ten"). "to", "too", and "for" are accepted as homophones for 2 and 4. Maximum count: 50.

### Grab (Selection)

"grab" works identically to "go" but holds Shift, extending the selection.

| Example | Effect |
|---|---|
| "grab to end" | Shift+End (select to end of line) |
| "grab to top" | Shift+Ctrl+Home (select to top of document) |
| "grab right 5 words" | Shift+Ctrl+Right x5 (select 5 words right) |
| "grab to start of paragraph" | Shift+Ctrl+Up (select to paragraph start) |

### Chaining with "then"

Multiple commands in one utterance, separated by "then":

| Example | Effect |
|---|---|
| "go home then grab to end" | Move to line start, then select to end |
| "go right 3 words then grab right 2 words" | Skip 3 words, then select the next 2 |
| "go top then grab to bottom" | Select entire document |

### Fallthrough

If any part of the utterance is unparseable, the entire phrase falls through to dictation
and is inserted as text. This prevents garbled speech from producing unexpected cursor movement.

## Related Commands

These aren't "press" or "select" commands per se, but they operate on or create selections:

| Voice Command | Category | Action |
|---|---|---|
| "copy" / "copy all" / "copy line" | Clipboard | Creates selection then copies |
| "cut" | Clipboard | Cuts current selection (requires hotword) |
| "upper case", "lower case", etc. | Transform | Transforms current selection in-place |
| "compress" | Transform | Removes extra whitespace from selection |
