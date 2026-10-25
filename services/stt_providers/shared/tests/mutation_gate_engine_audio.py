"""Mutation gate for wh-first-sound-lost.1 (the opt-in engine audio saver).

Acceptance item 6 says the provider deletes the files this feature wrote when
they are older than 2 days, never deletes any other file, and that a mutation
gate covers the age check and the file-name check. Groups A and C below are
that gate. Group B keeps the stage C mutations (hand-off to the saver, copy of
the samples, local clock, hourly default, sample-rate wiring) so one script
re-proves them.

  A  the age check, the name check, and the hourly loop of the running saver
  B  stage C: M1 (early read of last_final_audio), M2 (no copy in submit),
     M3 (UTC clock), M4 (24 hour default interval), M5 (fixed sample rate in
     the Parakeet provider main), M9 (save on a partial chunk)
  C  D1 (deletion while the key is off): class _OffCleanup and the call in
     create_from_environment

Run it from services/stt_providers/shared:

    python tests/mutation_gate_engine_audio.py            # the whole set
    python tests/mutation_gate_engine_audio.py name       # one, by name filter
    python tests/mutation_gate_engine_audio.py --check    # patterns only

Notes on mutants that leave a daemon thread behind: C1, C3 and C5 can start a
cleanup thread that the unmutated code never starts, or that never ends. The
threads are daemon threads, so the pytest process still exits.

Mutations the bead comment names that are NOT written as named (see the
dispatcher report): M2's second edit (convert in _write before mkdir) is an
equivalent mutant once submit copies, and removing the is_file check alone is
an equivalent mutant because os.remove cannot remove a folder. Group A entry
`delete-removes-folders-too` carries the version of that check that can fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path; this keeps it working when the gate is
# invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402
from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent

PARAKEET = PROVIDERS / "sherpa_offline_parakeet_stt_server"
GOOGLE = PROVIDERS / "google_stt_server"

SAVER = SHARED / "shared_stt" / "engine_audio_saver.py"
PROCESSOR = SHARED / "shared_stt" / "audio_processor.py"

SAVER_TESTS = "tests/test_engine_audio_saver.py"
HAND_OFF_TESTS = "tests/test_audio_processor.py::TestEngineAudioSaverHandOff"
WIRING_TESTS = "tests/test_engine_audio_wiring.py"


# --------------------------------------------------------------------------
# The launcher: start pytest directly, never through ``uv run``
# --------------------------------------------------------------------------
# The runner's default launcher starts pytest through scripts/run_tests.py,
# which runs ``uv run pytest`` in the service directory. This gate must not
# let uv run in services/stt_providers/shared: the shared package has no venv
# of its own in a worktree (.claude/scripts/prepare_worktree.py leaves it
# out on purpose), and a uv sync there would build a .venv inside the
# worktree, which makes the worktree undeletable. So pytest starts directly.
#
# The shared tests run under the Google provider's interpreter, not the
# Parakeet one: the shared test directory needs pytest_asyncio, which the
# Parakeet venv does not install, and it is collected as one rootdir. (They
# ran under the Distil-Whisper provider's interpreter until that provider
# was deleted.) The Parakeet provider runs under its own venv. Every pytest
# runs with the service directory as its working directory. The runner
# already sets COLUMNS=1000 and PYTHONDONTWRITEBYTECODE=1 for the child
# (mutation_gate_runner._invoke), so the failure reasons stay in the short
# summary and a same-size mutant cannot run from stale bytecode.
def _launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple."""
    owner = GOOGLE if service.resolve() == SHARED.resolve() else service
    python = owner / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(
            f"No interpreter for {service}: {python} does not exist. Run "
            f".claude/scripts/prepare_worktree.py to sync the providers.")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


# --------------------------------------------------------------------------
# Test name groups. Names are the part after "::" without a parameter list;
# the runner strips "[...]" from both the collected and the failed names.
# --------------------------------------------------------------------------

_YOUNG_AND_OLD = [
    "test_a_matching_file_older_than_two_days_is_deleted",
    "test_a_matching_file_younger_than_two_days_stays",
]

_NOT_OURS = [
    "test_a_file_that_is_not_ours_is_never_deleted",
    "test_anything_else_does_not",
]

# The hand-off tests that each need a submit on the final decode.
_HAND_OFF_EXPECT = [
    "test_the_saver_is_called_after_send_final_and_before_reset",
    "test_the_saver_gets_the_engines_final_audio",
    "test_a_partial_decode_before_the_endpoint_is_not_saved",
]


def _re_line(body: str) -> str:
    """The one source line that holds the file-name regular expression."""
    return '    r"' + body + '", re.ASCII\n'


# The pattern in engine_audio_saver.py, written as a raw string here so that
# the backslashes are the ones the source holds.
_RE = r"engine-audio_\d{8}-\d{6}-\d{3}_[a-z0-9_]+_utt\d+\.wav\Z"


# --------------------------------------------------------------------------
# A: the age check, the name check, and the hourly loop
# --------------------------------------------------------------------------

AGE_AND_NAME_MUTATIONS = [
    {
        # Direction of the age comparison flipped: old files are skipped and
        # young files are deleted. Equal ages still stay, so the boundary
        # test passes and only the three tests below can see it.
        "name": "age-comparison-flipped",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "st_mtime <= MAX_AGE_SECONDS:\n",
        "new": "st_mtime >= MAX_AGE_SECONDS:\n",
        "expect": _YOUNG_AND_OLD + ["test_a_fresh_file_stays"],
    },
    {
        # The limit is ten times too long: a 3 day old file stays. The test
        # ages are MAX_AGE_SECONDS plus a margin, so the margin cannot reach
        # 10 times the limit.
        "name": "age-limit-times-ten",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "st_mtime <= MAX_AGE_SECONDS:\n",
        "new": "st_mtime <= MAX_AGE_SECONDS * 10:\n",
        "expect": [
            "test_a_matching_file_older_than_two_days_is_deleted",
            "test_the_count_and_the_survivors_with_a_mixed_folder",
            "test_the_age_is_judged_against_the_now_argument",
            "test_start_deletes_old_files_at_once",
        ],
    },
    {
        # No age limit at all: every file of this feature that has an age
        # above zero is deleted. A file of age 0 stays, so the fresh-file
        # test cannot see this one.
        "name": "age-limit-zero",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "st_mtime <= MAX_AGE_SECONDS:\n",
        "new": "st_mtime <= 0:\n",
        "expect": [
            "test_a_matching_file_younger_than_two_days_stays",
            "test_the_count_and_the_survivors_with_a_mixed_folder",
            "test_the_age_is_judged_against_the_now_argument",
        ],
    },
    {
        # The boundary: the rule is "more than 2 days", so a file that is
        # exactly 2 days old stays. `<` deletes it.
        "name": "age-boundary-deletes-exactly-two-days",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "st_mtime <= MAX_AGE_SECONDS:\n",
        "new": "st_mtime < MAX_AGE_SECONDS:\n",
        "expect": ["test_a_file_exactly_two_days_old_stays"],
    },
    {
        # The constant itself. Every other test builds its ages from the
        # imported constant, so only the test that pins the value can see
        # this one.
        "name": "max-age-one-day",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "MAX_AGE_SECONDS = 2 * 24 * 3600\n",
        "new": "MAX_AGE_SECONDS = 1 * 24 * 3600\n",
        "expect": ["test_two_days_and_one_hour"],
    },
    {
        # The name check removed: any old regular file in the folder goes.
        # `if False:` rather than deleting the check, because a deleted
        # `if` with its `continue` body is not a statement to keep.
        "name": "name-check-removed",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            if not is_saver_file(entry.name):\n"
                "                continue\n"),
        "new": ("            if False:\n"
                "                continue\n"),
        "expect": [
            "test_a_file_that_is_not_ours_is_never_deleted",
            "test_the_count_and_the_survivors_with_a_mixed_folder",
            "test_start_deletes_old_files_at_once",
            "test_off_deletes_an_old_saver_file_and_only_that",
        ],
    },
    {
        # The leading anchor: re.match anchors at the start, re.search does
        # not, so "x" + a good name and "dir/" + a good name match.
        "name": "name-regex-start-anchor-dropped",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "    return _SAVER_FILE_RE.match(name) is not None\n",
        "new": "    return _SAVER_FILE_RE.search(name) is not None\n",
        "expect": _NOT_OURS,
    },
    {
        # The trailing anchor dropped: a suffix after ".wav" matches.
        "name": "name-regex-end-anchor-dropped",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace(r"\Z", "")),
        "expect": _NOT_OURS,
    },
    {
        # \Z weakened to $: a name that ends in a newline matches.
        "name": "name-regex-end-anchor-weakened-to-dollar",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace(r"\Z", "$")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # The date part loosened from exactly 8 digits to any number.
        "name": "name-regex-date-width-loosened",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace(r"_\d{8}-", r"_\d+-")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # The provider part accepts upper case letters.
        "name": "name-regex-provider-accepts-upper-case",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace("[a-z0-9_]+", "[A-Za-z0-9_]+")),
        "expect": _NOT_OURS,
    },
    {
        # The provider part accepts a hyphen.
        "name": "name-regex-provider-accepts-hyphen",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace("[a-z0-9_]+", "[a-z0-9_-]+")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # The provider part may be empty.
        "name": "name-regex-provider-may-be-empty",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace("[a-z0-9_]+", "[a-z0-9_]*")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # The utterance number may be missing.
        "name": "name-regex-utterance-number-optional",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace(r"_utt\d+", r"_utt\d*")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # Anything word-like may follow "utt".
        "name": "name-regex-utterance-number-accepts-junk",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace(r"_utt\d+", r"_utt\w+")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # The extension part accepts any extension, not only ".wav".
        "name": "name-regex-extension-loosened",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": _re_line(_RE),
        "new": _re_line(_RE.replace(r"\.wav", r"\.\w+")),
        "expect": ["test_anything_else_does_not"],
    },
    {
        # The folder check. Removing `is_file` alone changes nothing a test
        # can see: os.remove cannot remove a folder, so the PermissionError
        # is swallowed by the surrounding except. The check earns its place
        # only if the removal is able to remove a folder, which is what a
        # later change from os.remove to shutil.rmtree would do. So this
        # mutant drops the check AND removes folders. `__import__` keeps the
        # name `shutil` out of the function's local names.
        "name": "delete-removes-folders-too",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            if not entry.is_file(follow_symlinks=False):\n"
                "                continue\n"
                "            if now - entry.stat(follow_symlinks=False)"
                ".st_mtime <= MAX_AGE_SECONDS:\n"
                "                continue\n"
                "            os.remove(entry.path)\n"),
        "new": ("            if now - entry.stat(follow_symlinks=False)"
                ".st_mtime <= MAX_AGE_SECONDS:\n"
                "                continue\n"
                "            if entry.is_dir(follow_symlinks=False):\n"
                "                __import__(\"shutil\").rmtree(entry.path)\n"
                "            else:\n"
                "                os.remove(entry.path)\n"),
        "expect": ["test_a_folder_with_a_matching_name_is_never_deleted"],
    },
    {
        # The running saver judges age against the wrong clock: the real
        # time, not the clock it was given.
        "name": "running-cleanup-ignores-the-given-clock",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "            delete_old_files(self.folder, self._clock())\n",
        "new": "            delete_old_files(self.folder, time.time())\n",
        "expect": [
            "test_the_first_pass_does_not_wait_for_the_interval",
            "test_it_deletes_again_while_it_runs",
        ],
    },
    {
        # Hourly deletion: after the pass at start, the empty-queue branch
        # no longer deletes. `pass` rather than deleting the statement, so
        # the branch keeps a body that is not the re-arm line.
        "name": "running-loop-never-cleans-again",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            except queue.Empty:\n"
                "                self._cleanup()\n"),
        "new": ("            except queue.Empty:\n"
                "                pass\n"),
        "expect": [
            "test_it_deletes_again_while_it_runs",
            "test_a_failing_deletion_does_not_stop_the_writer",
        ],
    },
    {
        # Hourly deletion: the interval is re-armed to a day, so the pass
        # that follows the first timeout never comes.
        "name": "running-loop-rearms-with-a-day",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            except queue.Empty:\n"
                "                self._cleanup()\n"
                "                next_cleanup = time.monotonic() + "
                "self._cleanup_interval\n"),
        "new": ("            except queue.Empty:\n"
                "                self._cleanup()\n"
                "                next_cleanup = time.monotonic() + 86400.0\n"),
        "expect": ["test_a_failing_deletion_does_not_stop_the_writer"],
    },
]


# --------------------------------------------------------------------------
# B: stage C
# --------------------------------------------------------------------------

STAGE_C_MUTATIONS = [
    {
        # M1, engine endpoint path. The processor reads last_final_audio
        # before process_audio and the early value is what the save sees.
        # Written as a read before the call and a write back after it, so
        # that one edit makes _save_engine_audio see the early value without
        # a second edit in that method. The real engines publish the audio
        # inside the decode call, so the early value is None.
        "name": "m1-engine-path-reads-the-audio-before-the-decode",
        "service": SHARED,
        "test_file": HAND_OFF_TESTS,
        "file": PROCESSOR,
        "old": "        self.engine.process_audio(float32_audio)\n",
        "new": ("        _early_final_audio = getattr("
                "self.engine, \"last_final_audio\", None)\n"
                "        self.engine.process_audio(float32_audio)\n"
                "        self.engine.last_final_audio = _early_final_audio\n"),
        "expect": _HAND_OFF_EXPECT,
    },
    {
        # M1, forced endpoint path: the read comes before finalize().
        "name": "m1-forced-path-reads-the-audio-before-the-decode",
        "service": SHARED,
        "test_file": HAND_OFF_TESTS,
        "file": PROCESSOR,
        "old": ("            _load_t0 = time.perf_counter()\n"
                "            finalize()\n"),
        "new": ("            _early_final_audio = getattr("
                "self.engine, \"last_final_audio\", None)\n"
                "            _load_t0 = time.perf_counter()\n"
                "            finalize()\n"
                "            self.engine.last_final_audio = "
                "_early_final_audio\n"),
        "expect": _HAND_OFF_EXPECT,
    },
    {
        # M2: submit keeps a reference to the caller's array instead of a
        # copy. The second edit of the original M2 (convert in _write before
        # the folder is made) is not a mutation of its own: once submit
        # copies, the order of conversion and open cannot change the file,
        # and the copy test says so in its docstring. This is the minimal
        # edit the copy test catches.
        "name": "m2-submit-keeps-the-callers-array",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            samples = np.array(audio, dtype=np.float32, "
                "copy=True)\n"),
        "new": "            samples = np.asarray(audio, dtype=np.float32)\n",
        "expect": [
            "test_a_change_to_the_callers_array_after_submit_does_not_"
            "reach_the_file",
        ],
    },
    {
        # M3: the default clock is UTC. The test skips at a UTC offset of 0;
        # this host runs EDT, so it runs here.
        "name": "m3-default-clock-is-utc",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("        local_now: Callable[[], datetime] = datetime.now,\n"),
        "new": ("        local_now: Callable[[], datetime] = datetime.utcnow,\n"),
        "expect": [
            "test_the_default_clock_names_the_file_with_the_local_time",
        ],
    },
    {
        # M4a: the saver's own default interval is a day. The next line is
        # part of the pattern because create_from_environment has the same
        # first line.
        "name": "m4-saver-default-interval-is-a-day",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("        cleanup_interval: float = CLEANUP_INTERVAL_SECONDS,\n"
                "        queue_size: int = 16,\n"),
        "new": ("        cleanup_interval: float = 86400.0,\n"
                "        queue_size: int = 16,\n"),
        "expect": [
            "test_the_default_cleanup_interval_is_one_hour",
            "test_the_saver_it_builds_deletes_once_an_hour",
        ],
    },
    {
        # M4b: create_from_environment passes a day to the saver it builds.
        "name": "m4-factory-passes-a-day-to-the-saver",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("    saver = EngineAudioSaver(provider, folder, "
                "sample_rate=sample_rate)\n"),
        "new": ("    saver = EngineAudioSaver(\n"
                "        provider, folder, sample_rate=sample_rate,\n"
                "        cleanup_interval=86400.0)\n"),
        "expect": ["test_the_saver_it_builds_deletes_once_an_hour"],
    },
    {
        # M5, Parakeet: the saver is built at a fixed 16000 Hz instead of
        # the engine sample rate.
        "name": "m5-parakeet-saver-ignores-the-engine-sample-rate",
        "service": PARAKEET,
        "test_file": WIRING_TESTS,
        "file": PARAKEET / "main.py",
        "old": ("        self._engine_audio_saver = create_from_environment(\n"
                "            \"parakeet_tdt\", sample_rate=sample_rate)"),
        "new": ("        self._engine_audio_saver = create_from_environment(\n"
                "            \"parakeet_tdt\", sample_rate=16000)"),
        "expect": ["test_the_saver_is_built_with_the_engine_sample_rate"],
    },
    {
        # M9: the engine path saves on a chunk that is not an endpoint. The
        # line is the last statement of the block that handles a changed
        # result; a partial chunk with new text reaches it.
        "name": "m9-a-partial-chunk-is-saved",
        "service": SHARED,
        "test_file": HAND_OFF_TESTS,
        "file": PROCESSOR,
        "old": "                self.engine.last_result = text\n",
        "new": ("                if not is_endpoint:\n"
                "                    self._save_engine_audio("
                "final_sent=False)\n"
                "                self.engine.last_result = text\n"),
        "expect": ["test_a_partial_decode_before_the_endpoint_is_not_saved"],
    },
]


# --------------------------------------------------------------------------
# C: D1, deletion while the key is off
# --------------------------------------------------------------------------

_OFF_WHOLE = [
    "test_off_deletes_an_old_saver_file_and_only_that",
]

OFF_CLEANUP_MUTATIONS = [
    {
        # C1: the pass deletes nothing. `pass` keeps the try block valid.
        "name": "c1-off-pass-deletes-nothing",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "            delete_old_files(self.folder, self.clock())\n",
        "new": "            pass\n",
        "expect": _OFF_WHOLE + [
            "test_off_keeps_deleting_while_a_young_saver_file_remains",
            "test_off_when_deletion_raises_warns_once_and_never_raises",
        ],
    },
    {
        # C2: every entry counts as a file of this feature when the pass
        # decides whether to keep a thread, so a file of someone else keeps
        # the thread alive.
        "name": "c2-off-pass-counts-every-file-as-ours",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": "any(is_saver_file(entry.name) for entry in entries)",
        "new": "any(True for entry in entries)",
        "expect": _OFF_WHOLE,
    },
    {
        # C3: the pass always reports that a file remains, so the thread
        # never ends. It is a daemon thread; the test asserts it ended.
        "name": "c3-off-pass-always-reports-a-file",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("                return any(is_saver_file(entry.name) "
                "for entry in entries)\n"),
        "new": "                return True\n",
        "expect": _OFF_WHOLE + [
            "test_off_keeps_deleting_while_a_young_saver_file_remains",
        ],
    },
    {
        # C4: the warning budget of _OffCleanup is gone, so every failed
        # pass warns. The lines after the check are part of the pattern
        # because EngineAudioSaver._warn_once has the same check.
        "name": "c4-off-cleanup-warns-every-time",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            if self._warned:\n"
                "                return\n"
                "            self._warned = True\n"
                "        try:\n"
                "            logger.warning(\n"
                "                \"Cannot delete old engine audio files in "
                "%s: %s: %s. \"\n"),
        "new": ("            if False:\n"
                "                return\n"
                "            self._warned = True\n"
                "        try:\n"
                "            logger.warning(\n"
                "                \"Cannot delete old engine audio files in "
                "%s: %s: %s. \"\n"),
        "expect": [
            "test_off_when_deletion_raises_warns_once_and_never_raises",
        ],
    },
    {
        # C5: run_once always starts the thread, even when no file of this
        # feature remains. The next line is part of the pattern because the
        # loop has the same check at a deeper indent.
        "name": "c5-off-always-starts-the-thread",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("            if not self._pass():\n"
                "                return\n"
                "            threading.Thread(\n"),
        "new": ("            if False:\n"
                "                return\n"
                "            threading.Thread(\n"),
        "expect": _OFF_WHOLE + [
            "test_off_with_no_folder_creates_none_and_starts_no_thread",
        ],
    },
    {
        # C6: create_from_environment no longer runs the pass when the key
        # is off.
        "name": "c6-off-runs-no-pass",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("        _OffCleanup(provider, folder, cleanup_interval, "
                "clock).run_once()\n"),
        "new": "        pass\n",
        "expect": _OFF_WHOLE + [
            "test_off_keeps_deleting_while_a_young_saver_file_remains",
            "test_off_when_deletion_raises_warns_once_and_never_raises",
        ],
    },
    {
        # C7: the default interval of the key-off path is a day, not an hour.
        # `    *,` makes the pattern unique: EngineAudioSaver.__init__ has the
        # same default line at a deeper indent.
        "name": "c7-off-default-interval-not-an-hour",
        "service": SHARED,
        "test_file": SAVER_TESTS,
        "file": SAVER,
        "old": ("    *,\n"
                "    cleanup_interval: float = CLEANUP_INTERVAL_SECONDS,\n"),
        "new": ("    *,\n"
                "    cleanup_interval: float = 86400.0,\n"),
        "expect": [
            "test_off_passes_the_hourly_interval_by_default",
        ],
    },
]


MUTATIONS = (
    AGE_AND_NAME_MUTATIONS
    + STAGE_C_MUTATIONS
    + OFF_CLEANUP_MUTATIONS
)


if __name__ == "__main__":
    sys.exit(run(MUTATIONS))
