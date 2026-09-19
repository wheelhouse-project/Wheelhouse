"""Mutation gate for the shared ITN stage (wh-shared-itn-rules-module).

The ITN tables were written before the rules and were watched failing
against a pass-through stub, so every conversion case is proven. The
command-safety cases are the ones a stub cannot prove: "backspace one"
stays "backspace one" whether or not the rule exists. This gate supplies
that proof by breaking each guard and requiring the table to go red.

The proof is per guard, not per row, and the gate checks only that a
named test goes red -- it does not compute a per-row killability
matrix. A guard is proven by whichever sibling row exercises it, not
by every row that reads as if it did (wh-shared-itn-review.4). The
lone-number guard is carried by "backspace one" (M01); the "oh" guard
by "oh five" (M01, the run refuses as a cardinal) and by "oh two
three" (M04, three digit words would read as a sequence); the "point"
guard by "the three point plan" (M11). Two rows that no mutation
could ever change -- ("oh no") and ("that is the point") -- were
deleted for that reason. The three pass-through canaries ("", "hello
world", "no numbers here") are equally unkillable and are kept on
purpose as regression canaries.

One hundred and four mutations, all inside
shared_stt/transcript_rules.py.
Each one loosens a real guard rather than deleting a line a later
branch would catch anyway.

Round-2 note (wh-shared-itn-review.6, .7, .8): four of the new
mutations do not delete a refusal, they shorten its reach -- M23 and
M24 stop a refused money or decimal phrase at its anchor instead of at
the end of its tail, which is exactly the half-conversion those
findings reported. A mutation that only deleted the refusal would be
caught by the same rows without proving the reach.

Round-3 note (wh-shared-itn-review.10, .11): M31 and M32 shorten the
reach the same way at a chained "point" -- M31 stops the refusal
before the decimal, M32 lets only the first "point" join it. M36 is
the counterpart of M35: M35 proves the "and"-before-a-scale refusal
exists, M36 proves it covers the whole phrase, because a run that
stopped at the "and" instead would type "100 and thousand dollars".

Round-4 note (wh-shared-itn-review.12, .13): M37 and M38 attack the one
shared fractional-tail scanner from both sides -- M37 stops the tail at
the first number word the fraction cannot read (which is what accepted
"three point five hundred dollars" as "3.5 $100"), and M38 takes the
leading "oh" back out of the tail (which is what stopped a chained
refusal in front of "oh five dollars"). M39 removes the conjunction
refusal, and M40 and M41 shorten its reach the way M23/M24 shorten an
anchored one: M40 drops the article the connector steps over, M41 stops
it after one hop.

Two older mutations needed work in the same round, and neither was
stale in the sense of a guard that went away. M11's pattern moved when
the decimal grammar started reading its fraction from _fraction_after,
so the pattern was refreshed against the new lines rather than dropped.
M36 was masked: the conjunction refusal covers the same span its
catcher used, so "one hundred and thousand dollars" stays words with
the run's "and" admitted or not, and the mutation read as a survivor
with its guard still doing work. Its catcher now uses an input only
_collect_run's admission handles -- "five dollars and one hundred and
thousand cents", where the cents tail collects its own run and the
conjunction refusal never sees the connector.

Round-6 note (wh-shared-itn-review.14, .15). Rounds 2 to 5 each filed
one shape of a single defect, so the fix replaced every shape-specific
refusal reach with one rule: a refusal absorbs the adjacent number-ish
vocabulary until punctuation or any other word (David's ruling,
2026-08-25). The mutations follow the code:

- M42 is the loop itself (absorb one continuation only), M43, M49 and
  M41 are its three call sites (the anchored refusal, the over-long
  run, the connector), and M31, M32, M44, M45, M46, M47 and M48 take
  the vocabulary apart one entry at a time. Each one reproduces a
  half-conversion the review reported, which is what the reach
  mutations M23, M24, M31 and M32 did before.
- Four older mutations were re-pointed rather than dropped, because
  absorption REPAIRS their old mutants: shortening one shape's reach no
  longer changes an output when the absorption loop reaches the same
  place anyway. M23 and M24 now delete the money and decimal claims
  themselves (the phrase half-converts, which is what they always
  proved); M31 and M32 moved onto the vocabulary, where "stops before
  an adjacent point" and "drops the number words behind it" are the
  same two half-conversions; M41 moved from the connector's own loop,
  which absorption replaced, onto the connector's absorption call.
- M21, M22, M39 and M40 are unchanged guards whose patterns moved when
  _refused_anchor_end became _anchor_refuses, _implied_cents_end became
  _implied_cents_follows, and _connector_refusal_end became
  _connector_refuses.
- M50 is new and pins the one anchor that is also an ordinary word: a
  "point" with nothing numeric behind it claims nothing, so "twenty one
  point plan" still reads as "21 point plan" (wh-shared-itn-review.8).
- M28 needed a new catcher, and it is the one mutation the ruling took
  work away from. Its old catcher ended in a dollar anchor, and a
  dollar anchor now refuses on its own, so the over-long run stayed
  words with the cap deleted and the mutant read as a survivor with its
  guard still doing work -- the same masking M36 hit in round 4. The
  catcher is now a 5000-word run with NO anchor behind it, which only
  the cap refuses: the digit-sequence rule joins strings and never
  converts an int, so nothing else stops it typing 5000 digits.

Round-7 note (wh-shared-itn-review.16). The fall-through at the bottom
of _match_number_phrase was the one refusal-class exit still ending at
its own run, so an ambiguous head such as "twelve thirty" was skipped
and the "oh five pm" behind it converted alone. M51 is that revert --
it puts `return None, end` back on the fall-through -- and only the
round-7 rows catch it, because no row before them could tell the two
endpoints apart.

M52 is the other side of the same line and the reason M51 is not the
whole proof: reverting a fix shows the tests notice it is gone, and a
mutation of what the fixed code READS shows they notice it going too
far. Absorption is for refusals only, so the empty run -- a token that
started no phrase at all -- is still stepped over singly. Absorbing it
too would walk "oh five pm" whole and leave it as words, so the
("oh five pm", "oh 5 PM") row is what holds that boundary open. Nothing
pinned that exit before this round.

M19 was re-pointed, not weakened. It has always mutated the same
fall-through into a one-token rescan, but its pattern spelled the two
lines `return cardinal, end` and `return None, end` together, and the
fix rewrote the second of them. Its pattern now matches the fall-through
line alone; the leading newline is load-bearing, because without it the
text is also a substring of the three indented absorption calls above
and the gate would report the pattern as ambiguous.

Phase-4 note (wh-shared-itn-addresses-dates): M53 to M74 cover the
three pieces of new parsing logic, and each one reproduces a wrong
conversion rather than deleting a rule a later branch would catch.

- Rule 6, the paired house number (M53 to M60). M53 unwires it and M54
  drops the street-suffix licence, which is the guard the whole rule
  rests on: without it "twelve thirty four" reads as 1234 and the
  command-safety table goes red beside the house table. M55 widens the
  reach from one word to three, so a long street name converts; M56
  lets a number-ish word stand between the number and the suffix, which
  is the "twelve thirty oh street" shape; M60 adds a seventh word to
  the closed suffix list.
- The three chunk guards of rule 6 need three different rows, because
  they defend in depth and no single row proves them all. M57 lets a
  ONE-chunk run read as a pair, which only ("fifty main street")
  catches: 50 passes both chunk bounds, so the lone-number rows already
  in the table ("one court", "backspace one street") are refused by the
  trailing-chunk bound whether the pair guard is there or not. M58
  drops the leading bound and is caught by the longer ambiguous run,
  M59 lets a one-digit trailing chunk through and is caught by "twelve
  four main street".
- Rule 7, dates (M61 to M68). M61 unwires it; M62 drops the month
  anchor, which is what keeps a bare ordinal a word ("press third");
  M63 stops punctuation detaching the anchor; M64 removes the 1-to-31
  day cap; M65 and M66 take the written suffix apart, one on the
  11th/12th/13th exception and one on the lookup's default; M67 stops
  the two-token ordinal being read at all and M68 reads it too far,
  taking a tens word plus any word as a day, which is what the
  ("january twenty five") row holds open.
- M63, M64 and M68 each need a row anchored on a month that is still IN
  the table. The month-anchor check stands in front of both the ordinal
  read and the day cap, so a row anchored on an EXCLUDED month is
  refused before the mutated code runs and can never catch them. The
  four rows that hold them open are therefore ("january, third"),
  ("january thirty second"), ("january forty first") and ("january
  twenty five"); their march counterparts held them open only while
  march was an anchor and are inert for these three mutations now.
- The three-month exclusion (M74). "may", "march" and "august" are
  deliberately absent from _ITN_MONTHS, because each is an ordinary
  English word and as an anchor it converted ordinary dictation
  wrongly (David ruling, 2026-08-25). M74 puts all three back, which
  is the exact regression a later editor "completing" the month table
  would cause, and the three refusal rows catch it -- ("you may first
  check the log"), ("we march third in the parade") and ("the august
  first edition"). The mixed row ("meet me on march third at eleven
  fifteen pm") catches it too, from the other side: under M74 its date
  head converts and only its clock tail is supposed to.
- The hyphen split (M69 to M73). M69 turns it off. M70 splits a token
  whose pieces are NOT all number words, which is what would take
  "twenty-five-year-old" apart. The remaining three prove the split is
  lossless: M71 and M72 drop the token's trailing and leading
  punctuation, and M73 stops the hyphen being restored as the separator
  between the pieces, which is what ("backspace one-two") catches -- a
  split token that no rule converts has to be rebuilt character for
  character.

Phase-4 review note (wh-itn-phase4-review.1, .2). M75 to M79 cover the
two rule-7 shapes that review found, and each one needs the new code to
die: no row before this round carried a hyphenated ordinal or a date
range, so no older mutation reaches either path.

- The hyphenated ordinal (M75, M76). M75 stops the compound being read
  at all, which is exactly the phase-4 state ("january twenty-fifth"
  stayed words while "january twenty fifth" converted). M76 is the
  guard side: it lets any head stand in front of a unit ordinal, and
  ("january one-third") catches it, because an ordinary hyphenated
  fraction is not the third of january. The remaining guards of the
  compound are the caller's and already have mutations -- M62 the month
  anchor, M64 the day cap -- and the new rows now catch those too.
- The date range (M77, M78, M79). M77 is the revert: the range refusal
  goes away and ("january third and fourth") half-converts to "january
  3rd and fourth", the mixed output the finding reported. M78 is the
  other side of the same line, the way M52 is the other side of M51 --
  it refuses on ANY "and", and ("january third and we left") holds that
  boundary open, because ordinary speech behind a date still converts.
  M79 drops the adjacency from the shape, so punctuation stops ending
  the span, and ("january third, and fourth") catches it. It keeps a
  bounds check the mutant would otherwise crash on: a mutation must
  reproduce a wrong conversion, not an exception. Its pattern is why
  _date_range_end opens on "not adjacent or not and" while
  _connector_refuses opens on "not (adjacent and and)" -- written the
  same way, the two lines are one string that this gate would report as
  ambiguous, and a pattern that spelled out a whole docstring to
  disambiguate would break on the next wording change.

Phase-4 review note (wh-itn-phase4-review.3). Rule 7's two post-read
refusals -- the day cap and the range -- returned a bare None, which
apply_itn reads as "no date here", so it rescanned the words behind the
refused span and converted the number phrase touching it: "january
third and fourth five dollars" typed "january third and fourth $5". Both
now end at _absorbed_refusal_end, the endpoint every other refusal in
this module already used, and _date_range_follows became
_date_range_end because a refusal needs the end of what it refused.

- M80 and M81 are the reverts, one per exit: each puts `return None`
  back and the tail converts again. They are the "reverts an absorption
  exit to bare None" proof, and only the new tail rows catch them --
  the standalone rows above ("january thirty second", "january third
  and fourth") stay words with or without the absorption, which is
  exactly why they could not report this defect.
- M82 is the reach mutation of the pair, in the tradition of M23, M24,
  M31 and M32: the range refusal absorbs, but from the FIRST day's
  endpoint. Absorption then stops at the second ordinal ("fourth" is
  not number-ish vocabulary), the scanner resumes on it, and the tail
  converts anyway. It proves the endpoint _date_range_end returns is
  load-bearing, not just the call to _absorbed_refusal_end.
- M64 and M77 to M79 kept their guards and moved their patterns onto
  the rewritten lines. M64's new pattern is the same two lines M80
  mutates -- the day-cap `if` disambiguates a return line that appears
  four times in this module -- and the two differ in what they do with
  it: M64 deletes the cap, M80 keeps the cap and takes the absorption.

Phase-4 review note (wh-itn-phase4-review.4). Both rule-7 refusals read
ONE "and <ordinal>" hop, so a spoken list of three days ended its
refusal at the second one and the number phrase behind it converted:
"january third and fourth and fifth five dollars" typed "... $5". The
day-cap exit was worse -- it answered before the range was read at all,
so a single continuation leaked the tail. Both now start from
_date_chain_end, which walks the pairs until they stop.

- M83 is the revert, and it is the reach mutation of this round in the
  tradition of M23, M24, M31, M32 and M82: the chain walk stays, but
  takes one hop and returns. The two-day rows cannot see it -- one hop
  is all they ever needed -- so it is caught only by the three-day rows
  ("january third and fourth and fifth five dollars") and by the capped
  day with two continuations behind it.
- M84 is the day-cap side, which no revert of the chain walk itself can
  reproduce: the cap absorbs from its OWN ordinal and steps over the
  chain standing in front of it, which is exactly the phase-4 state.
  ("january thirty second and third five dollars") catches it.
- M64, M77, M80 and M81 to M82 moved their patterns onto the rewritten
  lines again. M77 changed shape with them: the range branch is no
  longer a separate _date_range_end call it could blank, so it now
  turns `chain_end != end` into `chain_end < end`, which the walk can
  never satisfy -- the same "no range stands here" answer by a different
  route, and ("january third and fourth") still catches it.

Thousands-separator note (wh-itn-thousands-commas). M85 to M97 cover the
one rule of the round: a QUANTITY takes comma grouping from five digits
up, an IDENTIFIER never does. Each half needs its own mutations, because
the two fail in opposite directions -- a quantity written bare and an
identifier written grouped are both wrong, and no single mutation
reproduces both.

- The quantity side (M85, M89 to M92). One per write site: the cardinal
  rule, a dollar amount with and without cents, a cents amount, and the
  whole half of a decimal. Each writes its value through _write_number
  and each mutation takes that call back out, which is the pre-change
  behavior at that one site.
- The threshold, from both sides (M86, M87). M86 lowers it to 1000 and
  is caught by the rows the threshold exists for -- ("two thousand
  five") and ("nine thousand nine hundred ninety nine"), which are 2005
  and 9999 and must stay bare. M87 raises it to 100000 and is caught by
  ("ten thousand") and ("fifteen thousand"). Neither side can catch the
  other, which is why the pair is not one mutation.
- Comma PLACEMENT (M88). The separator survives but goes in only once,
  before the last three digits, so 1000000 writes as "1000,000". A
  five-digit value is unchanged by it -- 15000 has one separator either
  way -- so only the ("one million") rows catch it. It is the mutation
  that proves the format spec is doing real work rather than a single
  hand-placed comma.
- The identifier side (M93 to M96). M94 drops the _scale_built guard, so
  the spelled-out amount behind a unit anchor groups and ("nine oh two
  one oh dollars") types "$90,210" instead of "$90210". M95 groups
  rule 5's own digit sequence, which ("zero zero seven") catches from the
  other direction: the int conversion eats the leading zeros. M93 groups
  the fractional half of a decimal, which ("fifteen thousand point one
  two three four five") catches as "15,000.12,345" and ("three point oh
  five") catches as "3.5". M96 lets an address number group. M97
  narrows the bare-address reach to rule 6's licensing reach, so a
  multi-word street name ("east main street", "maple grove road")
  groups again -- the review finding wh-itn-thousands-commas.1.1.
- M07, M10, M11 and M18 kept their guards and moved their patterns onto
  the rewritten lines: the two money returns and the decimal return now
  write through _write_number, and _try_cardinal takes the records and
  the endpoint so it can see a street suffix. M11's two lines had to
  stay adjacent, which is why the comment about grouping in _try_decimal
  sits above the _fraction_after call rather than between them.

Letter-alias note (wh-itn-oh-letter-alias). The recognizers emit the
LETTER "o" where a speaker said the spoken zero, so "seven point o
seven" half-converted to "seven point o $7". The fix adds one alias set,
_ITN_OH_WORDS, and reads it at five sites; M98 to M104 are one mutation
per way the alias can be got wrong.

- Removed at a site (M98 to M102). M98 takes the letter out of the
  vocabulary, which turns every alias row back into its pre-fix output.
  M99 leaves the set alone and gives only the interjection the zero
  VALUE, which is the half-conversion shape: the run still admits the
  letter and then nothing can read it. M100, M101 and M102 narrow the
  three remaining sites one at a time -- the run admission, the clock
  minutes, and the absorption vocabulary -- and each reproduces a
  different one of the outputs the bead reported.
- Widened (M103). The letter must obey the same after-a-digit rule the
  interjection obeys, so M103 lets a LEADING letter start a run while
  the interjection is untouched. Only the letter rows catch it, which is
  what makes it a separate mutation from M04.
- Rewritten (M104). The alias is lookup-only: nothing turns the letter
  into the word, so a phrase that stays words keeps what the speaker
  said. M104 rewrites the token at the one place the output is rebuilt
  from, and ("o brother") catches it as "oh brother".
- M04, M32 and M46 kept their guards and moved their patterns onto the
  rewritten lines, which now read the alias set instead of the bare
  word. M04's replacement widens both spellings and is still caught by
  the command-safety table.

Deliberately NOT mutated: the _parse_cardinal refusal on a run holding a
spoken zero. Measured 2026-08-28 by building three variants of the
module in memory -- the refusal as written, narrowed to the interjection,
and deleted outright -- and comparing _parse_cardinal over nine runs
holding "o" or "oh" plus apply_itn over seven phrases: all three
variants answer identically, None for every run. Neither spelling is in
_ITN_NUMBER_WORDS, so the parser's own vocabulary walk refuses these
runs first and the explicit refusal never decides anything. It is
defense in depth against a later edit promoting a spoken zero to a
number word, and no test can distinguish it, so a mutation there would
be a permanent survivor rather than a proof.

Deliberately NOT mutated: moving _try_house_number ahead of
_try_cardinal. That mutant SURVIVES, and it survives for the reason the
fall-through placement was chosen -- the house rule declines every run
the cardinal rule reads, so the order cannot be observed from behavior.
The placement is a boss ruling recorded in the code, not a behavior a
mutation can pin.

The gate reports pattern-not-found, pattern-ambiguous, does-not-compile,
timeout, and a missing expected test name as ERRORS, never as survivors.
It restores the source with write_bytes inside a finally block, so
Windows cannot rewrite the line endings.

The patterns are written with "\n" and are translated to the target's
own line ending before matching. An editor that rewrites the whole file
as CRLF -- which is what happened to transcript_rules.py on 2026-08-28 --
otherwise makes every multi-line pattern miss at once: all ninety-seven
patterns of the day reported as not-found in one batch. The gate never
rewrites the target's endings to suit itself.

Run it from anywhere; it finds its own service directory:
    uv run --no-project --with pytest python tests/itn_mutation_gate.py
or, when the service venv exists:
    .venv/Scripts/python.exe tests/itn_mutation_gate.py

Any arguments are name fragments, and only the mutations whose names
contain one of them run:
    .venv/Scripts/python.exe tests/itn_mutation_gate.py M98 M99
A filtered run says so in its scope line and is not a full sweep. Run
the whole set once before the final commit of a piece of work.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
TARGET = SERVICE / "shared_stt" / "transcript_rules.py"

_VENV_PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

SELECTION = ["tests/test_transcript_rules.py"]

# (name, pattern, replacement, expected failing tests)
MUTATIONS = [
    (
        "M01-a-lone-number-word-converts",
        "    if len(words) < 2:\n        return None\n",
        "    if len(words) < 1:\n        return None\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M02-an-ambiguous-phrase-converts-to-its-first-number",
        "    if values is None or len(values) != 1:\n        return None\n",
        "    if values is None or len(values) < 1:\n        return None\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M03-two-digit-words-read-as-a-sequence",
        "    if len(run) < 3 or any(word not in _ITN_DIGITS for word in run):\n",
        "    if len(run) < 2 or any(word not in _ITN_DIGITS for word in run):\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M04-the-interjection-oh-starts-a-number-phrase",
        '        elif (\n'
        "            low in _ITN_OH_WORDS\n"
        "            and run\n"
        "            and (run[-1] in _ITN_DIGITS or run[-1] in _ITN_TEENS)\n"
        "        ):\n",
        "        elif low in _ITN_OH_WORDS:\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M05-an-hour-outside-1-to-12-still-reads-as-a-time",
        "    if hour is None or not 1 <= hour <= 12:\n        return None\n",
        "    if hour is None:\n        return None\n",
        ["test_clock_times"],
    ),
    (
        "M06-minutes-lose-the-leading-zero",
        '        return f"{_ITN_UNITS[words[1]]:02d}"\n',
        '        return f"{_ITN_UNITS[words[1]]}"\n',
        ["test_clock_times"],
    ),
    (
        "M07-cents-lose-the-leading-zero",
        '        return f"${_write_number(run, value)}.{amount:02d}", end\n',
        '        return f"${_write_number(run, value)}.{amount}", end\n',
        ["test_money_phrases"],
    ),
    (
        "M08-more-than-99-cents-becomes-a-cents-part",
        "    if value is None or not 0 <= value <= 99:\n        return None\n",
        "    if value is None:\n        return None\n",
        ["test_money_phrases"],
    ),
    (
        "M09-the-period-is-not-uppercased",
        '    return f"{hour}:{minutes} {period.upper()}", anchor + 1\n',
        '    return f"{hour}:{minutes} {period}", anchor + 1\n',
        ["test_clock_times"],
    ),
    (
        "M10-money-loses-the-dollar-sign",
        '            return f"${_write_number(run, value)}", anchor + 1\n',
        '            return f"{_write_number(run, value)}", anchor + 1\n',
        ["test_money_phrases"],
    ),
    (
        "M11-point-with-no-digits-after-it-still-converts",
        "    if digits is None:\n        return None\n"
        '    return f"{_write_number(run, whole)}.{digits}", end\n',
        "    if digits is None:\n        pass\n"
        '    return f"{_write_number(run, whole)}.{digits}", end\n',
        ["test_decimals"],
    ),
    (
        "M12-a-phrase-runs-through-punctuation",
        "        if index > start and (token.lead or records[index - 1].trail):\n",
        "        if index > start and token.lead:\n",
        ["test_surrounding_text_is_preserved"],
    ),
    (
        "M13-a-unit-anchor-is-read-across-punctuation",
        '        and records[index - 1].trail == ""\n',
        "        and True\n",
        ["test_surrounding_text_is_preserved"],
    ),
    (
        "M14-consecutive-units-add-instead-of-starting-a-new-number",
        "        elif word in _ITN_UNITS:\n"
        "            if has_unit:\n                flush()\n",
        "        elif word in _ITN_UNITS:\n"
        "            if False:\n                flush()\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M15-consecutive-tens-add-instead-of-starting-a-new-number",
        "        if word in _ITN_TENS:\n"
        "            if has_ten or has_unit:\n                flush()\n",
        "        if word in _ITN_TENS:\n"
        "            if has_unit:\n                flush()\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M16-the-anchored-rules-never-run",
        "    anchor = end if _adjacent(records, end) else None\n",
        "    anchor = None\n",
        ["test_money_phrases", "test_clock_times", "test_decimals"],
    ),
    (
        "M17-the-digit-sequence-rule-is-not-wired-in",
        "    digits = _try_digit_sequence(run)\n",
        "    digits = None\n",
        ["test_digit_sequences"],
    ),
    (
        "M18-the-cardinal-rule-is-not-wired-in",
        "    cardinal = _try_cardinal(records, run, end)\n",
        "    cardinal = None\n",
        ["test_cardinal_phrases"],
    ),
    (
        "M19-a-refused-phrase-is-rescanned-one-word-deeper",
        "\n    return None, _absorbed_refusal_end(records, end)\n",
        "\n    return None, start + 1\n",
        ["test_command_words_stay_words"],
    ),
    (
        "M20-the-oh-gate-narrows-back-to-units-so-teen-hours-break",
        "            and (run[-1] in _ITN_DIGITS or run[-1] in _ITN_TEENS)\n",
        "            and run[-1] in _ITN_DIGITS\n",
        ["test_clock_times"],
    ),
    (
        "M21-an-ampm-run-the-clock-rule-refused-converts-anyway",
        "    if _period_word(records[anchor]) is not None:\n"
        "        return True\n",
        "    if _period_word(records[anchor]) is not None:\n"
        "        return False\n",
        ["test_clock_times"],
    ),
    (
        "M22-implied-cents-half-convert-instead-of-staying-words",
        "            if _implied_cents_follows(records, anchor + 1):\n"
        "                return None\n",
        "            if False:\n                return None\n",
        ["test_money_phrases"],
    ),
    (
        "M23-a-refused-money-anchor-lets-its-tail-half-convert",
        "    if word in _DOLLAR_WORDS or word in _CENT_WORDS:\n"
        "        return True\n",
        "    if word in _DOLLAR_WORDS or word in _CENT_WORDS:\n"
        "        return False\n",
        ["test_money_phrases"],
    ),
    (
        "M24-a-refused-decimal-lets-its-fraction-half-convert",
        '    if word == "point":\n'
        "        return _refused_decimal_follows(records, anchor)\n",
        '    if word == "point":\n        return False\n',
        ["test_decimals"],
    ),
    (
        "M25-an-ascending-or-repeated-scale-is-read-as-a-number",
        "                if last_scale and scale >= last_scale:\n"
        "                    return None\n",
        "                if False:\n                    return None\n",
        ["test_scale_sequences"],
    ),
    (
        "M26-a-second-hundred-in-one-group-is-read-as-a-number",
        "                if has_hundred:\n                    return None\n",
        "                if False:\n                    return None\n",
        ["test_scale_sequences"],
    ),
    (
        "M27-the-run-length-cap-never-refuses",
        "    return len(words) <= _ITN_MAX_RUN_WORDS\n",
        "    return True\n",
        [
            "test_a_long_digit_run_under_a_unit_anchor_is_returned_unchanged",
            "test_a_long_cents_tail_leaves_its_words_alone",
        ],
    ),
    (
        "M28-an-over-long-run-reaches-the-rules-instead-of-being-refused",
        "    if not _within_length_cap(run):\n",
        "    if False:\n",
        [
            "test_a_long_digit_run_with_no_anchor_is_returned_unchanged",
            "test_a_long_digit_run_under_a_unit_anchor_is_returned_unchanged",
        ],
    ),
    (
        "M29-the-cardinal-parser-reads-an-over-long-run-as-numbers",
        "    if not _within_length_cap(words):\n        return None\n",
        "    if False:\n        return None\n",
        ["test_the_cardinal_parser_refuses_an_over_long_run"],
    ),
    (
        "M30-a-long-cents-tail-reaches-the-spelled-out-amount-fallback",
        "    if not words or not _within_length_cap(words):\n",
        "    if not words:\n",
        ["test_a_long_cents_tail_leaves_its_words_alone"],
    ),
    (
        "M31-a-refused-span-stops-before-an-adjacent-point",
        '    if low == "point":\n        return True\n',
        "    if False:\n        return True\n",
        ["test_money_phrases"],
    ),
    (
        "M32-an-absorbed-span-drops-the-number-words-behind-it",
        "    if low in _ITN_NUMBER_WORDS or low in _ITN_OH_WORDS:\n"
        "        return True\n",
        "    if low in _ITN_OH_WORDS:\n        return True\n",
        ["test_money_phrases"],
    ),
    (
        "M33-an-explicit-zero-scales-as-though-it-were-absent",
        '    if "zero" in words and any(word in _ITN_SCALES for word in words):\n'
        "        return None\n",
        "    if False:\n        return None\n",
        ["test_scale_sequences"],
    ),
    (
        "M34-a-scale-with-an-empty-coefficient-group-is-read-as-one",
        "            if started and not has_group:\n"
        "                return None\n",
        "            if False:\n                return None\n",
        ["test_scale_sequences"],
    ),
    (
        "M35-an-and-directly-before-a-scale-is-read-as-grammar",
        '        if word == "and" and run[index + 1] in _ITN_SCALES:\n'
        "            return None\n",
        "        if False:\n            return None\n",
        ["test_scale_sequences"],
    ),
    (
        "M36-the-run-stops-at-an-and-before-a-scale-and-half-converts",
        "            and records[index + 1].low in _ITN_NUMBER_WORDS\n",
        "            and records[index + 1].low in _ITN_NUMBER_WORDS\n"
        "            and records[index + 1].low not in _ITN_SCALES\n",
        ["test_scale_sequences"],
    ),
    (
        "M37-a-fractional-tail-stops-at-the-first-unreadable-number-word",
        "            readable = False\n",
        "            break\n",
        ["test_decimals"],
    ),
    (
        "M38-a-leading-oh-drops-out-of-the-fractional-tail",
        "        if low in _ITN_DIGITS:\n"
        "            digits.append(str(_ITN_DIGITS[low]))\n",
        '        if low in _ITN_DIGITS and (low != "oh" or digits):\n'
        "            digits.append(str(_ITN_DIGITS[low]))\n",
        ["test_decimals", "test_money_phrases"],
    ),
    (
        "M39-the-conjunction-refusal-is-not-wired-in",
        "    if _connector_refuses(records, end):\n",
        "    if False:\n",
        ["test_conjunction_connectors"],
    ),
    (
        "M40-the-connector-shape-does-not-step-over-an-article",
        "    if records[start].low in _ITN_ARTICLES:\n"
        "        start += 1\n"
        "        if not _adjacent(records, start):\n"
        "            return False\n",
        "    if False:\n"
        "        start += 1\n"
        "        if not _adjacent(records, start):\n"
        "            return False\n",
        ["test_conjunction_connectors"],
    ),
    (
        "M41-the-connector-refusal-does-not-absorb-what-follows",
        "    if _connector_refuses(records, end):\n"
        "        return None, _absorbed_refusal_end(records, end)\n",
        "    if _connector_refuses(records, end):\n        return None, end\n",
        ["test_conjunction_connectors"],
    ),
    (
        "M42-absorption-stops-at-the-first-continuation",
        "    index = end\n"
        "    while _adjacent(records, index):\n"
        '        if records[index].low == "and":\n',
        "    index = end\n"
        "    while _adjacent(records, index) and index == end:\n"
        '        if records[index].low == "and":\n',
        ["test_conjunction_connectors"],
    ),
    (
        "M43-a-refused-anchor-is-not-absorbed",
        "        if _anchor_refuses(records, anchor):\n"
        "            return None, _absorbed_refusal_end(records, end)\n",
        "        if _anchor_refuses(records, anchor):\n"
        "            return None, end\n",
        ["test_decimals", "test_money_phrases"],
    ),
    (
        "M44-absorption-drops-the-dollar-and-cent-anchors",
        "    if low in _DOLLAR_WORDS or low in _CENT_WORDS:\n"
        "        return True\n",
        "    if False:\n        return True\n",
        ["test_conjunction_connectors"],
    ),
    (
        "M45-absorption-drops-the-ampm-anchor",
        "    return _period_word(record) is not None\n",
        "    return False\n",
        ["test_clock_times"],
    ),
    (
        "M46-absorption-drops-the-spoken-zero-oh",
        "    if low in _ITN_NUMBER_WORDS or low in _ITN_OH_WORDS:\n"
        "        return True\n",
        "    if low in _ITN_NUMBER_WORDS:\n        return True\n",
        ["test_money_phrases"],
    ),
    (
        "M47-absorption-drops-the-connector-and",
        '        if records[index].low == "and":\n',
        "        if False:\n",
        ["test_conjunction_connectors"],
    ),
    (
        "M48-absorption-does-not-step-over-the-article",
        "            if (\n"
        "                _adjacent(records, index)\n"
        "                and records[index].low in _ITN_ARTICLES\n"
        "            ):\n"
        "                index += 1\n",
        "            if (\n"
        "                False\n"
        "                and records[index].low in _ITN_ARTICLES\n"
        "            ):\n"
        "                index += 1\n",
        ["test_conjunction_connectors"],
    ),
    (
        "M49-an-over-long-run-is-refused-without-absorbing",
        "    if not _within_length_cap(run):\n"
        "        return None, _absorbed_refusal_end(records, end)\n",
        "    if not _within_length_cap(run):\n        return None, end\n",
        ["test_an_over_long_run_absorbs_an_adjacent_amount"],
    ),
    (
        "M50-every-point-anchor-refuses-even-the-ordinary-word",
        '    if word == "point":\n'
        "        return _refused_decimal_follows(records, anchor)\n",
        '    if word == "point":\n        return True\n',
        ["test_decimals"],
    ),
    (
        "M51-an-ambiguous-fall-through-run-is-refused-without-absorbing",
        "\n    return None, _absorbed_refusal_end(records, end)\n",
        "\n    return None, end\n",
        ["test_clock_times", "test_money_phrases", "test_decimals"],
    ),
    (
        "M52-the-empty-run-exit-absorbs-as-though-it-were-a-refusal",
        "    if not run:\n        return None, start + 1\n",
        "    if not run:\n"
        "        return None, _absorbed_refusal_end(records, start + 1)\n",
        ["test_clock_times"],
    ),
    (
        "M53-the-paired-house-number-rule-is-not-wired-in",
        "    house = _try_house_number(records, run, end)\n",
        "    house = None\n",
        ["test_house_numbers"],
    ),
    (
        "M54-a-paired-number-converts-with-no-street-suffix",
        "    if not _street_suffix_near(records, end):\n        return None\n",
        "    if False:\n        return None\n",
        ["test_house_numbers", "test_command_words_stay_words"],
    ),
    (
        "M55-the-suffix-may-sit-three-words-past-the-number",
        "_STREET_SUFFIX_REACH = 1\n",
        "_STREET_SUFFIX_REACH = 3\n",
        ["test_house_numbers"],
    ),
    (
        "M56-a-number-ish-word-may-stand-between-number-and-suffix",
        '        if _number_ish(records[index]) or records[index].low == "and":\n'
        "            return False\n",
        "        if False:\n            return False\n",
        ["test_house_numbers"],
    ),
    (
        "M57-a-one-chunk-run-reads-as-a-pair",
        "    if values is None or len(values) != 2:\n        return None\n",
        "    if values is None or len(values) not in (1, 2):\n"
        "        return None\n",
        ["test_house_numbers"],
    ),
    (
        "M58-the-leading-chunk-may-be-any-size",
        "    if not 1 <= leading <= 99:\n        return None\n",
        "    if False:\n        return None\n",
        ["test_house_numbers"],
    ),
    (
        "M59-a-one-digit-trailing-chunk-still-concatenates",
        "    if not 10 <= trailing <= 99:\n        return None\n",
        "    if not 1 <= trailing <= 99:\n        return None\n",
        ["test_house_numbers"],
    ),
    (
        "M60-the-street-suffix-list-is-not-closed",
        '_STREET_SUFFIXES = {"street", "avenue", "road", "drive", "lane", "court"}\n',
        '_STREET_SUFFIXES = {"street", "avenue", "road", "drive", "lane", "court", "boulevard"}\n',
        ["test_house_numbers"],
    ),
    (
        "M61-the-date-rule-is-not-wired-in",
        "        dated = _try_date_ordinal(records, index)\n",
        "        dated = None\n",
        ["test_date_ordinals"],
    ),
    (
        "M62-an-ordinal-converts-with-no-month-anchor",
        "    if records[start - 1].low not in _ITN_MONTHS:\n        return None\n",
        "    if False:\n        return None\n",
        ["test_date_ordinals"],
    ),
    (
        "M63-punctuation-no-longer-detaches-the-month-anchor",
        "    if not _adjacent(records, start):\n"
        "        return None\n"
        "    if records[start - 1].low not in _ITN_MONTHS:\n",
        "    if False:\n"
        "        return None\n"
        "    if records[start - 1].low not in _ITN_MONTHS:\n",
        ["test_date_ordinals"],
    ),
    (
        "M64-a-day-outside-1-to-31-still-reads-as-a-date",
        "    if not 1 <= day <= _ITN_MAX_DAY:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        "    if False:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        ["test_date_ordinals"],
    ),
    (
        "M65-the-eleventh-twelfth-thirteenth-exception-is-dropped",
        '    if day % 100 in (11, 12, 13):\n        return "th"\n',
        '    if False:\n        return "th"\n',
        ["test_date_ordinals"],
    ),
    (
        "M66-the-ordinal-suffix-lookup-loses-its-default",
        '    return _ITN_ORDINAL_SUFFIXES.get(day % 10, "th")\n',
        '    return _ITN_ORDINAL_SUFFIXES.get(day % 10, "st")\n',
        ["test_date_ordinals"],
    ),
    (
        "M67-the-two-token-ordinal-is-not-read",
        "    tens = _ITN_TENS.get(low)\n",
        "    tens = None\n",
        ["test_date_ordinals"],
    ),
    (
        "M68-a-tens-word-plus-any-word-reads-as-a-day",
        "    unit = _ITN_ORDINAL_UNITS.get(records[start + 1].low)\n",
        "    unit = _ITN_ORDINAL_UNITS.get(records[start + 1].low, 0)\n",
        ["test_date_ordinals"],
    ),
    (
        "M69-the-hyphenated-number-word-never-splits",
        '    if "-" not in token.core:\n        return None\n',
        "    if True:\n        return None\n",
        ["test_hyphenated_number_words"],
    ),
    (
        "M70-a-token-splits-when-its-pieces-are-not-all-number-words",
        "    if not all(part.lower() in _ITN_NUMBER_WORDS for part in parts):\n"
        "        return None\n",
        "    if False:\n        return None\n",
        ["test_hyphenated_number_words"],
    ),
    (
        "M71-the-split-drops-the-tokens-trailing-punctuation",
        '            token.trail if position == last else "",\n',
        '            "",\n',
        ["test_hyphenated_number_words"],
    ),
    (
        "M72-the-split-drops-the-tokens-leading-punctuation",
        '            token.lead if position == 0 else "",\n',
        '            "",\n',
        ["test_hyphenated_number_words"],
    ),
    (
        "M73-the-hyphen-is-not-restored-as-the-separator",
        '            separators.append("-" if position < last else gap)\n',
        "            separators.append(gap)\n",
        ["test_hyphenated_number_words"],
    ),
    (
        "M74-the-three-excluded-months-are-put-back",
        '    "january", "february", "april", "june",\n'
        '    "july", "september", "october", "november", "december",\n',
        '    "january", "february", "march", "april", "may", "june",\n'
        '    "july", "august", "september", "october", "november", "december",\n',
        ["test_date_ordinals"],
    ),
    (
        "M75-the-hyphenated-ordinal-is-not-read",
        "    compound = _hyphenated_ordinal(low)\n",
        "    compound = None\n",
        ["test_date_ordinals"],
    ),
    (
        "M76-the-head-of-a-compound-day-need-not-be-a-tens-word",
        "    tens = _ITN_TENS.get(head)\n",
        "    tens = _ITN_TENS.get(head, 0)\n",
        ["test_date_ordinals"],
    ),
    (
        "M77-a-date-range-half-converts-instead-of-staying-words",
        "    if chain_end != end:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        "    if chain_end < end:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        ["test_date_ordinals"],
    ),
    (
        "M78-any-and-behind-a-date-refuses-it-not-only-a-second-day",
        "    return None if read is None else read[1]\n",
        "    return end + 1 if read is None else read[1]\n",
        ["test_date_ordinals"],
    ),
    (
        "M79-punctuation-no-longer-detaches-the-second-day",
        '    if not _adjacent(records, end) or records[end].low != "and":\n'
        "        return None\n",
        '    if end >= len(records) or records[end].low != "and":\n'
        "        return None\n",
        ["test_date_ordinals"],
    ),
    (
        "M80-the-day-cap-refusal-absorbs-nothing",
        "    if not 1 <= day <= _ITN_MAX_DAY:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        "    if not 1 <= day <= _ITN_MAX_DAY:\n        return None\n",
        ["test_date_ordinals"],
    ),
    (
        "M81-the-date-range-refusal-absorbs-nothing",
        "    if chain_end != end:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        "    if chain_end != end:\n        return None\n",
        ["test_date_ordinals"],
    ),
    (
        "M82-the-date-range-refusal-absorbs-from-its-first-day",
        "    if chain_end != end:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        "    if chain_end != end:\n"
        "        return None, _absorbed_refusal_end(records, end)\n",
        ["test_date_ordinals"],
    ),
    (
        "M83-the-day-chain-is-walked-one-hop-only",
        "        index = hop\n",
        "        return hop\n",
        ["test_date_ordinals"],
    ),
    (
        "M84-the-day-cap-refusal-skips-the-chain-it-stands-in-front-of",
        "    if not 1 <= day <= _ITN_MAX_DAY:\n"
        "        return None, _absorbed_refusal_end(records, chain_end)\n",
        "    if not 1 <= day <= _ITN_MAX_DAY:\n"
        "        return None, _absorbed_refusal_end(records, end)\n",
        ["test_date_ordinals"],
    ),
    (
        "M85-the-cardinal-rule-writes-its-value-ungrouped",
        "    return _write_number(run, values[0])\n",
        "    return str(values[0])\n",
        ["test_thousands_separators"],
    ),
    (
        "M86-the-grouping-threshold-drops-below-ten-thousand",
        "_ITN_GROUP_MIN = 10000\n",
        "_ITN_GROUP_MIN = 1000\n",
        ["test_thousands_separators"],
    ),
    (
        "M87-the-grouping-threshold-rises-past-ten-thousand",
        "_ITN_GROUP_MIN = 10000\n",
        "_ITN_GROUP_MIN = 100000\n",
        ["test_thousands_separators"],
    ),
    (
        "M88-only-the-last-three-digits-are-separated",
        '    return f"{value:,}"\n',
        '    return f"{str(value)[:-3]},{str(value)[-3:]}"\n',
        ["test_thousands_separators"],
    ),
    (
        "M89-a-dollar-amount-is-written-ungrouped",
        '            return f"${_write_number(run, value)}", anchor + 1\n',
        '            return f"${value}", anchor + 1\n',
        ["test_thousands_separators"],
    ),
    (
        "M90-a-dollar-amount-with-cents-is-written-ungrouped",
        '        return f"${_write_number(run, value)}.{amount:02d}", end\n',
        '        return f"${value}.{amount:02d}", end\n',
        ["test_thousands_separators"],
    ),
    (
        "M91-a-cents-amount-is-written-ungrouped",
        "        return f\"{_write_number(run, value)} {records[anchor].core}\""
        ", anchor + 1\n",
        '        return f"{value} {records[anchor].core}", anchor + 1\n',
        ["test_thousands_separators"],
    ),
    (
        "M92-the-whole-half-of-a-decimal-is-written-ungrouped",
        '    return f"{_write_number(run, whole)}.{digits}", end\n',
        '    return f"{whole}.{digits}", end\n',
        ["test_thousands_separators"],
    ),
    (
        "M93-the-fractional-half-of-a-decimal-is-grouped",
        '    return f"{_write_number(run, whole)}.{digits}", end\n',
        '    return f"{_write_number(run, whole)}'
        '.{_write_number(run, int(digits))}", end\n',
        ["test_thousands_separators", "test_decimals"],
    ),
    (
        "M94-a-spelled-out-digit-run-is-grouped",
        "    if value < _ITN_GROUP_MIN or not _scale_built(run):\n"
        "        return str(value)\n",
        "    if value < _ITN_GROUP_MIN:\n        return str(value)\n",
        ["test_thousands_separators"],
    ),
    (
        "M95-the-digit-sequence-rule-groups-its-identifier",
        '    return "".join(str(_ITN_DIGITS[word]) for word in run)\n',
        "    return f\"{int(''.join(str(_ITN_DIGITS[word]) for word in run)):,}\"\n",
        ["test_thousands_separators", "test_digit_sequences"],
    ),
    (
        "M96-an-address-number-is-grouped-like-a-quantity",
        "    if _street_suffix_near(records, end, _BARE_ADDRESS_REACH):\n",
        "    if False:\n",
        ["test_thousands_separators"],
    ),
    (
        "M97-the-bare-address-reach-narrows-to-the-licensing-reach",
        "_BARE_ADDRESS_REACH = 3\n",
        "_BARE_ADDRESS_REACH = 1\n",
        ["test_thousands_separators"],
    ),
    (
        "M98-the-vocabulary-drops-the-letter",
        '_ITN_OH_WORDS = {"oh", "o"}\n',
        '_ITN_OH_WORDS = {"oh"}\n',
        ["test_letter_o_reads_as_the_spoken_zero"],
    ),
    (
        "M99-only-the-interjection-carries-the-zero-value",
        "for _oh_word in _ITN_OH_WORDS:\n"
        "    _ITN_DIGITS[_oh_word] = 0\n"
        "del _oh_word\n",
        '_ITN_DIGITS["oh"] = 0\n',
        ["test_letter_o_reads_as_the_spoken_zero"],
    ),
    (
        "M100-the-run-admission-narrows-to-the-interjection",
        "            low in _ITN_OH_WORDS\n            and run\n",
        '            low == "oh"\n            and run\n',
        ["test_letter_o_reads_as_the_spoken_zero"],
    ),
    (
        "M101-the-clock-minutes-narrow-to-the-interjection",
        "        and words[0] in _ITN_OH_WORDS\n",
        '        and words[0] == "oh"\n',
        ["test_letter_o_reads_as_the_spoken_zero"],
    ),
    (
        "M102-absorption-narrows-to-the-interjection",
        "    if low in _ITN_NUMBER_WORDS or low in _ITN_OH_WORDS:\n",
        '    if low in _ITN_NUMBER_WORDS or low == "oh":\n',
        ["test_letter_o_reads_as_the_spoken_zero"],
    ),
    (
        "M103-a-leading-letter-starts-a-run",
        "        elif (\n"
        "            low in _ITN_OH_WORDS\n"
        "            and run\n"
        "            and (run[-1] in _ITN_DIGITS or run[-1] in _ITN_TEENS)\n"
        "        ):\n",
        '        elif low == "o" or (\n'
        "            low in _ITN_OH_WORDS\n"
        "            and run\n"
        "            and (run[-1] in _ITN_DIGITS or run[-1] in _ITN_TEENS)\n"
        "        ):\n",
        ["test_letter_o_reads_as_the_spoken_zero"],
    ),
    (
        "M104-the-letter-is-rewritten-as-the-interjection",
        "            words.append(token)\n",
        '            words.append("oh" if token.lower() == "o" else token)\n',
        [
            "test_the_ordinary_letter_still_stays_words",
            "test_letter_o_reads_as_the_spoken_zero",
        ],
    ),
]


def _env():
    inherited = dict(os.environ)
    inherited["PYTHONDONTWRITEBYTECODE"] = "1"
    inherited["PYTHONPATH"] = str(SERVICE)
    return inherited


def _clear_pycache():
    """Stale bytecode can outlive a same-length mutation, so drop it."""
    for cache in SERVICE.rglob("__pycache__"):
        if ".venv" in cache.parts:
            continue
        shutil.rmtree(cache, ignore_errors=True)


def run_selection():
    _clear_pycache()
    return subprocess.run(
        [
            PYTHON, "-m", "pytest", *SELECTION,
            "-q", "-rf", "--no-header", "-p", "no:cacheprovider",
        ],
        cwd=str(SERVICE),
        capture_output=True,
        text=True,
        timeout=180,
        env=_env(),
    )


def collect_test_cases():
    """(node count, unique test names) for the selection.

    A parametrized table is one test name and many test cases, so the
    two counts differ by a lot here (300 cases over 40 names). Expected-
    failure lookup needs the names, and the scope line needs the case
    count: reporting the name count as "collected tests" understates how
    much of the table each mutation ran (wh-shared-itn-review.9).
    """
    completed = subprocess.run(
        [
            PYTHON, "-m", "pytest", *SELECTION,
            "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider",
        ],
        cwd=str(SERVICE),
        capture_output=True,
        text=True,
        timeout=180,
        env=_env(),
    )
    names = set()
    cases = 0
    for line in completed.stdout.splitlines():
        if "::" not in line:
            continue
        cases += 1
        name = line.rsplit("::", 1)[1].strip()
        names.add(name.split("[", 1)[0])
    return cases, names


def select_mutations(wanted):
    """The mutations to run this round, or None when a filter matched
    nothing.

    The skill's run-scope rule: a round runs the mutations added or
    re-pointed in it, and the whole set runs once before the final
    commit. Any command-line argument is a substring of a mutation name.
    """
    if not wanted:
        return list(MUTATIONS)
    chosen = [
        mutation
        for mutation in MUTATIONS
        if any(fragment in mutation[0] for fragment in wanted)
    ]
    return chosen or None


def main():
    print(f"interpreter: {PYTHON}")
    selected = select_mutations(sys.argv[1:])
    if selected is None:
        print("ERROR: no mutation name matched", sys.argv[1:])
        return 1

    cases, known = collect_test_cases()
    if not known:
        print("ERROR: collected no test names -- refusing to run the gate")
        return 1

    missing = sorted(
        {
            name
            for _, _, _, expected in selected
            for name in expected
            if name not in known
        }
    )
    if missing:
        print("ERROR: these expected test names do not exist:", missing)
        return 1

    baseline = run_selection()
    if baseline.returncode != 0:
        print("BASELINE IS RED -- refusing to run the gate")
        print(baseline.stdout[-3000:])
        return 1
    print(
        f"baseline green: {cases} test cases "
        f"({len(known)} unique test names)"
    )
    skipped = len(MUTATIONS) - len(selected)
    if skipped:
        print(
            f"scope: {len(selected)} of {len(MUTATIONS)} mutations, "
            f"{skipped} skipped by the name filter {sys.argv[1:]} -- "
            "this run is NOT a full sweep"
        )
    else:
        print(f"scope: all {len(MUTATIONS)} mutations, none skipped")

    original = TARGET.read_bytes()
    source = original.decode("utf-8")
    # The patterns above are written with "\n". A target stored with
    # CRLF makes every multi-line pattern miss, in a batch, while the
    # single-line ones still match (mutation-gate skill, 2026-08-27).
    # Translate the patterns to the file's own ending; never rewrite the
    # file's endings, which is the separate damage the skill records.
    newline = "\r\n" if "\r\n" in source else "\n"
    print(f"target line ending: {newline!r}")
    failures = 0

    for name, old, new, expected in selected:
        old = old.replace("\n", newline)
        new = new.replace("\n", newline)
        count = source.count(old)
        if count != 1:
            print(f"ERROR {name}: pattern matched {count} times, expected 1")
            failures += 1
            continue

        mutated = source.replace(old, new, 1)
        try:
            compile(mutated, str(TARGET), "exec")
        except SyntaxError as error:
            print(f"ERROR {name}: the mutated file does not compile: {error}")
            failures += 1
            continue

        TARGET.write_bytes(mutated.encode("utf-8"))
        try:
            completed = run_selection()
        except subprocess.TimeoutExpired:
            print(f"ERROR {name}: the run timed out")
            failures += 1
            continue
        finally:
            TARGET.write_bytes(original)

        output = completed.stdout + completed.stderr
        if "+++ Timeout +++" in output:
            print(f"ERROR {name}: the suite timeout aborted the run")
            failures += 1
            continue

        caught = [test for test in expected if test in output]
        if completed.returncode == 0:
            print(f"SURVIVED {name}: every test still passed")
            failures += 1
        elif not caught:
            print(f"SURVIVED {name}: tests failed, but none of {expected}")
            print(output[-2000:])
            failures += 1
        else:
            print(f"caught   {name}: by {caught}")

    if TARGET.read_bytes() != original:
        print(f"ERROR: {TARGET.name} was not restored")
        failures += 1

    _clear_pycache()
    print("errors/survivors:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
