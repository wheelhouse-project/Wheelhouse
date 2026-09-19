# Spoken phrase commands feasibility report

## Decision summary

WheelHouse can support phrase selection and removal in controls that expose a usable UI Automation TextPattern.

WheelHouse can also start a formatting command from that selection.

The application must perform the formatting operation.

UI Automation TextPattern does not provide a general command that makes text bold.

This feature would not work uniformly across Windows applications.

Controls that expose only ValuePattern can provide their plain text but cannot provide a selectable text range.

Controls that expose neither pattern provide no safe direct method.

The most practical product would therefore use UI Automation when it can and report that the operation is unavailable when it cannot.

This report does not recommend that WheelHouse build the feature.

## Verified findings from WheelHouse code

### Current UI Automation reads

`services/wheelhouse/ui/uia_text_reader.py` contains two read paths.

`read_context_via_text_pattern` requests TextPattern from the focused control.

It uses TextPattern2 when the control provides it.

The TextPattern2 path calls `GetCaretRange` on the raw COM pattern.

It then uses `DocumentRange`, `Clone`, `MoveEndpointByRange`, and `GetText` to read text before the caret.

The legacy path uses `GetSelection`, `DocumentRange`, `Clone`, `MoveEndpointByRange`, and `GetText`.

The function returns at most a small amount of preceding text and a selection flag.

It does not call `FindText`.

It does not call `TextRange.Select`.

It does not call `GetBoundingRectangles`.

`read_value_pattern_text` requests ValuePattern and returns its `Value` property.

ValuePattern returns one plain string.

It does not return a range, a caret position, or match rectangles.

The text-target predicate confirms an important compatibility gap.

It accepts an enabled Edit control even when TextPattern is absent.

It does not treat ValuePattern alone as proof that a control accepts text.

This means WheelHouse already encounters editable controls that may have no TextPattern range.

### Current insertion strategies

The insertion strategies receive a `UIContext`.

That context contains the focused UI Automation control, process name, class name, process identifier, Flutter flag, and terminal flag.

The strategies use this information to choose Unicode input, clipboard paste, Flutter handling, or rejection.

They also use the captured control and top-level window to check focus before and after input.

The strategies know the target control's identity.

They do not know the semantic contents of an arbitrary document unless TextPattern synchronization succeeds.

They do not contain a phrase search operation.

They do not contain a way to retain a text range across an ambiguity prompt.

The selection transformation code acts only on text that is already selected.

It copies that selection, transforms the string, and inserts replacement text.

It does not find the selection.

### Clipboard handling and the shadow buffer

The clipboard fallback does not copy the whole control.

`ClipboardOperations.gather_context` selects and copies up to two characters before the caret.

It also probes one character after the caret.

It uses arrow keys to return the caret to its prior position.

The code treats partial key delivery as unsafe because a failed arrow can leave text selected.

The caller then stops before pasting.

`clear_selection` can copy and delete the user's current selection before context gathering.

The code tries to restore that text after a clean failure before paste.

The code cannot restore it safely after every failure.

The shadow buffer can hold the full document after a successful TextPattern synchronization.

It also holds the caret position and selection length.

This full copy depends on the same TextPattern capability needed by the direct phrase method.

When clipboard context seeds the shadow buffer, the buffer contains only the nearby context and newly inserted text.

That partial buffer cannot support a document-wide phrase search.

The shadow buffer also represents cached state.

WheelHouse invalidates it after several kinds of uncertain input.

It should not be treated as the authority for a destructive phrase command.

### Existing numbered overlay

The numbered overlay starts with a UI Automation walk of interactive elements.

The walk records one bounding rectangle and one live control reference for each element.

The cross-process summary keeps the element identifier, number, name, role, bounds, and monitor identifier.

The GUI painter reads only each item's bounds and display number.

The painter can therefore draw a number over any supplied screen rectangle.

The existing end-to-end overlay cannot identify text matches without changes.

Its snapshot store retains clickable control references rather than text ranges.

Its number routing resolves a number to a snapshot item and then invokes or clicks that control.

Its UI Automation walker also filters for interactive elements.

Text positions inside one document are not separate UI Automation elements in this model.

The paint subsystem is reusable.

The walk, snapshot, schema, number routing, and action path are not directly reusable for text matches.

## Verified findings from `uiautomation` 2.0.29

The installed library defines `FindText` on `TextRange`.

`FindText` returns a smaller `TextRange` or `None`.

It accepts a backward-search flag and an ignore-case flag.

The installed library defines `GetText` on `TextRange`.

It returns the plain text represented by the range.

The installed library defines `Select` on `TextRange`.

It replaces the prior selection with the range.

The installed library defines `GetBoundingRectangles` on `TextRange`.

It returns screen-coordinate rectangles for the fully or partially visible lines in that range.

It does not return exactly one rectangle for every match.

One match can span several visible lines and produce several rectangles.

An off-screen match can have no useful visible rectangle.

WheelHouse would call `GetBoundingRectangles` separately on each matched range to obtain positions for that match.

The library's `FindText`, `GetText`, and `GetBoundingRectangles` wrappers do not add a fixed delay.

The library's `Select` wrapper sleeps for 0.5 seconds by default after the COM call.

The library's `MoveEndpointByRange` wrapper also sleeps for 0.5 seconds by default.

The same default applies to several other state-changing range methods.

WheelHouse's measured 500 ms caret read used the wrapper form of `MoveEndpointByRange`.

The installed library source explains the flat duration.

The wrapper sets `OPERATION_WAIT_TIME` to 0.5 seconds and calls `time.sleep` after the COM operation.

The QPlainTextEdit benchmark measured about 500 ms at every document size.

The raw COM form of the same endpoint move took about 0.013 ms at the 99th percentile when it used a TextPattern2 caret range.

The document `GetText` call took about 0.1 ms at the 99th percentile in that benchmark.

The old 500 ms result therefore does not predict a 500 ms `FindText` call.

The application UI Automation provider can still make any COM call slow or unresponsive.

## Feasible direct behavior

WheelHouse could request TextPattern from the focused control.

It could search `DocumentRange` with `FindText` for an exact or case-insensitive phrase.

It could call `GetText` on the result to verify the source text.

It could call `Select` to make the result the active selection.

It could send Delete only after it rechecked focus and the selected text.

It could send an application command such as Ctrl+B only after the same checks.

The formatting command would remain application-specific.

Plain-text controls such as Notepad cannot make text bold.

Code editors may assign Ctrl+B to a command that has nothing to do with rich-text formatting.

Rich-text editors such as Microsoft Word can apply bold to a selection.

TextPattern can report text attributes, but it has no general setter for bold.

Multiple matches require repeated searches over the remaining document range.

The search must advance past each accepted result.

WheelHouse must keep all match ranges inside the Input process because live COM objects cannot cross its process boundary.

WheelHouse must also treat those ranges as stale after document or focus changes.

## Beliefs that need verification: application coverage

The following table combines code evidence with an assessment.

The codebase did not contain current manual tests of this proposed operation in these applications.

This review did not launch any application because it used code only.

The assessment is therefore not a verified compatibility promise.

| Application or control | Evidence in this checkout | Feasibility assessment that still needs manual verification |
|---|---|---|
| Windows 11 Notepad | WheelHouse documents full TextPattern support and uses TextPattern2 for its modern control. | Phrase selection and removal should work. Bold cannot work because Notepad stores plain text. |
| WordPad | WheelHouse says WordPad uses a different RichEdit control from modern Notepad. No phrase-range test is recorded. | Phrase selection, removal, and Ctrl+B formatting should be feasible when WordPad is installed and its RichEdit provider exposes TextPattern. Current Windows releases may not include WordPad. |
| Microsoft Word | WheelHouse documents full TextPattern support. | Phrase selection and removal should work. Bold should work after selection through Word's command handling. Large and structured documents need performance and range-fidelity tests. |
| VS Code | WheelHouse documents that TextPattern requires `editor.accessibilitySupport` set to `on`. | The direct method should be limited to sessions where the focused editor exposes TextPattern. It should report unavailable otherwise. Bold is not a general text-format operation in a source editor. |
| Chrome and Edge text boxes | WheelHouse documents variable TextPattern support. It also documents browser Edit controls and Value-only or missing-pattern cases. | Some address bars, inputs, text areas, and content-editable controls may work. A simple field that exposes only ValuePattern cannot use direct range selection. A page document that exposes readable text must not be mistaken for an editable text box. Rich content-editable fields may support Ctrl+B, but ordinary text boxes do not. |
| Windows Terminal | WheelHouse identifies `TermControl` and redirects shell-prompt dictation into its own editor. The code does not use a terminal text range for editing. | The terminal surface should be treated as unsupported for direct destructive range actions until proven otherwise. It may expose readable screen text without exposing an editable range for the command line. Bold is not an applicable edit operation. |
| Explorer file-name edit boxes | WheelHouse accepts enabled Edit controls even without TextPattern. The checkout has no recorded Explorer phrase-range test. | A rename field may expose only ValuePattern through an Edit proxy. Full text could then be read, but no UI Automation range could be selected. The direct method should work only when the actual focused rename control also exposes TextPattern. Bold is not applicable. |
| Custom, Flutter, Java, canvas, or GPU-rendered editors | WheelHouse already uses clipboard or explicit approval when TextPattern is absent or unreliable. | The direct method will often be unavailable. A keyboard fallback may work in selected applications, but it cannot offer the same accuracy. |

Notepad, Word, and the WheelHouse QPlainTextEdit provide the strongest evidence for a first feasibility boundary.

VS Code and Chromium controls have conditional support.

Windows Terminal and Value-only Explorer rename fields should not be claimed as supported.

## Search time and absent-phrase time

An exact `FindText` search should normally take less than the old 500 ms caret read.

The reason is that `FindText` has no fixed wrapper sleep.

A search still crosses the process boundary into the application's UI Automation provider.

Its actual time can depend on document size, provider design, virtualization, and application load.

The existing QPlainTextEdit data only measures related range calls.

It does not measure `FindText`.

The available data supports an expectation of milliseconds for a responsive provider.

It does not support a hard maximum.

Selecting the result through the library wrapper would add 500 ms unless the caller passed a zero wait or used the raw COM method.

WheelHouse should not remove that wait without measuring whether target applications need a settle interval.

An exact absent phrase produces `None` from one `FindText` call.

WheelHouse could report absence as soon as that call returns.

A tolerant search would instead read the document text, normalize it locally, and report absence after the local scan finds no match.

That path adds one full `GetText` transfer and local string processing.

It should still avoid the fixed 500 ms wait.

A COM error, missing pattern, timeout, or incomplete provider response must not be reported as "phrase absent."

Those outcomes mean that WheelHouse could not search the control.

## More than one match

WheelHouse could number visible matches and ask the user to say a number.

Each match range could supply one or more rectangles through `GetBoundingRectangles`.

The overlay painter could place a number near one rectangle for each match.

The number should identify the whole match even when the match spans several lines.

Off-screen matches need a different treatment because they may have no rectangles.

WheelHouse could state the match count and ask for an ordinal.

It could also number only visible matches and say that more matches are outside the view.

Scrolling every match into view would move the user's document and would need its own safety rules.

The existing numbered overlay is not reusable as a complete feature.

Its current numbers refer to clickable UI Automation elements.

Its current number action clicks an element.

Only its rectangle placement and paint code are directly reusable.

The text-match state would need separate identity, staleness, and selection behavior.

## Spoken matching rules

The spoken phrase should not be required to match capitalization exactly.

STT often returns lowercase text.

The installed `FindText` method can ignore case.

That option does not ignore punctuation or normalize whitespace.

An exact case-insensitive search would still miss `Hello, world` when STT returns `hello world`.

A useful rule would compare Unicode-normalized, case-folded word sequences.

It would collapse runs of whitespace.

It would treat common spoken punctuation as optional separators between words.

It would preserve an offset map back to the original on-screen text.

It would require whole-word boundaries unless the user explicitly requested a partial word.

It would select the original characters, including punctuation that the rule associated with the phrase.

The rule must not discard arbitrary symbols inside identifiers, file names, code, URLs, or numbers.

For those targets, exact text or an explicit literal mode is safer.

Any normalization that creates several candidates must trigger the multiple-match choice.

WheelHouse should prefer an exact match over a normalized match.

It should state when it used a tolerant match before a destructive action.

## Fallbacks when TextPattern is unavailable

ValuePattern can answer whether a phrase occurs in a plain value.

It cannot select the occurrence.

WheelHouse could use the plain value to guide keyboard movement from a known endpoint.

That approach would depend on the control's arrow-key behavior and character counting.

A clipboard fallback could send Ctrl+A and Ctrl+C to obtain all selectable text.

WheelHouse does not currently have this document-wide operation.

The current clipboard context code reads only three nearby characters in total.

A document-wide copy would replace the user's current selection with the whole document selection.

Restoring the original caret and selection would be difficult without a text range.

After finding an offset, WheelHouse could send Ctrl+Home, arrow keys, and Shift+arrow keys to select the phrase.

This would lose accuracy across Unicode grapheme clusters, surrogate pairs, line endings, application-specific navigation, hidden text, and virtualized content.

It would also be slow for a distant match.

An application search command such as Ctrl+F provides another keyboard fallback.

Search behavior, punctuation handling, match order, and selection state differ across applications.

Escape may leave a match selected in one application and remove the selection in another.

The fallback cannot safely promise the same behavior across applications.

The safest general fallback is to report that direct phrase control is unavailable.

A keyboard or clipboard fallback should require an application-specific proof of behavior.

When a control exposes neither TextPattern nor ValuePattern, WheelHouse cannot distinguish an absent phrase from unreadable text.

It must report that it could not read the control.

## Failure modes and safety consequences

- Focus can move after the search and before selection.

- Focus can move after selection and before Delete or Ctrl+B.

- A Delete key can then remove text or an item in a different control.

- The document can change after the range was found.

- A stale range can select different text or fail.

- A tolerant match can choose punctuation or whitespace that the user did not intend to include.

- A tolerant match can collapse two distinct on-screen phrases into the same spoken form.

- A repeated phrase can be assigned a stale number after editing, scrolling, or layout changes.

- An off-screen match can have no rectangle and therefore no honest badge position.

- A UI Automation provider can expose readable document text for a non-editable page body.

- The same provider can expose only part of a virtualized document.

- A read-only control can allow selection but reject removal.

- Ctrl+B can toggle bold off when the chosen text is already bold.

- Ctrl+B can run an unrelated application command in a plain-text editor.

- A keyboard fallback can count Unicode characters differently from the application.

- A keyboard fallback can stop on a soft-wrapped line or move by a different text unit.

- Ctrl+A can select more than the focused field in a custom application.

- Ctrl+C can fail and leave the clipboard sentinel or old clipboard content in place.

- Clipboard ownership can change while WheelHouse is searching.

- A failed caret-restoration key can leave live text selected.

- A later paste or Delete can then remove text that the user wanted to keep.

- A partial Delete or replacement can leave the document in an unknown state.

- Elevated applications can reject synthetic input through Windows integrity rules.

- Password controls can expose restricted or empty values and must remain excluded.

The destructive path needs stronger checks than the read path.

It should recheck the top-level window, focused control, document state, selected source text, and requested action immediately before input.

It should fail closed when any check is uncertain.

It should not send compensating Delete or paste operations after a partial or ambiguous failure.

## Effort estimate

These estimates are engineering judgments.

They are not measurements from an implemented feature.

A narrow proof for exact, case-insensitive selection and removal in Notepad and Word would take about three to five developer days.

That proof would not establish broad product feasibility.

A reliable first release for proven TextPattern controls would take about four to eight developer weeks for one developer.

That estimate includes command routing, process-safe schemas, normalized matching with source offsets, duplicate handling, overlay adaptation, focus and staleness checks, regression tests, and manual application validation.

Rich-text formatting and application-specific fallbacks could extend the work beyond eight weeks.

The largest schedule risk is not the phrase search.

The largest schedule risk is proving that selection and the following destructive or formatting command still target the same text across different application providers.

## Conclusion

The capability is feasible as an opportunistic UI Automation feature.

It is not feasible as one uniform Windows-wide command with the current evidence.

TextPattern controls provide the required search and selection primitives.

Value-only and pattern-less controls require lower-accuracy, application-specific fallbacks or a clear unavailable result.

The feature should not claim support for an application until selection, removal, duplicate choice, focus drift, and failure recovery have been tested in that application.
