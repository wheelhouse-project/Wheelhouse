"""Mutation gate for the training-word part of WSForwarder
(wh-first-sound-lost.23, acceptance item 31).

The code is in shared_stt/ws_forwarder.py: LEAD_WORD_COMMANDS,
LEAD_WORD_STAGES and the two stage sets for the optional fields, the command
dispatch (request_id coercion, call_soon_threadsafe, the no-callback log),
send_lead_word_result (the stage check, the payload, the per-stage fields),
the lead_word_result branch of the sender, the lead_word field of the
capabilities frame, and the microphone_device property. The tests are
tests/test_ws_forwarder_lead_word.py. The recording mode of the audio path is
in mutation_gate_chunk_recorder.py.

Run it from services/stt_providers/shared:

    python tests/mutation_gate_ws_forwarder_lead_word.py            # the whole set
    python tests/mutation_gate_ws_forwarder_lead_word.py name       # one, by name filter
    python tests/mutation_gate_ws_forwarder_lead_word.py --check    # patterns only
    python tests/mutation_gate_ws_forwarder_lead_word.py --equivalent
        # runs EQUIVALENT_MUTANTS; every one of them must be reported SURVIVED

The async tests need websockets and pytest-asyncio, which the shared service
venv has; the launcher starts pytest under the
shared venv's interpreter, never through ``uv run``. Each mutation runs only
the tests that can see it, selected by node id, because the whole file takes
about 47 seconds. A pattern of one line gets a final newline added to both
texts, so it matches a whole line. A pattern of several lines is used as
written.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402
from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]

FORWARDER = SHARED / "shared_stt" / "ws_forwarder.py"
FILE = "tests/test_ws_forwarder_lead_word.py"


def _launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple."""
    python = SHARED / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(
            f"No interpreter for the shared service: {python} does not exist. "
            "Run .claude/scripts/prepare_worktree.py to sync the providers.")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:cacheprovider",
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


def _sel(*names):
    return tuple(f"{FILE}::{name}" for name in names)


# --- Test names ----------------------------------------------------------
T_CMD = "test_each_command_reaches_the_callback_with_its_request_id"
T_NOID = "test_a_command_with_no_request_id_gives_an_empty_one"
T_BADID = "test_a_request_id_that_is_not_text_gives_an_empty_one"
T_NOCB = "test_a_command_with_no_callback_registered_does_not_crash"
T_LISTEN = "test_listening_frame_has_no_key_path_and_no_state_fields"
T_CHECK = "test_checking_frame_has_no_key_path_and_no_state_fields"
T_STATUS = "test_status_frame_has_key_path_and_state_fields"
T_PASSED = "test_passed_frame_has_measures_and_key_path_but_no_state_fields"
T_REFUSED = "test_refused_frame_has_reason_and_measures"
T_STATE = "test_state_stages_carry_state_fields_and_no_key_path"
T_FAILED = "test_failed_frame_carries_the_error_text"
T_UNKNOWN = "test_unknown_stage_is_refused"
T_BEFORE = "test_send_lead_word_result_before_start_does_not_raise"
T_BOOLS = "test_the_state_fields_are_sent_as_booleans"
T_CAPS = "test_capabilities_carry_the_lead_word_field_when_the_provider_gives_one"
T_CAPS_NONE = "test_capabilities_have_no_lead_word_field_without_a_provider_callable"
T_CAPS_FAIL = "test_a_failing_lead_word_callable_leaves_the_field_out_and_sends_the_frame"
T_MIC = "test_microphone_device_property_reads_the_last_name_sent"
# wh-training-word-noisy-reject: the noise advice of the passed frame.
T_ADVICE = "test_passed_frame_carries_the_noise_advice_as_a_boolean"
T_ADVICE_ONLY = "test_no_other_stage_carries_the_noise_advice"

SEL_CMD = _sel(T_CMD, T_NOID, T_BADID, T_NOCB)
SEL_FRAMES = _sel(T_LISTEN, T_CHECK, T_STATUS, T_PASSED, T_REFUSED, T_STATE,
                  T_FAILED, T_UNKNOWN, T_BEFORE, T_BOOLS, T_ADVICE, T_ADVICE_ONLY)
SEL_CAPS = _sel(T_CAPS, T_CAPS_NONE, T_CAPS_FAIL, T_MIC)


def m(name, selection, old, new, expect):
    if "\n" not in old.rstrip("\n"):
        if not old.endswith("\n"):
            old += "\n"
        if not new.endswith("\n"):
            new = new + "\n" if new else ""
    return {
        "name": name,
        "service": SHARED,
        "test_file": selection,
        "file": FORWARDER,
        "old": old,
        "new": new,
        "expect": expect,
    }


def cmd(name, old, new, expect):
    return m(name, SEL_CMD, old, new, expect)


def frame(name, old, new, expect):
    return m(name, SEL_FRAMES, old, new, expect)


def caps(name, old, new, expect):
    return m(name, SEL_CAPS, old, new, expect)


_STAGES_A = '    "status", "listening", "checking", "passed", "refused", "kept",'
_STAGES_B = '    "restored", "off", "on", "failed",'
_KEY = '_LEAD_WORD_KEY_STAGES = frozenset({"status", "passed"})'
_STATE = '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off", "on"})'
_CALL = ("                                self._loop.call_soon_threadsafe(\n"
         "                                    self.lead_word_callback,\n"
         "                                    msg_type, request_id,\n"
         "                                )\n")

_STAGE_CHECK = '            raise ValueError(f"unknown lead_word_result stage: {stage!r}")\n'
_NO_LOOP = "        if not self._loop or not self._queue:\n            return\n"

MUTATIONS = [
    # ---- the seven commands --------------------------------------------------
    cmd("command-status-unknown", '    "lead_word_status",', "", [T_CMD]),
    cmd("command-record-unknown", '    "lead_word_record",', "", [T_CMD]),
    cmd("command-keep-unknown", '    "lead_word_keep",', "", [T_CMD]),
    cmd("command-discard-unknown", '    "lead_word_discard",', "", [T_CMD]),
    cmd("command-restore-unknown", '    "lead_word_restore",', "", [T_CMD]),
    cmd("command-off-unknown", '    "lead_word_off",', "", [T_CMD]),
    cmd("command-on-unknown", '    "lead_word_on",', "", [T_CMD]),

    # ---- the ten stages ------------------------------------------------------
    frame("stage-status-unknown", _STAGES_A,
          '    "listening", "checking", "passed", "refused", "kept",', [T_STATUS]),
    frame("stage-listening-unknown", _STAGES_A,
          '    "status", "checking", "passed", "refused", "kept",', [T_LISTEN]),
    frame("stage-checking-unknown", _STAGES_A,
          '    "status", "listening", "passed", "refused", "kept",', [T_CHECK]),
    frame("stage-passed-unknown", _STAGES_A,
          '    "status", "listening", "checking", "refused", "kept",', [T_PASSED]),
    frame("stage-refused-unknown", _STAGES_A,
          '    "status", "listening", "checking", "passed", "kept",', [T_REFUSED]),
    frame("stage-kept-unknown", _STAGES_A,
          '    "status", "listening", "checking", "passed", "refused",', [T_STATE]),
    frame("stage-restored-unknown", _STAGES_B,
          '    "off", "on", "failed",', [T_STATE]),
    frame("stage-off-unknown", _STAGES_B,
          '    "restored", "on", "failed",', [T_STATE]),
    frame("stage-on-unknown", _STAGES_B,
          '    "restored", "off", "failed",', [T_STATE]),
    frame("stage-failed-unknown", _STAGES_B,
          '    "restored", "off", "on",', [T_FAILED]),
    frame("stage-bogus-accepted", _STAGES_B,
          '    "restored", "off", "on", "failed", "bogus",', [T_UNKNOWN]),

    # ---- which stages carry key_path and the state fields --------------------
    frame("key-stages-lose-status", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"passed"})', [T_STATUS]),
    frame("key-stages-lose-passed", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"status"})', [T_PASSED]),
    frame("key-stages-gain-listening", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"status", "passed", "listening"})', [T_LISTEN]),
    frame("key-stages-gain-checking", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"status", "passed", "checking"})', [T_CHECK]),
    frame("key-stages-gain-refused", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"status", "passed", "refused"})', [T_REFUSED]),
    frame("key-stages-gain-kept", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"status", "passed", "kept"})', [T_STATE]),
    frame("key-stages-gain-failed", _KEY,
          '_LEAD_WORD_KEY_STAGES = frozenset({"status", "passed", "failed"})', [T_FAILED]),
    frame("state-stages-lose-status", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"kept", "restored", "off", "on"})', [T_STATUS]),
    frame("state-stages-lose-kept", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "restored", "off", "on"})', [T_STATE]),
    frame("state-stages-lose-restored", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "off", "on"})', [T_STATE]),
    frame("state-stages-lose-off", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "on"})', [T_STATE]),
    frame("state-stages-lose-on", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off"})', [T_STATE]),
    frame("state-stages-gain-listening", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off", "on", "listening"})',
          [T_LISTEN]),
    frame("state-stages-gain-checking", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off", "on", "checking"})',
          [T_CHECK]),
    frame("state-stages-gain-passed", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off", "on", "passed"})',
          [T_PASSED]),
    frame("state-stages-gain-refused", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off", "on", "refused"})',
          [T_REFUSED]),
    frame("state-stages-gain-failed", _STATE,
          '_LEAD_WORD_STATE_STAGES = frozenset({"status", "kept", "restored", "off", "on", "failed"})',
          [T_FAILED]),

    # ---- the command dispatch ------------------------------------------------
    cmd("dispatch-request-id-not-coerced", "                        if not isinstance(request_id, str):",
        "                        if False:", [T_BADID]),
    cmd("dispatch-request-id-coerced-always", "                        if not isinstance(request_id, str):",
        "                        if True:", [T_CMD]),
    cmd("dispatch-request-id-default-is-none-text", '                        request_id = data.get("request_id", "")',
        '                        request_id = data.get("request_id", "none")', [T_NOID]),
    cmd("dispatch-request-id-always-empty", '                        request_id = data.get("request_id", "")',
        '                        request_id = ""', [T_CMD]),
    cmd("dispatch-callback-called-directly", _CALL,
        "                                self.lead_word_callback(msg_type, request_id)\n", [T_CMD]),
    cmd("dispatch-arguments-swapped", "                                    msg_type, request_id,",
        "                                    request_id, msg_type,", [T_CMD]),
    cmd("dispatch-callback-not-scheduled",
        "                            if self._loop:\n"
        "                                self._loop.call_soon_threadsafe(\n"
        "                                    self.lead_word_callback,\n",
        "                            if not self._loop:\n"
        "                                self._loop.call_soon_threadsafe(\n"
        "                                    self.lead_word_callback,\n", [T_CMD]),
    cmd("dispatch-callback-test-inverted", "                        if self.lead_word_callback:",
        "                        if not self.lead_word_callback:", [T_CMD, T_NOCB]),
    cmd("dispatch-no-callback-log-changed", '                                "no callback registered")',
        '                                "ignored")', [T_NOCB]),

    # ---- the lead_word_result branch of the sender ---------------------------
    frame("sender-payload-empty", '                                payload = dict(msg_data.get("payload", {}))',
          "                                payload = {}", [T_STATUS]),
    frame("sender-payload-wrong-key", '                                payload = dict(msg_data.get("payload", {}))',
          '                                payload = dict(msg_data.get("body", {}))', [T_STATUS]),

    # ---- send_lead_word_result -----------------------------------------------
    frame("send-stage-check-off", "        if stage not in LEAD_WORD_STAGES:", "        if False:", [T_UNKNOWN]),
    frame("send-stage-check-inverted", "        if stage not in LEAD_WORD_STAGES:",
          "        if stage in LEAD_WORD_STAGES:", [T_UNKNOWN]),
    frame("send-stage-check-returns", '            raise ValueError(f"unknown lead_word_result stage: {stage!r}")',
          "            return", [T_UNKNOWN]),
    frame("send-stage-checked-after-the-loop-check",
          "        if stage not in LEAD_WORD_STAGES:\n"
          '            raise ValueError(f"unknown lead_word_result stage: {stage!r}")\n'
          "        if not self._loop or not self._queue:\n            return\n",
          "        if not self._loop or not self._queue:\n            return\n"
          "        if stage not in LEAD_WORD_STAGES:\n"
          '            raise ValueError(f"unknown lead_word_result stage: {stage!r}")\n',
          [T_UNKNOWN]),
    frame("send-no-loop-returns-an-error", _STAGE_CHECK + _NO_LOOP,
          _STAGE_CHECK + "        if not self._loop or not self._queue:\n"
          "            raise RuntimeError(\"not started\")\n",
          [T_BEFORE]),
    frame("send-type-changed", '            "type": "lead_word_result",\n            "request_id": request_id,',
          '            "type": "lead_word",\n            "request_id": request_id,', [T_LISTEN]),
    frame("send-request-id-empty", '            "request_id": request_id,', '            "request_id": "",', [T_LISTEN]),
    frame("send-stage-fixed", '            "stage": stage,', '            "stage": "status",', [T_LISTEN]),
    frame("send-reason-empty", '            "reason": reason,', '            "reason": "",', [T_REFUSED, T_FAILED]),
    frame("send-measures-empty", '            "measures": dict(measures or {}),',
          '            "measures": {},', [T_REFUSED, T_PASSED]),
    frame("send-measures-none", '            "measures": dict(measures or {}),',
          '            "measures": measures,', [T_LISTEN]),
    frame("send-key-path-empty", '            payload["key_path"] = key_path',
          '            payload["key_path"] = ""', [T_STATUS, T_PASSED]),
    frame("send-key-path-test-inverted", "        if stage in _LEAD_WORD_KEY_STAGES:",
          "        if stage not in _LEAD_WORD_KEY_STAGES:", [T_STATUS, T_PASSED]),
    frame("send-key-path-always", "        if stage in _LEAD_WORD_KEY_STAGES:", "        if True:", [T_LISTEN]),
    frame("send-key-path-never", "        if stage in _LEAD_WORD_KEY_STAGES:", "        if False:", [T_STATUS]),
    frame("send-state-test-inverted", "        if stage in _LEAD_WORD_STATE_STAGES:",
          "        if stage not in _LEAD_WORD_STATE_STAGES:", [T_STATUS, T_STATE]),
    frame("send-state-always", "        if stage in _LEAD_WORD_STATE_STAGES:", "        if True:", [T_LISTEN]),
    frame("send-state-never", "        if stage in _LEAD_WORD_STATE_STAGES:", "        if False:", [T_STATUS]),
    frame("send-loaded-not-bool", '            payload["loaded"] = bool(loaded)',
          '            payload["loaded"] = loaded', [T_BOOLS]),
    frame("send-loaded-inverted", '            payload["loaded"] = bool(loaded)',
          '            payload["loaded"] = not loaded', [T_STATUS]),
    frame("send-recorded-empty", '            payload["recorded"] = recorded',
          '            payload["recorded"] = ""', [T_STATUS, T_STATE]),
    frame("send-microphone-empty", '            payload["microphone"] = microphone',
          '            payload["microphone"] = ""', [T_STATUS, T_STATE]),
    frame("send-previous-exists-not-bool", '            payload["previous_exists"] = bool(previous_exists)',
          '            payload["previous_exists"] = previous_exists', [T_BOOLS]),
    frame("send-previous-exists-inverted", '            payload["previous_exists"] = bool(previous_exists)',
          '            payload["previous_exists"] = not previous_exists', [T_STATUS]),
    # wh-first-sound-lost.23.1.1: hello.wav exists, also with no recorded date.
    frame("send-current-exists-not-bool", '            payload["current_exists"] = bool(current_exists)',
          '            payload["current_exists"] = current_exists', [T_BOOLS]),
    frame("send-current-exists-inverted", '            payload["current_exists"] = bool(current_exists)',
          '            payload["current_exists"] = not current_exists', [T_STATUS, T_STATE]),
    frame("send-advice-not-bool", '            payload["noise_advice"] = bool(noise_advice)',
          '            payload["noise_advice"] = noise_advice', [T_ADVICE]),
    frame("send-advice-inverted", '            payload["noise_advice"] = bool(noise_advice)',
          '            payload["noise_advice"] = not noise_advice', [T_PASSED, T_ADVICE]),
    frame("send-advice-always", '        if stage == "passed":\n            payload["noise_advice"]',
          '        if True:\n            payload["noise_advice"]', [T_ADVICE_ONLY]),
    frame("send-advice-never", '        if stage == "passed":\n            payload["noise_advice"]',
          '        if False:\n            payload["noise_advice"]', [T_PASSED, T_ADVICE]),
    frame("send-advice-on-the-key-stages", '        if stage == "passed":\n            payload["noise_advice"]',
          '        if stage in _LEAD_WORD_KEY_STAGES:\n            payload["noise_advice"]', [T_ADVICE_ONLY]),
    frame("send-message-without-the-payload",
          '        msg_data = {"type": "lead_word_result", "payload": payload}',
          '        msg_data = {"type": "lead_word_result", "payload": {}}', [T_LISTEN]),

    # ---- the capabilities frame ----------------------------------------------
    caps("caps-field-test-inverted", "                    if self.lead_word_capabilities is not None:",
         "                    if self.lead_word_capabilities is None:", [T_CAPS]),
    caps("caps-field-never-called", "                    if self.lead_word_capabilities is not None:",
         "                    if False:", [T_CAPS]),
    caps("caps-field-renamed", '                            capabilities["lead_word"] = (',
         '                            capabilities["lead_word_"] = (', [T_CAPS]),
    caps("caps-failure-escapes", "                        except Exception as lw_err:",
         "                        except KeyError as lw_err:", [T_CAPS_FAIL]),

    # ---- the microphone name -------------------------------------------------
    caps("microphone-none-not-empty", '        return self._microphone_device or ""',
         "        return self._microphone_device", [T_MIC]),
    caps("microphone-always-empty", '        return self._microphone_device or ""',
         '        return ""', [T_MIC]),
]

# Mutants that no test can fail for. `--equivalent` runs them; each must be
# reported SURVIVED, and one that is caught is no longer equivalent.
EQUIVALENT_MUTANTS = [
    # A request_id that is missing arrives as None, which the isinstance check
    # on the next line turns into "" -- the same result as the default "".
    cmd("eq-dispatch-request-id-default-none", '                        request_id = data.get("request_id", "")',
        '                        request_id = data.get("request_id")', [T_NOID]),
    # The frame is serialised once by the sender and the payload is not
    # changed afterwards, so copying it is not observable.
    frame("eq-sender-payload-not-copied", '                                payload = dict(msg_data.get("payload", {}))',
          '                                payload = msg_data.get("payload", {})', [T_STATUS]),
    frame("eq-send-measures-not-copied", '            "measures": dict(measures or {}),',
          '            "measures": measures or {},', [T_REFUSED]),
    # Without a callable, `if True` calls None(); the TypeError is caught by
    # the except below, which leaves the field out and writes one log line.
    # The frame is the same.
    caps("eq-caps-field-always-called", "                    if self.lead_word_capabilities is not None:",
         "                    if True:", [T_CAPS_NONE]),
    # Before start both _loop and _queue are None. Without the early return,
    # self._queue.put raises AttributeError inside the try, whose
    # `except Exception: pass` swallows it: the same silent result.
    frame("eq-send-no-loop-check", _STAGE_CHECK + _NO_LOOP,
          _STAGE_CHECK + "        if False:\n            return\n", [T_BEFORE]),
    frame("eq-send-loop-check-needs-both", _STAGE_CHECK + _NO_LOOP,
          _STAGE_CHECK + "        if not self._loop and not self._queue:\n"
          "            return\n", [T_BEFORE]),
]


if __name__ == "__main__":
    if "--equivalent" in sys.argv[1:]:
        argv = [a for a in sys.argv[1:] if a != "--equivalent"]
        print(f"EQUIVALENT mutants: {len(EQUIVALENT_MUTANTS)}; each must print SURVIVED")
        sys.exit(run(EQUIVALENT_MUTANTS, argv))
    sys.exit(run(MUTATIONS))
