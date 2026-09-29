"""Speech-to-text support that runs inside the Logic process.

What is left here does not transcribe anything. WheelHouse takes audio
through a provider process it launches, and this package holds the
piece of that arrangement that lives on the WheelHouse side:

    remote_stt_launcher.py  starts, stops and discovers those processes

vad.py, a second copy of the Silero voice-activity model over pysilero-vad,
was deleted with that package (wh-vad-releases-gil). Its one user,
score_vad_leak.py, moved to services/stt_providers/shared/tools/ and uses
the shipped detector.

wh-in-process-capture-removal deleted the rest. Until then the package
also built its own capture and ran a provider in this process
(audio_capture.py, stt_manager.py, base.py, providers/google_provider.py),
which `stt.mode = "in_process"` selected. Nothing selects it now: every
value of that key runs remote, and config_service.warn_if_stt_mode_unsupported
says so once at WARNING.

This module deliberately re-exports nothing. Import what you need from
stt.remote_stt_launcher directly.
"""
