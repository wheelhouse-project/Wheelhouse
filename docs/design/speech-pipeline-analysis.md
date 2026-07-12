# WheelHouse Speech Processing Pipeline

> **Status:** Current
> **Date:** 2026-03-30
> **Supersedes:** Previous version of this document (analysis notes format)
> **Related:** `text-insertion-pipeline.md` (covers text insertion after this pipeline hands off), `stt-and-ai.md` (covers the STT providers, model delivery, and AI text processing)

---

## 1. Overview

WheelHouse listens to your voice and converts it into actions -- keyboard shortcuts, text insertions, window management, and more. The speech processing pipeline is the system that decides whether a word is a command to execute or text to type.

**What this means for you:**

- **Commands at utterance start:** Say "delete five" at the start of a phrase and it deletes five characters. Say "I want to delete five items" and the word "delete" is typed as text -- position determines intent.
- **Text replacements anywhere:** Say "hello comma world" at any point and you get "hello, world" -- replacement patterns work mid-sentence.
- **Safety gate:** Dangerous commands require a hotword prefix ("x-ray save") to prevent accidental execution during dictation.
- **No lost words:** Every word either executes as a command or appears as text. Buffering timeouts ensure nothing gets stuck.

### 1.1 Pipeline at a Glance

```
Microphone -> STT Provider -> WebSocket -> Word Events -> State Machine -> Action
                                                              |
                                                 +------------+------------+
                                                 |            |            |
                                              COMMAND     DICTATION   REPLACEMENT
                                                 |            |            |
                                            TextParser    insert_text  TextParser
                                                 |            |            |
                                            Action IPC    Action IPC   Action IPC
                                                 |            |            |
                                            Input Process (SendInput -> Windows)
```

The pipeline has three stages:
1. **STT intake:** Audio becomes transcript words delivered over WebSocket
2. **Speech processing:** A state machine decides what each word means
3. **Action execution:** Matched patterns become keyboard events, text insertions, or system commands

This document covers stages 1 and 2. Stage 3's text insertion mechanics are in `text-insertion-pipeline.md`.

---

## 2. STT Providers

### 2.1 Provider Architecture

STT providers run as separate processes, communicating with the WheelHouse Logic process over WebSocket. This isolation means a provider crash doesn't bring down the application, and providers can be swapped at runtime without restarting WheelHouse.

WheelHouse has two active STT providers:

```
STT Provider Directory                  +-- vulkan_small (Whisper small.en, Vulkan GPU)
(services/stt_providers/)  --- discovers +-- google_stt_server (Google Cloud Speech)
         |
         v
   RemoteSTTLauncher
         |
         +-- Discovers providers by scanning for config.toml files
         +-- Starts provider subprocess (launcher.py)
         +-- Monitors via PID files in %APPDATA%\WheelHouse\
         +-- Stops via WebSocket shutdown command
```

Each provider directory contains a `config.toml` with a `[provider]` section and a `launcher.py` entry point. The `RemoteSTTLauncher` discovers available providers at startup and exposes them to the GUI for runtime switching. The active provider is persisted in `config.toml` under `[stt] last_provider`.

### 2.2 Shared Infrastructure

All providers share a common audio and transport layer in `services/stt_providers/shared/`:

| Component | File | Purpose |
|-----------|------|---------|
| Audio capture | `shared_audio/microphone.py` | 16 kHz 16-bit PCM via sounddevice (PortAudio) |
| Voice activity detection | `shared_audio/silero_vad.py` | Neural VAD (Silero), 512-sample frames, configurable threshold |
| Automatic gain control | `shared_audio/agc.py` | Normalizes speech volume dynamically |
| Audio processor | `shared_stt/audio_processor.py` | Orchestrates VAD, AGC, lead-in buffer, utterance IDs |
| WebSocket forwarder | `shared_stt/ws_forwarder.py` | Sends transcripts to WheelHouse, receives provider commands back |
| Process launcher | `shared_stt/launcher.py` | Crash recovery (3 strikes), restart flag files |

**Audio capture** runs in a PortAudio callback thread, producing 30ms chunks (configurable via `chunk_ms`). An overflow monitor tracks input overflow events and triggers automatic microphone restart if 5 overflows occur within 30 seconds, with safeguards: 60-second cooldown between restarts, maximum 3 attempts, and a 5-minute stability window that resets the attempt counter.

**VAD lead-in buffer** captures audio *before* speech is confirmed. When Silero VAD commits to speech detection, the lead-in buffer (default 300ms) is flushed to the recognition engine so the beginning of the utterance isn't clipped.

**AGC** runs after VAD confirmation, normalizing speech volume to a target RMS level. It receives feedback from STT outcomes (`on_stt_outcome`) to adjust future gain -- if transcription quality degrades, gain adjusts.

### 2.3 How Local Providers Work (Chunked Re-inference)

The local providers (sherpa-onnx Parakeet on CPU by default; Distil-Whisper via faster-whisper on CUDA as an opt-in) share a chunked re-inference streaming engine. See `stt-and-ai.md` for provider selection and model delivery.

```
Audio chunks (16 kHz float32)
    |
    v
Accumulate into growing buffer
    |
    v
Re-run inference every re_inference_interval_ms (per provider config)
    |
    v
LocalAgreement-2 stability detection
    |  (compare consecutive inference runs, promote words that agree)
    v
Confirmed words -> "stable" message
    |
    v
Trailing silence >= 800ms (endpoint_silence_ms)
    |
    v
All remaining words promoted -> "final" message
```

**LocalAgreement-2** is the stability detection algorithm in the streaming engine: it compares consecutive inference runs and finds the longest common prefix (LCP) of words that agree. Words in the LCP are confirmed; confirmed words never shrink (monotonicity invariant). On utterance endpoint (silence), all remaining words are promoted without the two-run requirement. The **N-1 holdback** is a separate mechanism in the AudioProcessor layer above the engine: it sends `current_words[:-1]` (all confirmed words except the last), holding back the most recent word until 300ms of trailing silence releases it. This prevents sending a word that might still be extended by the next audio chunk.

**Text numerizer** post-processes local STT output for formatting cleanup: it normalizes time patterns ("9.45 am" to "9:45 AM") and removes redundant dollar words ("$200 dollars" to "$200"). It does not convert spelled-out numbers to digits. This is only applied to local providers because Google Cloud STT already returns well-formatted text.

### 2.4 How Google Cloud STT Works

Google Cloud STT streams raw audio to Google's servers and receives interim results with stability scores.

```
Audio chunks -> Google Streaming API
    |
    v
Interim results with stability scores (0.0 - 1.0)
    |
    v
Stability filter (>= 0.89 threshold)
    |
    v
Words above threshold -> "stable" message
    |
    v
Finalization (multiple triggers, see below)
    |
    v
Complete transcript -> "final" message
```

**Finalization is complex** because Google's behavior is inconsistent -- sometimes it sends both an End-of-Single-Utterance (EOS) event and a final result, sometimes only one or the other. The provider uses a hybrid strategy:

| Trigger | What happens | Why |
|---------|-------------|-----|
| `is_final` response | Finalize immediately, cancel EOS timer | Most reliable signal |
| EOS event | Start 500ms fallback timer | Wait for final result that usually follows |
| EOS timer expires | Finalize with last known text | Google didn't send final after EOS |
| 2500ms silence timeout | Finalize regardless | No response from Google at all |
| 3.9s no-text timeout | Abort stream | Nothing recognized, stop wasting API calls |

This layered approach prevents both premature finalization (cutting off words) and freezing (waiting forever for a final that never comes).

### 2.5 WebSocket Message Protocol

All providers send the same JSON message format to WheelHouse:

**vad_start** -- Speech detected (VAD gate opened)
```json
{"type": "vad_start", "text": "", "utterance_id": 42, "is_partial": false, "trace_id": "T-17720345601"}
```

**stable** -- Partial transcript (confirmed words only)
```json
{"type": "stable", "text": "delete five", "utterance_id": 42, "is_partial": true, "trace_id": "T-17720345601"}
```

**final** -- Utterance complete (all words)
```json
{"type": "final", "text": "delete five times", "utterance_id": 42, "is_partial": false, "trace_id": "T-17720345601"}
```

The `trace_id` format is `T-{deciseconds}` (Unix epoch * 10, truncated to integer) for cross-system observability. Utterance IDs are integers that increment per speech detection within each provider session.

### 2.6 Commands from WheelHouse to Providers

The WebSocket is bidirectional. WheelHouse sends control messages back to providers:

| Command | Purpose |
|---------|---------|
| `set_transcription_status` | Enable/disable transcription (with reason: "audio", "sonos", "idle") |
| `add_hint` | Add a word to the recognition vocabulary |
| `restart_service` | Reload configuration without restarting process |
| `hard_restart_service` | Exit process (launcher restarts it) |
| `set_interim_results` | Toggle stable message delivery |
| `set_log_level` | Change provider logging verbosity |
| `shutdown` | Graceful process exit |

The `add_hint` command is handled differently by each provider: the Vulkan provider appends the phrase to `initial_prompt` in its config.toml (biasing Whisper's decoder toward the word), while the Google provider adds it to the `adaptation.hints[]` array (using Google's phrase bias API). Both trigger a hard restart to apply the change. Users invoke this by voice: say "x-ray boost" with text selected, and the selected word is added to the active provider's vocabulary.

---

## 3. WebSocket Intake

### 3.1 Delta Extraction

STT providers send the *complete* transcript text on each stable message, not just new words. The WebSocketManager extracts only the new words (the delta) to avoid processing duplicates.

```
stable: "delete"       -> delta: "delete"    (first message)
stable: "delete five"  -> delta: "five"      (already sent "delete")
final:  "delete five"  -> delta: ""           (no new words)
```

Delta extraction uses **word-level comparison**, not character-level prefix matching. Character-level matching fails when a word is extended across messages:

```
stable: "comm"         -> character delta: "comm"
stable: "comma"        -> character delta: "a"     [!] BUG: sends "a" as a word
```

Word-level comparison detects that `["comm"]` != `["comma"]` and flags this as a revision instead of sending a spurious "a".

### 3.2 Revision Detection and Retraction

When STT changes previously sent words (a revision), the WebSocketManager detects the mismatch and queues a retraction marker:

```
stable: "delete five"  -> delta: "delete", "five"  (sent normally)
final:  "delete fife"  -> REVISION: "five" changed to "fife"
                       -> Queue retraction marker with full final text
```

The SpeechProcessor handles retraction by:
1. Canceling any pending buffer/timeout
2. Requesting the Input Process to retract previously inserted text
3. Replaying the corrected words through the normal pipeline

Retraction is skipped if a command was already executed in the utterance -- you can't un-execute a "delete five".

### 3.3 WordEvent Creation

Each delta word becomes a `WordEvent` -- a frozen dataclass carrying the word and its utterance context:

```python
@dataclass(frozen=True)
class WordEvent:
    word: str                    # The transcribed word
    start_of_utterance: bool     # First word of a fresh utterance?
    end_of_utterance: bool       # Last word of the utterance?
    utterance_id: Optional[int]  # Groups messages from the same speaking turn
    is_utterance_end_marker: bool  # Special: signals "no more words coming"
    is_retraction_marker: bool     # Special: STT revised previous words
    retraction_full_text: Optional[str]  # Corrected text for replay
    trace_id: Optional[str]      # Observability trace ID
```

The `start_of_utterance` flag is critical for the truth table -- it determines whether a command word triggers buffering (fresh utterance) or passes through as dictation text (mid-utterance).

After the final message, the WebSocketManager always queues an **utterance end marker** (`is_utterance_end_marker=True`). This marker is the only way the downstream SpeechProcessor knows an utterance finished. Without it, clipboard restoration would time out waiting for an end signal that never comes.

---

## 4. Speech Processing State Machine

### 4.1 The Central Problem

When you say "delete five times," WheelHouse receives words one at a time: "delete", then "five", then "times". It must decide:

- **"delete"** -- is this a command or are you dictating the word "delete"?
- **"five"** -- is this the count for "delete five" or the start of new dictation?
- **"times"** -- is this part of the command or leftover text?

The speech processor solves this with a truth table state machine that evaluates each word based on its position in the utterance and whether it appears in the pattern catalog.

### 4.2 Processing Modes

The state machine has four modes:

| Mode | Purpose | Timeout |
|------|---------|---------|
| **IDLE** | Normal state. Words evaluated individually. 95% of words. | -- |
| **COMMAND_BUFFERING** | Collecting words for a command pattern (e.g., "delete five") | 700ms |
| **REPLACEMENT_BUFFERING** | Collecting words for a replacement pattern (e.g., "mary smith") | 700ms |
| **HOTWORD_BUFFERING** | After hotword detected, waiting for the actual command | 700ms |

**[!] Note on timeouts:** The production config (`config.toml`) sets both `COMMAND_TIMEOUT_MS` and `REPLACEMENT_TIMEOUT_MS` to 700ms. Several source code comments and docstrings reference older values of 1000ms and 400ms respectively -- these are stale and should not be relied upon.

### 4.3 The Truth Table

When the processor is in IDLE mode, each word is classified by two dimensions:

1. **Position:** Is this the first word of a fresh utterance (`start_of_utterance=True`) or a mid-utterance word?
2. **Catalog membership:** Does this word appear as the first word of any known pattern? If so, is it a COMMAND or REPLACEMENT pattern?

| Case | Fresh? | In Catalog? | Type | Action | Why |
|------|--------|-------------|------|--------|-----|
| FRESH_HOTWORD | Yes | N/A | Hotword | TRANSITION -> HOTWORD_BUFFERING | Safety gate for protected commands |
| FRESH_PASSTHROUGH | Yes | No | NONE | DICTATE immediately | Not a pattern -- type it |
| FRESH_COMMAND | Yes | Yes | COMMAND | BUFFER (700ms) | Could be multi-word command |
| FRESH_REPLACEMENT | Yes | Yes | REPLACEMENT | BUFFER (700ms) | Could be multi-word replacement |
| MID_PASSTHROUGH | No | No | NONE | DICTATE immediately | Not a pattern -- type it |
| MID_COMMAND_PASSTHROUGH | No | Yes | COMMAND | DICTATE immediately | "I want to **delete** something" -- typing, not commanding |
| MID_REPLACEMENT_BUFFER | No | Yes | REPLACEMENT | BUFFER (700ms) | "hello **comma** world" -- still a replacement |

**The key insight:** Commands mid-utterance are dictation. Replacements mid-utterance are still replacements.

When you say "I want to delete this file," the word "delete" appears mid-utterance. It's a command word, but you're clearly dictating a sentence, not issuing a command. The MID_COMMAND_PASSTHROUGH case sends it straight to dictation.

But when you say "my name is mary smith," the word "mary" appears mid-utterance and it's a replacement pattern. You still want "mary smith" to become "Mary Smith" -- the MID_REPLACEMENT_BUFFER case catches this.

### 4.4 Buffering Decisions

Once buffering begins, each new word triggers re-evaluation:

```
BUFFERING mode
    |
    +-> Utterance ends? -> Finalize buffer (try command -> replacement -> dictate)
    |
    +-> Pattern complete? -> Execute immediately
    |     |
    |     +-> Optional numeric group unfilled? -> Keep buffering
    |         (e.g., "backspace" matches but "backspace three" is better)
    |
    +-> Pattern impossible? -> Finalize buffer
    |     |
    |     +-> Was COMMAND, could be REPLACEMENT? -> Switch to REPLACEMENT_BUFFERING
    |
    +-> Could continue? -> Keep buffering (restart timeout)
```

**Single-word optimization:** If a word matches a complete pattern AND cannot continue with additional words (e.g., "redo" has no "redo N" variant), it executes immediately without entering buffering mode. This eliminates the timeout delay for unambiguous single-word commands.

**Optional numeric groups:** A pattern like `^backspace\s*(\d+)?$` matches "backspace" alone, but the user might say "backspace three" next. The router detects the unfilled optional group and continues buffering until the timeout expires or a non-numeric word arrives.

**Mode switching:** If COMMAND_BUFFERING fails (the buffered words can't match any command), the router checks if they could match a replacement pattern. If so, it switches to REPLACEMENT_BUFFERING instead of immediately finalizing. This handles cases like "quotes now is the time" where "quotes" indexes both command and replacement patterns.

### 4.5 Buffer Finalization

When a buffer must be resolved (timeout, utterance end, or impossible pattern), the **Command -> Replacement -> Dictate** fallback chain runs:

1. **Try command match:** Does the full buffer text match any command pattern? (fullmatch mode -- entire text must match)
2. **Try replacement match:** Does any replacement pattern match anywhere in the buffer? (search mode -- can match mid-text)
3. **Fallback to dictation:** No pattern matched -- insert the buffer as typed text. If hotword was active, include the hotword in the dictated text ("x-ray hello" becomes dictated text "x-ray hello").

### 4.6 Hotword Mode

The hotword (default: "x-ray", configured via `COMMAND_HOTWORD` in `patterns.toml`) is a safety gate for commands marked with `requires_hotword = true`. It prevents accidental execution of dangerous commands during dictation.

```
"x-ray save"
    |
    "x-ray" detected at utterance start
    -> TRANSITION to HOTWORD_BUFFERING
    -> hotword_active = True
    -> hotword NOT buffered (cleared from buffer)
    |
    "save" arrives
    -> Buffered as ["save"]
    -> Pattern "^save$" matches with requires_hotword=True
    -> hotword_active=True satisfies requirement
    -> EXECUTE

"save" (without hotword)
    -> FRESH_COMMAND case
    -> Pattern "^save$" matches...
    -> BUT requires_hotword=True and hotword_active=False
    -> Pattern rejected
    -> Finalized as dictation: types "save"
```

The hotword only works at the start of a fresh utterance. "I said x-ray" mid-utterance treats "x-ray" as dictation text.

### 4.7 Pending Utterance End

The utterance end marker controls clipboard restoration (see `text-insertion-pipeline.md` for clipboard lifecycle details). A race condition occurs if the end marker arrives while a buffer is still pending:

```
Problem:
1. Buffer finalizes -> command executes (contains clipboard paste)
2. Utterance end arrives -> clipboard restored
3. Application tries to read clipboard for paste -> ALREADY RESTORED

Solution: Defer the end marker.
```

When the end marker arrives during buffering, the processor stores it in `_pending_utterance_end` instead of sending it immediately. After the buffer finalizes and the resulting command or dictation completes, the deferred end marker is sent. This ensures clipboard restoration happens *after* all paste operations finish.

### 4.8 Error Resilience

The processing loop wraps each word in a try/except. A failure processing one word resets the state machine to IDLE and continues with the next word. This prevents a single malformed WordEvent or pattern match error from crashing the entire speech pipeline.

```python
try:
    await self.process_word_event(word_event)
except Exception as e:
    logger.error(f"Error processing word: {e}")
    self.mode = ProcessingMode.IDLE
    self.buffer.clear()
    self.hotword_active = False
    # Continue processing next word
```

---

## 5. Pattern System

### 5.1 Pattern Catalog

All voice patterns live in a single TOML file: `speech/config/patterns.toml`. The `PatternCatalog` loads this file at startup, compiles regexes, and builds a first-word hash table for O(1) lookup.

**Pattern types are auto-detected from the regex anchor:**

| Anchor | Type | Match mode | Example |
|--------|------|-----------|---------|
| `^` prefix | COMMAND | `fullmatch()` -- entire text must match | `^delete\s*(\d+)?$` |
| No `^` prefix | REPLACEMENT | `search()` -- can match anywhere | `\bcomma\b` |

This is the single source of truth for match behavior. The `^` anchor in the pattern string determines everything: match mode, whether the pattern triggers COMMAND_BUFFERING or REPLACEMENT_BUFFERING, and whether it's subject to the mid-utterance passthrough rule.

### 5.2 First-Word Indexing

The catalog extracts every possible first word from each pattern and builds a hash table. When a word arrives, the processor looks it up in O(1) time to decide whether to buffer.

```
Pattern: "^(delete|del)\s*(\d+)?$"
    -> Extracted first words: ["delete", "del"]
    -> Both indexed to this pattern

Pattern: "^(?:go )?down"
    -> Extracted first words: ["go", "down"]
    -> "go down" or just "down" both trigger buffering

Pattern: "quotes?"
    -> Extracted first words: ["quote", "quotes"]
    -> Optional 's' generates both variants
```

Alternations (`(a|b)`), optional prefixes (`(?:prefix )?`), and optional characters (`word?`) are all handled during extraction.

### 5.3 Pattern Format

```toml
COMMAND_HOTWORD = "x-ray"

# Command: entire utterance must match
[[pattern]]
pattern = '''^delete\s*(\d+)?$'''
actions = [{ function = "press", params = ["del", "g1"] }]

# Command with hotword requirement
[[pattern]]
pattern = '''^save$'''
requires_hotword = true
actions = [{ function = "hk", params = ["ctrl", "s"] }]

# Replacement: can match mid-utterance
[[pattern]]
pattern = '''\bcomma\b'''
actions = [{ function = "text", params = [", "] }]

# Multi-step command with awaits_done sequencing
[[pattern]]
pattern = '''^delete word$'''
actions = [
    { function = "hk", params = ["ctrl", "left"], awaits_done = true },
    { function = "hk", params = ["shift", "ctrl", "right"], awaits_done = true },
    { function = "hk", params = ["del"], awaits_done = true }
]
```

**Pattern fields:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `pattern` | string | (required) | Regex, case-insensitive. `^` = command, no `^` = replacement |
| `actions` | array | (required) | Sequence of action steps to execute |
| `requires_hotword` | bool | false | Must be preceded by hotword to execute |

**Action step fields:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `function` | string | (required) | Action function name (see Section 6) |
| `params` | array | (required) | Parameters. "g1"-"g9" reference capture groups |
| `awaits_done` | bool | false | Wait for action to complete before next step |

**Ordering rules** (documented in patterns.toml, not enforced by code):
1. Commands with `^` anchor are checked before replacements without `^`
2. More specific patterns before generic patterns
3. Longer trigger phrases before shorter ones

**[!] The ordering rules are honor-system only.** Patterns are processed in file order with first-match-wins. If you add a new pattern in the wrong position, it could shadow a more specific pattern below it. There is no automated validation.

### 5.4 Pattern Matching

The `PatternMatcher` consolidates all match logic:

- **`match_complete(text, pattern_type)`** -- Core matching. Commands use `fullmatch()`, replacements use `search()`. Returns `MatchResult` with match object, before/after remainder text, and validation status.
- **`match_for_routing(buffer, type, hotword)`** -- Used by SpeechRouter during buffering. Joins buffer words, delegates to `match_complete()`.
- **`match_single_pattern(text, pattern)`** -- Used by TextParser for first-match-wins execution. Same fullmatch/search logic applied to one pattern.
- **`can_continue(buffer, type)`** -- Checks if the buffer is a valid prefix that could match with more words.
- **`validate_numeric(match, group)`** -- Validates that a captured group is a valid English number word via `words_to_int()`.

**Numeric validation** uses `words_to_int()` to convert captured groups: "five" -> 5, "three" -> 3, digit strings pass through. If the captured value isn't a recognized number word, the pattern match is rejected.

**Remainder handling** for replacements: when a pattern matches mid-text, the text before and after the match is captured as `before_remainder` and `remainder`. The processor executes the before-remainder first (it arrived earlier), then the matched pattern, then the after-remainder. Each remainder is recursively checked for additional patterns.

---

## 6. Command Execution

### 6.1 TextParser

When the state machine decides to EXECUTE, the `TextParser` (`speech/command_engine.py`) receives the buffer text and iterates through patterns in file order:

```
"delete five" arrives
    |
    v
Iterate patterns (first-match-wins)
    |-> "^delete word$" -- fullmatch("delete five") -- no match
    |-> "^delete\s*(\d+)?$" -- fullmatch("delete five") -- MATCH
    |
    v
Validate numeric group: words_to_int("five") -> 5
    |
    v
Build context: {g1: "five", g2: None, ...}
    |
    v
Resolve parameters: ["del", "g1"] -> ["del", "five"]
    |
    v
Call: ActionFunctions.press("del", "five")
    -> Returns: {action: "press_key_action", params: {key: "del", repeat: 5}}
    |
    v
Fire-and-forget IPC to Input Process
    -> Input Process presses Delete key 5 times
```

**Parameter resolution** replaces capture group tokens in action parameters with values from the regex match. There are two substitution modes:

- **Direct substitution:** When a parameter is exactly `"g1"`, it's replaced with the raw capture group value. The action function receives the captured text directly. Example: `params = ["del", "g1"]` with capture group 1 = "five" becomes `params = ["del", "five"]`.
- **Embedded substitution:** When a parameter contains a group token inside other text, string replacement is used. Example: `params = ["go g1"]` with capture group 1 = "down three" becomes `params = ["go down three"]`. This allows building compound strings from captured speech.

### 6.2 Action Functions

Actions are registered in `ActionFunctions` (`speech/actions.py`). There are two execution paths:

**Fire-and-forget** (`send_command`): Returns a dict with an `action` key. Serialized to shared memory, Input Process executes, no response expected. Used for keyboard events, text insertion, and other UI mutations that don't need confirmation.

**Request-response** (`send_request`): Returns a dict with `action` + generates a `request_id`. Caller awaits a Future that's resolved when the Input Process responds. Used for clipboard capture, AI text correction, and other operations that return data.

**Key action categories:**

| Category | Functions | IPC Mode |
|----------|----------|----------|
| Keyboard | `hk()` (hotkey), `press()`, `press_keys()` | Fire-and-forget |
| Text | `insert_text()`, `type_text()`, `text()`, `insert_raw()` | Fire-and-forget |
| Selection | `transform_selection()`, `wrap_or_insert()` | Fire-and-forget |
| Window | `activate()` | Fire-and-forget |
| Clipboard | `capture_clipboard()`, `skip_clipboard_restore()` | Local / fire-and-forget |
| AI | `fix_text_ai()`, `wheelhouse_help()` | Request-response (async) |
| STT | `add_hint_to_stt()` | Request-response (async) |

### 6.3 Numeric Parameter Handling

`words_to_int()` converts spoken number words to integers:

```python
_WORD_TO_INT_MAP = {
    "zero": 0, "one": 1, "two": 2, "to": 2, "too": 2,
    "three": 3, "four": 4, "for": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10
}
```

Digit strings ("5") pass through directly. The function handles two special cases differently:

- **`None` input** (from an unfilled optional capture group, e.g., user said "delete" without a number): returns `1` as a sensible default repeat count.
- **Unrecognized string input** (e.g., "twenty"): returns `None`, which signals to the pattern matcher that numeric validation failed and the pattern should be rejected.

**Practical note on larger numbers:** The `words_to_int` map only covers 0-10, but this rarely matters in practice. Both active STT providers (Vulkan Whisper and Google Cloud) usually transcribe spoken numbers above 10 as digit strings ("20", "50"), which pass through `\d+` and `int()` directly. The limitation only surfaces when the provider transcribes a number as a word (e.g., "twenty" instead of "20"), in which case the `\d+` regex doesn't match and the command falls through to dictation. No external number-parsing library is imported; the mapping is a hardcoded dictionary.

### 6.4 Multi-Step Action Sequences

Patterns can define multiple action steps. Each step executes in order, with optional `awaits_done` synchronization:

```toml
# "delete word" = select word, then delete it
actions = [
    { function = "hk", params = ["ctrl", "left"], awaits_done = true },
    { function = "hk", params = ["shift", "ctrl", "right"], awaits_done = true },
    { function = "hk", params = ["del"], awaits_done = true }
]
```

When `awaits_done = true`, the TextParser uses `send_request()` and waits for the Input Process to confirm completion before executing the next step. Without it, steps are fired in rapid succession via `send_command()`. A 120ms debounce separates consecutive fire-and-forget UI actions to prevent keystroke overlap.

---

## 7. Speech Suppression

Speech processing can be paused by multiple independent conditions. The `StateManager` computes a single `speech_enabled` property from all of them:

```python
speech_enabled = (
    user_toggle AND
    NOT audio_suppressed AND
    NOT sonos_suppressed AND
    NOT idle_suppressed
)
```

| Condition | Trigger | Config Toggle |
|-----------|---------|---------------|
| User toggle | System tray menu or voice command | Always available |
| Audio suppression | System audio playing (peak > 0.05 RMS) | `ENABLE_AUDIO_SUPPRESSION` |
| Sonos suppression | Sonos speaker playing | `ENABLE_SONOS_SUPPRESSION` |
| Idle suppression | System idle > 10 minutes | `ENABLE_IDLE_SUPPRESSION` |

Each suppression source publishes events via the EventBus. The StateManager subscribes and aggregates state. When `speech_enabled` changes, the WebSocket sends a `set_transcription_status` command to the STT provider with the suppression reason, and the provider stops sending transcripts.

**Wake word recovery:** When idle suppression is active, the wake word ("computer", configured in `[wake_word]`) can reactivate speech processing. The STT provider detects the wake word locally and sends a `wake_word_detected` message, which clears idle suppression.

**Manual toggle** clears all suppression flags -- if you explicitly enable speech, all automatic suppression is overridden.

---

## 8. Configuration

### 8.1 Speech Processing (`config.toml`)

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `REPLACEMENT_TIMEOUT_MS` | 700 | Buffer timeout for replacement patterns |
| `COMMAND_TIMEOUT_MS` | 700 | Buffer timeout for command patterns |
| `GREEDY_TIMEOUT_MS` | 5000 | Buffer timeout when the current buffer already matches a greedy pattern (`.*` or `.+`); prevents the 700 ms timer from racing streaming STT word delivery. See `wh-greedy-buffer-race`. |
| `COMMAND_COMPLETION_WAIT_MS` | 1000 | Wait time after command execution |
| `SPEECH_WEBSOCKET_HOST` | "127.0.0.1" | WebSocket bind address for STT server |
| `SPEECH_ENABLED_ON_STARTUP` | true | Initial speech processing state |
| `SHOW_SPEECH_PULSE` | true | Visual feedback during speech input |
| `speech.notify_on_revision` | false | Toast notification when STT revises text |

### 8.2 STT Provider Selection (`config.toml`)

```toml
[stt]
last_provider = "google_stt"    # Persisted after runtime switch

[stt.google]
boost_words = []                # Phrase bias list for recognition

[stt.azure]
subscription_key = ""           # Azure Cognitive Services key
region = "eastus"
```

### 8.3 Local STT Provider (`stt_providers/vulkan_small/config.toml`)

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `model.model_path` | `ggml-small.en-q5_1.bin` | Quantized Whisper model |
| `model.n_threads` | 8 | CPU threads for non-GPU work |
| `engine.re_inference_interval_ms` | 1500 | How often to re-run inference |
| `engine.endpoint_silence_ms` | 800 | Silence duration to end utterance |
| `client.silero_threshold` | 0.5 | VAD confidence threshold |
| `client.vad_lead_in_ms` | 300 | Pre-speech audio buffer |
| `agc.enabled` | true | Automatic gain control |
| `agc.target_speech_rms` | 0.1 | Target speech amplitude |

### 8.4 Pattern Configuration (`speech/config/patterns.toml`)

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `COMMAND_HOTWORD` | "x-ray" | Safety prefix for protected commands |

Patterns are defined as `[[pattern]]` sections (see Section 5.3 for format).

### 8.5 Wake Word (`config.toml`)

```toml
[wake_word]
enabled = true
keyword = "computer"
sensitivity = 0.5
mode = "idle_recovery"          # Only active during idle suppression
model_dir = "../shared/data/wake_words"  # Pre-configured models only (downloaded from GitHub wakewords collection)
```

---

## 9. Key Design Decisions

### Why a truth table instead of a mode toggle?

Early voice control systems use explicit mode switching: "start dictation" / "stop dictation" / "command mode". WheelHouse uses implicit mode detection based on utterance position. This eliminates the cognitive overhead of remembering which mode you're in and the frustration of issuing a command in dictation mode (or vice versa).

The trade-off is that commands only work at the start of an utterance. You can't say "now delete that" and have "delete" be a command. In practice this isn't limiting because voice commands are naturally uttered as standalone phrases. The alternative -- buffering every utterance to check for mid-sentence commands -- would add unacceptable latency to all dictation.

### Why fullmatch for commands and search for replacements?

Commands are explicit actions with side effects (deleting text, pressing keys). They require precision: the entire utterance must match the pattern. If "delete five items" matched the "delete five" command, the word "items" would be lost.

Replacements are text corrections that should work anywhere in natural speech. "Hello comma world" should produce "hello, world" regardless of what came before "comma." Search mode enables this.

### Why buffer timeouts instead of waiting for utterance end?

Utterances can be long. Waiting for the end of "I want to go down to the store and buy some milk" before processing the replacement "comma" (if one appeared) would delay text insertion by several seconds. Timeouts let the processor commit to a decision within 700ms, keeping the system responsive.

The 700ms timeout balances two pressures: short enough that dictation doesn't feel laggy, long enough that multi-word commands like "delete five" have time for the second word to arrive.

### Why a single patterns.toml instead of separate command and replacement files?

Pattern ordering matters (first-match-wins), and some words index both command and replacement patterns. A single file makes ordering explicit and prevents "which file takes priority?" confusion. The `^` anchor convention makes the type distinction self-documenting: if you see `^`, it's a command.

### Why shared audio infrastructure across providers?

All providers need the same audio pipeline: microphone capture, VAD, AGC, lead-in buffering. Both the Vulkan and Google providers import from the shared layer (`SileroVAD`, `SmartAGC`, `get_audio_provider`). Duplicating this per provider would mean maintaining parallel implementations with slightly different bugs. The shared layer in `services/stt_providers/shared/` ensures consistent audio quality regardless of which recognition engine processes it.

---

## 10. Known Issues and Limitations

| Issue | Impact | Status |
|-------|--------|--------|
| `words_to_int` limited to 0-10 | STT providers usually transcribe numbers above 10 as digits (which work), but when they don't, spoken words like "twenty" fail | Low impact -- hardcoded map, no external number-parsing library imported |
| Pattern ordering not enforced | Misordered patterns silently shadow each other | Open -- documented as honor-system rules |
| Stale timeout comments in source | `domain.py` and `speech_processor.py` docstrings reference 1000ms/400ms instead of 700ms | Open -- cosmetic, does not affect behavior |
| `hotwords.txt` referenced in architecture overview | `architecture-overview.md` lists `speech/config/hotwords.txt` as a config file, but this file doesn't exist -- hotword is in `patterns.toml` | Open -- stale documentation |
| Word queue has no backpressure | `asyncio.Queue(maxsize=1000)` will block the WebSocket handler if full, but no monitoring or warning | Low risk -- would require extreme processing delay |

---

## 11. Complete Pipeline Example

**User speaks: "delete five times"**

```
STT Provider (Vulkan)
    |
    | vad_start (utterance 42)
    | stable: "delete"
    | stable: "delete five"
    | final:  "delete five times"
    v
WebSocketManager
    | delta("delete") -> WordEvent(word="delete", start=True)
    | delta("five")   -> WordEvent(word="five", start=False)
    | delta("times")  -> WordEvent(word="times", start=False)
    | end marker      -> WordEvent(is_utterance_end_marker=True)
    v
SpeechProcessor
    |
    | "delete" arrives (IDLE, fresh, COMMAND type)
    | -> FRESH_COMMAND: BUFFER, enter COMMAND_BUFFERING, start 700ms timeout
    |    buffer = ["delete"]
    |
    | "five" arrives (COMMAND_BUFFERING)
    | -> New buffer: ["delete", "five"]
    | -> Pattern "^delete\s*(\d+)?$" matches "delete five"
    | -> Numeric validation: words_to_int("five") = 5
    | -> _has_unfilled_numeric_group? No -- group IS filled with "five"
    | -> Pattern complete, no remainder
    | -> EXECUTE "delete five"
    |    -> Cancel 700ms timeout
    |    -> TextParser: press("del", "five") -> press Delete key 5 times
    |    -> Return to IDLE
    |
    | "times" arrives (IDLE, mid-utterance, NONE type)
    | -> MID_PASSTHROUGH: DICTATE "times"
    | -> insert_text("times") -> Input Process inserts "times"
    |
    | end marker arrives (IDLE)
    | -> Send end_utterance -> clipboard restored
    v
Result: 5 characters deleted, then "times" typed
```

---

## 12. Files Reference

| File | Purpose |
|------|---------|
| **STT Providers** | |
| `stt_providers/shared/shared_audio/microphone.py` | Audio capture (16 kHz, sounddevice) |
| `stt_providers/shared/shared_audio/silero_vad.py` | Silero neural VAD |
| `stt_providers/shared/shared_audio/agc.py` | Automatic gain control |
| `stt_providers/shared/shared_stt/audio_processor.py` | VAD + AGC + lead-in orchestration |
| `stt_providers/shared/shared_stt/ws_forwarder.py` | WebSocket transport to WheelHouse |
| `stt_providers/shared/shared_stt/launcher.py` | Process supervision and crash recovery |
| `stt_providers/shared/vulkan_engine/vulkan_engine.py` | Whisper.cpp streaming with LocalAgreement-2 |
| `stt_providers/shared/vulkan_engine/vulkan_server.py` | Vulkan provider main server |
| `stt_providers/google_stt_server/main.py` | Google Cloud STT with hybrid EOS/final strategy |
| **WebSocket Intake** | |
| `integrations/websocket_manager.py` | Receives transcripts, creates WordEvents, delta extraction |
| **Speech Processing** | |
| `speech/word_event.py` | WordEvent dataclass definition |
| `speech/domain.py` | ProcessingMode, Action, Decision definitions |
| `speech/speech_handler.py` | Service container, wires pipeline components |
| `speech/speech_processor.py` | Main state machine loop |
| `speech/router.py` | Truth table logic (SpeechRouter) |
| `speech/pattern_catalog.py` | TOML loading, first-word indexing |
| `speech/pattern_matcher.py` | Fullmatch/search logic, numeric validation |
| `speech/command_engine.py` | TextParser: pattern execution, capture groups |
| `speech/actions.py` | Action function library (30+ functions) |
| `speech/number_word_parser.py` | Spoken number word parsing for "click N" routing |
| `speech/config/patterns.toml` | All voice patterns (commands and replacements) |
| **IPC** | |
| `app.py` | SharedMemory IPC to Input Process |
| `stt/remote_stt_launcher.py` | Provider discovery and lifecycle management |
| **State Management** | |
| `state_manager.py` | Speech suppression aggregation |
| `handlers/audio_monitor.py` | Audio playback detection |
| `config.toml` | Runtime settings |
