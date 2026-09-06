# Wheelhouse Voice Command and Configuration Reference

This is the complete, automatically generated reference for every Wheelhouse voice command and configuration setting. It is built from the same sources the application uses, so it stays in step with what Wheelhouse actually does. For a guided introduction to using and installing Wheelhouse, see the installation guide (wheelhouse_install.md).

## Voice Command Reference

### Dictation Control

| Say this | What happens | Notes |
|---|---|---|
| literal [words] | Types the words after "literal" exactly, skipping all command and replacement processing. Takes effect wherever it appears in an utterance, not only as the first word. | The escape hatch -- see the detailed explanation in "Special Commands" |
| insert [text] | Inserts raw text with no capitalization, spacing, or formatting applied | Useful for exact fragments like an email address or a product code |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item 1", "item 5" |
| submit | Presses Enter. Recognized at the trailing position of an utterance. | Also works as the last word of a sentence: "hello world submit" types "hello world" and then presses Enter. To type the word itself, say "literal submit" |
| type [words] / dictate [words] | Types the words that follow as ordinary dictation, so a phrase that would otherwise run as a command is written out instead | e.g. "type delete all" writes those words rather than clearing the field |
| enter | Presses the Enter key. Fires only when it is the whole utterance. | "submit" does the same and also works as the last word of a sentence |

### Text Editing

| Say this | What happens | Notes |
|---|---|---|
| backspace [number] | Deletes one character to the left, or that many with a number | e.g. "backspace 5" or "backspace twenty three" -- say the count as digits or as words; the number is optional, counts capped at 50. |
| delete [number] | Deletes one character (or that many) to the right. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | e.g. "delete 5" or "delete twenty three" -- say the count as digits or as words; counts capped at 50 |
| delete word | Deletes the entire word under the cursor |  |
| undo [number] | Undoes the last action, or several. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Ctrl+Z; e.g. "undo 3". Common mishearings "undue" and "undu" also fire |
| redo [number] | Redoes the last undone action, or several. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Ctrl+Y; the common mishearing "redu" also fires |
| new line | Inserts a line break without submitting the field | Works inline during dictation |
| new paragraph | Inserts two line breaks | Works inline during dictation |
| tab [number] | Presses Tab that many times | e.g. "tab 3" or "tab eleven" -- say the count as digits or as words; "indent 3" does the same. The number is required here; a bare "tab" spoken on its own presses Tab once, and "tab" inside a longer sentence is typed as the word |
| shift tab | Outdents (Shift+Tab) | "outdent" does the same |
| escape | Presses the Escape key. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "dismiss" also works. "dismiss the meeting invite" on its own is typed as ordinary text, not treated as this command. |
| press [keys] | Presses any key or key combination by name | e.g. "press enter", "press alt f4", "press f5"; See the press-keys detail subsection of the Voice Commands section |
| copy | Copies the current selection. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| copy line | Copies the entire current line |  |
| copy all | Copies everything in the current field |  |
| copy screen | Starts the Windows screenshot snipping tool |  |
| cut | Cuts the current selection. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "cut that" also works. Say it as the whole sentence; "cut the vegetables" on its own is typed as ordinary text, not treated as this command. |
| paste | Pastes the clipboard contents. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires the hotword |
| select all | Selects everything in the current field |  |
| delete all | Selects everything in the current field and deletes it | Say it as the whole sentence. "delete all the files" on its own is typed as ordinary text, not treated as this command. |
| select word | Selects the word under the cursor | "select this word" also works. "select word by word until it looks right" on its own is typed as ordinary text, not treated as this command. |
| select line | Selects the line under the cursor | "select this line" also works. "select line six and copy it" on its own is typed as ordinary text, not treated as this command. |
| select paragraph | Selects the paragraph under the cursor | "select this paragraph" also works. "select paragraph three of the contract" on its own is typed as ordinary text, not treated as this command. |
| x-ray select [words] | Selects the first place those words appear in the document. The words must match the document exactly, apart from capital letters. | Wheelhouse shows a Windows notification when it selects nothing, so a screen reader can read the reason. |
| save | Saves the current document (Ctrl+S). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| x-ray find [text] | Opens the app's find bar and types the search term | e.g. "x-ray find invoice" |
| replace | Opens find-and-replace (Ctrl+H). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| search | Copies the current selection and runs a web search for it. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Select the text first |
| delete next [number] characters | Selects that many characters to the right of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete next [number] words | Selects that many words to the right of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete next [number] lines | Moves to the start of the line, selects down that many lines, and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete next [number] paragraphs | Selects that many paragraphs below the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete previous / last [number] characters | Selects that many characters to the left of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete previous / last [number] words | Selects that many words to the left of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete previous / last [number] lines | Moves to the start of the line, selects up that many lines, and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete previous / last [number] paragraphs | Selects that many paragraphs above the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| delete this word | Moves to the start of the word under the cursor, selects the whole word, and deletes it. Fires only when it is the whole utterance. | "delete word" runs the same three keystrokes |
| delete line | Selects the whole line the cursor is on and deletes its text. Fires only when it is the whole utterance. | "delete this line" does the same; the line break stays, so the line is left empty |
| delete paragraph | Moves to the start of the paragraph the cursor is in, selects the whole paragraph, and deletes it. Fires only when it is the whole utterance. | "delete this paragraph" does the same |
| cut next [number] characters | Selects that many characters to the right of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut next [number] words | Selects that many words to the right of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut next [number] lines | Moves to the start of the line, selects down that many lines, and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut next [number] paragraphs | Selects that many paragraphs below the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] characters | Selects that many characters to the left of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] words | Selects that many words to the left of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] lines | Moves to the start of the line, selects up that many lines, and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] paragraphs | Selects that many paragraphs above the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] characters | Selects that many characters to the right of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] words | Selects that many words to the right of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] lines | Moves to the start of the line, selects down that many lines, and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] paragraphs | Selects that many paragraphs below the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] characters | Selects that many characters to the left of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] words | Selects that many words to the left of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] lines | Moves to the start of the line, selects up that many lines, and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] paragraphs | Selects that many paragraphs above the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| select next [number] characters | Selects that many characters to the right of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select next [number] words | Selects that many words to the right of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select next [number] lines | Selects down that many lines from the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select next [number] paragraphs | Selects that many paragraphs below the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select previous / last [number] characters | Selects that many characters to the left of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select previous / last [number] words | Selects that many words to the left of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select previous / last [number] lines | Selects up that many lines from the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| select previous / last [number] paragraphs | Selects that many paragraphs above the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| unselect that / clear selection | Drops the selection with a Right Arrow key press, leaving the cursor at the right-hand end of what was selected. Fires only when it is the whole utterance. | Nothing is deleted; with nothing selected the cursor simply moves one character right |
| tab | Presses the Tab key once. Fires only when it is the whole utterance. | "tab [number]" presses it that many times |

### Text Formatting

| Say this | What happens | Notes |
|---|---|---|
| uppercase | Converts the selection to UPPERCASE. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "upper case" and "uppercase that" also work |
| all caps that | Converts the selection to UPPERCASE. Applies only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. |  |
| lowercase | Converts the selection to lowercase. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "lower case" and "lowercase that" also work |
| no caps that | Converts the selection to lowercase. Applies only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. |  |
| capitalize | Capitalizes the first letter of the selection and lowercases the rest. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "capitalize that" also works |
| cap that | Capitalizes the first letter of the selection and lowercases the rest. Applies only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. |  |
| title case | Converts the selection to Title Case |  |
| snake case | Converts the selection to snake_case |  |
| camel case | Converts the selection to camelCase |  |
| pascal case | Converts the selection to PascalCase |  |
| kebab case | Converts the selection to kebab-case |  |
| compress | Removes the spaces from the selection, joining the words together. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| bold text | Bolds the selection (Ctrl+B) | "bold that" also works. Works in apps that support rich text |
| italics | Italicizes the selection (Ctrl+I). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "italicize that" also works. Works in apps that support rich text |
| underline | Underlines the selection (Ctrl+U). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "underline that" also works. Works in apps that support rich text |
| parentheses [text] | Wraps the selection in ( ), inserts an empty ( ) pair, or inserts the spoken text wrapped | "parentheses hello" gives "(hello)" |
| brackets [text] | Wraps the selection in [ ], inserts an empty pair, or wraps the spoken text |  |
| braces [text] | Wraps the selection in { }, inserts an empty pair, or wraps the spoken text |  |
| angle brackets [text] | Wraps the selection in < >, inserts an empty pair, or wraps the spoken text |  |
| quotes [text] | Wraps the selection in double quotes, inserts an empty pair, or wraps the spoken text |  |
| single quotes [text] | Wraps the selection in single quotes, inserts an empty pair, or wraps the spoken text |  |
| bold next [number] characters | Selects that many characters to the right of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold previous / last [number] characters | Selects that many characters to the left of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold next [number] words | Selects that many words to the right of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold previous / last [number] words | Selects that many words to the left of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold next [number] lines | Selects down that many lines from the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold previous / last [number] lines | Selects up that many lines from the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold next [number] paragraphs | Selects that many paragraphs below the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| bold previous / last [number] paragraphs | Selects that many paragraphs above the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize next [number] characters | Selects that many characters to the right of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize previous / last [number] characters | Selects that many characters to the left of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize next [number] words | Selects that many words to the right of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize previous / last [number] words | Selects that many words to the left of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize next [number] lines | Selects down that many lines from the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize previous / last [number] lines | Selects up that many lines from the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize next [number] paragraphs | Selects that many paragraphs below the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| italicize previous / last [number] paragraphs | Selects that many paragraphs above the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline next [number] characters | Selects that many characters to the right of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline previous / last [number] characters | Selects that many characters to the left of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline next [number] words | Selects that many words to the right of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline previous / last [number] words | Selects that many words to the left of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline next [number] lines | Selects down that many lines from the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline previous / last [number] lines | Selects up that many lines from the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline next [number] paragraphs | Selects that many paragraphs below the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| underline previous / last [number] paragraphs | Selects that many paragraphs above the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. Works in apps that support rich text |
| uppercase next [number] characters | Selects that many characters to the right of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase previous / last [number] characters | Selects that many characters to the left of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase next [number] words | Selects that many words to the right of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase previous / last [number] words | Selects that many words to the left of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase next [number] lines | Selects down that many lines from the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase previous / last [number] lines | Selects up that many lines from the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase next [number] paragraphs | Selects that many paragraphs below the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| uppercase previous / last [number] paragraphs | Selects that many paragraphs above the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "upper case" also works |
| lowercase next [number] characters | Selects that many characters to the right of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase previous / last [number] characters | Selects that many characters to the left of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase next [number] words | Selects that many words to the right of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase previous / last [number] words | Selects that many words to the left of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase next [number] lines | Selects down that many lines from the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase previous / last [number] lines | Selects up that many lines from the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase next [number] paragraphs | Selects that many paragraphs below the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| lowercase previous / last [number] paragraphs | Selects that many paragraphs above the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50. "lower case" also works |
| capitalize next [number] characters | Selects that many characters to the right of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize previous / last [number] characters | Selects that many characters to the left of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize next [number] words | Selects that many words to the right of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize previous / last [number] words | Selects that many words to the left of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize next [number] lines | Selects down that many lines from the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize previous / last [number] lines | Selects up that many lines from the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize next [number] paragraphs | Selects that many paragraphs below the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| capitalize previous / last [number] paragraphs | Selects that many paragraphs above the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |

### Navigation

| Say this | What happens | Notes |
|---|---|---|
| go [where] | Moves the cursor without touching the keyboard: go home / go end / go top / go bottom / go left / go right, with counts, word and paragraph units, and "then"-chained "grab" steps that select along the way | See the Navigation subsection of the Voice Commands section for the full move list |
| go up [number] lines / move up [number] lines | Moves the cursor up that many lines. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go down [number] lines / move down [number] lines | Moves the cursor down that many lines. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go up [number] paragraphs / move up [number] paragraphs | Moves the cursor back that many paragraphs (Ctrl and Up Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go down [number] paragraphs / move down [number] paragraphs | Moves the cursor forward that many paragraphs (Ctrl and Down Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go left [number] characters / move left [number] characters | Moves the cursor left that many characters. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go right [number] characters / move right [number] characters | Moves the cursor right that many characters. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go left [number] words / move left [number] words | Moves the cursor back that many words (Ctrl and Left Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go right [number] words / move right [number] words | Moves the cursor forward that many words (Ctrl and Right Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 50 |
| go to home / go to the beginning of the line | Moves the cursor to the start of the current line (Home). Fires only when it is the whole utterance. | "go to the start of the line" also works |
| go to the top / go to the top of the document | Moves the cursor to the very start of the document (Ctrl+Home). Fires only when it is the whole utterance. | "go to the beginning of the document" and "go to the start of the document" also work |
| go to the bottom / go to the bottom of the document | Moves the cursor to the very end of the document (Ctrl+End). Fires only when it is the whole utterance. | "go to the end of the document" also works |
| go to the end of the line / go to the end | Moves the cursor to the end of the current line (End). Fires only when it is the whole utterance. |  |
| go to the beginning of the word | Moves the cursor back one word (Ctrl and Left Arrow). Fires only when it is the whole utterance. | "go to the start of the word" also works; each app decides where a word boundary falls |
| go to the end of the word | Moves the cursor forward one word (Ctrl and Right Arrow). Fires only when it is the whole utterance. | Each app decides where a word boundary falls, so the cursor may land at the start of the next word |
| go to the beginning of the paragraph | Moves the cursor back one paragraph (Ctrl and Up Arrow). Fires only when it is the whole utterance. | "go to the start of the paragraph" also works |
| go to the end of the paragraph | Moves the cursor forward one paragraph (Ctrl and Down Arrow). Fires only when it is the whole utterance. | Each app decides where a paragraph boundary falls, so the cursor may land at the start of the next paragraph |
| move to the beginning of the selection | Presses Left Arrow, which drops the selection and leaves the cursor at its left-hand end. Fires only when it is the whole utterance. | With nothing selected the cursor simply moves one character left |
| move to the end of the selection | Presses Right Arrow, which drops the selection and leaves the cursor at its right-hand end. Fires only when it is the whole utterance. | With nothing selected the cursor simply moves one character right |
| scroll down [number] | Turns the mouse wheel down, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The number is how many wheel notches to send, up to 50; e.g. "scroll down 3". The count can be digits or spoken words, so "scroll down eleven" works whether your speech provider writes numbers as digits or as words. The command turns the wheel without moving the pointer, so it scrolls whatever a real wheel turn would scroll from where the pointer already sits |
| scroll up [number] | Turns the mouse wheel up, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The number is how many wheel notches to send, up to 50; e.g. "scroll up 3". The count can be digits or spoken words, so "scroll up eleven" works whether your speech provider writes numbers as digits or as words. |
| scroll left [number] | Turns the sideways mouse wheel left, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. The number is how many wheel notches to send, up to 50. The count can be digits or spoken words, so "scroll left eleven" works whether your speech provider writes numbers as digits or as words. |
| scroll right [number] | Turns the sideways mouse wheel right, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. The number is how many wheel notches to send, up to 50. The count can be digits or spoken words, so "scroll right eleven" works whether your speech provider writes numbers as digits or as words. |
| start scrolling down | Keeps turning the mouse wheel down until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The scroll keeps going with no further speech, so you can read a long page hands-free. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so, in case the stop command is not heard. If Windows refuses to move the wheel, which can happen over a window that runs as administrator, the scroll stops early and shows a different notification. Any scroll command you say while it runs replaces it, so only one scroll is ever going. |
| start scrolling up | Keeps turning the mouse wheel up until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The scroll keeps going with no further speech. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| start scrolling left | Keeps turning the sideways mouse wheel left until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| start scrolling right | Keeps turning the sideways mouse wheel right until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| stop scrolling | Stops a scroll that was started with "start scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The words are "stop scrolling" and not "stop" on its own. A command for the single word "stop" would take that word out of everything you dictate. Saying it when nothing is scrolling does nothing at all. |

### Punctuation and Symbols

| Say this | What happens | Notes |
|---|---|---|
| period | Types . inline during dictation | "full stop" also works |
| comma | Types , inline during dictation |  |
| colon | Types : inline during dictation |  |
| semicolon | Types ; inline during dictation |  |
| question mark | Types ? inline during dictation |  |
| exclamation point | Types ! inline during dictation | "exclamation mark" also works |
| apostrophe | Types ' inline during dictation |  |
| hyphen | Types - inline during dictation | "minus sign" also works |
| dash | Types an em dash (the long dash) inline during dictation |  |
| slash | Types / inline during dictation | "forward slash" also works |
| backslash | Types \\ inline during dictation |  |
| backtick | Types \` inline during dictation |  |
| at sign | Types @ inline during dictation |  |
| hashtag | Types # inline during dictation | "number sign" and "pound sign" also work |
| dollar sign | Types $ inline during dictation |  |
| percent | Types % inline during dictation |  |
| caret sign | Types ^ inline during dictation | Also fires if heard as "carrot sign" |
| ampersand | Types & inline during dictation | "and sign" also works |
| asterisk | Types * inline during dictation |  |
| underscore | Types _ inline during dictation |  |
| plus sign | Types + inline during dictation |  |
| equal sign | Types = inline during dictation |  |
| tilde | Types ~ inline during dictation | Also fires if heard as "tilda" |
| vertical bar | Types the pipe character inline during dictation | "pipe character" also works |
| ellipsis | Types ... inline during dictation | "dot dot dot" also works |
| space bar | Types a single literal space inline during dictation |  |
| open bracket | Types [ inline during dictation |  |
| close bracket | Types ] inline during dictation |  |
| open brace | Types { inline during dictation | "left brace" also works |
| close brace | Types } inline during dictation | "right brace" also works |
| open parentheses | Types ( inline during dictation | "left parentheses" also works |
| close parentheses | Types ) inline during dictation | "right parentheses" also works |
| open quotes | Types a double quote inline during dictation |  |
| close quotes | Types a double quote inline during dictation |  |
| open single quote | Types a single quote inline during dictation | "begin single quote" also works |
| close single quote | Types a single quote inline during dictation | "end single quote" also works |
| less than sign | Types < inline during dictation |  |
| greater than sign | Types > inline during dictation |  |
| euro sign | Types € inline during dictation |  |
| yen sign | Types ¥ inline during dictation |  |
| pound sterling sign | Types £ inline during dictation |  |
| copyright sign | Types © inline during dictation |  |
| registered sign | Types ® inline during dictation |  |
| section sign | Types § inline during dictation |  |
| paragraph sign | Types ¶ inline during dictation | "paragraph mark" also works |
| degree symbol | Types ° inline during dictation |  |
| multiplication sign | Types × inline during dictation |  |
| division sign | Types ÷ inline during dictation |  |
| colin | Mishear tolerance: inserts : when "colin" is the entire utterance. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| come | Mishear tolerance: inserts , when "come", "kama", "commer", or "come on" is the entire utterance. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |

### Application Switching

| Say this | What happens | Notes |
|---|---|---|
| x-ray activate [app name] | Brings the named application's window forward; when nothing by that name is open, Wheelhouse looks the name up among your installed programs and starts it | e.g. "x-ray activate outlook"; when more than one installed program matches the name, Wheelhouse starts none of them and lists them, so you can say the full name |
| browser | Brings your default web browser to the front. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Wheelhouse looks up which browser is your Windows default at the moment you speak |
| notepad | Brings Notepad to the front. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| switch to [app name] / go to [app name] / show [app name] | Brings the named application's window forward, starting it when nothing by that name is open. Needs the hotword first. | e.g. "x-ray switch to outlook"; the same action as "x-ray activate [app name]", including the program lookup |
| close [app name] / exit [app name] / quit [app name] | Brings the named application's window forward and then closes it (Alt+F4). Needs the hotword first. | Destructive -- requires the hotword |
| minimize [app name] | Brings the named application's window forward and then sends Windows and Down Arrow. Needs the hotword first. | That one keystroke has two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. |
| maximize [app name] | Brings the named application's window forward and then maximizes it (Windows and Up Arrow). Needs the hotword first. |  |

### System

| Say this | What happens | Notes |
|---|---|---|
| zoom in | Zooms in (Ctrl and plus) |  |
| zoom out | Zooms out (Ctrl and minus) |  |
| create tab | Sends Ctrl+N | New tab in most editors; note that in most browsers Ctrl+N opens a new window, not a tab |
| create window | Sends Ctrl+Shift+N | New window in editors; opens a private/incognito window in most browsers |
| x-ray close window | Closes the active window (Alt+F4) | Requires the hotword for safety |
| maximize | Maximizes the active window. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| minimize | Sends Windows and Down Arrow to the active window. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | That one keystroke has two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. "restore window" sends the same keystroke. |
| desktop | Shows the desktop (Windows+D). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| Windows settings | Opens the Windows Settings app | Also fires if heard as "Window settings" |
| close tab | Closes the current tab (Ctrl+W). Needs the hotword first. | What Ctrl+W closes is decided by the app |
| restore window | Sends Windows and Down Arrow to the active window. Fires only when it is the whole utterance. | The same keystroke as "minimize", so it has the same two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. |
| snap window to the left | Snaps the active window to the left half of the screen (Windows and Left Arrow). Fires only when it is the whole utterance. |  |
| snap window to the right | Snaps the active window to the right half of the screen (Windows and Right Arrow). Fires only when it is the whole utterance. |  |
| snap window to the top | Snaps the active window to the top of the screen (Windows, Alt and Up Arrow). Fires only when it is the whole utterance. |  |
| snap window to the bottom | Snaps the active window to the bottom of the screen (Windows, Alt and Down Arrow). Fires only when it is the whole utterance. |  |
| minimize all windows | Minimizes every open window (Windows+M). Fires only when it is the whole utterance. | "minimize all" does the same |
| show task switcher / list all windows / show all windows | Opens Task View, the Windows overview of every open window (Windows+Tab). Fires only when it is the whole utterance. |  |
| keyboard | Toggles the Windows on-screen touch keyboard (Windows, Ctrl and O). Fires only when it is the whole utterance. | One word, both directions: it opens the keyboard when closed and closes it when open. The earlier show and hide phrasings were removed because they promised a direction the toggle cannot deliver |
| search windows for [words] | Opens Windows Search and types what you say into it. Needs the hotword first. |  |
| search on google for [words] / search for [words] | Opens a Google search for the words you say in your default browser. Needs the hotword first. | "search [words]" without "for" does the same |
| search on bing for [words] | Opens a Bing search for the words you say in your default browser. Needs the hotword first. |  |
| search on youtube for [words] | Opens a YouTube search for the words you say in your default browser. Needs the hotword first. |  |

### Voice Element Clicking

| Say this | What happens | Notes |
|---|---|---|
| click [name] | Clicks the button, link, menu item, or other control with that name; add a role word to narrow the search, or give the overlay number instead of a name while the numbered overlay is showing. Needs the hotword first, as "x-ray click cancel". | See the Voice Element Clicking subsection of the Voice Commands section |
| show numbers | Paints a number on every clickable control in the front window | Numbers stay up until you say "hide numbers". "apply numbers" also works. "show numbers in the report" on its own is typed as ordinary text, not treated as this command. |
| hide numbers | Removes the numbers | "dismiss numbers" also works. "hide numbers on the chart" on its own is typed as ordinary text, not treated as this command. |
| right click [name] / double click [name] | Clicks the named control with a right click or a double click instead of a normal click; also works with a number while the numbered overlay is showing ("right click 3"). Presses a real mouse click at the control's center, with the same checks as a normal click. Useful where a normal click is not enough: File Explorer items open on a double click, and a right click opens the context menu. |  |

### Mouse Grid

| Say this | What happens | Notes |
|---|---|---|
| show grid | Lays a numbered three-by-three grid over the screen the front window is on, for moving the pointer by voice | Opening the grid removes the numbered overlay; the two are never on screen together. "apply grid" also works. "show grid lines on the chart" on its own is typed as ordinary text, not treated as this command. See the Mouse Control subsection of the Voice Commands section |
| hide grid | Closes the grid without clicking anything | "dismiss grid" also works. "hide grid lines before printing" on its own is typed as ordinary text, not treated as this command. |
| grid next screen | Moves the open grid to the next monitor, restarted at full size |  |
| [number 1-9] | Redraws the open grid inside that cell, narrowing the target. With the grid closed, numbers are ordinary dictation and are typed normally. |  |
| number [1-9] | Same cell narrowing with the word "number" first ("number five"), which speech engines hear more reliably than a single word. With the grid closed, the phrase is ordinary dictation and is typed normally. |  |
| click / right click / double click | Clicks at the center of the grid's current cell and closes the grid; a number in the same utterance narrows first ("x-ray click 5") |  |
| mark | Pins the start point of a drag at the current cell center and restarts the grid so you can navigate to the destination |  |
| drag | Holds the left button at the marked point, moves gradually to the current cell center, and releases -- a complete drag and drop | Requires a "mark" first; without one, a notice explains the step |
| move here | Moves the pointer to the current cell center without pressing anything, for hover menus and tooltips |  |

### Wheelhouse Control

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening: Wheelhouse listens only while you hold the floating button | A notification confirms the switch |
| click to talk mode | Switches back to toggle listening (click to start, click to stop) -- the default |  |
| help | Opens the Wheelhouse Assistant (the official online help) in your browser. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Uses the gem_url setting under [ai.help]; if blanked, Wheelhouse says out loud that online help is not configured |
| patterns | Opens the Pattern Manager. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "pattern manager" also works; see "Special Commands" |
| learn my voice | Opens the voice-teaching window, where Wheelhouse learns how you sound so it stops missing short words | "calibrate my voice" also works; only the Distil-Whisper speech engine uses it; See "Teaching Wheelhouse your voice" in the Speech Engines section |
| x-ray fix | Sends the selected text to the configured AI server for grammar and polish, then replaces the selection with the corrected version | Requires the AI server to be configured and reachable; Wheelhouse speaks its progress and always preserves your original text on any failure |
| simplify | Rewrites the selected text in plain language, using shorter sentences and simpler words. Keeps every fact and leaves the layout alone -- line breaks, indentation, bullet marks, numbering, code lines and addresses come back unchanged. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Same AI server and same safeguards as "x-ray fix"; the selection comes back as plain text, so formatting applied in a word processor is lost |
| shorten | Rewrites the selected text more briefly, cutting repetition and padding. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Same AI server and same safeguards as "x-ray fix" |
| x-ray make formal | Rewrites the selected text in a formal register, avoiding contractions and casual wording | Same AI server and same safeguards as "x-ray fix" |
| pirate | Rewrites the selected text the way a pirate would say it. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Ships as a worked example: it is the same action as the three above with a different sentence in the pattern file. See "Special Commands" for writing your own. |
| x-ray translate to [language] | Translates the selected text into the language you name, for example "x-ray translate to spanish" or "x-ray translate to brazilian portuguese". Keeps every fact and leaves names and numbers as they are. | Same AI server and same safeguards as "x-ray fix"; say the language in English and in lower case, as one or more plain words with no punctuation. How good the translation is depends on the model you have configured. |
| x-ray cancel fix | Cancels an in-progress fix or rewrite |  |
| boost | Adds the selected text to the speech recognition hints. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | See "Special Commands" -- on the default engine this saves the hint but does not apply it until you opt in |

## Configuration Reference

### General

**SPEECH_WEBSOCKET_HOST** *(default: `"127.0.0.1"`)* -- The internal address the speech engine uses to reach Wheelhouse; the default 127.0.0.1 means this computer only. Change only for the advanced setup where speech recognition runs on a second computer on your home network.

**REPLACEMENT_TIMEOUT_MS** *(default: `700`)* -- How long Wheelhouse waits after you stop speaking, in milliseconds, before deciding a correction phrase is complete. Raise to 900-1000 if corrections fire before you finish (common on slower machines); lower slightly if responses feel sluggish.

**COMMAND_TIMEOUT_MS** *(default: `700`)* -- How long Wheelhouse waits after you stop speaking, in milliseconds, before deciding a command phrase is complete. Raise to 900-1000 if commands fire before you finish (common on slower machines); lower slightly if responses feel sluggish.

**GREEDY_TIMEOUT_MS** *(default: `5000`)* -- A longer wait, in milliseconds, for commands that intentionally keep listening for more words. Rarely needs changing.

**COMMAND_COMPLETION_WAIT_MS** *(default: `1000`)* -- A short pause, in milliseconds, after a command finishes so a fast follow-up does not collide with it. Raise on a slow machine if back-to-back commands step on each other.

**ENABLE_AUDIO_SUPPRESSION** *(default: `true`)* -- Pause listening while computer audio is playing. Turn off only if you want Wheelhouse listening during playback; expect more misrecognitions, because the microphone picks up the audio.

**ENABLE_SONOS_SUPPRESSION** *(default: `true`)* -- Pause listening while Sonos music is playing. Turn off only if you want Wheelhouse listening during playback; expect more misrecognitions, because the microphone picks up the audio.

**ENABLE_IDLE_SUPPRESSION** *(default: `true`)* -- Pause listening after the computer sits idle. Turn off only if you never want idle pauses; the Idle Monitor plugin controls the timing.

**LOG_FILE** *(default: `""`)* -- Where the activity log goes; empty means the standard log location. Change only when a support conversation asks you to.

**LOG_LEVEL** *(default: `"INFO"`)* -- How detailed the activity log is. Change only when a support conversation asks you to.

**LOG_TRANSCRIPTS** *(default: `false`)* -- A privacy setting: false keeps the words you dictate and your clipboard contents out of the log files (only text lengths are noted). Set true only while troubleshooting recognition, then turn it back off; while on, everything you dictate, including passwords, accumulates in the logs.

**SIDE_OFFSET** *(default: `10`)* -- Width in pixels of the left-edge screen zone where the mouse thumb wheel adjusts brightness instead of volume. Raise it if the brightness zone is hard to hit.

**BRIGHTNESS_INCREMENT** *(default: `1.0`)* -- The size of each thumb-wheel brightness adjustment step. Raise for faster, coarser changes; lower for finer control.

**VOLUME_INCREMENT** *(default: `0.5`)* -- The size of each thumb-wheel volume adjustment step. Raise for faster, coarser changes; lower for finer control.

**FLOATING_BUTTON_SIZE** *(default: `50`)* -- Size in pixels of the small on-screen status button. Dragging the button's outer edge, or holding Ctrl and rolling the mouse wheel over it, writes this setting.

**FLOATING_BUTTON_POS** *(default: `[100, 100]`)* -- Screen position of the small on-screen status button, as [x, y] pixels from the top-left of the desktop. Dragging the button writes this setting, and so does resizing it, because the button grows and shrinks around its own centre.

**FLOATING_BUTTON_VISIBLE** *(default: `true`)* -- Whether the small on-screen status button is shown. Set true for an always-visible microphone click target, especially handy in push-to-talk mode.

**SPEECH_ENABLED_ON_STARTUP** *(default: `true`)* -- Whether Wheelhouse starts listening as soon as it launches. Set false to turn the microphone on manually each session.

**SHOW_SPEECH_PULSE** *(default: `true`)* -- Pulse the tray icon while Wheelhouse hears you -- a useful yes-I-can-hear-you signal. Turn off only if the animation distracts.

**SPATIAL_SOUND_EXEC** *(default: `""`)* -- Path to the small free NirSoft helper tool used for voice switching of Dolby Atmos spatial sound; empty means the feature is off. Fill in the tool path only if you use Dolby Atmos and have that tool installed; everyone else can ignore it.

**SPATIAL_SOUND_FORMAT** *(default: `"Dolby Atmos for home theater"`)* -- The spatial sound format name passed to the helper tool. Only matters if SPATIAL_SOUND_EXEC is set.

### [brightness_coordinator]

**software_dimmer** *(default: `"gamma_dimmer"`)* -- The software dimming method used once hardware brightness is as low as it goes. Valid values: "gamma_dimmer" (darkens through the graphics card), "overlay" (a translucent overlay window), or "flux" (drives a companion dimming app via hotkeys). Change only if dimming misbehaves with your monitor setup.

**unwinding_threshold** *(default: `10`)* -- Currently has no effect -- Wheelhouse hands control back to the hardware only once software dimming is fully undone, whatever this is set to.

**flux_transition_percent** *(default: `2`)* -- Percent of brightness per simulated hotkey press when driving a companion dimming app.

**flux_dim_hotkey** *(default: `["alt", "pagedown"]`)* -- The shortcut pressed to drive the companion dimming app's dim action. Change only if you remapped the app's own hotkeys.

**flux_brighten_hotkey** *(default: `["alt", "pageup"]`)* -- The shortcut pressed to drive the companion dimming app's brighten action. Change only if you remapped the app's own hotkeys.

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

**enabled** *(default: `true`)* -- Turns the Idle Monitor plugin on or off; it pauses listening when you step away and resumes when you return or say the wake word. Almost everyone should leave this on.

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

**enabled** *(default: `true`)* -- Turns wake-word listening on or off; after an idle pause you can wake Wheelhouse by saying its wake word out loud.

**keyword** *(default: `"computer"`)* -- The wake word.

**sensitivity** *(default: `0.5`)* -- Wake-word detection sensitivity, range 0-1. Lower it if saying the wake word often fails to wake Wheelhouse; raise it if ordinary conversation keeps waking it by accident.

**mode** *(default: `"idle_recovery"`)* -- What the wake word is used for -- waking Wheelhouse from an idle pause. Valid values: "idle_recovery".

**model_dir** *(default: `"../shared/data/wake_words"`)* -- Where the wake-word listening model lives on disk; set by the installer, do not change it.

### [ui_actions.timing]

**clipboard_verification_timeout_ms** *(default: `250`)* -- How long, in milliseconds, to wait for the clipboard to verify during text insertion. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**clipboard_operation_delay_ms** *(default: `50`)* -- Delay, in milliseconds, between clipboard operations during text insertion. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**selection_clear_delay_ms** *(default: `20`)* -- Delay, in milliseconds, after clearing a selection during text insertion. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**context_gather_delay_ms** *(default: `10`)* -- Delay, in milliseconds, before gathering the text context around the caret. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**post_paste_delay_ms** *(default: `30`)* -- Delay, in milliseconds, after pasting text into the target application. On older or heavily loaded machines, raising this can fix text that arrives garbled, half-pasted, or out of order.

**utterance_clipboard_timeout_seconds** *(default: `60.0`)* -- How long, in seconds, a copied utterance stays available for the paste-that style of command.

### [ui_actions.verified_unicode]

**max_chars** *(default: `50`)* -- Dictations up to this length are typed directly, character by character, avoiding your clipboard; longer ones go through the clipboard. Lower it if a particular app mishandles direct typing; raise it to have more dictations bypass the clipboard.

### [ui_actions.foreground_check]

**same_process_browser_names** *(default: `["brave.exe", "brave_beta.exe", "chrome.exe", "chromium.exe", "msedge.exe", "edge.exe", "vivaldi.exe", "opera.exe", "operagx.exe", "arc.exe"]`)* -- The web browsers Wheelhouse recognizes (browsers manage their windows in an unusual way); all the mainstream ones are already listed.

**same_process_browser_names_extend** *(default: `[]`)* -- Adds an unusual browser to the recognized list without retyping the built-ins.

### [ui_actions.text_target]

**allow_class_names_extend** *(default: `[]`)* -- Extends the built-in list of window classes allowed to receive dictation. Most people should use the built-in approval prompt instead -- when Wheelhouse is unsure about a text box, it asks on screen and remembers your answer.

**deny_control_types_extend** *(default: `[]`)* -- Extends the built-in list of control types denied dictation. Most people should use the built-in approval prompt instead -- when Wheelhouse is unsure about a text box, it asks on screen and remembers your answer.

**deny_class_names_extend** *(default: `[]`)* -- Extends the built-in list of window classes denied dictation. Most people should use the built-in approval prompt instead -- when Wheelhouse is unsure about a text box, it asks on screen and remembers your answer.

**browser_process_names_extend** *(default: `[]`)* -- Extends the built-in list of browser process names used by the dictation safety check. Most people should use the built-in approval prompt instead -- when Wheelhouse is unsure about a text box, it asks on screen and remembers your answer.

### [speech]

**interaction_mode** *(default: `"toggle"`)* -- The microphone interaction mode: toggle keeps the microphone on until you turn it off; push_to_talk listens only while you hold the floating button, muting system audio during the hold. Valid values: "toggle" or "push_to_talk". You can also switch by voice (push to talk mode / click to talk mode) without editing anything.

**ptt_safety_timeout_seconds** *(default: `30`)* -- In push-to-talk mode, automatically releases the microphone if a hold gets stuck. Raise it if you routinely dictate longer than 30 seconds in one hold.

**notify_on_revision** *(default: `false`)* -- Show a small notice when the speech engine revises its guess at what you said.

### [stt]

**last_provider** *(default: `"parakeet_tdt"`)* -- Which speech-to-text engine Wheelhouse uses; you normally switch engines from the tray menu, and Wheelhouse writes your choice here for you, which is why it is called the last provider. Valid values: "parakeet_tdt" (local, offline, no account), "distil_medium_en" (local, runs on an NVIDIA graphics card), or "google_stt" (Google Cloud; needs an account, sends audio to Google).

### [stt.google]

**credentials_file** *(default: `""`)* -- Full path to the Google service-account key file (the JSON file downloaded during Google Cloud setup). Type the path here yourself, doubling each backslash, and restart Wheelhouse; when this is empty, the GOOGLE_APPLICATION_CREDENTIALS environment variable is used instead.

### [ai]

**enabled** *(default: `true`)* -- The master switch for all AI features; today this means dictation text correction (it also gates the in-app help chat, which is currently disabled). New installs leave it off unless you chose the AI helper during setup.

**knowledge_base** *(default: `"knowledge/wheelhouse_help.md"`)* -- The document the in-app help assistant would consult; because the in-app help chat is currently disabled, this setting has no effect today.

### [ai.server]

**base_url** *(default: `"http://127.0.0.1:8781/v1"`)* -- The address of the AI server Wheelhouse talks to, using the standard OpenAI-style interface; empty leaves AI off. Any OpenAI-compatible address works, local or hosted; the installer's AI-helper choice fills in Google's Gemini address.

**model** *(default: `"gemma-4-e4b"`)* -- The model name to request from the AI server. Change it to whatever model your server has installed.

**kind** *(default: `"local"`)* -- Whether the AI server is on your own machine or out on the internet, which frames the privacy tradeoff: with a local server, the text being corrected never leaves your computer. Valid values: "local" or "cloud". This setting does not decide where your text is sent -- base_url above does that. Capitals and stray spaces are forgiven; anything else falls back to local and says so in the log.

**timeout_s** *(default: `30`)* -- Seconds Wheelhouse waits for the AI server before giving up on a request. Raise it if a slow local model keeps timing out.

### [ai.runtime]

**enabled** *(default: `false`)* -- Whether Wheelhouse starts its own model server. False means you start one yourself and point base_url at it. Valid values: true or false. When true, base_url above must name 127.0.0.1 or localhost and include a port -- the port Wheelhouse starts its server on comes from that address, so the two cannot drift apart. The installer sets this to true when it has downloaded a model for you. Set it to false if you would rather run your own server, or point Wheelhouse at a hosted one.

**model_path** *(default: `""`)* -- The full path to the model file Wheelhouse loads. The installer writes this. Point it at a different model file to change which model answers. Nothing else has to change.

**binary_dir** *(default: `""`)* -- The folder holding llama-server.exe, the program that runs the model. The installer writes this. Change it if you moved the llama.cpp build, or to use a build made for different graphics hardware.

**context_size** *(default: `8192`)* -- How much text the model can consider at once, counted in tokens. Raise it if you correct or rewrite long passages and the reply comes back cut short. A larger value uses more memory.

**gpu_layers** *(default: `99`)* -- How much of the model to place on the graphics card. Valid values: 99 places the whole model on the graphics card; 0 runs it entirely on the processor, which the installer chooses for a machine with enough system memory but no suitable graphics card. Values in between split it. Lower it if the server fails to start because the graphics card is out of memory.

**startup_timeout_seconds** *(default: `90`)* -- How long Wheelhouse waits for its model server to report itself ready before giving up and leaving the AI features off. Raise it if a large model on a slow disk is still loading when Wheelhouse stops waiting.

### [ai.help]

**gem_url** *(default: `"https://chatgpt.com/g/g-6a5ab92068d0819198db2a83135b9540-wheelhouse"`)* -- The web address the wheelhouse-help-online voice command opens in your browser; if you blank it out, the command answers out loud that online help is not configured.

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

**walk_deadline_ms** *(default: `2500`)* -- How long, in milliseconds, Wheelhouse searches a window for the control a spoken click names before giving up. Raise it on a slow machine if clicks time out in complex windows.

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

Generated: 2026-07-31 for the v1.0.7 release
