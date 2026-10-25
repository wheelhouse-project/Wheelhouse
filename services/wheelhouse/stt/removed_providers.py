"""Speech engines that Wheelhouse no longer ships, and the move off them.

wh-distil-whisper-removal. A machine that updates from a release with
Distil-Whisper can name the removed engine in two ways:

* ``stt.last_provider`` still holds its name (the app updated alone, or the
  settings file was not rewritten). The provider folder may or may not still
  exist, so the name alone decides.
* The installer wrote ``stt.removed_provider``. The installer rewrites
  ``stt.last_provider`` to the engine it installed before the app starts, so
  on an installer update the app cannot see the old name there. The marker is
  the only record that the user's engine was removed.

``migrate_removed_provider`` runs first in ``ServiceManager.start_remote_stt``.
It changes the settings in memory only; the caller saves them and, after it
has decided which engine starts, shows the notice. The notice text names the
engine that actually started (see ``removed_provider_message``). Both triggers
are removed by the move, so the notice shows once. If
the save fails the triggers stay on disk and the notice shows again at the
next start (crewcut: no second record of "already told"; the cost is one
repeated notice, and only while the settings file cannot be written).

Kroko left no notice. A missing provider folder still falls back to the
default provider in ``start_remote_stt`` without a notice, for any engine
that is not listed here.
"""
import logging
from typing import Optional

from services.wheelhouse.config_service import DEFAULT_STT_PROVIDER

log = logging.getLogger(__name__)

# Provider name -> the name a person knows it by.
REMOVED_PROVIDERS = {"distil_medium_en": "Distil-Whisper"}
LAST_PROVIDER_KEY = "stt.last_provider"
MARKER_KEY = "stt.removed_provider"
# A *TITLE module string is what helpdoc_inventory_audit reads, and every
# such title needs an entry in knowledge/helpdoc/notice_descriptions.toml.
REMOVED_PROVIDER_NOTICE_TITLE = "Wheelhouse: Speech engine changed"
_REPLACEMENT_NAME = "Parakeet"

# What happened at the start that followed the move. Each one has its own text.
OUTCOME_REPLACEMENT_STARTED = "replacement_started"
OUTCOME_MODEL_MISSING = "model_missing"
OUTCOME_OTHER_ENGINE_STARTED = "other_engine_started"
OUTCOME_NO_ENGINE_STARTED = "no_engine_started"


def _removed_name(value) -> Optional[str]:
    """The display name when value names a removed provider; else None."""
    if isinstance(value, str):
        return REMOVED_PROVIDERS.get(value)
    return None


def migrate_removed_provider(config_service) -> Optional[str]:
    """Move the settings off a removed engine, in memory.

    Returns the removed engine's display name when this start must show the
    notice, otherwise None. The caller then starts DEFAULT_STT_PROVIDER, saves
    the settings, and shows the notice.
    """
    last_provider = config_service.get(LAST_PROVIDER_KEY, None)
    marker = config_service.get(MARKER_KEY, None)
    last_name = _removed_name(last_provider)
    marker_name = _removed_name(marker)
    # A marker beside a different, still-shipped engine is stale: the user
    # chose that engine after the installer ran. The notice would say Parakeet
    # is in use when it is not, so only the marker is cleared.
    marker_applies = marker_name is not None and last_provider in (None, DEFAULT_STT_PROVIDER)
    name = last_name or (marker_name if marker_applies else None)

    try:
        if last_name is not None:
            config_service.set(LAST_PROVIDER_KEY, DEFAULT_STT_PROVIDER)
        if marker_name is not None:
            config_service.unset(MARKER_KEY)
    except Exception as exc:
        # A user config can hold a plain value where the [stt] table belongs
        # (the same case start_remote_stt's fallback repair guards). The
        # engine still starts as the default; only the record is lost.
        log.warning("Could not record the move off %s in the settings: %s",
                    last_name or marker_name, exc)
    return name


def removed_provider_message(display_name: str, outcome: str,
                             other_engine: Optional[str] = None) -> str:
    """The notice text for one outcome of the start that followed the move.

    ``other_engine`` is the display name of the engine that started instead of
    the replacement; only OUTCOME_OTHER_ENGINE_STARTED uses it.
    """
    removed = f"{display_name} was removed from Wheelhouse."
    if outcome == OUTCOME_REPLACEMENT_STARTED:
        return f"{removed} {_REPLACEMENT_NAME} is now in use as your speech engine."
    if outcome == OUTCOME_MODEL_MISSING:
        return (f"{removed} {_REPLACEMENT_NAME} replaces it, but its speech "
                "model is not installed.")
    if outcome == OUTCOME_OTHER_ENGINE_STARTED:
        return (f"{removed} {_REPLACEMENT_NAME} could not start, so {other_engine} "
                "is in use as your speech engine.")
    return f"{removed} No speech engine could start."


def notify_removed_provider(display_name: str, outcome: str,
                            other_engine: Optional[str] = None) -> None:
    """Queue the one notice, without waiting for Windows toast delivery."""
    from services.wheelhouse.utils.logging_setup import get_notifier_worker
    # Production's qualified logging_setup constructs this bare notifier class;
    # its consumer rejects the distinct qualified NotifierPayload identity.
    from utils.notifier_worker import NotifierPayload

    message = removed_provider_message(display_name, outcome, other_engine)
    try:
        worker = get_notifier_worker()
        if worker is None or not worker.submit(NotifierPayload(
            title=REMOVED_PROVIDER_NOTICE_TITLE, message=message,
            levelname="INFO", trace_id="",
        )):
            log.warning("Could not queue the removed speech engine notice: %s", message)
    except Exception as exc:
        log.warning("Could not queue the removed speech engine notice: %s (%s)", message, exc)


async def save_removed_provider_switch(config_service, display_name: str) -> bool:
    """Write the settings after the move; a failure warns and never raises."""
    try:
        saved = await config_service.save()
    except Exception as exc:
        log.warning("Could not save the switch from %s to %s: %s", display_name,
                    _REPLACEMENT_NAME, exc)
        return False
    if not saved:
        log.warning("Could not save the switch from %s to %s; the notice will "
                    "show again at the next start.", display_name, _REPLACEMENT_NAME)
    return bool(saved)
