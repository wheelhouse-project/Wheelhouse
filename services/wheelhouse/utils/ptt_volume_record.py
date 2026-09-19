"""The speaker level that a push-to-talk hold lowered, kept on disk.

A push-to-talk hold lowers the speaker level in the Logic process
(plugins/system_volume_plugin.py, _mute_for_ptt) and keeps the level to put
back only in that process's memory. When the Logic process was lost during a
hold, that level was lost with it, and the speakers stayed at the lowered
level with nothing able to put them back
(wh-ptt-mute-orphaned-on-process-loss).

This module keeps a copy of the hold on disk:

* The plugin writes the record before it lowers the level, writes it again
  when a wheel turn during the hold changes the level to put back, and
  deletes it after it has put the level back.
* The launcher calls restore_orphaned_volume() at the two points where no
  Logic process can end a hold: after every child process has exited, and at
  launcher start before any child process starts. A record on disk at those
  points belongs to a hold that nothing else will end. The launcher runs the
  restore in a thread, waits for it at most SHUTDOWN_GRACE_PERIOD_S, and then
  sets the cancel flag, so a restore that goes on after that time writes no
  level and deletes no record.

The restore writes the recorded level only when the endpoint is still at the
lowered level, so a level set after the loss stays as it is.
"""
import ctypes
import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

from services.wheelhouse.utils import system

logger = logging.getLogger(__name__)

RECORD_FILE_NAME = "ptt_volume_record.json"

# A record older than this is deleted without a write. 24 hours is the limit
# in the acceptance criteria of wh-ptt-mute-orphaned-on-process-loss. After
# that long, the level on the endpoint is more probably a level the user set
# than the level the lost hold left. A record dated in the future (the clock
# was set back) counts as not too old; the level check still guards the write.
MAX_RECORD_AGE_S = 24 * 60 * 60

# The restore counts the endpoint as still at the lowered level when its
# level is within this many dB of the lowered level in the record.
#
# The value is 1.0 dB. A level that WheelHouse wrote normally reads back
# exactly. Microsoft Learn, IAudioEndpointVolume::GetVolumeRange, Remarks: a
# level between two steps is applied at the closest step, but
# GetMasterVolumeLevel returns the level that was requested, not the step.
# Only the float32 storage changes that value, by about 1e-6 dB. The margin
# is for an endpoint that reports its own step instead, for example after a
# device restart or an audio service restart, because devices round the level
# to their own step size. The closest step is at most half a step away: 0.75
# dB for the 1.5 dB step measured on 2026-09-17 on the only active render
# endpoint of the development machine. 1.0 dB covers steps up to 2.0 dB.
#
# A level that the user set within 1.0 dB of the lowered level also counts as
# the lowered level, and gets the pre-hold level back. The acceptance criteria
# record this limit for a level set to exactly the lowered level.
#
# crewcut: no device was seen to report its own step, and a step larger than
# 2.0 dB is not covered. To remove the limit, store the endpoint's
# GetVolumeRange step in the record and use half of that step as the
# tolerance.
LEVEL_MATCH_TOLERANCE_DB = 1.0

# The outcomes of restore_orphaned_volume(). Each comment says what happens to
# the record.
OUTCOME_NO_RECORD = "no_record"  # No record on disk. COM is not started.
OUTCOME_UNREADABLE = "unreadable"  # The file cannot be read. Kept.
OUTCOME_MALFORMED = "malformed"  # The file is not a usable record. Deleted.
OUTCOME_STALE = "stale"  # Older than MAX_RECORD_AGE_S. Deleted, no write.
OUTCOME_COM_UNAVAILABLE = "com_unavailable"  # No attempt was possible. Kept.
OUTCOME_ENDPOINT_ABSENT = "endpoint_absent"  # The endpoint is not present. Kept.
OUTCOME_LEVEL_CHANGED = "level_changed"  # Not at the lowered level. Deleted, no write.
OUTCOME_RESTORED = "restored"  # The pre-hold level was written. Deleted.
OUTCOME_FAILED = "failed"  # The attempt raised. Deleted (kept if no attempt started).
OUTCOME_CANCELLED = "cancelled"  # The cancel flag was set before the write or the delete. Kept.

_LOAD_OK = "ok"

_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

# HRESULTs, as signed 32-bit values, that mean the recorded endpoint is not
# there to restore.
_E_NOTFOUND = -2147023728  # 0x80070490: IMMDeviceEnumerator::GetDevice, no such ID
_AUDCLNT_E_DEVICE_INVALIDATED = -2004287484  # 0x88890004: the endpoint was removed
_ENDPOINT_ABSENT_HRESULTS = frozenset({_E_NOTFOUND, _AUDCLNT_E_DEVICE_INVALIDATED})


@dataclass(frozen=True)
class PttVolumeRecord:
    """One push-to-talk hold, as the plugin wrote it."""

    endpoint_id: str  # IMMDevice::GetId of the endpoint the hold lowered
    pre_hold_db: float  # the level to put back
    lowered_db: float  # the level the hold wrote
    written_at: float  # time.time() when the record was written


def record_path() -> str:
    """Return the path of the record. The app-data directory is looked up at each call."""
    return os.path.join(system.get_app_data_path(), RECORD_FILE_NAME)


def write_record(
    endpoint_id: str,
    pre_hold_db: float,
    lowered_db: float,
    *,
    path: Optional[str] = None,
    now: Optional[float] = None,
) -> bool:
    """Write the record, replacing any record on disk.

    Returns True when the record is on disk, False on any failure. Never
    raises. A failure leaves the previous record, if there is one, unchanged.
    """
    try:
        if not (
            _is_endpoint_id(endpoint_id)
            and _is_finite_number(pre_hold_db)
            and _is_finite_number(lowered_db)
        ):
            logger.warning(
                "Push-to-talk volume record not written: endpoint %r with levels "
                "%r and %r is not a record that the restore can use",
                endpoint_id, pre_hold_db, lowered_db,
            )
            return False
        target = path if path is not None else record_path()
        payload = json.dumps(
            {
                "endpoint_id": endpoint_id,
                "pre_hold_db": float(pre_hold_db),
                "lowered_db": float(lowered_db),
                "written_at": time.time() if now is None else float(now),
            },
            allow_nan=False,
        ).encode("utf-8")
        _replace_atomically(target, payload)
        return True
    except Exception as exc:
        logger.warning("Could not write the push-to-talk volume record: %s", exc)
        return False


def read_record(*, path: Optional[str] = None) -> Optional[PttVolumeRecord]:
    """Return the record on disk, or None when there is no usable record. Never raises."""
    try:
        _status, record = _load(path if path is not None else record_path())
        return record
    except Exception as exc:
        logger.warning("Could not read the push-to-talk volume record: %s", exc)
        return None


def delete_record(*, path: Optional[str] = None) -> bool:
    """Delete the record. Returns True when no record is left on disk. Never raises."""
    try:
        os.remove(path if path is not None else record_path())
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        logger.warning("Could not delete the push-to-talk volume record: %s", exc)
        return False


def restore_orphaned_volume(
    *,
    path: Optional[str] = None,
    now: Optional[float] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Put back the level of a push-to-talk hold that no process can end.

    Call this only when no Logic process runs. Returns one of the OUTCOME_*
    values. Never raises.

    * No record: nothing is done, and COM is not started.
    * A record older than MAX_RECORD_AGE_S: deleted without a write.
    * The endpoint is not present: the record is kept for a later restore.
    * The endpoint level is within LEVEL_MATCH_TOLERANCE_DB of the lowered
      level: the pre-hold level is written. Otherwise nothing is written.
      After either result, and after a failed read or write, the record is
      deleted.
    * cancel is set immediately before the write, or immediately before the
      delete that follows the Core Audio calls: that step and every step after
      it are not done, the record is kept, and the outcome is
      OUTCOME_CANCELLED. The launcher sets cancel when it stops waiting for
      the restore; by then a new Logic process can hold the speakers down
      and have its own record on disk.
    """
    try:
        target = path if path is not None else record_path()
        status, record = _load(target)
        if record is None:
            if status == OUTCOME_MALFORMED:
                delete_record(path=target)
            return status
        current_time = time.time() if now is None else now
        if current_time - record.written_at > MAX_RECORD_AGE_S:
            logger.info(
                "Push-to-talk volume record is older than %d hours: deleted without a restore",
                MAX_RECORD_AGE_S // 3600,
            )
            delete_record(path=target)
            return OUTCOME_STALE
        outcome = _restore_on_endpoint(record, cancel)
        if outcome in (OUTCOME_RESTORED, OUTCOME_LEVEL_CHANGED, OUTCOME_FAILED):
            if _cancelled(cancel):
                logger.warning(
                    "The push-to-talk volume record is kept after the outcome %s: the "
                    "launcher's time limit for the restore ended before the delete",
                    outcome,
                )
                return OUTCOME_CANCELLED
            delete_record(path=target)
        return outcome
    except Exception as exc:
        logger.error(
            "The push-to-talk speaker restore stopped on an unexpected error: %s",
            exc, exc_info=True,
        )
        return OUTCOME_FAILED


def _load(target: str) -> tuple[str, Optional[PttVolumeRecord]]:
    """Read and check the record file. Returns (status, record); record is None unless status is ok."""
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return OUTCOME_NO_RECORD, None
    except OSError as exc:
        logger.warning("Could not read the push-to-talk volume record %s: %s", target, exc)
        return OUTCOME_UNREADABLE, None
    try:
        fields = json.loads(data)
        if not isinstance(fields, dict):
            raise ValueError("the record is not a JSON object")
        record = PttVolumeRecord(
            endpoint_id=fields["endpoint_id"],
            pre_hold_db=fields["pre_hold_db"],
            lowered_db=fields["lowered_db"],
            written_at=fields["written_at"],
        )
        if not (
            _is_endpoint_id(record.endpoint_id)
            and _is_finite_number(record.pre_hold_db)
            and _is_finite_number(record.lowered_db)
            and _is_finite_number(record.written_at)
        ):
            raise ValueError("the record holds a value that the restore cannot use")
    except (ValueError, KeyError) as exc:
        # json.JSONDecodeError and UnicodeDecodeError are ValueError subclasses.
        logger.warning("The push-to-talk volume record %s is not usable: %s", target, exc)
        return OUTCOME_MALFORMED, None
    return _LOAD_OK, record


def _replace_atomically(target: str, payload: bytes) -> None:
    """Write payload to target so that a reader finds the old file or the new file, never part of one.

    The same steps as utils/click_counter_writer.py: a temporary file in the
    same directory, write, flush, fsync, then os.replace. On failure the
    temporary file is removed and the exception is raised again.

    crewcut: a process lost between the creation of the temporary file and
    os.replace leaves a ptt_volume_record.json.*.tmp file in the app-data
    directory, and nothing removes it. To remove the limit, delete those files
    in restore_orphaned_volume().
    """
    directory, name = os.path.split(target)
    temp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=directory,
            prefix=name + ".",
            suffix=".tmp",
        ) as handle:
            temp_path = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def _restore_on_endpoint(
    record: PttVolumeRecord, cancel: Optional[threading.Event] = None
) -> str:
    """Start COM, put the level back on the recorded endpoint, and stop COM again. Never raises."""
    owes_uninitialize = _initialize_com()
    if owes_uninitialize is None:
        return OUTCOME_COM_UNAVAILABLE
    try:
        # _restore_with_com holds every COM object in its own frame, so each
        # object is released when it returns, before COM stops below.
        return _restore_with_com(record, cancel)
    finally:
        if owes_uninitialize:
            _uninitialize_com()


def _restore_with_com(
    record: PttVolumeRecord, cancel: Optional[threading.Event] = None
) -> str:
    """Open the endpoint by its ID, check its level, and write the pre-hold level. Never raises.

    The pre-hold level is not written when cancel is set immediately before
    the write.
    """
    try:
        enumerator = _device_enumerator()
    except Exception as exc:
        logger.warning(
            "Push-to-talk speaker level not restored: the audio device enumerator is "
            "not available (%s). The record is kept.", exc,
        )
        return OUTCOME_COM_UNAVAILABLE
    try:
        volume = _open_endpoint_volume(enumerator, record.endpoint_id)
        if volume is None:
            logger.warning(
                "Push-to-talk speaker level not restored: endpoint %s is not active. "
                "The record is kept.", record.endpoint_id,
            )
            return OUTCOME_ENDPOINT_ABSENT
        current_db = volume.GetMasterVolumeLevel()
        # Written as "not within" so that a NaN level counts as changed.
        if not abs(current_db - record.lowered_db) <= LEVEL_MATCH_TOLERANCE_DB:
            logger.info(
                "Push-to-talk speaker level not restored: the level is %.2f dB, not the "
                "lowered level %.2f dB, so it was changed after the hold was lost",
                current_db, record.lowered_db,
            )
            return OUTCOME_LEVEL_CHANGED
        if _cancelled(cancel):
            logger.warning(
                "Push-to-talk speaker level not restored: the launcher's time limit for "
                "the restore ended before the write. The record is kept."
            )
            return OUTCOME_CANCELLED
        volume.SetMasterVolumeLevel(record.pre_hold_db, None)
        logger.info(
            "Push-to-talk speaker level restored to %.2f dB after a hold that no process ended",
            record.pre_hold_db,
        )
        return OUTCOME_RESTORED
    except Exception as exc:
        if _hresult(exc) in _ENDPOINT_ABSENT_HRESULTS:
            logger.warning(
                "Push-to-talk speaker level not restored: endpoint %s is not present (%s). "
                "The record is kept.", record.endpoint_id, exc,
            )
            return OUTCOME_ENDPOINT_ABSENT
        logger.error("Push-to-talk speaker level not restored: %s", exc)
        return OUTCOME_FAILED


def _device_enumerator():
    """Create the Core Audio device enumerator (IMMDeviceEnumerator).

    pycaw is imported here and not at module level: importing comtypes starts
    COM on the importing thread, and a launcher with no record to restore must
    not do that.
    """
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetDeviceEnumerator()


def _open_endpoint_volume(enumerator, endpoint_id: str):
    """Return the IAudioEndpointVolume of the endpoint with this ID, or None when it is not active.

    GetDevice raises COMError E_NOTFOUND for an unknown ID; the caller sorts
    that error. The activation is the same call that
    SystemVolumePlugin._connect_audio_device makes.
    """
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import DEVICE_STATE, IAudioEndpointVolume

    device = enumerator.GetDevice(endpoint_id)
    if device.GetState() != DEVICE_STATE.ACTIVE.value:
        return None
    return device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(
        IAudioEndpointVolume
    )


def _load_ole32():
    """Load ole32 with CoInitializeEx and CoUninitialize typed.

    A separate WinDLL object, so that these types do not change the shared
    ctypes.windll.ole32 object that handlers/volume_router.py calls.
    """
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    return ole32


def _initialize_com(ole32=None) -> Optional[bool]:
    """Start COM on this thread in the single-threaded apartment mode that comtypes uses.

    Returns True when the call started COM, or added a count to COM that was
    already started in the same mode (S_OK or S_FALSE). The caller then owes
    one _uninitialize_com(). Returns False when COM was already started in the
    other mode (RPC_E_CHANGED_MODE): COM is usable, and the call added no
    count. Returns None when COM could not be started.
    """
    try:
        if ole32 is None:
            ole32 = _load_ole32()
        hresult = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    except Exception as exc:
        logger.warning("COM did not start for the push-to-talk speaker restore: %s", exc)
        return None
    if hresult in (_S_OK, _S_FALSE):
        return True
    if hresult == _RPC_E_CHANGED_MODE:
        return False
    logger.warning(
        "COM did not start for the push-to-talk speaker restore: HRESULT 0x%08X",
        hresult & 0xFFFFFFFF,
    )
    return None


def _uninitialize_com(ole32=None) -> None:
    """Balance one _initialize_com() call that returned True. Never raises."""
    try:
        if ole32 is None:
            ole32 = _load_ole32()
        ole32.CoUninitialize()
    except Exception as exc:
        logger.warning("COM did not stop after the push-to-talk speaker restore: %s", exc)


def _cancelled(cancel: Optional[threading.Event]) -> bool:
    """Return True when the caller has set the cancel flag."""
    return cancel is not None and cancel.is_set()


def _hresult(exc: BaseException) -> Optional[int]:
    """Return the HRESULT of a COMError or OSError as a signed 32-bit value, or None."""
    value = getattr(exc, "hresult", None)
    if value is None:
        value = getattr(exc, "winerror", None)
    if not isinstance(value, int):
        return None
    return ctypes.c_int32(value).value


def _is_endpoint_id(value) -> bool:
    return isinstance(value, str) and value != ""


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
