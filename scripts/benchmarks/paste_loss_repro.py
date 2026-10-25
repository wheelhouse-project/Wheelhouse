"""Reproduce the dictation paste loss, without David's voice.

BEAD wh-dictation-paste-loss-load, acceptance item 2. David dictated 40
sentences into Notepad twice on 2026-10-19 (ECORE-DICTATION-CHECK-2026-10-19.md).
Under heavy load, words were lost or repeated. This harness sends the same
40 sentences through the same Logic-to-Input transport that dictation uses,
into a scratch Notepad window, and counts the missing, duplicated and
out-of-order words. The load is started separately, by whoever runs this.

THIS SCRIPT TAKES THE KEYBOARD FOCUS AND USES THE CLIPBOARD. Run it only
after the boss has given the word. Nothing in it runs on import.

WHAT RUNS (--method paste, the default: the real path)
  1. The parent creates the same transport launcher.py:1003-1011 creates:
     a SharedMemory block (SHARED_MEM_SIZE = 64 KiB, launcher.py:86), the
     command_ready, input_ready and shutdown events, and the response
     queue.
  2. The parent starts the REAL ``input_process_main`` (input_proc.py:2242)
     in a ``multiprocessing.Process`` with the five arguments the launcher
     passes (launcher.py:1010-1011). That function builds the real
     UIActionHandler, the real clipboard and the real SendInput.
  3. The parent builds the real ``WheelHouseApp`` (app.py:244) with the five
     arguments main.py's Logic process gets (launcher.py:1008), and starts
     only its response demuxer and outbound sender, which are what
     ``WheelHouseApp.start`` (app.py:338-365) starts besides the websocket.
     It sends through ``send_command`` (app.py:1000) and ``send_request``
     (app.py:1185), the functions Logic uses.
  4. Per sentence: ``start_utterance``, one ``intelligent_insert_text`` per
     word, one for the period, then ``end_utterance``.

WORD PAYLOADS. Logic does not add a leading space, a capital letter or the
period itself. Measured by reading the code (2026-10-19):
  - A spoken word reaches the Input process as the recogniser's word, with
    no change: speech_processor.py:2452 calls ``_send_to_dictation`` with
    ``decision.payload`` (the router's ``payload=word``, router.py:348), and
    speech_processor.py:4926-4929 sends
    ``send_request(action='intelligent_insert_text',
    params={'insertion_string': text})``. That is the only field.
  - The spoken word "period" is a replacement: patterns.toml:3474-3478 maps
    it to ``text(".")``, and speech/actions.py:758 packs it as the same
    ``intelligent_insert_text`` with ``insertion_string="."``
    (command_engine.py:405-460 sends it).
  - The leading space and the sentence-start capital are added on the Input
    side by ``TextPerfector.perfected_string`` (ui/text_perfector.py:60-106,
    spacing at 108-160, capitals at 173-215), from the text before the caret.
  - ``start_utterance`` and ``end_utterance`` carry ``{'utterance_id': id}``
    (websocket_manager.py:1496 and speech_processor.py:2044;
    speech_processor.py:3600-3603).
So this harness sends each word exactly as the check file spells it, then
".", and lets the Input process do the rest. ASSUMPTION: the real
recogniser's casing of mid-sentence words is not known here; the comparison
ignores case, so only the Input side's capital rule could differ.

--method unicode types the same text into the same Notepad with the
SendInput Unicode method that ui/router.py:136 turns off for RichEditD2DPT.
It starts no Input process. It formats each word with the same
TextPerfector the Input process uses, and types it with
``utils.win_input_sender.type_string_verified`` (win_input_sender.py:676),
at the same pacing.

PACING. --word-gap-ms is the time between consecutive sends inside one
utterance. Default 112 ms: the median gap between consecutive
``IPC_SENT action=intelligent_insert_text`` lines inside one utterance in
wheelhouse.log, 2026-10-19 19:44:20-19:48:15 (410 gaps, 41 utterances;
90th percentile 664 ms, longest 6.5 s). A send goes out at its planned time,
or at once when the previous send ran late.

LISTENERS. The real ``input_process_main`` starts a global pynput mouse
listener and keyboard listener (input_proc.py:2439-2442) with no switch to
turn them off. By default the child runs it unchanged. --inert-listeners
replaces the two listener classes with do-nothing classes in the child,
before it imports ``input_proc`` (install_inert_listeners, one small
function, easy to replace). The RESULT line and the JSON record the mode.

PRE-PASTE SETTLE. --pre-paste-settle-ms N (default 0, no change) makes the
child wait N ms before each Ctrl+V chord, by wrapping
``ui.clipboard_operations.verified_press_keys`` (install_pre_paste_settle).

AFTER EACH RUN the harness joins the child, prints its pid and exit code,
and checks that no process with that pid still exists (child_exited). A
child that is still alive after the join timeout is terminated by pid, and
child_exited is false.

SAFETY. Each run opens its OWN Notepad (scripts/benchmarks/scratch_window.py)
and refuses to type unless the focused control is Notepad's document control
and the document is empty, as notepad_typing_trials.py does. It records the
one window handle it opened and closes only that window, discarding the
text. When more than one new Notepad window appears, it refuses to type and
closes nothing.

USAGE (from services/wheelhouse, with that service's Python):

    python ../../scripts/benchmarks/paste_loss_repro.py --label baseline
    python ../../scripts/benchmarks/paste_loss_repro.py --label load --runs 3 \\
        --out load.json

No emojis in output (Windows CP1252 consoles).
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import difflib
import importlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]
WHEELHOUSE_DIR = REPO_ROOT / "services" / "wheelhouse"

# The check file is untracked, so it lives in the main checkout only.
DEFAULT_CHECK_PATH = (
    Path.home() / "Projects" / "WheelHouse" / "ECORE-DICTATION-CHECK-2026-10-19.md"
)

# Median gap between sends inside one utterance, run 2 of 2026-10-19.
DEFAULT_WORD_GAP_MS = 112.0
DEFAULT_SENTENCE_GAP_MS = 1000.0

# launcher.py:86.
SHARED_MEM_SIZE = 1024 * 64

NOTEPAD_CLASS = "RichEditD2DPT"
# Seconds to wait after the last send before reading the document.
FINAL_SETTLE_S = 2.0
# Seconds to wait for the Input process to report ready.
INPUT_READY_TIMEOUT_S = 90.0
# Seconds to wait for the Input process to exit after the shutdown event.
CHILD_JOIN_TIMEOUT_S = 15.0
CHILD_TERMINATE_JOIN_S = 5.0

SKIP_LINES = frozenset({"notepad", "new paragraph"})


# ---------------------------------------------------------------------------
# Pure parts (unit-tested in services/wheelhouse/tests/test_paste_loss_repro.py)
# ---------------------------------------------------------------------------


def load_sentences(path: Path) -> list[list[str]]:
    """Read the dictation text from the check file.

    The text is the code fence under the "The text to dictate" heading.
    Lines "notepad" and "new paragraph" and blank lines are skipped. Every
    other line is one sentence ending in the spoken word "period"; the
    returned list holds each sentence's words WITHOUT that last word.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    in_text_section = False
    in_fence = False
    sentences: list[list[str]] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("## The text to dictate"):
            in_text_section = True
            continue
        if not in_text_section:
            continue
        if stripped.startswith("```"):
            if in_fence:
                break
            in_fence = True
            continue
        if not in_fence or not stripped:
            continue
        if stripped.lower() in SKIP_LINES:
            continue
        words = stripped.split()
        if words[-1].lower() != "period":
            raise ValueError(f"sentence does not end in 'period': {stripped!r}")
        sentences.append(words[:-1])
    if not sentences:
        raise ValueError(f"no sentences found in {path}")
    return sentences


@dataclass(frozen=True)
class Step:
    """One send in the plan. ``offset_s`` is the planned time since the start."""

    offset_s: float
    kind: str  # "start" | "word" | "end"
    sentence: int
    utterance_id: str
    text: str = ""


def build_plan(
    sentences: list[list[str]],
    word_gap_s: float,
    sentence_gap_s: float,
    id_prefix: str = "repro",
) -> list[Step]:
    """Plan every send, with its time, from the sentences and the two gaps.

    Per sentence: start_utterance, each word, then "." (the spoken word
    "period"), then end_utterance at the same time as the ".". Consecutive
    sends inside a sentence are ``word_gap_s`` apart. The next sentence
    starts ``sentence_gap_s`` after the previous sentence's last send.
    """
    steps: list[Step] = []
    clock = 0.0
    for index, words in enumerate(sentences, start=1):
        utterance_id = f"{id_prefix}-s{index:02d}"
        steps.append(Step(clock, "start", index, utterance_id))
        texts = [*words, "."]
        for position, text in enumerate(texts):
            if position:
                clock += word_gap_s
            steps.append(Step(clock, "word", index, utterance_id, text))
        steps.append(Step(clock, "end", index, utterance_id))
        clock += sentence_gap_s
    return steps


def expected_text(sentences: list[list[str]]) -> str:
    """The text the document should hold, with periods."""
    return " ".join(" ".join(words) + "." for words in sentences)


_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def normalize_words(text: str) -> list[str]:
    """Lower case, no punctuation: the words of a text."""
    return _WORD_RE.findall(text.lower())


_PERIOD_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|\.")


# The cost of an extra token of got that does not repeat the token before
# it. A paste typed twice puts its second copy directly after the first, so
# a lost or repeated paste never leaves such a token. The cost is above any
# count of period errors, so an alignment uses one only when no history of
# lost and repeated pastes explains the text.
_STRAY_INSERT = 1_000_000


def count_periods(expected: str, got: str) -> tuple[int, int]:
    """(missing, extra) periods of ``got`` against ``expected``, by position.

    normalize_words drops punctuation, so a lost "." (one paste each, like
    a word) is invisible to compare_words. This aligns the words AND the
    periods, each "." a token of its own. A "." of ``expected`` outside the
    alignment is missing; a "." of ``got`` outside it is extra.

    The count is the fewest period errors of any history of lost pastes and
    pastes typed twice that explains the text. In such a history every extra
    token of ``got`` repeats the token before it, so "a. b." typed as
    "a b.." lost the period after "a" and repeated the one after "b". It did
    not lose "b" and type "b" again after "a", because that "b" repeats
    nothing. A text that no such history explains still gets a count, from
    the alignment with the fewest extra tokens that repeat nothing.

    wh-dictation-paste-loss-load.1.4: difflib's alignment paired a period
    with a neighbouring period, so a lost period and a repeated one
    cancelled. wh-dictation-paste-loss-load.1.5: an alignment that took the
    longest match first could take a word that repeats nothing as an extra
    token, and so read fewer period errors than any real history. Here the
    words cost nothing to leave out, so only the period errors and the
    extra tokens that repeat nothing decide.

    The text cannot always show every period error. "x .." is "x. y." with
    only "y" lost, and also with the period after "x" lost, "y" lost, and
    the period after "y" typed twice. This counts the history with fewer
    period errors, (0, 0), so a count can be low when two histories explain
    the text. It is never high, and missing minus extra is always right: it
    is the period count of ``expected`` minus that of ``got``.

    crewcut: the table has len(expected) x len(got) cells, about 180,000 for
    the 40-sentence check and well under a second in Python. A much longer
    document needs a banded or Hirschberg alignment.
    """
    e = _PERIOD_TOKEN_RE.findall(expected.lower())
    g = _PERIOD_TOKEN_RE.findall(got.lower())
    rows, cols = len(e), len(g)
    # The cost of leaving a token of got out: one more for a period, and
    # _STRAY_INSERT more when it does not repeat the token before it.
    insert_cost = [
        (g[j] == ".") + (0 if j > 0 and g[j] == g[j - 1] else _STRAY_INSERT)
        for j in range(cols)
    ]
    # cost[i][j]: the lowest cost of aligning e[i:] with g[j:].
    cost = [[0] * (cols + 1) for _ in range(rows + 1)]
    last = cost[rows]
    for j in range(cols - 1, -1, -1):
        last[j] = last[j + 1] + insert_cost[j]
    for i in range(rows - 1, -1, -1):
        row, below = cost[i], cost[i + 1]
        token = e[i]
        skip = token == "."
        row[cols] = below[cols] + skip
        for j in range(cols - 1, -1, -1):
            best = below[j] + skip
            insert = row[j + 1] + insert_cost[j]
            if insert < best:
                best = insert
            if g[j] == token and below[j + 1] < best:
                best = below[j + 1]
            row[j] = best
    # Each period error costs one, so the errors are the cost left after the
    # strays, and missing minus extra is fixed by the two texts.
    errors = cost[0][0] % _STRAY_INSERT
    net = e.count(".") - g.count(".")
    missing = (errors + net) // 2
    return missing, missing - net


@dataclass
class Comparison:
    expected: int
    got: int
    missing: int
    duplicated: int
    out_of_order: int
    cut: int
    extra: int
    differences: list[str]


def _is_cut(got_word: str, expected_word: str) -> bool:
    return (
        len(got_word) < len(expected_word)
        and expected_word.startswith(got_word)
    )


def compare_words(expected: list[str], got: list[str]) -> Comparison:
    """Count what is wrong in ``got`` against ``expected``.

    The words are aligned with difflib (``autojunk=False``). The words the
    alignment leaves unmatched are then sorted:

    - out_of_order: a word present in both unmatched lists, so it is in the
      text but at the wrong position (a moved block).
    - cut: an unmatched got word that is a shorter start of an unmatched
      expected word (for example "mor" for "morning"). A cut word is
      counted here and not in ``missing``.
    - missing: an expected word with no match left.
    - duplicated: a got word left over whose word is in the expected text
      (typed more often than expected).
    - extra: a got word left over that is not in the expected text at all
      (garbled or joined words).
    """
    matcher = difflib.SequenceMatcher(None, expected, got, autojunk=False)
    unmatched_expected: list[str] = []
    unmatched_got: list[str] = []
    differences: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        unmatched_expected.extend(expected[i1:i2])
        unmatched_got.extend(got[j1:j2])
        differences.append(
            f"{tag} expected[{i1}:{i2}]={' '.join(expected[i1:i2])!r} "
            f"got[{j1}:{j2}]={' '.join(got[j1:j2])!r}"
        )

    moved = Counter(unmatched_expected) & Counter(unmatched_got)
    out_of_order = sum(moved.values())
    left_expected = list((Counter(unmatched_expected) - moved).elements())
    left_got = list((Counter(unmatched_got) - moved).elements())

    cut = 0
    for word in list(left_got):
        for candidate in left_expected:
            if _is_cut(word, candidate):
                left_got.remove(word)
                left_expected.remove(candidate)
                cut += 1
                break

    vocabulary = set(expected)
    duplicated = sum(1 for word in left_got if word in vocabulary)
    extra = len(left_got) - duplicated
    return Comparison(
        expected=len(expected),
        got=len(got),
        missing=len(left_expected),
        duplicated=duplicated,
        out_of_order=out_of_order,
        cut=cut,
        extra=extra,
        differences=differences,
    )


def unicode_texts(sentences: list[list[str]]) -> list[str]:
    """The strings the unicode method types, one per word and period.

    Each string is formatted by the same TextPerfector the Input process
    uses (spacing and the sentence-start capital), from the text typed so
    far, in the order of build_plan.
    """
    sys.path.insert(0, str(WHEELHOUSE_DIR))
    from ui.text_perfector import TextPerfector

    perfector = TextPerfector()
    typed = ""
    out: list[str] = []
    for words in sentences:
        for text in [*words, "."]:
            piece = perfector.perfected_string(text, preceding_chars=typed)
            out.append(piece)
            typed += piece
    return out


def format_result_line(label: str, record: dict) -> str:
    """The one RESULT line for a run."""
    child = record.get("child_exited")
    child_text = "na" if child is None else str(bool(child)).lower()
    return (
        f"RESULT run={label} method={record['method']} "
        f"listeners={record['listeners']} "
        f"pre_paste_settle_ms={record.get('pre_paste_settle_ms', 0):g} "
        f"paste_check={record.get('paste_check', 'na')} "
        f"expected={record['expected']} got={record['got']} "
        f"missing={record['missing']} duplicated={record['duplicated']} "
        f"out_of_order={record['out_of_order']} cut={record['cut']} "
        f"missing_periods={record.get('missing_periods', 'na')} "
        f"extra_periods={record.get('extra_periods', 'na')} "
        f"elapsed_s={record['elapsed_s']:.1f} child_exited={child_text}"
    )


# ---------------------------------------------------------------------------
# Child process
# ---------------------------------------------------------------------------


class _InertListener:
    """Stand-in for a pynput listener: every method does nothing."""

    def __init__(self, *args, **kwargs):
        self.running = False

    def start(self):
        return None

    def stop(self):
        return None

    def join(self, *args, **kwargs):
        return None


def install_inert_listeners() -> None:
    """Replace pynput's mouse and keyboard listener classes with do-nothing ones.

    This is the only place the harness changes how the Input process
    behaves, and only when --inert-listeners is given. It must run before
    ``input_process_main`` starts the listeners (input_proc.py:2439-2442).
    """
    from pynput import keyboard, mouse

    mouse.Listener = _InertListener
    keyboard.Listener = _InertListener


def install_pre_paste_settle(settle_ms: float) -> None:
    """Make every Ctrl+V chord wait ``settle_ms`` before it is sent.

    Replaces ``verified_press_keys`` in the ``ui.clipboard_operations``
    namespace, because ``verified_paste`` looks the name up there
    (ui/clipboard_operations.py:960; also 1287 and 1456). The wrapper sleeps
    only when the keys are exactly ``('ctrl', 'v')`` and then calls the
    original function; every other call passes through unchanged. Used only
    when --pre-paste-settle-ms is above 0, to test whether a short pause
    between the clipboard write and the paste stops the loss.
    """
    import importlib

    module = importlib.import_module("ui.clipboard_operations")
    original = module.verified_press_keys

    def verified_press_keys_with_settle(*keys, **kwargs):
        if keys == ("ctrl", "v"):
            time.sleep(settle_ms / 1000.0)
        return original(*keys, **kwargs)

    module.verified_press_keys = verified_press_keys_with_settle


def install_no_paste_check() -> None:
    """Make the Input child paste without the paste check.

    Replaces ``prepare_paste_check`` in ``ui.clipboard_operations`` with a
    function that always answers (None, "class_out_of_scope"), so
    verified_paste behaves as before wh-dictation-paste-loss-load. Used by
    --no-paste-check for the origin/dev baseline in the load session.
    """
    module = importlib.import_module("ui.clipboard_operations")
    module.prepare_paste_check = lambda hwnd, class_name: (
        None, "class_out_of_scope",
    )


def input_child_main(
    shm_name,
    command_ready_event,
    input_ready_event,
    response_queue,
    shutdown_event,
    inert_listeners: bool,
    pre_paste_settle_ms: float = 0,
    no_paste_check: bool = False,
) -> None:
    """Entry point of the Input child: the real input_process_main."""
    for path in (str(REPO_ROOT), str(WHEELHOUSE_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)
    if inert_listeners:
        install_inert_listeners()
    if pre_paste_settle_ms > 0:
        install_pre_paste_settle(pre_paste_settle_ms)
    if no_paste_check:
        install_no_paste_check()
    from input_proc import input_process_main

    input_process_main(
        shm_name,
        command_ready_event,
        input_ready_event,
        response_queue,
        shutdown_event,
    )


def _pid_exists_ctypes(pid: int) -> bool:
    """True when a process with this pid is still running (no psutil)."""
    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(process_query_limited_information, 0, pid)
    if not handle:
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def pid_exists(pid: int) -> bool:
    """psutil.pid_exists when psutil is installed, else OpenProcess."""
    try:
        import psutil
    except ImportError:
        return _pid_exists_ctypes(pid)
    return bool(psutil.pid_exists(pid))


def verify_child_exit(
    process,
    join_timeout_s: float = CHILD_JOIN_TIMEOUT_S,
    pid_exists_fn=pid_exists,
) -> dict:
    """Join the Input child and prove it is gone.

    Returns ``{"pid", "exitcode", "terminated", "child_exited"}``.
    ``child_exited`` is true only when the child ended by itself inside the
    join timeout AND no process with its pid exists afterwards. A child
    still alive after the timeout is terminated (that pid only) and
    ``child_exited`` is false.
    """
    pid = process.pid
    process.join(join_timeout_s)
    terminated = False
    if process.is_alive():
        terminated = True
        process.terminate()
        process.join(CHILD_TERMINATE_JOIN_S)
    exitcode = process.exitcode
    try:
        process.close()
    except Exception:
        pass
    still_exists = bool(pid_exists_fn(pid)) if pid is not None else False
    return {
        "pid": pid,
        "exitcode": exitcode,
        "terminated": terminated,
        "child_exited": (not terminated) and (not still_exists),
    }


# ---------------------------------------------------------------------------
# One run (takes the keyboard focus; never called by the unit tests)
# ---------------------------------------------------------------------------


async def _run_sends(
    plan, send_word, send_command, started_ref: list, stop_check=None,
) -> tuple[list, str | None]:
    """Send the plan at its planned times.

    Returns (failures, stop_reason). ``stop_check(step)`` runs after the
    planned delay and before each "start" and "word" send, so before every
    sentence and every typed word (wh-dictation-paste-loss-load.1.11); a
    reason it returns ends the run at once with nothing more sent, and the
    reason names the sentence. stop_reason is None when the plan finished.
    """
    failures: list[str] = []
    loop = asyncio.get_running_loop()
    start = loop.time()
    started_ref.append(time.perf_counter())
    for step in plan:
        delay = start + step.offset_s - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if stop_check is not None and step.kind != "end":
            reason = stop_check(step)
            if reason is not None:
                return failures, (
                    f"stopped at sentence {step.sentence}, before its "
                    f"{step.kind} send: {reason}"
                )
        try:
            if step.kind == "word":
                await send_word(step)
            else:
                await send_command(step)
        except Exception as exc:  # a lost send is a result, not a crash
            failures.append(f"{step.kind} {step.text!r}: {exc!r}")
    return failures, None


def focus_own_window(
    handle: int, *, launched_pid: int, launch_time: float,
    before_pids: set[int], pid_of, create_time, steal,
) -> str | None:
    """Bring this run's own Notepad window to the foreground.

    Returns a refusal reason, or None when the window was sent forward.
    Measured 2026-10-20 00:26: the live foreground lock timeout read
    2147483647 ms, and a new Notepad window stayed behind claude.exe, so
    the run refused to type. ``steal`` is utils.foreground.steal_foreground
    (AttachThreadInput, SetForegroundWindow, detach in a finally block);
    it changes no setting. The window must belong to the process that
    this run launched, or to a process that started after the launch and
    is not a Notepad process that was open before the run: the System32
    notepad.exe can hand the window to the packaged Notepad. The caller's
    RichEditD2DPT focus check still decides whether the run types.
    """
    window_pid = pid_of(handle)
    if window_pid in before_pids:
        return (
            f"window {handle} belongs to process {window_pid}, a Notepad "
            "that was open before the run"
        )
    if window_pid != launched_pid:
        try:
            started = create_time(window_pid)
        except Exception as exc:
            return f"cannot read the start time of process {window_pid}: {exc}"
        if started < launch_time:
            return (
                f"window {handle} belongs to process {window_pid}, which "
                "started before this run launched Notepad"
            )
    steal(handle)
    return None


PROOF_LAUNCHED_PID = "launched pid"
PROOF_SINGLE_NEW = "single new process"


def own_window_proof(
    window_pid: int, *, launched_pid: int, before_notepad_pids: set[int],
    list_notepad_pids,
) -> tuple[str | None, str | None]:
    """(proof, refusal) that the accepted window derives from this launch.

    wh-dictation-paste-loss-load.1.6. The run calls this immediately before
    its first keystroke. A window owned by the launched process is proven
    by its process ID. Any other window (the System32 notepad.exe can hand
    its window to the packaged Notepad) is proven only while exactly one
    new notepad.exe process exists: one whose ID was not running before the
    run, not counting the launched process. A second new Notepad could be
    David's, and typing into his window loses his text when the cleanup
    closes it. Every other case returns a refusal and no proof.
    ``list_notepad_pids`` returns the current notepad.exe process IDs.
    """
    # crewcut: a Notepad that David opens during the launch, at the moment
    # the launch hands its window to another process and the launch's own
    # window never appears, passes as the single new process when no other
    # new Notepad process exists. Remove the risk with a marker that only
    # the harness can set on its own window before any check, or with a
    # launch path that keeps the window in the launched process.
    if window_pid == launched_pid:
        return PROOF_LAUNCHED_PID, None
    try:
        current = set(list_notepad_pids())
    except Exception as exc:
        return None, f"cannot list the Notepad processes: {exc!r}"
    new = current - before_notepad_pids - {launched_pid}
    if len(new) != 1:
        return None, (
            f"{len(new)} new Notepad processes exist besides the launched "
            f"process {launched_pid}, so the window of process {window_pid} "
            "cannot be proven to derive from this launch"
        )
    # The single new process proves only its own window: a window owned by
    # a process that psutil does not name notepad.exe, or by a reused
    # process ID, is not that process.
    if window_pid not in new:
        return None, (
            f"the window of process {window_pid} is not the single new "
            f"Notepad process {next(iter(new))}"
        )
    return PROOF_SINGLE_NEW, None


def cleanup_refusal(
    handle: int, identity, *, is_window, read_identity
) -> str | None:
    """None when the cleanup may close ``handle``; otherwise the reason.

    wh-dictation-paste-loss-load.1.8. ``identity`` is the window class,
    process ID, and process start time that the run read when
    focus_own_window accepted the window, or None when it never did.
    The handle must still name that same window: a closed scratch window
    can free its handle for a window that David opens later, and
    close_window discards unsaved text.
    """
    if identity is None:
        return (
            f"window {handle} was never accepted as this run's own window"
        )
    if not is_window(handle):
        return f"window {handle} is already closed"
    try:
        now = read_identity(handle)
    except Exception as exc:
        return f"cannot read window {handle}: {exc!r}"
    if now != identity:
        return (
            f"window {handle} is now {now}, not the window {identity} "
            "that this run accepted"
        )
    return None


def control_window_refusal(control, own_handle: int, root_of) -> str | None:
    """None when ``control`` lies inside window ``own_handle``; else the reason.

    wh-dictation-paste-loss-load.1.11. The class check alone cannot tell this
    run's Notepad from David's: both use RichEditD2DPT. The control's
    top-level window must be the window that the run accepted. ``root_of``
    maps a window handle to its top-level window (win32gui.GetAncestor with
    GA_ROOT). A control whose handle cannot be read, or whose top-level
    window cannot be read, is refused.
    """
    try:
        handle = control.NativeWindowHandle
    except Exception as exc:
        return f"cannot read the window handle of the focused control: {exc!r}"
    if not handle:
        return (
            "the focused control has no window handle, so it cannot be "
            f"tied to this run's window {own_handle}"
        )
    try:
        root = root_of(handle)
    except Exception as exc:
        return (
            f"cannot read the top-level window of control handle {handle}: "
            f"{exc!r}"
        )
    if root != own_handle:
        return (
            f"the focused control is inside window {root}, not this run's "
            f"window {own_handle}"
        )
    return None


def foreground_stop_reason(read_foreground, own_handle: int) -> str | None:
    """None while window ``own_handle`` is in front; else why the run stops.

    wh-dictation-paste-loss-load.1.11. Dictation pastes into whatever
    window is in front, so a run that sees another window in front (or no
    window, or cannot read the foreground) must type nothing more.
    ``read_foreground`` is win32gui.GetForegroundWindow.
    """
    try:
        foreground = read_foreground()
    except Exception as exc:
        return f"cannot read the foreground window: {exc!r}"
    if foreground != own_handle:
        return (
            f"the foreground window is {foreground}, not this run's window "
            f"{own_handle}"
        )
    return None


STATUS_STOPPED = "stopped"


def mark_stopped(record: dict, reason: str) -> dict:
    """Turn ``record`` into a stopped run: no counts, status ``stopped``.

    A stopped run sent only part of the text into a window that it then
    lost. Its document was never compared, so it must not read as a pass
    or as lost words; main() reports it and counts nothing from it.
    """
    record["status"] = STATUS_STOPPED
    record["reason"] = reason
    return record


def _measure_document(auto, own_handle: int, root_of):
    """Check the focused control is an empty Notepad document of this run."""
    control = auto.GetFocusedControl()
    if control is None:
        return None, "no focused control"
    refusal = control_window_refusal(control, own_handle, root_of)
    if refusal is not None:
        return None, refusal
    class_name = getattr(control, "ClassName", "?")
    if class_name != NOTEPAD_CLASS:
        return None, f"focused control is {class_name!r}, not {NOTEPAD_CLASS!r}"
    pattern = control.GetPattern(auto.PatternId.TextPattern)
    if pattern is None:
        return None, "no TextPattern"
    before = pattern.DocumentRange.GetText(-1)
    if before.strip():
        return None, f"document is not empty ({len(before)} characters)"
    return pattern, None


def run_once(args, sentences: list[list[str]], label: str, index: int) -> dict:
    """One full run. Opens and closes its own Notepad window."""
    import multiprocessing
    from multiprocessing import shared_memory

    import uiautomation as auto

    sys.path.insert(0, str(SCRIPT_DIR))
    import scratch_window

    for path in (str(REPO_ROOT), str(WHEELHOUSE_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)

    import psutil
    import win32con
    import win32gui
    import win32process

    from services.wheelhouse.utils.foreground import steal_foreground

    def _root_window(handle: int) -> int:
        return win32gui.GetAncestor(handle, win32con.GA_ROOT)

    def _window_pid(handle: int) -> int:
        return win32process.GetWindowThreadProcessId(handle)[1]

    def _window_identity(handle: int) -> tuple:
        pid = _window_pid(handle)
        return (
            win32gui.GetClassName(handle), pid,
            psutil.Process(pid).create_time(),
        )

    run_label = f"{label}.{index}" if args.runs > 1 else label
    plan = build_plan(
        sentences,
        args.word_gap_ms / 1000.0,
        args.sentence_gap_ms / 1000.0,
        id_prefix=f"repro-{run_label}",
    )
    expected = normalize_words(expected_text(sentences))
    use_input = args.method == "paste"

    shm = process = app = None
    shutdown_event = None
    child_info = None
    record: dict = {
        "run": run_label,
        "method": args.method,
        "listeners": (
            "inert" if args.inert_listeners else "real"
        ) if use_input else "none",
        "word_gap_ms": args.word_gap_ms,
        "sentence_gap_ms": args.sentence_gap_ms,
        "pre_paste_settle_ms": args.pre_paste_settle_ms if use_input else 0,
        "paste_check": (
            "off" if args.no_paste_check else "on"
        ) if use_input else "na",
        "status": "failed",
    }
    window_class = scratch_window.NOTEPAD_WINDOW_CLASS
    before_handles = scratch_window.list_handles(window_class)
    # Every notepad.exe process open before the run, with or without a
    # window (the window owners alone are before_pids).
    def _notepad_pids() -> set[int]:
        return {
            proc.info["pid"]
            for proc in psutil.process_iter(["pid", "name"])
            if (proc.info.get("name") or "").lower() == "notepad.exe"
        }

    before_pids = set()
    for handle in before_handles:
        try:
            before_pids.add(_window_pid(handle))
        except Exception:  # the window closed after the walk
            pass
    own_handle = None
    # Set only when focus_own_window accepts the window (.1.8).
    own_identity = None
    document_text = ""
    failures: list[str] = []
    started: list = []
    finished = None
    stop_reason = None

    def _stop_check(step) -> str | None:
        return foreground_stop_reason(win32gui.GetForegroundWindow, own_handle)

    try:
        if use_input:
            shm = shared_memory.SharedMemory(
                create=True, size=SHARED_MEM_SIZE,
                name=f"WheelHouse_PASTE_REPRO_{time.time_ns()}",
            )
            response_queue = multiprocessing.Queue()
            command_ready_event = multiprocessing.Event()
            input_ready_event = multiprocessing.Event()
            shutdown_event = multiprocessing.Event()
            process = multiprocessing.Process(
                target=input_child_main,
                args=(
                    shm.name, command_ready_event, input_ready_event,
                    response_queue, shutdown_event, bool(args.inert_listeners),
                    float(args.pre_paste_settle_ms),
                    bool(args.no_paste_check),
                ),
                name="InputProcess",
            )
            process.start()
            print(f"[+] {run_label}: Input child pid {process.pid}")
            if not input_ready_event.wait(INPUT_READY_TIMEOUT_S):
                record["reason"] = "the Input process never reported ready"
                return record

        with auto.UIAutomationInitializerInThread(debug=False):
            # Taken just before the launch, so a Notepad that David opens
            # after this point counts as a new process.
            before_notepad_pids = _notepad_pids()
            launch_time = time.time()
            launched_pid = scratch_window.open_notepad_process(before_handles)
            if launched_pid is None:
                record["reason"] = (
                    "the launch opened no new window, so the focused "
                    "document belongs to somebody else"
                )
                return record
            # Keep the one handle this run opened, so the cleanup closes
            # only that window. A Notepad that David opens during the run
            # is never closed, and its text is never discarded.
            new_handles = scratch_window.list_handles(window_class) - before_handles
            if len(new_handles) != 1:
                record["reason"] = (
                    f"{len(new_handles)} new Notepad windows appeared, so the "
                    "run cannot tell which one is its own"
                )
                return record
            own_handle = next(iter(new_handles))
            refusal = focus_own_window(
                own_handle, launched_pid=launched_pid,
                launch_time=launch_time, before_pids=before_pids,
                pid_of=_window_pid,
                create_time=lambda pid: psutil.Process(pid).create_time(),
                steal=steal_foreground,
            )
            if refusal is not None:
                record["reason"] = refusal
                return record
            # The cleanup may close the window only after the proof below
            # passes (.1.6): a refusal before it leaves the window open.
            try:
                candidate_identity = _window_identity(own_handle)
            except Exception as exc:
                record["reason"] = (
                    f"cannot read the identity of window {own_handle}: "
                    f"{exc!r}"
                )
                return record
            time.sleep(0.5)  # let the foreground change take effect
            pattern, refusal = _measure_document(
                auto, own_handle, _root_window,
            )
            if pattern is None:
                record["reason"] = refusal
                return record
            # The last check before the first keystroke (.1.6). A refusal
            # leaves own_identity None, so the cleanup closes nothing.
            window_pid = _window_pid(own_handle)
            proof, refusal = own_window_proof(
                window_pid, launched_pid=launched_pid,
                before_notepad_pids=before_notepad_pids,
                list_notepad_pids=_notepad_pids,
            )
            if proof is None:
                record["reason"] = refusal
                return record
            own_identity = candidate_identity
            print(
                f"[+] {run_label}: own window {own_handle} pid "
                f"{window_pid} (launched pid {launched_pid}); "
                f"proof={proof}; "
                f"foreground is now {win32gui.GetForegroundWindow()}"
            )

            if use_input:
                from services.wheelhouse.app import WheelHouseApp

                async def drive() -> list:
                    nonlocal app
                    app = WheelHouseApp(
                        shm.name, command_ready_event, input_ready_event,
                        response_queue, SHARED_MEM_SIZE,
                    )
                    app._start_demuxer()
                    app._start_sender()
                    try:
                        async def send_word(step):
                            await app.send_request(
                                action="intelligent_insert_text",
                                params={"insertion_string": step.text},
                            )

                        async def send_command(step):
                            await app.send_command({
                                "action": (
                                    "start_utterance" if step.kind == "start"
                                    else "end_utterance"
                                ),
                                "params": {"utterance_id": step.utterance_id},
                            })

                        result = await _run_sends(
                            plan, send_word, send_command, started,
                            _stop_check,
                        )
                        # A fire-and-forget command only reaches the
                        # outbound queue; let the sender task write it
                        # before stop() cancels that task.
                        drain_end = time.monotonic() + 5.0
                        while (
                            not app._outbound_q.empty()
                            and time.monotonic() < drain_end
                        ):
                            await asyncio.sleep(0.02)
                        return result
                    finally:
                        await app.stop()

                failures, stop_reason = asyncio.run(drive())
            else:
                from utils.win_input_sender import type_string_verified

                texts = iter(unicode_texts(sentences))

                async def send_word(step):
                    ok, _sent, error = type_string_verified(next(texts))
                    if not ok:
                        raise RuntimeError(error or "type_string_verified failed")

                async def send_command(step):
                    return None

                failures, stop_reason = asyncio.run(
                    _run_sends(
                        plan, send_word, send_command, started, _stop_check,
                    )
                )
            finished = time.perf_counter()

            # A stopped run reads no document: the text is partial, and the
            # comparison below would count the unsent words as lost.
            if stop_reason is None:
                time.sleep(FINAL_SETTLE_S)
                document_text = pattern.DocumentRange.GetText(-1)
    finally:
        try:
            if own_handle is not None:
                leave_open = cleanup_refusal(
                    own_handle, own_identity, is_window=win32gui.IsWindow,
                    read_identity=_window_identity,
                )
                if leave_open is None:
                    scratch_window.close_window(own_handle)
                else:
                    print(f"[!] {run_label}: Notepad left open: {leave_open}")
        except Exception as exc:
            print(f"[!] could not close the Notepad window: {exc!r}")
        if process is not None:
            shutdown_event.set()
            child_info = verify_child_exit(process)
            print(
                f"[+] {run_label}: Input child pid={child_info['pid']} "
                f"exitcode={child_info['exitcode']} "
                f"child_exited={child_info['child_exited']}"
            )
        if app is not None:
            try:
                app.shm.close()
            except Exception:
                pass
        if shm is not None:
            shm.close()
            try:
                shm.unlink()
            except FileNotFoundError:
                pass

    if stop_reason is not None:
        # Reported as stopped, never as a measured result (.1.11).
        return mark_stopped(record, stop_reason)

    comparison = compare_words(expected, normalize_words(document_text))
    missing_periods, extra_periods = count_periods(
        expected_text(sentences), document_text
    )
    elapsed = (finished - started[0]) if started and finished else 0.0
    record.update({
        "status": "measured",
        "expected": comparison.expected,
        "got": comparison.got,
        "missing": comparison.missing,
        "duplicated": comparison.duplicated,
        "out_of_order": comparison.out_of_order,
        "cut": comparison.cut,
        "extra": comparison.extra,
        "missing_periods": missing_periods,
        "extra_periods": extra_periods,
        "elapsed_s": elapsed,
        "failed_sends": failures,
        "differences": comparison.differences,
        "document_text": document_text,
        "child_pid": child_info["pid"] if child_info else None,
        "child_exitcode": child_info["exitcode"] if child_info else None,
        "child_terminated": child_info["terminated"] if child_info else None,
        "child_exited": child_info["child_exited"] if child_info else None,
    })
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce dictation paste loss through the real "
        "Logic-to-Input path, into a scratch Notepad."
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--check-path", type=Path, default=DEFAULT_CHECK_PATH)
    parser.add_argument(
        "--word-gap-ms", type=float, default=DEFAULT_WORD_GAP_MS,
        help="time between sends inside one utterance (default: the "
        "2026-10-19 median, 112 ms)",
    )
    parser.add_argument(
        "--sentence-gap-ms", type=float, default=DEFAULT_SENTENCE_GAP_MS,
    )
    parser.add_argument(
        "--method", choices=("paste", "unicode"), default="paste",
        help="paste: the real path through the Input process; unicode: "
        "SendInput Unicode typing from this process",
    )
    parser.add_argument(
        "--inert-listeners", action="store_true",
        help="replace the Input child's pynput listeners with do-nothing "
        "classes (default: the real listeners run)",
    )
    parser.add_argument(
        "--pre-paste-settle-ms", type=float, default=0,
        help="wait this long before each Ctrl+V chord in the Input child "
        "(default 0: no change to the Input process)",
    )
    parser.add_argument(
        "--no-paste-check", action="store_true",
        help="turn off the wh-dictation-paste-loss-load paste check in the "
        "Input child (the origin/dev baseline)",
    )
    args = parser.parse_args(argv)

    sentences = load_sentences(args.check_path)
    print(f"[+] {len(sentences)} sentences from {args.check_path}")
    print(
        f"[+] method={args.method} word_gap_ms={args.word_gap_ms} "
        f"sentence_gap_ms={args.sentence_gap_ms} "
        f"inert_listeners={args.inert_listeners} "
        f"pre_paste_settle_ms={args.pre_paste_settle_ms}"
    )

    records: list[dict] = []
    exit_code = 0
    for index in range(1, args.runs + 1):
        record = run_once(args, sentences, args.label, index)
        records.append(record)
        if record["status"] != "measured":
            lead = "stopped, no result: " if (
                record["status"] == STATUS_STOPPED
            ) else ""
            print(f"[x] run {index}: {lead}{record.get('reason', 'failed')}")
            exit_code = 2
            break
        print(format_result_line(record["run"], record))
        for difference in record["differences"]:
            print(f"    {difference}")
        for failure in record["failed_sends"]:
            print(f"    [!] failed send: {failure}")

    if args.out is not None:
        args.out.write_text(
            json.dumps({"runs": records}, indent=2), encoding="utf-8",
        )
        print(f"[+] wrote {args.out}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
