"""Run the procedure: load, speech, log, verdict.

Everything in this module touches the machine -- config files, subprocesses,
the performance counters, the speakers, the log. The judgement it prints
comes from script.py, logparse.py, judge.py and report.py, none of which
touch any of that, which is why those can be driven by tests and this cannot.

Three rules shape what happens here.

The speech reaches the recognizer through the real microphone. No file is
handed to the recognizer. A test that fed audio in directly would measure the
recognizer and miss the whole capture half of the question the procedure
exists to answer.

By default a PERSON speaks the six sentences, prompted one at a time.
ENABLE_AUDIO_SUPPRESSION is on in services/wheelhouse/config.toml, and
WheelHouse stops listening while anything plays through the speakers, so a
recording played out of them would be heard by nothing and the run would
report an empty log. The recorded playback is kept behind --playback for a
future headset loop, where the sound does not reach the machine's speakers.
The silences the recording carried become silences the prompts enforce.

Only the processes this run started are stopped. The spinners are held as
process objects from the moment they are created, so stopping them cannot
reach anything else on the machine -- which matters, because python.exe is
not a unique name here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import tomllib
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import judge, logparse, record, report, script
from .script import DEFAULT_ENDPOINT_SILENCE_MS

# How busy the machine has to be before the run will play anything. The
# procedure measured 100.0% on the reference machine with one spinner per
# logical core; anything far below that means the load did not start and the
# run would measure an idle machine while reporting a loaded one.
SATURATION_FLOOR_PERCENT = 90.0

# Above this, a baseline run is not measuring an idle machine.
BASELINE_CEILING_PERCENT = 30.0

# Seconds to let the spinners settle before reading the counter, matching the
# procedure's own five-second settle.
SETTLE_S = 5.0

# How long to wait for the operator to restart the application after a flag
# was flipped, and how long to let the log catch up after playback.
RESTART_TIMEOUT_S = 300.0
LOG_SETTLE_S = 15.0
# How many times the run tries to read its own log before it gives up, and
# how long it waits between tries. ConcurrentRotatingFileHandler renames
# wheelhouse.log and then creates the replacement; a read landing between
# the two meets a sharing violation that clears in milliseconds.
LOG_READ_ATTEMPTS = 20
LOG_READ_RETRY_INTERVAL_S = 0.5

# One busy loop, one process. A module constant rather than a literal
# inside start_spinners so the test file can replace it for every test
# at once: a test that reaches start_spinners with this command starts
# os.cpu_count() busy loops on whatever machine runs the suite, and a
# mutant that loses hold of them leaves them running after the run
# (wh-load-test-script.2 -- Ikon hung five times under exactly that).
SPINNER_ARGV = [sys.executable, '-c', 'while True: pass']

BASELINE_NAME = 'baseline.json'

# What an operator has to do when the tool could not remove the old
# calibration itself. One sentence in one place, because the two paths that
# reach it -- a filesystem that refused the removal, and a Ctrl+C that
# arrived before it finished -- leave the operator with the same file and
# the same job, and two copies of it would drift.
DELETE_THE_BASELINE_YOURSELF = (
    'Delete that file yourself before the next load run: until it is gone, '
    'that run can reuse a calibration this run did not make.')


@dataclass(frozen=True)
class Paths:
    """Where the run reads and writes."""
    repo_root: Path
    sherpa_config: Path
    app_config: Path
    log_file: Path


def find_paths(repo_root: Optional[Path] = None) -> Paths:
    """The four paths the run needs.

    They default to this file's own checkout, which is right whenever the
    tool and the running WheelHouse come from the same one. Pass a root to
    measure a DIFFERENT checkout: a copy of this tool in a git worktree
    would otherwise flip a flag in a config file the running provider never
    reads, and then wait for a log line in a file the application never
    writes. All four paths move together, because a run that took its config
    from one checkout and its log from another would report on somebody
    else's lines.
    """
    repo_root = (Path(__file__).resolve().parents[5] if repo_root is None
                 else Path(repo_root).resolve())
    return Paths(
        repo_root=repo_root,
        sherpa_config=(repo_root / 'services' / 'stt_providers'
                       / 'sherpa_offline_parakeet_stt_server'
                       / 'config.toml'),
        app_config=repo_root / 'services' / 'wheelhouse' / 'config.toml',
        log_file=repo_root / 'wheelhouse.log',
    )


def read_endpoint_silence_ms(config_path: Path) -> float:
    """The provider's endpoint threshold, or the value its code defaults to.

    Never raises: a missing or malformed file means the provider itself is
    running on its coded default, which is the value to build the recording
    against.
    """
    try:
        with open(config_path, 'rb') as handle:
            value = tomllib.load(handle).get('engine', {}).get(
                'endpoint_silence_ms')
    except (OSError, ValueError):
        return float(DEFAULT_ENDPOINT_SILENCE_MS)
    # bool before the numeric check, because bool is a subclass of int and
    # `endpoint_silence_ms = true` is valid TOML: it would otherwise read as
    # a 1 ms threshold, leaving a 3 ms gap between sentences and joining
    # them into one utterance. isfinite because `inf` is valid TOML too, and
    # it reaches time.sleep, which raises OverflowError.
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0):
        return float(value)
    return float(DEFAULT_ENDPOINT_SILENCE_MS)


def set_toml_flag(text: str, key: str, value: bool) -> str:
    """Return ``text`` with ``key`` set to ``value``, and nothing else changed.

    A line edit rather than a parse-and-rewrite, because these files are
    mostly comments explaining what each setting is for and a round trip
    through tomllib would delete all of it.

    Refuses on anything but exactly one match. A key that appears twice, or
    not at all, means this is not the file this function was written for, and
    editing the first match would silently change the wrong setting.
    """
    # Horizontal whitespace only around the value. `\s*$` under MULTILINE
    # would swallow the newline that ends the line, because \s matches it and
    # $ also matches at the end of the whole string -- the rewritten file then
    # loses its final newline, or joins two lines together.
    pattern = re.compile(
        rf'^([^\S\n]*{re.escape(key)}[^\S\n]*=[^\S\n]*)(true|false)[^\S\n]*$',
        re.MULTILINE)
    found = pattern.findall(text)
    if len(found) != 1:
        raise ValueError(
            f'{key} appears {len(found)} times as a boolean; expected once')
    return pattern.sub(lambda m: f'{m.group(1)}{str(value).lower()}', text, 1)


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace the whole file in one step, or not at all.

    A plain write truncates the file first, so an interrupt in the middle
    leaves bytes that match neither the original nor the intended content --
    and the restore below then reads that as somebody else's edit and
    refuses to touch it, which is the worst of the three outcomes. Writing a
    neighbouring file and moving it over the target makes the change
    all-or-nothing. ``os.replace`` is atomic on Windows and on POSIX.

    The temporary file is removed when the move does not happen, so a failed
    write leaves nothing beside a config file the application reads.
    """
    # Named for this process. Two copies of the tool, or a gate run
    # beside one, used to write to the same neighbouring path and could
    # each move the other's half-written file over the target.
    temp = path.with_name(f'{path.name}.stt-load-test.{os.getpid()}.tmp')
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class FlagEdit:
    """Flip a boolean in a config file, and put it back afterwards.

    The restore is checked. If the file no longer holds exactly what this
    object wrote -- another session edited it, or the operator did -- the
    original is NOT written back over their change; the path and the value to
    restore are printed instead. Clobbering someone else's edit to tidy up
    after a test is a worse outcome than a flag left on.

    crewcut: the check and the replacement are not one transaction. A save
    landing between the read and ``os.replace`` -- in ``apply`` after the
    original bytes are read, or in ``restore`` after the guard is
    satisfied -- is overwritten. Removing that needs a per-path
    interprocess lock around the whole read-check-write. It is not built,
    on two grounds. A lock binds only writers that take it, and the writers
    this race involves are a human editor and other agent sessions saving
    the file, which never will. And two concurrent copies of this tool on
    one host is already an invalid run, because each loads the CPU the
    other is measuring. Ruling of 2026-08-30 on wh-load-test-script.1.12.
    """

    def __init__(self, path: Path, key: str, value: bool):
        self.path = path
        self.key = key
        self.value = value
        self._original: Optional[bytes] = None
        self._written: Optional[bytes] = None
        self.changed = False

    def apply(self) -> bool:
        """Set the flag. True when the file was actually changed."""
        original = self.path.read_bytes()
        updated = set_toml_flag(
            original.decode('utf-8'), self.key, self.value).encode('utf-8')
        if updated == original:
            return False
        self._original = original
        self._written = updated
        # Claim the cleanup BEFORE the write, not after it. An interrupt
        # arriving between the write and this assignment used to leave the
        # file flipped while ``changed`` was still false, so ``restore``
        # returned at its first line and the run ended with
        # log_load_diagnostics -- or, on a --log-transcripts run,
        # LOG_TRANSCRIPTS -- still turned on. Both bytes are already
        # recorded above, so claiming it early costs nothing: a write that
        # never lands is recognised below by the bytes on disk.
        self.changed = True
        _atomic_write(self.path, updated)
        return True

    def restore(self) -> bool:
        """Put the flag back. True when the original bytes were written.

        The answer is about the WRITE, not about whether cleanup is
        finished. Those two came apart in the case that matters: ``apply``
        claims the cleanup before it writes, so a write that raised left
        ``changed`` true over a file still holding the original. Cleanup
        here is complete -- there is nothing to put back -- but nothing was
        written, and ``restore_all`` used to read the pair (was changed, is
        not changed) as a completed restoration and announce a flag that
        never turned on.
        """
        if not self.changed or self._original is None:
            return False
        current = self.path.read_bytes()
        if current == self._original:
            # The write never landed. ``apply`` claims the cleanup before it
            # writes, so this is the ordinary interrupted-early case and not
            # a concurrent edit; there is nothing to put back and nothing to
            # warn about.
            self.changed = False
            return False
        if current != self._written:
            print(f'[!] {self.path} changed while the test ran; leaving it '
                  f'alone. Set {self.key} back to '
                  f'{str(not self.value).lower()} yourself.')
            return False
        _atomic_write(self.path, self._original)
        self.changed = False
        return True


def _say(out, message: str, interrupt):
    """Say one cleanup line, holding a Ctrl+C that lands while it prints.

    Returns the interrupt to carry on with: the one already held, or the
    one this call caught. ``restore_all`` promises a second Ctrl+C is
    deferred until the whole cleanup AND its notices are done, and every
    ``out`` call used to sit outside a try. An interrupt from there left
    the remaining flags unrestored, or suppressed the restart and
    dictated-text notices and the re-raise of an interrupt already stored.

    The retry is bounded at two attempts, the same bound the mutation
    gate's ``_restore`` uses: an operator holding Ctrl+C down must not put
    the cleanup in a loop it cannot leave, so the second failure gives the
    line up and the rest of the cleanup goes on without it.

    A line the first attempt did print is printed again by the second,
    because an interrupted write says nothing about how much of it landed.
    A warning read twice costs the operator a moment; a warning never read
    costs them a setting still on in a running application.
    """
    for _attempt in (1, 2):
        try:
            out(message)
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        return interrupt
    return interrupt


def _mark(handled, step: str) -> None:
    """Record one step past which the caller has nothing left to say.

    The baseline save takes a ``handled`` list from a caller that has to
    decide what an interrupt left on disk. A Ctrl+C is delivered at a
    bytecode boundary, so it can land between any two steps of the save,
    and the file sitting there afterwards means opposite things depending
    on which step it landed between. Every entry is added AFTER the step it
    names, so an entry that is there is a step that is over: an empty list
    is the save having got past nothing, which is the one case the caller
    speaks about (finding wh-load-test-script.1.30).

    ``handled`` is None for every caller with nothing to decide, and the
    check is here rather than at each call site so it cannot be forgotten
    at one of them.
    """
    if handled is not None:
        handled.append(step)


def _call_cleanup(call, *args):
    """Call a cleanup helper again if the interrupt beat it to its door.

    A guard written inside a helper cannot cover that helper's own call
    boundary. The interpreter checks for a pending signal at the callee's
    FIRST instruction, so a Ctrl+C landing there skips the whole function
    while the caller is still in its ``finally`` -- every spinner left
    holding a core, or every config flag left at the test value with none
    of the warnings said (finding wh-load-test-script.1.33).

    Returns ``(result, deferred)``: what ``call`` returned, and the
    KeyboardInterrupt held back here, or None. What to do with a held
    interrupt is the caller's to decide, because the two shapes of cleanup
    helper disagree. ``stop_spinners`` and ``restore_all`` RAISE their own
    held interrupt, so ``main`` re-raises this one after them. The
    mutation gate's twin of this function serves ``_restore`` and
    ``_clear_pycache``, which RETURN theirs inside an
    ``(error, interrupt)`` tuple; raising there would skip the bytecode
    clearing that has to follow the source restore, so those callers fold
    it into the tuple instead. The function itself is blind to the
    difference: it asks only where the interrupt was raised, never what
    the callee would have returned.

    THE DISTINCTION THIS EXISTS TO MAKE. Because ``stop_spinners`` and
    ``restore_all`` end by re-raising an interrupt they held, an escaping
    KeyboardInterrupt is the ORDINARY outcome of a call that did all of
    its work -- the common case, not the rare one. Catching one and simply
    calling again would put every flag back twice, print every warning
    twice, and kill and wait every spinner twice. So the retry happens
    only when the body never ran, and that is read off the traceback: a
    call the interrupt reached at its door leaves a frame of the callee's
    still sitting on the callee's own ``def`` line, or no frame of the
    callee's at all. A body that ran left its frame on one of its
    statements -- the ``raise`` at the end, or whatever it called.
    Measured on CPython 3.12.10 with a second thread interrupting the main
    one during a guarded call: 93 of 93 interrupts the caller caught had
    the frame-at-the-def-line shape.

    The line is what is compared, not the instruction offset. A closure's
    frame begins past offset 0 -- ``COPY_FREE_VARS`` runs before the
    ``RESUME`` where the interpreter looks for the signal -- so an offset
    test would read a closure stopped at its door as one that had run. A
    function whose whole body sits on its ``def`` line would fool the line
    test in the other direction; none of the callees here is written that
    way.

    A callee with no code object -- a C function, or an object with
    ``__call__`` -- leaves nothing to read and is treated as having run.
    Calling something twice that may have finished is the failure this
    function is built to avoid.

    Only KeyboardInterrupt is deferred. An OSError at the same door
    propagates on the first attempt, exactly as it did before this
    existed. The bound is two attempts, the bound ``_say`` and the gate's
    ``_restore`` already use: an operator holding Ctrl+C down must not be
    put in a loop they cannot leave.

    crewcut: this does not close the window. The interrupt can land at
    this function's own call bytecode, or between the callee returning and
    its value being stored here. It makes the operator land two precisely
    timed interrupts where one used to do, rather than making the cleanup
    unskippable. Removing the remainder needs the calls to run with the
    signal blocked, which Python does not offer for SIGINT on Windows.

    crewcut: the seven ``_say`` call sites keep their uncovered door and
    are deliberately not wrapped here. They are, as of this commit,
    ``restore_all`` lines 444, 453, 473 and 481, ``stop_spinners`` line
    689, ``clear_baseline`` line 1030, and ``main`` line 1345 -- the line
    numbers move whenever anything above them changes, including this
    function, so the enclosing function names are the part that lasts.
    ``_say`` already retries twice inside itself, so a retry here would
    give four output attempts where wh-load-test-script.1.27 chose two,
    and its reason still holds: an operator holding Ctrl+C down must not
    be put in a loop they cannot leave. What would change the answer is a
    decision to move the deferral out to the call boundary AND cut
    ``_say``'s own loop to one attempt so the total stays at two. That
    re-opens .1.27's ruling and is not this fix's to make.
    """
    deferred = None
    for _entry in (1, 2):
        try:
            result = call(*args)
        except KeyboardInterrupt as stop:
            code = getattr(call, '__code__', None)
            started = code is None
            step = stop.__traceback__
            while step is not None:
                if (step.tb_frame.f_code is code
                        and step.tb_lineno != code.co_firstlineno):
                    started = True
                step = step.tb_next
            if started:
                raise
            if deferred is None:
                deferred = stop
            continue
        return result, deferred
    raise deferred


def restore_all(edits, out=print) -> None:
    """Put every flag back, in reverse, whatever any one of them does.

    ``FlagEdit.restore`` reads the file and writes to it, and both can raise:
    the file was deleted while the run was in flight, or the tree is
    read-only. A bare loop stops at the first exception, so a failure
    restoring the last flag flipped used to skip the first -- leaving the
    provider writing a diagnostic line every ten seconds -- and replaced
    whatever failure the operator needed to see with a traceback.

    A failure here says which file and which value, which is the same
    disclosure ``restore`` already makes when it finds a concurrent edit.

    A second Ctrl+C is deferred the same way ``stop_spinners`` defers it,
    and for a worse consequence: an abandoned restore leaves
    LOG_TRANSCRIPTS at the test value, so dictated text keeps reaching the
    log. The interrupt is raised after the WHOLE function rather than
    after the loop, because the operator whose flags did go back still has
    to be told the running application keeps the test value until a
    restart -- and an interrupted run is exactly when they are least
    likely to read the config file themselves.

    Every ``out`` call goes through ``_say``, which holds an interrupt
    raised while the line prints. Without that, the deferral covered only
    ``edit.restore()``: a Ctrl+C during a cleanup warning left this
    function at once, and the flags after it stayed at the test value.
    """
    interrupt = None
    put_back = []
    for edit in reversed(list(edits)):
        try:
            restored = edit.restore()
        except OSError as error:
            interrupt = _say(
                out,
                f'[!] could not put {edit.path} back ({error}). Set '
                f'{edit.key} to {str(not edit.value).lower()} yourself.',
                interrupt)
            continue
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            interrupt = _say(
                out,
                f'[!] interrupted before {edit.path} went back. Set '
                f'{edit.key} to {str(not edit.value).lower()} yourself.',
                interrupt)
            continue
        # ``restore`` answers about the write. The old test here was
        # "changed, then not changed", which a write that raised inside
        # ``apply`` also satisfies: the flag never reached the file, yet
        # the operator was told it went back and that the running
        # application keeps the test value until a restart.
        if restored:
            put_back.append(edit)
    # Putting the file back is not the same as turning the setting off.
    # The launcher reads LOG_TRANSCRIPTS once and exports
    # WHEELHOUSE_LOG_TRANSCRIPTS to the Logic, Input, GUI and provider
    # children, and the sherpa provider builds its load reporter once from
    # the startup configuration. Saying nothing here left the operator
    # believing the logging stopped when it had not.
    for edit in put_back:
        interrupt = _say(
            out,
            f'[!] {edit.key} went back to '
            f'{str(not edit.value).lower()} in {edit.path}, but WheelHouse '
            'read it once when it started. The running application keeps '
            'the value this test set until you restart it.',
            interrupt)
    if any(edit.key == 'LOG_TRANSCRIPTS' for edit in put_back):
        interrupt = _say(
            out,
            '[!] Until that restart, DICTATED TEXT keeps reaching the log.',
            interrupt)
    if interrupt is not None:
        raise interrupt


def speak_sentences(prompts, lead_out_s: float, *, wait, sleep=time.sleep,
                    out=print) -> None:
    """Walk a person through the fixed script, holding them to the pacing.

    The silence comes FIRST and the sentence second. That order is the whole
    of the pacing: a sentence on screen is a sentence being read aloud, so a
    prompt shown before its gap has elapsed puts two sentences inside one
    utterance, and the per-sentence join in judge.py has nothing to tell them
    apart with.

    ``wait`` is called after each sentence and returns when the speaker says
    they have finished; the next gap is measured from there rather than from
    a guess about how long a sentence takes to read.
    """
    for prompt in prompts:
        out(f'    ... {prompt.silence_before_s:.1f}s of silence, please')
        sleep(prompt.silence_before_s)
        out(f'[{prompt.index} of {len(prompts)}] SAY THIS, then press Enter:')
        out(f'    {prompt.sentence}')
        wait()
    out(f'[+] letting the last sentence reach its endpoint '
        f'({lead_out_s:.1f}s of silence)')
    sleep(lead_out_s)


def measure_busy_percent(samples: int = 5) -> Optional[float]:
    """The whole machine's busy percentage, averaged over ``samples`` seconds.

    Reads the same performance counter the procedure prescribes, through the
    same PowerShell command, so the number in the report is the number an
    operator following the document by hand would have written down.

    Returns None for anything that is not a usable percentage, including a
    reading that parses as a number but is not finite. ``float()`` accepts
    "NaN", "Infinity" and "-Infinity", and every comparison against NaN is
    false, so a NaN reading passed both of the caller's gates: a loaded run
    treated saturation as confirmed while holding no measurement, and a
    --baseline run called the same machine idle. The infinities each defeat
    one of the two gates. This is the only place the counter's text becomes
    a number, and None already means "no measurement" to both callers, so
    the whole class is refused here rather than at each comparison.

    A negative reading is refused for the same reason and in the same
    place: it is finite, so the gates disagree about it, and the baseline
    gate is the one that gets it wrong -- -5 > 30 is false, so a
    --baseline run calls that machine idle and can save a calibration
    from it, which later runs then reuse.

    The upper end is deliberately left open. Windows processor counters
    can read slightly over 100 on a healthy saturated machine from timing
    skew, so refusing above 100 would risk a false refusal in exactly the
    case this tool exists for, while accepting a high reading only makes
    the loaded run conclude "saturated" -- close to true whenever a real
    machine reads high. Recorded as an accepted trade-off on finding
    wh-load-test-script.1.23, boss ruling of 2026-08-30.
    """
    command = (
        "(Get-Counter '\\Processor(_Total)\\% Processor Time' "
        f"-SampleInterval 1 -MaxSamples {samples})"
        ".CounterSamples.CookedValue | Measure-Object -Average "
        "| ForEach-Object { $_.Average }")
    try:
        finished = subprocess.run(
            ['pwsh', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, text=True, timeout=samples + 60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if finished.returncode != 0:
        return None
    try:
        busy = float(finished.stdout.strip())
    except ValueError:
        return None
    if not math.isfinite(busy) or busy < 0.0:
        return None
    return busy


class _HeldSpinner(subprocess.Popen):
    """A spinner that joins the caller's list the moment its child exists.

    ``into.append(subprocess.Popen(...))`` puts the child in the list only
    after the whole constructor has returned. A ``KeyboardInterrupt``
    delivered in between -- Windows has already made the process, and
    ``__init__`` has not handed it back yet -- leaves a busy loop nobody
    holds. Read on this interpreter (CPython 3.12.10):
    ``Popen.__init__``'s failure cleanup does not call ``kill``, and
    ``Popen.__del__`` does not either, so nothing in the standard library
    reaps a child whose object never reached the caller. A local variable
    moves that window rather than closing it: the interrupt can land
    before the assignment exactly as easily as before the append.
    Stopping by name instead is not open to this tool for the reason
    ``start_spinners`` records below.

    ``_execute_child`` is CPython's own private hook, and it is the
    earliest point at which the caller can be handed a process that
    exists: it returns with ``self.pid`` and ``self._handle`` already set,
    which is everything ``stop_spinners`` needs to kill and reap. Its
    signature was read from this interpreter's ``subprocess`` module
    (twenty-four positional parameters, all of them internal) and is taken
    through ``*args``/``**kwargs`` and passed straight back, so a CPython
    that adds or renames one cannot break this override.

    crewcut: this narrows the window, it does not close it. An interrupt
    arriving inside ``_execute_child`` itself, after ``CreateProcess`` has
    returned but before this override regains control, still orphans that
    one child. Removing the limit needs the creation to run with SIGINT
    blocked, which Python does not offer on Windows -- the same limit
    ``stop_spinners`` records.
    """

    def __init__(self, into: list, *args, **kwargs) -> None:
        self._into = into
        super().__init__(*args, **kwargs)

    def _execute_child(self, *args, **kwargs) -> None:
        super()._execute_child(*args, **kwargs)
        self._into.append(self)


def start_spinners(count: int,
                   into: list[subprocess.Popen]) -> list[subprocess.Popen]:
    """One busy process per logical core, appended to the caller's list.

    Held rather than looked up later: stopping by name would reach every
    other python.exe on the machine, and this is a shared workstation.

    The caller owns the list, and every process is in it the moment it
    exists -- literally: ``_HeldSpinner`` registers itself from inside the
    constructor rather than being appended after it, which is what keeps a
    Ctrl+C landing on the way out of ``Popen`` from orphaning the child it
    just made. Building the list here and returning it would lose the
    processes already started if Popen raised partway through -- and
    process creation failing under pressure is precisely what this test
    manufactures. The caller's `finally` would then stop an empty list
    while the machine it reported on stayed saturated. A KeyboardInterrupt
    inside the loop has the same shape.

    A Python busy loop, not the obvious PowerShell one. The manual procedure
    this replaces measured both on the reference machine (Ikon, 20 logical
    cores): one `python -c "while True: pass"` per core reaches 100.0% on
    \\Processor(_Total)\\% Processor Time, while twenty
    `pwsh -Command "while($true){[Math]::Sqrt(2)}"` reach only 55.2%, which
    does not saturate and would understate the problem. The same document
    warned that `Start-Process` joins -ArgumentList with spaces, so a
    mis-quoted spinner dies instantly on a syntax error and the run measures
    an idle machine; passing the argument list to Popen directly is what
    removes that trap rather than documenting it.
    """
    for _ in range(count):
        _HeldSpinner(
            into, SPINNER_ARGV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return into


def stop_spinners(spinners: list[subprocess.Popen], out=print) -> None:
    """Stop exactly the processes this run started.

    A second Ctrl+C used to abandon the rest of the teardown here.
    ``KeyboardInterrupt`` inherits from ``BaseException``, so neither
    handler below saw it and it left the loop at the spinner it arrived
    on -- leaving processes nobody killed, each holding one core at full
    load until the operator found it by hand. It is deferred instead:
    every spinner still gets its kill and its wait, and the interrupt is
    raised once both loops are done.

    The timeout warning goes through ``_say`` for the same reason every
    notice in ``restore_all`` does. It used to be a bare ``print`` inside
    the ``TimeoutExpired`` arm, and an exception raised inside an
    ``except`` arm is not caught by a sibling arm of the same ``try``, so
    the ``KeyboardInterrupt`` handler three lines below it never saw a
    Ctrl+C that landed while the warning printed. Measured on this
    function before the fix, with three spinners and the first one timing
    out: killed=[1, 2, 3] but waited=[1]. The two children after it were
    left unreaped, and a kill that had not landed left a core at full
    load.

    ``out`` is the injection point that lets a test supply that output,
    the same shape ``clear_baseline`` uses. ``main`` passes nothing and
    still writes to stdout.

    crewcut: a signal can arrive anywhere, including inside a handler, so
    this is best effort rather than a guarantee. Removing that limit needs
    the loops to run with the signal blocked, which Python does not offer
    for SIGINT on Windows.
    """
    interrupt = None
    for spinner in spinners:
        try:
            spinner.kill()
        except OSError:
            pass
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
    for spinner in spinners:
        try:
            spinner.wait(timeout=10)
        except subprocess.TimeoutExpired:
            interrupt = _say(
                out,
                f'[!] spinner pid {spinner.pid} did not exit; stop it '
                'yourself',
                interrupt)
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
    if interrupt is not None:
        raise interrupt


def play_through_speakers(wav_path: Path,
                          device: Optional[str]) -> tuple[float, int]:
    """Play ``wav_path`` out of the speakers. Returns (seconds, underflows).

    The underflow count is the point. This runs on a machine the test has
    deliberately saturated, and a stuttering PLAYBACK sounds exactly like a
    stuttering capture. Without this number the report could not tell the
    reader which one they were looking at.
    """
    import numpy as np
    import sounddevice as sd

    with wave.open(str(wav_path), 'rb') as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)

    underflows = 0
    position = 0
    total = len(samples)

    def callback(outdata, frame_count, time_info, status):
        nonlocal underflows, position
        if status.output_underflow:
            underflows += 1
        end = min(position + frame_count, total)
        block = samples[position:end]
        written = len(block)
        outdata[:written] = block.reshape(written, channels)
        if written < frame_count:
            outdata[written:] = 0
            position = total
            raise sd.CallbackStop
        position = end

    started = time.monotonic()
    stream = sd.OutputStream(samplerate=rate, channels=channels,
                             dtype='int16', device=device,
                             callback=callback)
    with stream:
        while stream.active:
            time.sleep(0.1)
    return time.monotonic() - started, underflows


def _identity_of(info: os.stat_result) -> Optional[tuple]:
    """What tells one log file apart from the file that replaced it.

    WheelHouse rotates at ten megabytes with two backups, so the file this
    run started reading can be renamed away and a new one put in its place.
    The replacement carries a different file index.

    The creation time used to be carried beside the index, to cover a
    filesystem that reports no index. It had to go: Windows does not
    report it stably across the two calls this code makes. Measured on
    this machine, ``Path.stat`` and ``os.fstat`` on one unchanged file
    disagreed on ``st_ctime_ns`` in 1867 of 3000 pairs, and ``os.fstat``
    disagreed with ITSELF in 581 of 2000 -- always by about a
    millisecond. An ordinary growing log then read as a replacement, at
    random, in roughly a third of reads. The file index and the device
    agreed in every one of those 2000 pairs.

    crewcut: a filesystem that reports ``st_ino`` as 0 tells nothing
    apart, so this returns None there and the caller falls back to the
    size alone -- where a replacement that already grew past the mark is
    read as a continuation. Removing that needs an identity source that
    filesystem does provide. NTFS on this machine reports a real index,
    so the fallback is unreachable here.

    This takes a stat result rather than a path on purpose. Reading the
    identity through its own ``stat`` call, beside a size read through
    another one, let a rotation land between the two and produce a pair
    that never described one file.
    """
    if not info.st_ino:
        return None
    return (info.st_ino, info.st_dev)


@dataclass(frozen=True)
class LogMark:
    """Where this run started reading, and which file it was reading."""
    size: int
    identity: Optional[tuple]


def log_mark(path: Path) -> Optional[LogMark]:
    """Where this run starts reading, from ONE look at the file.

    The size and the identity used to come from two separate ``stat``
    calls. A rotation between them produced a torn mark -- the old file's
    size beside the replacement's identity -- and once that replacement
    grew past the old size, both halves agreed and the run read it as a
    continuation, seeking past a beginning it never reported dropping.

    A ``stat`` that was REFUSED returns None, not a zero mark.
    ``LogMark(0, None)`` is indistinguishable from the mark for an empty
    log, and ``main`` refuses before any mark unless the log file exists --
    so a transient sharing violation, which is ordinary on Windows while
    ConcurrentRotatingFileHandler is rotating wheelhouse.log, put the mark
    at zero and the run read every historical record as its own.

    A log that is ABSENT still marks at zero, and that is not the same
    case. A file nobody has created yet holds no history to mistake for
    this run, and the log is briefly absent during an ordinary rotation --
    between the rename and the new file -- where the file that appears is
    empty. Only a refusal can be hiding bytes.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return LogMark(size=0, identity=None)
    except OSError:
        return None
    return LogMark(size=info.st_size, identity=_identity_of(info))


def read_since(path: Path,
               mark: Optional[LogMark]) -> Optional[tuple[str, bool]]:
    """The log written after ``mark``, and whether the log rotated.

    None means the log could NOT be read, and it is a different answer
    from an empty slice. Both used to come back as ``('', False)``: a
    refused ``open`` -- ordinary on Windows while
    ConcurrentRotatingFileHandler is rotating wheelhouse.log -- was
    indistinguishable from a readable log with nothing new in it, so
    ``main`` parsed nothing, judged nothing, printed a completed template
    and returned 0. Under ``--baseline`` the same empty slice reached
    ``save_baseline`` with no ratio, which REMOVES the operator's
    existing calibration: a run with no evidence about its own log threw
    away the last run that had some
    (finding wh-load-test-script.1.36).

    ``os.fstat``, ``seek`` and ``read`` all run after the open has already
    succeeded, and each can fail on its own. They are inside the guard
    for the same reason, rather than escaping as a traceback.

    No mark also means no reading, and it answers None as well. A caller
    that ignored a failed ``log_mark`` would otherwise start at zero,
    which is what turned pre-run bytes into a measurement of this run.
    Nothing distinguishes the two failures because no caller needs to:
    ``main`` refuses on a failed mark before it ever reads, and both are
    the same statement -- this run cannot say what its log holds.

    Rotation is decided from the file's identity first and its size second.
    Size alone was not enough: a replacement file that has already grown
    past the old position reads as the same file that simply got longer, so
    the run seeks to a numeric offset inside a different file and discards
    its beginning -- the transcript and metric lines included -- without
    saying anything. On a rotation the run reads the whole current file and
    says so, rather than reporting a measurement made from whatever bytes
    happened to line up.
    """
    if mark is None:
        return None
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            # From the OPEN HANDLE, not from another look at the path. A
            # rotation landing between a decision made on the path and
            # the open that follows it hands the run a file its decision
            # never described; fstat cannot be wrong about the file it
            # was given.
            info = os.fstat(handle.fileno())
            size = info.st_size
            identity = _identity_of(info)
            replaced = (mark.identity is not None and identity is not None
                        and identity != mark.identity)
            rotated = replaced or size < mark.size
            start = 0 if rotated else mark.size
            handle.seek(start)
            return handle.read(), rotated
    except OSError:
        return None


def read_since_retrying(
        path: Path, mark: Optional[LogMark],
        attempts: int = LOG_READ_ATTEMPTS) -> Optional[tuple[str, bool]]:
    """``read_since``, tried again while the log refuses to be read.

    None still means the log could not be read, now after ``attempts``
    tries rather than one. A rotation's sharing violation clears in
    milliseconds, and refusing on the first one throws away a run whose
    machine was deliberately saturated for minutes to produce it.

    Bounded by a count of attempts rather than by a deadline so that a
    test which stands ``time.sleep`` down finishes at once instead of
    spinning for the whole window.
    """
    for remaining in range(attempts, 0, -1):
        result = read_since(path, mark)
        if result is not None:
            return result
        if remaining > 1:
            time.sleep(LOG_READ_RETRY_INTERVAL_S)
    return None


def wait_for_window_line(path: Path, timeout_s: float) -> bool:
    """Wait until a periodic [load-diag] window= line appears in the log.

    This is what makes the restart verified rather than assumed. The flag is
    read once when the provider starts, so the only proof it took effect is
    a line that could not have been written without it.
    """
    mark = log_mark(path)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if mark is None:
            # Keep trying for a mark rather than reading from zero. A
            # window line already in the log is not proof of a restart:
            # reading from zero used to accept one written before this run
            # and announce the flag live while the provider had never been
            # restarted. The retry comes BEFORE the wait, so the first
            # mark a cleared refusal allows is taken at once rather than
            # one round later.
            mark = log_mark(path)
            if mark is None:
                time.sleep(2.0)
            continue
        result = read_since(path, mark)
        if result is not None and logparse.parse_log(result[0]).windows:
            return True
        time.sleep(2.0)
    return False


def baseline_file(cache_dir: Path) -> Path:
    return cache_dir / BASELINE_NAME


def baseline_provenance(paths: Paths, provider: str,
                        endpoint_silence_ms: float) -> dict:
    """What a saved baseline has to have been measured against to be reused.

    The cache sits beside record.py, so it does not move with
    ``--repo-root``. Without this, the documented worktree mode -- run the
    tool from one checkout against a different running checkout -- read the
    tool checkout's calibration and judged the other one against it, and
    the verdict ladder compares the measured ratio against that number.

    crewcut: three values the run already holds, not a full fingerprint of
    the provider and its engine. Reading and hashing configuration the tool
    does not otherwise touch is a wider change, and where its boundary
    falls is a judgement rather than a defect. Extend this dict to close
    that gap.
    """
    return {
        'repo_root': str(paths.repo_root),
        'provider': provider,
        'endpoint_silence_ms': endpoint_silence_ms,
    }


def save_baseline(cache_dir: Path, ratio: Optional[float],
                  provenance: dict, handled=None) -> bool:
    """Write the baseline. False when this run measured none to write.

    The old baseline is removed FIRST, before the new one is written and
    before anything else this function does. ``_atomic_write`` replaces
    the target in one move, so a move that raised used to leave the
    previous calibration in place with its provenance unchanged, and the
    next load run read that ratio as this machine's. No baseline is a safe
    state -- the run says it has none and the operator calibrates. A stale
    one is not.

    The invalidation is the first executable statement on purpose. This
    used to open with ``cache_dir.mkdir`` and reach ``clear_baseline``
    only on the next line, and a Ctrl+C delivered during that directory
    setup left here with the old baseline untouched -- past every guard
    ``clear_baseline`` carries, because none of them had started
    (finding wh-load-test-script.1.29). The removal needs no directory:
    ``unlink(missing_ok=True)`` on a path whose parent does not exist
    raises nothing. So the directory is made further down, for the write
    that is the only part needing it.

    Raises OSError when the old baseline could not be removed or the new
    one could not be written. The caller reports it; this refuses rather
    than returning as though the cache had been replaced.

    A Ctrl+C at the removal comes out of here as a KeyboardInterrupt, with
    the old baseline already gone or the operator already told to delete
    it. Nothing replaces it: an interrupted calibration run records no
    baseline, which is the safe state this function exists to keep.

    A ratio that is not finite counts as none measured, on the same rule.
    ``json.dumps`` writes NaN and Infinity by default and ``json.loads``
    reads them back, so one such value became this machine's persisted
    calibration and every later run with matching provenance reused it --
    and every comparison against NaN in the verdict ladder is false
    (finding wh-load-test-script.1.25).

    Pass a list as ``handled`` to be told what this got through before an
    interrupt. Nothing inside here can close the window on the CALL into
    it: the signal is delivered at a bytecode boundary, so it can arrive
    before this function's first statement, or before ``clear_baseline``'s.
    What the caller can do is tell that case from the others, and the list
    is what lets it (finding wh-load-test-script.1.30).
    """
    if not clear_baseline(cache_dir, handled=handled):
        raise OSError(f'{baseline_file(cache_dir)} could not be removed')
    # Recorded after the invalidation, never before it: from here on, a
    # baseline file on disk is one this run wrote, so the caller has
    # nothing to say about the file it finds.
    _mark(handled, 'the old baseline is gone')
    if ratio is None or not math.isfinite(ratio):
        return False
    cache_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        baseline_file(cache_dir),
        json.dumps({'engine_ratio': ratio, **provenance},
                   indent=2).encode('utf-8'))
    return True


def clear_baseline(cache_dir: Path, out=print, handled=None) -> bool:
    """Remove a baseline a failed calibration would outlive. True when gone.

    A file that was never there counts as gone. False means the removal
    itself was refused -- the file is open elsewhere, or the tree is
    read-only.

    A Ctrl+C is held rather than allowed out, because this is the only
    invalidation the tool has: the interrupt used to leave here and leave
    ``save_baseline`` before either the old cache was removed or a
    replacement was written, and the old file stayed on disk with its
    provenance unchanged. The next ordinary load run then matched that
    provenance and read the old ratio as this machine's calibration, which
    is the state the fail-closed rule exists to prevent -- no baseline is
    safe, a stale one is not.

    The removal is attempted twice and the held interrupt is raised
    afterwards, so the operator gets the stop they asked for and the file
    is gone. Two attempts is the bound ``_say`` and the mutation gate's
    ``_restore`` use, for the same reason: an operator holding Ctrl+C down
    must not put the tool in a loop it cannot leave. When neither attempt
    finished, the file is still there and the operator is told to delete
    it themselves, in the words ``main`` uses when the removal fails.

    Pass a list as ``handled`` to be told when that instruction was given.
    The caller has its own guard over the same file and the same sentence,
    and an operator sent after one file twice reads the second copy as
    noise (finding wh-load-test-script.1.30).

    crewcut: nothing inside this tool can delete a file the filesystem
    refuses to delete, so that case ends in a message telling the operator
    to remove the file themselves rather than in an enforced invalidation.
    Removing that limit needs a second location the tool can always write,
    which the cache directory does not give it.
    """
    interrupt = None
    gone = False
    for _attempt in (1, 2):
        try:
            baseline_file(cache_dir).unlink(missing_ok=True)
        except OSError:
            break
        except KeyboardInterrupt as stop:
            if interrupt is None:
                interrupt = stop
            continue
        gone = True
        break
    if interrupt is not None:
        if not gone:
            # Said here rather than in ``main``, because this is where the
            # answer is known. ``main`` sees an interrupt whether or not
            # the file went, and telling an operator to delete a file that
            # is already gone sends them looking for nothing.
            interrupt = _say(
                out,
                f'[x] the baseline cache at {baseline_file(cache_dir)} was '
                'not removed before the run was interrupted. '
                + DELETE_THE_BASELINE_YOURSELF,
                interrupt)
            _mark(handled, 'the operator was told to delete it')
        raise interrupt
    return gone


def load_baseline(cache_dir: Path, provenance: dict,
                  out=print) -> Optional[float]:
    """The saved ratio, but only when it was measured against this run.

    A value that is not a finite number is ignored the same way, because
    it is not a measurement and no baseline is a safe state here. Both
    spellings could reach this file: ``json.loads`` reads NaN, Infinity
    and -Infinity back, and ``isinstance(True, (int, float))`` is True
    because bool subclasses int, so a JSON ``true`` used to become a
    calibration of 1.0 that nobody measured (finding
    wh-load-test-script.1.25).
    """
    try:
        value = json.loads(
            baseline_file(cache_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    differs = [k for k in provenance if value.get(k) != provenance[k]]
    if differs:
        # A file written before provenance existed carries none of these
        # keys, so it lands here too and the operator calibrates once.
        out('[!] the saved baseline was not measured against this run: '
            + '; '.join(f'{k} was {value.get(k)!r}, this run has '
                        f'{provenance[k]!r}' for k in differs)
            + '. Ignoring it. Run again with --baseline to calibrate '
              'this one.')
        return None
    ratio = value.get('engine_ratio')
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    if not math.isfinite(ratio):
        out(f'[!] the saved baseline at {baseline_file(cache_dir)} holds '
            f'{ratio!r}, which is not a measurement. Ignoring it. Run '
            'again with --baseline to calibrate this machine.')
        return None
    return float(ratio)


def worst_ratio(parsed: logparse.ParsedLog) -> Optional[float]:
    # None is an utterance whose provider measured no engine time -- every
    # Google line. It is dropped rather than compared: max() over a list
    # holding None raises, and a saved baseline must in any case rest on
    # measurements (wh-stt-load-metrics.4).
    ratios = [u.engine_ratio for u in parsed.utterances
              if u.engine_ratio is not None]
    return max(ratios) if ratios else None


def baseline_ratio_to_save(parsed: logparse.ParsedLog,
                           rotated: bool) -> Optional[float]:
    """The ratio a calibration run may record, or None for none at all.

    A rotated log leaves this run holding a fragment of itself, so
    ``worst_ratio`` searched only part of what the machine produced and the
    real worst per-utterance ratio may be in the file that rotated away. A
    baseline carries no record of how much of its run it saw -- the
    provenance names the machine, the provider and the endpoint setting,
    nothing about completeness -- so a fragment saved here becomes this
    machine's calibration for every later load run with nothing to mark
    it, and a calibration that is too low turns ordinary work into a
    reported failure.

    None is the safe answer, and ``save_baseline`` removes the earlier
    baseline before it returns for it. No baseline is a safe state, a
    wrong one is not (ruling of 2026-08-30 on wh-load-test-script.1.17).
    """
    if rotated:
        return None
    return worst_ratio(parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m tools.stt_load_test',
        description=('Run docs/testing/stt-cpu-load-test-procedure.md end to '
                     'end and print the completed verdict template.'))
    parser.add_argument(
        '--baseline', action='store_true',
        help='Run with no load, to record this machine\'s idle engine_ratio. '
             'Do this once before the loaded run; `neither` cannot be '
             'awarded without it.')
    parser.add_argument(
        '--playback', action='store_true',
        help='Play the recorded script out of the speakers instead of '
             'prompting a person to speak it. OFF by default because '
             'ENABLE_AUDIO_SUPPRESSION stops WheelHouse listening while '
             'sound plays through the speakers, so the recording would be '
             'heard by nothing. Use it with a headset loop, where the '
             'playback does not reach the machine\'s own speakers.')
    parser.add_argument(
        '--log-transcripts', action='store_true',
        help='Turn on LOG_TRANSCRIPTS for this run so the words themselves '
             'can be compared, then set it back. This WRITES DICTATED TEXT '
             'TO DISK in wheelhouse.log. Without it, sentences are judged '
             'only by their character and word counts.')
    parser.add_argument(
        '--repo-root', default=None, type=Path,
        help='The checkout the WheelHouse under test is running from. '
             'Default: the checkout this tool is in, which is right when '
             'they are the same one. Give it when running this tool from a '
             'git worktree: the config file the provider reads and the log '
             'it writes both live in the other checkout.')
    parser.add_argument(
        '--device', default=None,
        help='Output device for the playback, as sounddevice names it. '
             'Default: the system default output.')
    parser.add_argument(
        '--voice', default=record.DEFAULT_VOICE,
        help='edge-tts voice for the recording.')
    parser.add_argument(
        '--regenerate', action='store_true',
        help='Rebuild the recording even if the cached one is current.')
    parser.add_argument(
        '--machine', default=os.environ.get('COMPUTERNAME', 'unknown'),
        help='Machine name for the report.')
    parser.add_argument(
        '--provider', default='Parakeet v3 (CPU)',
        help='Provider and mode, for the report.')
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    paths = find_paths(args.repo_root)
    cores = os.cpu_count() or 1

    try:
        log_present = paths.log_file.is_file()
    except OSError as error:
        # Path.is_file re-raises every OSError its own ignore list does
        # not cover. Measured on this interpreter that list holds ENOENT,
        # ENOTDIR, EBADF and ELOOP, and does NOT hold EACCES, EPERM or a
        # Windows sharing violation -- so the refusal this tool is most
        # likely to meet arrived as a traceback rather than as the
        # diagnostic sitting on the very next line.
        print(f'[x] {paths.log_file} could not be read ({error}). '
              'WheelHouse may be rotating it; try again in a moment.')
        return 1
    if not log_present:
        print(f'[x] {paths.log_file} does not exist. Start WheelHouse first: '
              'the run reads the provider\'s own log lines.')
        return 1

    if not args.playback and not sys.stdin.isatty():
        print('[x] the default run prompts a person to speak the six '
              'sentences, so it needs a terminal to read from. Run it in '
              'one, or pass --playback with a headset loop.')
        return 1

    endpoint_silence_ms = read_endpoint_silence_ms(paths.sherpa_config)
    print(f'[+] endpoint_silence_ms {endpoint_silence_ms:.0f} '
          f'(from {paths.sherpa_config.name})')

    wav_path = None
    if args.playback:
        print('[!] --playback plays the script out of the speakers. '
              'ENABLE_AUDIO_SUPPRESSION stops WheelHouse listening while '
              'sound is playing, so this run measures nothing unless the '
              'playback goes to a headset the microphone can hear and the '
              'speakers cannot.')
        wav_path, built = record.ensure_recording(
            endpoint_silence_ms, voice=args.voice, force=args.regenerate)
        print(f'[+] recording {"built" if built else "reused"}: {wav_path}')

    edits: list[FlagEdit] = []
    try:
        # crewcut: this flips the flag in the Parakeet provider's config
        # only. Since wh-stt-load-metrics.4 the Google provider reads the
        # same [debug] log_load_diagnostics key, so a Google run needs
        # the operator to set and clear it by hand. The fix is a
        # provider-aware runner: read the active provider from the app
        # config and flip that provider's file here instead.
        diagnostics = FlagEdit(paths.sherpa_config, 'log_load_diagnostics',
                               True)
        edits.append(diagnostics)
        flipped = diagnostics.apply()
        if args.log_transcripts:
            print('[!] --log-transcripts writes DICTATED TEXT to '
                  f'{paths.log_file}. It is set back when this run ends.')
            transcripts = FlagEdit(paths.app_config, 'LOG_TRANSCRIPTS', True)
            edits.append(transcripts)
            try:
                flipped = transcripts.apply() or flipped
            except (OSError, ValueError) as error:
                # The switch is a convenience; the operator can always set the
                # value themselves. Editing a file this function does not
                # recognise, or inventing the line, is not a convenience.
                edits.remove(transcripts)
                print(f'[x] {paths.app_config} does not carry a '
                      f'LOG_TRANSCRIPTS line this run can flip ({error}). '
                      'Add "LOG_TRANSCRIPTS = true" to it yourself, or run '
                      'without --log-transcripts.')
                return 1

        if flipped:
            print('[!] A config flag changed. Restart WheelHouse now -- the '
                  'provider reads these once, at startup.')
        print('[+] waiting for a periodic [load-diag] window= line ...')
        if not wait_for_window_line(paths.log_file, RESTART_TIMEOUT_S):
            print('[x] no [load-diag] window= line arrived within '
                  f'{RESTART_TIMEOUT_S:.0f}s. WheelHouse is not running the '
                  'sherpa provider with log_load_diagnostics on.')
            return 1
        print('[+] the periodic line is live')

        spinners: list[subprocess.Popen] = []
        busy: Optional[float] = None
        mark = log_mark(paths.log_file)
        if mark is None:
            print(f'[x] {paths.log_file} could not be read to mark where '
                  'this run starts. Nothing was played. Run it again.')
            return 1
        underflows: Optional[int] = None
        try:
            if not args.baseline:
                print(f'[+] starting {cores} spinners, one per logical core')
                start_spinners(cores, into=spinners)
                time.sleep(SETTLE_S)
            busy = measure_busy_percent()
            if busy is None:
                print('[x] the processor counter gave no usable number, '
                      'so saturation cannot be confirmed. Nothing was '
                      'played.')
                return 1
            print(f'[+] machine busy {busy:.1f}%')
            if args.baseline and busy > BASELINE_CEILING_PERCENT:
                print(f'[x] a baseline needs an idle machine and this one is '
                      f'{busy:.1f}% busy. Nothing was played.')
                return 1
            if not args.baseline and busy < SATURATION_FLOOR_PERCENT:
                print(f'[x] the load reached only {busy:.1f}%, below the '
                      f'{SATURATION_FLOOR_PERCENT:.0f}% this test needs. '
                      'Nothing was played.')
                return 1

            mark = log_mark(paths.log_file)
            if mark is None:
                print(f'[x] {paths.log_file} could not be read to mark '
                      'where the playback starts. Nothing was played. Run '
                      'it again.')
                return 1
            if wav_path is not None:
                print('[+] playing the six sentences through the speakers')
                seconds, underflows = play_through_speakers(
                    wav_path, args.device)
                print(f'[+] played {seconds:.1f}s, {underflows} playback '
                      'underflow(s)')
            else:
                print('[+] speak the six sentences when each one appears. '
                      'Wait through the silences: they are what makes each '
                      'sentence its own utterance.')
                speak_sentences(
                    script.prompt_plan(endpoint_silence_ms),
                    script.lead_out_s(endpoint_silence_ms),
                    wait=lambda: input())
            print(f'[+] letting the log catch up for {LOG_SETTLE_S:.0f}s')
            time.sleep(LOG_SETTLE_S)
        finally:
            # Through ``_call_cleanup``: a Ctrl+C at this call's own door
            # used to skip every kill and every wait, leaving one busy
            # process per core on a machine this run deliberately
            # saturated.
            _, deferred = _call_cleanup(stop_spinners, spinners)
            if deferred is not None:
                raise deferred

        after_playback = read_since_retrying(paths.log_file, mark)
        if after_playback is None:
            print(f'[x] {paths.log_file} could not be read after the '
                  'playback, so this run has NO evidence about its own '
                  'log and cannot be judged. Nothing was recorded and any '
                  'earlier baseline is untouched. Run it again.')
            return 1
        text, rotated = after_playback
        if rotated:
            print('[!] the log rotated during the run; reading the current '
                  'file from its start, so lines from before the playback '
                  'may appear below.')
        parsed = logparse.parse_log(text)
        sentences = judge.judge_sentences(parsed.transcripts)

        provenance = baseline_provenance(
            paths, args.provider, endpoint_silence_ms)
        if args.baseline:
            baseline = None
        else:
            baseline = load_baseline(record.CACHE_DIR, provenance)

        verdict = judge.judge_run(parsed, sentences, baseline)
        print()
        print(report.render(
            machine=args.machine, logical_cores=cores,
            provider=args.provider,
            busy_percent=busy, baseline_ratio=baseline, parsed=parsed,
            sentences=sentences, verdict=verdict,
            playback_underflows=underflows, rotated=rotated))
        if args.baseline:
            ratio = baseline_ratio_to_save(parsed, rotated)
            # What the save got past, for the interrupt handler below. A
            # Ctrl+C is delivered at a bytecode boundary, so it can land on
            # the CALL itself -- before ``save_baseline``'s first
            # statement, or before ``clear_baseline``'s -- and no
            # arrangement of statements inside them closes that. Only the
            # caller can, and only if it can tell which file is on disk.
            handled: list[str] = []
            try:
                saved = save_baseline(record.CACHE_DIR, ratio, provenance,
                                      handled=handled)
            except OSError as error:
                print('[x] the baseline cache at '
                      f'{baseline_file(record.CACHE_DIR)} could not be '
                      f'updated ({error}). '
                      + DELETE_THE_BASELINE_YOURSELF)
                return 1
            except KeyboardInterrupt as stop:
                # Three outcomes, not two. An existence check on its own
                # decides only whether a file is there, and once the save
                # has handled anything at all the file there is either the
                # one this run wrote -- the interrupt can land after
                # ``_atomic_write`` finished and before ``saved`` is
                # stored -- or one the operator has already been sent to
                # delete. Sending them to delete a valid current
                # calibration, or a file that is not there, is the same
                # false instruction as saying nothing about a stale one.
                # So the line is said only when the save got past nothing
                # AND a file is still sitting there
                # (finding wh-load-test-script.1.30).
                if not handled and baseline_file(record.CACHE_DIR).exists():
                    # Through ``_say``, because this line is the only thing
                    # between the operator and a calibration nobody
                    # measured: a second Ctrl+C landing while it prints
                    # must not carry it away.
                    stop = _say(
                        print,
                        '[x] the run was interrupted before the baseline '
                        f'cache at {baseline_file(record.CACHE_DIR)} was '
                        'invalidated, so the file there is the EARLIER '
                        'calibration and not this run\'s. '
                        + DELETE_THE_BASELINE_YOURSELF,
                        stop)
                raise stop
            if saved:
                print(f'[+] baseline engine_ratio recorded: {ratio}')
            elif rotated:
                # A different reason for the same refusal, so it gets its
                # own sentence: the log did carry per-utterance lines, and
                # this run simply cannot say they were all of them.
                print('[x] the log rotated during this run, so it holds '
                      'only part of itself and NO baseline was recorded. '
                      'The worst per-utterance ratio this machine produces '
                      'may be in the file that rotated away, and a '
                      'calibration that is too low turns ordinary work '
                      'into a reported failure. Any earlier baseline has '
                      'been removed. Run --baseline again.')
                return 1
            else:
                # Saying nothing here printed "recorded: None" and exited
                # 0, and any earlier baseline stayed on disk for the next
                # load run to read as this machine's calibration.
                # ``save_baseline`` has already removed it by this point.
                print('[x] no per-utterance engine_ratio reached the log, '
                      'so this run measured NO baseline. Any earlier '
                      'baseline has been removed, so the next load run '
                      'cannot reuse it. Check that the provider wrote its '
                      'per-utterance lines, then run --baseline again.')
                return 1
        return 0
    finally:
        # Through ``_call_cleanup`` as well, and for the worse
        # consequence: a Ctrl+C at this call's door left LOG_TRANSCRIPTS
        # true in the config file and said none of the three warnings, so
        # dictated text kept reaching the log with nobody told.
        _, deferred = _call_cleanup(restore_all, edits)
        if deferred is not None:
            raise deferred
