"""BareNumberHolds: the bare-number and spoken-click holds (overlay click N).

wh-full-refactor.4.3 (stage B of place 2) moved this code out of
SpeechProcessor in speech/speech_processor.py without a change in behavior.
The stage A boundary tests (tests/test_overlay_stack_requirements_s3.py,
docs/design/overlay-stack-requirements.md, rows BN-n) pin that behavior.
SpeechProcessor creates one instance as self.bare_number_holds and calls in
through it; this class reads the SpeechProcessor members it does not own
through self._host (see bare_number_host.py). It runs in the Logic process.
"""

import logging
from typing import TYPE_CHECKING, Optional

from utils.redact import redact_transcript
from utils.trace_context import elapsed_ms

from .click_parser import _NUMBER_FILLERS, _TRAILING_PUNCT
from .number_word_parser import parse_number_word
from .word_event import WordEvent

if TYPE_CHECKING:
    from .bare_number_host import BareNumberHost

# The logger of speech_processor.py, by name: the moved code logs exactly the
# records it logged before the move, so log routing and the tests that filter
# on the logger name see no change.
logger = logging.getLogger(__name__.rpartition(".")[0] + ".speech_processor")
pipeline_logger = logging.getLogger("wheelhouse.pipeline")


class BareNumberHolds:
    """The bare-number hold and the spoken-click hold of one SpeechProcessor."""

    def __init__(self, host: "BareNumberHost") -> None:
        """``host`` is the SpeechProcessor that owns this object."""
        self._host = host

    # ========================================================================
    # BARE-NUMBER CLICKS (wh-click-number-dictation)
    # ========================================================================
    #
    # The rule was added for the Distil-Whisper engine (since removed),
    # which dropped the word "click" from short "click N" finals
    # (live evidence 2026-08-08: segments oscillate ' click six.' / ' 6.'
    # and the FINAL keeps only the number); it stays in place. The bare
    # number matches no command pattern and would dictate. While the
    # overlay is showing badges, a FINAL utterance that is JUST a number
    # 1..999 is almost certainly a badge pick, so it is held back from
    # dictation and, at the utterance-end marker, executed as the click
    # command "click N".
    #
    # The lifecycle:
    #   - held only when the word opened its utterance, parses as a
    #     number 1..999, and the overlay state machine is in a
    #     badge-showing state (painted / refresh_in_flight);
    #   - extended by a following word when the words so far still
    #     parse as a number 1..999, because a user reading badge 23
    #     says "twenty three". The extension runs after the router has
    #     classified the word, so a word the router wanted for a
    #     command is never swallowed. This hold reaches only the
    #     numbers whose FIRST word no whole-utterance pattern claims:
    #     "twenty three" opens it, "one twelve" does not, because a
    #     grid pattern takes "one" as a command and the router buffers
    #     it (wh-overlay-count-homophones.1.3, measured). Badge 112
    #     said as "one twelve" is clicked by the finalized-payload
    #     check in the DICTATE branch instead, which is why that check
    #     exists;
    #   - flushed back to dictation by any regular event (the number was
    #     not the whole utterance), by a new-utterance auto-finalize, or
    #     conservatively at shutdown;
    #   - consumed at the utterance-end marker. The retraction-replay
    #     path is covered by the same marker: the websocket manager
    #     queues the end marker directly behind every retraction marker,
    #     so a replayed bare-number final is held during the replay and
    #     consumed one event later.

    def _overlay_accepts_bare_number(self) -> bool:
        """True when the overlay state machine is showing badges.

        Reads the state's VALUE string through
        ``SpeechProcessor.overlay_state_value``, which gives None when no
        controller or no machine exists yet (legacy fixtures, early
        startup), and compares that string rather than the OverlayState
        enum member. This module can be
        imported under two roots (``speech.`` and
        ``services.wheelhouse.speech.``); enum members from the two
        module copies never compare equal, so an identity comparison
        here could silently disable the feature. The value strings are
        the stable, IPC-visible names of ``OverlayState.PAINTED`` and
        ``OverlayState.REFRESH_IN_FLIGHT`` -- the derived "is the
        overlay usable" rule from the state machine's contract.
        """
        return self._host.overlay_state_value() in ("painted", "refresh_in_flight")

    def maybe_hold_bare_number(self, text: str) -> bool:
        """Hold ``text`` as a bare-number click candidate if it qualifies.

        Returns True if the text was captured (caller MUST NOT dispatch
        it to dictation). Qualifies only when the text is a single word
        that opened its utterance, the overlay is currently showing
        badges, and the word -- ignoring terminal punctuation on it
        (_spoken_number_text) -- either parses as a number 1..999 or is
        the "number"/"numbers" filler that can only precede one.
        Mid-utterance numbers always dictate. A multi-word payload (from buffer
        finalization) goes to _maybe_hold_spoken_click, which holds only
        the spoken "click N" form; every other multi-word payload
        dictates.

        The filler is here because the recogniser drops the leading
        "click": the user says "click number three" and the final is
        "number three". parse_number_word reads that as 3 -- it supports
        the form for exactly this reason -- but "number" ALONE parses to
        None, so the hold never opened and both words dictated. David
        ruled on 2026-08-27 that "number n should click bubble n"
        (wh-click-number-dictation.1.2).

        Two shipped inputs open the filler hold (wh-full-refactor.4.4
        corrected the earlier text, which said that none did). "number"
        and "numbers" are the first word of the grid-number-prefixed
        pattern, so the router buffers them as a COMMAND. (a) "numbers"
        alone, badges painted, then the end marker: the word opened the
        utterance and current_word_starts_utterance keeps that value, so
        the marker finalization opens the hold; the held word does not
        parse as a number and is typed (boundary test BN-14b). (b)
        "number" with no end marker, then a new utterance: the
        auto-finalize opens the hold, because the flag belongs to the new
        word (boundary test BN-9b). David's ruling for "number N" is
        honoured by the finalized-payload check in the DICTATE branch of
        SpeechProcessor._execute_decision, which sends "number seventy
        four" and "numbers 75" to the badge click.

        A hold opened this way is the first that does not parse as a
        number on its own, which is what
        consume_pending_bare_number's parse check is for.
        """
        if not text:
            return False
        if " " in text.strip():
            return self._maybe_hold_spoken_click(text)
        if not self._host.current_word_starts_utterance:
            return False
        # wh-number-badge-problems.1.4 (boss ruling 2026-09-02): the same
        # terminal punctuation the spoken-click hold ignores reaches the
        # bare hold too, where "74." parsed to None and the badge was
        # never selected. The held word stays raw; only these guards read
        # the stripped form, and the command text keeps its punctuation
        # because ClickCommandParser strips it again (click_parser.py:170).
        bare = self._spoken_number_text([text])
        if (
            # wh-overlay-count-homophones: aliases so a badge number the
            # engine returned as "to", "too" or "for" opens the hold. The
            # badges-showing gate below is what keeps the words typable in
            # ordinary dictation, where no hold ever opens.
            #
            # Measured, wh-overlay-count-homophones.1.1: for the three
            # homophones this branch is unreachable today, and the alias
            # here changes nothing that ships. The router classifies a
            # lone "to", "too" or "for" as a COMMAND -- the grid patterns
            # match the whole utterance -- so it buffers and never
            # reaches this DICTATE branch. Those words take the grid
            # actions' badge fallback instead
            # (ActionFunctions._click_badge_instead_of_dictating). The
            # alias stays for the numbers the grid patterns do NOT claim,
            # and so this guard reads a spoken number the same way every
            # other site does; a mutation of it is caught only by the
            # unit tests that call this helper directly.
            parse_number_word(bare, aliases=True) is None
            # .strip() because _spoken_number_text removes punctuation and
            # not whitespace, and this check compares the whole string
            # against the filler set. parse_number_word strips its own
            # input, so only this side needs it.
            and bare.strip().casefold() not in _NUMBER_FILLERS
        ):
            return False
        if not self._overlay_accepts_bare_number():
            return False
        self._host.pending_bare_number_words = [text]
        pipeline_logger.info(
            "BARE-NUMBER candidate held word=%r elapsed_ms=%.1f",
            redact_transcript(text), elapsed_ms(),
        )
        return True

    # crewcut: these verbs mirror click_element's trigger
    # "^(?:click|clicks|tap)" in action_catalog.py; a user-customized
    # trigger is not consulted. Read them from the catalog entry once the
    # catalog exposes its verbs. "clicks" is left out: the click pattern
    # now runs without the safety word (wh-safety-word-free-commands), so a
    # "clicks ..." buffer goes straight to click_element and this fallback
    # hold never sees it (wh-clicks-spoken-click).
    _SPOKEN_CLICK_VERBS = ("click", "tap")

    @staticmethod
    def _spoken_number_text(tokens: list[str]) -> str:
        """Held or spoken tokens, ready for ``parse_number_word``.

        wh-number-badge-problems.1.4 (codex round 4): local STT appends
        terminal punctuation to the last word of an utterance, so a
        spoken "click 74" arrives as "click 74." and
        ``parse_number_word("74.")`` is None. click_parser strips
        ``_TRAILING_PUNCT`` from its own final token for the same
        reason; this is that rule for both number holds, reusing
        click_parser's own constant
        because the command text they build is what click_parser
        parses. Only the LAST token is stripped, so a
        payload whose other words carry punctuation ("click 74, please")
        still fails to parse and dictates. The caller keeps the raw
        text: every dictation fallback must type the punctuation the
        user's dictation would have carried.
        """
        if not tokens:
            return ""
        stripped = list(tokens)
        stripped[-1] = stripped[-1].rstrip(_TRAILING_PUNCT)
        return " ".join(token for token in stripped if token)

    def utterance_end_is_confirmed(
        self, word_event: Optional[WordEvent],
    ) -> bool:
        """True when this finalization stands on a CONFIRMED utterance end.

        Two signals, and only these two.
        ``marker_finalization_active`` covers BOTH marker-driven
        finalizations -- the end-marker branch's and the Mode 1
        lifecycle-reset branch's -- each setting the flag around
        exactly its own ``_execute_decision`` call. A ``word_event``
        whose ``end_of_utterance`` flag sits on a
        REAL word covers the in-process bridge, which flags the
        utterance's last real word and finalizes through router step 1
        before the queued marker arrives
        (wh-whole-utterance-command-matching.3.1.1). Marker events
        themselves are excluded: the harness end marker and
        websocket_manager's finalize and retraction markers carry flags
        of their own and are not the utterance's last spoken word.

        Timeout and auto-finalize payloads answer False -- their calls
        pass no ``word_event`` and run outside both marker branches, so
        nothing has yet proven that the words so far were the whole
        utterance.

        The lifecycle reset answers True, deliberately.
        wh-whole-utterance-command-matching.3.1.6 and .3.1.8 (boss
        rulings, option (a)) declare that marker a confirmed utterance
        end, and the branch at the ``is_lifecycle_reset_marker`` close
        has clicked a HELD bare number there since before this file
        carried this helper. The marker is not the evidence-free
        client-side timer it resembles:
        ``integrations/websocket_manager.py`` reaches Mode 1 only when
        the server's final does not extend the stable, there was no
        stable disagreement, no end-of-speech arrived, and the server's
        own ``final_reason`` is GOOGLE_SILENCE_2S, EOS_FALLBACK or
        NO_TEXT_TIMEOUT -- all server-side silence or end-of-speech
        finalizations. wh-overlay-count-homophones.1.6 (codex round 6)
        read the earlier wording here as describing a defect; the
        wording was wrong, not the code, and
        tests/test_grid_number_badge_click.py now pins the behaviour.
        First written for the trailing-command split (wh-2vz, removed
        by wh-remove-trailing-submit) and extracted for
        wh-overlay-count-homophones.1.4, which needs the same rule.
        """
        return self._host.marker_finalization_active or (
            word_event is not None
            and word_event.end_of_utterance
            and bool(word_event.word)
            and not word_event.is_utterance_end_marker
            and not word_event.is_timeout_finalize_marker
            and not word_event.is_retraction_marker
            and not word_event.is_lifecycle_reset_marker
        )

    def payload_is_the_whole_utterance(self, text: str) -> bool:
        """True when ``text`` is every word of the utterance, in order.

        The rule two badge-click paths share, in one place. A click is a
        click only when the user said nothing else: "hello click 74"
        types, and so does "hello one twelve". ``words_this_utterance``
        is the utterance as spoken, so any word in it that ``text`` does
        not account for proves the payload was a tail, not the whole.
        Casefolded on both sides because the recogniser capitalises the
        first word of an utterance.
        """
        spoken = [word.casefold() for word in self._host.words_this_utterance]
        return [token.casefold() for token in text.split()] == spoken

    def _maybe_hold_spoken_click(self, text: str) -> bool:
        """Hold a finalized "click N" buffer as a badge click if it qualifies.

        wh-number-badge-problems.2: the shipped recogniser keeps the word
        "click", so "click 74" opens command buffering. When click_element
        needed the safety word, router._cannot_match deferred the buffer as
        impossible and the end marker finalized it as dictation (David's
        evidence log of 2026-09-02, lines 21777-21806); this hold turned
        that buffer into the badge click David ruled for on 2026-08-27
        (C7), exactly like the bare number whose verb the recogniser
        dropped. Since wh-safety-word-free-commands click_element runs
        without the safety word, so "click 74" reaches the overlay route
        directly and this hold is only a fallback for a buffer that still
        finalizes as dictation. Qualifies only when the text is the whole utterance,
        opens with "click" or "tap", the rest -- optionally after one
        "number"/"numbers" filler, and ignoring terminal punctuation on
        the last token (_spoken_number_text) -- parses as a number
        1..999, and the overlay is showing badges. "click 0",
        "click 1000", "click submit button" and "click 74 please" all
        dictate as before.

        The one held word is the spoken payload itself ("click 74",
        "tap seventy four"), the same shape the bare path holds, so every
        dictation fallback -- the flush when a later word or the next
        utterance proves the payload was not the whole utterance, the
        overlay re-check at consume time, a click pattern that does not
        match -- types exactly the string the pre-fix finalization typed,
        in one insertion (wh-number-badge-problems.1.3).
        _held_bare_number_command derives "click <N>" in digits from it
        at consume time, whatever form was spoken. The whole-utterance
        check replaces the bare number's current_word_starts_utterance
        check, which is False at marker finalization.
        """
        tokens = text.split()
        if len(tokens) < 2:
            return False
        if tokens[0].casefold() not in self._SPOKEN_CLICK_VERBS:
            return False
        # wh-overlay-count-homophones: aliases, so "click for" is held as a
        # spoken badge click rather than finalized as dictation.
        number = parse_number_word(
            self._spoken_number_text(tokens[1:]), aliases=True,
        )
        if number is None:
            return False
        if not self.payload_is_the_whole_utterance(text):
            return False
        if not self._overlay_accepts_bare_number():
            return False
        self._host.pending_bare_number_words = [text]
        pipeline_logger.info(
            "BARE-NUMBER candidate held from spoken click text=%r "
            "number=%d elapsed_ms=%.1f",
            redact_transcript(text), number, elapsed_ms(),
        )
        return True

    def is_spoken_click_hold(self, held: Optional[list[str]]) -> bool:
        """True when the held words are a spoken click, not a bare number.

        A bare hold can never open with "click" or "tap" -- neither word
        parses as a number nor is a "number"/"numbers" filler -- so the
        verb alone tells the two holds apart. That is the same
        discriminator _held_bare_number_command uses to decide which
        command text to build.
        """
        if not held:
            return False
        tokens = " ".join(held).split()
        return (
            len(tokens) >= 2
            and tokens[0].casefold() in self._SPOKEN_CLICK_VERBS
        )

    def arm_spoken_click_release(self) -> None:
        """Arm the deadline that releases a held spoken click.

        wh-number-badge-problems.1.6 (codex round 6). The hold waits for
        the utterance-end marker, and integrations/websocket_manager.py
        builds that marker from the terminal FINAL message, so a dropped
        message takes the marker with it; its idle watchdog
        (_fire_idle_watchdog) only writes the GUI activity state. A user
        who then stops speaking sends no further event, so without this
        deadline the spoken click is lost in silence -- no click, and not
        even the text the pre-fix finalization typed.

        Only the spoken-click hold is armed. The bare-number hold waits
        the same way, which codex recorded as a separate, pre-existing
        question outside this branch's scope. ``command_timeout_ms`` is
        the duration because the held words are a command, and it is the
        timeout the router already gives a command buffer.
        """
        if not self.is_spoken_click_hold(self._host.pending_bare_number_words):
            return
        self._host.start_timeout(self._host.command_timeout_ms)

    def _held_bare_number_command(self, held: list[str]) -> Optional[str]:
        """The click command the held words name, or None to dictate them.

        A bare-number hold keeps the spoken words and runs
        "click <words>", the command text a spoken "click N" produces. A
        spoken-click hold keeps the whole spoken payload -- "click 74",
        "tap seventy four", "click number 74", "click 74." -- and runs
        "click <N>" with the number in digits, so the click_parser sees
        one form whatever the user said and whatever punctuation the
        recogniser appended. Held words that name no number (the lone
        "number"/"numbers" filler the bare path can hold) return None.
        A bare hold can never open with "click" or "tap" (neither parses
        as a number nor is a filler), so the verb alone tells the two
        holds apart.
        """
        tokens = " ".join(held).split()
        if len(tokens) >= 2 and tokens[0].casefold() in self._SPOKEN_CLICK_VERBS:
            # wh-overlay-count-homophones: aliases at both branches, so the
            # command text this builds names the same badge the hold opened
            # on. The spoken-click branch resolves to digits ("click for"
            # -> "click 4"); the bare branch keeps the spoken words and
            # main.py resolves them.
            number = parse_number_word(
                self._spoken_number_text(tokens[1:]), aliases=True,
            )
            return None if number is None else f"click {number}"
        if parse_number_word(
            self._spoken_number_text(held), aliases=True,
        ) is None:
            return None
        return f"click {' '.join(held)}"

    def bare_number_extends(self, text: Optional[str]) -> bool:
        """True when ``text`` would keep the held words a valid number.

        Answers the flush guard's question: does this next word
        continue the held bare number ("twenty" + "three" = 23), or
        does it prove the number was not the whole utterance ("6" +
        "pack")? A single token only, and the overlay must still be
        showing badges, so a number whose badges vanished mid-utterance
        stops growing and dictates. Terminal punctuation on the new word
        is ignored (_spoken_number_text), so "seventy" + "four?" is
        still 74.
        """
        held = self._host.pending_bare_number_words
        if not held or not text or " " in text.strip():
            return False
        if not self._overlay_accepts_bare_number():
            return False
        # wh-overlay-count-homophones: aliases, so "twenty" + "for" stays a
        # growing number (24) instead of flushing the hold to dictation.
        return parse_number_word(
            self._spoken_number_text(held + [text]), aliases=True,
        ) is not None

    def extend_bare_number_hold(self, text: str) -> bool:
        """Append ``text`` to the held bare number if it still parses.

        Returns True if the word was captured (caller MUST NOT dispatch
        it to dictation).
        """
        held = self._host.pending_bare_number_words
        if held is None or not self.bare_number_extends(text):
            return False
        held.append(text)
        pipeline_logger.info(
            "BARE-NUMBER candidate extended words=%r elapsed_ms=%.1f",
            redact_transcript(" ".join(held)), elapsed_ms(),
        )
        return True

    def drop_bare_number_hold(self, reason: str) -> None:
        """Drop a held bare number without typing it or clicking it.

        The third way the hold can end, beside the flush below that types
        the words and the consume after it that clicks them. This one is
        for the cases where the held words have stopped belonging to
        anything the user is still saying. Two callers:
        the per-word exception handler in _processing_loop, which has
        made this drop since wh-click-number-dictation.1.6, and
        LogicController._switch_stt_provider, from the two places in its
        remote branch where the outgoing engine is known to be unable to
        finish the hold -- either arm, once the old process has been
        seen to exit (wh-provider-switch-stale-hold). A third place used
        to be the in-process branch, where STTManager swapped the
        provider without raising; wh-in-process-capture-removal deleted
        that branch with the engine it drove.

        Two weaker triggers are deliberately refused, and each was used
        first. Merely having SENT the stop is not enough: that send can
        fail and the old engine can survive the whole switch
        (wh-provider-switch-stale-hold.1.1). A successful SPAWN of the
        replacement is not enough either: start_provider returns on
        subprocess.Popen, the replacement can fail its own startup
        afterwards, and the engine it replaced keeps transcribing
        (wh-provider-switch-stale-hold.2.1). Both mistake an attempt for
        an outcome, and both throw away a number the user is still in
        the middle of saying to an engine they still have. stop() is
        deliberately
        NOT a caller: it drops the held tail whatever kind it carries,
        and routing it here would narrow that to BARE_NUMBER and strand a
        replacement-prefix tail across shutdown.

        A hold survives a provider switch because nothing in the switch
        reaches the processor: the word queue and its consumer outlive
        both engines. The first word the NEW engine delivers opens a new
        utterance, and the new-utterance guard in maybe_hold_bare_number
        flushes the standing hold as dictation -- so a number spoken to
        the engine that is gone is typed into whatever the user is
        working in, with no badge click and nothing on screen to connect
        it to. That guard is right for two utterances from one engine; it
        cannot see that the engine was replaced between them.

        Three limits, all deliberate, all left for later work.

        The two other tray paths that replaced the engine without
        dropping the hold are gone. LogicController's credentials handler
        and its hard STT restart, reached by the tray items "Google Cloud
        Credentials" and "Restart Transcription Service", were removed on
        David's order (wh-remove-restart-credentials-items,
        QUESTIONS-2026-09-04.md item 20), with the hold they stranded as
        one of his reasons (wh-provider-switch-stale-hold.1.3). The
        switch is now the only path here that replaces the engine.

        crewcut: this is a point-in-time clear, so it says nothing about
        words the outgoing engine has already sent
        (wh-provider-switch-stale-hold.1.2). The switch drops the hold as
        soon as the new engine is launched, but the old process keeps its
        socket until it exits, and WebSocketManager does not
        filter transcript frames by client, so
        an old-engine word can still reach the queue afterwards and open
        a fresh hold or click a badge of its own. Removing the limit
        means giving those frames an identity the processor can reject --
        adding an active-client gate in websocket_manager.py, or
        draining the queue at the switch -- neither of
        which belongs to a change scoped to the hold.

        crewcut: the switch's proof of the exit is a bounded wait, so
        an outgoing process that outlives it is treated as still running
        and the hold is kept. If that process then exits, nothing drops
        the hold and the first word of the next engine still flushes it
        as dictation -- the original harm, in a narrower cell than
        before (wh-provider-switch-stale-hold.2.1). The wait is
        _PROVIDER_EXIT_WAIT_S in main.py, ten seconds. Lengthening it is
        not the way out, because no finite
        wait closes the cell and every extra second is a second the
        switch's own cleanup is still on the loop. Removing the limit
        needs the same thing the limit below needs: an identity on
        transcript frames that lets the processor reject the old
        engine's words outright, at which point the hold does not have
        to be dropped at the switch at all.

        crewcut: the held words are sacrificed rather than typed, the
        same trade-off stop() makes for its own reason. For the switch,
        typing them would keep what the user said, but a switch is often
        the user saying the transcription is not working, which makes the
        held words the least trustworthy text in the process; the
        handler's reason is at its call site and is a different one. To
        remove the limit for the switch, dictate the words here once a
        switch can tell "this engine misheard me" apart from "this engine
        is fine and I want the other one".

        Synchronous on purpose: it sends no IPC, so it cannot stall a
        caller that is midway through swapping engines.
        """
        pending = self._host.pending_bare_number_words
        deferred = self._host.bare_number_deferred_word
        # Assigning None to the property clears the held tail only while
        # the tail carries BARE_NUMBER, which is what the two slots
        # named here mean. A tail held for another kind is left alone.
        self._host.pending_bare_number_words = None
        self._host.bare_number_deferred_word = None
        if pending or deferred:
            pipeline_logger.info(
                "BARE-NUMBER candidate dropped reason=%s words=%r "
                "deferred=%r elapsed_ms=%.1f",
                reason,
                redact_transcript(" ".join(pending or [])),
                redact_transcript(deferred or ""),
                elapsed_ms(),
            )

    async def flush_pending_bare_number_as_dictation(self) -> None:
        """Dispatch the held bare number as ordinary dictation.

        Called on every event that proves the held number was not the
        whole utterance. Clears the pending slot before the IPC so a
        failure cannot leak the word into the next consume path.
        """
        pending = self._host.pending_bare_number_words
        if not pending:
            return
        self._host.pending_bare_number_words = None
        self._host.bare_number_deferred_word = None
        pipeline_logger.info(
            "BARE-NUMBER candidate flushed-as-text words=%r elapsed_ms=%.1f",
            redact_transcript(" ".join(pending)), elapsed_ms(),
        )
        # One IPC per held word, in the spoken order. Sending the joined
        # text instead would change what a multi-word hold types.
        for held_word in pending:
            await self._host.send_to_dictation(held_word)

    async def consume_pending_bare_number(self) -> None:
        """Execute the held bare number as the click command "click N".

        Called from the utterance-end marker handler, which proves the
        number was the whole utterance. Re-checks the overlay state at
        consume time: if the badges disappeared between hold and
        utterance end, the number is ordinary dictation after all.

        The click is synthesized as the command text "click <word>"
        (_held_bare_number_command: the spoken words for a bare hold, the
        number in digits for a spoken-click hold) and run through the
        same TextParser path a spoken "click N" takes, so downstream
        behaviour (state-aware badge routing, notices, trace ids) is
        identical. If the click pattern does not match -- a user pattern
        override changed it -- the fallback dictates what was actually
        SPOKEN (the bare number, or the whole spoken "click N"), never
        the synthesized text.
        """
        pending = self._host.pending_bare_number_words
        if not pending:
            return
        self._host.pending_bare_number_words = None
        self._host.bare_number_deferred_word = None
        if not self._overlay_accepts_bare_number():
            pipeline_logger.info(
                "BARE-NUMBER overlay no longer showing badges; dictating "
                "words=%r elapsed_ms=%.1f",
                redact_transcript(" ".join(pending)), elapsed_ms(),
            )
            for held_word in pending:
                await self._host.send_to_dictation(held_word)
            return
        if not await self._execute_bare_number_click(pending):
            for held_word in pending:
                await self._host.send_to_dictation(held_word)

    async def _execute_bare_number_click(self, words: list[str]) -> bool:
        """Run ``words`` as the click command. True when the click ran.

        The one place that turns a spoken bare number into a click. Two
        callers reach it: ``consume_pending_bare_number`` for a number
        the hold captured, and ``try_bare_number_badge_click`` for a
        number the grid patterns claimed before the hold could see it
        (wh-overlay-count-homophones.1.1). Neither dictates from here --
        a False return tells the caller to type what was SPOKEN, which
        differs between them: the hold types its held words one by one,
        the grid path types the whole utterance through its own
        dictation fallback so the parse is recorded as a fallback.

        Returns False when the words name no number, and when the click
        command did not match -- a user pattern override can change it.
        """
        command_text = self._held_bare_number_command(words)
        if command_text is None:
            # The hold can open on the "number"/"numbers" filler, which
            # parses to None by itself, so a hold can reach this point
            # holding a word that is not a number. Synthesizing "click
            # number" would be worse than dictating: click_parser drops
            # that filler only when something parsing follows it
            # (click_parser.py:189), so a lone one resolves BY NAME and
            # would click an element called "number". Every hold before
            # the filler parsed on its own and could not reach here
            # (wh-click-number-dictation.1.2).
            pipeline_logger.info(
                "BARE-NUMBER held words do not parse as a number; dictating "
                "words=%r elapsed_ms=%.1f",
                redact_transcript(" ".join(words)), elapsed_ms(),
            )
            return False
        pipeline_logger.info(
            "BARE-NUMBER consumed as command=%r elapsed_ms=%.1f",
            redact_transcript(command_text), elapsed_ms(),
        )
        logger.info(
            f"Executing bare-number click: '{redact_transcript(command_text)}'"
        )
        executed = await self._host.text_parser.parse_and_execute(
            command_text, authorized_command=True,
        )
        if not executed:
            return False
        if self._host.text_parser.last_executed_pattern_type == "command":
            # A click is an irreversible side effect; a later STT
            # revision must not retract past it (same rule as
            # _execute_command).
            self._host.command_executed_in_utterance = True
        return True

    async def try_bare_number_badge_click(self, spoken: str) -> bool:
        """Click the badge ``spoken`` names, if badges are showing.

        wh-overlay-count-homophones.1.1. The three grid patterns match
        the whole utterance for every spoken 1..9, so the router
        classifies "six", "7", "number three", "to", "too" and "for" as
        COMMAND and finalizes them into ActionFunctions.
        grid_number_command. The bare-number hold lives under the
        DICTATE branch, so those words never reach it, and before this
        they typed while badges were showing. The grid actions call this
        from their closed-grid fallback, which keeps the precedence the
        grid already had: the grid consumes the number first, and the
        overlay is consulted only when the grid did not.

        Returns True when the click ran and the caller must NOT dictate.
        Returns False -- badges not showing, or the click did not run --
        and the caller types the words as before.
        """
        if not self._overlay_accepts_bare_number():
            return False
        return await self._execute_bare_number_click(spoken.split())
