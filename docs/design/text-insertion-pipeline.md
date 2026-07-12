# WheelHouse Text Insertion Pipeline

> **Status:** Current
> **Date:** 2026-03-29
> **Supersedes:** `clipboard-race-fix-plan.md` (incorporated), portions of `speech-pipeline-analysis.md` (Input Process sections)

---

## 1. Overview

WheelHouse converts spoken words into text appearing in whatever application you're using -- Notepad, VS Code, a browser, a terminal. The text insertion pipeline is the system that takes a recognized word and gets it into the right place with correct spacing, capitalization, and punctuation.

**What this means for you:**

- **Works everywhere:** Text appears in any Windows application, including browsers, Electron apps, Flutter apps, and terminals.
- **Hands-free editing:** Spacing, capitalization after periods, and clipboard management happen automatically.
- **No clipboard loss:** Your clipboard is saved before dictation and restored afterward. Copy something, dictate, paste -- it's still there.
- **Fast:** After the first word of an utterance, each subsequent word inserts in ~1ms.

### 1.1 The Problem This Solves

Windows has no universal "insert text at cursor" API. Unlike mobile keyboards that have direct text input channels, desktop applications receive text through keyboard events or clipboard paste. WheelHouse must:

1. Figure out what application is focused
2. Read the text around the cursor (for spacing/capitalization decisions)
3. Format the word with correct spacing
4. Get the text into the application without disrupting the user's clipboard

Each of these steps has failure modes that vary by application. The pipeline handles this through a strategy pattern with graduated fallbacks.

---

## 2. Architecture

### 2.1 Component Overview

```
UIActionHandler (orchestrator)
    |
    +-- InsertionRouter (selects strategy based on app type)
    |       |
    |       +-- StandardStrategy (most apps)
    |       |       +-- ShadowBufferStrategy (fast path: cached UIA state)
    |       |       +-- ClipboardFallbackStrategy (slow path: clipboard round-trip)
    |       |               +-- TextPattern fast path (UIA context, ~0.4ms)
    |       |               +-- Clipboard gather_context (sequence polling, ~2-50ms)
    |       |
    |       +-- FlutterStrategy (Flutter apps: same as Standard, SendKeys API)
    |       +-- SimplePasteStrategy (last resort: direct paste)
    |
    +-- ShadowBufferManager (cached text state from UIA)
    +-- TextPerfector (spacing and capitalization logic)
    +-- ClipboardOperations (verified paste, context gathering)
    +-- UtteranceClipboardManager (save/restore clipboard per utterance)
    +-- WindowFocusManager (focus tracking and restoration)
```

### 2.2 Word Insertion Flow

When you say "now is the time," each word follows this path:

```
"now" (first word)                    "is" "the" "time" (subsequent words)
    |                                      |
    v                                      v
InsertionRouter                       InsertionRouter
    |                                      |
    v                                      v
StandardStrategy                      StandardStrategy
    |                                      |
    v                                      v
ShadowBufferStrategy                  ShadowBufferStrategy
    | buffer invalid                       | buffer VALID (cached)
    v                                      v
synchronize() via UIA (~2ms)          get_context() (~0ms, memory read)
    |                                      |
    v                                      v
TextPerfector ("Now")                 TextPerfector (" is", " the", " time")
    |                                      |
    v                                      v
verified_paste() via clipboard        verified_paste() via clipboard
```

The first word pays a one-time cost for UIA synchronization. Every subsequent word in the same utterance uses the cached buffer -- a memory read, not a system call.

---

## 3. Strategy Selection

The InsertionRouter examines the focused control and selects a strategy:

| Condition | Strategy | Why |
|-----------|----------|-----|
| No focusable control | SimplePaste | Nothing to inspect, just paste and hope |
| Flutter app | FlutterStrategy | Flutter's accessibility layer requires UIA SendKeys instead of SendInput for keyboard events |
| Everything else | StandardStrategy | ShadowBuffer fast path with clipboard fallback |

StandardStrategy is a graduated fallback:

1. **Try ShadowBufferStrategy** -- uses cached UIA state from the first word. If the buffer is valid, context comes from memory (~0ms). If invalid, synchronizes via UIA TextPattern (~2ms).
2. **Fall back to ClipboardFallbackStrategy** -- if ShadowBuffer sync fails (app doesn't support UIA TextPattern), gathers context via clipboard round-trip.

ClipboardFallbackStrategy itself has a fast path:

1. **Try TextPattern context read** (~0.4ms) -- reads 2 characters before the cursor via UIA without touching the clipboard.
2. **Fall back to clipboard gather_context** -- selects text with Shift+Left, copies with Ctrl+C, reads clipboard. Uses adaptive sequence polling (~2-50ms) instead of fixed delays.

---

## 4. ShadowBuffer: Why the First Word Is the Only Slow One

### 4.1 The Problem

Reading text context (what characters precede the cursor) is expensive if done through the clipboard. It requires:
- Shift+Left (select text)
- Ctrl+C (copy to clipboard)
- Read clipboard
- Right arrow (deselect)

This takes 50-150ms and disrupts the clipboard. Doing it for every word in a sentence would be visible to the user.

### 4.2 The Solution

ShadowBufferManager maintains a local copy of the focused control's text, cursor position, and selection state. It synchronizes once via UIA TextPattern on the first word, then updates locally as text is inserted.

```python
# First word: full UIA sync (~2ms)
synchronize() -> buffer = "The quick brown fox", cursor_pos = 19

# Second word: local update (~0ms)
get_context() -> {'preceding_chars': 'ox', 'has_selection': False}
update_after_insertion(" jumps") -> cursor_pos = 25
```

The buffer is invalidated when the HID listener detects keyboard or mouse input (the user interacted with the app directly), forcing a re-sync on the next utterance.

### 4.3 The GetCaretRange Optimization

The original synchronization used `MoveEndpointByRange` on UIA TextRange objects to calculate cursor position. This took **500ms** on some controls (notably Windows 11's RichEditD2DPT in Notepad) due to overhead in the Python `uiautomation` library's wrapper layer.

The fix uses `TextPattern2.GetCaretRange()` via raw comtypes COM pointers, bypassing the wrapper:

| Approach | Time | Notes |
|----------|------|-------|
| `MoveEndpointByRange` (wrapped) | 502ms | Python wrapper adds ~500ms overhead |
| `GetCaretRange` (raw comtypes) | 2ms | Direct COM call, same result |
| `MoveEndpointByRange` (raw comtypes) | 0.3ms | The wrapper was the bottleneck, not the API |

**Implementation:** `ShadowBufferManager.synchronize()` tries `TextPattern2.GetCaretRange()` first via the raw `tp2.pattern` comtypes pointer. If `TextPattern2` is unavailable, it falls back to the old `MoveEndpointByRange` approach.

**Key insight from benchmarking:** The 500ms was not in Windows, not in the RichEdit control, and not in COM. It was in the Python `uiautomation` library's `TextRange` wrapper class. The same `MoveEndpointByRange` call takes 0.3ms when called on raw comtypes pointers. This was only discovered by measuring each component independently -- the obvious assumption (COM cross-process overhead) was wrong.

---

## 5. Clipboard Operations

### 5.1 Verified Paste

All text insertion ultimately uses the clipboard: copy text to clipboard, Ctrl+V to paste. `verified_paste()` adds reliability:

1. Copy text to clipboard (`pyperclip.copy`)
2. **Verification loop** -- poll clipboard until content matches (250ms timeout). If another process overwrites the clipboard, re-copy up to 3 times.
3. Restore window focus
4. Execute paste (SendInput for normal apps, UIA SendKeys for Flutter)
5. Post-paste delay (30ms) -- ensures the application consumes the clipboard before restoration

### 5.2 Context Gathering with Adaptive Polling

When the clipboard fallback path needs to read text around the cursor, it uses `gather_context()`:

1. Set a **sentinel value** on the clipboard (unique string)
2. Shift+Left (select 2 characters before cursor)
3. Capture clipboard sequence number (`GetClipboardSequenceNumber`)
4. Ctrl+C (copy selection)
5. **Adaptive polling** -- wait for the clipboard sequence number to change (2-50ms typical), instead of a fixed 50ms sleep
6. Read clipboard -- if it changed from the sentinel, we got the selected text
7. Right arrow (restore cursor position)

The adaptive polling means: fast applications respond in 2-5ms (saving ~45ms per operation), while slow applications get up to 150ms timeout (more reliable than the old fixed 50ms). Flutter apps keep the fixed delay because their SendKeys API is too slow for polling to help.

The **sentinel mechanism** guards against concurrent clipboard writers. If another process writes to the clipboard between our Ctrl+C and our read, the sequence number changes but the content isn't ours. The sentinel check catches this.

### 5.3 TextPattern Fast Path

Before doing the clipboard round-trip, `ClipboardFallbackStrategy` tries reading context via UIA `TextPattern`:

```python
uia_context = read_context_via_text_pattern()  # ~0.4ms
if uia_context is not None:
    # Skip clipboard entirely -- use UIA context
else:
    # Fall back to clipboard gather_context
```

This skips the clipboard entirely for applications that expose UIA TextPattern (Notepad, some Chromium apps with accessibility mode, Word). For applications that don't support TextPattern, the clipboard path fires as before.

TextPattern is skipped for Flutter apps (UIA is unreliable there) and logs which path was taken at INFO level for debugging.

---

## 6. Utterance Clipboard Lifecycle

The clipboard is a shared global resource. WheelHouse borrows it temporarily for each utterance and restores the user's content afterward.

```
User copies "important data" to clipboard
    |
    v
Utterance starts (user starts speaking)
    | -> Save all clipboard formats (text, images, rich text)
    v
Word 1: clipboard used for paste, then available for next word
Word 2: clipboard used for paste
Word N: clipboard used for paste
    |
    v
Utterance ends (user stops speaking)
    | -> 100ms delay (ensures last paste is consumed)
    | -> Restore all saved clipboard formats
    v
User pastes -> "important data" (still there)
```

**Formats preserved:** All safe Win32 clipboard formats (text, Unicode, rich text, images). Handle-based formats (bitmaps, metafiles, palettes) are skipped to avoid access violations.

**Safety timeout:** If the utterance-end signal is never received (STT crash, WebSocket drop), a 1-second timer force-restores the clipboard.

**Copy/cut detection:** If the user says a copy or cut command during dictation, clipboard restoration is skipped (the user intended to change the clipboard).

---

## 7. Terminal Dictation (Focus-Redirect Editor)

Terminals get special treatment because:
- Ctrl+V can send SIGINT or paste control characters
- Shell prompts have complex editing semantics
- Command output shouldn't receive dictated text

The legacy `TerminalEditorStrategy` was deleted in wh-1g6er (Phase 4 of
wh-u3tj2). Terminal dictation now flows through the focus-redirect path:
a separate WheelHouse dictation editor opens, the user dictates into it
as if it were any other text target, and Enter submits the composed
text to the terminal in one verified paste.

### 7.1 Flow

```
User focuses Windows Terminal, starts dictating "list files"
    |
    v
SpeechProcessor receives WordEvent("list")
    |
    v
FocusRedirectPath.handle_dictation("list")
    |
    +-- FocusRedirectPolicy.should_redirect(terminal_hwnd)
    |       |
    |       +-- terminal at prompt? -> open_editor=True
    |       +-- terminal running a command? -> reject ("terminal_busy")
    |       +-- non-terminal focus? -> fall through to legacy dispatch
    |
    v
LogicMirror -> OPEN_REQUESTED
Buffer "list" in FocusChangeWordBuffer
Send "open editor for redirect" IPC to GUI
    |
    v
GUI opens TerminalDictationEditorWindow (PySide6 QPlainTextEdit)
    |
    +-- editor.show_editor(text="", hwnd=<terminal HWND>, rect=...)
    +-- emits editor_event_acked(op="show") -> LogicMirror OPEN_APPLIED
    +-- after a 50 ms QTimer, _focus_text_edit checks both Qt focus
        AND foreground HWND match -> emits editor_event_acked(
        op="focus_confirmed") -> LogicMirror FOCUS_CONFIRMED
    |
    v
FocusRedirectPath._drain_and_dispatch
    |
    +-- For each buffered word, send_request("intelligent_insert_text",
    |   params={"insertion_string": word, "target_hwnd": editor_hwnd})
    +-- Input Process resolves the request through the standard
    |   InsertionRouter. The editor's QPlainTextEdit advertises UIA
    |   TextPattern, so the predicate accepts; the router picks
    |   VerifiedUnicodeStrategy (short text) or StandardStrategy
    |   (long text). The strategy types into the editor exactly as
    |   it would type into Notepad.
    +-- accumulated_paste_chars updates so retraction works the same
    |   as for any other UIA target.
    |
    v
User dictates more words ("the", "files"). The path is at
FOCUS_CONFIRMED; handle_dictation returns False and the words flow
through the legacy _send_to_dictation path. Same UIA strategies,
same editor target.
    |
    v
User presses Enter in the editor
    |
    v
TerminalDictationEditorWindow.do_submit
    |
    +-- _submit_via_gui_paste(text=editor.toPlainText(),
    |                         hwnd=captured terminal HWND)
    +-- emits submit_started -> LogicMirror SUBMITTING
    +-- _paste_helper runs verified-paste against the terminal HWND
    +-- on success: emits submit_complete -> LogicMirror CLOSED
    +-- on abort:  emits submit_failed:<reason> -> LogicMirror ERROR
    |
    v
Terminal receives the perfected text in one paste; user sees the
command they would have typed manually.
```

### 7.2 Why an editor, not direct paste

The naive alternative is to detect "terminal at prompt" and paste each
word directly into the terminal HWND. That fails for two reasons.
First, every word triggers Ctrl+V at the shell prompt; an in-flight
shell tab-completion or running-command state would consume the
keystroke. Second, the user has no chance to review or correct the
recognition before it commits — once the words land at the shell
prompt, Ctrl+C is the only undo, and it kills any partial typing.

The editor solves both. The focus-redirect policy gates opening on
prompt-detector confirmation (`services/wheelhouse/ui/prompt_detector.py`)
so an editor never opens against a busy shell. The editor stays open
until the user presses Enter, so they can correct STT errors with the
standard retraction pipeline or by editing the QPlainTextEdit directly.
The single paste at submit time happens against a verified terminal
foreground, with the editor's GUI-side verified-paste helper
re-checking foreground before sending Enter.

### 7.3 Why the editor is a "normal" UIA target

Before wh-u3tj2, the editor had its own `TerminalEditorStrategy` in the
Input Process plus a `te_event` IPC carrying per-word verbatim flags.
That meant retraction, grapheme handling, foreground verification,
shadow buffer state, and clipboard accounting all had two code paths
(editor and normal). Bugs in one path went undetected by tests for the
other; the verbatim-flag plumbing in particular fought TextPerfector.

The wh-u3tj2 contract is "the editor is just a normal UIA target."
After wh-1g6er deleted the strategy and the verbatim plumbing, the
editor's `QPlainTextEdit` participates in the same predicate,
strategy, retract, and accumulated_paste_chars chains as Notepad or
VS Code. The Logic Process tracks the editor's session lifecycle via
the `EditorLifecycleEvent` contract in
`services/wheelhouse/shared/editor_lifecycle.py`; the Input Process
treats the editor's HWND as any other text target HWND. The slim
`TerminalEditorProxy` survives only as the IPC forwarder for
show/cancel/submit commands.

### 7.4 Failure modes

| Failure | Path response |
|---------|---------------|
| Terminal is running a command (no prompt) | Policy rejects with `terminal_busy`; word is consumed (not redirected to the focused control). The path's `_notify_user` hook logs the message at INFO and routes it through `SpeechNotifier.notify_dictation_drop` so the user sees a Windows toast titled "WheelHouse: Dictation rejected" (wh-mgbik.1). The same path covers other fail-closed reasons: `editor_already_open`, `submit_timeout`, `focus_lost`, and `dropped_word_count` overflow. |
| Editor open IPC dispatched but no `show` ack | Lifecycle timeout fires at 0.5 s; mirror moves to ERROR; word buffer fails closed. |
| Editor shown but focus never settles | `focus_pending` timeout at 0.5 s; mirror moves to ERROR; word buffer fails closed. |
| User clicks back to terminal (or another window) after focus_confirmed | The editor does not emit a follow-up `focus_lost` -- that signal only fires from the one-shot 50 ms post-show check. The next drained word's foreground re-check in `intelligent_insert_text` fails instead; the word is dropped and not credited to `accumulated_paste_chars`. The mirror stays in `FOCUS_CONFIRMED` until the user returns foreground and presses Enter, the editor is cancelled, or the session is otherwise torn down. |
| Foreground drifted between focus_confirmed and the per-word insert | Strategy's post-send foreground check fails; the word is NOT credited to `accumulated_paste_chars` (so retraction does not over-count). |
| User presses Enter before drain completes | The editor reads `_text_edit.toPlainText()` at that instant and submits whatever is there. Tracked as `wh-drain-submit-interlock` (Phase 4 follow-up); race is narrow in practice but not zero. |
| Submit helper aborts (invalid hwnd, foreground failure, partial send) | Editor emits `submit_failed:<reason>`; mirror moves to ERROR; the GUI shows a content-neutral failure toast and the editor closes. |
| Terminal HWND closes before submit (user closes the shell tab) | The GUI verified-paste helper detects the invalid HWND at submit time and emits `submit_failed:invalid_hwnd`; mirror moves to ERROR; the editor closes with a failure toast. Composed text in the editor is lost (not re-routed to another target). |
| Logic, Input, or GUI process crashes while a session is open | The launcher detects the dead child, sets the shutdown event, tears down the surviving children and shared memory, and exits unless an explicit `RESTART_FLAG_PATH` is present. The next launch starts a fresh trio; any open editor session is lost. There is no surviving Logic mirror to rehydrate. |
| Queue put fails between Input, Logic, and GUI (IPC drop) | The failure is logged and the queued message is dropped. If the dropped message was an ack the receiver was waiting for, the corresponding per-state lifecycle timeout (0.5 s open/focus, 5.0 s submitting) fires and the mirror moves to ERROR. |

---

## 8. Configuration

Text insertion timing lives in `services/wheelhouse/config.toml` under `[ui_actions.timing]`:

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `clipboard_verification_timeout_ms` | 250 | Max time to verify clipboard content matches after copy |
| `clipboard_operation_delay_ms` | 50 | Base timeout for adaptive sequence polling (actual wait adapts; 3x multiplier for max) |
| `selection_clear_delay_ms` | 20 | Delay after clearing a selection |
| `context_gather_delay_ms` | 10 | Delay between context-gathering operations |
| `post_paste_delay_ms` | 30 | Delay after Ctrl+V to ensure app consumes clipboard |

The utterance clipboard manager has additional timing in `utterance_clipboard_manager.py`:

| Parameter | Default | Purpose |
|-----------|---------|---------|
| Utterance end delay | 100ms | Wait for final paste to complete before restoring clipboard |
| Safety timeout | 1.0s | Force-restore clipboard if utterance-end signal lost |

---

## 9. Application Compatibility

| Application Type | Strategy | Context Method | Notes |
|-----------------|----------|---------------|-------|
| Notepad (Win11) | Standard | ShadowBuffer (TextPattern2) | Full UIA support, 2ms sync |
| VS Code | Standard | ShadowBuffer or Clipboard | TextPattern requires `editor.accessibilitySupport: "on"` |
| Chrome/Edge | Standard | Clipboard fallback | TextPattern support varies by page content |
| Word | Standard | ShadowBuffer (TextPattern) | Full UIA support |
| Flutter apps | Flutter | ShadowBuffer or Clipboard | UIA SendKeys for paste, TextPattern fast path skipped |
| Windows Terminal | Editor (focus-redirect) | Popup editor | Prompt detection gates editor opening |
| Legacy Win32 | Standard | Clipboard fallback | No TextPattern, clipboard is the only option |
| Java apps | Standard | Clipboard fallback | Typically no UIA TextPattern |
| Citrix/RDP | Standard | Clipboard fallback | Remote apps have limited UIA |

---

## 10. Key Design Decisions

### Why clipboard-based insertion instead of direct Win32 messages or UIA?

Windows offers several alternatives to clipboard paste, but none are universal:

| Method | Limitation |
|--------|-----------|
| `EM_REPLACESEL` (Win32 message) | Only works for Edit and RichEdit controls (Notepad, dialog boxes). Chrome, VS Code, WPF, Flutter, Java apps don't process it -- they render their own text areas. |
| `ValuePattern.SetValue` (UIA) | Replaces the *entire* control value, not insert-at-cursor. Also not widely supported. |
| `WM_CHAR` (character messages) | Widely supported but slow for multi-character strings and doesn't handle undo properly. |
| `ITextStoreACP` (Text Services Framework) | Designed for input methods, very complex to implement, inconsistent app support. |

`EM_REPLACESEL` is the most tempting -- it's a proper insert-at-cursor operation that handles undo and doesn't touch the clipboard. But it only works for the small set of native Win32 Edit controls. The apps where people spend most of their time (browsers, VS Code, Electron apps, terminals) don't support it. Adding it as another strategy tier was considered and rejected: the detection cost is negligible (~1us class name check) but the maintenance cost of another insertion path isn't justified when the clipboard approach already works at 1ms per word for these controls.

Ctrl+V works in every application because the application itself handles the paste. WheelHouse doesn't need to know the application's internal text model -- it just puts text on the clipboard and simulates the keyboard shortcut the user would press.

**Design principle:** The clipboard path is the safety net for every application. UIA is an optimization layer for *reading* context, not for writing text. You design the landing gear for the worst runway, not the best one.

### Why save/restore the clipboard instead of using a private clipboard?

Windows has only one clipboard. There's no API for a "private" clipboard that applications don't see. The alternative (using clipboard history APIs from Windows 10+) would be complex and still wouldn't solve the race condition for applications that read the clipboard asynchronously.

### Why adaptive polling instead of clipboard change notifications?

Windows provides `WM_CLIPBOARDUPDATE` for clipboard change notifications, but:
- It requires a message pump (window handle + message loop)
- The Input Process runs on a single thread processing IPC commands -- adding a message pump would require architecture changes
- `GetClipboardSequenceNumber` polling at 2ms intervals is simple, fast, and sufficient

---

## 11. Future Considerations

### Voice-Accessible Clipboard Manager

External clipboard managers (ArsClip, Ditto, Windows clipboard history) see every word WheelHouse pastes as a separate clipboard entry. This pollutes the user's clipboard history with dictation noise. These tools can't distinguish WheelHouse's mechanical clipboard use from intentional user copies because only WheelHouse has that context.

A WheelHouse-integrated clipboard manager could:

- **Filter noise at the source.** Only record clipboard changes from explicit copy/cut commands, never from dictation paste operations. The `UtteranceClipboardManager` already tracks this distinction via the `_skip_restore` flag.
- **Provide voice-accessible history.** Existing clipboard managers require mouse interaction to browse and select from history. Voice commands ("clipboard two", "clipboard search invoice") would be a genuine accessibility win.
- **Eliminate external tool conflicts.** No need to configure ArsClip to ignore WheelHouse or set minimum capture intervals.

**Existing infrastructure:** `UtteranceClipboardManager` already saves/restores multi-format clipboard contents per utterance. Extending it to maintain a persistent history of intentional copies would be incremental. Voice commands would use the existing pattern matching system.

**Not yet justified:** The current pain (ArsClip noise) has simpler workarounds (process exclusion, minimum interval settings). Build this when voice-accessible clipboard history is the goal, not as a reaction to tool noise.

---

## 12. Files Reference

| File | Purpose |
|------|---------|
| `ui/ui_action_handler.py` | Orchestrator: routes actions to specialists |
| `ui/router.py` | Strategy selection based on app context |
| `ui/strategies/specific.py` | Strategy implementations (Standard, Flutter, SimplePaste) |
| `ui/shadow_buffer.py` | Cached UIA text state with GetCaretRange optimization |
| `ui/uia_text_reader.py` | TextPattern/ValuePattern context reading |
| `ui/clipboard_operations.py` | Verified paste, context gathering with sequence polling |
| `ui/clipboard_sequence.py` | Win32 GetClipboardSequenceNumber wrapper |
| `ui/utterance_clipboard_manager.py` | Clipboard save/restore lifecycle |
| `ui/text_perfector.py` | Spacing and capitalization logic |
| `ui/context.py` | UI context capture (focused control, app detection) |
| `ui/window_focus_manager.py` | Window focus tracking and restoration |
| `ui/clipboard_poller.py` | Clipboard change detection for selections |
| `utils/clipboard_manager.py` | Multi-format clipboard save/restore context manager |
