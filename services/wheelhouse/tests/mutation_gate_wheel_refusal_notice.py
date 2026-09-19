"""Mutation gate for the wheel refusal notice (wh-wheel-refusal-notice).

WHY THIS GATE EXISTS. Every test this bead added was written before its
implementation and was seen to fail. That is the first half of the rule. The
second half is that a test can fail for the wrong reason, so each protected
behaviour is broken here at the level of the INPUT the code depends on -- the
membership set the refusal check consults, the log level itself, the source
string the shared notice helper is given -- and not only by deleting the fix.

WHAT THE BEAD CHANGED, and therefore what this gate defends:

  step 1: the discrete spoken scroll writes its own notice
      no-notice-call            the notice call is deleted
      refusal-inverted          success and refusal swap places
      exclusion-inverted        the exclusion check is inverted, so every
                                ordinary refusal loses its notice
      validation-set-repopulated
                                the published tuple names a reason again,
                                which silences that refusal's notice
      exclusion-list-copied     the caller keeps its own copy of the list
                                instead of reading the primitive's
      raise-branch-error        the defensive branch logs ERROR again, which
                                would show a second box beside the notice
      raise-branch-not-refused  a raising seam stops producing a notice
      message-changed           the wording the user reads
      wrong-source              the notice names the wrong scroll
      wrong-title               the shared title is replaced
      notice-raise-escapes      a failing notice escapes the Input loop

  step 1: the shared notice helper learns who is calling it
      helper-source-hardcoded   the log line names the caller it used to
      helper-default-changed    the continuous scroll's own wording changes

  step 2: the wheel refusal stops popping its own box
      short-send-error          the refusal logs ERROR again
      raise-error               the except branch logs ERROR again
      short-send-loses-counts   the level drops AND so does the diagnosis

  round 5: NO wheel refusal logs an ERROR (the .1.7 reversal -- the
  rate limiter can drop the box, so an ERROR record is not proof the
  user was told, and the caller's written notice is the whole report)
      direction-error-promoted  a malformed direction goes back to ERROR,
                                which adds the generic box beside the
                                notice the caller now always writes
      clicks-error-promoted     the same for a malformed notch count

  round 2: a refusal reason added LATER cannot evade the published list
      undeclared-error-refusal  a new branch logs ERROR and is left out

  rounds 4 and 5: the source walk that reads those branches is itself the
  thing under test. Every mutation here was seen to survive the walk as
  it stood before its round's fix
      split-block-error         a record inside an if that falls through
      split-match-block-error   the same, written as a match case
      nearest-record-shadows-error
                                an ERROR then a WARNING above one return,
                                where only the nearest record was read
      repeated-reason-overwrites
                                one reason returned from two branches that
                                disagree, where one answer per reason let
                                the last branch win
      producer-levels-disagree  two producers that disagree about one
                                reason
      delegation-shadowed       the delegate rebound by an assignment
      delegation-walrus-shadowed
                                the delegate rebound by a walrus
      primitive-return-delegated
                                a refusal handed off instead of returned
      seam-error-not-demoted    the seam's own record goes back to ERROR
      seam-record-removed       the seam stops writing a record at all
      seam-drops-exc-info       the seam keeps the record, loses the
                                traceback
      undeclared-error-refusal-in-the-seam
                                the round-2 mutation, in the wrapper

  round 5: a notice that arrives after the test has stopped looking
      late-second-notice        _stop_and_say speaks twice, half a second
                                apart, which the round-4 default-seam test
                                could not see

  round 6: the uniform rule, and the guard's own reading of it. The
  round-5 walk held the published tuple EQUAL to the reasons it found
  logging an ERROR, and read a record only from a bare logger.error
  expression statement. Every mutation here was seen to survive that walk
      paired-error-refusal-and-tuple
                                a new ERROR branch AND its reason added to
                                the tuple in one edit, which kept the
                                equality true and left the user with the
                                rate-limited box or nothing
      critical-refusal-branch   an ERROR+ record written as logger.critical,
                                which the handler shows and the walk did not
                                read
      embedded-error-record     the same record with its return value bound,
                                so it was no longer a bare expression
      aliased-logger-error-refusal
                                the same record through a name other than
                                logger
      nested-nonlocal-rebinds-delegate
                                the delegate rebound from a nested scope,
                                which the walk stops before
      nested-default-walrus-rebinds-delegate
                                the same binding through a walrus in a
                                nested def's default, evaluated in the
                                wrapper's own scope

  round 7: loud records the round-6 walk could not see. It read a logging
  call only where the call's own callee was an attribute, and it read
  records only from statements ABOVE the return in the return's own
  block. Every mutation here was seen to survive that walk
      getattr-error-refusal     the method fetched by name, so the outer
                                call's callee is a call rather than an
                                attribute
      module-alias-error-refusal
                                the bound method aliased at module level,
                                leaving a plain local call inside the
                                producer
      ancestor-condition-error-refusal
                                the record in the `if` header that guards
                                the refusal return, above nothing in the
                                return's own block
      ancestor-finally-error-refusal
                                the record in a `finally`, which runs on
                                the way OUT of the return

  round 8: the record moved OUT of the producer, into something it
  calls. The round-7 check walked the producer's own subtree and read
  module-level aliases, so a module-level def carrying the record was
  invisible from both directions. Every mutation here was seen to
  survive that check
      module-helper-error-refusal
                                a module-level def holding the record,
                                called from a new refusal branch in the
                                primitive
      wrapper-helper-error-refusal
                                the same shape in the producer that owns
                                the delegation exception, so the
                                exemption cannot swallow the check
      unanalysable-helper-refusal
                                an IMPORTED helper, quiet at run time:
                                the guard must fail because it cannot
                                read the body, not because it saw a
                                record

  round 9: the callable REPLACED rather than moved. A decorator is a
  bare name in decorator_list, so the round-8 walk -- which reacted to
  attributes, string constants and bare-name CALLS -- had no branch
  that saw it, and what runs after decoration is written somewhere the
  walk never reads. Both mutations were seen to survive that check
  with no other test failing, which is also what proves them
  runtime-identical for every direction the suite passes
      decorated-producer-error-refusal
                                a decorator on the primitive itself,
                                answering a refusal with an ERROR
                                record in place of the real body
      decorated-nested-helper-error-refusal
                                a decorator on a nested def the
                                producer calls, which the walk skipped
                                as defined inside the producer -- true
                                of the body written there, false of
                                what runs

  round 10: the callee NOT WRITTEN AS A NAME, and the name no longer
  pointing at the def. Rounds 6 to 9 all resolved a call only when its
  callee was a bare name, while the guard's own docstring already
  claimed a callable it cannot read fails -- so this round is the
  over-claim caught from the other direction. Both mutations were seen
  to survive that check with no other test failing, which is also what
  proves them runtime-identical for every direction the suite passes
      container-called-helper-refusal
                                the helper reached through
                                ``[_helper][0]()``, whose callee is a
                                Subscript, so no branch of the walk
                                fired at all
      rebound-helper-error-refusal
                                a quiet def followed by
                                ``_quiet = _loud`` at module level: the
                                body the walk reads is not the body the
                                call runs, and the alias scan misses it
                                because the value is a plain name

  round 11: the binding that RUNS where round 10 assumed nothing ran,
  and the class header the walk never opened. Round 10 skipped every
  module-level def, async def and class statement whole, on the reading
  that such a statement only binds its own name -- true of the body,
  false of the decorators, defaults, annotations, bases, keywords and
  class suite, all of which execute at import. The first four mutations
  below were seen to survive that fence with no other test failing.
  The fifth attacks the computed-callee exception itself
      default-walrus-rebinds-helper-refusal
                                a walrus in a def default argument,
                                which evaluates at import and replaces
                                the quiet definition the walk read
      match-capture-rebinds-helper-refusal
                                a bare ``case <name>:``, which binds
                                that name -- and lives under
                                Match.cases, a block the fence never
                                descended into
      class-global-rebinds-helper-refusal
                                a class suite that declares ``global``
                                and then assigns, which binds at module
                                level rather than class level
      star-import-rebinds-helper-refusal
                                a star import, quiet at run time on
                                purpose: the guard must fail because it
                                cannot know which names the import
                                binds. Round 10 recorded the literal
                                '*', which matches no definition
      metaclass-mul-error-refusal
                                the record inside a metaclass __mul__,
                                reached through the computed-callee
                                exception. Round 10 followed the left
                                name and read its class BODY, so
                                ``metaclass=`` pointed at code the walk
                                never opened while the multiplication
                                ran it
      ctypes-callee-exception-removed
                                the POSITIVE PIN, and the only mutation
                                that touches the guard file itself.
                                Deleting the exception must FAIL the
                                guard on unmodified shipped source,
                                because the primitive reaches
                                ``(Input * count)(*events)`` through
                                _send_mouse_events. This one is caught
                                before the fix as well as after -- that
                                is its point: it proves the exception is
                                load-bearing rather than dead permission

  round 12: the class suite read one statement deep. Round 11's own
  sentence said a class suite declaring ``global`` and then assigning
  binds at module level; its code collected the declaration only from
  a direct statement and the binding only from a direct assignment.
  All four mutations below were seen to survive that fence with no
  other test failing
      class-nested-if-global-rebinds-helper-refusal
                                the ``global`` and its assignment both
                                inside an ``if``, which runs at import
                                exactly as a direct statement would
      class-global-for-target-rebinds-helper-refusal
                                a direct ``global`` with a ``for``
                                target as the binding form -- round 11
                                read targets only from an assignment
                                and a ``del``
      class-nested-class-global-rebinds-helper-refusal
                                a ``global`` in a NESTED class body,
                                which round 11 never descended into;
                                both class bodies run at import
      class-global-import-alias-rebinds-helper-refusal
                                an import alias as the binding form,
                                quiet at run time on purpose: like the
                                star import, the guard must fail
                                because it cannot read what the name
                                now holds

  round 13: every BINDING form, no DEFINITION form. Round 12 taught both
  scans to read a whole suite and share one binding reader, and neither
  scan recorded the one thing a ``def`` or ``class`` statement does when
  it runs -- bind its own name. _definitions_in reads module.body alone,
  so the guard kept following the first, quiet definition. All six
  mutations below were seen to survive with NO test failing at all, and
  each is caught afterwards by that one structural test alone
      module-nested-if-def-rebinds-helper-refusal
                                a ``def`` inside a module-level ``if``
      module-nested-if-class-rebinds-helper-refusal
                                the same with a ``class``; calling the
                                name now constructs, and __init__ logs
      module-nested-try-def-rebinds-helper-refusal
                                ``try`` rather than ``if``, since the
                                scope walk descends into both
      module-nested-if-async-def-rebinds-helper-refusal
                                ``async def``, quiet at run time on
                                purpose: calling it only builds a
                                coroutine, so like the import alias
                                above the guard must fail because the
                                body it read is not what the name holds
      class-global-def-rebinds-helper-refusal
                                a ``def`` as the binding form for a
                                declared ``global`` in a class suite
      class-global-class-rebinds-helper-refusal
                                the ``class`` form of the same, which
                                binds in the enclosing scope AND opens
                                a scope of its own

  round 14: the PRODUCER'S own scope was never read. Rounds 10 to 13
  taught the walk what module level and a class suite bind. A producer
  binds names too, and _defined_inside recorded only definitions --
  saying outright that a parameter and an assignment do not count, while
  nothing else recorded them either, so the walk followed a module
  definition of the same name. It also read ast.walk, so a definition in
  a NESTED scope, which cannot bind the producer's call, stopped the walk
  resolving that call at all. All eight below were seen to survive with
  NO test failing, then each caught by that one structural test alone
      local-parameter-default-shadows-helper-refusal
                                a same-named parameter, holding whatever
                                the caller passed
      local-assign-shadows-helper-refusal
                                a plain local assignment
      local-for-target-shadows-helper-refusal
                                a local ``for`` target
      local-with-as-shadows-helper-refusal
                                a local ``with ... as`` target
      local-walrus-shadows-helper-refusal
                                a local walrus -- round 11 taught the
                                MODULE scan to read one, a producer
                                scope still did not
      nested-def-only-suppresses-resolution-refusal
                                the INVERSE direction: the module helper
                                is loud, and a quiet def of the same name
                                sits inside a function nested in the
                                producer, where it cannot bind the
                                producer's own call
      nested-async-def-only-suppresses-resolution-refusal
                                the same with ``async def``
      nested-class-only-suppresses-resolution-refusal
                                the same with ``class``

  round 15: every CHILD SCOPE was read with the producer's bindings.
  Round 14 gave the producer its own; _loud_record_sites still crossed
  each child scope with ast.walk while holding one local and one
  shadowed set, so a name written inside a nested function, class suite,
  lambda or comprehension resolved against the producer and then against
  a module definition rather than against the bindings that govern it.
  The shipped scroll_wheel already holds a list comprehension, safe only
  because its target is named underscore. All ten below survived the
  round-14 guard with NO test failing, then each was caught by that one
  structural test alone
      nested-def-scope-shadows-helper-refusal
                                a nested function binds the callee
      nested-async-def-scope-shadows-helper-refusal
                                the same with ``async def``, never
                                awaited, so quiet on purpose
      nested-class-scope-shadows-helper-refusal
                                a class suite, which runs where it is
                                written in a namespace of its own
      lambda-parameter-shadows-helper-refusal
                                a lambda parameter -- the one scope kind
                                holding an expression, not statements
      list-comprehension-target-shadows-helper-refusal
      set-comprehension-target-shadows-helper-refusal
      dict-comprehension-target-shadows-helper-refusal
      generator-comprehension-target-shadows-helper-refusal
                                the four comprehension target scopes
      nested-def-parameter-shadows-helper-refusal
                                a nested function's own parameter, the
                                form round 14 read for the producer only
      comprehension-in-nested-def-shadows-helper-refusal
                                a comprehension two scopes down

  round 16: two DECLARATION forms decide a name without binding it, and
  one whole scope kind was never read. A ``global`` declaration makes a
  bare name skip every enclosing function and mean the module name, so a
  child scope reached the loud module helper while the producer's own
  quiet definition of that name sat beside it; nothing in the walk
  represented ast.Global. A ``nonlocal`` declaration rebinds an
  enclosing scope's name from inside a nested one, which the enclosing
  scope's own reader cannot see because it stops at a ``def`` -- the
  round-16 finding called that case fail-closed, and measuring it showed
  it is not. And a PEP 695 type parameter bound is executable code in an
  annotation scope that the walk never yielded at all, on the supported
  3.12 runtime. All thirteen below survived the round-15 guard with NO
  test failing, then each was caught by that one structural test alone
      global-in-nested-def-reaches-loud-helper-refusal
                                a nested function declares the name
                                ``global``
      global-in-nested-async-def-reaches-loud-helper-refusal
                                the same with ``async def``
      global-in-nested-class-suite-reaches-loud-helper-refusal
                                the same in a class suite
      global-in-compound-block-reaches-loud-helper-refusal
                                the declaration inside an ``if`` rather
                                than at the top of the scope
      global-two-scopes-down-reaches-loud-helper-refusal
                                the declaration two scopes below the
                                producer
      global-in-class-method-reaches-loud-helper-refusal
                                the declaration in a method of a nested
                                class, a function scope inside a scope
                                that does not close
      nonlocal-rebinding-read-by-the-producer-refusal
                                a nested scope rebinds the producer's
                                own definition and the PRODUCER calls it
                                afterwards
      generic-def-bound-runs-on-the-refusal-path
                                a type parameter bound, evaluated by
                                reading __bound__
      generic-async-def-bound-runs-on-the-refusal-path
                                the same on an ``async def``
      generic-class-bound-runs-on-the-refusal-path
                                the same on a ``class``
      generic-nested-two-scopes-down-bound-runs-on-the-refusal-path
                                the same two scopes below the producer
      generic-producer-bound-runs-on-the-refusal-path
                                the type parameter list on the PRODUCER
                                itself
      generic-module-definition-bound-runs-on-the-refusal-path
                                the type parameter list on a module
                                definition the producer calls by name

  round 17: the round-16 answers, applied at the wrong set of ENTRY
  POINTS. _globals_rebound_in learned to read a ``global`` but queued
  only a CLASS suite for later reading, so a declaration written in a
  function body -- where one is usually written -- never reached
  _module_level_rebindings. The rebinding scope stays quiet for a
  reason worth naming: a shadowed name produces a site only when a CALL
  to it is resolved in that scope, and a scope that only rebinds
  contains no such call. Separately, the ninth scope round 16 REFUSED is
  refused only where the walk arrives, and a MODULE-level ``type``
  statement is visited by nothing: _definitions_in returns def, async
  def and class, and ast.TypeAlias was not a binding form. Its value and
  its bounds run lazily, on the first read of ``__value__`` or
  ``__bound__``. All thirteen below survived the round-16 guard with NO
  test failing, then each was caught by that one structural test alone
      nested-def-global-write-read-by-the-producer-refusal
                                a nested function declares the name
                                ``global`` and BINDS it; the producer
                                calls that name afterwards
      nested-async-def-global-write-read-by-the-producer-refusal
                                the same with ``async def``
      nested-class-global-write-read-by-the-producer-refusal
                                the same in a class suite
      nested-global-write-through-a-for-target-refusal
                                the write is a ``for`` target
      nested-global-write-through-a-with-as-target-refusal
                                the write is a ``with ... as`` target
      nested-global-write-two-scopes-down-refusal
                                the write sits two scopes below the
                                producer
      nested-global-write-read-by-a-sibling-scope-refusal
                                a SIBLING nested scope reads the name,
                                so neither scope holds both halves
      module-definition-global-write-read-by-the-producer-refusal
                                the write sits inside a module
                                definition the producer calls by name
      module-generic-type-alias-bound-runs-on-the-refusal-path
                                a module ``type`` statement's bound,
                                evaluated by reading __bound__
      module-plain-type-alias-value-runs-on-the-refusal-path
                                a module ``type`` statement's value,
                                evaluated by reading __value__
      module-type-alias-value-read-in-a-nested-scope-refusal
                                the same read written in a nested scope
                                of the producer
      module-type-alias-value-read-by-a-module-definition-refusal
                                the same read written in a module
                                definition the producer calls by name
      producer-local-type-alias-called-through-value-refusal
                                a producer-local alias CALLED through
                                ``__value__()``, which the
                                attribute-callee boundary let through

  Two shapes constructed for round 17 are NOT escapes and are recorded
  here so nobody re-derives them: ``global _helper`` followed by an
  annotated assignment is a Python SyntaxError, so no program holds it;
  and a global write through a ``def`` of that name was already caught,
  because the walk reads the nested def's own body.

  round 18: the round-17 lazy-member set was one member short. A PEP 695
  type parameter carries a BOUND or a set of CONSTRAINTS, never both, and
  _LAZY_MEMBERS named only __bound__. A branch that stored a constrained
  parameter in a module-level name and read .__constraints__ ran the
  constraint expressions with nothing reported, and never had to write
  __type_params__, which was the only other member that would have
  refused it. The set is now measured rather than recalled: every dunder
  member of a bounded parameter, a constrained one, a class's parameter,
  a plain alias and a parameterised alias was read on the pinned 3.12
  runtime with the logger captured, and exactly three reads run
  author-written code. All three below survived the round-17 guard with
  NO test failing, then each was caught by that one structural test alone
      stored-type-parameter-constraints-on-the-refusal-path
                                the branch reads a stored constrained
                                parameter's __constraints__
      stored-type-parameter-constraints-read-in-a-nested-scope-refusal
                                the same read written in a nested scope
                                of the producer
      stored-type-parameter-constraints-read-by-a-module-definition-refusal
                                the same read written in a module
                                definition the producer calls by name

  Round 18's second finding, .1.33, has NO mutation here, and the absence
  is deliberate. It replaced a paragraph in _module_level_rebindings that
  still described a gap round 17 had closed. A mutation gate cannot prove
  prose -- any mutation written for it would pass whatever the paragraph
  said -- so the fix is verified by reading, and this note records that
  rather than leaving a reader to wonder which mutation covers it.

  step 2: the blast radius, pinned so the demotion cannot spread
      press-keys-demoted        press_keys returns None, so its ERROR record
                                is the only signal a dropped hotkey has
      type-string-demoted       the same for a partly typed phrase
      click-release-demoted     the stuck-button ERROR click already pins

WHAT THIS GATE DOES NOT CLAIM. It covers the tests THIS BEAD ADDED, plus the
one existing test the bead's review named as its blast-radius pin. The two
test files it runs also hold many tests from wh-voice-access-parity.2.3, and
this gate makes no claim about those: they are the earlier bead's, and
mutation_gate_continuous_scroll.py is where that feature's claims live. The
completeness check below is scoped to FEATURE_TESTS for exactly that reason,
and the scope line printed at the end says which subset ran.

Run it from services/wheelhouse, and NEVER while a test suite is running in
the same worktree -- this file rewrites the sources that suite is importing:

    uv run python tests/mutation_gate_wheel_refusal_notice.py

``--check`` answers "does every pattern still match exactly once, and
does each mutant still parse" in seconds, without running a single
test. It is not a sweep: a pattern can match once, parse, and still be
caught by nothing, so a clean --check line never stands in for a full
run.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE_DIR = Path(__file__).resolve().parents[1]

HANDLER = SERVICE_DIR / "ui" / "ui_action_handler.py"
SCROLLER = SERVICE_DIR / "ui" / "continuous_scroll.py"
SENDER = SERVICE_DIR / "utils" / "win_input_sender.py"
# The guard itself, mutated by exactly one entry: removing the ctypes
# array exception must FAIL the guard on unmodified shipped source,
# which is the positive pin that the exception is load-bearing rather
# than dead permission (wh-wheel-refusal-notice.1.22).
GUARD = SERVICE_DIR / "tests" / "test_win_mouse_scroll.py"

HANDLER_TESTS = "tests/test_ui/test_mouse_scroll_handler.py"
WHEEL_TESTS = "tests/test_win_mouse_scroll.py"
CLICK_TESTS = "tests/test_win_input_sender_mouse.py"
SCROLLER_TESTS = "tests/test_continuous_scroller.py"

FEATURE_TEST_FILES = (
    HANDLER_TESTS, WHEEL_TESTS, CLICK_TESTS, SCROLLER_TESTS,
)

# The four files together run in about seven seconds clean. No mutation here
# can make a test wait on a timer -- every one changes a log level, a string
# or a boolean -- so this limit exists only to turn a hang into a reported
# error rather than a lost run.
RUN_TIMEOUT_S = 300

_NOTICE_TESTS = "TestTheDiscreteScrollWritesItsOwnNotice"
_SOURCE_TESTS = "TestTheNoticeNamesTheScrollThatSentIt"
_LEVEL_TESTS = "TestTheWheelRefusalDoesNotPopItsOwnBox"
_RADIUS_TESTS = "TestTheOtherInputPrimitivesKeepTheirError"
_CONTRACT_TESTS = "TestTheRefusalLevelContractIsMachineReadable"
_OWNER_TESTS = "TestTheExclusionListComesFromThePrimitive"
_STRUCTURE_TESTS = "TestEveryRefusalBranchDeclaresItsLevelInTheSource"
_SEAM_TESTS = "TestARaisingPrimitiveThroughTheDefaultSeam"


def _notice(name):
    return f"{_NOTICE_TESTS}::{name}"


def _source(name):
    return f"{_SOURCE_TESTS}::{name}"


def _level(name):
    return f"{_LEVEL_TESTS}::{name}"


def _radius(name):
    return f"{_RADIUS_TESTS}::{name}"


def _contract(name):
    return f"{_CONTRACT_TESTS}::{name}"


def _owner(name):
    return f"{_OWNER_TESTS}::{name}"


def _structure(name):
    return f"{_STRUCTURE_TESTS}::{name}"


def _seam(name):
    return f"{_SEAM_TESTS}::{name}"


# Every test that shows a notice for an ordinary refusal. Deleting the call,
# or inverting the check that reaches it, breaks all of them at once.
#
# The default-seam test belongs here for the same reason, one level
# out (wh-wheel-refusal-notice.1.3): it drives a refusal through the
# real _win32_scroll_wheel and asserts exactly one notice, so
# anything that removes, inverts or misroutes the notice breaks it
# too. Its continuous sibling is NOT here -- that route reports
# through the scroller's own notifier, which none of these three
# mutations touch.
_EVERY_REFUSAL_NOTICE = [
    _notice("test_a_short_send_shows_one_written_notice"),
    _notice("test_an_invalid_direction_shows_one_written_notice"),
    _notice("test_an_invalid_notch_count_shows_one_written_notice"),
    _notice("test_a_sendinput_error_shows_one_written_notice"),
    _notice("test_an_unknown_refusal_reason_still_shows_the_notice"),
    _notice("test_the_notice_carries_the_shared_title"),
    _notice("test_the_notice_says_the_exact_words_the_user_hears"),
    _source("test_the_handler_names_the_discrete_scroll_when_it_reports"),
    _seam("test_the_discrete_scroll_shows_one_notice_and_logs_no_error"),
]

MUTATIONS = [
    # -----------------------------------------------------------------
    # Step 1: the discrete spoken scroll writes its own notice.
    # -----------------------------------------------------------------
    {
        "name": "no-notice-call",
        "target": HANDLER,
        "old": (
            "        if refused:\n"
            "            self._say_the_wheel_would_not_turn()\n"
        ),
        "new": "        if refused:\n            pass\n",
        "expect": _EVERY_REFUSAL_NOTICE + [
            _notice("test_a_raising_seam_shows_the_notice_and_logs_a_warning"),
        ],
    },
    {
        # The check itself, rather than the call it guards. A test that only
        # watched for "send_notice was called" would survive this.
        "name": "refusal-inverted",
        "target": HANDLER,
        "old": (
            "                not succeeded "
            "and reason not in _WHEEL_VALIDATION_REFUSALS\n"
        ),
        "new": (
            "                succeeded "
            "and reason not in _WHEEL_VALIDATION_REFUSALS\n"
        ),
        "expect": _EVERY_REFUSAL_NOTICE + [
            _notice("test_a_successful_scroll_shows_no_notice"),
        ],
    },
    # exclusion-dropped was removed by wh-wheel-refusal-notice.1.7.
    # It replaced `reason not in _WHEEL_VALIDATION_REFUSALS` with
    # `True`, and the tuple is now empty, so the two are the same
    # expression. It would have reported a catch while proving
    # nothing. exclusion-inverted below covers the check's polarity,
    # and validation-set-repopulated covers the tuple's emptiness.
    {
        "name": "exclusion-inverted",
        "target": HANDLER,
        "old": "reason not in _WHEEL_VALIDATION_REFUSALS\n",
        "new": "reason in _WHEEL_VALIDATION_REFUSALS\n",
        "expect": _EVERY_REFUSAL_NOTICE,
    },
    {
        # The input-level form of exclusion-dropped: the check is untouched
        # and the set it reads is emptied instead. This is what shows the two
        # tests read the behaviour rather than the shape of the condition.
        #
        # The set moved into utils/win_input_sender.py under
        # wh-wheel-refusal-notice.1.1, and .1.7 emptied it, so this
        # mutation was INVERTED rather than deleted: it now puts the
        # two validation reasons back. That is the exact regression
        # .1.7 removed -- re-listing a reason makes the handler trust
        # the generic box again, and the rate limiter drops that box
        # for a repeat inside ten seconds, leaving zero notices. The
        # source no longer logs either reason at ERROR, so the
        # contract and structural tests both see the re-listing too.
        "name": "validation-set-repopulated",
        "target": SENDER,
        "old": "WHEEL_REFUSALS_THAT_LOG_ERROR: tuple[str, ...] = ()\n",
        "new": (
            'WHEEL_REFUSALS_THAT_LOG_ERROR: tuple[str, ...] = '
            '("invalid_direction", "invalid_clicks")\n'
        ),
        "expect": [
            _notice("test_an_invalid_direction_shows_one_written_notice"),
            _notice("test_an_invalid_notch_count_shows_one_written_notice"),
            _contract(
                "test_every_error_logged_refusal_is_listed_and_no_other_is"
            ),
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # The list is copied into the handler instead of read from the
        # primitive. Behaviour is identical today, so every behavioural test
        # stays green; only the identity pin sees it. That is the point --
        # the copy is what lets a refusal reason added later log at ERROR in
        # one module and never reach the exclusion list in the other, which
        # is the duplicate box coming back (wh-wheel-refusal-notice.1.1).
        "name": "exclusion-list-copied",
        "target": HANDLER,
        "old": "_WHEEL_VALIDATION_REFUSALS = WHEEL_REFUSALS_THAT_LOG_ERROR\n",
        "new": (
            '_WHEEL_VALIDATION_REFUSALS = '
            '("invalid_direction", "invalid_clicks")\n'
        ),
        "expect": [
            _owner("test_the_handler_reads_the_primitive_s_own_list"),
            _notice("test_an_invalid_direction_shows_one_written_notice"),
            _notice("test_an_invalid_notch_count_shows_one_written_notice"),
        ],
    },
    {
        "name": "raise-branch-error",
        "target": HANDLER,
        "old": (
            "            logger.warning(\n"
            '                "scroll_wheel: unexpected error '
            '(direction=%r clicks=%r): %s",\n'
        ),
        "new": (
            "            logger.error(\n"
            '                "scroll_wheel: unexpected error '
            '(direction=%r clicks=%r): %s",\n'
        ),
        "expect": [
            _notice("test_a_raising_seam_shows_the_notice_and_logs_a_warning"),
        ],
    },
    {
        "name": "raise-branch-not-refused",
        "target": HANDLER,
        "old": "            refused = True\n        if refused:\n",
        "new": "            refused = False\n        if refused:\n",
        "expect": [
            _notice("test_a_raising_seam_shows_the_notice_and_logs_a_warning"),
        ],
    },
    {
        "name": "message-changed",
        "target": HANDLER,
        "old": 'SCROLL_REFUSED_MESSAGE = "Scrolling failed"\n',
        "new": 'SCROLL_REFUSED_MESSAGE = "Something went wrong"\n',
        # expect is the EXACT set that fails, and it is ONE test. The two
        # tests that compare the notice against SCROLL_REFUSED_MESSAGE do NOT
        # fail here, because changing the constant moves both sides of their
        # assertion together. That is a true fact about those two tests: they
        # pin that the handler uses the shared constant rather than an ad-hoc
        # string, which is worth pinning, but they cannot see the wording. The
        # test below is the one that reads the sentence itself, and it was
        # added because this mutation survived without it.
        "expect": [
            _notice("test_the_notice_says_the_exact_words_the_user_hears"),
        ],
    },
    {
        "name": "wrong-source",
        "target": HANDLER,
        "old": '                source="discrete scroll",\n',
        "new": '                source="continuous scroll",\n',
        "expect": [
            _source(
                "test_the_handler_names_the_discrete_scroll_when_it_reports"
            ),
        ],
    },
    {
        "name": "wrong-title",
        "target": HANDLER,
        "old": "                NOTICE_TITLE, SCROLL_REFUSED_MESSAGE,\n",
        "new": '                "Scroll", SCROLL_REFUSED_MESSAGE,\n',
        "expect": [
            _notice("test_the_notice_carries_the_shared_title"),
        ],
    },
    {
        # The handler runs inside the Input process command loop, so a raise
        # here costs the loop the command. Narrowing the except is the input-
        # level way to let one through: nothing is deleted, and an OSError
        # from the notifier stops being caught.
        #
        # wh-gate-wheel-refusal-pattern-repair: the except line on its own
        # matched ui_action_handler.py TWICE once 43c2b931 added
        # _say_the_keyboard_refused with the same containment shape, so the
        # gate refused this mutation as ambiguous and no full sweep was
        # clean after 2026-09-04. The pattern now carries site A's own
        # send_notice call above it, which names SCROLL_REFUSED_MESSAGE and
        # the discrete scroll source, so it can only match
        # _say_the_wheel_would_not_turn. The mutation is unchanged: narrow
        # the except, delete nothing.
        "name": "notice-raise-escapes",
        "target": HANDLER,
        "old": (
            "        try:\n"
            "            send_notice(\n"
            "                NOTICE_TITLE, SCROLL_REFUSED_MESSAGE,\n"
            '                source="discrete scroll",\n'
            "            )\n"
            "        except Exception as exc:  "
            "# noqa: BLE001 -- a report must never raise\n"
        ),
        "new": (
            "        try:\n"
            "            send_notice(\n"
            "                NOTICE_TITLE, SCROLL_REFUSED_MESSAGE,\n"
            '                source="discrete scroll",\n'
            "            )\n"
            "        except ValueError as exc:  "
            "# noqa: BLE001 -- a report must never raise\n"
        ),
        "expect": [
            _notice("test_a_raising_notice_never_escapes_the_handler"),
        ],
    },
    # -----------------------------------------------------------------
    # Step 1: the shared notice helper learns who is calling it.
    # -----------------------------------------------------------------
    {
        "name": "helper-source-hardcoded",
        "target": SCROLLER,
        "old": (
            '                "%s: no notifier worker, %r not shown", '
            "source, message\n"
        ),
        "new": (
            '                "continuous scroll: no notifier worker, '
            '%r not shown", message\n'
        ),
        "expect": [
            _source("test_send_notice_names_the_caller_in_its_log_line"),
        ],
    },
    {
        "name": "helper-default-changed",
        "target": SCROLLER,
        "old": '    title: str, message: str, *, source: str = "continuous scroll",\n',
        "new": '    title: str, message: str, *, source: str = "discrete scroll",\n',
        "expect": [
            _source("test_send_notice_defaults_to_the_continuous_scroll"),
        ],
    },
    # -----------------------------------------------------------------
    # Step 2: the wheel refusal stops popping its own box.
    # -----------------------------------------------------------------
    {
        "name": "short-send-error",
        "target": SENDER,
        "old": (
            "            logger.warning(\n"
            '                "scroll_wheel: short SendInput for %d %s '
            'notches: sent %d/%d; "\n'
        ),
        "new": (
            "            logger.error(\n"
            '                "scroll_wheel: short SendInput for %d %s '
            'notches: sent %d/%d; "\n'
        ),
        "expect": [
            _level("test_a_short_send_logs_a_warning_and_no_error"),
            # sendinput_short would log ERROR while absent from
            # WHEEL_REFUSALS_THAT_LOG_ERROR, which is the two-box shape the
            # contract test exists to catch (wh-wheel-refusal-notice.1.1).
            _contract(
                "test_every_error_logged_refusal_is_listed_and_no_other_is"
            ),
            # The structural test reads the same disagreement out of
            # the source rather than out of a call, so it sees this
            # one too (wh-wheel-refusal-notice.1.2).
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        "name": "raise-error",
        "target": SENDER,
        "old": (
            "        logger.warning(\n"
            '            "scroll_wheel: unexpected error: %s", '
            "exc, exc_info=True,\n"
        ),
        "new": (
            "        logger.error(\n"
            '            "scroll_wheel: unexpected error: %s", '
            "exc, exc_info=True,\n"
        ),
        "expect": [
            _level("test_a_raising_send_logs_a_warning_and_no_error"),
            # Same shape as short-send-error: sendinput_error would log ERROR
            # while absent from WHEEL_REFUSALS_THAT_LOG_ERROR.
            _contract(
                "test_every_error_logged_refusal_is_listed_and_no_other_is"
            ),
            # And the structural test, which reads the level of the
            # record in the except block straight from the source.
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # Demoting a record is only acceptable while the record still says
        # enough to diagnose the failure. This drops the counts and the Win32
        # error, which is the way a level change usually loses information.
        "name": "short-send-loses-counts",
        "target": SENDER,
        "old": (
            '                "scroll_wheel: short SendInput for %d %s '
            'notches: sent %d/%d; "\n'
            '                "Win32 error %s",\n'
            "                clicks, direction, accepted, len(events),\n"
            "                kernel32.GetLastError(),\n"
        ),
        "new": (
            '                "scroll_wheel: short SendInput for %d %s '
            'notches",\n'
            "                clicks, direction,\n"
        ),
        "expect": [
            _level("test_the_refused_wheel_call_still_writes_a_line"),
        ],
    },
    # -----------------------------------------------------------------
    # Step 2, as .1.7 left it: the two argument refusals log BELOW
    # ERROR like every other wheel refusal. These two mutations were
    # demotions until .1.7; they are promotions now, and they guard
    # the reversal from being undone one record at a time.
    # -----------------------------------------------------------------
    {
        "name": "direction-error-promoted",
        "target": SENDER,
        "old": (
            '        logger.warning("scroll_wheel: unsupported direction %r", '
            "direction)\n"
        ),
        "new": (
            '        logger.error("scroll_wheel: unsupported direction %r", '
            "direction)\n"
        ),
        "expect": [
            _level("test_an_unknown_direction_logs_a_warning_not_an_error"),
            # The record now shows a generic box BESIDE the written
            # notice the handler still writes, because the reason is
            # not listed -- the duplicate this bead exists to remove.
            _contract(
                "test_every_error_logged_refusal_is_listed_and_no_other_is"
            ),
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        "name": "clicks-error-promoted",
        "target": SENDER,
        "old": (
            '        logger.warning("scroll_wheel: unsupported notch count '
            '%r", clicks)\n'
        ),
        "new": (
            '        logger.error("scroll_wheel: unsupported notch count %r", '
            "clicks)\n"
        ),
        "expect": [
            _level("test_an_unsupported_notch_count_logs_a_warning_not_an_error"),
            _contract(
                "test_every_error_logged_refusal_is_listed_and_no_other_is"
            ),
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    # -----------------------------------------------------------------
    # Round 2 (wh-wheel-refusal-notice.1.2): a refusal reason added
    # LATER cannot evade the published list.
    # -----------------------------------------------------------------
    {
        # The exact shape the finding describes: a new branch logs an
        # ERROR and returns a new reason, with the published list left
        # alone. Every behavioural test stays green, because none of
        # them passes "corkscrew" and so none of them reaches the
        # branch; the four-provocation contract test cannot reach it
        # either. Measured before the structural test existed: 119
        # passed across the four files with this branch in place. Only
        # a test that reads the SOURCE sees it.
        "name": "undeclared-error-refusal",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            '    if direction == "corkscrew":\n'
            '        logger.error("scroll_wheel: corkscrew is not '
            'a wheel direction")\n'
            '        return (False, "corkscrew_unsupported")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    # -----------------------------------------------------------------
    # Round 3 (wh-wheel-refusal-notice.1.3): the handler's own seam
    # wrapper is a SECOND producer of a wheel refusal reason. It turns a
    # raising primitive into (False, "sendinput_error") -- a reason
    # OUTSIDE the published list, so both callers write their own notice
    # -- and it logged that at ERROR for two review rounds, unguarded,
    # because every behavioural test replaced the seam and the round-2
    # structural walk read only the primitive's file.
    # -----------------------------------------------------------------
    {
        "name": "seam-error-not-demoted",
        "target": HANDLER,
        "old": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
        ),
        "new": (
            '        logger.error("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
        ),
        "expect": [
            _seam(
                "test_the_discrete_scroll_shows_one_notice_and_logs_no_error"
            ),
            _seam(
                "test_the_continuous_scroll_shows_one_notice_and_logs_no_error"
            ),
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # The record itself, not its level. A seam that swallows the
        # raise silently leaves the log with no trace of why the wheel
        # stopped, which is the failure the demotion must not become.
        "name": "seam-record-removed",
        "target": HANDLER,
        "old": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
            '        return (False, "sendinput_error")\n'
        ),
        "new": '        return (False, "sendinput_error")\n',
        "expect": [
            _seam(
                "test_the_discrete_scroll_shows_one_notice_and_logs_no_error"
            ),
            _seam(
                "test_the_continuous_scroll_shows_one_notice_and_logs_no_error"
            ),
        ],
    },
    {
        # exc_info is the whole diagnostic value of the demoted record:
        # without it the log says a wheel call failed and never says why.
        "name": "seam-drops-exc-info",
        "target": HANDLER,
        "old": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
        ),
        "new": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed")\n'
        ),
        "expect": [
            _seam(
                "test_the_discrete_scroll_shows_one_notice_and_logs_no_error"
            ),
        ],
    },
    {
        # The round-2 mutation's sibling, aimed at the second producer: a
        # refusal branch nobody has written yet, in the wrapper. No
        # behavioural test can reach it (clicks=99 is nobody's scroll),
        # so only the structural walk sees it -- and only because that
        # walk now reads BOTH producers.
        "name": "undeclared-error-refusal-in-the-seam",
        "target": HANDLER,
        "old": (
            "    try:\n"
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "new": (
            "    if clicks == 99:\n"
            '        logger.error("_win32_scroll_wheel: 99 notches '
            'is not a scroll")\n'
            '        return (False, "too_many_notches")\n'
            "    try:\n"
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # Generalising the walk to two producers must not have loosened
        # it for the first. The wrapper may delegate its whole answer to
        # one named call; the primitive may not delegate at all, so a
        # return the walk cannot read fails instead of being skipped.
        # tuple((True, None)) is (True, None), so nothing behavioural
        # changes and only the structural walk sees this.
        "name": "primitive-return-delegated",
        "target": SENDER,
        "old": (
            "            # Nothing to undo. A wheel event is complete on its own, so a\n"
            "            # partly accepted batch has simply scrolled less far than asked,\n"
            "            # unlike a half-sent click that leaves a button held down.\n"
            '            return (False, "sendinput_short")\n'
            "        return (True, None)\n"
        ),
        "new": (
            "            # Nothing to undo. A wheel event is complete on its own, so a\n"
            "            # partly accepted batch has simply scrolled less far than asked,\n"
            "            # unlike a half-sent click that leaves a button held down.\n"
            '            return (False, "sendinput_short")\n'
            "        return tuple((True, None))\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.6, bypass (a): two producers that
        # disagree about one reason. The wrapper refuses clicks == 97
        # with "invalid_clicks" and logs an ERROR, while the primitive
        # logs that same reason at WARNING. 97 notches is nobody's
        # scroll, so no behavioural test reaches the branch.
        #
        # HONEST LIMIT, changed by wh-wheel-refusal-notice.1.7. Until
        # .1.7 this mutation isolated the per-producer agreement check:
        # it used a LISTED reason at WARNING, so the union of both
        # producers' ERROR reasons still equalled the published tuple
        # and only the per-producer rule could see it. .1.7 emptied
        # that tuple, and with it empty ANY ERROR anywhere breaks union
        # equality too, so the two rules now coincide and no mutation
        # can separate them. This one still proves a real contradiction
        # is caught; it no longer proves WHICH rule caught it. The
        # isolation returns if a reason is ever listed again.
        "name": "producer-levels-disagree",
        "target": HANDLER,
        "old": (
            "    try:\n"
            "        from utils.win_input_sender import scroll_wheel\n"
        ),
        "new": (
            "    if clicks == 97:\n"
            '        logger.error("_win32_scroll_wheel: 97 notches '
            'is not a scroll")\n'
            '        return (False, "invalid_clicks")\n'
            "    try:\n"
            "        from utils.win_input_sender import scroll_wheel\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.6, bypass (b). A record written inside
        # a compound statement that FALLS THROUGH to the refusal return
        # below it. The round-3 walk scanned the enclosing block backwards
        # for a bare logger call, saw an ast.If instead, skipped it, and
        # recorded no level at all -- so this branch read as not-an-ERROR
        # while the running code logs one. clicks == 99 is nobody's
        # scroll, so no behavioural test can reach it either.
        "name": "split-block-error",
        "target": HANDLER,
        "old": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
            '        return (False, "sendinput_error")\n'
        ),
        "new": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
            "        if clicks == 99:\n"
            '            logger.error("_win32_scroll_wheel: 99 notches '
            'is not a scroll")\n'
            '        return (False, "sendinput_error")\n'
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.6, bypass (c). The delegation
        # exception let the wrapper hand its whole answer to any bare
        # name that happened to be spelled scroll_wheel. Here the name is
        # bound by an assignment rather than by the import, which is
        # runtime-identical -- so every behavioural test passes -- and is
        # exactly the shape that would let a later edit point the
        # delegation at something that reports its refusals differently.
        "name": "delegation-shadowed",
        "target": HANDLER,
        "old": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "new": (
            "        from utils.win_input_sender import scroll_wheel as "
            "_real_wheel\n"
            "\n"
            "        scroll_wheel = _real_wheel\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.8. The same shape as split-block-error
        # above, written as a match statement instead of an if. The
        # round-4 walk looked for nested blocks under body / orelse /
        # finalbody and under except handlers, and a match statement
        # keeps its blocks somewhere else entirely -- in case clauses --
        # so the ERROR below reads as no record at all while the running
        # code writes one. 91 notches is nobody's scroll, so nothing
        # behavioural reaches it.
        "name": "split-match-block-error",
        "target": HANDLER,
        "old": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
            '        return (False, "sendinput_error")\n'
        ),
        "new": (
            '        logger.warning("_win32_scroll_wheel: seam '
            'failed", exc_info=True)\n'
            "        match clicks:\n"
            "            case 91:\n"
            '                logger.error("_win32_scroll_wheel: 91 '
            'notches is not a scroll")\n'
            '        return (False, "sendinput_error")\n'
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.9, first half. Two bare records above
        # one refusal return, ERROR first and WARNING second. The round-4
        # walk stopped at the NEAREST record and read "warning", so the
        # branch declared itself silent while the running code had
        # already written the ERROR that pops the generic box. Both
        # records reach the handler, so the ERROR is what the user sees.
        # 92 notches is nobody's scroll, and the reason is this
        # mutation's own, so no other branch and no behavioural test is
        # touched.
        "name": "nearest-record-shadows-error",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            "    if clicks == 92:\n"
            '        logger.error("scroll_wheel: 92 notches is not a '
            'scroll")\n'
            '        logger.warning("scroll_wheel: 92 notches, '
            'reported")\n'
            '        return (False, "ninety_two_notches")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.9, second half. One producer, one
        # reason, two branches that disagree: this one logs an ERROR
        # above its return, the real notch check below logs a WARNING
        # above its own. The round-4 walk kept ONE level per reason in a
        # plain dict, so whichever return the walk reached last simply
        # overwrote the other. The real check is reached last, so the
        # ERROR vanished and the empty published tuple still matched. 93
        # notches is nobody's scroll, so nothing behavioural reaches it.
        #
        # This one needs the accumulating structure, not the any-record
        # rule: the ERROR here IS the nearest record above its return, so
        # nearest-vs-any makes no difference to it.
        "name": "repeated-reason-overwrites",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            "    if clicks == 93:\n"
            '        logger.error("scroll_wheel: 93 notches is not a '
            'scroll")\n'
            '        return (False, "invalid_clicks")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.10. delegation-shadowed above rebinds
        # the delegate with a plain assignment; this one uses a walrus,
        # which the round-4 rebinding scan did not look at. The name the
        # return calls is still the name the import bound, so the walk
        # accepts the delegation -- and a later edit could put any
        # callable at all on the right of that walrus, delegating the
        # whole answer to something that reports its refusals
        # differently. Runtime-identical: the walrus assigns the real
        # primitive back to its own name and the None branch never runs.
        "name": "delegation-walrus-shadowed",
        "target": HANDLER,
        "old": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "new": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        _real_wheel = scroll_wheel\n"
            "        if (scroll_wheel := _real_wheel) is None:\n"
            '            return (False, "no_wheel")\n'
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # wh-wheel-refusal-notice.1.11. _stop_and_say sends a SECOND
        # notice half a second after the first. Nothing the discrete
        # route does can reach it -- only the scroller's timer thread
        # runs this code -- and the round-4 default-seam test waited on
        # an Event the FIRST notice sets, then read the list while the
        # timer thread was still inside _stop_and_say. stop() cannot
        # save it: _clear_if_current has already set _run to None by
        # then, so stop() owns no run to join. The fixed test joins the
        # thread the notifier itself ran on before it reads the list.
        "name": "late-second-notice",
        "target": SCROLLER,
        "old": (
            "        try:\n"
            "            self._notifier(NOTICE_TITLE, message)\n"
            "        except Exception:  # noqa: BLE001 -- a report must "
            "never raise\n"
            "            logger.warning(\n"
            '                "continuous scroll: the stop report failed", '
            "exc_info=True,\n"
            "            )\n"
        ),
        "new": (
            "        try:\n"
            "            self._notifier(NOTICE_TITLE, message)\n"
            "        except Exception:  # noqa: BLE001 -- a report must "
            "never raise\n"
            "            logger.warning(\n"
            '                "continuous scroll: the stop report failed", '
            "exc_info=True,\n"
            "            )\n"
            "        from time import sleep as _sleep\n"
            "\n"
            "        _sleep(0.5)\n"
            "        self._notifier(NOTICE_TITLE, message)\n"
        ),
        "expect": [
            _seam(
                "test_the_continuous_scroll_shows_one_notice"
                "_and_logs_no_error"
            ),
        ],
        # Both belong to wh-voice-access-parity.2.3, and both take the
        # refused-thread-start path, where start() reports the failure on
        # the CALLER's own thread rather than a timer thread. The second
        # notice therefore lands before either test reads its list, with
        # no race to lose -- which is exactly why neither says anything
        # about the claim under test here. The claim is that the
        # default-seam test can no longer read the notice list while the
        # timer thread is still speaking, and only a test that waits on
        # that thread can establish it.
        "also_fails": {
            "TestAThreadTheMachineWillNotStart::"
            "test_a_refused_thread_start_tells_the_user":
                "counts notices on the synchronous refused-start path, "
                "where no thread has to be joined to see the second one",
            "TestOneNotificationPerFailure::"
            "test_a_refused_thread_start_shows_one_notification":
                "the same synchronous path, counted one level up",
        },
    },
    # -----------------------------------------------------------------
    # Round 6 (wh-wheel-refusal-notice.1.12, .1.13): what the guard reads
    # as a record, and who it lets rebind the delegate.
    # -----------------------------------------------------------------
    {
        # .1.12. Round 5 made the rule uniform -- NO wheel refusal logs an
        # ERROR -- but the structural test still only held the published
        # tuple EQUAL to the reasons it found logging one. A new ERROR
        # branch whose reason is added to the tuple keeps that equality
        # true, so the round-5 guard passed it. At run time the discrete
        # caller reads the reason out of the tuple and suppresses its
        # written notice, leaving the rate-limited generic box as the only
        # report -- zero notices for a repeat inside ten seconds, which is
        # the defect .1.7 measured.
        "name": "paired-error-refusal-and-tuple",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction not "
                    "in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    '    if direction == "corkscrew":\n'
                    '        logger.error("scroll_wheel: corkscrew is not '
                    'a wheel direction")\n'
                    '        return (False, "corkscrew_unsupported")\n'
                    "    if not isinstance(direction, str) or direction not "
                    "in _SCROLL_DIRECTIONS:\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "WHEEL_REFUSALS_THAT_LOG_ERROR: tuple[str, ...] = ()\n"
                ),
                "new": (
                    "WHEEL_REFUSALS_THAT_LOG_ERROR: tuple[str, ...] = "
                    '("corkscrew_unsupported",)\n'
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
            # The other half, from the opposite direction: the four
            # provocations reach no corkscrew branch, so they measure an
            # empty set of ERROR-logging reasons and the tuple no longer
            # equals it.
            _contract(
                "test_every_error_logged_refusal_is_listed_and_no_other_is"
            ),
        ],
    },
    {
        # .1.12. ErrorNotificationHandler is installed at logging.ERROR
        # (utils/logging_setup.py), so a CRITICAL record shows the generic
        # box exactly as an ERROR one does. The round-5 walk compared the
        # method name against the literal string "error", so this branch
        # read as writing no record at all: the tuple stayed empty, the
        # guard passed, and at run time the user gets the caller's written
        # notice AND the generic box -- the two-notice bug this bead
        # exists to remove.
        "name": "critical-refusal-branch",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            '    if direction == "spiral":\n'
            '        logger.critical("scroll_wheel: spiral is not '
            'a wheel direction")\n'
            '        return (False, "spiral_unsupported")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.12. The round-5 walk read a record only from a BARE
        # ``logger.x(...)`` expression statement, so binding the return
        # value hid it. The record still reaches the handler; only the
        # guard stopped seeing it.
        "name": "embedded-error-record",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            '    if direction == "helix":\n'
            '        _reported = logger.error("scroll_wheel: helix is not '
            'a wheel direction")\n'
            '        return (False, "helix_unsupported")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.12. The round-5 walk required the receiver to be the NAME
        # ``logger``, so any other way of holding the same logger hid the
        # record: an alias here, ``self._log`` in a method, or
        # ``logging.getLogger(__name__).error(...)`` written inline. The
        # handler sees one ERROR record in every case.
        "name": "aliased-logger-error-refusal",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            '    if direction == "gyre":\n'
            "        _log = logger\n"
            '        _log.error("scroll_wheel: gyre is not '
            'a wheel direction")\n'
            '        return (False, "gyre_unsupported")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.13. delegation-walrus-shadowed above rebinds the delegate in
        # the wrapper's own scope. This one reaches the same binding from
        # a NESTED scope, which the round-5 walk stops before: it yields
        # the nested def and descends no further, so the ``nonlocal``
        # inside it was invisible. Runtime-identical -- the rebinding
        # assigns the real primitive back to its own name -- but a later
        # edit could put any callable there, and the wrapper's whole
        # answer would come from something the guard never read.
        "name": "nested-nonlocal-rebinds-delegate",
        "target": HANDLER,
        "old": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "new": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        def _shadow_delegate():\n"
            "            nonlocal scroll_wheel\n"
            "            scroll_wheel = scroll_wheel\n"
            "\n"
            "        _shadow_delegate()\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.13. The second way past the scope boundary: a walrus in a
        # nested function's DEFAULT expression is evaluated in the
        # enclosing scope at def time, so it binds ``scroll_wheel`` in the
        # wrapper. Decorators, annotations and class bases are the same
        # shape. Runtime-identical for the same reason as the nonlocal
        # mutation above.
        "name": "nested-default-walrus-rebinds-delegate",
        "target": HANDLER,
        "old": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "new": (
            "        from utils.win_input_sender import scroll_wheel\n"
            "\n"
            "        def _unused(_held=(scroll_wheel := scroll_wheel)):\n"
            "            return _held\n"
            "\n"
            "        return scroll_wheel(direction, clicks)\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    # -----------------------------------------------------------------
    # Step 2: the blast radius. press_keys and type_string both return
    # None, so their callers cannot see a failure at all and the ERROR
    # record is the only user-visible signal those paths have. Only, and
    # not reliable: ErrorNotificationHandler drops a repeat of the same
    # (logger, level, message) inside ten seconds, so a second failed
    # hotkey tells the user nothing (wh-wheel-refusal-notice.1.14, filed
    # against this branch and out of its scope -- the bead's acceptance
    # criteria hold typing behaviour still). Demoting these two would
    # remove the only signal there is, which is why they are pinned.
    # -----------------------------------------------------------------
    {
        "name": "press-keys-demoted",
        "target": SENDER,
        "old": (
            '            logger.error(f"SendInput failed. Sent '
            '{events_sent}/{num_events} events. '
            'Win32 Error: {kernel32.GetLastError()}")\n'
        ),
        "new": (
            '            logger.warning(f"SendInput failed. Sent '
            '{events_sent}/{num_events} events. '
            'Win32 Error: {kernel32.GetLastError()}")\n'
        ),
        "expect": [
            _radius("test_press_keys_still_logs_an_error_on_a_short_send"),
        ],
    },
    {
        "name": "type-string-demoted",
        "target": SENDER,
        "old": (
            '            logger.error(f"SendInput failed for a chunk. Sent '
            '{sent_events}/{num_events_in_chunk}. '
            'Win32 Error: {kernel32.GetLastError()}")\n'
        ),
        "new": (
            '            logger.warning(f"SendInput failed for a chunk. Sent '
            '{sent_events}/{num_events_in_chunk}. '
            'Win32 Error: {kernel32.GetLastError()}")\n'
        ),
        "expect": [
            _radius("test_type_string_still_logs_an_error_on_a_short_send"),
        ],
    },
    {
        # The pin the bead's review named: click keeps its stuck-button ERROR.
        "name": "click-release-demoted",
        "target": SENDER,
        "old": (
            "                                logger.error(\n"
            '                                    "click_at: compensating '
            '%s release also "\n'
        ),
        "new": (
            "                                logger.warning(\n"
            '                                    "click_at: compensating '
            '%s release also "\n'
        ),
        "expect": ["test_compensating_up_failure_is_logged"],
    },
    # -----------------------------------------------------------------
    # Round 7 (wh-wheel-refusal-notice.1.15, .1.16): loud records the
    # round-6 walk could not see. It read a logging call only where the
    # call's own func was an attribute, and it read records only from
    # STATEMENTS ABOVE the return in the return's own block. Every
    # mutation here was seen to survive that walk.
    # -----------------------------------------------------------------
    {
        # .1.15. The method fetched by name. The outer call's func is a
        # Call, not an Attribute, so _logging_methods returned nothing
        # and the branch read as silent while the running code writes an
        # ERROR record and pops the generic box.
        "name": "getattr-error-refusal",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            '    if direction == "spiral":\n'
            '        getattr(logger, "error")("scroll_wheel: spiral is '
            'not a wheel direction")\n'
            '        return (False, "spiral_unsupported")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.15. The bound method aliased at MODULE level. Nothing
        # inside the producer is an attribute of a logging method at
        # all, so a walk confined to the function body sees a plain
        # call to a local name.
        "name": "module-alias-error-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "_shout = logger.error\n"
                    "\n"
                    "\n"
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    '    if direction == "spiral":\n'
                    '        _shout("scroll_wheel: spiral is not a '
                    'wheel direction")\n'
                    '        return (False, "spiral_unsupported")\n'
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.16. The record in the `if` HEADER that guards the refusal
        # return. logger.error returns None, so the condition is the
        # direction test alone and only a spiral call logs. The walk
        # read the statements above the return inside the if body --
        # there are none -- and never looked at the header that ran to
        # get there.
        "name": "ancestor-condition-error-refusal",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            '    if direction == "spiral" and logger.error(\n'
            '        "scroll_wheel: spiral is not a wheel direction"\n'
            "    ) is None:\n"
            '        return (False, "spiral_unsupported")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.16. The record in a `finally`, which runs on the way OUT of
        # the refusal return rather than above it. _statement_lists
        # hands the finalbody out as its own block, so the walk saw a
        # record that belongs to no return and a return with no record.
        "name": "ancestor-finally-error-refusal",
        "target": SENDER,
        "old": (
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "new": (
            "    try:\n"
            '        if direction == "spiral":\n'
            '            return (False, "spiral_unsupported")\n'
            "    finally:\n"
            '        if direction == "spiral":\n'
            '            logger.error("scroll_wheel: spiral is not a '
            'wheel direction")\n'
            "    if not isinstance(direction, str) or direction not "
            "in _SCROLL_DIRECTIONS:\n"
        ),
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    # -----------------------------------------------------------------
    # Round 8 (wh-wheel-refusal-notice.1.17): the loud record moved OUT
    # of the producer entirely, into a callable the producer calls by
    # name. The round-7 check walks the producer's own subtree and reads
    # module-level ALIASES, so a module-level def carrying the record
    # was invisible from both directions. Every mutation here was seen
    # to survive that check.
    # -----------------------------------------------------------------
    {
        # .1.17, the primitive. A module-level helper is the smallest
        # form of the class and needs no API change: the branch calls a
        # bare name, and the .error reference sits in another function's
        # body.
        "name": "module-helper-error-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "def _spiral_is_not_a_direction():\n"
                    '    logger.error("scroll_wheel: spiral is not a '
                    'wheel direction")\n'
                    "\n"
                    "\n"
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    '    if direction == "spiral":\n'
                    "        _spiral_is_not_a_direction()\n"
                    '        return (False, "spiral_unsupported")\n'
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.17, the WRAPPER. The same shape in the producer that owns
        # the delegation exception, which is where an exemption is most
        # likely to swallow a check: the wrapper is allowed to hand its
        # whole answer to scroll_wheel, and this proves that permission
        # does not extend to any other name it calls.
        "name": "wrapper-helper-error-refusal",
        "edits": [
            {
                "target": HANDLER,
                "old": (
                    "def _win32_scroll_wheel(direction, clicks) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "def _spiral_is_not_a_direction():\n"
                    '    logger.error("_win32_scroll_wheel: spiral is '
                    'not a wheel direction")\n'
                    "\n"
                    "\n"
                    "def _win32_scroll_wheel(direction, clicks) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
            {
                "target": HANDLER,
                "old": (
                    "    try:\n"
                    "        from utils.win_input_sender import "
                    "scroll_wheel\n"
                ),
                "new": (
                    '    if direction == "spiral":\n'
                    "        _spiral_is_not_a_direction()\n"
                    '        return (False, "spiral_unsupported")\n'
                    "    try:\n"
                    "        from utils.win_input_sender import "
                    "scroll_wheel\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.17, the fail-closed direction rather than a caught ERROR.
        # This helper is IMPORTED, so no readable body exists anywhere
        # for the walk to summarise. getLogger writes nothing, so the
        # mutant is quiet at run time and only the guard should object:
        # a callable it cannot follow must fail rather than read as a
        # silent refusal, because the next indirection will look exactly
        # like this one.
        "name": "unanalysable-helper-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": "import logging\nimport time\n",
                "new": (
                    "import logging\n"
                    "from logging import getLogger as _spiral_helper\n"
                    "import time\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    '    if direction == "spiral":\n'
                    "        _spiral_helper()\n"
                    '        return (False, "spiral_unsupported")\n'
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.19, a decorator on the PRODUCER itself. A plain ``@name`` is
        # an ast.Name in decorator_list -- not a Call, not an Attribute --
        # so no branch of the walk reacts to it and the decorator's body
        # is never read. The wrapper here really does answer a refusal
        # with an ERROR record, in place of the function it wraps, which
        # is the escape the walk cannot see. No test passes "spiral", so
        # every existing call runs the real body unchanged.
        "name": "decorated-producer-error-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "def _spiral_watch(func):\n"
                    "    def _wrapped(direction, clicks=1):\n"
                    '        if direction == "spiral":\n'
                    '            logger.error("scroll_wheel: spiral is '
                    'not a wheel direction")\n'
                    '            return (False, "spiral_unsupported")\n'
                    "        return func(direction, clicks)\n"
                    "    return _wrapped\n"
                    "\n"
                    "\n"
                    "@_spiral_watch\n"
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.19, a decorator on a NESTED helper the producer calls.
        # _defined_inside collects nested definitions by name and the walk
        # then skips calls to them as already covered, which is true of
        # the body written here and false of what runs: the decorator
        # replaces that callable with one that writes an ERROR record.
        # Again quiet for every direction any test passes.
        "name": "decorated-nested-helper-error-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "def _spiral_mark(func):\n"
                    "    def _wrapped():\n"
                    '        logger.error("scroll_wheel: spiral is not '
                    'a wheel direction")\n'
                    "    return _wrapped\n"
                    "\n"
                    "\n"
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    "    @_spiral_mark\n"
                    "    def _note_spiral():\n"
                    "        pass\n"
                    "\n"
                    '    if direction == "spiral":\n'
                    "        _note_spiral()\n"
                    '        return (False, "spiral_unsupported")\n'
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.20, the callee COMPUTED rather than named. The walk resolved
        # a call only when its func was a bare name, so a subscript into
        # a list literal reached a module-level helper the walk never
        # read. The helper's own name is an ast.Name inside the list, but
        # bare names are not checked -- only calls through them.
        "name": "container-called-helper-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "def _corkscrew_hidden_record():\n"
                    '    logger.error("scroll_wheel: corkscrew is not a '
                    'wheel direction")\n'
                    "\n"
                    "\n"
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    '    if direction == "corkscrew":\n'
                    "        [_corkscrew_hidden_record][0]()\n"
                    '        return (False, "corkscrew_unsupported")\n'
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.20, the definition REBOUND after it is written. The walk
        # followed a bare-name call to the def of that name in the module
        # body, without checking that module level still leaves the name
        # pointing there. The alias scan does not catch it either: it
        # records an assignment only when the value holds a loud
        # ATTRIBUTE, and this value is a plain name.
        "name": "rebound-helper-error-refusal",
        "edits": [
            {
                "target": SENDER,
                "old": (
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
                "new": (
                    "def _corkscrew_note():\n"
                    "    pass\n"
                    "\n"
                    "\n"
                    "def _corkscrew_loud():\n"
                    '    logger.error("scroll_wheel: corkscrew is not a '
                    'wheel direction")\n'
                    "\n"
                    "\n"
                    "_corkscrew_note = _corkscrew_loud\n"
                    "\n"
                    "\n"
                    "def scroll_wheel(direction, clicks: int = 1) -> "
                    "tuple[bool, str | None]:\n"
                ),
            },
            {
                "target": SENDER,
                "old": (
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
                "new": (
                    '    if direction == "corkscrew":\n'
                    "        _corkscrew_note()\n"
                    '        return (False, "corkscrew_unsupported")\n'
                    "    if not isinstance(direction, str) or direction "
                    "not in _SCROLL_DIRECTIONS:\n"
                ),
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.21, the name rebound by a WALRUS IN A DEF DEFAULT. The
        # round-10 fence skipped every module-level def outright, so
        # a default argument -- which evaluates at import, in the
        # module namespace -- could replace a quiet definition with
        # a loud one while the walk read the quiet body.
        "name": 'default-walrus-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef _corkscrew_init(value=(_corkscrew_note := _corkscrew_loud)):\n    pass\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.21, the name rebound by a MATCH CAPTURE. ast.Match
        # keeps its blocks under .cases, which the round-10 fence
        # never descended into, and a bare `case <name>:` is a
        # capture pattern that binds that name to the subject.
        "name": 'match-capture-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\nmatch _corkscrew_loud:\n    case _corkscrew_note:\n        pass\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.21, the name rebound from a CLASS SUITE DECLARED
        # global. A class body runs at import; an ordinary
        # assignment there is class-local, but one under `global`
        # binds the module name. The round-10 fence skipped every
        # ClassDef, so it saw neither.
        "name": 'class-global-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\nclass _CorkscrewRebind:\n    global _corkscrew_note\n    _corkscrew_note = _corkscrew_loud\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.21, a STAR IMPORT, quiet at run time on purpose. The
        # guard must fail because it cannot know which names the
        # import binds -- not because it saw a record. The
        # round-10 fence bound the literal '*', which matches no
        # definition, so every definition stayed followable.
        "name": 'star-import-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\nfrom ctypes import *\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.22, the loud record inside a METACLASS __mul__ reached
        # through the computed-callee exception. The round-10 walk
        # followed the left name and read its class BODY, never its
        # header, so `metaclass=` pointed at code the walk never
        # opened while the multiplication ran it.
        "name": 'metaclass-mul-error-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'class _CorkscrewMeta(type):\n    def __mul__(cls, count):\n        logger.error("scroll_wheel: corkscrew is not a wheel direction")\n        return lambda *events: None\n\n\nclass _CorkscrewArray(metaclass=_CorkscrewMeta):\n    pass\n\n\ndef _corkscrew_send(events):\n    return (_CorkscrewArray * len(events))(*events)\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_send([])\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.22, the POSITIVE PIN codex asked for, and the only
        # mutation that touches the guard itself. Removing the
        # computed-callee exception must fail the guard on UNMODIFIED
        # shipped source, because utils/win_input_sender.py reaches
        # `(Input * count)(*events)` through _send_mouse_events. A
        # caught verdict here is the proof the exception is
        # load-bearing rather than dead permission.
        "name": "ctypes-callee-exception-removed",
        "target": GUARD,
        "old": '        if (\n            isinstance(func, ast.BinOp)\n            and isinstance(func.op, ast.Mult)\n            and isinstance(func.left, ast.Name)\n        ):\n            return [func.left.id]\n',
        "new": "",
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.24, a class suite whose ``global`` and its binding
        # both sit inside an ``if``. Round 11 read only the
        # suite's outermost statements, so it collected neither
        # and the helper stayed followable. The block runs at
        # import exactly as a direct statement would.
        "name": 'class-nested-if-global-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\nclass _CorkscrewRebind:\n    if True:\n        global _corkscrew_note\n        _corkscrew_note = _corkscrew_loud\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.24, a NON-ASSIGNMENT target form. The ``global``
        # is direct, so round 11 saw the declaration, but it
        # read binding targets only from an assignment and a
        # ``del``; a ``for`` target binds just as surely.
        "name": 'class-global-for-target-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\nclass _CorkscrewRebind:\n    global _corkscrew_note\n    for _corkscrew_note in (_corkscrew_loud,):\n        pass\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.24, a ``global`` in a NESTED class body. Round 11
        # never descended past the outer suite, and the outer
        # suite declares nothing, so the inner rebinding was
        # invisible. Both class bodies run at import.
        "name": 'class-nested-class-global-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\nclass _CorkscrewOuter:\n    class _CorkscrewInner:\n        global _corkscrew_note\n        _corkscrew_note = _corkscrew_loud\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.24, an IMPORT ALIAS as the binding form, quiet at
        # run time on purpose -- like the star import above,
        # the guard must fail because it cannot read what the
        # name now holds, not because it saw a record. Round 11
        # read import aliases at module level and not in a
        # class suite, which is the drift this round closed.
        "name": 'class-global-import-alias-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\nclass _CorkscrewRebind:\n    global _corkscrew_note\n    from logging import warning as _corkscrew_note\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.25, a ``def`` inside a module-level ``if``. Round 12 taught
        # both scans every BINDING form but no DEFINITION form:
        # each scan skips a def outright, and _definitions_in
        # maps only definitions directly in module.body, so the
        # guard followed the first, quiet body.
        "name": 'module-nested-if-def-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\nif True:\n    def _corkscrew_note():\n        logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.25, the same shape with a ``class``. Calling the name now
        # constructs, and __init__ is what logs.
        "name": 'module-nested-if-class-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\nif True:\n    class _corkscrew_note:\n        def __init__(self):\n            logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.25, ``try`` rather than ``if``. _statements_in_scope already
        # descends into a try body; the definition inside it was
        # skipped for the same reason.
        "name": 'module-nested-try-def-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ntry:\n    def _corkscrew_note():\n        logger.error("scroll_wheel: corkscrew is not a wheel direction")\nexcept Exception:\n    pass\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.25, ``async def``, quiet at run time on purpose: calling it
        # only builds a coroutine. Like the import alias in round
        # 12, the guard must fail because the body it read is not
        # the one the name now holds.
        "name": 'module-nested-if-async-def-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\nif True:\n    async def _corkscrew_note():\n        logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.25, a ``def`` as the binding form for a declared ``global``
        # in a class suite. Round 12 covered every assignment-like
        # form here and none of the definition forms.
        "name": 'class-global-def-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\nclass _CorkscrewRebind:\n    global _corkscrew_note\n    def _corkscrew_note():\n        logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.25, the ``class`` form of the same rebinding. The nested
        # class is also its own scope, so the scan must add the
        # name to the ENCLOSING scope and still descend.
        "name": 'class-global-class-rebinds-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\nclass _CorkscrewRebind:\n    global _corkscrew_note\n    class _corkscrew_note:\n        def __init__(self):\n            logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, a same-named PARAMETER. It holds whatever the caller
        # passed, so the module definition of that name describes
        # nothing the call runs. _defined_inside said outright that
        # a parameter does not count as local, and nothing else
        # recorded it either, so the walk followed the module def.
        "name": 'local-parameter-default-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1, _corkscrew_note=_corkscrew_loud) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, a plain local assignment, the shape codex reproduced.
        "name": 'local-assign-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    _corkscrew_note = _corkscrew_loud\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, a local ``for`` target. Module level and a class suite both
        # read this binding form; a producer scope did not.
        "name": 'local-for-target-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    for _corkscrew_note in (_corkscrew_loud,):\n        pass\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, a local ``with ... as`` target, the same omission.
        "name": 'local-with-as-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'import contextlib\n\n\ndef _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    with contextlib.nullcontext(_corkscrew_loud) as _corkscrew_note:\n        pass\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, a local walrus. Round 11 taught the MODULE scan to read a
        # walrus; a producer scope still did not.
        "name": 'local-walrus-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if (_corkscrew_note := _corkscrew_loud):\n        pass\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, the INVERSE direction. The module helper is loud, and a
        # quiet def of the same name sits inside a function nested
        # in scroll_wheel. That nested def cannot bind
        # scroll_wheel's own call, but _defined_inside collected it
        # through ast.walk and the walk then resolved nothing.
        "name": 'nested-def-only-suppresses-resolution-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    def _corkscrew_inner():\n        def _corkscrew_note():\n            pass\n        return _corkscrew_note\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, the same with ``async def``.
        "name": 'nested-async-def-only-suppresses-resolution-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    def _corkscrew_inner():\n        async def _corkscrew_note():\n            pass\n        return _corkscrew_note\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.26, the same with ``class``.
        "name": 'nested-class-only-suppresses-resolution-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    def _corkscrew_inner():\n        class _corkscrew_note:\n            pass\n        return _corkscrew_note\n    if direction == "corkscrew":\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, a nested FUNCTION binds the callee. Round 14 gave the
        # producer its own bindings; _loud_record_sites still
        # crossed every child scope with ast.walk and resolved
        # what it found there against the producer's sets.
        "name": 'nested-def-scope-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_inner():\n            _corkscrew_note = _corkscrew_loud\n            _corkscrew_note()\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, the same with ``async def``. It is never awaited, so this
        # one is QUIET on purpose -- the defect is that the body
        # the guard read is not the one the name holds.
        "name": 'nested-async-def-scope-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        async def _corkscrew_inner():\n            _corkscrew_note = _corkscrew_loud\n            _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, a class SUITE binds the callee. The suite runs where it is
        # written, in a namespace of its own.
        "name": 'nested-class-scope-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        class _CorkscrewInner:\n            _corkscrew_note = _corkscrew_loud\n            _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, a LAMBDA parameter binds the callee. A lambda holds one
        # expression rather than statements, and it was the one
        # scope kind with no statement for the walk to read.
        "name": 'lambda-parameter-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        list(map(\n            lambda value, _corkscrew_note=_corkscrew_loud:\n            _corkscrew_note(),\n            [None],\n        ))\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, a list comprehension TARGET binds the callee. The shipped
        # scroll_wheel already holds a comprehension, safe today
        # only because its target is named underscore.
        "name": 'list-comprehension-target-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        [_corkscrew_note()\n         for _corkscrew_note in (_corkscrew_loud,)]\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, the same in a set comprehension.
        "name": 'set-comprehension-target-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        {_corkscrew_note()\n         for _corkscrew_note in (_corkscrew_loud,)}\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, the same in a dict comprehension, whose key and value both
        # run in the comprehension's own scope.
        "name": 'dict-comprehension-target-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        {index: _corkscrew_note()\n         for index, _corkscrew_note in ((0, _corkscrew_loud),)}\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, the same in a generator expression.
        "name": 'generator-comprehension-target-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        list(_corkscrew_note()\n             for _corkscrew_note in (_corkscrew_loud,))\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, a nested function's own PARAMETER binds the callee. Round
        # 14 read the producer's parameters and no child scope's.
        "name": 'nested-def-parameter-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_inner(_corkscrew_note=_corkscrew_loud):\n            _corkscrew_note()\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.27, a comprehension inside a nested function, which is two
        # scopes down from the producer.
        "name": 'comprehension-in-nested-def-shadows-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_inner():\n            return [_corkscrew_note()\n                    for _corkscrew_note in (_corkscrew_loud,)]\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, a nested function declares the name ``global``, so it
        # reaches the loud MODULE helper while the producer's own
        # quiet definition of that name sits right beside it.
        "name": 'global-in-nested-def-reaches-loud-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_loud():\n            pass\n        def _corkscrew_inner():\n            global _corkscrew_loud\n            _corkscrew_loud()\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, the same with ``async def``, never awaited, so quiet on
        # purpose -- the defect is which body the name holds.
        "name": 'global-in-nested-async-def-reaches-loud-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_loud():\n            pass\n        async def _corkscrew_inner():\n            global _corkscrew_loud\n            _corkscrew_loud()\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, the same in a class suite, which runs where it is
        # written in a namespace of its own.
        "name": 'global-in-nested-class-suite-reaches-loud-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_loud():\n            pass\n        class _CorkscrewInner:\n            global _corkscrew_loud\n            _corkscrew_loud()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, the declaration sits inside an ``if`` of the nested
        # function rather than at its top.
        "name": 'global-in-compound-block-reaches-loud-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_loud():\n            pass\n        def _corkscrew_inner():\n            if clicks:\n                global _corkscrew_loud\n                _corkscrew_loud()\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, the declaration sits two scopes below the producer.
        "name": 'global-two-scopes-down-reaches-loud-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_loud():\n            pass\n        def _corkscrew_middle():\n            def _corkscrew_inner():\n                global _corkscrew_loud\n                _corkscrew_loud()\n            _corkscrew_inner()\n        _corkscrew_middle()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, the declaration sits in a method of a nested class, which
        # is a function scope inside a scope that does not close.
        "name": 'global-in-class-method-reaches-loud-helper-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_loud():\n            pass\n        class _CorkscrewInner:\n            def method(self):\n                global _corkscrew_loud\n                _corkscrew_loud()\n        _CorkscrewInner().method()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.28, a nested scope rebinds the producer's own definition
        # through ``nonlocal`` and the PRODUCER calls it
        # afterwards. Seen from inside the nested scope the walk
        # already refused; seen from out here it did not.
        "name": 'nonlocal-rebinding-read-by-the-producer-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_note():\n            pass\n        def _corkscrew_inner():\n            nonlocal _corkscrew_note\n            _corkscrew_note = _corkscrew_loud\n        _corkscrew_inner()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.29, a PEP 695 type parameter bound, which is executable code
        # in an annotation scope. Reading __bound__ evaluates it.
        "name": 'generic-def-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_helper[T: _corkscrew_loud()]():\n            pass\n        _corkscrew_helper.__type_params__[0].__bound__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.29, the same on an ``async def``.
        "name": 'generic-async-def-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        async def _corkscrew_helper[T: _corkscrew_loud()]():\n            pass\n        _corkscrew_helper.__type_params__[0].__bound__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.29, the same on a ``class``.
        "name": 'generic-class-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        class _corkscrew_helper[T: _corkscrew_loud()]:\n            pass\n        _corkscrew_helper.__type_params__[0].__bound__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.29, the same two scopes below the producer.
        "name": 'generic-nested-two-scopes-down-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_inner():\n            def _corkscrew_helper[T: _corkscrew_loud()]():\n                pass\n            _corkscrew_helper.__type_params__[0].__bound__\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.29, the type parameter list is on the PRODUCER itself, which
        # the walk reads with header_read False.
        "name": 'generic-producer-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel[T: _corkscrew_loud()](direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        scroll_wheel.__type_params__[0].__bound__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.29, the type parameter list is on a MODULE definition the
        # producer calls by name, which the walk follows.
        "name": 'generic-module-definition-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef _corkscrew_generic[T: _corkscrew_loud()]():\n    _corkscrew_generic.__type_params__[0].__bound__\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_generic()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, a nested function declares the name ``global`` and
        # ASSIGNS to it; the producer calls the name afterwards.
        # The rebinding scope writes no call, so nothing is
        # reported there.
        "name": 'nested-def-global-write-read-by-the-producer-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_mutate():\n            global _corkscrew_note\n            _corkscrew_note = _corkscrew_loud\n        _corkscrew_mutate()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, the same with ``async def``, never awaited, so quiet
        # on purpose.
        "name": 'nested-async-def-global-write-read-by-the-producer-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        async def _corkscrew_mutate():\n            global _corkscrew_note\n            _corkscrew_note = _corkscrew_loud\n        _corkscrew_mutate()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, the same in a class suite, which runs where it is
        # written.
        "name": 'nested-class-global-write-read-by-the-producer-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        class _CorkscrewMutate:\n            global _corkscrew_note\n            _corkscrew_note = _corkscrew_loud\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, the write is a ``for`` target rather than an
        # assignment.
        "name": 'nested-global-write-through-a-for-target-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_mutate():\n            global _corkscrew_note\n            for _corkscrew_note in (_corkscrew_loud,):\n                pass\n        _corkscrew_mutate()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, the write is a ``with ... as`` target.
        "name": 'nested-global-write-through-a-with-as-target-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        import contextlib\n        def _corkscrew_mutate():\n            global _corkscrew_note\n            with contextlib.nullcontext(\n                _corkscrew_loud\n            ) as _corkscrew_note:\n                pass\n        _corkscrew_mutate()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, the write sits two scopes below the producer.
        "name": 'nested-global-write-two-scopes-down-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_middle():\n            def _corkscrew_mutate():\n                global _corkscrew_note\n                _corkscrew_note = _corkscrew_loud\n            _corkscrew_mutate()\n        _corkscrew_middle()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, a SIBLING nested scope reads the name, not the
        # producer, so neither scope holds both halves.
        "name": 'nested-global-write-read-by-a-sibling-scope-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_mutate():\n            global _corkscrew_note\n            _corkscrew_note = _corkscrew_loud\n        def _corkscrew_reader():\n            _corkscrew_note()\n        _corkscrew_mutate()\n        _corkscrew_reader()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.30, the write sits inside a MODULE definition the producer
        # calls by name.
        "name": 'module-definition-global-write-read-by-the-producer-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef _corkscrew_prepare():\n    def _corkscrew_mutate():\n        global _corkscrew_note\n        _corkscrew_note = _corkscrew_loud\n    _corkscrew_mutate()\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_prepare()\n        _corkscrew_note()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.31, a MODULE-level generic ``type`` statement, whose bound
        # is evaluated by reading __bound__.
        "name": 'module-generic-type-alias-bound-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ntype _CorkscrewAlias[T: _corkscrew_loud()] = int\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _CorkscrewAlias.__type_params__[0].__bound__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.31, a MODULE-level plain ``type`` statement, whose value is
        # evaluated by reading __value__.
        "name": 'module-plain-type-alias-value-runs-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ntype _CorkscrewAlias = _corkscrew_loud()\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _CorkscrewAlias.__value__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.31, the same read written inside a nested scope of the
        # producer.
        "name": 'module-type-alias-value-read-in-a-nested-scope-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ntype _CorkscrewAlias = _corkscrew_loud()\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_inner():\n            _CorkscrewAlias.__value__\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.31, the same read written inside a module definition the
        # producer calls by name.
        "name": 'module-type-alias-value-read-by-a-module-definition-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ntype _CorkscrewAlias = _corkscrew_loud()\n\n\ndef _corkscrew_prepare():\n    _CorkscrewAlias.__value__\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_prepare()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.31, a producer-local plain alias CALLED through
        # ``__value__()``, which the attribute-callee boundary
        # let through.
        "name": 'producer-local-type-alias-called-through-value-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_note():\n    pass\n\n\ndef _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        type _CorkscrewLocal = _corkscrew_loud\n        _CorkscrewLocal.__value__()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.32, the branch reads a stored CONSTRAINED type parameter's
        # __constraints__, which runs the constraint
        # expressions. A type parameter carries a bound or a set
        # of constraints, never both, so the round-17 set naming
        # only __bound__ left this alternative open.
        "name": 'stored-type-parameter-constraints-on-the-refusal-path',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n    return int\n\n\ndef _corkscrew_holder[T: (_corkscrew_loud(), str)]():\n    pass\n\n\n_CorkscrewParam = _corkscrew_holder.__type_params__[0]\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _CorkscrewParam.__constraints__\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.32, the same read written inside a nested scope of the
        # producer.
        "name": 'stored-type-parameter-constraints-read-in-a-nested-scope-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n    return int\n\n\ndef _corkscrew_holder[T: (_corkscrew_loud(), str)]():\n    pass\n\n\n_CorkscrewParam = _corkscrew_holder.__type_params__[0]\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        def _corkscrew_inner():\n            _CorkscrewParam.__constraints__\n        _corkscrew_inner()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
    {
        # .1.32, the same read written inside a module definition the
        # producer calls by name.
        "name": 'stored-type-parameter-constraints-read-by-a-module-definition-refusal',
        "edits": [
            {
                "target": SENDER,
                "old": 'def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
                "new": 'def _corkscrew_loud():\n    logger.error("scroll_wheel: corkscrew is not a wheel direction")\n    return int\n\n\ndef _corkscrew_holder[T: (_corkscrew_loud(), str)]():\n    pass\n\n\n_CorkscrewParam = _corkscrew_holder.__type_params__[0]\n\n\ndef _corkscrew_prepare():\n    _CorkscrewParam.__constraints__\n\n\ndef scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:\n',
            },
            {
                "target": SENDER,
                "old": '    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
                "new": '    if direction == "corkscrew":\n        _corkscrew_prepare()\n        return (False, "corkscrew_unsupported")\n    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:\n',
            },
        ],
        "expect": [
            _structure(
                "test_the_published_list_matches_every_refusal_branch"
                "_in_the_source"
            ),
        ],
    },
]

# Every test this bead added, plus the one existing test its review named as
# the blast-radius pin. The check below refuses to run unless each of these
# is either broken by some mutation or named in NOT_MUTATED with a reason.
# It is deliberately NOT every test in the four files: the earlier scroll
# bead's tests live there too, and claiming them here would be an over-claim.
FEATURE_TESTS = {
    _notice("test_a_short_send_shows_one_written_notice"),
    _notice("test_a_sendinput_error_shows_one_written_notice"),
    _notice("test_an_unknown_refusal_reason_still_shows_the_notice"),
    _notice("test_a_raising_seam_shows_the_notice_and_logs_a_warning"),
    _notice("test_a_successful_scroll_shows_no_notice"),
    _notice("test_an_invalid_direction_shows_one_written_notice"),
    _notice("test_an_invalid_notch_count_shows_one_written_notice"),
    _notice("test_the_notice_carries_the_shared_title"),
    _notice("test_a_raising_notice_never_escapes_the_handler"),
    _notice("test_the_notice_says_the_exact_words_the_user_hears"),
    _source("test_send_notice_names_the_caller_in_its_log_line"),
    _source("test_send_notice_defaults_to_the_continuous_scroll"),
    _source("test_the_handler_names_the_discrete_scroll_when_it_reports"),
    _level("test_a_short_send_logs_a_warning_and_no_error"),
    _level("test_a_raising_send_logs_a_warning_and_no_error"),
    _level("test_the_refused_wheel_call_still_writes_a_line"),
    _level("test_an_unknown_direction_logs_a_warning_not_an_error"),
    _level("test_an_unsupported_notch_count_logs_a_warning_not_an_error"),
    _radius("test_press_keys_still_logs_an_error_on_a_short_send"),
    _radius("test_type_string_still_logs_an_error_on_a_short_send"),
    _contract("test_every_error_logged_refusal_is_listed_and_no_other_is"),
    _owner("test_the_handler_reads_the_primitive_s_own_list"),
    _structure(
        "test_the_published_list_matches_every_refusal_branch_in_the_source"
    ),
    _seam("test_the_discrete_scroll_shows_one_notice_and_logs_no_error"),
    _seam(
        "test_the_continuous_scroll_shows_one_notice_and_logs_no_error"
    ),
    "test_compensating_up_failure_is_logged",
}

# Tests this gate deliberately does not break, each with the reason. A reason
# must state a property of the behaviour itself. It may NOT cite another
# mutation ("already broken by X"): that claim is unverifiable where it is
# written and unchecked where it is read.
NOT_MUTATED = {}


def _edits(mutation):
    """Every (target, old, new) this mutation applies, in order."""
    if "edits" in mutation:
        return [(e["target"], e["old"], e["new"]) for e in mutation["edits"]]
    return [(mutation["target"], mutation["old"], mutation["new"])]


def _run_pytest(test_files):
    """Run every named test file in ONE pytest process."""
    env = dict(os.environ)
    # Python decides whether cached bytecode is current from the source
    # file's (mtime, size). Several mutations here keep the file the same
    # length -- logger.error and logger.warning differ by two characters,
    # and one keeps the length exactly -- so the cache is disabled rather
    # than relied on.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *test_files, "-q", "-rf",
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _node_key(node_id):
    """The part of a pytest node id after the file path, without parameters.

    ``Class::method`` for a class-based test, ``method`` for a module-level
    one. The bare method name is not enough: this bead puts
    test_a_short_send_shows_one_written_notice and
    test_a_short_send_logs_a_warning_and_no_error in different classes, and
    two more classes hold same-shaped names.
    """
    return node_id.split("::", 1)[1].split("[")[0].strip()


def _failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            node_id = line.split(" ", 1)[1].split(" ")[0]
            if "::" in node_id:
                names.add(_node_key(node_id))
    return names


def _coverage_errors(every_test):
    """Check that every test this bead added is mutated or excluded by name.

    Five ways the claim can stop being true, all reported here:

    1. A FEATURE_TESTS entry that no mutation breaks and NOT_MUTATED does not
       name. That is the over-claim itself.
    2. A FEATURE_TESTS entry that no longer exists, so this file names a test
       that is not there.
    3. NOT_MUTATED names a test that no longer exists.
    4. A name is in NOT_MUTATED and in some mutation's expect list, so the
       file says both "covered" and "deliberately not covered".
    5. A reason cites another mutation by name. That claim is unverifiable
       where it is written and unchecked where it is read, so the shape is
       banned rather than each instance corrected.
    """
    errors = []
    expected = {
        name for mutation in MUTATIONS for name in mutation["expect"]
    }

    for name in sorted(FEATURE_TESTS):
        if name not in every_test:
            errors.append(f"FEATURE_TESTS names {name}, which does not exist")
            continue
        if name not in expected and name not in NOT_MUTATED:
            errors.append(
                f"{name} is broken by no mutation and NOT_MUTATED does not "
                "name it"
            )

    for name, reason in sorted(NOT_MUTATED.items()):
        if name not in every_test:
            errors.append(f"NOT_MUTATED names {name}, which does not exist")
        if not reason.strip():
            errors.append(f"NOT_MUTATED entry {name} carries no reason")
        if name in expected:
            errors.append(
                f"{name} is in NOT_MUTATED and in a mutation's expect list"
            )
        lowered = reason.lower()
        for mutation in MUTATIONS:
            if mutation["name"] in lowered:
                errors.append(
                    f"NOT_MUTATED entry {name} cites mutation "
                    f"{mutation['name']}; a reason must state a property of "
                    "the behaviour, not point at another mutation"
                )
    return errors


def _apply(mutation, sources):
    """Return (working sources, error string or None).

    The sweep and --check both go through here, so a pattern --check calls
    current can never be one the sweep refuses. Copied from
    tests/mutation_gate_keyboard_refusal_notice.py (_apply, line 1807).
    """
    working = dict(sources)
    for target, old, new in _edits(mutation):
        count = working[target].count(old)
        if count == 0:
            return None, f"{mutation['name']}: pattern matched 0 times, " \
                         "need 1"
        if count > 1:
            return None, f"{mutation['name']}: pattern matched {count} " \
                         "times, need 1"
        working[target] = working[target].replace(old, new, 1)
    for target, mutated in working.items():
        if mutated == sources[target]:
            continue
        try:
            compile(mutated, str(target), "exec")
        except SyntaxError as exc:
            return None, f"{mutation['name']}: mutant does not compile: {exc}"
    return working, None


def _check(sources):
    """Answer "are the patterns current, and does each mutant parse".

    Three counts, kept apart, because each is a different repair:

    * stale -- the pattern matches nothing. A later fix rewrote the source.
      Refresh the pattern; do not delete the mutation, because the behaviour
      it protects is usually still there under new code.
    * ambiguous -- the pattern matches more than once. ``str.replace(o, n, 1)``
      would edit the FIRST match, so the mutation lands somewhere its name
      does not claim. Add the neighbouring line that makes the snippet
      unique rather than loosening it. This is the defect
      wh-gate-wheel-refusal-pattern-repair exists to fix: after 43c2b931 the
      notice-raise-escapes except line matched ui_action_handler.py twice.
    * does not compile -- the worst of the three, because in a full sweep it
      reads as ``caught``. The interpreter rejected the mutant before a
      single test ran, so nothing was proven.

    Summing them would hide which one this run found.
    """
    stale, ambiguous, broken = [], [], []
    for mutation in MUTATIONS:
        _, error = _apply(mutation, sources)
        if error is None:
            continue
        if "does not compile" in error:
            broken.append(error)
        elif "matched 0 times" in error:
            stale.append(error)
        else:
            ambiguous.append(error)
    for line in stale + ambiguous + broken:
        print(f"ERROR {line}")
    print(f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
          f"{len(ambiguous)} ambiguous, {len(broken)} that do not compile")
    return 1 if stale or ambiguous or broken else 0


def main(argv):
    # A name on the command line runs that mutation alone. The scope line at
    # the end says which subset ran, so a partial run cannot be read as a
    # full sweep.
    args = list(argv[1:])
    # --check reads every pattern in the file, never the named subset:
    # a check of three patterns answers nothing about the other 112.
    check_only = "--check" in args
    selected = [] if check_only else [a for a in args if a != "--check"]
    if selected:
        known = {m["name"] for m in MUTATIONS}
        unknown = [name for name in selected if name not in known]
        if unknown:
            print(f"ERROR: no such mutation: {unknown}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in selected]
    else:
        mutations = list(MUTATIONS)

    duplicate = [
        m["name"] for m in MUTATIONS
        if [x["name"] for x in MUTATIONS].count(m["name"]) > 1
    ]
    if duplicate:
        print(f"ERROR: duplicate mutation names: {sorted(set(duplicate))}")
        return 1

    targets = {
        target for mutation in mutations for target, _, _ in _edits(mutation)
    }
    originals = {}
    for target in targets:
        raw = target.read_bytes()
        if b"\r\n" in raw:
            # The patterns above are written with LF. A file stored with CRLF
            # makes every multi-line pattern miss in a batch, which reads as a
            # survivor. Refuse rather than rewrite the file's endings, and
            # check per target: this repository can mix conventions per file.
            print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
            return 1
        originals[target] = raw
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    if check_only:
        return _check(sources)

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation. A name
    # that no longer exists can never appear in the failed set, so a genuine
    # catch would be reported as a survivor.
    every_test = set()
    for test_file in FEATURE_TEST_FILES:
        collect = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=SERVICE_DIR, capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S,
        )
        every_test |= {
            _node_key(line.strip())
            for line in collect.stdout.splitlines() if "::" in line
        }
    for mutation in mutations:
        for name in mutation["expect"]:
            if name not in every_test:
                errors.append(f"{mutation['name']}: no such test {name}")
    errors.extend(_coverage_errors(every_test))
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation.
    baseline = _run_pytest(FEATURE_TEST_FILES)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print(f"baseline green; running {len(mutations)} mutations")

    try:
        for mutation in mutations:
            name = mutation["name"]
            working, error = _apply(mutation, sources)
            if working is None:
                errors.append(str(error))
                print(f"ERROR {error}")
                continue

            changed = {
                target: source for target, source in working.items()
                if source != sources[target]
            }

            for target, mutated in changed.items():
                target.write_bytes(mutated.encode("utf-8"))
            try:
                result = _run_pytest(FEATURE_TEST_FILES)
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: mutant never terminated")
                print(f"ERROR {name}: mutant never terminated")
                continue
            finally:
                for target in changed:
                    target.write_bytes(originals[target])

            combined = result.stdout + result.stderr
            if "+++ Timeout +++" in combined:
                # pytest-timeout kills the process before the -rf summary
                # prints, so a parser would find no FAILED lines and report a
                # survivor for a mutation that may have been caught.
                errors.append(f"{name}: suite-timeout abort, no verdict")
                print(f"ERROR {name}: suite-timeout abort")
                continue

            failed = _failed_names(combined)
            expect = set(mutation["expect"])
            collateral = set(mutation.get("also_fails", {}))
            missing = sorted(expect - failed)
            # The expect list is an EXACT claim, not a lower bound. A test
            # that falls over on a shared precondition has not established
            # its own behaviour under the mutation, so every failure must be
            # declared, either as proof (expect) or as collateral
            # (also_fails, with the reason it proves nothing).
            undeclared = sorted(failed - expect - collateral)
            stale = sorted(collateral - failed)
            if missing:
                survivors.append(
                    f"{name}: expected failures missing: {missing}"
                )
                print(f"SURVIVED {name}: {missing} did not fail")
            elif undeclared or stale:
                if undeclared:
                    errors.append(
                        f"{name}: these tests failed and are in neither "
                        f"expect nor also_fails: {undeclared}"
                    )
                    print(f"ERROR    {name}: undeclared failures {undeclared}")
                if stale:
                    errors.append(
                        f"{name}: also_fails names {stale}, which did not "
                        "fail; the declaration is stale"
                    )
                    print(f"ERROR    {name}: stale also_fails {stale}")
            else:
                caught.append(name)
                if collateral:
                    print(
                        f"caught   {name}: {sorted(expect)} "
                        f"(also, proving nothing: {sorted(collateral)})"
                    )
                else:
                    print(f"caught   {name}: {sorted(expect)}")
    finally:
        for target, raw in originals.items():
            target.write_bytes(raw)

    if len(mutations) == len(MUTATIONS):
        scope = f"all {len(MUTATIONS)} mutations for this bead, none skipped"
    else:
        ran = {m["name"] for m in mutations}
        skipped = [m["name"] for m in MUTATIONS if m["name"] not in ran]
        scope = (
            f"{len(mutations)} of {len(MUTATIONS)} mutations, named on the "
            f"command line. NOT RUN in this pass: {skipped}"
        )
    print(
        f"\nscope: {scope}."
        f"\ncaught {len(caught)}, survivors {len(survivors)}, "
        f"errors {len(errors)}"
    )
    for line in survivors + errors:
        print(f"  {line}")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
