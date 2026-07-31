# Wheelhouse Voice Command and Configuration Reference

This is the complete, automatically generated reference for every Wheelhouse voice command and configuration setting. It is built from the same sources the application uses, so it stays in step with what Wheelhouse actually does. For a guided introduction to using and installing Wheelhouse, see the help document (wheelhouse_help.md).

## Voice Command Reference

### Dictation Control

| Say this | What happens | Notes |
|---|---|---|
| literal [words] | Types the words after "literal" exactly, skipping all command and replacement processing Takes effect wherever it appears in an utterance, not only as the first word. | The escape hatch -- see the detailed explanation in "Special Commands" |
| insert [text] | Inserts raw text with no capitalization, spacing, or formatting applied | Useful for exact fragments like an email address or a product code |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item 1", "item 5" |
| submit | Presses Enter Recognized at the trailing position of an utterance. | Also works as the last word of a sentence: "hello world submit" types "hello world" and then presses Enter. To type the word itself, say "literal submit" |

### Text Editing

| Say this | What happens | Notes |
|---|---|---|
| backspace [number] | Deletes one character to the left, or that many with a number | e.g. "backspace 5"; the number is optional, counts capped at 50 |
| delete [number] | Deletes one character (or that many) to the right | e.g. "delete 5"; counts capped at 50 |
| delete word | Deletes the entire word under the cursor |  |
| undo [number] | Undoes the last action, or several | Ctrl+Z; e.g. "undo 3". Common mishearings "undue" and "undu" also fire |
| redo [number] | Redoes the last undone action, or several | Ctrl+Y; the common mishearing "redu" also fires |
| new line | Inserts a line break without submitting the field | Works inline during dictation |
| new paragraph | Inserts two line breaks | Works inline during dictation |
| tab [number] | Presses Tab that many times | e.g. "tab 3"; "indent 3" does the same. The number is required -- "tab" alone is typed as the word |
| shift tab | Outdents (Shift+Tab) | "outdent" does the same |
| escape | Presses the Escape key |  |
| press [keys] | Presses any key or key combination by name | e.g. "press enter", "press alt f4", "press f5"; See the press-keys detail subsection of the Voice Commands section |
| copy | Copies the current selection |  |
| copy line | Copies the entire current line |  |
| copy all | Copies everything in the current field |  |
| copy screen | Starts the Windows screenshot snipping tool |  |
| x-ray cut | Cuts the current selection | Requires the hotword for safety |
| paste | Pastes the clipboard contents |  |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires the hotword |
| select all | Selects everything in the current field |  |
| select word | Selects the word under the cursor |  |
| select line | Selects the line under the cursor |  |
| select paragraph | Selects the paragraph under the cursor |  |
| x-ray save | Saves the current document (Ctrl+S) |  |
| x-ray find [text] | Opens the app's find bar and types the search term | e.g. "x-ray find invoice" |
| x-ray replace | Opens find-and-replace (Ctrl+H) |  |
| x-ray search | Copies the current selection and runs a web search for it | Select the text first |

### Text Formatting

| Say this | What happens | Notes |
|---|---|---|
| uppercase | Converts the selection to UPPERCASE |  |
| lowercase | Converts the selection to lowercase |  |
| capitalize | Capitalizes the first letter of the selection and lowercases the rest |  |
| title case | Converts the selection to Title Case |  |
| snake case | Converts the selection to snake_case |  |
| camel case | Converts the selection to camelCase |  |
| pascal case | Converts the selection to PascalCase |  |
| kebab case | Converts the selection to kebab-case |  |
| compress | Removes the spaces from the selection, joining the words together |  |
| x-ray bold text | Bolds the selection (Ctrl+B) | Works in apps that support rich text |
| x-ray italics | Italicizes the selection (Ctrl+I) | Works in apps that support rich text |
| x-ray underline | Underlines the selection (Ctrl+U) | Works in apps that support rich text |
| parentheses [text] | Wraps the selection in ( ), inserts an empty ( ) pair, or inserts the spoken text wrapped | "parentheses hello" gives "(hello)" |
| brackets [text] | Wraps the selection in [ ], inserts an empty pair, or wraps the spoken text |  |
| braces [text] | Wraps the selection in { }, inserts an empty pair, or wraps the spoken text |  |
| angle brackets [text] | Wraps the selection in < >, inserts an empty pair, or wraps the spoken text |  |
| quotes [text] | Wraps the selection in double quotes, inserts an empty pair, or wraps the spoken text |  |
| single quotes [text] | Wraps the selection in single quotes, inserts an empty pair, or wraps the spoken text |  |

### Navigation

| Say this | What happens | Notes |
|---|---|---|
| go [where] | Moves the cursor without touching the keyboard: go home / go end / go top / go bottom / go left / go right, with counts, word and paragraph units, and "then"-chained "grab" steps that select along the way | See the Navigation subsection of the Voice Commands section for the full move list |

### Punctuation and Symbols

| Say this | What happens | Notes |
|---|---|---|
| period | Types . inline during dictation |  |
| comma | Types , inline during dictation |  |
| colon | Types : inline during dictation |  |
| semicolon | Types ; inline during dictation |  |
| question mark | Types ? inline during dictation |  |
| exclamation point | Types ! inline during dictation | "exclamation mark" also works |
| apostrophe | Types ' inline during dictation |  |
| hyphen | Types - inline during dictation |  |
| dash | Types an em dash (the long dash) inline during dictation |  |
| slash | Types / inline during dictation |  |
| backslash | Types \\ inline during dictation |  |
| backtick | Types \` inline during dictation |  |
| at sign | Types @ inline during dictation |  |
| hashtag | Types # inline during dictation |  |
| dollar sign | Types $ inline during dictation |  |
| percent | Types % inline during dictation |  |
| caret sign | Types ^ inline during dictation | Also fires if heard as "carrot sign" |
| ampersand | Types & inline during dictation | "and sign" also works |
| asterisk | Types * inline during dictation |  |
| underscore | Types _ inline during dictation |  |
| plus sign | Types + inline during dictation |  |
| equal sign | Types = inline during dictation |  |
| tilde | Types ~ inline during dictation | Also fires if heard as "tilda" |
| vertical bar | Types the pipe character inline during dictation |  |
| ellipsis | Types ... inline during dictation |  |
| space bar | Types a single literal space inline during dictation |  |
| colin | Mishear tolerance: inserts : when "colin" is the entire utterance Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| come | Mishear tolerance: inserts , when "come", "kama", "commer", or "come on" is the entire utterance Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |

### Application Switching

| Say this | What happens | Notes |
|---|---|---|
| x-ray activate [app name] | Brings the named application's window forward; when a pattern's target is a program file (.exe) and it has no window, Wheelhouse starts it | e.g. "x-ray activate outlook" |
| x-ray browser | Brings your default web browser to the front | Wheelhouse looks up which browser is your Windows default at the moment you speak |
| x-ray notepad | Brings Notepad to the front |  |

### System

| Say this | What happens | Notes |
|---|---|---|
| zoom in | Zooms in (Ctrl and plus) |  |
| zoom out | Zooms out (Ctrl and minus) |  |
| create tab | Sends Ctrl+N | New tab in most editors; note that in most browsers Ctrl+N opens a new window, not a tab |
| create window | Sends Ctrl+Shift+N | New window in editors; opens a private/incognito window in most browsers |
| x-ray close window | Closes the active window (Alt+F4) | Requires the hotword for safety |
| x-ray maximize | Maximizes the active window |  |
| x-ray minimize | Minimizes the active window |  |
| x-ray desktop | Shows the desktop (Windows+D) |  |
| Windows settings | Opens the Windows Settings app | Also fires if heard as "Window settings" |

### Voice Element Clicking

| Say this | What happens | Notes |
|---|---|---|
| click [name] | Clicks the button, link, menu item, or other control with that name; add a role word to narrow the search, or say a number while the numbered overlay is showing | See the Voice Element Clicking subsection of the Voice Commands section |
| apply numbers | Paints a number on every clickable control in the front window | Numbers stay up until you dismiss them |
| dismiss numbers | Removes the numbers |  |

### Wheelhouse Control

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening: Wheelhouse listens only while you hold the floating button | A notification confirms the switch |
| click to talk mode | Switches back to toggle listening (click to start, click to stop) -- the default |  |
| x-ray help | Opens the Wheelhouse Assistant (the official online help) in your browser | Uses the gem_url setting under [ai.help]; if blanked, Wheelhouse says out loud that online help is not configured |
| x-ray patterns | Opens the Pattern Manager | "x-ray pattern manager" also works; see "Special Commands" |
| x-ray fix | Sends the selected text to the configured AI server for grammar and polish, then replaces the selection with the corrected version | Requires the AI server to be configured and reachable; Wheelhouse speaks its progress and always preserves your original text on any failure |
| x-ray simplify | Rewrites the selected text in plain language, using shorter sentences and simpler words Keeps every fact and leaves the layout alone -- line breaks, indentation, bullet marks, numbering, code lines and addresses come back unchanged. | Same AI server and same safeguards as "x-ray fix"; the selection comes back as plain text, so formatting applied in a word processor is lost |
| x-ray shorten | Rewrites the selected text more briefly, cutting repetition and padding | Same AI server and same safeguards as "x-ray fix" |
| x-ray make formal | Rewrites the selected text in a formal register, avoiding contractions and casual wording | Same AI server and same safeguards as "x-ray fix" |
| x-ray pirate | Rewrites the selected text the way a pirate would say it | Ships as a worked example: it is the same action as the three above with a different sentence in the pattern file. See "Special Commands" for writing your own. |
| x-ray translate to [language] | Translates the selected text into the language you name, for example "x-ray translate to spanish" or "x-ray translate to brazilian portuguese" Keeps every fact and leaves names and numbers as they are. | Same AI server and same safeguards as "x-ray fix"; say the language in English and in lower case, as one or more plain words with no punctuation. How good the translation is depends on the model you have configured. |
| x-ray cancel fix | Cancels an in-progress fix or rewrite |  |
| x-ray boost | Adds the selected text to the speech recognition hints | See "Special Commands" -- on the default engine this saves the hint but does not apply it until you opt in |

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

**device_type** *(default: `"default"`)* -- Which audio device to control. Valid values: "default" (the usual choice), "communications", or a specific device name.

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

**credentials_file** *(default: `""`)* -- Full path to the Google service-account key file (the JSON file downloaded during Google Cloud setup). Wheelhouse writes this when you pick the file from the tray menu's Google Cloud Credentials item; when it is empty, the GOOGLE_APPLICATION_CREDENTIALS environment variable is used instead.

### [stt.azure]

**subscription_key** *(default: `""`)* -- Credential for the Azure cloud speech option; only matters if you deliberately set up Azure, which most people never do.

**region** *(default: `"eastus"`)* -- Azure service region for the Azure cloud speech option.

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

**overlay_badge_font_pt** *(default: `12`)* -- The size of the painted overlay numbers. Raise it if the numbers are hard to read.

**response_timeout_ms** *(default: `3000`)* -- How long, in milliseconds, Wheelhouse waits for a click command's search before giving up. Raise it on a slow machine if clicks time out in complex windows.

**walk_deadline_ms** *(default: `2500`)* -- How long, in milliseconds, Wheelhouse searches a window for clickable things before giving up. Raise it on a slow machine if clicks time out in complex windows.

**snapshot_ttl_seconds** *(default: `30`)* -- How long the numbered overlay's snapshot stays valid, in seconds.

**browser_processes** *(default: `["brave.exe", "chrome.exe", "msedge.exe", "vivaldi.exe", "slack.exe", "discord.exe", "code.exe", "ms-teams.exe", "Teams.exe", "spotify.exe", "notion.exe", "obsidian.exe", "ChatGPT.exe"]`)* -- The browser-like apps (browsers, Slack, Discord, and similar) that need a deeper search for clickable elements.

**browser_processes_extend** *(default: `[]`)* -- Adds a browser-like app to the deeper-search list without retyping the built-ins. Add an app here if voice clicking cannot see controls inside it.

**enable_screen_reader_flag** *(default: `false`)* -- Tells apps a screen reader is present, which makes some expose more clickable elements. Try true if an app hides its buttons; note some apps change their appearance when this is on.

---

Generated: 2026-07-30 for the v1.0.6 release
