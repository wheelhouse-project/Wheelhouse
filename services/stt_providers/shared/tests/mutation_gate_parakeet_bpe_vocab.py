"""Mutation gate for the wh-parakeet-hotword-vocab guard tests.

Proves each guard test fails for the right reason when the behaviour it
protects is broken. Run from services/stt_providers/shared with plain
python (the gate needs only the standard library; it invokes `uv run
pytest` as a subprocess in the parakeet service directory):

    python tests/mutation_gate_parakeet_bpe_vocab.py
    python tests/mutation_gate_parakeet_bpe_vocab.py tokens-txt
    python tests/mutation_gate_parakeet_bpe_vocab.py --check

The defect this branch fixed: sherpa's bpe_vocab argument wants a
sentencepiece vocabulary, two columns of token and log probability, and
the engine handed it the model's tokens.txt, whose second column is an
integer id. The shape check passes and the tokenization is wrong, so
nothing visible failed -- hint boosting simply did not do what the code
said it did.

So the first group of mutations restores that defect and the weaker
forms of it: pass tokens.txt again, skip validation entirely, accept a
file that is not a vocabulary, and reject one that is. The id test is
the load-bearing one, because it is the only check that separates
tokens.txt from a real vocabulary -- every other check passes on both.

The second group breaks the degrade path. An unusable vocabulary must
leave the recognizer with no hotword arguments at all: passing
modeling_unit='bpe' with a missing bpe_vocab access-violates sherpa
natively, so a half-degrade crashes the provider rather than losing a
feature.

The third group breaks the honest report. The whole point of the status
is that the user is not told boosting is on when it is off, so these
mutate the notice into the shapes that lie: claim active when it is not,
drop the reason, and drop the notice altogether.

Note on mutation 'notice-truthy-not-is-true': _hotwords_notice reads
each field with `is True` so a MagicMock engine cannot make the claim.
Relaxing it to truthiness is caught by the older readiness test whose
fixture engine is a MagicMock: every attribute of a MagicMock is truthy,
so the notice grows " Word boosting is on." and the test's equality
assertion on the notice text fails. The test that hands the
announcement a bare object() does NOT catch it, because getattr returns
None for the status and None's missing attributes read as False under
both forms.

The fourth group breaks delivery. A correct vocabulary in the repository
does nothing until the installer puts it beside the model, and the
expensive way to do that is to re-download 650 MB. These mutations require
the vocabulary for model completeness (which makes every model installed
before this release read as incomplete, and the incomplete path deletes
and re-downloads), drop the call site, write the file through a text-mode
call that rewrites its line endings, skip the sidecar check, keep a stale
copy, and turn a missing vocabulary into a refusal to install.

Those six mutate a PowerShell file, so they are the reason the runner now
parses a .ps1 mutant before running it: the installer's test harness
begins by parsing the whole script, so one mutant that does not parse
would fail every test in the selection at once and read as caught.

Nothing here covers tests/test_hotwords_integration.py, and that is
deliberate. Those tests need a machine carrying an installed int8 model
with a bpe.vocab beside it, and they skip on one that does not. A
mutation whose expected catchers are those tests would therefore report
a survivor on every machine without the model, which is worse than no
entry at all. Their red-first proof was taken by hand instead, with a
byte-exact replace and revert, and it is recorded in the commit that
rewrote them: restoring `bpe_vocab = tokens` fails the two engine tests
on `hotwords_status.active`, and disabling the id-column test fails the
one that separates the real vocabulary from the real tokens.txt. Re-take
it the same way if that file changes.

Runner discipline lives in tests/mutation_gate_runner.py.
"""
import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path, but this keeps it working when the gate
# is invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PARAKEET = SHARED.parent / "sherpa_offline_parakeet_stt_server"

ENGINE = PARAKEET / "sherpa_engine.py"
MAIN = PARAKEET / "main.py"

HOTWORD_TESTS = "tests/test_hotwords.py"
READINESS_TESTS = "tests/test_startup_readiness.py"

# The delivery half lives in the release tooling: the installer is a
# PowerShell script, and its tests drive it through a PowerShell subprocess
# from the scripts/release service.
RELEASE = SHARED.parents[2] / "scripts" / "release"
INSTALLER = RELEASE / "public" / "install-wheelhouse.ps1"

# Node ids, not the whole file: test_installer.py holds over 150 tests and
# a mutation only needs the eight about vocabulary delivery. Every name
# here is validated against real collection before the first mutation runs.
_INSTALLER_FILE = "tests/test_installer.py"
INSTALLER_TESTS = tuple(
    f"{_INSTALLER_FILE}::{name}"
    for name in (
        "test_installer_parses_clean",
        "test_the_vocabulary_reaches_a_model_directory_that_lacks_it",
        "test_an_lf_vocabulary_is_not_translated_to_crlf",
        "test_an_already_complete_model_gains_the_vocabulary_without_downloading",
        "test_the_vocabulary_is_never_required_for_model_completeness",
        "test_a_vocabulary_that_fails_its_sidecar_is_not_installed",
        "test_a_stale_vocabulary_is_replaced",
        "test_a_missing_shipped_vocabulary_does_not_stop_the_install",
        "test_the_install_flow_delivers_the_vocabulary_after_the_model",
    )
)


MUTATIONS = [
    # ---- the defect itself, and the checks that keep it out --------------
    {
        # The original bug in one line: hand sherpa the token list again.
        "name": "tokens-txt-as-the-vocabulary",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": '        bpe_vocab = model_dir / BPE_VOCAB_FILENAME\n',
        "new": '        bpe_vocab = tokens\n',
        "expect": [
            "test_hotwords_passed_with_bpe_vocab_and_beam_search",
            "test_good_vocab_reports_active",
            "test_missing_vocab_degrades_and_says_so",
        ],
    },
    {
        # Validation removed: whatever sits at bpe.vocab is used.
        "name": "validation-skipped",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "                reason = bpe_vocab_rejection_reason(bpe_vocab, tokens)\n",
        "new": "                reason = None\n",
        "expect": [
            "test_missing_vocab_degrades_and_says_so",
            "test_tokens_txt_as_the_vocabulary_is_rejected",
            "test_vocabulary_from_another_model_is_rejected",
            "test_one_column_vocabulary_is_rejected",
            "test_empty_vocabulary_is_rejected",
        ],
    },
    {
        # The load-bearing check. Without it tokens.txt passes every other
        # test in the function, because it really does have two columns and
        # its ids really do parse as floats.
        "name": "id-column-check-dropped",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    if every_value_is_an_id:\n",
        "new": "    if False:\n",
        "expect": [
            "test_tokens_txt_as_the_vocabulary_is_rejected",
            "test_tokens_txt_with_ids_that_start_at_one_is_rejected",
            "test_tokens_txt_in_another_line_order_is_rejected",
        ],
    },
    {
        # The check this fix replaced (wh-parakeet-hotword-vocab.1.1). It
        # compares each value with its own line number, so it rejects only
        # an id-ordered copy of tokens.txt: renumber from one, or sort the
        # lines, and the same wrong file is accepted as a vocabulary.
        "name": "id-column-check-only-catches-line-numbered-ids",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": (
            '        written_negative = value_text.strip().startswith("-")\n'
            "        if every_value_is_an_id and (\n"
            "            written_negative or not value.is_integer()\n"
            "        ):\n"
        ),
        "new": "        if every_value_is_an_id and value != float(index):\n",
        "expect": [
            "test_tokens_txt_with_ids_that_start_at_one_is_rejected",
            "test_tokens_txt_in_another_line_order_is_rejected",
        ],
    },
    {
        # wh-parakeet-hotword-vocab.2.1. Read the sign from the number
        # instead of the text and a score written -0 becomes the id 0:
        # float("-0") is -0.0, which is whole and is not less than zero.
        # This mutation is the pre-fix code exactly.
        "name": "id-column-check-ignores-the-written-sign",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": (
            '        written_negative = value_text.strip().startswith("-")\n'
            "        if every_value_is_an_id and (\n"
            "            written_negative or not value.is_integer()\n"
            "        ):\n"
        ),
        "new": (
            "        if every_value_is_an_id and not "
            "(value >= 0 and value.is_integer()):\n"
        ),
        "expect": [
            "test_a_vocabulary_scored_only_zero_and_negative_zero_is_accepted"
        ],
    },
    {
        # The other half of the same rule. An id is whole; a value that
        # is not whole is a score however it is signed. Dropping the
        # whole-number half calls a column of unsigned fractions a token
        # list.
        "name": "id-column-check-ignores-fractions",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "            written_negative or not value.is_integer()\n",
        "new": "            written_negative\n",
        "expect": [
            "test_a_vocabulary_of_unsigned_fractional_scores_is_accepted"
        ],
    },
    {
        # Two columns no longer required, so a one-column file is read as a
        # vocabulary whose every token is the whole line. The fabricated
        # token is stripped because Path.write_text writes CRLF on
        # Windows and 62a350fb reads the bytes, so an unstripped line
        # carries a trailing carriage return into the piece; the model
        # overlap floor then refuses the file for a reason that has
        # nothing to do with the shape check, and the mutation reads as
        # a survivor of a test that is working correctly.
        "name": "two-column-shape-not-required",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        if len(items) < 2:\n",
        "new": "        if False:\n            pass\n        if len(items) < 2:\n            items = [line.strip(), \"-1.0\"]\n        if False:\n",
        "expect": ["test_one_column_vocabulary_is_rejected"],
    },
    {
        # An empty file accepted. sherpa is then handed a vocabulary with no
        # tokens in it, which is the silent-no-boosting case again.
        "name": "empty-vocabulary-accepted",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    if not any(line.strip() for line in raw_lines):\n        return f\"the vocabulary file is empty: {vocab_path}\"\n",
        "new": "    if not any(line.strip() for line in raw_lines):\n        return None\n",
        "expect": ["test_empty_vocabulary_is_rejected"],
    },
    {
        # Blank rows skipped again, the way the pre-fix code skipped them.
        # sherpa's own loader reads every physical line, so a blank row it
        # never sees here is a row that ends the provider later
        # (wh-parakeet-hotword-vocab.2.2).
        "name": "blank-rows-skipped-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    for index, line in enumerate(raw_lines):\n",
        "new": "    for index, line in enumerate([l for l in raw_lines if l.strip()]):\n",
        "expect": ["test_a_blank_row_in_the_vocabulary_is_rejected"],
    },
    {
        # The blank-row branch removed. The line is still rejected by the
        # two-field test below it, so what this proves is the reason the
        # user is given: the notice has to say the row is blank.
        "name": "blank-row-reason-lost",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        if not items:\n            return (\n                f\"line {index + 1} is blank, and sherpa\'s vocabulary \"\n                f\"loader ends the provider on a line it cannot read as \"\n                f\"a token and a score\"\n            )\n",
        "new": "        if False:\n            pass\n",
        "expect": ["test_a_blank_row_in_the_vocabulary_is_rejected"],
    },
    {
        # Splitting on the last tab again, which is exactly the pre-fix
        # rule. It keeps a piece holding a space or a tab whole and reads
        # the score after it, so the line passes and the native loader
        # ends the provider on it (wh-parakeet-hotword-vocab.2.2).
        "name": "piece-split-on-the-last-tab-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        # The same line opens read_token_pieces since .2.6, so the next
        # line keeps this pattern on the vocabulary check.
        "old": "        items = _loader_items(line)\n        if not items:\n",
        "new": "        items = line.rsplit(\"\t\", 1) if \"\t\" in line else line.rsplit(None, 1)\n        if not items:\n",
        "expect": [
            "test_a_piece_holding_a_space_is_rejected",
            "test_a_piece_holding_a_tab_is_rejected",
        ],
    },
    {
        # Python's own Unicode split back in place of the loader's C
        # whitespace. It separates on U+00A0 and U+2028, which the loader
        # reads as bytes inside a token, so a row the loader dies on was
        # accepted and two rows it reads happily were refused
        # (wh-parakeet-hotword-vocab.2.3).
        "name": "unicode-item-split-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        # The same line opens read_token_pieces since .2.6, so the next
        # line keeps this pattern on the vocabulary check.
        "old": "        items = _loader_items(line)\n        if not items:\n",
        "new": "        items = line.split()\n        if not items:\n",
        "expect": [
            "test_a_non_breaking_space_as_the_score_delimiter_is_rejected",
            "test_a_piece_holding_a_non_breaking_space_is_accepted",
            "test_a_piece_holding_a_line_separator_is_accepted",
        ],
    },
    {
        # str.splitlines back in place of the newline split. std::getline
        # ends a line at a newline and at nothing else, so a piece holding
        # U+2028 is one good row to the loader and was two broken rows
        # here (wh-parakeet-hotword-vocab.2.3). The split lives in
        # _loader_lines since .2.6, shared by both readers, so the
        # tokens.txt tests catch this as well.
        "name": "unicode-line-split-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    lines = text.split(\"\\n\")\n",
        "new": "    lines = text.splitlines()\n",
        "expect": [
            "test_a_piece_holding_a_line_separator_is_accepted",
            "test_a_model_token_holding_a_line_separator_is_read_whole",
            "test_a_character_the_model_keeps_inside_a_token_is_covered",
        ],
    },
    {
        # Exactly two items required again. The loader reads two items and
        # ignores the rest of the line, so this refuses a vocabulary it
        # accepts and turns boosting off for it
        # (wh-parakeet-hotword-vocab.2.3).
        "name": "exactly-two-items-required-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        if len(items) < 2:\n",
        "new": "        if len(items) != 2:\n",
        "expect": ["test_a_row_carrying_a_third_column_is_accepted"],
    },
    {
        # The empty item that a final newline leaves behind kept as a
        # line. std::getline does not report one, so keeping it makes
        # every well-formed vocabulary look like it ends in a blank row
        # (wh-parakeet-hotword-vocab.2.3).
        "name": "final-newline-read-as-a-blank-row",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    if lines and lines[-1] == \"\":\n        lines.pop()\n",
        "new": "    if False:\n        lines.pop()\n",
        # Since .2.6 the same empty item would read as the space symbol
        # in read_token_pieces, which the reader test pins.
        "expect": [
            "test_good_vocab_reports_active",
            "test_a_trailing_newline_does_not_add_a_token",
        ],
    },
    {
        # read_text back in place of the byte read. Its universal-newline
        # translation turns a lone \r into a \n before the check sees the
        # file, so a row the loader reads as one token and one score
        # becomes two broken lines here
        # (wh-parakeet-hotword-vocab.2.4).
        "name": "read-text-normalizes-carriage-returns-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        text = _loader_text(vocab_path)\n",
        "new": "        text = vocab_path.read_text(encoding=\"utf-8\")\n",
        "expect": ["test_a_row_delimited_by_a_carriage_return_is_accepted"],
    },
    {
        # The existence check back in front of the read. Path.exists calls
        # stat and pathlib re-raises a stat failure that is not a
        # missing-path error, so a vocabulary the account cannot read ends
        # the provider instead of turning boosting off
        # (wh-parakeet-hotword-vocab.2.5).
        "name": "vocabulary-existence-checked-before-the-read-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    the difference.\"\"\"\n    try:\n",
        "new": ("reaches the difference.\"\"\"\n"
                "    if not vocab_path.exists():\n"
                "        return f\"the vocabulary file is missing: {vocab_path}\"\n"
                "    try:\n"),
        "expect": [
            "test_a_vocabulary_the_account_cannot_read_degrades_and_says_so"
        ],
    },
    {
        # The missing-file arm removed, so the general arm catches a
        # missing file too and reports it as unreadable. The two cases
        # need different words: one is the ordinary state of an
        # installation without a bpe.vocab, the other is a fault
        # (wh-parakeet-hotword-vocab.2.5).
        "name": "missing-vocabulary-reported-as-unreadable",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    except FileNotFoundError:\n"
                "        return f\"the vocabulary file is missing: {vocab_path}\"\n"),
        "new": "",
        "expect": ["test_missing_vocab_degrades_and_says_so"],
    },
    {
        # The hotwords file's stat failure left uncaught, which is the
        # shape this code had before wh-parakeet-hotword-vocab.2.5. The
        # engine never reads that file, so its existence check is the only
        # place a denial reaches this code.
        "name": "hotwords-file-stat-error-not-caught",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    try:\n"
                "        if not hotwords_path.exists():\n"
                "            return f\"the hotwords file is missing: {hotwords_path}\"\n"
                "    except OSError as e:\n"
                "        return f\"the hotwords file could not be read: {e}\"\n"),
        "new": ("    if not hotwords_path.exists():\n"
                "        return f\"the hotwords file is missing: {hotwords_path}\"\n"),
        "expect": [
            "test_a_hotwords_file_the_account_cannot_read_degrades_and_says_so"
        ],
    },
    # ---- .2.6: tokens.txt read the way the loader reads it -------------
    {
        # The tokens.txt reader back on str.splitlines, bypassing
        # _loader_lines. A token holding U+2028 comes out in fragments,
        # a vocabulary made of such tokens is refused as foreign, and a
        # hint holding the character is dropped
        # (wh-parakeet-hotword-vocab.2.6).
        "name": "tokens-txt-lines-split-on-unicode-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    for line in _loader_lines(_loader_text(path)):\n",
        "new": "    for line in path.read_text(encoding=\"utf-8\").splitlines():\n",
        "expect": [
            "test_a_model_token_holding_a_line_separator_is_read_whole",
            "test_a_character_the_model_keeps_inside_a_token_is_covered",
        ],
    },
    {
        # The token separated from its id on Unicode whitespace again.
        # A token ending in U+00A0 loses it, and str.split also separates
        # on U+2028, so both reader tests and the charset test catch it
        # (wh-parakeet-hotword-vocab.2.6).
        "name": "token-column-split-on-unicode-whitespace-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        items = _loader_items(line)\n        pieces.append(items[0] if len(items) >= 2 else \" \")\n",
        "new": "        items = line.split()\n        pieces.append(items[0] if len(items) >= 2 else \" \")\n",
        "expect": [
            "test_a_model_token_ending_in_a_non_breaking_space_is_read_whole",
            "test_a_model_token_holding_a_line_separator_is_read_whole",
            "test_a_character_the_model_keeps_inside_a_token_is_covered",
        ],
    },
    {
        # A one-item line read as its item rather than as the space
        # symbol. ReadTokens reads such a line as the space symbol whose
        # id is that item (wh-parakeet-hotword-vocab.2.6).
        "name": "one-item-line-not-the-space-symbol",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        pieces.append(items[0] if len(items) >= 2 else \" \")\n",
        "new": "        pieces.append(items[0] if items else \" \")\n",
        "expect": ["test_a_one_item_line_is_the_space_symbol"],
    },
    {
        # The charset back on its own splitlines loop, bypassing the
        # shared reader. This is the main.py half of the finding: the
        # engine can read tokens.txt correctly while the hint check still
        # drops a hint the model covers (wh-parakeet-hotword-vocab.2.6).
        "name": "charset-read-with-splitlines-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": MAIN,
        "old": "        for piece in read_token_pieces(tokens_path):\n            chars.update(piece)\n",
        "new": "        for line in tokens_path.read_text(encoding=\"utf-8\").splitlines():\n            if not line.strip():\n                continue\n            chars.update(line.rsplit(None, 1)[0])\n",
        "expect": ["test_a_character_the_model_keeps_inside_a_token_is_covered"],
    },
    {
        # The score read with plain float() again, which is exactly the
        # pre-fix rule. float() reads nan, inf, Unicode digits, Unicode
        # whitespace and any magnitude a double holds; the loader reads
        # an ASCII number within float32 range and ends the provider on
        # every one of those (wh-parakeet-hotword-vocab.2.7).
        "name": "score-read-with-plain-float-again",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    if _SCORE_SYNTAX.fullmatch(value_text) is None:\n"
                "        return None\n"
                "    value = float(value_text)\n"),
        "new": ("    try:\n"
                "        value = float(value_text)\n"
                "    except ValueError:\n"
                "        return None\n"),
        "expect": [
            # The range arms below the syntax check still refuse an
            # out-of-range magnitude under this mutation, so the range
            # test is not one of its catchers.
            "test_a_score_the_loader_cannot_read_as_a_number_is_rejected",
            "test_a_score_with_a_digit_group_underscore_is_rejected",
        ],
    },
    {
        # The score grammar written with \d, which matches every Unicode
        # decimal digit. The loader collects ASCII digits only
        # (wh-parakeet-hotword-vocab.2.7).
        "name": "score-syntax-accepts-unicode-digits",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "_SCORE_SYNTAX = re.compile(r\"[+-]?(?:[0-9]+\\.?[0-9]*|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?\")\n",
        "new": "_SCORE_SYNTAX = re.compile(r\"[+-]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][+-]?\\d+)?\")\n",
        "expect": ["test_a_score_the_loader_cannot_read_as_a_number_is_rejected"],
    },
    {
        # The overflow arm removed. A magnitude above the float32 range
        # is one strtof refuses, so the loader ends the provider on a
        # score this would accept (wh-parakeet-hotword-vocab.2.7).
        #
        # The replacement keeps the double rather than substituting
        # 0.0: a synthetic zero is caught by the underflow arm below,
        # which narrows the mutation to the one case that reaches
        # float32 as an infinity, and leaves the other four proving
        # nothing. Round 8 named that narrowness under "Notes, not
        # findings"; measured, the synthetic zero let only -1e999
        # through while keeping the double lets -1e40 and
        # -3.4028236e38 through as well.
        "name": "score-overflow-not-refused",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    try:\n"
                "        as_float32 = struct.unpack(\"<f\", struct.pack(\"<f\", value))[0]\n"
                "    except OverflowError:\n"
                "        return None\n"
                "    if math.isinf(as_float32):\n"
                "        return None\n"),
        "new": ("    try:\n"
                "        as_float32 = struct.unpack(\"<f\", struct.pack(\"<f\", value))[0]\n"
                "    except OverflowError:\n"
                "        as_float32 = value\n"),
        "expect": ["test_a_score_outside_float32_range_is_rejected"],
    },
    {
        # The underflow arm removed. A magnitude below the float32 range
        # is one strtof refuses as well, and this one reaches float32 as
        # a zero rather than an infinity
        # (wh-parakeet-hotword-vocab.2.7).
        "name": "score-underflow-not-refused",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    mantissa = re.split(\"[eE]\", value_text, maxsplit=1)[0]\n"
                "    if as_float32 == 0.0 and mantissa.strip(\"+-.0\"):\n"
                "        return None\n"),
        "new": "",
        "expect": ["test_a_score_outside_float32_range_is_rejected"],
    },
    {
        # A written zero read as an underflow. strtof converts 0 and -0
        # without complaint, so refusing them would turn boosting off for
        # a vocabulary the loader accepts
        # (wh-parakeet-hotword-vocab.2.7).
        "name": "a-written-zero-refused-as-an-underflow",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    if as_float32 == 0.0 and mantissa.strip(\"+-.0\"):\n",
        "new": "    if as_float32 == 0.0:\n",
        "expect": [
            "test_a_score_of_zero_alone_does_not_reject_a_real_vocabulary",
            "test_a_vocabulary_scored_only_zero_and_negative_zero_is_accepted",
        ],
    },
    {
        # The text-mode end of file ignored. Both loaders open the file
        # as a text stream, which on Windows ends at the first 0x1A byte:
        # a marker a text tool appended reads here as a token with no
        # score and turns boosting off for a vocabulary the loader
        # accepts, and one inside a piece cuts the loader's copy of that
        # row while the row read whole here passes
        # (wh-parakeet-hotword-vocab.2.8).
        "name": "text-mode-end-of-file-ignored",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    if os.name == \"nt\":\n"
                "        data = data.split(b\"\\x1a\", 1)[0]\n"),
        "new": "",
        "expect": [
            "test_a_vocabulary_ending_in_a_text_mode_eof_marker_is_accepted",
            "test_bytes_after_a_text_mode_eof_marker_are_not_read",
            "test_a_text_mode_eof_marker_inside_a_row_cuts_it_and_is_rejected",
            "test_tokens_after_a_text_mode_eof_marker_are_not_read",
            "test_a_character_only_after_a_text_mode_eof_marker_is_not_covered",
        ],
    },
    {
        # The cut made after decoding rather than before. Bytes after the
        # marker never reach the loader, so bytes that are not UTF-8
        # there are not a reason to refuse the file
        # (wh-parakeet-hotword-vocab.2.8).
        "name": "text-mode-end-of-file-cut-after-decoding",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    data = path.read_bytes()\n"
                "    if os.name == \"nt\":\n"
                "        data = data.split(b\"\\x1a\", 1)[0]\n"
                "    return data.decode(\"utf-8\")\n"),
        "new": ("    text = path.read_bytes().decode(\"utf-8\")\n"
                "    if os.name == \"nt\":\n"
                "        text = text.split(\"\\x1a\", 1)[0]\n"
                "    return text\n"),
        "expect": ["test_bytes_after_a_text_mode_eof_marker_are_not_read"],
    },
    {
        # The vocabulary read straight from bytes again, bypassing the
        # shared file-read rule. The engine can read tokens.txt the
        # loader's way while the vocabulary check still stops at a
        # different place than the loader does
        # (wh-parakeet-hotword-vocab.2.8).
        "name": "vocabulary-read-bypasses-the-loader-file-rule",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        text = _loader_text(vocab_path)\n",
        "new": "        text = vocab_path.read_bytes().decode(\"utf-8\")\n",
        "expect": [
            "test_a_vocabulary_ending_in_a_text_mode_eof_marker_is_accepted",
            "test_bytes_after_a_text_mode_eof_marker_are_not_read",
        ],
    },
    {
        # The tokens.txt reader read straight from bytes again, the other
        # half of the same rule. Its two callers are the vocabulary's
        # overlap check and main.py's hint character set
        # (wh-parakeet-hotword-vocab.2.8).
        "name": "tokens-read-bypasses-the-loader-file-rule",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "    for line in _loader_lines(_loader_text(path)):\n",
        "new": "    for line in _loader_lines(path.read_bytes().decode(\"utf-8\")):\n",
        "expect": [
            "test_tokens_after_a_text_mode_eof_marker_are_not_read",
            "test_a_character_only_after_a_text_mode_eof_marker_is_not_covered",
        ],
    },
    {
        # A vocabulary built for a different model accepted. The floor is
        # deliberately loose (see the crewcut comment beside it); this proves
        # the loose floor still catches the case it claims to.
        "name": "foreign-vocabulary-accepted",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        if shared * 2 < len(pieces):\n",
        "new": "        if False:\n",
        "expect": ["test_vocabulary_from_another_model_is_rejected"],
    },
    {
        # A missing file no longer refused. This is the state of every
        # installation today, so it is the case the degrade path exists for.
        "name": "missing-vocabulary-accepted",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    except FileNotFoundError:\n"
                "        return f\"the vocabulary file is missing: {vocab_path}\"\n"),
        "new": ("    except FileNotFoundError:\n"
                "        return None\n"),
        "expect": ["test_missing_vocab_degrades_and_says_so"],
    },
    {
        # A missing hotwords file no longer refused, so the runtime path the
        # engine is handed is passed to sherpa unchecked.
        "name": "missing-hotwords-file-accepted",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": ("    try:\n"
                "        if not hotwords_path.exists():\n"
                "            return f\"the hotwords file is missing: {hotwords_path}\"\n"),
        "new": ("    try:\n"
                "        if not hotwords_path.exists():\n"
                "            return None\n"),
        "expect": [
            "test_missing_hotwords_file_degrades_to_no_hotwords",
            "test_missing_hotwords_file_reports_requested_not_active",
        ],
    },

    # ---- the degrade must be complete, not partial -----------------------
    {
        # Half a degrade: the beam-search decoder and modeling_unit stay
        # while the vocabulary goes. This is worse than the bug, because
        # sherpa access-violates on modeling_unit='bpe' with no bpe_vocab.
        "name": "degrade-keeps-bpe-modeling-unit",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "                self.hotwords_status = HotwordsStatus(\n                    requested=True, active=False, detail=reason\n                )\n",
        "new": "                hotwords_kwargs = {\n                    \"modeling_unit\": \"bpe\",\n                    \"decoding_method\": \"modified_beam_search\",\n                }\n                self.hotwords_status = HotwordsStatus(\n                    requested=True, active=False, detail=reason\n                )\n",
        "expect": [
            "test_missing_vocab_degrades_and_says_so",
            "test_tokens_txt_as_the_vocabulary_is_rejected",
            "test_missing_hotwords_file_degrades_to_no_hotwords",
        ],
    },

    # ---- the status must be recorded on every path -----------------------
    {
        # A rejected vocabulary reported as active. This is the exact lie
        # the criterion exists to stop.
        "name": "rejection-reported-as-active",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "                self.hotwords_status = HotwordsStatus(\n                    requested=True, active=False, detail=reason\n                )\n",
        "new": "                self.hotwords_status = HotwordsStatus(\n                    requested=True, active=True, detail=reason\n                )\n",
        "expect": [
            "test_missing_vocab_degrades_and_says_so",
            "test_tokens_txt_as_the_vocabulary_is_rejected",
            "test_vocabulary_from_another_model_is_rejected",
            "test_one_column_vocabulary_is_rejected",
            "test_empty_vocabulary_is_rejected",
            "test_missing_hotwords_file_reports_requested_not_active",
        ],
    },
    {
        # The reason dropped. The status still says boosting is off, but
        # the user is given nothing to act on.
        "name": "rejection-reason-dropped",
        "service": PARAKEET,
        "test_file": (HOTWORD_TESTS, READINESS_TESTS),
        "file": ENGINE,
        "old": "                    requested=True, active=False, detail=reason\n",
        "new": '                    requested=True, active=False, detail=""\n',
        "expect": [
            "test_missing_vocab_degrades_and_says_so",
            "test_tokens_txt_as_the_vocabulary_is_rejected",
        ],
    },
    {
        # A run with no hints at all reported as boosting requested, which
        # would put a notice in front of every user who set no hints.
        "name": "no-hints-reported-as-requested",
        "service": PARAKEET,
        "test_file": HOTWORD_TESTS,
        "file": ENGINE,
        "old": "        if not hotwords_file:\n            self.hotwords_status = HotwordsStatus()\n",
        "new": "        if not hotwords_file:\n            self.hotwords_status = HotwordsStatus(requested=True)\n",
        "expect": ["test_no_hotwords_requested_reports_neither"],
    },

    # ---- the notice the user actually sees -------------------------------
    {
        # The notice removed: the ready notification says the same thing
        # whether boosting works or not, which is the state before this
        # branch.
        "name": "notice-not-appended",
        "service": PARAKEET,
        "test_file": READINESS_TESTS,
        "file": MAIN,
        # The origin/dev merge (wh-ready-connection-stamp.2.2.1)
        # added kind="ready" to this call, so the old pattern, which
        # closed the call on this line, stopped matching. The
        # behaviour the mutation breaks is unchanged: drop the
        # boosting sentence from the ready notice.
        "old": '                    "Transcription service ready" + self._hotwords_notice(),\n',
        "new": '                    "Transcription service ready",\n',
        "expect": [
            "test_a_rejected_vocabulary_says_boosting_is_off",
            "test_the_reason_reaches_the_user",
            "test_working_boosting_says_so",
        ],
    },
    {
        # Boosting always reported on. The user is told the feature works
        # in exactly the case where it does not.
        "name": "notice-always-says-on",
        "service": PARAKEET,
        "test_file": READINESS_TESTS,
        "file": MAIN,
        "old": '        if getattr(status, "active", False) is True:\n            return " Word boosting is on."\n',
        "new": '        return " Word boosting is on."\n',
        # The mutation sits below the requested check, so a run with no
        # hints and a run with no status attribute still return "" and
        # still pass. Naming them here made the first sweep report a
        # survivor for a mutation three tests had caught.
        "expect": [
            "test_a_rejected_vocabulary_is_never_reported_as_boosting_on",
            "test_a_rejected_vocabulary_says_boosting_is_off",
            "test_the_reason_reaches_the_user",
        ],
    },
    {
        # The reason dropped from the notice. The user learns boosting is
        # off and nothing about why.
        "name": "notice-drops-the-reason",
        "service": PARAKEET,
        "test_file": READINESS_TESTS,
        "file": MAIN,
        "old": '        return f" Word boosting is off: {detail}."\n',
        "new": '        return " Word boosting is off."\n',
        "expect": ["test_the_reason_reaches_the_user"],
    },
    {
        # The `is True` reads relaxed to truthiness. An engine object that
        # answers every attribute with something truthy -- which is what a
        # test double does -- then reports boosting on.
        "name": "notice-truthy-not-is-true",
        "service": PARAKEET,
        "test_file": READINESS_TESTS,
        "file": MAIN,
        "old": '        if getattr(status, "requested", False) is not True:\n            return ""\n        if getattr(status, "active", False) is True:\n',
        "new": '        if not getattr(status, "requested", False):\n            return ""\n        if getattr(status, "active", False):\n',
        # A bare object() cannot catch this one: getattr returns None for
        # the status, and None's missing attributes read as False under
        # both `is not True` and plain truthiness. The catcher is the
        # older readiness test, whose fixture engine is a MagicMock --
        # every attribute of a MagicMock is truthy, so under truthiness
        # the notice grows " Word boosting is on." and its equality
        # assertion on the notice text fails.
        "expect": ["test_a_ready_capture_gets_the_ready_notice"],
    },
    # ---- delivery: the installer puts the vocabulary beside the model ----
    #
    # A correct vocabulary in the repository does nothing until it reaches
    # the model directory on a user's machine, and the expensive way to get
    # it there is to re-download 650 MB. These mutations break the delivery
    # and the protection around it.
    {
        # The trap this design exists to avoid, in one line: make the
        # completeness check require the vocabulary. Every model installed
        # before this release then reads as 'incomplete', and the
        # incomplete path deletes the tree and downloads the model again.
        "name": "vocabulary-required-for-completeness",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '    $tokens = Get-ModelFileState -Path (Join-Path $ModelDir "tokens.txt")\n',
        "new": '    $tokens = Get-ModelFileState -Path (Join-Path $ModelDir "bpe.vocab")\n',
        # The second name is the one that costs money: it proves the
        # already-installed machine reached a download.
        "expect": [
            "test_the_vocabulary_is_never_required_for_model_completeness",
            "test_an_already_complete_model_gains_the_vocabulary_without_downloading",
        ],
    },
    {
        # The call site removed. Everything else is correct and no
        # installation ever gets the file. Assigning the variable keeps the
        # branch non-empty, so the mutant still parses.
        "name": "vocabulary-never-installed",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '        Install-ModelVocabulary -ModelDir (Join-Path $ModelsDir $ModelDirName)\n',
        "new": '        $null = $ModelDirName\n',
        "expect": ["test_the_install_flow_delivers_the_vocabulary_after_the_model"],
    },
    {
        # The copy replaced by a text-mode write. This is the realistic
        # slip, not a contrived one: Set-Content is the obvious PowerShell
        # way to write a text file, and on Windows PowerShell 5.1 it adds a
        # BOM and turns every LF into CRLF. The file still looks right in
        # an editor and no longer matches the checksum recorded for it.
        "name": "vocabulary-written-as-text",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '        Copy-Item -LiteralPath $source -Destination $staging -Force -ErrorAction Stop\n',
        "new": '        Set-Content -LiteralPath $staging -Value ([System.IO.File]::ReadAllLines($source)) -Encoding UTF8\n',
        "expect": [
            "test_an_lf_vocabulary_is_not_translated_to_crlf",
            "test_the_vocabulary_reaches_a_model_directory_that_lacks_it",
        ],
    },
    {
        # The sidecar check dropped. Item 1 of the vendoring discipline is
        # that the sidecar is verified before the file is used; without it
        # any bytes sitting at that path are installed as a vocabulary.
        "name": "sidecar-not-verified",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '    if (-not $expected -or -not $actual -or ($actual -ine $expected)) {\n',
        "new": '    if ($false) {\n',
        "expect": ["test_a_vocabulary_that_fails_its_sidecar_is_not_installed"],
    },
    {
        # The up-to-date check widened to always match, so an installation
        # carrying an older vocabulary keeps it through every upgrade.
        "name": "stale-vocabulary-kept",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '    if ((Get-FileSha256IfReadable -Path $destination) -ieq $actual) {\n',
        "new": '    if ($true) {\n',
        "expect": [
            "test_a_stale_vocabulary_is_replaced",
            "test_the_vocabulary_reaches_a_model_directory_that_lacks_it",
        ],
    },
    {
        # A missing vocabulary turned into a stop. Hint boosting is an
        # enhancement; refusing to install because an optional file is
        # absent costs the user the whole application.
        "name": "missing-vocabulary-stops-the-install",
        "service": RELEASE,
        "test_file": INSTALLER_TESTS,
        "file": INSTALLER,
        "old": '        Write-Warn "The spoken-hint vocabulary is not part of this release; spoken hints will not be boosted."\n        return\n',
        "new": '        Stop-Install "The spoken-hint vocabulary is not part of this release." "Run the installer again."\n',
        "expect": ["test_a_missing_shipped_vocabulary_does_not_stop_the_install"],
    },
]


if __name__ == "__main__":
    raise SystemExit(run(MUTATIONS))
