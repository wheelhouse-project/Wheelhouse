"""Assertion-only endpoint regression proof using the existing PTT gate adapter."""
from mutation_gate_ptt_mode_consistency import SERVICE, runner
import mutation_gate_ptt_mode_consistency as adapter
from mutation_gate_owned_guard import MutationRun
from types import SimpleNamespace
import sys


def mutation(name, old, new, test):
    return dict(name=name, service=SERVICE,
                test_file="tests/test_ptt_endpoint_identity.py::" + test,
                file=SERVICE / "plugins/system_volume_plugin.py",
                old=old, new=new, expect=[test])


DISTINCT = "test_distinct_communications_mute_cannot_enable_speech"
RECOVER = "test_default_device_change_is_re_resolved_on_the_next_press"
RETRY = "test_identity_read_failure_is_retried_on_the_next_press"
HEALTH = "test_health_reports_a_persisting_endpoint_refusal_and_the_next_success_clears_it"
NOTICE = "test_a_configured_endpoint_refusal_is_reported_to_the_user"
ONCE = "test_a_repeated_refusal_does_not_repeat_the_notice"
REARM = "test_a_successful_mute_re_arms_the_configured_endpoint_notice"
# The health test above reaches its success THROUGH a re-resolve, and
# _connect_audio_device clears _last_error itself, so that test cannot see the
# clearing on the mute's own return. This one reaches the success with no
# reconnect at all, which is the only input the mutated line still handles.
CLEARED = "test_a_success_without_a_reconnect_still_clears_the_recorded_error"
KEEP_ID = "test_a_failed_re_resolve_keeps_the_identity_for_the_next_press"
TRANSIENT = "test_an_unverifiable_endpoint_is_not_reported_as_a_configuration_error"
STALE_MUTE = "test_a_failed_re_resolve_is_not_reported_as_a_configured_device_mismatch"
INHERITED_ID = "test_a_replaced_interface_never_inherits_the_previous_identity"
RE_RESOLVE = (
    "                    previous_id = self._audio_device_id\n"
    "                    previous_interface = self._volume_interface\n"
    "                    try:\n"
    "                        await self._connect_audio_device()\n"
    "                    except Exception as reconnect_error:\n"
    '                        logger.warning("[PTT] Cannot re-resolve the audio endpoint: %s", reconnect_error)\n'
    "                    if self._audio_device_id is None:\n"
    "                        endpoint_confirmed = False\n"
    "                        if previous_id and self._volume_interface is previous_interface:\n"
    "                            self._audio_device_id = previous_id\n"
    "                    metered_id = await self._metered_endpoint_id()\n"
)
UNVERIFIED = ("                if metered_id is None or not self._audio_device_id "
              "or not endpoint_confirmed:\n")
WITHHOLD = '        if reason != "endpoint_mismatch":\n'
TRIGGER = "                if metered_id is None or metered_id != self._audio_device_id:\n"
MUTATIONS = [
    mutation("accept-different-endpoint",
             "                covers_metered_endpoint = metered_id == self._audio_device_id\n",
             "                covers_metered_endpoint = True\n", DISTINCT),
    mutation("meter-the-configured-role",
             "            return await asyncio.to_thread(lambda: AudioUtilities.GetSpeakers().GetId())\n",
             "            return await asyncio.to_thread(lambda: self._get_audio_device().GetId())\n", DISTINCT),
    mutation("accept-unknown-identity", UNVERIFIED,
             "                if False:\n",
             "test_unverifiable_connected_identity_refuses_override[None]"),
    mutation("bind-identity-to-wrong-interface",
             "                device_id = await asyncio.to_thread(device.GetId)\n",
             "                device_id = await asyncio.to_thread(lambda: AudioUtilities.GetSpeakers().GetId())\n", DISTINCT),
    mutation("reject-matching-endpoint",
             "                self._audio_device_id = device_id if isinstance(device_id, str) and device_id else None\n",
             "                self._audio_device_id = None\n",
             "test_matching_endpoint_keeps_default_ptt_behavior[default]"),
    # The comparison that decides whether to re-resolve. Asking the configured
    # role again, instead of using the identity bound to the cached interface,
    # makes a moved default look like a match: the reconnect is skipped and the
    # stale interface is muted and reported as covering the metered endpoint.
    mutation("trust-fresh-role-instead-of-cached-interface", TRIGGER,
             "                if metered_id is None or metered_id != await asyncio.to_thread(lambda: self._get_audio_device().GetId()):\n",
             RECOVER),
    mutation("report-mismatch-as-muted",
             '                    return False, "endpoint_mismatch"\n',
             '                    return True, "endpoint_mismatch"\n', DISTINCT),
    mutation("reuse-identity-after-reconnect",
             "        self._audio_device_id = None\n        try:\n            # Get audio device based on configuration\n",
             "        try:\n            # Get audio device based on configuration\n",
             "test_reconnect_cannot_reuse_an_old_endpoint_identity"),
    # wh-codex-merge-audit.6.1.6: a mismatch withholds the override, and the
    # configured device is still turned down for the hold.
    mutation("drop-the-mute-on-mismatch",
             "                if not covers_metered_endpoint:\n"
             '                    logger.warning("PTT override withheld -- volume device differs from monitored speakers")\n',
             "                if not covers_metered_endpoint:\n"
             '                    return False, "endpoint_mismatch"\n',
             DISTINCT),
    # wh-codex-merge-audit.6.1.5: a refusal re-resolves the endpoint once, and
    # no state makes it last beyond the press. The same mutation is pinned by
    # two tests, because a moved default and an unreadable identity are the two
    # ways in and each has its own recovery test.
    mutation("never-re-resolve-a-moved-default", RE_RESOLVE,
             "                    pass\n", RECOVER),
    mutation("never-retry-an-unreadable-identity", RE_RESOLVE,
             "                    pass\n", RETRY),
    mutation("make-the-refusal-permanent-again", TRIGGER,
             "                if metered_id is None or (metered_id != self._audio_device_id and self._last_error is None):\n",
             HEALTH),
    mutation("hide-the-refusal-from-health",
             '                    self._last_error = "Push-to-talk audio endpoint differs from the monitored speakers"\n',
             "", HEALTH),
    mutation("keep-the-recorded-error-after-a-success",
             '                self._last_error = None\n                return True, "muted"\n',
             '                return True, "muted"\n', CLEARED),
    # wh-codex-merge-audit.6.2.1: a re-resolve that cannot read a new identity
    # leaves the previous one on the interface it did not replace, so the next
    # press still has it.
    mutation("re-clear-the-id-on-a-failed-re-resolve",
             "                            self._audio_device_id = previous_id\n",
             "                            self._audio_device_id = None\n", KEEP_ID),
    # wh-codex-merge-audit.6.2.3: the identity goes back only onto the object
    # it names, and a re-resolve that produced no identity refuses the press
    # instead of calling it a mismatch the user is told to correct.
    mutation("restore-the-identity-onto-any-interface",
             "                        if previous_id and self._volume_interface is previous_interface:\n",
             "                        if previous_id:\n", INHERITED_ID),
    mutation("report-a-failed-re-resolve-as-a-mismatch", UNVERIFIED,
             "                if metered_id is None or not self._audio_device_id:\n", STALE_MUTE),
    # wh-codex-merge-audit.6.1.4: the refusal reaches the user, once per reason.
    mutation("never-publish-the-configured-device-notice", WITHHOLD,
             "        if True:\n", NOTICE),
    # wh-codex-merge-audit.6.2.2: an unreadable render endpoint is a device
    # condition, so the configuration notice is withheld for it.
    mutation("publish-the-notice-on-an-unverifiable-endpoint", WITHHOLD,
             '        if reason not in ("endpoint_unverified", "endpoint_mismatch"):\n',
             TRANSIENT),
    mutation("publish-the-notice-on-every-press",
             "        if reason in self._ptt_endpoint_errors_reported or not self._event_bus:\n",
             "        if not self._event_bus:\n", ONCE),
    mutation("never-re-arm-the-notice-after-a-success",
             "        if muted:\n            self._ptt_endpoint_errors_reported.clear()\n            return\n",
             "        if muted:\n            return\n", REARM),
]


def main():
    if "--check" in sys.argv:
        return runner.run(MUTATIONS)
    guard = MutationRun(adapter.ROOT, [item["file"] for item in MUTATIONS])
    subprocess_api = adapter.subprocess
    try:
        adapter.subprocess = SimpleNamespace(run=guard.subprocess_run)
        with guard.protect(runner, ("_restore", "_clear_pycache")):
            return runner.run(MUTATIONS)
    finally:
        adapter.subprocess = subprocess_api


if __name__ == "__main__":
    raise SystemExit(main())
