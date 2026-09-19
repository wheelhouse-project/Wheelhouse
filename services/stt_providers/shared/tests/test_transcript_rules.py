"""Tests for shared_stt.transcript_rules.

The normalize behavior is the canonical extraction of the parakeet
engine's `_normalize_text`; the representative cases below are ported
verbatim from the parakeet suite (tests/test_sherpa_engine.py) so the
extraction cannot drift from the proven behavior.
"""
from __future__ import annotations

import pytest

from shared_stt.transcript_rules import (
    _XRAY_JOIN,
    _parse_cardinal,
    _try_digit_sequence,
    agreement_prefix_len,
    apply_itn,
    normalize_transcript,
)


class TestTimeRules:
    def test_period_form_becomes_colon(self):
        assert normalize_transcript("call at 9.45 am") == "call at 9:45 AM"

    def test_idempotent_on_uppercase_colon_output(self):
        assert (
            normalize_transcript("remind me at 6:30 PM")
            == "remind me at 6:30 PM"
        )

    def test_uppercases_lowercase_ampm_beside_colon_time(self):
        assert (
            normalize_transcript("remind me at 6:30 pm")
            == "remind me at 6:30 PM"
        )

    def test_dotted_digits_before_amps_untouched(self):
        assert (
            normalize_transcript("the meter reads 0.75 amps")
            == "the meter reads 0.75 amps"
        )

    def test_invalid_minutes_dotted_untouched(self):
        assert (
            normalize_transcript("the log shows 0.75 am today")
            == "the log shows 0.75 am today"
        )
        assert (
            normalize_transcript("value 13.99 pm recorded")
            == "value 13.99 pm recorded"
        )

    def test_long_dotted_number_before_am_untouched(self):
        assert (
            normalize_transcript("part 123.45 am reading")
            == "part 123.45 am reading"
        )

    def test_dotted_ampm_period_time_form(self):
        assert normalize_transcript("It is 8.17 p.m.") == "it is 8:17 PM"
        # Moved from the parakeet suite (wh-shared-itn-parakeet). Same
        # rule, a different sentence prefix -- the parakeet row carried
        # both wordings and only the first had a home here.
        assert (
            normalize_transcript("Call me at 9.45 p.m.")
            == "call me at 9:45 PM"
        )

    def test_dotted_am_period_time_form(self):
        # Moved from the parakeet suite (wh-shared-itn-parakeet). The
        # dotted "a.m." form is the AM half of the rule above; only the
        # "p.m." half had a row here.
        assert normalize_transcript("It is 9.45 a.m.") == "it is 9:45 AM"

    def test_dotted_form_mixed_case_period_word(self):
        # Moved from the parakeet suite. _TIME_PERIOD is IGNORECASE, and
        # no row here exercised a period word that is neither all-lower
        # nor all-upper.
        assert (
            normalize_transcript("see you at 6.30 Pm")
            == "see you at 6:30 PM"
        )

    def test_preserves_two_digit_hours(self):
        # Moved from the parakeet suite. The hour group is \d{1,2} and
        # every other row here uses a one-digit hour.
        assert normalize_transcript("lunch at 10:45 AM") == "lunch at 10:45 AM"
        assert (
            normalize_transcript("noon meeting 12:00 PM")
            == "noon meeting 12:00 PM"
        )

    def test_plain_decimal_without_ampm_left_alone(self):
        # Moved from the parakeet suite. A decimal with no AM/PM anchor
        # must keep its period and must not read as a time.
        assert normalize_transcript("pi is about 3.14") == "pi is about 3.14"


class TestPhoneHyphenation:
    def test_hyphenates_ten_digit_phone_number(self):
        assert (
            normalize_transcript("call 7035551234 now")
            == "call 703-555-1234 now"
        )
        # Both moved from the parakeet suite (wh-shared-itn-parakeet):
        # a 10-digit run at the end of the sentence rather than mid-
        # sentence, and a second area code, which is the digit content
        # the replacement groups reorder.
        assert (
            normalize_transcript("call me at 7035551234")
            == "call me at 703-555-1234"
        )
        assert (
            normalize_transcript("my number is 2025559876")
            == "my number is 202-555-9876"
        )

    def test_leaves_short_digit_runs_alone(self):
        assert normalize_transcript("delete 12345 now") == "delete 12345 now"
        # Moved from the parakeet suite. A 7-digit run is the local
        # phone-number shape a widened rule would most plausibly start
        # matching; the 5-digit row above does not reach that far.
        assert (
            normalize_transcript("the code is 5551234")
            == "the code is 5551234"
        )

    def test_preserves_already_hyphenated_form(self):
        # Moved from the parakeet suite. The rule is idempotent on its
        # own output; no row here fed it an already-hyphenated number.
        assert (
            normalize_transcript("call 703-555-1234 now")
            == "call 703-555-1234 now"
        )

    def test_leaves_long_digit_runs_alone(self):
        # Moved from the parakeet suite. The short-run row above guards
        # the lower side; this guards the upper side -- without the
        # trailing \b, an 11-digit run would hyphenate its first ten.
        assert (
            normalize_transcript("the id is 12345678901")
            == "the id is 12345678901"
        )


class TestColonHandling:
    """Moved from the parakeet suite (wh-shared-itn-parakeet).

    The punctuation pass keeps colons only between digits. No row in
    this file exercised either side of that condition.
    """

    def test_non_digit_colons_still_stripped(self):
        assert (
            normalize_transcript("subject: dinner plans")
            == "subject dinner plans"
        )

    def test_digit_flanked_colons_preserved_in_non_time_contexts(self):
        # Ratios, scores, port suffixes -- any colon between digits --
        # survive, even when no time rule produced them.
        assert (
            normalize_transcript("the score was 3:2 tonight")
            == "the score was 3:2 tonight"
        )


class TestXrayJoin:
    def test_split_comma_form_rejoined(self):
        # wh-first-char-lowercase changed this row from "x-ray Boost".
        # "Boost" is capitalized, which the condition reads as evidence
        # that the leading X was not capitalized merely because it opens
        # the utterance, so the X survives. The hotword is unaffected:
        # speech.router._word_matches_hotword lowercases both sides, and
        # "X-ray" matches the hotword "x-ray" (verified directly).
        assert normalize_transcript("X, Ray Boost") == "X-ray Boost"

    def test_split_form_with_trailing_period(self):
        # Moved from the parakeet suite (wh-shared-itn-parakeet). This
        # is the only row that sends the comma form AND a sentence-final
        # period through the join. The punctuation pass strips both
        # before the join runs, and that ordering was previously covered
        # only by inference.
        assert normalize_transcript("X, Ray patterns.") == "x-ray patterns"

    def test_plain_split_form_rejoined(self):
        # Moved from the parakeet suite. The split form with no comma
        # reaches the join without the punctuation pass having to remove
        # anything, which is the other half of the ordering above.
        assert (
            normalize_transcript("X Ray close window")
            == "x-ray close window"
        )

    def test_mid_sentence_lowercase_split_rejoined(self):
        assert (
            normalize_transcript("Take an x ray tomorrow")
            == "take an x-ray tomorrow"
        )

    def test_regex_does_not_match_already_hyphenated_form(self):
        assert _XRAY_JOIN.search("X-ray") is None
        # Moved from the parakeet suite (wh-shared-itn-parakeet): the
        # lowercase mid-sentence form is the one a second pass over the
        # rule's own output would hit.
        assert _XRAY_JOIN.search("see the x-ray boost") is None

    def test_hyphenated_form_unchanged_end_to_end(self):
        # Moved from the parakeet suite. The regex row above proves the
        # join does not fire; this proves no OTHER rule in the pipeline
        # mangles an already-hyphenated "x-ray".
        assert normalize_transcript("X-ray boost") == "x-ray boost"

    def test_mid_sentence_preserves_leading_capital(self):
        # Moved from the parakeet suite. The join keeps the X's case,
        # which is only visible away from position 0 -- the row above
        # has its X first, where the lowercase-first-character rule
        # hides the difference.
        assert (
            normalize_transcript("see the X Ray result")
            == "see the X-ray result"
        )

    def test_word_ending_in_x_not_joined(self):
        assert normalize_transcript("Max ray gun") == "max ray gun"

    def test_ray_prefix_word_not_joined(self):
        # Moved from the parakeet suite. "Max ray" guards the left \b;
        # "raymond" guards the right one.
        assert normalize_transcript("x raymond called") == "x raymond called"


class TestCaseAndPronoun:
    def test_lowercases_first_character(self):
        assert normalize_transcript("Delete two") == "delete two"

    def test_capitalizes_pronoun_i(self):
        assert normalize_transcript("today i went home") == "today I went home"

    def test_spelled_out_letters_collapsed(self):
        assert normalize_transcript("say V-O-X now") == "say vox now"


class TestCapitalEvidence:
    """wh-first-char-lowercase. The first character stays as the model
    wrote it when the utterance carries a capital that is not merely
    positional.

    Parakeet capitalizes position 0 on every utterance it reads as a
    sentence, so that character alone proves nothing: measured over the
    recorded evaluation runs, 2,041 of 2,041 uppercase openings had a
    lowercase ground truth ("delete" came back "Delete."). A capital
    elsewhere in the utterance is the evidence that survives, and it is
    what these rows pin.
    """

    def test_a_capitalized_second_word_keeps_the_leading_capital(self):
        assert normalize_transcript("Bill Smith") == "Bill Smith"

    def test_an_internal_capital_keeps_the_leading_capital(self):
        assert normalize_transcript("McDonald called") == "McDonald called"

    def test_an_all_capitals_first_word_survives(self):
        assert normalize_transcript("NASA launched it") == "NASA launched it"

    def test_a_lowercase_second_word_still_lowercases_the_first(self):
        assert normalize_transcript("Delete two") == "delete two"

    def test_a_single_word_utterance_is_still_lowercased(self):
        assert normalize_transcript("Delete") == "delete"

    def test_a_lowercase_opening_is_left_alone(self):
        # Real parakeet output for a continuation fragment: the model
        # withholds the capital itself, so there is nothing to decide.
        assert (
            normalize_transcript("and paste the replacement")
            == "and paste the replacement"
        )

    def test_a_capital_further_along_is_not_evidence(self):
        # Only the SECOND word counts. A capital further into the
        # utterance belongs to its own word and says nothing about
        # whether position 0 was capitalized for a reason.
        assert normalize_transcript("Call me Friday") == "call me Friday"

    # ---- wh-first-char-lowercase.1.1: the pronoun is not evidence ----

    def test_a_capitalized_pronoun_is_not_evidence(self):
        # English capitalizes "I" by grammar, not because it names
        # anything, and the recognizers capitalize it on nearly every
        # utterance -- 32 of 36 standalone occurrences in the recorded
        # parakeet runs. Counting it kept a positional capital on
        # ordinary dictation.
        assert normalize_transcript("So I'm going") == "so I'm going"

    def test_the_bare_pronoun_is_not_evidence(self):
        assert normalize_transcript("So I am going") == "so I am going"

    def test_every_pronoun_contraction_is_excluded(self):
        assert (
            normalize_transcript("Well I've been there")
            == "well I've been there"
        )
        assert normalize_transcript("Okay I'd like that") == "okay I'd like that"
        assert normalize_transcript("Yes I'll go") == "yes I'll go"

    def test_a_curly_apostrophe_pronoun_is_not_evidence(self):
        # A recognizer may emit the typographic apostrophe, and the
        # punctuation pass keeps both forms.
        assert (
            normalize_transcript("So I\u2019m going") == "so I\u2019m going"
        )

    def test_a_word_that_merely_starts_with_i_is_still_evidence(self):
        # Only the pronoun itself is excluded, not every word that
        # begins with the letter. "Illinois" is a real proper noun.
        assert normalize_transcript("Visit Illinois") == "Visit Illinois"

    def test_a_capitalized_day_name_is_still_evidence(self):
        # The ruling on wh-first-char-lowercase.1.1 took the pronoun
        # exclusion alone. A day or month name in second position is a
        # real proper noun, so its capital still counts as evidence.
        assert (
            normalize_transcript("On Monday I am busy")
            == "On Monday I am busy"
        )

    def test_a_spelled_out_acronym_cannot_be_seen(self):
        # wh-first-char-lowercase.1.2, pinned as a known limit rather
        # than fixed. The spelled-out-letter rule runs before the
        # condition and lowercases its whole match, so the interior
        # capitals are gone by the time the condition looks. The word
        # form "NASA launched it" survives; the spelled form cannot.
        # This is not a regression -- the rule typed "nasa" before this
        # change too. See the crewcut: comment on has_capital_evidence.
        assert normalize_transcript("N-A-S-A launched it") == "nasa launched it"


# --------------------------------------------------------------------
# Inverse text normalization (wh-shared-itn-rules-module, phase 1).
#
# Every table is (spoken form, expected written form). The spoken forms
# are what a word-emitting recognizer hands us; the expected forms are
# what WheelHouse should type.
# --------------------------------------------------------------------

CARDINAL_CASES = [
    ("fifty nine", "59"),
    ("eight thousand", "8000"),
    ("twenty one", "21"),
    ("three hundred", "300"),
    ("one hundred and five", "105"),
    # Both grouped by wh-itn-thousands-commas: a value of five digits or
    # more takes thousands separators. See THOUSANDS_SEPARATOR_CASES.
    ("twenty one thousand", "21,000"),
    ("two million", "2,000,000"),
    ("fifteen hundred", "1500"),
    ("send fifty nine of them", "send 59 of them"),
    # A command word in front of a two-word number still converts: the
    # refusal absorption of wh-shared-itn-review.14/.15 never starts
    # here, because nothing refused.
    ("delete twenty one", "delete 21"),
]

# The command-safety rule: a phrase converts only when it carries two or
# more number words that read as ONE number, or a unit anchor. Anything
# else stays words, because Wheelhouse matches spoken commands against
# word forms ("backspace one" must never become "backspace 1").
COMMAND_SAFETY_CASES = [
    ("backspace one", "backspace one"),
    ("seven", "seven"),
    ("press three", "press three"),
    ("delete two", "delete two"),
    ("one more time", "one more time"),
    # Two number words that do NOT read as one number stay words: they
    # are the ambiguous forms (clock time, house number) that later
    # phases resolve with an anchor.
    ("four ten", "four ten"),
    ("twelve thirty four", "twelve thirty four"),
    ("twenty thirty", "twenty thirty"),
    ("one two", "one two"),
    # "oh" is a digit only after another digit, never at the start of a
    # phrase -- it is far more often the interjection.
    ("oh five", "oh five"),
    ("oh two three", "oh two three"),
]

MONEY_CASES = [
    # David's live failure, 2026-08-25.
    ("fifty nine dollars and seventeen cents", "$59.17"),
    ("five dollars", "$5"),
    ("one dollar", "$1"),
    ("five dollars and five cents", "$5.05"),
    ("five dollars fifty cents", "$5.50"),
    ("two hundred dollars", "$200"),
    ("twenty cents", "20 cents"),
    ("twenty dollars", "$20"),
    ("twenty five cents", "25 cents"),
    ("five dollars and twenty five cents", "$5.25"),
    (
        "that will be fifty nine dollars and seventeen cents today",
        "that will be $59.17 today",
    ),
    # Implied cents ("one dollar fifty") is not a grammar this module
    # reads, so the whole phrase stays words rather than half-convert
    # to "$1 fifty" (wh-shared-itn-review.3).
    ("one dollar fifty", "one dollar fifty"),
    ("five dollars and fifty", "five dollars and fifty"),
    ("five dollars and fifty people", "five dollars and fifty people"),
    # The tail may be any length: a two-word tail used to convert on its
    # own ("five dollars 59"), and a second pass then wrote "$5 59"
    # (wh-shared-itn-review.6).
    ("five dollars fifty nine", "five dollars fifty nine"),
    ("five dollars and fifty nine", "five dollars and fifty nine"),
    (
        "fifty nine dollars and fifty nine",
        "fifty nine dollars and fifty nine",
    ),
    ("five dollars one hundred five", "five dollars one hundred five"),
    # More than 99 cents is not a cents part; the dollars still convert
    # and the oversized cents phrase converts on its own.
    ("five dollars and one hundred cents", "$5 and 100 cents"),
    # A refused implied-cents tail that ends against a "point" carries
    # the refusal through the decimal: the tail used to convert alone,
    # writing "five dollars twenty one point 25" and, with a second
    # dollar anchor, "... point $25" (wh-shared-itn-review.10).
    (
        "five dollars twenty one point twenty five",
        "five dollars twenty one point twenty five",
    ),
    (
        "five dollars and twenty one point twenty five",
        "five dollars and twenty one point twenty five",
    ),
    (
        "five dollars twenty one point twenty five dollars",
        "five dollars twenty one point twenty five dollars",
    ),
    # Every further "point" against the tail joins the same refusal.
    (
        "five dollars twenty one point twenty five point thirty six",
        "five dollars twenty one point twenty five point thirty six",
    ),
    # A fractional tail that STARTS with "oh" is part of the same
    # refusal: the refusal used to stop in front of the "oh", and the
    # words behind it converted as "... point oh $5"
    # (wh-shared-itn-review.12).
    (
        "five dollars twenty one point oh five dollars",
        "five dollars twenty one point oh five dollars",
    ),
    (
        "five dollars twenty one point oh five",
        "five dollars twenty one point oh five",
    ),
    # An amount whose run reads as several numbers is refused at its own
    # anchor, and the refusal absorbs the adjacent cents or dollars the
    # speaker never separated (the generalized absorption of
    # wh-shared-itn-review.14/.15). These read "... and 20 cents" and
    # "... and $20" while each anchored rule reached only its own shape.
    (
        "twelve thirty four dollars and twenty cents",
        "twelve thirty four dollars and twenty cents",
    ),
    (
        "four ten cents and twenty dollars",
        "four ten cents and twenty dollars",
    ),
    # Punctuation still detaches: the comma ends the money phrase, and
    # the decimal phrase after it is judged on its own.
    (
        "five dollars, twenty one point twenty five",
        "$5, twenty one point twenty five",
    ),
    # An ambiguous run no rule reads absorbs the dollar amount standing
    # against it, the same way a refused anchor does. This typed
    # "twenty thirty oh $5" (wh-shared-itn-review.16).
    (
        "twenty thirty oh five dollars",
        "twenty thirty oh five dollars",
    ),
]

CLOCK_TIME_CASES = [
    # David's live failure, 2026-08-25.
    ("eleven fifteen PM", "11:15 PM"),
    ("eleven fifteen pm", "11:15 PM"),
    ("four ten AM", "4:10 AM"),
    ("nine oh five AM", "9:05 AM"),
    # A teen hour takes "oh" minutes the same way (wh-shared-itn-review.1).
    ("twelve oh five AM", "12:05 AM"),
    ("ten oh five PM", "10:05 PM"),
    ("eleven oh two AM", "11:02 AM"),
    ("twelve forty five PM", "12:45 PM"),
    ("eleven PM", "11 PM"),
    ("meet me at eleven fifteen p.m.", "meet me at 11:15 PM."),
    # An hour outside 1-12 is not a clock time.
    ("thirteen fifteen PM", "thirteen fifteen PM"),
    # The AM/PM anchor belongs to the clock rule: when that rule
    # refuses the run, no unanchored rule may convert it instead
    # (wh-shared-itn-review.2). "five five five pm" is a time this
    # module cannot read, not the number 555.
    ("five five five pm", "five five five pm"),
    ("one two three pm", "one two three pm"),
    ("five five pm", "five five pm"),
    # Accepted consequence of the generalized absorption (David's
    # ruling, 2026-08-25): a refused phrase suppresses an adjacent
    # unpunctuated amount that MIGHT have been separate speech. This
    # typed "five five five pm and $20" before, and unpunctuated
    # adjacency to a refused phrase is exactly where separateness
    # cannot be trusted.
    (
        "five five five pm and twenty dollars",
        "five five five pm and twenty dollars",
    ),
    # A leading "oh" is skipped by design, and "five pm" that follows
    # is a valid hour, so this one does convert.
    ("oh five pm", "oh 5 PM"),
    # A run no rule reads is refused like any other, so it absorbs the
    # words behind it. _collect_run admits "oh" only after a digit or a
    # teen, so a tens word ends the run in front of it: the ambiguous
    # "twelve thirty" was skipped, the un-admitted "oh" was skipped, and
    # "five pm" converted on its own as "twelve thirty oh 5 PM"
    # (wh-shared-itn-review.16). One unpunctuated spoken time this
    # module cannot read stays words entirely.
    ("twelve thirty oh five pm", "twelve thirty oh five pm"),
    ("one thirty oh five pm", "one thirty oh five pm"),
    ("eleven forty oh two am", "eleven forty oh two am"),
    # Punctuation ends the absorbed span, so the standalone phrase
    # behind the comma is judged on its own and still converts.
    ("twelve thirty, oh five pm", "twelve thirty, oh 5 PM"),
]

DECIMAL_CASES = [
    ("three point five", "3.5"),
    ("three point one four", "3.14"),
    ("three point five six", "3.56"),
    ("twenty one point five", "21.5"),
    ("nine point nine nine", "9.99"),
    # "oh" is a digit anywhere in the fraction, leading included.
    ("three point oh five", "3.05"),
    # "point" without digits after it is the ordinary word.
    ("the three point plan", "the three point plan"),
    ("point five", "point five"),
    # A fractional part this module cannot read makes the WHOLE phrase
    # stay words: "21 point 25" is neither the spoken phrase nor the
    # number the speaker meant (wh-shared-itn-review.8).
    ("twenty one point twenty five", "twenty one point twenty five"),
    ("zero point twenty five", "zero point twenty five"),
    ("three point twenty five", "three point twenty five"),
    # The refusal reaches no further than the fractional words: an
    # ordinary "point" after a cardinal still converts the cardinal.
    ("twenty one point plan", "21 point plan"),
    # A number word continuing the fraction makes the WHOLE attempted
    # decimal stay words. Reading only the digit prefix accepted "five"
    # as the fraction, stopped in front of "hundred", and left it to
    # convert alone: "3.5 $100" (wh-shared-itn-review.12).
    (
        "three point five hundred dollars",
        "three point five hundred dollars",
    ),
    (
        "three point oh twenty five dollars",
        "three point oh twenty five dollars",
    ),
    ("three point five hundred", "three point five hundred"),
    # An unreadable fractional tail followed by a conjunction: the
    # refusal used to end in front of the "and", and the amount behind
    # it converted on its own as "... and $20" / "... and 25 cents"
    # (wh-shared-itn-review.15). The whole attempted decimal is one
    # amount, so all of it stays words.
    (
        "three point five hundred and twenty dollars",
        "three point five hundred and twenty dollars",
    ),
    (
        "three point five hundred and twenty five cents",
        "three point five hundred and twenty five cents",
    ),
    # The same boundary behind a leading fractional "oh".
    (
        "three point oh five hundred and twenty dollars",
        "three point oh five hundred and twenty dollars",
    ),
    # Punctuation still detaches the continuation, so the decimal in
    # front of the comma is a complete phrase.
    ("three point five, hundred dollars", "3.5, $100"),
    ("three point five, twenty dollars", "3.5, $20"),
    # An ambiguous run no rule reads absorbs the decimal standing
    # against it too. This typed "twenty thirty oh 5.2"
    # (wh-shared-itn-review.16).
    (
        "twenty thirty oh five point two",
        "twenty thirty oh five point two",
    ),
]

# Conjunction connectors (wh-shared-itn-review.13). _collect_run admits
# "and" only after a scale and in front of a number word, so the article
# of ordinary speech ends the run. One uninterrupted spoken amount must
# not become two written quantities on that boundary: "1000 and a $100"
# was one amount the speaker said once.
CONJUNCTION_CONNECTOR_CASES = [
    ("one thousand and a hundred dollars", "one thousand and a hundred dollars"),
    ("two thousand and a hundred dollars", "two thousand and a hundred dollars"),
    (
        "one hundred and twenty and five dollars",
        "one hundred and twenty and five dollars",
    ),
    # Every repeat of the connector shape joins the same refusal; one
    # hop alone left "... and a $1000" behind it.
    (
        "one thousand and a hundred and a thousand dollars",
        "one thousand and a hundred and a thousand dollars",
    ),
    # The refused connector also absorbs the decimal or cents
    # continuation standing against it: these typed "... point $5" and
    # "... and 25 cents", neither of which the speaker said as a
    # separate amount (wh-shared-itn-review.14).
    (
        "one thousand and a hundred point five dollars",
        "one thousand and a hundred point five dollars",
    ),
    (
        "one thousand and a hundred dollars and twenty five cents",
        "one thousand and a hundred dollars and twenty five cents",
    ),
    # Punctuation ends the absorbed span the same way it ends a run, so
    # the cents phrase behind the comma is judged on its own.
    (
        "one thousand and a hundred dollars, twenty five cents",
        "one thousand and a hundred dollars, 25 cents",
    ),
    # The refusal is the connector SHAPE, not the word "and": an "and"
    # that carries no number after it leaves the head conversion alone.
    ("fifty nine and then some", "59 and then some"),
    ("twenty one and a half", "21 and a half"),
    # Accepted widening of the refusal (boss ruling, wh-shared-itn-review.13):
    # this read "21 and five" before, which is the same fragment shape.
    ("twenty one and five", "twenty one and five"),
    # The "and" the grammar DOES read still converts: the money rule and
    # _collect_run reach their own conjunctions before this refusal.
    ("one thousand and one dollars", "$1001"),
    ("five dollars and seventeen cents", "$5.17"),
    ("one hundred and five", "105"),
]

# Scale grammar (wh-shared-itn-review.7). An ascending or repeated scale
# is not English ("one thousand million"), so the phrase stays words
# rather than becoming a number nobody said.
SCALE_SEQUENCE_CASES = [
    ("one thousand million", "one thousand million"),
    ("one million million", "one million million"),
    ("one hundred hundred", "one hundred hundred"),
    ("one thousand million dollars", "one thousand million dollars"),
    # Valid compounds still convert. The four grouped forms are
    # wh-itn-thousands-commas; 2500 stays bare below the threshold.
    ("one billion", "1,000,000,000"),
    ("one million two hundred thousand", "1,200,000"),
    ("one hundred thousand two hundred", "100,200"),
    ("two thousand five hundred", "2500"),
    ("five hundred million", "500,000,000"),
    ("nine hundred ninety nine", "999"),
    # A smaller scale after a larger one is not English even when its
    # coefficient group is there ("one thousand two million").
    ("one thousand two million", "one thousand two million"),
    # An explicit zero is never an absent coefficient: these read as
    # $100 / $1000 / $1000000 before wh-shared-itn-review.11.
    ("zero hundred", "zero hundred"),
    ("zero hundred dollars", "zero hundred dollars"),
    ("zero thousand dollars", "zero thousand dollars"),
    ("zero million dollars", "zero million dollars"),
    # A scale whose coefficient group is empty after an earlier scale
    # invents that group: "$1001000" and "$1100" before the fix.
    ("one million thousand dollars", "one million thousand dollars"),
    ("one thousand hundred dollars", "one thousand hundred dollars"),
    # "and" directly before a scale is not a grammar position this
    # module reads, and the refusal covers the WHOLE phrase: stopping
    # the run at "one hundred" would type "100 and thousand dollars".
    ("one hundred and thousand dollars", "one hundred and thousand dollars"),
    # The same "and" inside a cents tail, where the conjunction refusal
    # of wh-shared-itn-review.13 cannot reach it: _cents_after collects
    # its own run, so keeping the "and" inside that run is what stops
    # "and 1000 cents" from converting against the cents anchor. The
    # dollars still convert, the same way they do for the oversized
    # cents phrase above.
    (
        "five dollars and one hundred and thousand cents",
        "$5 and one hundred and thousand cents",
    ),
    # A LEADING bare scale keeps its implicit one: "a hundred" and "a
    # thousand" are ordinary speech whose article is not a number word.
    ("hundred dollars", "$100"),
    ("thousand dollars", "$1000"),
    # A zero with no scale beside it is still the number zero.
    ("zero dollars", "$0"),
]

DIGIT_SEQUENCE_CASES = [
    ("nine oh two one oh", "90210"),
    ("one two three", "123"),
    ("zero zero seven", "007"),
    ("five five five one two three four", "5551234"),
    ("my code is four four four", "my code is 444"),
]

SURROUNDING_TEXT_CASES = [
    ("", ""),
    ("hello world", "hello world"),
    ("no numbers here", "no numbers here"),
    ("call me at eleven fifteen PM.", "call me at 11:15 PM."),
    ("it was fifty nine, then", "it was 59, then"),
    # Punctuation inside a phrase splits it; punctuation before the unit
    # anchor detaches the anchor.
    ("fifty, nine", "fifty, nine"),
    ("fifty nine, dollars", "59, dollars"),
    ("fifty nine\nnext line", "59\nnext line"),
]

# --------------------------------------------------------------------
# Phase 4 (wh-shared-itn-addresses-dates): spec rules 6 and 7, plus the
# hyphenated number word the phase-1 module crewcut assigned here.
# --------------------------------------------------------------------

# Rule 6. The paired reading is the ONLY thing this rule adds: a house
# number that reads as one cardinal already converted in phase 1, with
# no street suffix needed. The suffix is what licenses the pair, because
# "twelve thirty four" on its own is also 12:34.
HOUSE_NUMBER_CASES = [
    # The spec's two examples.
    ("twelve thirty four main street", "1234 main street"),
    ("one twenty three oak avenue", "123 oak avenue"),
    # The remaining four suffixes of the closed six-word list.
    ("twelve thirty four oak road", "1234 oak road"),
    ("one twenty three elm drive", "123 elm drive"),
    ("twelve thirty four maple lane", "1234 maple lane"),
    ("one twenty three elm court", "123 elm court"),
    # The suffix may also stand directly against the pair, which is the
    # zero-word end of the one-word window.
    ("twelve thirty four street", "1234 street"),
    # Already converting before phase 4, through _try_cardinal and with
    # no suffix in the argument at all. These three are the measured
    # proof that the new rule changed nothing that already worked.
    ("four hundred main street", "400 main street"),
    ("fifty nine main street", "59 main street"),
    ("nine hundred and one elm court", "901 elm court"),
    # No suffix in reach -> the pair stays words. This is the rule's
    # whole safety argument.
    ("twelve thirty four", "twelve thirty four"),
    ("one twenty three", "one twenty three"),
    ("twelve thirty four blocks away", "twelve thirty four blocks away"),
    # The window is ONE word (boss ruling, 2026-08-25), so a long street
    # name puts its suffix out of reach.
    (
        "twelve thirty four martin luther king drive",
        "twelve thirty four martin luther king drive",
    ),
    # "boulevard" is not one of the six the spec allows.
    (
        "twelve thirty four main boulevard",
        "twelve thirty four main boulevard",
    ),
    # Command safety: a lone number word in front of a suffix is not a
    # pair, so the suffix licenses nothing.
    ("one court", "one court"),
    ("backspace one street", "backspace one street"),
    ("delete court", "delete court"),
    # The lone number word the pair guard alone refuses: 50 passes both
    # chunk bounds, so nothing but "this run is not a pair" keeps
    # "fifty main street" from reading as 5050.
    ("fifty main street", "fifty main street"),
    # The word between the pair and the suffix has to be an ordinary
    # word. A number-ish word there belongs to a spoken number this
    # module could not read, and an "and" there is the connector shape.
    ("twelve thirty oh street", "twelve thirty oh street"),
    ("twelve thirty and street", "twelve thirty and street"),
    # A trailing chunk under ten leaves the concatenation ambiguous
    # ("twelve four" is 124 or 1204), so the pair stays words.
    ("twelve four main street", "twelve four main street"),
    # A leading chunk over 99 is a longer ambiguous run, not a pair.
    (
        "five hundred twelve thirty four main street",
        "five hundred twelve thirty four main street",
    ),
    # Punctuation detaches the suffix the way it detaches a unit anchor.
    ("twelve thirty four, main street", "twelve thirty four, main street"),
    # Surrounding text is preserved.
    (
        "meet me at twelve thirty four main street",
        "meet me at 1234 main street",
    ),
]

# Rule 7. The month name is the unit anchor an ordinal needs, the role
# "dollars" plays for money: a bare ordinal stays a word. Phase 4 emits
# LOWERCASE (boss ruling, 2026-08-25); the spec's "March 3rd" is phase
# 5's output, because rule 8 owns every capitalization.
DATE_ORDINAL_CASES = [
    # The spec's own example is "march third", but march is one of the
    # three month names the anchor table excludes (see the crewcut
    # beside _ITN_MONTHS), so the rows that exercise rule 7's mechanics
    # are anchored on january. January converts every one of these
    # ordinal forms exactly as march did: the month word moved, the
    # coverage did not.
    ("january third", "january 3rd"),
    ("january twenty third", "january 23rd"),
    ("january twenty first", "january 21st"),
    ("december thirty first", "december 31st"),
    ("june first", "june 1st"),
    ("january second", "january 2nd"),
    ("january eighth", "january 8th"),
    # The 11th/12th/13th exception to the suffix rule.
    ("january eleventh", "january 11th"),
    ("january twelfth", "january 12th"),
    ("january thirteenth", "january 13th"),
    # The two ordinals that carry their own tens word.
    ("january twentieth", "january 20th"),
    ("january thirtieth", "january 30th"),
    # All nine month names left in the anchor table still convert, so
    # the exclusion cost exactly the three names it names and no more.
    ("january first", "january 1st"),
    ("february second", "february 2nd"),
    ("april twenty second", "april 22nd"),
    ("june third", "june 3rd"),
    ("july fourth", "july 4th"),
    ("september eleventh", "september 11th"),
    ("october thirty first", "october 31st"),
    ("november tenth", "november 10th"),
    ("december twenty fifth", "december 25th"),
    # A bare ordinal with no month anchor stays a word, the same way a
    # lone number word does.
    ("third", "third"),
    ("press third", "press third"),
    ("the third of the month", "the third of the month"),
    # Kept deliberately (wh-shared-itn-addresses-dates.1): under the
    # nine-month anchor table these four rows refuse at the month check
    # before any later rule-7 code runs, so no rule-7 mutation
    # (M63/M64/M68/M74) reaches them. The january rows with the matching
    # per-row mutation comments hold those mutations open. These rows
    # still pin the anchor-refusal output, and "march twenty five" still
    # exercises the phase-1 cardinal path. Do not delete them as dead,
    # and do not cite them as rule-7 coverage.
    # A day outside 1-31 is not a date.
    ("march thirty second", "march thirty second"),
    ("march forty first", "march forty first"),
    # A tens word behind a month is not an ordinal on its own, so rule 7
    # declines and the phase-1 cardinal rule reads the pair. The written
    # form is the same day either way; what this row pins is that the
    # date rule did not swallow the second word as an ordinal.
    ("march twenty five", "march 25"),
    # Punctuation between the month and the ordinal detaches the anchor.
    ("march, third", "march, third"),
    # Holds M64 open: a day above 31 must not read as a date. The march
    # rows of this shape cannot hold it, because march is not an anchor
    # and the anchor check refuses them before the day cap is reached.
    # NOT a redundant january duplicate -- do not delete it.
    ("january thirty second", "january thirty second"),
    # Holds M64 open as well, with the other tens word. NOT a redundant
    # january duplicate -- do not delete it.
    ("january forty first", "january forty first"),
    # Holds M68 open: a tens word followed by a NON-ordinal must not read
    # as a day. The march row of this shape cannot hold it, because march
    # is not an anchor and the anchor check refuses it before the ordinal
    # is read. NOT a redundant january duplicate -- do not delete it.
    ("january twenty five", "january 25"),
    # Holds M63 open: punctuation between the month and the ordinal must
    # detach the anchor. The march row of this shape cannot hold it,
    # because march is not an anchor and the anchor check refuses it
    # whether or not the punctuation detaches. NOT a redundant january
    # duplicate -- do not delete it.
    ("january, third", "january, third"),
    # Surrounding text is preserved, and the ordinal keeps the
    # punctuation of its own token.
    ("meet me on january third.", "meet me on january 3rd."),
    (
        "meet me on january third at eleven fifteen pm",
        "meet me on january 3rd at 11:15 PM",
    ),
    # The three excluded month names, each an ordinary English word:
    # "may" the modal verb, "march" the verb, "august" the adjective.
    # They are not anchors, so an ordinal standing behind one stays a
    # word. These rows record the exclusion ruling (boss, 2026-08-25,
    # ratified by David the same day), NOT an accepted cost -- what the
    # ruling removed was a wrong conversion in ordinary dictation, and
    # this module ranks a wrong conversion worse than a missed one.
    ("you may first check the log", "you may first check the log"),
    ("we march third in the parade", "we march third in the parade"),
    ("the august first edition", "the august first edition"),
    # An excluded month stops the DATE, not the whole sentence: the
    # clock rule still reads the tail legitimately, so the output here
    # is mixed and this row pins the mixed string. Pinning the sentence
    # as wholly unchanged would pass for the wrong reason -- the date
    # refusal would hide the clock conversion, and the row would stay
    # green if the clock rule stopped firing.
    (
        "meet me on march third at eleven fifteen pm",
        "meet me on march third at 11:15 PM",
    ),
    # wh-itn-phase4-review.1. A recognizer that hands this module
    # "twenty-five" as ONE token hands it "twenty-fifth" the same way,
    # and phase 4 read only the cardinal of that pair: every row down to
    # the march row below stayed words. A hyphenated ordinal converts
    # exactly where its two-token form converts -- same nine-month
    # anchor, same day cap -- so each converting row here has a
    # two-token sibling above it.
    ("january twenty-fifth", "january 25th"),
    ("january twenty-first", "january 21st"),
    ("december thirty-first", "december 31st"),
    # Surrounding text is preserved, and the rule consumes exactly the
    # one token the compound occupies: the clock tail behind it still
    # converts on its own.
    (
        "meet me on january twenty-fifth at eleven fifteen pm",
        "meet me on january 25th at 11:15 PM",
    ),
    # A bare hyphenated ordinal has no anchor, so it stays words the way
    # a bare two-token ordinal does. Command safety does not move.
    ("twenty-fifth", "twenty-fifth"),
    ("press twenty-first", "press twenty-first"),
    # The day cap belongs to the caller, so the compound obeys it too --
    # one row a day past the cap and one whose tens word no month ever
    # reaches, the pair the two-token rows above already carry.
    ("january thirty-second", "january thirty-second"),
    ("january forty-fifth", "january forty-fifth"),
    # The head of a compound day has to be a TENS word. "one-third" is an
    # ordinary hyphenated fraction and is not the third of january. The
    # anchor is january because an excluded month refuses in front of the
    # compound read and could never reach it.
    ("january one-third", "january one-third"),
    # A hyphenated CARDINAL behind a month still reads as the phase-1
    # cardinal, not as a day: the same output its two-token sibling
    # ("january twenty five") pins.
    ("january twenty-five", "january 25"),
    # Inert row, kept for the same reason as the four march rows above:
    # march is not an anchor, so this refuses at the month check before
    # any compound-ordinal code runs. It pins the anchor-refusal output
    # for the hyphenated form; do not cite it as coverage of that code.
    ("march twenty-fifth", "march twenty-fifth"),
    # wh-itn-phase4-review.2, David's ruling of 2026-08-25 (option 1 of
    # the two he was offered): one month anchor over two days is a span
    # this module does not read, so the WHOLE span stays words. Phase 4
    # converted the ordinal behind the month and left the second one a
    # word -- "january 3rd and fourth" -- which is the mixed output the
    # refusal machinery exists to prevent.
    ("january third and fourth", "january third and fourth"),
    ("on january first and second", "on january first and second"),
    # The compound form of the same span, which needs both phase-4
    # review fixes at once: the compounds are read (review.1) and the
    # span they make is then refused (review.2).
    (
        "january twenty-fifth and twenty-sixth",
        "january twenty-fifth and twenty-sixth",
    ),
    # The shape is narrow. An "and" in front of anything that is not an
    # ordinal is ordinary speech, and the date behind it still converts.
    ("january third and we left", "january 3rd and we left"),
    # A second month reads its own date, which is what the range refusal
    # must not reach.
    ("january first and february second", "january 1st and february 2nd"),
    # Punctuation ends the span the way it ends every other one in this
    # module, so the date in front of it converts.
    ("january third, and fourth", "january 3rd, and fourth"),
    # wh-itn-phase4-review.3. Rule 7 calls both of these spans refusals,
    # so each one absorbs the number phrase standing against it, the way
    # every phase-1 refusal does ("twenty one and five five dollars"
    # stays words entirely). Rule 7 refused at its own endpoint before
    # this row set, and the scanner rescanned the words behind it: the
    # refused span stayed words and the phrase touching it converted --
    # "january third and fourth $5" -- which is the mixed output the
    # refusal machinery exists to prevent.
    # The range exit, one row per downstream rule the tail would reach:
    # money, the clock (in the compound form, which refuses through the
    # same exit), the digit sequence, and the paired house number.
    (
        "january third and fourth five dollars",
        "january third and fourth five dollars",
    ),
    (
        "january twenty-first and twenty-second five pm",
        "january twenty-first and twenty-second five pm",
    ),
    (
        "january third and fourth one two three",
        "january third and fourth one two three",
    ),
    (
        "january first and second twelve thirty four main street",
        "january first and second twelve thirty four main street",
    ),
    # The day-cap exit, in both the two-token and the hyphenated form,
    # and once in front of a decimal.
    (
        "january thirty second five dollars",
        "january thirty second five dollars",
    ),
    (
        "january thirty-second five dollars",
        "january thirty-second five dollars",
    ),
    (
        "january forty fifth three point five",
        "january forty fifth three point five",
    ),
    # Absorption reaches the number-ish vocabulary alone, so an ordinary
    # word ends the refused span and the amount behind it is separate
    # speech that still converts. This is the counterexample that keeps
    # the fix from being "a refused date silences the rest of the line".
    (
        "january third and fourth we paid five dollars",
        "january third and fourth we paid $5",
    ),
    # wh-itn-phase4-review.4. The chain of days is walked WHOLE. Both
    # refusals read one "and <ordinal>" hop and stopped there, so a third
    # day left the refused span early and the number phrase touching it
    # converted anyway -- "january third and fourth and fifth $5" -- the
    # same half conversion review.3 closed for the two-day form. A chain
    # element is a one-token, a two-token tens-plus-unit, or a hyphenated
    # ordinal, in any mix, and the walk reads all three.
    (
        "january third and fourth and fifth five dollars",
        "january third and fourth and fifth five dollars",
    ),
    (
        "january third and fourth and fifth and sixth five dollars",
        "january third and fourth and fifth and sixth five dollars",
    ),
    (
        "january third and fourth and fifth one two three",
        "january third and fourth and fifth one two three",
    ),
    (
        "january twenty-first and twenty-second and twenty-third five pm",
        "january twenty-first and twenty-second and twenty-third five pm",
    ),
    # The day-cap exit walks the same chain. It ran BEFORE the range
    # branch and so absorbed nothing past its own ordinal at all: one
    # continuation was enough to leak the tail, where the range exit
    # needed two.
    (
        "january thirty second and third five dollars",
        "january thirty second and third five dollars",
    ),
    (
        "january thirty-second and third five pm",
        "january thirty-second and third five pm",
    ),
    (
        "january thirty second and third and fourth five dollars",
        "january thirty second and third and fourth five dollars",
    ),
    (
        "january forty fifth and third three point five",
        "january forty fifth and third three point five",
    ),
    (
        "january thirty second and third twelve thirty four main street",
        "january thirty second and third twelve thirty four main street",
    ),
    # The walk stops where every other span in this module stops, and
    # these three rows are what keep it from being "a refused date
    # silences the rest of the line": an ordinary word, punctuation, and
    # a second month each end the chain, and the phrase behind them
    # converts.
    (
        "january third and fourth and fifth we paid five dollars",
        "january third and fourth and fifth we paid $5",
    ),
    (
        "january third and fourth, and fifth five dollars",
        "january third and fourth, and fifth $5",
    ),
    (
        "january third and fourth and february fifth",
        "january third and fourth and february 5th",
    ),
]

# The phase-1 module crewcut assigned the hyphenated token to this bead:
# "twenty-five" arrives as ONE token and no rule could read it.
HYPHENATED_NUMBER_CASES = [
    ("twenty-five dollars", "$25"),
    ("twenty-five", "25"),
    ("fifty-nine dollars and seventeen cents", "$59.17"),
    ("twenty-one thousand", "21,000"),
    ("one hundred and twenty-five", "125"),
    ("twelve-thirty-four main street", "1234 main street"),
    # A token whose pieces are not ALL number words never splits, so
    # nothing inside it can convert.
    ("x-ray", "x-ray"),
    ("well-known", "well-known"),
    ("twenty-five-year-old", "twenty-five-year-old"),
    ("one-off", "one-off"),
    # The hyphenated phone number normalize_transcript produces reaches
    # apply_itn intact and must survive it.
    ("call 703-555-1234 now", "call 703-555-1234 now"),
    # Command safety does not move: one number word is still one number
    # word, hyphen or no hyphen.
    ("backspace one", "backspace one"),
    # The token's own punctuation stays on its pieces -- the leading on
    # the first, the trailing on the last -- so a converted span keeps
    # both.
    ("twenty-five, dollars", "25, dollars"),
    ("(twenty-five)", "(25)"),
    # A split token no rule converts is rebuilt character for character,
    # hyphen included: the separator between the pieces is the hyphen
    # they were split at.
    ("backspace one-two", "backspace one-two"),
]

# --------------------------------------------------------------------
# Thousands separators (wh-itn-thousands-commas). David's request from
# live testing, 2026-08-25: "$1000000" must type as "$1,000,000" and
# "15000" as "15,000". Grouping starts at five digits, the threshold he
# ratified with the bead's acceptance criteria, so a scale-built year
# stays clean.
#
# A QUANTITY groups; an IDENTIFIER never does. That is the whole of the
# rule, and the rows below are grouped by which side of it they pin.
# --------------------------------------------------------------------

THOUSANDS_SEPARATOR_CASES = [
    # David's two examples.
    ("one million dollars", "$1,000,000"),
    ("fifteen thousand", "15,000"),
    ("one million", "1,000,000"),
    # The threshold, from both sides: 10000 is the smallest value that
    # groups, 9999 the largest that stays bare.
    ("ten thousand", "10,000"),
    ("nine thousand nine hundred ninety nine", "9999"),
    ("ten thousand dollars", "$10,000"),
    ("nine thousand nine hundred ninety nine dollars", "$9999"),
    # What the threshold is FOR: a scale-built year stays clean.
    ("two thousand five", "2005"),
    ("two thousand twenty five", "2025"),
    # Money. The cents are a two-digit field and never group; the
    # dollars do, which is the acceptance criteria's own "$15,000.20".
    ("fifteen thousand dollars", "$15,000"),
    ("fifteen thousand dollars and twenty cents", "$15,000.20"),
    ("twenty five thousand dollars and fifty cents", "$25,000.50"),
    ("one million two hundred thousand dollars", "$1,200,000"),
    # A cents anchor reads the same cardinal a dollar anchor does.
    ("fifteen thousand cents", "15,000 cents"),
    ("twenty cents", "20 cents"),
    # Decimals: the integer half is a value and groups, the fractional
    # half is a string of digits and never does.
    ("fifteen thousand point five", "15,000.5"),
    ("fifteen thousand point one two three four five", "15,000.12345"),
    ("nine thousand nine hundred ninety nine point five", "9999.5"),
    ("three point one two three four five", "3.12345"),
    # Digit-by-digit runs are identifiers, not quantities, so they never
    # group -- the ZIP code of the bead's own second criterion. Rule 5's
    # sequence and the spelled-out amount behind a unit anchor are both
    # such runs, which is why _write_number asks for the run and not
    # only the value. The accepted cost is that one amount has two
    # written forms: "fifteen thousand dollars" is "$15,000" and the
    # digits of the same amount are "$15000".
    ("nine oh two one oh", "90210"),
    ("one two three four five", "12345"),
    ("nine oh two one oh dollars", "$90210"),
    ("one five oh oh oh dollars", "$15000"),
    # Clock times, date ordinals and rule 6's paired house number are
    # identifiers too, and none of them can reach the threshold on its
    # own: the hour caps at 12, the day at 31, and both chunks of a
    # paired house number at 99, so 9999 is the largest it writes.
    ("eleven fifteen PM", "11:15 PM"),
    ("january twenty fifth", "january 25th"),
    ("twelve thirty four main street", "1234 main street"),
    # An address number spoken as ONE cardinal is the one house number
    # that CAN reach the threshold, and it stays bare -- see the
    # crewcut in _try_cardinal.
    ("fifteen thousand main street", "15000 main street"),
    ("one million main street", "1000000 main street"),
    # The suffix may stand up to three ordinary words past the number:
    # a directional prefix and a two-word street name are the common
    # multi-word address shapes (finding wh-itn-thousands-commas.1.1).
    # This reach is the FORMATTING guard's own; rule 6's licensing
    # reach stays one word.
    ("twenty thousand east main street", "20000 east main street"),
    ("fifteen thousand maple grove road", "15000 maple grove road"),
    (
        "fifteen thousand north maple grove road",
        "15000 north maple grove road",
    ),
    ("one million west elm court", "1000000 west elm court"),
    # The accepted cost that same crewcut names: the six suffix words
    # are ordinary nouns, so a quantity merely standing in front of one
    # loses its separators as well. A missed grouping, which this module
    # ranks below a wrong conversion.
    ("twenty thousand road miles", "20000 road miles"),
    # The counterexample that keeps the suppression narrow: with no
    # suffix in reach the quantity groups.
    ("fifteen thousand blocks away", "15,000 blocks away"),
    # Four ordinary words put the suffix past the bare-address reach,
    # so the quantity reading keeps its separators.
    (
        "fifty thousand people walked down the road",
        "50,000 people walked down the road",
    ),
    # Idempotence on written text. apply_itn runs on every interim
    # hypothesis and on text that already holds numerals, so a grouped
    # number it produced has to survive the next pass untouched.
    ("15,000", "15,000"),
    ("1,000,000", "1,000,000"),
    ("$1,000,000", "$1,000,000"),
    ("$15,000.20", "$15,000.20"),
    ("15,000.5", "15,000.5"),
    ("we sold 15,000 of them", "we sold 15,000 of them"),
]

# --------------------------------------------------------------------
# wh-itn-oh-letter-alias: the letter "o" is the spoken zero too.
#
# The provider emits the LETTER "o" where this module's digit
# vocabulary held only the word "oh", so David's dictated "seven point
# oh seven" typed the words. The alias is LOOKUP-ONLY: "o" is read as a
# digit wherever "oh" is read as one, and it is never rewritten to "oh"
# in the output, so a phrase that stays words keeps the word the
# speaker actually said ("o brother" is never "oh brother").
#
# The defect was worse than "the phrase stays words". The letter ended
# the number run, and the tail behind it converted alone, which is the
# half-conversion class the phase-1 review spent seven findings on
# (wh-shared-itn-review .6, .8, .10, .12, .14, .15, .16). Measured on
# dev 12c4db9b before the fix:
#   "seven point o seven dollars" -> "seven point o $7"
#   "five five five o one two"    -> "555 o one two"
#   "twelve o five pm"            -> "twelve o 5 PM"
#   "nine o five am"              -> "nine o 5 AM"
#
# Every row below mirrors an existing "oh" row and carries that row's
# measured value, so the two spellings can never drift apart.
# --------------------------------------------------------------------

LETTER_O_ALIAS_CASES = [
    # The bead's own reproduction. Both letter cases, because the
    # tokenizer compares on the lowercased word.
    ("seven point o seven", "7.07"),
    ("seven point O seven", "7.07"),
    ("seven point oh seven", "7.07"),
    # The half-conversions the defect produced, now whole.
    ("seven point o seven dollars", "7.07 dollars"),
    ("five five five o one two", "555012"),
    ("twelve o five pm", "12:05 PM"),
    ("nine o five am", "9:05 AM"),
    # Mirrors of COMMAND_SAFETY_CASES: a leading "o" never starts a
    # phrase, exactly as a leading "oh" never does.
    ("o five", "o five"),
    ("o two three", "o two three"),
    # Mirrors of MONEY_CASES.
    (
        "five dollars twenty one point o five dollars",
        "five dollars twenty one point o five dollars",
    ),
    (
        "five dollars twenty one point o five",
        "five dollars twenty one point o five",
    ),
    ("twenty thirty o five dollars", "twenty thirty o five dollars"),
    # Mirrors of CLOCK_TIME_CASES.
    ("nine o five AM", "9:05 AM"),
    ("twelve o five AM", "12:05 AM"),
    ("ten o five PM", "10:05 PM"),
    ("eleven o two AM", "11:02 AM"),
    ("o five pm", "o 5 PM"),
    ("twelve thirty o five pm", "twelve thirty o five pm"),
    ("one thirty o five pm", "one thirty o five pm"),
    ("eleven forty o two am", "eleven forty o two am"),
    ("twelve thirty, o five pm", "twelve thirty, o 5 PM"),
    # Mirrors of DECIMAL_CASES.
    ("three point o five", "3.05"),
    (
        "three point o twenty five dollars",
        "three point o twenty five dollars",
    ),
    (
        "three point o five hundred and twenty dollars",
        "three point o five hundred and twenty dollars",
    ),
    ("twenty thirty o five point two", "twenty thirty o five point two"),
    # Mirror of DIGIT_SEQUENCE_CASES.
    ("nine o two one o", "90210"),
    # Mirror of HOUSE_NUMBER_CASES.
    ("twelve thirty o street", "twelve thirty o street"),
    # Mirrors of THOUSANDS_SEPARATOR_CASES.
    ("nine o two one o dollars", "$90210"),
    ("one five o o o dollars", "$15000"),
    # The consequence the boss ruled on (option 1, 2026-08-28): "oh" is
    # a digit in this position today, so the letter has to be one too.
    # "five oh dollars" is "$50" and "five oh cents" is "50 cents".
    # Marked overridable pending David; if he objects the alias narrows
    # to fractions and long digit sequences only.
    ("five o dollars", "$50"),
    ("five o cents", "50 cents"),
]

# The letter "o" is an ordinary word far more often than it is a spoken
# zero. Every row here stays words BEFORE the alias and must still stay
# words after it. They carry no red-first evidence for that reason; the
# mutation gate is what proves them.
LETTER_O_STAYS_WORDS_CASES = [
    ("o brother", "o brother"),
    ("press o", "press o"),
    ("delete o", "delete o"),
    ("o", "o"),
    ("o five minutes", "o five minutes"),
    ("type o negative", "type o negative"),
    ("blood type o", "blood type o"),
    ("o positive", "o positive"),
    ("row o seat five", "row o seat five"),
    ("section o", "section o"),
    # "o'clock" is the letter's commonest neighbour. "clock" is not a
    # number word, so the run ends in front of it and no rule reads
    # what is left -- the same reason "nine oh clock" stays words.
    ("three o'clock", "three o'clock"),
    ("nine o clock", "nine o clock"),
    ("twelve o clock", "twelve o clock"),
    ("meet me at nine o clock", "meet me at nine o clock"),
]


ITN_ALL_CASES = (
    CARDINAL_CASES
    + COMMAND_SAFETY_CASES
    + MONEY_CASES
    + CLOCK_TIME_CASES
    + DECIMAL_CASES
    + CONJUNCTION_CONNECTOR_CASES
    + SCALE_SEQUENCE_CASES
    + DIGIT_SEQUENCE_CASES
    + SURROUNDING_TEXT_CASES
    + HOUSE_NUMBER_CASES
    + DATE_ORDINAL_CASES
    + HYPHENATED_NUMBER_CASES
    + THOUSANDS_SEPARATOR_CASES
    + LETTER_O_ALIAS_CASES
    + LETTER_O_STAYS_WORDS_CASES
)


class TestItnCardinals:
    @pytest.mark.parametrize("spoken,expected", CARDINAL_CASES)
    def test_cardinal_phrases(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnCommandSafety:
    @pytest.mark.parametrize("spoken,expected", COMMAND_SAFETY_CASES)
    def test_command_words_stay_words(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnMoney:
    @pytest.mark.parametrize("spoken,expected", MONEY_CASES)
    def test_money_phrases(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnClockTimes:
    @pytest.mark.parametrize("spoken,expected", CLOCK_TIME_CASES)
    def test_clock_times(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnDecimals:
    @pytest.mark.parametrize("spoken,expected", DECIMAL_CASES)
    def test_decimals(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnConjunctionConnectors:
    @pytest.mark.parametrize("spoken,expected", CONJUNCTION_CONNECTOR_CASES)
    def test_conjunction_connectors(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnDigitSequences:
    @pytest.mark.parametrize("spoken,expected", DIGIT_SEQUENCE_CASES)
    def test_digit_sequences(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnScaleSequences:
    @pytest.mark.parametrize("spoken,expected", SCALE_SEQUENCE_CASES)
    def test_scale_sequences(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnHouseNumbers:
    @pytest.mark.parametrize("spoken,expected", HOUSE_NUMBER_CASES)
    def test_house_numbers(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnDateOrdinals:
    @pytest.mark.parametrize("spoken,expected", DATE_ORDINAL_CASES)
    def test_date_ordinals(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnHyphenatedNumberWords:
    @pytest.mark.parametrize("spoken,expected", HYPHENATED_NUMBER_CASES)
    def test_hyphenated_number_words(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnLetterOAlias:
    """wh-itn-oh-letter-alias: the letter "o" reads as the spoken zero
    wherever the word "oh" does."""

    @pytest.mark.parametrize("spoken,expected", LETTER_O_ALIAS_CASES)
    def test_letter_o_reads_as_the_spoken_zero(self, spoken, expected):
        assert apply_itn(spoken) == expected

    @pytest.mark.parametrize("spoken,expected", LETTER_O_STAYS_WORDS_CASES)
    def test_the_ordinary_letter_still_stays_words(self, spoken, expected):
        assert apply_itn(spoken) == expected

    @pytest.mark.parametrize(
        "run",
        [
            ["twelve", "o", "five"],
            ["ten", "o", "five"],
            ["eleven", "o", "two"],
        ],
    )
    def test_cardinal_rule_refuses_a_run_holding_the_letter(self, run):
        assert _parse_cardinal(run) is None

    @pytest.mark.parametrize(
        "run",
        [
            ["twelve", "o", "five"],
            ["ten", "o", "five"],
            ["eleven", "o", "two"],
        ],
    )
    def test_digit_sequence_rule_refuses_a_teen_beside_the_letter(self, run):
        assert _try_digit_sequence(run) is None


class TestItnThousandsSeparators:
    @pytest.mark.parametrize("spoken,expected", THOUSANDS_SEPARATOR_CASES)
    def test_thousands_separators(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnOversizedRuns:
    """apply_itn runs on every interim hypothesis, so a pathological run
    must leave the text alone rather than raise (wh-shared-itn-review.7).
    Both of these raised ValueError: Exceeds the limit (4300 digits) for
    integer string conversion before the fix."""

    def test_a_long_run_of_hundred_words_is_returned_unchanged(self):
        spoken = " ".join(["hundred"] * 2150)
        assert apply_itn(spoken) == spoken

    def test_a_long_digit_run_under_a_unit_anchor_is_returned_unchanged(self):
        spoken = " ".join(["five"] * 5000) + " dollars"
        assert apply_itn(spoken) == spoken

    def test_a_long_digit_run_with_no_anchor_is_returned_unchanged(self):
        # The run cap, with no anchored rule standing behind it: the
        # digit-sequence rule joins strings and never converts an int,
        # so only the cap in _match_number_phrase keeps a 5000-word run
        # from being typed as a 5000-digit number.
        spoken = " ".join(["five"] * 5000)
        assert apply_itn(spoken) == spoken

    def test_the_cardinal_parser_refuses_an_over_long_run(self):
        # _parse_cardinal is reached through two capped callers today,
        # so this pins the parser's own cap for a future caller.
        assert _parse_cardinal(["five"] * 5000) is None

    def test_an_over_long_run_absorbs_an_adjacent_amount(self):
        # The over-long refusal is a refusal like any other, so it
        # absorbs the amount standing against it (wh-shared-itn-review
        # .14/.15). This ended at the run and typed "... and 20 cents".
        spoken = " ".join(["five"] * 65) + " dollars and twenty cents"
        assert apply_itn(spoken) == spoken

    def test_a_long_cents_tail_leaves_its_words_alone(self):
        # The cents tail is collected by _cents_after, past the run the
        # scanner capped, so _single_value carries its own cap.
        spoken = "one dollar and " + " ".join(["five"] * 5000) + " cents"
        assert apply_itn(spoken).endswith("five five five cents")


class TestItnIdempotence:
    """A second pass must change nothing. A phrase this module refuses
    stays words on every pass, and a phrase it converts is already
    written form (wh-shared-itn-review.6)."""

    @pytest.mark.parametrize("spoken,expected", ITN_ALL_CASES)
    def test_second_application_changes_nothing(self, spoken, expected):
        once = apply_itn(spoken)
        assert once == expected
        assert apply_itn(once) == once


class TestItnSurroundingText:
    @pytest.mark.parametrize("spoken,expected", SURROUNDING_TEXT_CASES)
    def test_surrounding_text_is_preserved(self, spoken, expected):
        assert apply_itn(spoken) == expected


class TestItnTeenOhRunsReachOnlyTheClockRule:
    """Admitting "oh" after a teen (wh-shared-itn-review.1) widens the
    runs _collect_run produces. These two invariants are why that is
    safe: no unanchored rule can convert a run of the widened shape,
    so the widened gate can only enable _try_clock_time."""

    @pytest.mark.parametrize(
        "run",
        [
            ["twelve", "oh", "five"],
            ["ten", "oh", "five"],
            ["eleven", "oh", "two"],
        ],
    )
    def test_cardinal_rule_refuses_a_run_holding_oh(self, run):
        assert _parse_cardinal(run) is None

    @pytest.mark.parametrize(
        "run",
        [
            ["twelve", "oh", "five"],
            ["ten", "oh", "five"],
            ["eleven", "oh", "two"],
        ],
    )
    def test_digit_sequence_rule_refuses_a_run_holding_a_teen(self, run):
        assert _try_digit_sequence(run) is None


class TestItnIsNotWiredIntoNormalize:
    """Phase 1 ships the stage only; the engines wire it in later."""

    def test_normalize_leaves_money_words_alone(self):
        assert (
            normalize_transcript("fifty nine dollars and seventeen cents")
            == "fifty nine dollars and seventeen cents"
        )

    def test_normalize_leaves_clock_words_alone(self):
        assert (
            normalize_transcript("eleven fifteen pm") == "eleven fifteen pm"
        )


class TestAgreementPrefixLen:
    def test_empty_prev_gives_zero(self):
        assert agreement_prefix_len([], ["delete", "two"]) == 0

    def test_full_agreement(self):
        assert agreement_prefix_len(["delete", "two"], ["delete", "two"]) == 2

    def test_partial_agreement_stops_at_divergence(self):
        assert (
            agreement_prefix_len(["delete", "two"], ["delete", "true", "x"])
            == 1
        )

    def test_current_shorter_than_prev(self):
        assert agreement_prefix_len(["delete", "two"], ["delete"]) == 1
