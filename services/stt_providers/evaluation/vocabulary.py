"""Command vocabulary for TTS test corpus generation.

Source: services/wheelhouse/speech/config/patterns.toml and pattern_catalog.py
Reference: docs/design/tts_test_corpus_design.md (Section 3)
"""

from dataclasses import dataclass


@dataclass
class Utterance:
    """A single utterance to synthesize and score against.

    Fields:
      text: the spoken phrase fed to TTS for audio synthesis.
      expected_transcription: the canonical string the STT model is
        expected to produce. The harness compares model output against
        this string under a loose match: a single trailing terminal
        punctuation character (. ? !) is stripped from each side, only
        the first character is lowercased, everything else stays strict.
        So mid-sentence proper-name capitalization, AM/PM casing, and
        digit vs word form are still tested. Sentence-start capitalization
        is handled downstream by WheelHouse, which is why the loose match
        treats it as cosmetic.
      category: filing label used only for directory layout in the corpus.
        Does not drive comparison logic.
    """

    text: str
    expected_transcription: str
    category: str
    is_litmus: bool = False


def _u(
    text: str,
    category: str,
    expected: str | None = None,
    is_litmus: bool = False,
) -> Utterance:
    """Build an Utterance. Defaults expected_transcription to text verbatim."""
    return Utterance(
        text=text,
        expected_transcription=expected if expected is not None else text,
        category=category,
        is_litmus=is_litmus,
    )


def build_vocabulary() -> list[Utterance]:
    """Build the complete utterance list for corpus generation."""
    utterances: list[Utterance] = []

    # ── 3.1 Single-Word Commands ─────────────────────────────────
    single_words = [
        "delete", "select", "copy", "paste", "cut", "undo", "redo",
        "escape", "enter", "tab", "save", "engage", "keyboard",
        "maximize", "minimize", "desktop", "browser", "editor",
        "find", "replace", "search", "bold", "italics", "underline",
        "uppercase", "lowercase", "capitalize", "compress",
    ]
    for word in single_words:
        utterances.append(_u(word, "single_word"))

    # ── 3.2 Multi-Word Commands ──────────────────────────────────
    multi_words = [
        "select all", "select word", "select line", "select paragraph",
        "delete word", "copy all", "copy line", "copy screen",
        "zoom in", "zoom out", "create tab", "create window",
        "close window", "new line", "new paragraph", "shift tab",
        "replace all", "bold text", "title case", "snake case",
        "camel case", "pascal case", "kebab case", "fix it",
        "cancel fix", "scroll down", "go back", "move left",
        "go right", "go home",
    ]
    for phrase in multi_words:
        utterances.append(_u(phrase, "multi_word"))

    # ── 3.3 Parameterized Commands ──────────────────────────────
    for n in ["one", "two", "three", "four", "five"]:
        utterances.append(_u(f"delete {n}", "parameterized"))
    for n in ["one", "two", "three"]:
        utterances.append(_u(f"backspace {n}", "parameterized"))
    for n in ["two", "three"]:
        utterances.append(_u(f"tab {n}", "parameterized"))
    for n in ["one", "two", "three", "four", "five"]:
        utterances.append(_u(f"item {n}", "parameterized"))
    for key in ["enter", "escape", "tab"]:
        utterances.append(_u(f"press {key}", "parameterized"))
    utterances.append(_u("go left", "parameterized"))
    utterances.append(_u("grab right", "parameterized"))

    # ── 3.4 Punctuation Commands ─────────────────────────────────
    punctuation_words = [
        "period", "comma", "colon", "semicolon", "question mark",
        "exclamation point", "hyphen", "dash", "slash", "backslash",
    ]
    for word in punctuation_words:
        utterances.append(_u(word, "punctuation"))

    # ── 3.5 Dictation Phrases ────────────────────────────────────
    # Sentence-start capitalization is handled by WheelHouse downstream, so
    # the expected_transcription uses lowercase for the first word except
    # for the pronoun "I" and proper nouns. The "port eight thousand"
    # phrase expects the digit form 8000 because in-context numbers are
    # an ITN test point.
    dictation_pairs = [
        ("The quick brown fox jumps over the lazy dog",
         "the quick brown fox jumps over the lazy dog"),
        ("Please send me the updated report by Friday",
         "please send me the updated report by Friday"),
        ("I need to fix the bug in the authentication module",
         "I need to fix the bug in the authentication module"),
        ("Can you review the pull request when you get a chance",
         "can you review the pull request when you get a chance"),
        # NOTE: "The server is running on port eight thousand" lives only
        # in the ITN category below. Keeping it here too would double-count
        # the same audio under different categories.
        ("We should refactor this function to be more readable",
         "we should refactor this function to be more readable"),
        ("Open the terminal and run the test suite",
         "open the terminal and run the test suite"),
        ("Save the file and close the editor window",
         "save the file and close the editor window"),
        ("Delete the selected text and paste the replacement",
         "delete the selected text and paste the replacement"),
        ("Go to the beginning of the line and select all",
         "go to the beginning of the line and select all"),
        ("Copy the error message from the console output",
         "copy the error message from the console output"),
        ("Undo the last three changes and redo the first one",
         "undo the last three changes and redo the first one"),
    ]
    for spoken, expected in dictation_pairs:
        utterances.append(_u(spoken, "dictation", expected=expected))

    # ── 3.6 Discontinuous Fragments ─────────────────────────────
    fragment_pairs = [
        (("Delete the selected text", "delete the selected text"),
         ("and paste the replacement", "and paste the replacement")),
        (("Go to the beginning", "go to the beginning"),
         ("of the line", "of the line")),
        (("Copy the error message", "copy the error message"),
         ("from the console output", "from the console output")),
        (("I need to fix", "I need to fix"),
         ("the bug in the authentication module",
          "the bug in the authentication module")),
        (("Open the terminal", "open the terminal"),
         ("and run the test suite", "and run the test suite")),
        (("Save the file", "save the file"),
         ("and close the editor", "and close the editor")),
        (("We should refactor this", "we should refactor this"),
         ("function to be more readable", "function to be more readable")),
        (("Please send me the", "please send me the"),
         ("updated report by Friday", "updated report by Friday")),
    ]
    for (first_text, first_expected), (second_text, second_expected) in fragment_pairs:
        utterances.append(_u(first_text, "discontinuous", expected=first_expected))
        utterances.append(_u(second_text, "discontinuous", expected=second_expected))

    # ── 3.7 ITN Test Cases ──────────────────────────────────────
    # Tests Inverse Text Normalization. Each entry has a spoken form and an
    # expected canonical form using digits, symbols, or proper-name casing.
    # A model that emits only spelled-out words fails ITN entries with
    # digit or symbol expected forms (which is the behavior we want, since
    # the corpus exists to catch models like that).
    itn_pairs = [
        # Cardinals (standalone)
        ("three", "3"),
        ("fifteen", "15"),
        ("forty two", "42"),
        ("one hundred", "100"),
        # Ordinals (verbal -- ordinals usually stay verbal in production)
        ("first", "first"),
        ("second", "second"),
        ("third", "third"),
        # Numbers in context
        ("there are five items in the list", "there are 5 items in the list"),
        ("move to line forty two", "move to line 42"),
        ("the server is running on port eight thousand",
         "the server is running on port 8000"),
        # Currency (round dollars)
        ("it costs five dollars", "it costs $5"),
        # Ambiguous colloquial currency -- canonical is the plain integer
        # because "twenty three fifty" can read as a year, a count, or a
        # currency phrase. We do not require the model to disambiguate.
        ("the total is twenty three fifty", "the total is 2350"),
        # Currency with cents (three phrasings, one canonical form)
        ("the price is five dollars and seventeen cents", "the price is $5.17"),
        ("the price is five seventeen", "the price is $5.17"),
        ("the price is five dollars seventeen cents", "the price is $5.17"),
        ("it costs ninety nine cents", "it costs $0.99"),
        # Time. AM/PM canonical uses uppercase because that is what
        # production STT models actually emit; under loose match,
        # mid-string casing is still strict.
        ("the time is eleven fifteen p m", "the time is 11:15 PM"),
        ("wake me at six thirty in the morning", "wake me at 6:30 AM"),
        ("set an alarm for two thirty", "set an alarm for 2:30"),
        # Decimals
        ("the value is three point one four", "the value is 3.14"),
        ("the version is two point seven", "the version is 2.7"),
        # Percentages
        ("battery is at fifty percent", "battery is at 50%"),
        ("the discount is twenty five percent", "the discount is 25%"),
        # Dates
        ("the date is January first twenty twenty six",
         "the date is January 1, 2026"),
        ("she was born on May fifteenth", "she was born on May 15"),
        # Phone numbers
        ("call me at five five five one two one two", "call me at 555-1212"),
        ("the number is five five five eight three two five seven six four",
         "the number is 555-832-5764"),
        # Email and URL
        ("send it to john at example dot com", "send it to john@example.com"),
        ("visit example dot com", "visit example.com"),
        # Acronyms removed: Edge-TTS does not pronounce English acronyms
        # letter-by-letter from plain text, and we test STT with audio
        # that approximates real speech. A 2026-04-26 trial run showed
        # the synthesizer either left the audio unintelligible or said
        # "Jason" instead of "J-S-O-N", so the entries were testing TTS
        # quality, not the model's acronym handling. Re-add only when
        # the corpus has audio recorded from a real human speaker.
        # IP address (digit concatenation plus dot preservation)
        ("connect to one nine two dot one six eight dot one dot seventeen",
         "connect to 192.168.1.17"),
        # Digit concatenation in non-phone context
        ("the room number is one one five nine", "the room number is 1159"),
        # Proper names -- places
        ("the meeting is in Boston on Friday",
         "the meeting is in Boston on Friday"),
        ("John went to Microsoft headquarters",
         "John went to Microsoft headquarters"),
        ("we use Python and JavaScript",
         "we use Python and JavaScript"),
        # Proper names -- people
        ("Bill Smith called this morning",
         "Bill Smith called this morning"),
        ("Dolores wrote the report",
         "Dolores wrote the report"),
        ("Edward Hopper painted Nighthawks",
         "Edward Hopper painted Nighthawks"),
    ]
    for spoken, expected in itn_pairs:
        utterances.append(_u(spoken, "itn", expected=expected))

    # ── 3.8 Litmus Tests ────────────────────────────────────────
    # "delete" is the known Zipformer failure case. Variants are handled
    # by the generator (rate/voice combos), but we still need an entry
    # to flag it.
    utterances.append(_u("delete", "litmus", is_litmus=True))

    return utterances
