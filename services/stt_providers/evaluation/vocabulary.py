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
    # "delete" is the known failure case of the retired streaming provider.
    # Variants are handled
    # by the generator (rate/voice combos), but we still need an entry
    # to flag it.
    utterances.append(_u("delete", "litmus", is_litmus=True))

    # ── Voice Access parity -- wh-voice-access-parity.1.1 punctuation and symbol names ──
    va_punctuation_names = [
        "full stop", "dot dot dot", "open quotes", "close quotes",
        "begin single quote", "open single quote", "end single quote",
        "close single quote", "left parentheses", "open parentheses",
        "right parentheses", "close parentheses", "open bracket",
        "close bracket", "left brace", "open brace", "right brace",
        "close brace", "forward slash", "pipe character", "paragraph sign",
        "paragraph mark", "section sign", "copyright sign", "registered sign",
        "degree symbol", "number sign", "pound sign", "minus sign",
        "multiplication sign", "division sign", "less than sign",
        "greater than sign", "pound sterling sign", "euro sign", "yen sign",
    ]
    for phrase in va_punctuation_names:
        utterances.append(_u(phrase, "va_punctuation_symbols"))

    # ── Voice Access parity -- wh-voice-access-parity.1.2 select-by-range ──
    for direction in ["next", "previous", "last"]:
        for unit in ["characters", "words", "lines", "paragraphs"]:
            for n in ["two", "five"]:
                utterances.append(
                    _u(f"select {direction} {n} {unit}", "va_select_range")
                )
    # "select word", "select line" and "select paragraph" are also named
    # by this bead, but they are already represented under multi_word
    # (3.2); the single-representation rule (see the .1.6 comment below)
    # keeps each utterance in the first category that required it
    # (wh-voice-access-parity.3.4).
    va_select_fixed = [
        "select this word", "select this line", "select this paragraph",
        "unselect that", "clear selection",
    ]
    for phrase in va_select_fixed:
        utterances.append(_u(phrase, "va_select_range"))

    # ── Voice Access parity -- wh-voice-access-parity.1.3 delete, cut and copy range commands ──
    for action in ["delete", "cut", "copy"]:
        for direction in ["next", "previous", "last"]:
            for unit in ["characters", "words", "lines", "paragraphs"]:
                for n in ["three", "four"]:
                    utterances.append(
                        _u(
                            f"{action} {direction} {n} {unit}",
                            "va_delete_cut_copy_range",
                        )
                    )
    # "delete word" stays under multi_word (3.2) per the
    # single-representation rule (wh-voice-access-parity.3.4).
    va_delete_fixed = [
        "delete all", "delete line", "delete paragraph",
        "delete this word", "delete this line", "delete this paragraph",
    ]
    for phrase in va_delete_fixed:
        utterances.append(_u(phrase, "va_delete_cut_copy_range"))

    # ── Voice Access parity -- wh-voice-access-parity.1.4 format-over-a-range commands ──
    # Two representative range specs per formatting verb (direction and unit
    # varied for coverage), matching the bead's own "about 12 forms" scope.
    va_format_range = [
        "bold next two words", "bold last three lines",
        "italicize next two words", "italicize previous three paragraphs",
        "underline next two words", "underline last three characters",
        "capitalize next two words", "capitalize previous three lines",
        "uppercase next two words", "uppercase last three paragraphs",
        "lowercase next two words", "lowercase previous three characters",
    ]
    for phrase in va_format_range:
        utterances.append(_u(phrase, "va_format_range"))

    # ── Voice Access parity -- wh-voice-access-parity.1.5 line and document navigation commands ──
    for verb in ["move", "go"]:
        for direction in ["up", "down"]:
            for unit in ["lines", "paragraphs"]:
                for n in ["two", "three"]:
                    utterances.append(
                        _u(f"{verb} {direction} {n} {unit}", "va_navigation")
                    )
    for verb in ["move", "go"]:
        for direction in ["left", "right"]:
            for unit in ["characters", "words"]:
                for n in ["two", "three"]:
                    utterances.append(
                        _u(f"{verb} {direction} {n} {unit}", "va_navigation")
                    )
    va_navigation_fixed = ["go to top", "go to bottom", "go to end"]
    for phrase in va_navigation_fixed:
        utterances.append(_u(phrase, "va_navigation"))
    for position in ["beginning", "end"]:
        for landmark in ["document", "line", "word", "paragraph"]:
            utterances.append(
                _u(f"go to the {position} of the {landmark}", "va_navigation")
            )
    for position in ["beginning", "end"]:
        utterances.append(
            _u(f"move to the {position} of the selection", "va_navigation")
        )

    # ── Voice Access parity -- wh-voice-access-parity.1.6 command aliases ──
    # "select this word", "select this line", "select this paragraph" and
    # "delete all" are also named in this bead's SCOPE, but they duplicate
    # forms already required by .1.2 and .1.3, so they are represented once
    # each under va_select_range and va_delete_cut_copy_range instead of
    # being repeated here.
    # "dismiss" MEASURES A DELIBERATE ABSENCE, and it is the one row here
    # that is expected to match no command at all. David deleted the bare
    # "dismiss" alias from the Escape entry on 2026-08-17; patterns.toml
    # now has ^escape$ with no alias. Only ^(?:hide|dismiss) numbers$ and
    # ^(?:hide|dismiss) grid$ came back, on 2026-08-20, under
    # wh-dismiss-alias-restore. Keep the row: it is how the benchmark shows
    # that the bare word still reaches nothing. Do not "fix" it by adding
    # an alias, and do not delete it as dead weight.
    va_command_aliases = [
        "tap", "copy that", "cut that", "paste that", "paste here",
        "undo that", "redo that", "bold that", "italicize that",
        "underline that", "capitalize that", "cap that", "uppercase that",
        "all caps that", "lowercase that", "no caps that", "show numbers",
        "hide numbers", "hide grid", "open voice access help", "dismiss",
    ]
    for phrase in va_command_aliases:
        utterances.append(_u(phrase, "va_command_aliases"))

    # ── Voice Access parity -- wh-voice-access-parity.1.7 window and per-app commands ──
    # "go home" stays under multi_word (3.2) per the
    # single-representation rule (wh-voice-access-parity.3.4).
    va_window_fixed = [
        "restore window", "show task switcher", "list all windows",
        "show all windows", "minimize all windows",
        "snap window to the left", "snap window to the right",
        "snap window to the top", "snap window to the bottom",
        "go to desktop",
    ]
    for phrase in va_window_fixed:
        utterances.append(_u(phrase, "va_window_app"))
    for verb in [
        "switch to", "go to", "close", "exit", "quit", "minimize",
        "maximize", "show",
    ]:
        for app in ["notepad", "chrome"]:
            utterances.append(_u(f"{verb} {app}", "va_window_app"))
    # No keyboard row belongs in this category, and the absence is the
    # point. The corpus speaks what the live file defines (same rule as
    # the provider search commands, commit b201a7c7). David removed the
    # four show/hide keyboard phrases on 2026-08-27 because win+ctrl+o is
    # a toggle, and ruled the same day that the word "keyboard" IS the
    # command, so the one live entry is ^keyboard$
    # (wh-keyboard-toggle-only). That word is already the single_word
    # entry "keyboard" above, and build_vocabulary's single-representation
    # rule (the .1.6 comment) forbids the same text in two categories: the
    # same audio would be scored twice and double-count both per-category
    # aggregates (wh-voice-access-parity.3.4). So the keyboard command is
    # measured, under single_word, and a reader who comes here looking for
    # it should look there.

    # ── Voice Access parity -- wh-voice-access-parity.1.8 search commands ──
    va_search_terms = ["restaurants nearby", "python tutorials"]
    # The three provider forms carry "on" because the live entries do:
    # ^search on google for (.+)$, ^search on bing for (.+)$ and
    # ^search on youtube for (.+)$ (patterns.toml). Without it these rows
    # fell into the catch-all ^search(?: for)? (.+)$, which captures
    # "Google for X" as the search term, so the benchmark never exercised
    # the provider entries at all (wh-voice-access-parity.3.7, Codex round
    # 2). "search Windows for {term}" keeps its wording on purpose: its
    # live entry is ^search windows for (.+)$, with no "on".
    # TestSearchProviderFormsMatchThePatternsFile in
    # tests/test_vocabulary.py holds both directions of this agreement.
    va_search_templates = [
        "search {term}", "search for {term}", "search Windows for {term}",
        "search on Google for {term}", "search on Bing for {term}",
        "search on YouTube for {term}",
    ]
    for template in va_search_templates:
        for term in va_search_terms:
            utterances.append(_u(template.format(term=term), "va_search"))

    # ── Voice Access parity -- wh-voice-access-parity.1.9 dictation escape and bare key commands ──
    # Bare "enter", "tab" and "escape" already exist in the 3.1 single-word
    # section above, so they are not duplicated here.
    va_literal_bypass = [
        "type delete", "type escape", "dictate select", "dictate copy",
    ]
    for phrase in va_literal_bypass:
        utterances.append(_u(phrase, "va_literal_bypass"))

    return utterances
