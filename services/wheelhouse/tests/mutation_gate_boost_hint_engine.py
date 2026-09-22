"""Mutation gate for the boost engine qualification (wh-boost-engine-qualification, B6).

WHY THIS GATE EXISTS. The tests of this bead were written before the
source change and were seen to fail. A test can still pass for the wrong
reason, so each protected behaviour is broken here at the level of the
decision the code makes, one place at a time, and the gate confirms the
named tests go red at their own assertion.

WHAT IT DEFENDS, in the four groups criterion B6 names:

  1. The refusal condition. Only the value False refuses a pattern whose
     actions include add_hint_to_stt. Each refusal place is broken on its
     own by handing it None instead of the running value: match_complete,
     match_single_pattern, the can_continue probe, the router's greedy
     prefix probe, SpeechProcessor._usable_trailing_command, the
     hold-to-fire recheck, and _find_earliest_replacement. The catalog
     derivation of requires_hint_engine from the actions is broken in both
     stored copies (the first-word index and the built list) and in the
     trailing entry.
  2. The unknown value. None matches as today; the stream boundary in
     WebSocketManager._reset_retraction_policy_state resets to None, not
     False; _apply_capabilities stores a value only when the key is
     present; only the active client's declaration counts.
  3. The value each provider reports. The forwarder sends the key only
     when the value is not None, and sends False as False; Google reports
     True before its forwarder starts; Distil-Whisper reports its startup
     [hotwords] enabled flag; Parakeet's applies_hints_value covers four
     cases and main wires it from engine.hotwords_status.
  4. The engine switch. The value is pushed WebSocketManager ->
     SpeechHandler -> SpeechProcessor -> router and TextParser, and every
     forwarding step between the matcher's entry points is broken once.
  5. The try-it box (wh-boost-engine-qualification.1.1). pattern_tester's
     _match_entry refuses by the runtime rule; the refusal is removed and
     fed None; each of its three _match_entry calls (phrase walk, draft
     walk, draft_matches recheck) is fed None; the draft's flag is
     derived from no actions; and each main.py handler (pm_test_phrase,
     pm_test_draft) drops parser.hint_engine.

SIX SUITES, SIX INTERPRETERS. The Logic process, the shared STT package,
each provider and the release scripts have their own virtual
environment. Each suite runs under its own interpreter, from its own
directory. The default is the suite's own .venv in this tree; an
environment variable overrides it:

    BOOST_GATE_PYTHON_LOGIC, BOOST_GATE_PYTHON_SHARED,
    BOOST_GATE_PYTHON_DISTIL, BOOST_GATE_PYTHON_GOOGLE,
    BOOST_GATE_PYTHON_PARAKEET, BOOST_GATE_PYTHON_RELEASE

THE TEST SET is every test file that imports or patches a module this
branch changes, as impact-brief.py --list-tests listed it on 2026-09-21
(base d54220d3, head ddf5b013): 109 Logic files, 7 shared, 2 Parakeet,
1 each for Distil-Whisper and Google, and the release test_helpdoc_ci.py,
which runs the real matcher and catalog and so runs for every mutation
of those two files. The boost test files come FIRST and pytest runs with
-x, so a caught mutant stops at its first failure in seconds and only a
survivor pays for the whole set. The baseline runs the whole set without
-x and must be green.

READING A -x RUN. With -x there is exactly one failure record. It counts
as a catch only when it is a FAILED record whose reason is an assertion
and the exit code is 1. An ERROR record (a fixture or setup error), a
collection or import error (exit code 2 or more), a reason that is not
an assertion, and the pytest-timeout abort banner are all errors, never
catches. A catch by a test outside the mutation's expected list is still
a real assertion failure against a green baseline; it is reported as
"caught (other)" with the test's name, so a reader can see which test
did the work.

Before the baseline, the gate imports every target module under its
suite's interpreter and refuses to start unless the module file is the
target in THIS tree. A worktree borrowing the main checkout's
interpreter would otherwise test the main checkout's code and report
every mutation as a survivor.

DISCIPLINE, per the mutation-gate skill: byte IO and atomic writes;
patterns translated to each target's own line ending; exactly one match
at a line start; each mutant compiled before it runs (and in --check);
every expected test name validated against real collection; a green
baseline per suite; __pycache__ cleared and PYTHONDONTWRITEBYTECODE set;
a per-run timeout; the pytest-timeout abort banner reported as an error;
COLUMNS=1000 so the short-summary reason is kept; verdicts read only
from the short-summary section, keyed on the exit code first; each catch
confirmed as an assertion; restore in a finally that holds a Ctrl+C and
refuses to overwrite a concurrent edit; a final byte check of every
target.

Kept OUT of the selections, both unrelated to this bead: the shared
test_ws_forwarder.py, whose port binds fail on host-reserved port ranges
(WinError 10013), and the two patterns-staging test files the impact
list names, which fail to collect at ddf5b013 (their dotted file names
are not importable module names). The root tests/test_help_doc_keys.py
and tests/test_llm_help_package.py and the other release tests on the
list import none of the mutated modules (grep), so they are not run.

Run it from services/wheelhouse. NEVER while a test suite is running in
the same tree -- this file rewrites the sources that suite imports:

    <interpreter> tests/mutation_gate_boost_hint_engine.py
    <interpreter> tests/mutation_gate_boost_hint_engine.py --check
    <interpreter> tests/mutation_gate_boost_hint_engine.py <name> ...
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

WH_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = WH_DIR.parents[1]
PROVIDERS = REPO_ROOT / "services" / "stt_providers"


def _venv_python(service_dir):
    return service_dir / ".venv" / "Scripts" / "python.exe"


# The boost test files first, so that under -x a caught mutant stops
# within seconds; then every other Logic test file the impact list
# names. test_pattern_tester.py holds the try-it box refusal tests
# (group 5), so it runs third.
_LOGIC_TESTS = (
    "tests/test_boost_hint_engine.py",
    "tests/e2e/test_e2e_boost_hint_engine.py",
    "tests/test_pattern_tester.py",
    "tests/e2e/test_app_adapter.py",
    "tests/e2e/test_e2e_multiword_phrase_replacement.py",
    "tests/test_action_catalog.py",
    "tests/test_actions.py",
    "tests/test_adversarial_stt.py",
    "tests/test_ai/test_actions.py",
    "tests/test_ai/test_ai_replacement_focus_drift.py",
    "tests/test_ai/test_ask_ai_action.py",
    "tests/test_ai/test_cancel_during_running_rewrite.py",
    "tests/test_ai/test_rewrite_action.py",
    "tests/test_ai/test_silent_actions.py",
    "tests/test_ai/test_text_transform_helper.py",
    "tests/test_apply_hotword.py",
    "tests/test_audio_suppression_controls_removed.py",
    "tests/test_calibration_trigger.py",
    "tests/test_click_flow.py",
    "tests/test_command_engine_gaps.py",
    "tests/test_command_engine_replacement_editor_routing.py",
    "tests/test_continuous_scroll_action.py",
    "tests/test_continuous_scroll_patterns.py",
    "tests/test_count_pattern_survey.py",
    "tests/test_create_pattern_dialog.py",
    "tests/test_erase_synonym_for_delete.py",
    "tests/test_forwarded_log_time.py",
    "tests/test_grid_closed_fallback_retraction.py",
    "tests/test_grid_number_badge_click.py",
    "tests/test_grid_speech_routing.py",
    "tests/test_launch_generation.py",
    "tests/test_mutation_gate_error_summary.py",
    "tests/test_mutation_gate_scroll_error_summary.py",
    "tests/test_open_url_action.py",
    "tests/test_overlay_journeys.py",
    "tests/test_pattern_catalog_and_sign.py",
    "tests/test_pattern_catalog_doc_id_merge.py",
    "tests/test_pattern_catalog_hyphenated_first_word.py",
    "tests/test_pattern_catalog_nested_group.py",
    "tests/test_pattern_catalog_punctuation.py",
    "tests/test_pattern_catalog_reload.py",
    "tests/test_pattern_catalog_toml_error.py",
    "tests/test_pattern_catalog_trailing.py",
    "tests/test_pattern_catalog_user_merge.py",
    "tests/test_pattern_catalog_whole_utterance.py",
    "tests/test_pattern_customize_keeps_identity.py",
    "tests/test_pattern_customize_vs_duplicate.py",
    "tests/test_pattern_disabled_override_stays.py",
    "tests/test_pattern_duplicate_stays_independent.py",
    "tests/test_pattern_legacy_id_written_at_load.py",
    "tests/test_pattern_legacy_override_migration.py",
    "tests/test_pattern_manager_live_refresh.py",
    "tests/test_pattern_manager_trigger.py",
    "tests/test_pattern_manager_update.py",
    "tests/test_pattern_matcher_gaps.py",
    "tests/test_pattern_matcher_punctuation.py",
    "tests/test_pattern_override_trigger_move.py",
    "tests/test_pattern_phrases.py",
    "tests/test_pattern_tester_doc_id.py",
    "tests/test_pattern_tester_save_agreement.py",
    "tests/test_pattern_transform_boundary_body.py",
    "tests/test_pattern_unbuildable_override.py",
    "tests/test_pipeline_boundary.py",
    "tests/test_press_keys_arrow_names.py",
    "tests/test_property_speech_pipeline.py",
    "tests/test_provider_watchdog.py",
    "tests/test_ptt_mode_consistency.py",
    "tests/test_retraction_pipeline.py",
    "tests/test_retraction_policy.py",
    "tests/test_retraction_policy_mode1_integration.py",
    "tests/test_router_command_prefix.py",
    "tests/test_router_command_prefix_word_loss.py",
    "tests/test_router_gaps.py",
    "tests/test_router_greedy_helper.py",
    "tests/test_router_hotword_snapshot.py",
    "tests/test_router_hotword_variants.py",
    "tests/test_router_multiword_phrase.py",
    "tests/test_run_capture_action.py",
    "tests/test_screen_read_dictation_gate.py",
    "tests/test_scroll_action.py",
    "tests/test_scroll_patterns.py",
    "tests/test_speech_handler_user_patterns_path.py",
    "tests/test_speech_pipeline.py",
    "tests/test_speech_processor_bare_number.py",
    "tests/test_speech_processor_editor_multiword_routing.py",
    "tests/test_speech_processor_editor_restore_unconditional.py",
    "tests/test_speech_processor_editor_routing.py",
    "tests/test_speech_processor_gaps.py",
    "tests/test_speech_processor_greedy_timer.py",
    "tests/test_speech_processor_restore_surface_ownership.py",
    "tests/test_speech_processor_retraction.py",
    "tests/test_speech_processor_trace.py",
    "tests/test_timeout_sentinel.py",
    "tests/test_transcript_redaction.py",
    "tests/test_ui/test_prompt_detector.py",
    "tests/test_ui/test_utterance_clipboard_race.py",
    "tests/test_utterance_word_list.py",
    "tests/test_va_delete_cut_copy_range.py",
    "tests/test_va_format_range.py",
    "tests/test_va_navigation.py",
    "tests/test_va_search.py",
    "tests/test_va_select_range.py",
    "tests/test_va_type_dictate.py",
    "tests/test_va_window_app.py",
    "tests/test_voice_access_alias_completeness.py",
    "tests/test_voice_access_punctuation_router.py",
    "tests/test_websocket_manager.py",
    "tests/test_websocket_trace_id.py",
    "tests/test_whole_utterance_finalization.py",
)


# name -> directory, interpreter, test files, sys.path entries for the
# provenance probe (the ones the suite's conftest or tests insert).
SUITES = {
    "logic": {
        "dir": WH_DIR,
        "env": "BOOST_GATE_PYTHON_LOGIC",
        "tests": _LOGIC_TESTS,
        "path": (REPO_ROOT, WH_DIR),
    },
    "shared": {
        "dir": PROVIDERS / "shared",
        "env": "BOOST_GATE_PYTHON_SHARED",
        "tests": ("tests/test_ws_forwarder_capabilities.py",
                  "tests/test_capture_load_metrics.py",
                  "tests/test_capture_log_forwarding.py",
                  "tests/test_log_frame_survival.py",
                  "tests/test_plain_handler_liveness.py",
                  "tests/test_ready_backend_name.py",
                  "tests/test_transcript_redaction.py"),
        "path": (PROVIDERS / "shared",),
    },
    "distil": {
        "dir": PROVIDERS / "distil_medium_en",
        "env": "BOOST_GATE_PYTHON_DISTIL",
        "tests": ("tests/test_applies_hints.py",),
        "path": (PROVIDERS / "distil_medium_en",),
    },
    "google": {
        "dir": PROVIDERS / "google_stt_server",
        "env": "BOOST_GATE_PYTHON_GOOGLE",
        "tests": ("tests/test_applies_hints.py",),
        "path": (PROVIDERS / "google_stt_server",),
    },
    "parakeet": {
        "dir": PROVIDERS / "sherpa_offline_parakeet_stt_server",
        "env": "BOOST_GATE_PYTHON_PARAKEET",
        "tests": ("tests/test_applies_hints.py",
                  "tests/test_load_diagnostics_wiring.py"),
        "path": (PROVIDERS / "sherpa_offline_parakeet_stt_server",),
    },
    # Runs the real matcher and catalog after putting services/wheelhouse
    # on sys.path, so it runs after the Logic set for every mutation of
    # those two files.
    "release": {
        "dir": REPO_ROOT / "scripts" / "release",
        "env": "BOOST_GATE_PYTHON_RELEASE",
        "tests": ("tests/test_helpdoc_ci.py",),
        "path": (WH_DIR,),
    },
}


def _python(suite):
    override = os.environ.get(SUITES[suite]["env"])
    return Path(override) if override else _venv_python(SUITES[suite]["dir"])


# Target file -> (suite, importable module name for the provenance probe).
MATCHER = WH_DIR / "speech" / "pattern_matcher.py"
CATALOG = WH_DIR / "speech" / "pattern_catalog.py"
ROUTER = WH_DIR / "speech" / "router.py"
PROCESSOR = WH_DIR / "speech" / "speech_processor.py"
HANDLER = WH_DIR / "speech" / "speech_handler.py"
PARSER = WH_DIR / "speech" / "command_engine.py"
TESTER = WH_DIR / "speech" / "pattern_tester.py"
MAIN = WH_DIR / "main.py"
MANAGER = WH_DIR / "integrations" / "websocket_manager.py"
FORWARDER = PROVIDERS / "shared" / "shared_stt" / "ws_forwarder.py"
DISTIL = PROVIDERS / "distil_medium_en" / "main.py"
GOOGLE = PROVIDERS / "google_stt_server" / "main.py"
PARAKEET = PROVIDERS / "sherpa_offline_parakeet_stt_server" / "main.py"

TARGETS = {
    MATCHER: ("logic", "speech.pattern_matcher"),
    CATALOG: ("logic", "speech.pattern_catalog"),
    ROUTER: ("logic", "speech.router"),
    PROCESSOR: ("logic", "speech.speech_processor"),
    HANDLER: ("logic", "speech.speech_handler"),
    PARSER: ("logic", "speech.command_engine"),
    TESTER: ("logic", "speech.pattern_tester"),
    MAIN: ("logic", "main"),
    MANAGER: ("logic", "integrations.websocket_manager"),
    FORWARDER: ("shared", "shared_stt.ws_forwarder"),
    DISTIL: ("distil", "main"),
    GOOGLE: ("google", "main"),
    PARAKEET: ("parakeet", "main"),
}

# Suites that run AFTER the target's own suite for a mutation of that
# target, and so must also import the target from this tree.
FOLLOW_ON = {
    MATCHER: ("release",),
    CATALOG: ("release",),
}


def _run_suites(mutation):
    """The target's own suite first (the expected tests live there), then
    any follow-on suite."""
    return [mutation["suite"], *FOLLOW_ON.get(mutation["target"], ())]


def _probes():
    """(suite, target, module) for every import the provenance check
    makes."""
    probes = [(suite, target, module)
              for target, (suite, module) in TARGETS.items()]
    for target, extra in FOLLOW_ON.items():
        probes.extend((suite, target, TARGETS[target][1]) for suite in extra)
    return sorted(probes, key=lambda p: (p[0], str(p[1])))

# The whole Logic set runs in about 8 minutes clean (3877 passed, 2
# xfailed, 483 s measured 2026-09-21), which a survivor pays in full.
# This limit only turns a hang into a reported error rather than a lost
# run.
RUN_TIMEOUT_S = 1500
TIMEOUT_BANNER = "+++ Timeout +++"
SUMMARY_HEADER = "short test summary info"
ASSERTION_REASONS = ("assert", "AssertionError", "Failed", "DID NOT RAISE")


def _t(cls, name=None):
    return f"{cls}::{name}" if name else cls


# Logic test classes.
_CAT = "TestCatalogDerivesTheFlag"
_MC = "TestMatchComplete"
_MS = "TestMatchSinglePattern"
_CC = "TestCanContinue"
_RT = "TestRouter"
_PU = "TestPush"
_WS = "TestWebSocketManager"
_B1 = "TestB1EngineDoesNotApplyHints"
_B2 = "TestB2EngineAppliesHints"
_B3 = "TestB3ValueUnknown"
_B4 = "TestB4ValueFollowsTheEngine"
_TR = "TestUserMadeTrailingPattern"
_RP = "TestUserMadeReplacementInARemainder"
_HR = "TestHintEngineRefusal"
_HH = "TestHintEngineHandlers"


def _m(name, suite, target, old, new, expect):
    return {"name": name, "suite": suite, "target": target,
            "old": old, "new": new, "expect": expect}


MUTATIONS = [
    # ------------------------------------------------------------------
    # 1. The refusal condition
    # ------------------------------------------------------------------
    _m("refusal-never-refuses", "logic", MATCHER,
       "    return (\n        hint_engine is False\n",
       "    return False and (\n        hint_engine is False\n",
       [_t(_MC, "test_refused_when_the_engine_does_not_apply_hints"),
        _t(_B1, "test_boost_is_dictation"),
        _t(_B4, "test_false_then_true"),
        _t(_B4, "test_true_then_false")]),
    _m("refusal-ignores-the-flag", "logic", MATCHER,
       '        and bool(data.get("requires_hint_engine", False))\n',
       "        and True\n",
       [_t(_MC, "test_other_patterns_are_not_refused")]),
    _m("match-complete-place", "logic", MATCHER,
       "                if _refused_for_hint_engine(data, hint_engine):\n"
       "                    continue\n",
       "                if _refused_for_hint_engine(data, None):\n"
       "                    continue\n",
       [_t(_MC, "test_refused_when_the_engine_does_not_apply_hints"),
        _t(_MC, "test_routing_entry_points_forward_the_value")]),
    _m("match-single-pattern-place", "logic", MATCHER,
       "        if _refused_for_hint_engine(data, hint_engine):\n"
       "            return None\n",
       "        if _refused_for_hint_engine(data, None):\n"
       "            return None\n",
       [_t(_MS, "test_refused_when_the_engine_does_not_apply_hints"),
        _t(_PU, "test_the_execution_walk_refuses_when_false")]),
    _m("can-continue-place", "logic", MATCHER,
       "            # serve (wh-boost-engine-qualification).\n"
       "            if _refused_for_hint_engine(data, hint_engine):\n",
       "            # serve (wh-boost-engine-qualification).\n"
       "            if _refused_for_hint_engine(data, None):\n",
       [_t(_CC, "test_refused_prefix_cannot_continue"),
        _t(_RT, "test_router_matches_use_the_pushed_value")]),
    _m("router-greedy-prefix-place", "logic", ROUTER,
       "            if _refused_for_hint_engine(data, self.hint_engine):\n"
       "                continue\n",
       "            if _refused_for_hint_engine(data, None):\n"
       "                continue\n",
       [_t(_RT, "test_greedy_prefix_refused_when_false")]),
    _m("trailing-usable-place", "logic", PROCESSOR,
       "        if _refused_for_hint_engine(entry, self.hint_engine):\n"
       "            return None\n",
       "        if _refused_for_hint_engine(entry, None):\n"
       "            return None\n",
       # The hold-to-fire recheck backs this place up, so the word still
       # ends as dictation; what differs is that it is held back as a
       # command candidate first. Only the hold state shows it.
       [_t(_TR, "test_false_is_not_held_as_a_candidate")]),
    _m("hold-to-fire-recheck-place", "logic", PROCESSOR,
       "        if _refused_for_hint_engine(entry, self.hint_engine):\n"
       "            # wh-boost-engine-qualification: the engine reported",
       "        if _refused_for_hint_engine(entry, None):\n"
       "            # wh-boost-engine-qualification: the engine reported",
       [_t(_TR, "test_false_between_hold_and_fire_refuses")]),
    _m("earliest-replacement-place", "logic", PROCESSOR,
       "            if _refused_for_hint_engine(pattern_data, self.hint_engine):\n",
       "            if _refused_for_hint_engine(pattern_data, None):\n",
       [_t(_RP, "test_the_finder_skips_it_when_false")]),
    _m("catalog-index-copy-unflagged", "logic", CATALOG,
       '                        data_dict["requires_hint_engine"] = True\n',
       '                        data_dict["requires_hint_engine"] = False\n',
       [_t(_CAT, "test_boost_needs_a_hint_engine_in_the_first_word_index"),
        _t(_CAT, "test_a_user_made_pattern_with_the_hint_action_does")]),
    _m("catalog-list-copy-unflagged", "logic", CATALOG,
       "                        'requires_hint_engine': requires_hint_engine,\n",
       "                        'requires_hint_engine': False,\n",
       [_t(_CAT, "test_boost_needs_a_hint_engine_in_the_built_list"),
        _t(_CAT, "test_the_shipped_boost_entry_carries_the_flag"),
        _t(_PU, "test_the_execution_walk_refuses_when_false")]),
    _m("catalog-trailing-entry-unflagged", "logic", CATALOG,
       '            "requires_hint_engine": _actions_need_hint_engine(actions_list),\n',
       '            "requires_hint_engine": False,\n',
       [_t(_TR, "test_alone_refused_when_false"),
        _t(_TR, "test_after_words_refused_when_false"),
        _t(_TR, "test_false_between_hold_and_fire_refuses")]),
    _m("catalog-derivation-needs-every-step", "logic", CATALOG,
       "    return any(\n        isinstance(step, dict)",
       "    return all(\n        isinstance(step, dict)",
       [_t(_CAT, "test_boost_needs_a_hint_engine_in_the_built_list"),
        _t(_CAT, "test_boost_needs_a_hint_engine_in_the_first_word_index"),
        _t(_CAT, "test_the_shipped_boost_entry_carries_the_flag")]),
    _m("catalog-derivation-wrong-action-name", "logic", CATALOG,
       'HINT_ACTION_NAME = "add_hint_to_stt"\n',
       'HINT_ACTION_NAME = "add_hint"\n',
       [_t(_CAT, "test_boost_needs_a_hint_engine_in_the_built_list"),
        _t(_CAT, "test_the_shipped_boost_entry_carries_the_flag")]),

    # ------------------------------------------------------------------
    # 2. The unknown value
    # ------------------------------------------------------------------
    _m("unknown-value-refuses", "logic", MATCHER,
       "        hint_engine is False\n        and bool(data)\n",
       "        not hint_engine\n        and bool(data)\n",
       [_t(_MC, "test_matches_when_the_engine_applies_or_is_unknown"),
        _t(_MC, "test_default_is_unknown"),
        _t(_B3, "test_boost_runs_with_no_frame_yet"),
        _t(_B4, "test_the_boundary_alone_restores_the_command")]),
    _m("boundary-resets-to-false", "logic", MANAGER,
       "        self._set_provider_applies_hints(None)\n",
       "        self._set_provider_applies_hints(False)\n",
       [_t(_WS, "test_a_new_active_client_resets_to_unknown"),
        _t(_WS, "test_the_last_client_leaving_resets_to_unknown"),
        _t(_B4, "test_the_boundary_alone_restores_the_command")]),
    _m("boundary-keeps-the-old-value", "logic", MANAGER,
       "        self._set_provider_applies_hints(None)\n",
       "        pass\n",
       [_t(_WS, "test_a_new_active_client_resets_to_unknown"),
        _t(_WS, "test_the_last_client_leaving_resets_to_unknown"),
        _t(_B4, "test_the_boundary_alone_restores_the_command")]),
    _m("absent-key-reads-as-false", "logic", MANAGER,
       '        if "applies_hints" in data:\n',
       "        if True:\n",
       [_t(_WS, "test_frame_without_the_key_keeps_unknown"),
        _t(_B3, "test_boost_runs_after_a_frame_without_the_key")]),
    _m("stale-client-declaration-counts", "logic", MANAGER,
       '                getattr(websocket, "remote_address", None),\n'
       "            )\n"
       "            return\n"
       "        declared_emits_eos",
       '                getattr(websocket, "remote_address", None),\n'
       "            )\n"
       "        declared_emits_eos",
       [_t(_WS, "test_stale_client_declaration_is_ignored")]),

    # ------------------------------------------------------------------
    # 3. The engine switch: the value pushed to every layer
    # ------------------------------------------------------------------
    _m("declared-value-ignored", "logic", MANAGER,
       '            declared_applies_hints = bool(data.get("applies_hints"))\n',
       "            declared_applies_hints = True\n",
       [_t(_WS, "test_stores_and_pushes_the_declared_value"),
        _t(_B4, "test_false_then_true"),
        _t(_B4, "test_true_then_false")]),
    _m("manager-does-not-store", "logic", MANAGER,
       "        self.provider_applies_hints = value\n",
       "        pass\n",
       [_t(_WS, "test_stores_and_pushes_the_declared_value"),
        _t(_WS, "test_no_speech_handler_is_survivable")]),
    _m("manager-does-not-push", "logic", MANAGER,
       "        if apply is not None:\n            apply(value)\n",
       "        if apply is not None:\n            pass\n",
       [_t(_WS, "test_stores_and_pushes_the_declared_value"),
        _t(_B4, "test_false_then_true"),
        _t(_B4, "test_true_then_false")]),
    _m("handler-does-not-forward", "logic", HANDLER,
       "            self.speech_processor.apply_hint_engine(value)\n",
       "            pass\n",
       [_t(_PU, "test_handler_forwards_to_the_processor")]),
    _m("handler-does-not-keep", "logic", HANDLER,
       "        self.hint_engine = value\n"
       "        if self.speech_processor is not None:\n",
       "        if self.speech_processor is not None:\n",
       [_t(_PU, "test_handler_keeps_the_value_before_the_processor_exists")]),
    _m("new-processor-not-given-the-kept-value", "logic", HANDLER,
       "        self.speech_processor.apply_hint_engine(self.hint_engine)\n",
       "        pass\n",
       [_t(_PU, "test_a_new_processor_receives_the_kept_value")]),
    _m("processor-keeps-no-value", "logic", PROCESSOR,
       "        self.hint_engine = value\n"
       "        self.router.hint_engine = value\n",
       "        self.router.hint_engine = value\n",
       [_t(_PU, "test_processor_pushes_to_its_router"),
        _t(_TR, "test_after_words_refused_when_false"),
        _t(_RP, "test_the_finder_skips_it_when_false")]),
    _m("processor-skips-the-router", "logic", PROCESSOR,
       "        self.router.hint_engine = value\n"
       "        self.text_parser.hint_engine = value\n",
       "        self.text_parser.hint_engine = value\n",
       [_t(_PU, "test_processor_pushes_to_its_router")]),
    _m("processor-skips-the-parser", "logic", PROCESSOR,
       "        self.router.hint_engine = value\n"
       "        self.text_parser.hint_engine = value\n",
       "        self.router.hint_engine = value\n",
       [_t(_PU, "test_processor_pushes_to_its_router")]),
    _m("parser-does-not-forward", "logic", PARSER,
       "                hint_engine=self.hint_engine,\n",
       "                hint_engine=None,\n",
       [_t(_PU, "test_the_execution_walk_refuses_when_false")]),
    _m("matcher-routing-does-not-forward", "logic", MATCHER,
       "            first_word=first_word,\n"
       "            hint_engine=hint_engine,\n",
       "            first_word=first_word,\n"
       "            hint_engine=None,\n",
       [_t(_MC, "test_routing_entry_points_forward_the_value")]),
    _m("matcher-is-complete-does-not-forward", "logic", MATCHER,
       "        result = self.match_for_routing(\n"
       "            buffer, pattern_type, hotword_active, hint_engine=hint_engine\n",
       "        result = self.match_for_routing(\n"
       "            buffer, pattern_type, hotword_active, hint_engine=None\n",
       [_t(_MC, "test_routing_entry_points_forward_the_value")]),
    _m("matcher-cannot-match-does-not-forward", "logic", MATCHER,
       "        return not self.can_continue(\n"
       "            buffer, pattern_type, hotword_active, hint_engine=hint_engine\n",
       "        return not self.can_continue(\n"
       "            buffer, pattern_type, hotword_active, hint_engine=None\n",
       [_t(_CC, "test_refused_prefix_cannot_continue")]),
    _m("router-greedy-fullmatch-does-not-forward", "logic", ROUTER,
       "                list(buffer), ptype, hotword_active, hint_engine=self.hint_engine\n",
       "                list(buffer), ptype, hotword_active, hint_engine=None\n",
       [_t(_RT, "test_greedy_timer_refused_for_a_whole_greedy_match_when_false")]),
    _m("router-decide-does-not-forward", "logic", ROUTER,
       "            new_buffer, target_type, hotword_active, hint_engine=self.hint_engine\n",
       "            new_buffer, target_type, hotword_active, hint_engine=None\n",
       # The execution walk refuses too and _execute_command then
       # dictates, so the typed text is the same; the router's own
       # decision is what differs.
       [_t(_RT, "test_a_completed_hint_command_is_not_executed_when_false")]),
    _m("router-decide-timeout-does-not-forward", "logic", ROUTER,
       '                buffer, "command", hotword_active, hint_engine=self.hint_engine\n'
       "            )\n"
       "            if allow_commands\n",
       '                buffer, "command", hotword_active, hint_engine=None\n'
       "            )\n"
       "            if allow_commands\n",
       [_t(_RT, "test_utterance_end_finalization_dictates_when_false")]),
    _m("router-is-complete-does-not-forward", "logic", ROUTER,
       "        return self.matcher.is_pattern_complete(\n"
       "            buffer, target_type, hotword_active, hint_engine=self.hint_engine\n",
       "        return self.matcher.is_pattern_complete(\n"
       "            buffer, target_type, hotword_active, hint_engine=None\n",
       [_t(_RT, "test_router_matches_use_the_pushed_value")]),
    _m("router-cannot-match-does-not-forward", "logic", ROUTER,
       "        return self.matcher.cannot_match(\n"
       "            buffer, target_type, hotword_active, hint_engine=self.hint_engine\n",
       "        return self.matcher.cannot_match(\n"
       "            buffer, target_type, hotword_active, hint_engine=None\n",
       [_t(_RT, "test_router_matches_use_the_pushed_value")]),

    # ------------------------------------------------------------------
    # 4. The value each provider reports
    # ------------------------------------------------------------------
    _m("forwarder-sends-an-unset-value", "shared", FORWARDER,
       "                    if self.applies_hints is not None:\n",
       "                    if True:\n",
       ["test_applies_hints_defaults_to_not_declared",
        "test_applies_hints_none_is_not_declared"]),
    _m("forwarder-drops-a-false-value", "shared", FORWARDER,
       "                    if self.applies_hints is not None:\n",
       "                    if self.applies_hints:\n",
       ["test_applies_hints_false_is_declared"]),
    _m("forwarder-sends-true-always", "shared", FORWARDER,
       "                            self.applies_hints\n"
       "                        )\n",
       "                            True\n"
       "                        )\n",
       ["test_applies_hints_false_is_declared"]),
    _m("forwarder-defaults-to-false", "shared", FORWARDER,
       "        self.applies_hints: bool | None = None\n",
       "        self.applies_hints: bool | None = False\n",
       ["test_applies_hints_defaults_to_not_declared"]),
    _m("google-reports-false", "google", GOOGLE,
       "            forwarder.applies_hints = True\n",
       "            forwarder.applies_hints = False\n",
       ["test_main_declares_hints_before_the_forwarder_starts"]),
    _m("google-reports-after-start", "google", GOOGLE,
       "            forwarder.applies_hints = True\n"
       "            forwarder.start()\n",
       "            forwarder.start()\n"
       "            forwarder.applies_hints = True\n",
       ["test_main_declares_hints_before_the_forwarder_starts"]),
    _m("distil-reports-true-always", "distil", DISTIL,
       "        self.forwarder.applies_hints = bool(hotwords_enabled)\n",
       "        self.forwarder.applies_hints = True\n",
       ["test_declares_its_startup_hotwords_flag",
        "test_default_construction_declares_no_hints"]),
    _m("distil-reads-the-hint-string", "distil", DISTIL,
       "        self.forwarder.applies_hints = bool(hotwords_enabled)\n",
       "        self.forwarder.applies_hints = bool(hotwords)\n",
       ["test_enabled_with_no_hint_yet_still_declares_hints"]),
    _m("parakeet-ignores-the-status", "parakeet", PARAKEET,
       '    if getattr(status, "requested", False) is True:\n',
       "    if False:\n",
       ["test_enabled_and_rejected_declares_no_hints",
        "test_the_decision_function"]),
    _m("parakeet-requested-means-active", "parakeet", PARAKEET,
       '        return getattr(status, "active", False) is True\n',
       "        return True\n",
       ["test_enabled_and_rejected_declares_no_hints",
        "test_the_decision_function"]),
    _m("parakeet-trusts-a-truthy-requested", "parakeet", PARAKEET,
       '    if getattr(status, "requested", False) is True:\n',
       '    if getattr(status, "requested", False):\n',
       ["test_a_mock_status_cannot_claim_boosting"]),
    _m("parakeet-not-requested-is-false", "parakeet", PARAKEET,
       "    return bool(hotwords_enabled)\n",
       "    return False\n",
       ["test_enabled_with_no_hint_yet_declares_hints",
        "test_the_decision_function"]),
    _m("parakeet-not-requested-is-true", "parakeet", PARAKEET,
       "    return bool(hotwords_enabled)\n",
       "    return True\n",
       ["test_disabled_declares_no_hints",
        "test_the_decision_function"]),
    _m("parakeet-wired-without-the-status", "parakeet", PARAKEET,
       '            getattr(self.engine, "hotwords_status", None),\n'
       "            hotwords_enabled,\n",
       "            None,\n"
       "            hotwords_enabled,\n",
       ["test_enabled_and_rejected_declares_no_hints"]),
    _m("parakeet-wired-without-the-flag", "parakeet", PARAKEET,
       '            getattr(self.engine, "hotwords_status", None),\n'
       "            hotwords_enabled,\n",
       '            getattr(self.engine, "hotwords_status", None),\n'
       "            False,\n",
       ["test_enabled_with_no_hint_yet_declares_hints"]),

    # ------------------------------------------------------------------
    # 5. The try-it box refuses as the runtime does (.1.1)
    # ------------------------------------------------------------------
    _m("tester-refusal-removed", "logic", TESTER,
       "    if _refused_for_hint_engine(entry, hint_engine):\n"
       "        return None\n",
       "",
       [_t(_HR, "test_phrase_hint_pattern_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_phrase_refused_hint_pattern_lets_a_later_pattern_answer"),
        _t(_HR, "test_draft_with_hint_action_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_draft_not_shadowed_by_refused_hint_pattern"),
        _t(_HR, "test_shadowed_hint_draft_reports_it_would_not_match")]),
    _m("tester-refusal-fed-none", "logic", TESTER,
       "    if _refused_for_hint_engine(entry, hint_engine):\n"
       "        return None\n",
       "    if _refused_for_hint_engine(entry, None):\n"
       "        return None\n",
       [_t(_HR, "test_phrase_hint_pattern_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_phrase_refused_hint_pattern_lets_a_later_pattern_answer"),
        _t(_HR, "test_draft_with_hint_action_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_draft_not_shadowed_by_refused_hint_pattern"),
        _t(_HR, "test_shadowed_hint_draft_reports_it_would_not_match")]),
    _m("tester-phrase-walk-does-not-forward", "logic", TESTER,
       "    for entry in patterns:\n"
       "        try:\n"
       "            result = _match_entry(text, entry, matcher, hint_engine)\n",
       "    for entry in patterns:\n"
       "        try:\n"
       "            result = _match_entry(text, entry, matcher, None)\n",
       [_t(_HR, "test_phrase_hint_pattern_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_phrase_refused_hint_pattern_lets_a_later_pattern_answer"),
        _t(_HH, "test_phrase_handler_uses_parser_hint_engine")]),
    _m("tester-draft-walk-does-not-forward", "logic", TESTER,
       "    for entry in simulated:\n"
       "        try:\n"
       "            result = _match_entry(text, entry, matcher, hint_engine)\n",
       "    for entry in simulated:\n"
       "        try:\n"
       "            result = _match_entry(text, entry, matcher, None)\n",
       [_t(_HR, "test_draft_with_hint_action_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_draft_not_shadowed_by_refused_hint_pattern"),
        _t(_HH, "test_draft_handler_uses_parser_hint_engine")]),
    _m("tester-draft-recheck-does-not-forward", "logic", TESTER,
       "                    _match_entry(text, draft_entry, matcher, hint_engine)\n",
       "                    _match_entry(text, draft_entry, matcher, None)\n",
       [_t(_HR, "test_shadowed_hint_draft_reports_it_would_not_match")]),
    _m("tester-draft-flag-from-no-actions", "logic", TESTER,
       '        "requires_hint_engine": _actions_need_hint_engine(actions),\n',
       '        "requires_hint_engine": _actions_need_hint_engine([]),\n',
       [_t(_HR, "test_draft_with_hint_action_refused_when_engine_does_not_apply_hints"),
        _t(_HR, "test_shadowed_hint_draft_reports_it_would_not_match"),
        _t(_HH, "test_draft_handler_uses_parser_hint_engine")]),
    _m("main-phrase-handler-drops-the-value", "logic", MAIN,
       '                    data["text"], parser.patterns, parser.matcher,\n'
       "                    hint_engine=parser.hint_engine,\n",
       '                    data["text"], parser.patterns, parser.matcher,\n',
       [_t(_HH, "test_phrase_handler_uses_parser_hint_engine")]),
    _m("main-draft-handler-drops-the-value", "logic", MAIN,
       "                    catalog=parser.pattern_catalog,\n"
       "                    hint_engine=parser.hint_engine,\n",
       "                    catalog=parser.pattern_catalog,\n",
       [_t(_HH, "test_draft_handler_uses_parser_hint_engine")]),
]


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------


def _endings(originals):
    """Each target's own line ending. The patterns are translated to it;
    the file is never normalised."""
    return {t: ("\r\n" if b"\r\n" in raw else "\n")
            for t, raw in originals.items()}


def _translate(text, ending):
    return text if ending == "\n" else text.replace("\n", ending)


def _apply(mutation, sources, endings):
    """Return (mutated source text, error or None)."""
    target = mutation["target"]
    source = sources[target]
    old = _translate(mutation["old"], endings[target])
    new = _translate(mutation["new"], endings[target])
    count = source.count(old)
    if count == 0:
        return None, f"{mutation['name']}: pattern not found in {target.name}"
    if count > 1:
        return None, (f"{mutation['name']}: pattern ambiguous in "
                      f"{target.name}, matched {count} times, need 1")
    idx = source.index(old)
    line_start = source.rfind("\n", 0, idx) + 1
    before = source[line_start:idx]
    if before and not before.strip():
        return None, (f"{mutation['name']}: pattern matches {len(before)} "
                      "characters into a deeper indent, not at a line start")
    mutated = source.replace(old, new, 1)
    if mutated == source:
        return None, f"{mutation['name']}: mutation changes nothing"
    try:
        compile(mutated, str(target), "exec")
    except SyntaxError as exc:
        return None, f"{mutation['name']}: mutant does not compile: {exc}"
    return mutated, None


def _check(mutations, sources, endings):
    stale, broken = [], []
    for mutation in mutations:
        _, error = _apply(mutation, sources, endings)
        if error is None:
            print(f"ok {mutation['name']}")
        elif "does not compile" in error:
            broken.append(error)
        else:
            stale.append(error)
    for line in stale + broken:
        print(f"ERROR {line}")
    print(f"checked {len(mutations)} patterns of {len(MUTATIONS)}, "
          f"{len(stale)} stale, {len(broken)} that do not compile")
    return 1 if stale or broken else 0


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "1000"
    return env


def _pytest(suite, args, stop_first=False):
    spec = SUITES[suite]
    first = ["-x"] if stop_first else []
    return subprocess.run(
        [str(_python(suite)), "-m", "pytest", *first, *args,
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=spec["dir"], env=_env(), capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _provenance(suites):
    """Import each target under its suite's interpreter; every module file
    must be the target in this tree. Return a list of problems."""
    problems = []
    for suite, target, module in _probes():
        if suite not in suites:
            continue
        paths = [str(p) for p in SUITES[suite]["path"]]
        code = ("import sys, importlib; sys.path[:0] = " + repr(paths)
                + "; print(importlib.import_module(" + repr(module)
                + ").__file__)")
        run = subprocess.run(
            [str(_python(suite)), "-c", code], cwd=SUITES[suite]["dir"],
            env=_env(), capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
        found = run.stdout.strip().splitlines()[-1:] or [""]
        if run.returncode != 0 or Path(found[0]).resolve() != target.resolve():
            problems.append(
                f"{suite}: {module} imports from {found[0]!r}, not {target} "
                f"(exit {run.returncode}) {run.stderr[-400:]}")
        else:
            print(f"provenance {suite}: {module} -> {found[0]}")
    return problems


def _node_key(node_id):
    """``Class::method`` or ``function``, parameters cut."""
    return node_id.split("::", 1)[1].split("[")[0].strip()


def _collect(suite):
    run = _pytest(suite, [*SUITES[suite]["tests"], "--collect-only", "-q"])
    if run.returncode != 0:
        return set(), (f"cannot collect {suite}: {run.stdout[-800:]}"
                       f"{run.stderr[-800:]}")
    return {_node_key(line.strip())
            for line in run.stdout.splitlines() if "::" in line}, None


def _summary_section(output):
    lines = output.splitlines()
    start = None
    for index, line in enumerate(lines):
        if SUMMARY_HEADER in line and line.startswith("="):
            start = index + 1
    return None if start is None else lines[start:]


def _clear_pycache():
    """Remove every __pycache__ under the suites, .venv excluded; return
    (error or None, held interrupt)."""
    interrupt, leftovers = None, []
    for _pass in (1, 2):
        leftovers = []
        try:
            for spec in SUITES.values():
                for d in spec["dir"].rglob("__pycache__"):
                    if ".venv" in d.parts:
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    if d.exists():
                        leftovers.append(d)
        except KeyboardInterrupt as stop:
            interrupt = interrupt or stop
            continue
        if not leftovers:
            return None, interrupt
    return (f"could not remove __pycache__ (a MUTANT .pyc MAY REMAIN): "
            f"{', '.join(str(d) for d in leftovers)}"), interrupt


def _atomic_write(target, raw):
    """Write the whole bytes through a pid-named neighbour and os.replace,
    retrying a PermissionError a few times (a language server or the last
    pytest can hold the file for a moment on Windows)."""
    tmp = target.with_name(f"{target.name}.mutation-gate.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(raw)
        for attempt in range(10):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.5)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            print(f"ERROR could not remove {tmp}: {exc}")


def _restore(target, original, mutated):
    """Put the pre-run bytes back; return (error or None, held interrupt).
    Never overwrite bytes this run did not write."""
    interrupt = None
    for _attempt in (1, 2):
        try:
            current = target.read_bytes()
            if current == original:
                return None, interrupt
            if current != mutated:
                return (f"{target} changed while pytest ran; refusing to "
                        "overwrite the concurrent edit -- reconcile by hand"
                        ), interrupt
            _atomic_write(target, original)
            return None, interrupt
        except KeyboardInterrupt as stop:
            interrupt = interrupt or stop
            continue
        except OSError as exc:
            return (f"restore failed; the MUTANT REMAINS in {target}: {exc}"
                    ), interrupt
    return (f"interrupted during every restore attempt; the MUTANT MAY "
            f"REMAIN in {target}"), interrupt


def _final_mismatches(originals):
    problems = []
    for target, raw in sorted(originals.items()):
        try:
            if target.read_bytes() != raw:
                problems.append(f"{target} does not hold its pre-run bytes "
                                "at the end of the sweep; check it by hand")
        except OSError as exc:
            problems.append(f"cannot read {target} at the end: {exc}")
        leftover = target.with_name(
            f"{target.name}.mutation-gate.{os.getpid()}.tmp")
        if leftover.exists():
            problems.append(f"temporary file left behind: {leftover}")
    return problems


def _first_failure(summary):
    """Every FAILED or ERROR record of a -x run, as (kind, node, reason)."""
    records = []
    for line in summary:
        for kind in ("FAILED ", "ERROR "):
            if not line.startswith(kind):
                continue
            rest = line[len(kind):]
            node = rest.split(" ", 1)[0]
            reason = rest.split(" - ", 1)[1].strip() if " - " in rest else ""
            records.append((kind.strip(), node, reason))
            break
    return records


def _verdict(mutation, suite, result):
    """Read one -x run that did not pass. Return ("caught" | "error",
    message). A catch needs exit code 1, a short summary holding exactly
    one record, that record a FAILED test (never an ERROR), and its reason
    an assertion. Anything else -- a collection or import error (exit 2 or
    more), a fixture error, a crash that is not an assertion, the timeout
    banner -- is an error, never a catch."""
    name = mutation["name"]
    combined = result.stdout + result.stderr
    if TIMEOUT_BANNER in combined:
        return "error", f"{name}: suite-timeout-abort in {suite}, no verdict"
    if result.returncode != 1:
        return "error", (f"{name}: pytest exited {result.returncode} in "
                         f"{suite}, neither a pass nor a test failure: "
                         f"{combined[-600:]!r}")
    summary = _summary_section(combined)
    if summary is None:
        return "error", f"{name}: failure in {suite} but no short summary"
    records = _first_failure(summary)
    if len(records) != 1:
        return "error", (f"{name}: {suite} -x run reported {len(records)} "
                         f"records, expected exactly one: {records}")
    kind, node, reason = records[0]
    if kind != "FAILED":
        return "error", f"{name}: {node} was reported as {kind}: {reason!r}"
    if not reason:
        return "error", f"{name}: {node} carries no reason; catch unconfirmed"
    if not reason.startswith(ASSERTION_REASONS):
        return "error", (f"{name}: {node} failed on {reason!r}, which is not "
                         "an assertion")
    key = _node_key(node) if "::" in node else node
    if suite == mutation["suite"] and key in mutation["expect"]:
        return "caught", f"{name}: (expected) {node}"
    return "caught", f"{name}: (other) {suite} {node} - {reason[:160]}"


def main(argv):
    args = list(argv[1:])
    check_only = "--check" in args
    selected = [a for a in args if a != "--check"]

    names = [m["name"] for m in MUTATIONS]
    duplicate = sorted({n for n in names if names.count(n) > 1})
    if duplicate:
        print(f"ERROR duplicate mutation names: {duplicate}")
        return 1
    if selected:
        unknown = [n for n in selected if n not in names]
        if unknown:
            print(f"ERROR no such mutation: {unknown}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in selected]
    else:
        mutations = list(MUTATIONS)

    originals = {t: t.read_bytes() for t in TARGETS}
    endings = _endings(originals)
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    if check_only:
        return _check(mutations, sources, endings)

    suites = sorted({s for m in mutations for s in _run_suites(m)})
    for suite in suites:
        print(f"suite {suite}: {_python(suite)} in {SUITES[suite]['dir']}")
    problems = _provenance(suites)
    if problems:
        for p in problems:
            print(f"ERROR {p}")
        return 1

    errors = []
    for suite in suites:
        found, problem = _collect(suite)
        if problem:
            print(f"ERROR {problem}")
            return 1
        for m in mutations:
            if m["suite"] != suite:
                continue
            for test in m["expect"]:
                if test not in found:
                    errors.append(f"{m['name']}: no such test {test} "
                                  f"in suite {suite}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1
    print("name validation: every expected test exists")

    error, stop = _clear_pycache()
    if error:
        print(f"ERROR {error}")
    if stop is not None:
        raise stop
    if error:
        return 1
    for suite in suites:
        started = time.monotonic()
        base = _pytest(suite, list(SUITES[suite]["tests"]))
        print(f"baseline {suite}: exit {base.returncode} in "
              f"{time.monotonic() - started:.0f}s; "
              f"{(base.stdout.strip().splitlines() or [''])[-1]}")
        if base.returncode != 0:
            print(f"ERROR baseline not green for {suite}; refusing to start")
            print(base.stdout[-2000:])
            return 1
        print(f"baseline green: {suite}")
    print(f"running {len(mutations)} mutations")

    caught, survivors = [], []
    held = None
    try:
        for mutation in mutations:
            name, target = mutation["name"], mutation["target"]
            mutated, error = _apply(mutation, sources, endings)
            if mutated is None:
                errors.append(error)
                print(f"ERROR    {error}")
                continue
            raw = mutated.encode("utf-8")
            result = None
            ran_in = None
            wrote = False
            started = time.monotonic()
            try:
                wrote = True
                _atomic_write(target, raw)
                for suite in _run_suites(mutation):
                    ran_in = suite
                    result = _pytest(suite, list(SUITES[suite]["tests"]),
                                     stop_first=True)
                    if result.returncode != 0:
                        break
            except subprocess.TimeoutExpired:
                result = None
                errors.append(f"{name}: never terminated within "
                              f"{RUN_TIMEOUT_S}s")
                print(f"ERROR    {name}: never terminated")
            except OSError as exc:
                errors.append(f"{name}: {exc}")
                print(f"ERROR    {name}: {exc}")
            finally:
                if wrote:
                    problem, stop = _restore(target, originals[target], raw)
                    if problem:
                        errors.append(f"{name}: {problem}")
                        print(f"ERROR    {name}: {problem}")
                    held = held or stop
                cache_error, stop = _clear_pycache()
                if cache_error:
                    errors.append(f"{name}: {cache_error}")
                    print(f"ERROR    {name}: {cache_error}")
                held = held or stop
            if held is not None:
                raise held
            if result is None:
                continue
            took = f" [{time.monotonic() - started:.0f}s]"
            if result.returncode == 0:
                kind = "survived"
                message = (f"{name}: every test passed in "
                           f"{_run_suites(mutation)}; expected "
                           f"{mutation['expect']}")
            else:
                kind, message = _verdict(mutation, ran_in, result)
            message += took
            if kind == "caught":
                caught.append(name)
                print(f"caught   {message}")
            elif kind == "survived":
                survivors.append(message)
                print(f"SURVIVED {message}")
            else:
                errors.append(message)
                print(f"ERROR    {message}")
    finally:
        for problem in _final_mismatches(originals):
            errors.append(problem)
            print(f"ERROR {problem}")

    if len(mutations) == len(MUTATIONS):
        scope = f"all {len(MUTATIONS)} mutations, none skipped"
    else:
        ran = {m["name"] for m in mutations}
        scope = (f"{len(mutations)} of {len(MUTATIONS)} mutations, named on "
                 f"the command line. NOT RUN: "
                 f"{[n for n in names if n not in ran]}")
    print(f"\nscope: {scope}.\ncaught {len(caught)}, survivors "
          f"{len(survivors)}, errors {len(errors)}")
    for line in survivors + errors:
        print(f"  {line}")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
