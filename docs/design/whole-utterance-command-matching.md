# Whole-Utterance Command Matching

## Decision

WheelHouse will execute a leading command only after it receives the utterance end marker.

WheelHouse will match the complete utterance against command patterns.

WheelHouse will not execute a command prefix from a longer utterance.

WheelHouse will keep ordinary dictation incremental.

WheelHouse will keep replacement processing incremental.

Only an utterance whose first word is a command candidate, or whose first word is the command hotword, can become a leading command.

The command timeout and greedy timeout will remain as text-fallback timers.

Neither timeout will authorize command execution.

## Check Of The Established Facts

The code supports all eight established facts.

There is one scope detail in fact 2.

Mid-utterance replacement patterns still buffer by design.

That buffering does not delay ordinary words that are not replacement candidates.

No established fact conflicts with the code.

## Current Behavior

### WebSocket intake

`services/wheelhouse/integrations/websocket_manager.py:350-396` compares each stable or final transcript with the words already sent.

A disagreement returns a retraction signal.

`services/wheelhouse/integrations/websocket_manager.py:398-436` queues a retraction marker first.

It queues the utterance end marker second.

This queue order lets every valid revision reach the speech processor before command matching at utterance end.

`services/wheelhouse/integrations/websocket_manager.py:1127-1222` also queues the utterance end marker after an agreeing final.

### Pattern classification

`services/wheelhouse/speech/pattern_catalog.py:94-109` defines `PatternType.COMMAND`, `PatternType.REPLACEMENT`, and `PatternType.NONE`.

`services/wheelhouse/speech/pattern_catalog.py:1000-1013` performs the first-word catalog lookup.

`services/wheelhouse/speech/pattern_catalog.py:1015-1088` gives command patterns priority when one word can start both a command and a replacement.

The catalog does not decide whether a command can execute.

It only identifies possible pattern starts and supplies matching patterns.

### Router decisions

`services/wheelhouse/speech/router.py:309-454` contains the idle truth table.

The key expression is at `services/wheelhouse/speech/router.py:322`.

It matches `start_of_utterance` and `pattern_type`.

A first command word enters command buffering at `services/wheelhouse/speech/router.py:327-386`.

A mid-utterance command word becomes dictation at `services/wheelhouse/speech/router.py:414-429`.

A normal word becomes dictation at `services/wheelhouse/speech/router.py:323-325`.

A replacement candidate can buffer at any utterance position at `services/wheelhouse/speech/router.py:388-449`.

The router can execute a command before the utterance ends.

The single-word path does this at `services/wheelhouse/speech/router.py:329-337`.

The buffering path does this at `services/wheelhouse/speech/router.py:505-549`.

The timeout path calls finalization at `services/wheelhouse/speech/router.py:612-630`.

Finalization can execute a complete command at `services/wheelhouse/speech/router.py:648-666`.

Finalization can also execute a command prefix at `services/wheelhouse/speech/router.py:668-747`.

The command prefix behavior does not match the new requirement.

For example, a longer utterance can execute its leading command and dictate the suffix.

### Processor decisions and retraction

`services/wheelhouse/speech/speech_processor.py:486-732` processes each word event in queue order.

`services/wheelhouse/speech/speech_processor.py:597-661` handles the utterance end marker.

It finalizes only the router buffer.

It does not have the complete spoken word list.

`services/wheelhouse/speech/speech_processor.py:734-833` executes router decisions.

The dictation branch starts at line 749.

The execute branch starts at line 802.

Replacement patterns also use the execute branch.

This detail prevents the new word list from being built only in the dictation branch.

`services/wheelhouse/speech/speech_processor.py:1369-1530` handles revisions.

It clears a pending router buffer.

It retracts inserted text.

It then replays the corrected final through normal word processing.

`services/wheelhouse/speech/speech_processor.py:1388-1391` skips this work after a command has executed.

That path discards the corrected final.

The flag is set for leading commands at `services/wheelhouse/speech/speech_processor.py:1158-1181`.

It is also set for trailing commands and bare-number clicks at lines 967-1021 and 1110-1154.

## New Behavior

### Ordinary dictation

The processor will classify only the first word as the leading-command candidate.

`PatternType.NONE` will continue to type immediately.

A mid-utterance command word will continue to type as dictation.

Replacement patterns will continue to use their current buffering and execution paths.

The new design will not wait for utterance end before typing an ordinary utterance.

### Leading command candidates

The first command candidate will enter command buffering as it does today.

The router will not return a command execution decision for a normal word event.

A complete command will remain pending until the utterance end marker.

An impossible command buffer can still fall back to replacement processing or dictation.

A timeout can also release pending words as replacement output or dictation.

The processor will keep the complete spoken word list after either fallback.

This means a command candidate can appear on screen before the pause.

The utterance-end path can retract that text before it executes a complete command.

The user has accepted this visible type-and-retract behavior.

### Utterance-end decision

The processor will run leading-command matching before it consumes a trailing command or a bare-number click.

The processor will skip leading-command matching unless the first word was a command candidate or the command hotword.

The router will match the complete saved utterance with command full-match rules.

The router will preserve hotword authorization.

The router will remove the spoken hotword from the command payload before command matching.

The router will not run the existing longest-prefix command loop.

If the complete utterance matches, the processor will discard any still-hidden command buffer.

It will then retract any text already inserted for this utterance.

A successful retraction permits command execution.

A `nothing_to_retract` response also permits command execution because all candidate words can still be hidden in the Logic process.

Any other retraction response blocks command execution.

The processor must keep the visible text when retraction is blocked.

The processor must provide clear failure feedback when it blocks the command.

If no complete command matches, the processor will finalize any pending buffer with command execution disabled.

It will leave text that was already inserted in place.

It will then retain the existing trailing-command and bare-number behavior.

### Partial commands

An utterance that contains only part of a command will not execute a command.

An utterance that starts with a complete command and then adds unmatched words will not execute the command prefix.

The pending words will become replacement output or dictation.

Already inserted words will remain in place.

This is an intentional change from `SpeechRouter._resolve_finalization`.

### Revisions

A revision marker will cancel the current router buffer and its timer.

It will retract inserted output when output exists.

It will replace the saved word list with the revised transcript.

The normal replay path will classify the revised first word again.

The utterance end marker will then match only the revised complete utterance.

No command will have executed before this sequence.

The corrected final will no longer be discarded because an earlier partial transcript fired a command.

## Per-Utterance Word List

`SpeechProcessor` will own `_utterance_words: list[str]`.

It will also own a boolean that records whether the utterance started as a command candidate.

It will own a conservative boolean that records whether any dictation or replacement path may have emitted text.

It will retain the existing hotword state needed for authorization and fallback dictation.

The processor will append each normal `WordEvent.word` before it asks the router for a decision.

This location captures words that later produce `Action.DICTATE`.

It also captures words that later produce `Action.BUFFER`.

It also captures replacement words that later produce `Action.EXECUTE`.

The processor will set the possible-output boolean before it awaits a dictation insertion or replacement execution.

Setting it before the await covers timeouts whose remote side effect may finish after the Logic-side wait fails.

The processor must not append `before_remainder` or `remainder` text again.

Those strings came from words that are already in the list.

The processor will clear the list after all utterance-end work and the `end_utterance` request finish.

It will clear the list before it starts a new utterance.

It will clear the list on a processing error, shutdown, and an abandoned utterance.

It will clear the list before the standard retraction replay.

The replayed first word will start and rebuild the list.

The persistent editor retraction path does not replay words through `process_word_event`.

That path will replace `_utterance_words` directly from `retraction_full_text.split()` after a successful retract-and-replay response.

It will recompute the leading-command candidate from the first replacement word.

If a new utterance starts without an end marker for the prior utterance, the processor will release the prior buffer as text and clear its saved list.

It will not execute a delayed command without an explicit end marker.

This is the fail-safe result for a broken provider lifecycle.

## Timeout Behavior

`COMMAND_TIMEOUT_MS` is 700 milliseconds in `services/wheelhouse/config.toml.example:25`.

It will remain for compatibility.

It will become the maximum normal delay before an unresolved command candidate falls back to visible text.

It will never authorize a command.

`GREEDY_TIMEOUT_MS` is 5000 milliseconds in `services/wheelhouse/config.toml.example:26`.

It will remain for greedy replacement behavior.

It will also remain the text-fallback delay for a greedy leading-command candidate.

It will never authorize a command.

The utterance end marker will be the only normal command match boundary.

The 2026-08-18 measurement supports this change.

The no-revision maximum was 611 milliseconds.

That leaves only 89 milliseconds below the current command timeout.

Two revised spans exceeded 700 milliseconds.

The revised maximum was 1132 milliseconds.

The sample covers one machine, one speaker, and the parakeet_tdt provider.

Increasing the command timeout would only move the same timing race.

Keeping 700 milliseconds as a text-fallback timer preserves responsiveness.

Waiting for the end marker before command execution removes the timing race.

## Command Execution Flag

The normal retraction gate based on `_command_executed_in_utterance` will be removed.

No leading command will execute before the revision window closes.

Trailing commands and bare-number clicks already execute from the utterance-end path.

The WebSocket manager queues revisions before that path.

The flag assignments for those end-only commands will no longer protect a valid sequence.

The processor will reject stale markers by utterance identity instead of using a command side-effect flag to discard a valid correction.

The valid corrected final will always be processed.

## Required Changes By File

### `services/wheelhouse/config.toml.example`

Update the timeout comments.

State that command timeouts release text but never execute a command.

If this is wrong, an administrator can treat 700 milliseconds as a safety boundary when it is not one.

### `services/wheelhouse/speech/router.py`

Remove every early command execution path from normal word and timeout decisions.

Keep early replacement execution.

Make timeout and impossible-buffer finalization disable command execution.

Add a pure complete-utterance command decision for the utterance-end path.

Use full-match behavior only.

Do not use the command-prefix loop for this decision.

Preserve hotword authorization and raw command payload handling.

If this is wrong, a partial transcript can still fire before a revision.

If prefix matching remains, a partly matching utterance can run a destructive command.

If hotword state is lost, protected commands can execute without authorization or valid commands can fail.

### `services/wheelhouse/speech/speech_processor.py`

Add the per-utterance word list and leading-candidate state.

Capture every normal word before routing.

Move leading-command execution into the utterance-end branch.

Retract emitted text before executing a late match.

Treat `nothing_to_retract` as success only when the Logic process still held the candidate text.

Treat `nothing_to_retract` as a failure when the possible-output boolean is set.

Fail closed on every unsafe retraction response.

Rebuild the word list on both standard and persistent editor revisions.

Clear all new state at every utterance boundary and error boundary.

Remove `_command_executed_in_utterance` from valid retraction handling.

Preserve trailing commands, bare-number clicks, replacements, deferred `end_utterance`, and editor routing.

If capture occurs only in the dictation branch, replacement words will disappear from the complete utterance.

If state clears before end-marker work finishes, commands will never match.

If state clears too late, words from two utterances can form one command.

If command execution continues after a failed retraction, visible command text and command side effects will both remain.

### `services/wheelhouse/main.py`

Change `LogicController.retract_editor_text` at `services/wheelhouse/main.py:9104-9212` to return a structured success or failure result.

Keep the existing GUI response validation.

The speech processor needs the result before it can execute a command whose text reached the persistent editor.

If this method continues to return silently, the speech processor cannot fail closed after an editor retraction failure.

### `services/wheelhouse/integrations/websocket_manager.py`

No production change is required.

Preserve the marker order at `services/wheelhouse/integrations/websocket_manager.py:398-436`.

If that order changes, a command can execute before its correction arrives.

### `services/wheelhouse/speech/pattern_catalog.py`

No production change is required.

`PatternType` and `get_pattern_type` already provide the required first-word classification.

The existing `whole_utterance_only` flag can remain for configuration compatibility.

All leading commands will now receive stronger utterance-end gating.

### `services/wheelhouse/speech/speech_handler.py`

No production change is required if the timeout keys remain compatible.

It can continue to pass both configured timeout values into `SpeechProcessor`.

## Test Plan

### Router tests

Change `services/wheelhouse/tests/test_router_gaps.py`.

Replace tests that expect immediate bounded-command execution with tests that expect buffering or text fallback before utterance end.

Add exact complete-utterance command tests for single-word, multi-word, numeric, hotword-required, and punctuation-normalized commands.

Change `services/wheelhouse/tests/test_router_command_prefix.py`.

Replace command-prefix execution expectations with full-utterance rejection expectations.

Verify that the full text becomes dictation when extra words follow a command.

Change `services/wheelhouse/tests/test_router_greedy_helper.py` and `services/wheelhouse/tests/test_speech_processor_greedy_timer.py`.

Keep timer-selection coverage.

Assert that timer expiry never executes a command.

Change `services/wheelhouse/tests/e2e/test_e2e_greedy_timer_expiry.py` for the same rule.

### Processor and integration tests

Change `services/wheelhouse/tests/test_speech_pipeline.py`.

Assert that no command runs before the utterance end marker.

Assert that a matching command runs immediately after the marker.

Assert that ordinary dictation appears before the marker.

Assert that a partial command and a command with extra words remain text.

Assert that a 700 millisecond timeout can type candidate text but cannot execute it.

Assert that a later exact end match retracts that text and then executes the command.

Assert that `nothing_to_retract` permits a fully hidden command and blocks a command after any path may have emitted text.

Change `services/wheelhouse/tests/test_speech_processor_retraction.py`.

Remove tests that require corrections to be discarded after early command execution.

Add tests for list reset, corrected-list rebuild, chained revisions, and stale utterance identifiers.

Assert that a revision changes the final command decision.

Change `services/wheelhouse/tests/test_retraction_pipeline.py`.

Cover stable command words, a disagreeing final, replay, the end marker, and one final command decision.

Assert that the old partial command never fires.

Change `services/wheelhouse/tests/test_command_engine_replacement_editor_routing.py`.

Assert that replacement words reach the per-utterance word list through `Action.EXECUTE`.

Change `services/wheelhouse/tests/test_speech_processor_editor_multiword_routing.py`.

Assert that persistent editor replay replaces the word list.

Assert that a successful whole-utterance editor retract permits command execution.

Assert that a timeout, generation mismatch, ledger failure, or editor rebuild blocks command execution.

Change `services/wheelhouse/tests/test_retract_editor_text_sender.py`.

Assert that `LogicController.retract_editor_text` returns validated success and failure results.

Change `services/wheelhouse/tests/test_websocket_manager.py`.

Strengthen the Mode 3 test to assert the exact event order.

The retraction marker must precede the utterance end marker for the same utterance identifier.

### Release-gate coverage

Add an integration test for a valid leading voice command.

Add an integration test for an ambiguous or invalid leading command that stays as text.

Add an integration test for a revision that removes a command candidate.

Add an integration test for a revision that creates a command candidate.

Add an integration test for timeout fallback followed by end-marker conversion.

Add an integration test for retraction refusal with clear feedback and no command side effect.

Add an integration test for a missing end marker followed by a new utterance.

The prior text must survive.

The prior command must not execute late.

Repeat the end-marker latency measurement for every shipped STT provider before release.

Include revised and unrevised spans for each provider.

## Risks

[!] The largest risk is the late retract-and-execute step.

A wrong retract can delete user text.

A partial retract can leave command text on screen.

The processor must execute only after an explicit safe result.

[!] A command action can fail after its typed words were retracted.

The user can lose the spoken text and still receive no command result.

Command failure feedback must remain clear.

[!] A provider that omits or reorders the end marker will stop commands from executing.

This fail-safe behavior is safer than a delayed destructive command.

[!] Candidate utterances can show a visible type-and-retract change after the 700 millisecond fallback.

The accepted behavior may still feel unstable on long revisions.

[!] The timing sample covers only one provider, machine, and speaker.

Another shipped STT provider can have a longer or less reliable end-marker delay.

[!] The persistent editor currently hides retract outcomes from the speech processor.

Executing without changing that contract would be unsafe.

[!] The new complete-utterance rule removes existing command-prefix behavior.

Users who rely on a command followed by dictated suffix text will see the entire utterance typed instead.

[!] The word list is raw STT input.

It must not be rebuilt from rendered replacement output.

Doing so would make command matching depend on text insertion formatting.

## Open Questions

None.
