"""Multi-word punctuation names, end to end through the pipeline
(wh-spaced-punctuation-names-unresolved, stage A4).

These tests pass on the code as it ships today and are kept as a
regression net. They exist because the behaviour they pin is NOT what
the bead recorded when it was filed.

The bead was filed 2026-08-18, before whole-utterance matching landed.
It recorded that one utterance of "open single quote" dictated the words
"open single" and then wrapped an empty pair of DOUBLE quotes, and that
"greater than sign" dictated its words instead of inserting '>'. Measured
again 2026-09-05 at be360c26, every one of those cases now produces the
single correct mark: whole-utterance matching answers first and the
word-by-word buffer is never asked. The user-visible failure that remains
is the separate-utterance case (a real pause between the words), which is
stage B and waits on a decision.

So the value here is protection, not proof of a fix. Stage A changed
``extract_full_literal_body`` to accept the ``\b...\b`` shape, which is
the buffer's path, not the whole-utterance path; these tests are what
would catch a later change to whole-utterance matching quietly handing
these names back to the buffer and reintroducing the filed behaviour.
The file holds two groups of tests, and they speak differently.

GROUP 1, the whole-utterance tests, say a name as ONE utterance through
the ``_speak`` helper. They are the Stage A regression net. Ten of their
twelve collected cases assert the exact insertion list: the eight
parametrized cases, ``test_words_on_both_sides_survive`` and
``test_a_quoted_phrase_gets_both_marks``. ``test_open_the_door_is_dictated``
asserts the joined dictation text instead, so it does not constrain how
the words were split into insertions.
``test_no_empty_double_quote_wrap_is_sent`` asserts absences only: no
``wrap_or_insert`` action and no dictated "open single". The four
single-case tests among them (``test_no_empty_double_quote_wrap_is_sent``,
``test_words_on_both_sides_survive``, ``test_a_quoted_phrase_gets_both_marks``
and ``test_open_the_door_is_dictated``) all assert that no
``wrap_or_insert`` was sent, because a stray empty wrap with double-quote
fences is the filed failure's own signature. The eight parametrized cases
check insertions only.

GROUP 2, the paused tests in ``TestAPauseBetweenTheWords``, say each word
as its OWN utterance through ``_speak_with_pauses`` and are Stage B
(wh-spaced-punctuation-names-unresolved.3). They are the case the bead
filed and Stage A did not fix. Each one states its own assertions; they
are not covered by the sentences above.

GROUP 3, the two tests in ``TestACommandPrefixIsUnaffected``, are Stage
B's regression net for the ruling's constraints (a) and (b): holding
"open" must not change how a command starting with "open" behaves. Both
PASS before the Stage B change and must keep passing after it, so
neither is red-first evidence. Both patch ``webbrowser.open``, because
the command they use really opens a browser.

The unit-level tests for the extractor and the matchers are in
tests/test_pattern_transform_boundary_body.py.
"""
import asyncio
import sys
import webbrowser
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness

# The single mark each name asks for. Written as an escape so the file
# stays ASCII: U+00A3 is the pound sign.
POUND = "\u00a3"


async def _speak(words, inter_word_delay_ms=50):
    """Say ``words`` as ONE utterance; return (insertions, actions).

    One utterance means every word carries the same utterance id, the
    first carries start_of_utterance and the last end_of_utterance,
    followed by the end marker the STT bridge sends on a final result.
    """
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        await harness.send_utterance(
            words, inter_word_delay_ms=inter_word_delay_ms
        )
        await harness.send_utterance_end_marker(harness._utterance_counter)
        # This wait is NOT a race margin for the expected output, and the
        # 400 ms replacement timeout is not the number that governs it.
        # The last word carries end_of_utterance, so the pipeline decides
        # and emits during send_utterance above. Measured 2026-09-05: for
        # every case in this file each output -- insertions and
        # end_utterance alike -- is already present 0 ms after the marker
        # call returns, both idle and under 4000 CPU-burning event-loop
        # tasks. Loop congestion cannot shorten this window relative to
        # the pipeline either, because the sleeper is resumed by the same
        # loop that runs the processor task.
        #
        # The wait is a settling window for the NEGATIVE assertions. Four
        # tests assert that no wrap_or_insert was sent
        # (test_no_empty_double_quote_wrap_is_sent,
        # test_words_on_both_sides_survive,
        # test_a_quoted_phrase_gets_both_marks and
        # test_open_the_door_is_dictated); the eight parametrized cases
        # assert exact insertions only. A "not in actions" assertion only
        # means something if a late output would still land inside the
        # window. No late output was observed in these cases -- not even
        # with the shipped defect restored by the gate's
        # the-boundary-shape-is-refused mutation, which produced no
        # wrap_or_insert at all within two seconds, because the filed
        # failure needs the separate-utterance path that is stage B.
        #
        # A regression that routed these names back to the buffer would
        # not change the timing either: the buffer finalizes at once on
        # the last word's end_of_utterance flag
        # (SpeechRouter._decide_buffering in speech/router.py) or on the
        # explicit end marker (SpeechProcessor.process_word_event in
        # speech/speech_processor.py). The 400 ms replacement timeout is
        # a fallback for a buffer that neither of those has finalized;
        # this helper snapshots at 300 ms and then stops the processor,
        # so it does not and cannot observe that fallback. The 300 ms is
        # margin for the four negative assertions, nothing more.
        await asyncio.sleep(0.3)
        outputs = harness.get_outputs()
    finally:
        await harness.stop()
    return (
        [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ],
        [out.action for out in outputs],
    )


async def _speak_with_pauses(utterances, pause_ms=150):
    """Say each entry in ``utterances`` as its OWN utterance.

    ``utterances`` is a list of word lists. Every entry gets its own
    utterance id, its own start and end flags, and its own end marker,
    which is what the STT bridge sends on a final result. ``pause_ms``
    of real time passes between one entry's marker and the next entry's
    first word.

    The pause is measured against the processor's release deadline,
    which this harness sets to ``replacement_timeout_ms=400``
    (tests/test_speech_pipeline.py). A pause below 400 ms leaves a held
    prefix armed when the next utterance opens; a pause above it does
    not. Tests that mean "inside the bound" pass a value well under
    400, and tests that mean "past the bound" pass one well over, so
    neither depends on scheduling noise near the edge.

    Returns the same (insertions, actions) pair ``_speak`` returns.
    """
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        for index, words in enumerate(utterances):
            if index:
                await asyncio.sleep(pause_ms / 1000.0)
                harness._elapsed += pause_ms
                harness.mock_app.set_time(harness._elapsed)
            await harness.send_utterance(words)
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
        # Long enough for the release deadline (400 ms) to fire after the
        # last utterance, so a test that expects the literal words on
        # expiry sees them, and a test that expects a mark has given the
        # slower path its chance to produce a wrong extra output too.
        await asyncio.sleep(0.6)
        outputs = harness.get_outputs()
    finally:
        await harness.stop()
    return (
        [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ],
        [out.action for out in outputs],
    )


async def _speak_with_pauses_remote(utterances, pause_ms=150):
    """Say each entry as its own utterance, in the REMOTE word shape.

    ``_speak_with_pauses`` above sends every utterance through
    ``SpeechPipelineHarness.send_utterance``, which sets
    ``end_of_utterance`` on each utterance's LAST real word. Only the
    in-process bridge builds words that way: ``main.py``
    ``_handle_stt_transcript`` sets ``end_of_utterance=is_last`` (line
    2347). The remote path never does. Every real word
    ``integrations/websocket_manager.py`` builds carries
    ``end_of_utterance=False``; the flag is true only on the empty
    end-marker event, and that is so at all seven of its
    ``end_of_utterance=True`` sites.

    The remote path is the only path a build runs: ``stt.mode``
    (config_service.py:230) answers ``"remote"`` when the ``stt.mode``
    key is absent. So the shape this helper sends is the shipped
    default, and ``_speak_with_pauses`` is the other one.

    Same arguments and same ``(insertions, actions)`` pair as
    ``_speak_with_pauses``.
    """
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        for index, words in enumerate(utterances):
            if index:
                await asyncio.sleep(pause_ms / 1000.0)
                harness._elapsed += pause_ms
                harness.mock_app.set_time(harness._elapsed)
            for position, word in enumerate(words):
                await harness.send_word(
                    word,
                    start_of_utterance=(position == 0),
                    end_of_utterance=False,
                    delay_before_ms=0 if position == 0 else 50,
                )
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
        await asyncio.sleep(0.6)
        outputs = harness.get_outputs()
    finally:
        await harness.stop()
    return (
        [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ],
        [out.action for out in outputs],
    )

class TestOneUtteranceFreshCase:
    """A name spoken as a whole utterance inserts its one mark."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words, mark",
        [
            pytest.param(["open", "single", "quote"], "'", id="open-single"),
            pytest.param(["begin", "single", "quote"], "'", id="begin-single"),
            pytest.param(["close", "single", "quote"], "'", id="close-single"),
            pytest.param(["greater", "than", "sign"], ">", id="greater-than"),
            pytest.param(["less", "than", "sign"], "<", id="less-than"),
            pytest.param(
                ["pound", "sterling", "sign"], POUND, id="pound-sterling"
            ),
        ],
    )
    async def test_a_three_word_name_inserts_its_mark(self, words, mark):
        insertions, actions = await _speak(words)
        assert insertions == [mark], (words, insertions, actions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words, mark",
        [
            pytest.param(["open", "bracket"], "[", id="open-bracket"),
            pytest.param(["question", "mark"], "?", id="question-mark"),
        ],
    )
    async def test_a_two_word_name_still_works(self, words, mark):
        """The two-word names were never broken; keep them pinned."""
        insertions, actions = await _speak(words)
        assert insertions == [mark], (words, insertions, actions)

    @pytest.mark.asyncio
    async def test_no_empty_double_quote_wrap_is_sent(self):
        """The filed failure's signature must not come back.

        The bead recorded an empty ``wrap_or_insert`` with double-quote
        fences after the words "open single" were dictated. Neither the
        words nor the wrap may appear.
        """
        insertions, actions = await _speak(["open", "single", "quote"])
        assert "wrap_or_insert" not in actions, actions
        assert "open single" not in insertions, insertions


class TestTheNameInTheMiddleOfAnUtterance:
    """A name surrounded by dictation resolves without eating it."""

    @pytest.mark.asyncio
    async def test_words_on_both_sides_survive(self):
        insertions, actions = await _speak(
            ["hello", "open", "single", "quote", "world"]
        )
        assert insertions == ["hello", "'", "world"], (insertions, actions)
        assert "wrap_or_insert" not in actions, actions

    @pytest.mark.asyncio
    async def test_a_quoted_phrase_gets_both_marks(self):
        """Two names in one utterance, with dictation between them."""
        insertions, actions = await _speak(
            ["open", "single", "quote", "hello", "close", "single", "quote"]
        )
        assert insertions == ["'", "hello", "'"], (insertions, actions)
        assert "wrap_or_insert" not in actions, actions


class TestOrdinaryDictationIsNotHeldBack:
    """A name's first word followed by other words is still dictation."""

    @pytest.mark.asyncio
    async def test_open_the_door_is_dictated(self):
        """"open" begins several names; "open the door" begins none.

        This is the negative control for the whole stage: the change that
        lets a buffer keep listening for the rest of a name must not let
        it swallow a sentence that merely starts with the same word.
        """
        insertions, actions = await _speak(["open", "the", "door"])
        assert "".join(insertions) == "open the door", (insertions, actions)
        assert "wrap_or_insert" not in actions, actions


class TestAPauseBetweenTheWords:
    """Stage B: a name survives a pause shorter than the release deadline.

    Every test here says each word as a separate utterance, which is the
    case wh-spaced-punctuation-names-unresolved was filed for and the one
    Stage A deliberately left alone. Stage A fixed the whole-utterance
    path only.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "utterances, mark",
        [
            pytest.param(
                [["open"], ["single"], ["quote"]], "'", id="open-single-quote"
            ),
            pytest.param(
                [["question"], ["mark"]], "?", id="question-mark"
            ),
            pytest.param(
                [["open"], ["bracket"]], "[", id="open-bracket"
            ),
        ],
    )
    async def test_a_paused_name_still_inserts_its_mark(
        self, utterances, mark
    ):
        """The three names C1 name, each said with a pause between words."""
        insertions, actions = await _speak_with_pauses(utterances)
        assert insertions == [mark], (utterances, insertions, actions)

    @pytest.mark.asyncio
    async def test_the_pause_inside_one_streaming_utterance_still_works(self):
        """C1's second shape: one utterance stays open across the pause.

        A streaming engine can hold one utterance open while the user
        pauses, so the words arrive without ``start_of_utterance``.

        The gap is 1100 ms, and that number is measured, not chosen.
        "open" is a command prefix as well as a punctuation-name prefix,
        so it buffers in COMMAND_BUFFERING and arms
        ``command_timeout_ms`` (1000 ms in this harness) rather than
        ``replacement_timeout_ms`` (400 ms). Any gap under 1000 ms
        therefore sits inside one ordinary command buffer, the second
        word joins that buffer directly, and the test proves nothing
        about the hold. Measured 2026-09-05 against unmodified code:
        600 ms and 900 ms both produced "[" with no held-prefix line in
        the log at all, while 1100, 1300 and 1500 ms each produced the
        two literal words. 1100 ms is the shortest measured gap that
        reaches the hold question.
        """
        harness = SpeechPipelineHarness()
        await harness.start()
        try:
            await harness.send_word("open", start_of_utterance=True)
            await asyncio.sleep(1.1)
            harness._elapsed += 1100
            harness.mock_app.set_time(harness._elapsed)
            await harness.send_word("bracket", end_of_utterance=True)
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
            await asyncio.sleep(0.6)
            outputs = harness.get_outputs()
        finally:
            await harness.stop()
        insertions = [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ]
        assert insertions == ["["], (insertions, [o.action for o in outputs])

    @pytest.mark.asyncio
    async def test_a_pause_past_the_deadline_types_the_words(self):
        """Expiry with an INCOMPLETE prefix types the literal words.

        The first of the two expiry outcomes. "open" alone is a prefix of
        several names and a whole name of none, so once the deadline
        passes it is just a word the user said.
        """
        insertions, actions = await _speak_with_pauses(
            [["open"], ["bracket"]], pause_ms=900
        )
        assert "".join(insertions) == "openbracket", (insertions, actions)
        assert "[" not in insertions, (insertions, actions)

    @pytest.mark.asyncio
    async def test_a_completed_name_fires_when_the_deadline_passes(self):
        """Expiry with a COMPLETE name fires the mark, and not before.

        The second expiry outcome named in the ruling on this bead. The
        completing word arrives without ending its utterance, so the
        completion rule must NOT fire the mark on arrival; the release
        deadline fires it instead. Without the deadline half, the mark
        would never arrive on a provider that keeps one utterance open
        across the pause.

        The assertion taken BEFORE the deadline is what makes this a
        test of expiry, and it is the half the first version lacked.
        Measured 2026-09-05 against unmodified code, the log for this
        exact sequence reads "REPLACEMENT-PREFIX held" and then
        "REPLACEMENT-PREFIX completed": "mark" arriving 600 ms later
        landed inside the 400 ms release window and completed the name
        on arrival, so the deadline never fired and an end-state-only
        assertion passed while proving nothing about expiry.
        """
        harness = SpeechPipelineHarness()
        await harness.start()
        try:
            await harness.send_word("question", start_of_utterance=True)
            await asyncio.sleep(0.6)
            harness._elapsed += 600
            harness.mock_app.set_time(harness._elapsed)
            await harness.send_word("mark")
            await asyncio.sleep(0.05)
            early = [
                out.params.get("insertion_string", "")
                for out in harness.get_outputs()
                if out.action == "intelligent_insert_text"
            ]
            await asyncio.sleep(0.8)
            outputs = harness.get_outputs()
        finally:
            await harness.stop()
        assert early == [], (
            "the mark fired on arrival, so the deadline never fired",
            early,
        )
        insertions = [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ]
        assert insertions == ["?"], (insertions, [o.action for o in outputs])

    @pytest.mark.asyncio
    async def test_question_mark_my_words_stays_words(self):
        """C2's named separation case, and the whole reason for the rule.

        "question" then "mark my words" must type four words and no
        mark. The words after "mark" are what prove the phrase was
        dictation, so the completing word cannot fire the mark on its
        own.

        The assertion is on the joined text rather than on the split
        into insertions, and the split really did move under
        wh-spaced-punctuation-names-unresolved.3.1. It used to read
        ["question", "mark my words"]: the guard refused the join the
        moment "mark" arrived, because "mark" did not carry
        ``end_of_utterance``. That flag is set on a real word only by
        the in-process bridge, so the refusal never happened on the
        shipped path, and the fix moved the decision one event later --
        the hold now carries "question mark" and flushes both together
        when "my" arrives. Measured after the fix, this shape produces
        ["question mark", "my", "words"] and the remote shape produces
        the same four words. The user sees the same text either way,
        which is why the split is not what this test pins.
        """
        insertions, actions = await _speak_with_pauses(
            [["question"], ["mark", "my", "words"]]
        )
        assert "?" not in insertions, (insertions, actions)
        assert " ".join(insertions) == "question mark my words", (
            insertions,
            actions,
        )

    @pytest.mark.asyncio
    async def test_a_sentence_ending_in_open_types_the_word(self):
        """C2's ordinary-sentence tail: nothing merges across the pause."""
        insertions, actions = await _speak_with_pauses(
            [["hold", "it", "open"], ["bracket"]], pause_ms=900
        )
        assert "[" not in insertions, (insertions, actions)
        assert "".join(insertions) == "holditopenbracket", (
            insertions,
            actions,
        )

    @pytest.mark.asyncio
    async def test_committed_text_is_never_absorbed(self):
        """Words already delivered stay delivered and stay unchanged.

        The first utterance is ordinary dictation that completes before
        the name begins. A later mark must not reach back and swallow
        it, which is the failure the bead recorded in its own way.
        """
        insertions, actions = await _speak_with_pauses(
            [["hello"], ["open"], ["bracket"]]
        )
        assert insertions[0] == "hello", (insertions, actions)
        assert insertions[1:] == ["["], (insertions, actions)


class TestACommandPrefixIsUnaffected:
    """The ruling's constraints (a) and (b) on .3.

    "open" is a punctuation-name prefix AND the opening of a command.
    Method and scope for that claim: grep for ``pattern = .*open`` in
    speech/config/patterns.toml returns six lines, of which line 2558,
    ``^(?:help|open voice access help)$``, is the only command; the
    other five are the punctuation names themselves. That one command
    is why "open" buffers as a command rather than as a replacement,
    which is the whole reason the hold could not reach it before.

    Both tests patch ``webbrowser.open``. Measured 2026-09-05: without
    the patch the real call raises ``TypeError: replace() argument 2
    must be str, not MagicMock`` because the harness supplies a mock
    URL, and the words are then dictated instead. So an unpatched test
    would assert the shape of a failure path, not of the command.
    """

    @pytest.mark.asyncio
    async def test_a_command_starting_with_open_still_executes(
        self, monkeypatch
    ):
        """Constraint (a): no pause, so the command runs as it does today.

        The hold must never shorten or change the command buffer inside
        one utterance. Measured 2026-09-05 against the code before the
        Stage B change: one browser call and no inserted text at all.
        """
        opened = []
        monkeypatch.setattr(
            webbrowser, "open", lambda *a, **k: opened.append(a) or True
        )
        harness = SpeechPipelineHarness()
        await harness.start()
        try:
            await harness.send_utterance(
                ["open", "voice", "access", "help"]
            )
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
            await asyncio.sleep(0.9)
            outputs = harness.get_outputs()
        finally:
            await harness.stop()
        insertions = [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ]
        assert len(opened) == 1, (opened, insertions)
        assert insertions == [], (insertions, [o.action for o in outputs])

    @pytest.mark.asyncio
    async def test_open_then_a_long_pause_keeps_the_rest_separate(
        self, monkeypatch
    ):
        """Constraint (b): a pause past the bound splits the command.

        "open", then a pause longer than the hold's bound, then the rest
        of the command. The first word is typed and the remaining words
        are handled as their own utterance, which is what happens today.
        The command must NOT run: the pause ended it. Measured
        2026-09-05 against the code before the Stage B change.
        """
        opened = []
        monkeypatch.setattr(
            webbrowser, "open", lambda *a, **k: opened.append(a) or True
        )
        insertions, actions = await _speak_with_pauses(
            [["open"], ["voice", "access", "help"]], pause_ms=900
        )
        assert opened == [], (opened, insertions, actions)
        assert insertions == ["open", "voice", "access", "help"], (
            insertions,
            actions,
        )
class TestTheShippedRemoteWordShape:
    """The same paused names, in the word shape the default path sends.

    wh-spaced-punctuation-names-unresolved.3.1, filed by deepseek in
    round 1 of the Stage B review. Every test above in
    ``TestAPauseBetweenTheWords`` speaks through ``_speak_with_pauses``,
    which marks each utterance's last word with ``end_of_utterance``.
    That is the in-process bridge's shape. The shipped default is the
    remote path, which never sets the flag on a real word, so the tests
    above could all pass while the bead's own filed case still typed its
    words for every user of a fresh install.

    Measured before the fix: five of the seven collected cases here
    failed. The three cases of
    ``test_a_paused_name_still_inserts_its_mark_on_the_shipped_shape``
    produced ['open', 'single'] plus an empty double-quote wrap,
    ['question', 'mark'] and ['open', 'bracket'];
    ``test_committed_text_is_never_absorbed_on_the_shipped_shape``
    produced ['hello', 'open', 'bracket']; and
    ``test_a_held_complete_name_fires_when_a_new_utterance_opens``
    produced ['question mark', 'hello']. Those five are the red-first
    evidence.

    The remaining two passed before the fix and must keep passing
    after it. They are what would catch a fix that made the join too
    eager and fused two utterances that are ordinary dictation.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "utterances, mark",
        [
            pytest.param(
                [["open"], ["single"], ["quote"]], "'", id="open-single-quote"
            ),
            pytest.param(
                [["question"], ["mark"]], "?", id="question-mark"
            ),
            pytest.param(
                [["open"], ["bracket"]], "[", id="open-bracket"
            ),
        ],
    )
    async def test_a_paused_name_still_inserts_its_mark_on_the_shipped_shape(
        self, utterances, mark
    ):
        """The bead's filed case, on the path a fresh install runs."""
        insertions, actions = await _speak_with_pauses_remote(utterances)
        assert insertions == [mark], (utterances, insertions, actions)

    @pytest.mark.asyncio
    async def test_question_mark_my_words_stays_words_on_the_shipped_shape(
        self,
    ):
        """The separation case must not fuse on this shape either.

        "question" then "mark my words" is four words and no mark. The
        words after "mark" are what prove the second utterance is
        ordinary dictation, and on this shape they are the ONLY proof
        available: no real word carries ``end_of_utterance``, so the
        decision cannot be taken when "mark" arrives.

        The assertion is on the joined text, not on how the words were
        split into insertions. The split moved with the fix -- the hold
        now carries "question mark" and flushes both together when "my"
        arrives -- and the split is not what the user sees.
        """
        insertions, actions = await _speak_with_pauses_remote(
            [["question"], ["mark", "my", "words"]]
        )
        assert "?" not in insertions, (insertions, actions)
        assert " ".join(insertions) == "question mark my words", (
            insertions,
            actions,
        )

    @pytest.mark.asyncio
    async def test_a_sentence_ending_in_open_types_the_word_on_the_shipped_shape(
        self,
    ):
        """An ordinary sentence tail still merges with nothing."""
        insertions, actions = await _speak_with_pauses_remote(
            [["hold", "it", "open"], ["bracket"]], pause_ms=900
        )
        assert "[" not in insertions, (insertions, actions)
        assert "".join(insertions) == "holditopenbracket", (
            insertions,
            actions,
        )

    @pytest.mark.asyncio
    async def test_committed_text_is_never_absorbed_on_the_shipped_shape(self):
        """Words already delivered stay delivered and stay unchanged."""
        insertions, actions = await _speak_with_pauses_remote(
            [["hello"], ["open"], ["bracket"]]
        )
        assert insertions[0] == "hello", (insertions, actions)
        assert insertions[1:] == ["["], (insertions, actions)

    @pytest.mark.asyncio
    async def test_a_held_complete_name_fires_when_a_new_utterance_opens(self):
        """A new utterance's first word closes a held COMPLETE name.

        The second cell of the same finding. A completing word that does
        not end its utterance leaves the hold carrying a complete name,
        and on the shipped shape that is EVERY completion. The utterance
        end marker normally fires it. When that marker is lost -- which
        this codebase treats as a real failure mode rather than a
        theoretical one, in ``_advance_held_tail``'s own docstring
        at speech_processor.py:650 -- the next utterance's first word
        arrives instead, and it is the same proof that the previous
        utterance ended.

        Timing, measured against the harness rather than chosen.
        "question" is not a command prefix, so its buffer finalizes at
        ``replacement_timeout_ms`` (400 ms here) and the hold's one
        release deadline then runs 400 ms from there. "mark" at 600 ms
        is inside that window and completes the name; "hello" at 700 ms
        is still inside it, so the deadline has not fired and the new
        utterance's first word is what closes the hold.
        """
        harness = SpeechPipelineHarness()
        await harness.start()
        try:
            await harness.send_word("question", start_of_utterance=True)
            await asyncio.sleep(0.6)
            harness._elapsed += 600
            harness.mock_app.set_time(harness._elapsed)
            await harness.send_word("mark")
            await asyncio.sleep(0.1)
            harness._elapsed += 100
            harness.mock_app.set_time(harness._elapsed)
            # No end marker for that utterance: this is the lost-marker
            # shape the docstring above names.
            await harness.send_word("hello", start_of_utterance=True)
            await harness.send_utterance_end_marker(
                harness._utterance_counter
            )
            await asyncio.sleep(0.6)
            outputs = harness.get_outputs()
        finally:
            await harness.stop()
        insertions = [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ]
        assert insertions == ["?", "hello"], (
            insertions,
            [o.action for o in outputs],
        )

# wh-spaced-punctuation-names-unresolved.3.1.3, codex round 2. Every
# three-word replacement name in the shipped catalog, with the pause
# after the FIRST word. The ids carry no spaces: the mutation gate reads
# a collected line as line.split("::")[-1].split(" ")[0], so a space in
# an id would truncate the name and match nothing.
THREE_WORD_NAMES = [
    pytest.param(["open", "single", "quote"], "'", id="open-single-quote"),
    pytest.param(["begin", "single", "quote"], "'", id="begin-single-quote"),
    pytest.param(["end", "single", "quote"], "'", id="end-single-quote"),
    pytest.param(["close", "single", "quote"], "'", id="close-single-quote"),
    pytest.param(["dot", "dot", "dot"], "...", id="dot-dot-dot"),
    pytest.param(["less", "than", "sign"], "<", id="less-than-sign"),
    pytest.param(["greater", "than", "sign"], ">", id="greater-than-sign"),
    pytest.param(
        ["pound", "sterling", "sign"], "\u00a3", id="pound-sterling-sign"
    ),
]


class TestThePauseAfterTheFirstWord:
    """A three-word name paused once, after its FIRST word.

    wh-spaced-punctuation-names-unresolved.3.1.3, filed by codex in
    round 2. "open" then a pause then "single quote" is the ordinary
    way a person says a three-word name with one pause, and it typed
    the words. The hold took "single" across the utterance boundary,
    recorded the join as tentative, and then flushed on "quote" --
    the very word that completes the name -- because the tentative
    branch flushed before the words were combined and offered to the
    matcher. The bare "quote" then routed to the greedy double-quote
    action, so the user also got an empty pair of double quotes.

    The 2 + 1 and 1 + 1 + 1 splits already worked and are kept here as
    controls: a fix that removes the tentative check outright would
    keep these green while breaking the separation case, and a fix that
    only special-cases the last word would break these.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words, mark", THREE_WORD_NAMES)
    async def test_the_pause_after_the_first_word_still_inserts_the_mark(
        self, words, mark
    ):
        """One pause, after the first word, on the bridge shape."""
        insertions, actions = await _speak_with_pauses(
            [words[:1], words[1:]]
        )
        assert insertions == [mark], (words, insertions, actions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words, mark", THREE_WORD_NAMES)
    async def test_the_pause_after_the_first_word_on_the_shipped_shape(
        self, words, mark
    ):
        """The same split on the path a fresh install runs."""
        insertions, actions = await _speak_with_pauses_remote(
            [words[:1], words[1:]]
        )
        assert insertions == [mark], (words, insertions, actions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words, mark", THREE_WORD_NAMES)
    async def test_the_pause_after_the_second_word_still_inserts_the_mark(
        self, words, mark
    ):
        """Control: the 2 + 1 split already worked and must keep working."""
        insertions, actions = await _speak_with_pauses_remote(
            [words[:2], words[2:]]
        )
        assert insertions == [mark], (words, insertions, actions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("words, mark", THREE_WORD_NAMES)
    async def test_a_pause_after_every_word_still_inserts_the_mark(
        self, words, mark
    ):
        """Control: the 1 + 1 + 1 split already worked."""
        insertions, actions = await _speak_with_pauses_remote(
            [[word] for word in words]
        )
        assert insertions == [mark], (words, insertions, actions)

    @pytest.mark.asyncio
    async def test_the_words_after_a_finished_name_are_still_dictation(self):
        """The separation case must survive whatever fixes the split.

        "question" then "mark my words" is the case the bead was filed
        around, and the tentative record is what refuses it on the
        shipped path. A fix that lets a tentative hold absorb any
        further word would insert a mark here.
        """
        insertions, actions = await _speak_with_pauses_remote(
            [["question"], ["mark", "my", "words"]]
        )
        assert "?" not in insertions, (insertions, actions)
        assert " ".join(insertions) == "question mark my words", (
            insertions,
            actions,
        )

    @pytest.mark.asyncio
    async def test_a_completed_name_does_not_absorb_the_words_after_it(self):
        """A name finished across the boundary, then ordinary words.

        "open" then "single quote please" completes the name on
        "quote" and then keeps going. The words after it prove the
        second utterance was dictation, which is the same rule the
        separation case above states, so all four words type and no
        mark is inserted.
        """
        insertions, actions = await _speak_with_pauses_remote(
            [["open"], ["single", "quote", "please"]]
        )
        assert "'" not in insertions, (insertions, actions)
        assert " ".join(insertions) == "open single quote please", (
            insertions,
            actions,
        )
