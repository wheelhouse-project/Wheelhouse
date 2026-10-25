"""Tests for cursor navigation utterance parser."""

import pytest

from speech.navigation.parser import NavigationParser
from speech.navigation.models import NavigationCommand


class TestLandmarks:
    @pytest.mark.parametrize("utterance,landmark", [
        ("go home", "home"),
        ("go end", "end"),
        ("go top", "top"),
        ("go bottom", "bottom"),
    ])
    def test_simple_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]

    @pytest.mark.parametrize("utterance,landmark", [
        ("go start of word", "start_of_word"),
        ("go beginning of word", "start_of_word"),
        ("go end of word", "end_of_word"),
        ("go start of paragraph", "start_of_paragraph"),
        ("go end of paragraph", "end_of_paragraph"),
    ])
    def test_compound_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]


class TestOptionalToAfterGo:
    """wh-ed4: 'go to <landmark>' must parse identically to 'go <landmark>'."""

    @pytest.mark.parametrize("utterance,landmark", [
        ("go to home", "home"),
        ("go to end", "end"),
        ("go to top", "top"),
        ("go to bottom", "bottom"),
    ])
    def test_go_to_simple_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]

    @pytest.mark.parametrize("utterance,landmark", [
        ("go to start of word", "start_of_word"),
        ("go to end of word", "end_of_word"),
        ("go to start of paragraph", "start_of_paragraph"),
        ("go to end of paragraph", "end_of_paragraph"),
    ])
    def test_go_to_compound_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]

    def test_bare_go_to_is_unparseable(self):
        assert NavigationParser.parse("go to") is None


class TestRelative:
    def test_go_right_default(self):
        cmds = NavigationParser.parse("go right")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=1, unit="character"
        )]

    def test_go_left_with_count_and_unit(self):
        cmds = NavigationParser.parse("go left three words")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="left", count=3, unit="word"
        )]


class TestGrabToUnchanged:
    """Ensure wh-ed4 fix does not regress existing 'grab to <landmark>' behavior."""

    def test_grab_to_end(self):
        cmds = NavigationParser.parse("grab to end")
        assert cmds == [NavigationCommand(verb="grab", kind="landmark", landmark="end")]

    def test_grab_relative(self):
        cmds = NavigationParser.parse("grab left two words")
        assert cmds == [NavigationCommand(
            verb="grab", kind="relative", direction="left", count=2, unit="word"
        )]


class TestChaining:
    def test_then_chain_with_optional_to(self):
        cmds = NavigationParser.parse("go to end then go home")
        assert cmds == [
            NavigationCommand(verb="go", kind="landmark", landmark="end"),
            NavigationCommand(verb="go", kind="landmark", landmark="home"),
        ]


class TestWriteIsHeardForRight:
    """"write" is the same direction as "right". wh-voice-access-parity.4.

    The shipped speech model returns "go write three characters" for the
    spoken words "go right three characters". The staged fragment
    wh-voice-access-parity.1.13 covers the bare utterance with two pattern
    blocks, but a chained utterance never reaches those blocks: the chain
    goes through the cursor-navigate entry into this parser, which knew
    only "right" and returned None, and the whole sentence was typed as
    text. David ruled on 2026-08-25: extend the parser.

    "write" is accepted wherever "right" is accepted, including after
    "grab" and on its own. That is wider than the two staged blocks, which
    both require a unit word. The alternative was a parser where "write"
    works in some shapes and not others, which is harder to reason about
    and would still have to be written down somewhere. The one utterance
    this adds beyond the fragment is the bare "go write", which now moves
    the caret one character right. It joins "go right", which has always
    done that.
    """

    def test_go_write_three_characters_moves_right(self):
        cmds = NavigationParser.parse("go write three characters")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=3, unit="character"
        )]

    def test_go_write_two_words_moves_right(self):
        cmds = NavigationParser.parse("go write two words")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=2, unit="word"
        )]

    def test_the_chained_form_parses_both_segments(self):
        """The defect itself: this returned None and became dictation."""
        cmds = NavigationParser.parse("go write three characters then grab to end")
        assert cmds == [
            NavigationCommand(
                verb="go", kind="relative", direction="right", count=3,
                unit="character",
            ),
            NavigationCommand(verb="grab", kind="landmark", landmark="end"),
        ]

    def test_grab_write_selects_to_the_right(self):
        cmds = NavigationParser.parse("grab write two words")
        assert cmds == [NavigationCommand(
            verb="grab", kind="relative", direction="right", count=2, unit="word"
        )]

    def test_a_digit_count_still_works(self):
        cmds = NavigationParser.parse("go write 4 characters")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=4, unit="character"
        )]

    @pytest.mark.parametrize("utterance", [
        "go write a book",
        "go write three emails",
        "go write the report then send it",
    ])
    def test_ordinary_speech_is_still_unparseable(self, utterance):
        """The trailing-token check is what keeps dictation out."""
        assert NavigationParser.parse(utterance) is None

    def test_the_word_it_stands_for_is_untouched(self):
        cmds = NavigationParser.parse("go right three characters")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=3, unit="character"
        )]

    def test_no_other_homophone_was_invented(self):
        """The staged fragment defines "write" and nothing else for nav."""
        for utterance in ("go wright three characters", "go rite three characters",
                          "go lft two words"):
            assert NavigationParser.parse(utterance) is None


class TestSpokenCountsAboveTen:
    """Cursor counts read through the one number-word parser.

    _parse_count carried its own one..ten table until
    wh-number-words-one-parser, so "go right fifteen characters" was
    unparseable and the whole sentence was dictated. The parser reads
    1..999, and MAX_COUNT still clamps afterwards.
    """

    def test_a_spoken_count_above_ten_is_read(self):
        cmds = NavigationParser.parse("go right fifteen characters")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=15,
            unit="character",
        )]

    def test_a_count_above_the_maximum_still_clamps(self):
        """90 clamps to MAX_COUNT (30) rather than being refused."""
        cmds = NavigationParser.parse("go right ninety characters")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=30,
            unit="character",
        )]

    def test_max_count_is_the_hotkey_repeat_cap(self):
        """Every cursor count is sent as hotkey_action repeats, which pause
        100 ms apart, so navigation takes the same cap as
        ActionFunctions.hotkey: 29 pauses stay under the 5.0 s wait
        (Boss e8 ruling 12:38, wh-voice-access-parity.1.15.6.1). A test
        ties the two, because parser.py importing speech.actions risks an
        import cycle."""
        from speech.actions import HOTKEY_REPEAT_CAP
        from speech.navigation.parser import MAX_COUNT
        assert MAX_COUNT == HOTKEY_REPEAT_CAP

    def test_the_help_document_states_max_count(self):
        """The Navigation narrative of the help document states the clamp;
        it said 50 after MAX_COUNT became 30
        (wh-voice-access-parity.1.15.6.2)."""
        from pathlib import Path
        from speech.navigation.parser import MAX_COUNT
        section = (Path(__file__).resolve().parents[1] / "knowledge"
                   / "helpdoc" / "sections" / "070-voice-commands.md")
        if not section.is_file():
            pytest.skip("development-only help-document sources are absent from this checkout")
        text = section.read_text(encoding="utf-8")
        assert f"Counts above {MAX_COUNT} move {MAX_COUNT}." in text

    @pytest.mark.parametrize("spoken, expected", [
        ("thirty", 30), ("31", 30), ("fifty", 30),
    ], ids=["thirty", "31", "fifty"])
    def test_a_large_count_sends_at_most_30_repeats(self, spoken, expected):
        """"go right fifty words" sent 50 repeats, and 49 pauses took 4.9 s
        of the 5.0 s wait (wh-voice-access-parity.1.15.6.1)."""
        from speech.navigation.executor import NavigationExecutor
        cmds = NavigationParser.parse(f"go right {spoken} words")
        assert cmds is not None
        actions = NavigationExecutor.to_actions(cmds)
        assert actions == [{"action": "hotkey_action",
                            "params": {"keys": ["ctrl", "right"],
                                       "repeat": expected}}]

    def test_the_speech_homophones_are_still_counts(self):
        """The old table carried to/too/for; the aliases option replaces it."""
        for spoken, expected in (("to", 2), ("too", 2), ("for", 4)):
            cmds = NavigationParser.parse(f"go right {spoken} characters")
            assert cmds == [NavigationCommand(
                verb="go", kind="relative", direction="right", count=expected,
                unit="character",
            )], spoken

    def test_zero_is_not_a_cursor_count(self):
        """The old table refused n <= 0; the parser is asked without zero=."""
        assert NavigationParser.parse("go right zero characters") is None

    def test_a_digit_count_above_nine_hundred_ninety_nine_is_dictation(self):
        """The one behaviour this fix changed here, recorded deliberately.

        int() used to clamp a digit run of ANY length to MAX_COUNT, so
        "1000" and "1000000" were both counts worth 50. The parser
        refuses a VALUE above 999, so both now fall through to dictation.
        The change is the user's to accept or override.
        """
        assert NavigationParser.parse("go right 1000 characters") is None
        assert NavigationParser.parse("go right 1000000 characters") is None

    def test_leading_zeroes_do_not_make_a_count_too_long(self):
        """The cutoff is the value, not the count of characters typed.

        parse_number_word strips leading zeroes before its three-digit
        bound, so a padded count of four or more characters still reads.
        This is unchanged from the int() path it replaced, and the test
        exists because the docstrings above once claimed the boundary was
        raw length (wh-number-words-one-parser.1.4).
        """
        padded = NavigationParser.parse("go right 0001 characters")
        assert padded is not None
        assert padded[0].count == 1

        clamped = NavigationParser.parse("go right 00051 characters")
        assert clamped is not None
        assert clamped[0].count == 30, "MAX_COUNT still clamps the value"

        assert NavigationParser.parse("go right 01000 characters") is None, (
            "padding does not rescue a value above 999"
        )
        assert NavigationParser.parse("go right 0000 characters") is None, (
            "zero is not a cursor count"
        )


class TestMultiWordCounts:
    """A count of two or more words reads as one number
    (wh-number-words-command-failures part B).

    The user cannot choose whether the speech engine writes "23" or
    "twenty three", so both forms must give the same move. _try_relative
    read one count token, so "go right twenty three" and the chained
    forms were dictated.
    """

    @pytest.mark.parametrize("words, digits", [
        ("go right twenty three", "go right 23"),
        ("go left twenty one", "go left 21"),
        ("go right twenty three characters", "go right 23 characters"),
        ("grab left twenty three", "grab left 23"),
        ("grab right twenty two words", "grab right 22 words"),
    ])
    def test_words_and_digits_give_the_same_move(self, words, digits):
        expected = NavigationParser.parse(digits)
        assert expected is not None, digits
        assert NavigationParser.parse(words) == expected

    def test_a_chain_with_multi_word_counts(self):
        cmds = NavigationParser.parse(
            "go right twenty three then grab left twenty one words")
        assert cmds == [
            NavigationCommand(verb="go", kind="relative", direction="right",
                              count=23, unit="character"),
            NavigationCommand(verb="grab", kind="relative", direction="left",
                              count=21, unit="word"),
        ]

    def test_a_multi_word_count_still_clamps(self):
        cmds = NavigationParser.parse("go right forty five words")
        assert cmds is not None
        assert cmds[0].count == 30

    @pytest.mark.parametrize("utterance", [
        "go right 2 3",
        "go right twenty three emails",
        "go right one two three four",
        "go right three words more",
        "go right twenty words three",
    ])
    def test_words_that_are_not_one_count_are_still_unparseable(self, utterance):
        """Words after the unit, or a phrase that is not one number, stay
        dictation."""
        assert NavigationParser.parse(utterance) is None


class TestUpAndDownInChains:
    """A "then" chain accepts "go up" and "go down" parts
    (wh-voice-chain-up-down).

    The parser knew only right and left, so one up or down part made it
    refuse the whole chain, and the chain was typed as text. The single
    forms in patterns.toml (nav-up-times, nav-up-lines, nav-up-paragraphs
    and their down twins) set what each part must press: no unit word or
    "times" after a count is the arrow key, "lines" is the arrow key, and
    "paragraphs" is Ctrl with the arrow key. No single form takes "words"
    or "characters" with up or down, and "grab up" and "grab down" are not
    built (Boss ruling 20:10, Q2 = 1), so those parts stay refused.
    """

    @pytest.mark.parametrize("utterance, direction, count, unit", [
        ("go up", "up", 1, "line"),
        ("go down", "down", 1, "line"),
        ("go up three", "up", 3, "line"),
        ("go up 3", "up", 3, "line"),
        ("go down three times", "down", 3, "line"),
        ("go up one time", "up", 1, "line"),
        ("go down four lines", "down", 4, "line"),
        ("go up one line", "up", 1, "line"),
        ("go up two paragraphs", "up", 2, "paragraph"),
        ("go down one paragraph", "down", 1, "paragraph"),
        ("go up twenty three", "up", 23, "line"),
        ("go up forty", "up", 30, "line"),
    ])
    def test_an_up_or_down_part_is_read(self, utterance, direction, count, unit):
        assert NavigationParser.parse(utterance) == [NavigationCommand(
            verb="go", kind="relative", direction=direction, count=count,
            unit=unit)]

    def test_the_parts_keep_the_spoken_order(self):
        cmds = NavigationParser.parse(
            "go down two then go left one word then go up three times")
        assert cmds == [
            NavigationCommand(verb="go", kind="relative", direction="down",
                              count=2, unit="line"),
            NavigationCommand(verb="go", kind="relative", direction="left",
                              count=1, unit="word"),
            NavigationCommand(verb="go", kind="relative", direction="up",
                              count=3, unit="line"),
        ]

    @pytest.mark.parametrize("utterance", [
        "go up two words",
        "go down three characters",
        "go up times",
        "go up three times lines",
        "go up 0",
        "go up zero",
        "go up 1000",
        "grab up three",
        "grab down two lines",
        "go up two words then go left one",
        "go up the stairs then go left",
    ])
    def test_an_up_or_down_part_without_a_single_form_is_refused(self, utterance):
        assert NavigationParser.parse(utterance) is None

    @pytest.mark.parametrize("utterance", [
        "go left three times",
        "go right two lines",
        "go right one line",
        "grab left two times",
    ])
    def test_right_and_left_still_refuse_lines_and_times(self, utterance):
        """"lines" and "times" are up and down words only. A right or
        left utterance with them stays typed text, as at origin/dev."""
        assert NavigationParser.parse(utterance) is None

    @pytest.mark.parametrize("utterance, keys, repeat", [
        ("go up three", ["up"], 3),
        ("go down four lines", ["down"], 4),
        ("go up two paragraphs", ["ctrl", "up"], 2),
        ("go down one paragraph", ["ctrl", "down"], 1),
        ("go up forty", ["up"], 30),
    ])
    def test_the_keys_are_the_keys_of_the_single_form(self, utterance, keys, repeat):
        from speech.navigation.executor import NavigationExecutor

        assert NavigationExecutor.to_actions(NavigationParser.parse(utterance)) == [
            {"action": "hotkey_action", "params": {"keys": keys, "repeat": repeat}}
        ]
