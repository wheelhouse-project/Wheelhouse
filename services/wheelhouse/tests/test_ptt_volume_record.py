"""Tests for utils/ptt_volume_record.py (wh-ptt-mute-orphaned-on-process-loss).

A push-to-talk hold turns the speakers down in the Logic process and keeps the
level to restore only in memory. A Logic process lost during a hold therefore
left the speakers down with nothing able to put them back. The record tested
here is the copy on disk that the launcher restores from.

No test here calls a real Windows audio API or ole32. COM initialisation is
replaced by a recorder, the device enumerator by a fake that serves fake
endpoints, and the app-data directory by a temporary directory.
"""
import json
import logging
import math
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from comtypes import COMError
from pycaw.pycaw import AudioUtilities

from services.wheelhouse.utils import ptt_volume_record as record_module
from services.wheelhouse.utils import system

# Captured before the autouse fixture below replaces them, so the tests of
# the HRESULT handling can call the real functions with a fake ole32.
REAL_INITIALIZE_COM = record_module._initialize_com
REAL_UNINITIALIZE_COM = record_module._uninitialize_com

SPEAKERS = "{0.0.0.00000000}.{synthetic-speakers}"
HEADSET = "{0.0.0.00000000}.{synthetic-unplugged-headset}"
PRE_HOLD_DB = -10.0
LOWERED_DB = -65.25
NOW = 1_800_000_000.0
DAY_S = 24 * 60 * 60

# HRESULTs as the signed values comtypes puts on COMError.hresult.
E_NOTFOUND = -2147023728  # 0x80070490, HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
E_INVALIDARG = -2147024809  # 0x80070057
E_OUTOFMEMORY = -2147024882  # 0x8007000E
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
AUDCLNT_E_DEVICE_INVALIDATED = -2004287484  # 0x88890004

DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATE_NOTPRESENT = 0x4


@pytest.fixture(autouse=True)
def app_data_dir(tmp_path, monkeypatch):
    """Send the record to a temporary directory, never the real %APPDATA%."""
    directory = tmp_path / "app_data"
    directory.mkdir()
    monkeypatch.setattr(system, "get_app_data_path", lambda: str(directory))
    return directory


@pytest.fixture(autouse=True)
def com_calls(monkeypatch):
    """Record COM initialisation instead of calling ole32."""
    calls = []

    def initialize():
        calls.append("initialize")
        return True

    monkeypatch.setattr(record_module, "_initialize_com", initialize)
    monkeypatch.setattr(record_module, "_uninitialize_com", lambda: calls.append("uninitialize"))
    return calls


class FakeEndpointVolume:
    """The two IAudioEndpointVolume methods the restore uses."""

    def __init__(self, level_db):
        self.level_db = level_db
        self.writes = []
        self.read_error = None
        self.write_error = None

    def GetMasterVolumeLevel(self):
        if self.read_error is not None:
            raise self.read_error
        return self.level_db

    def SetMasterVolumeLevel(self, level_db, event_context):
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(level_db)
        self.level_db = level_db


class FakeDevice:
    def __init__(self, volume, state=DEVICE_STATE_ACTIVE):
        self.volume = volume
        self.state = state

    def GetState(self):
        return self.state

    def Activate(self, iid, cls_context, activation_params):
        return SimpleNamespace(QueryInterface=lambda interface: self.volume)


class FakeEnumerator:
    """Serves devices by endpoint ID, and answers E_NOTFOUND as Windows does."""

    def __init__(self):
        self.devices = {}
        self.requested = []

    def GetDevice(self, endpoint_id):
        self.requested.append(endpoint_id)
        if endpoint_id not in self.devices:
            raise COMError(E_NOTFOUND, "Element not found.", None)
        return self.devices[endpoint_id]


@pytest.fixture
def speakers(monkeypatch):
    """One active fake endpoint, still at the lowered level."""
    volume = FakeEndpointVolume(LOWERED_DB)
    device = FakeDevice(volume)
    enumerator = FakeEnumerator()
    enumerator.devices[SPEAKERS] = device
    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", lambda: enumerator)
    return SimpleNamespace(volume=volume, device=device, enumerator=enumerator)


def place_record(directory, **overrides):
    """Write a record file directly, in the format the plugin writes."""
    fields = {
        "endpoint_id": SPEAKERS,
        "pre_hold_db": PRE_HOLD_DB,
        "lowered_db": LOWERED_DB,
        "written_at": NOW,
    }
    fields.update(overrides)
    path = directory / record_module.RECORD_FILE_NAME
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The record file
# ---------------------------------------------------------------------------


class TestTheRecordFile:
    def test_a_written_record_holds_the_endpoint_both_levels_and_the_time(self, app_data_dir):
        assert record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB, now=NOW) is True

        on_disk = json.loads(
            (app_data_dir / record_module.RECORD_FILE_NAME).read_text(encoding="utf-8")
        )
        assert on_disk == {
            "endpoint_id": SPEAKERS,
            "pre_hold_db": PRE_HOLD_DB,
            "lowered_db": LOWERED_DB,
            "written_at": NOW,
        }

    def test_a_written_record_reads_back(self):
        record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB, now=NOW)

        assert record_module.read_record() == record_module.PttVolumeRecord(
            endpoint_id=SPEAKERS, pre_hold_db=PRE_HOLD_DB, lowered_db=LOWERED_DB, written_at=NOW,
        )

    def test_a_second_write_replaces_the_first(self):
        record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB, now=NOW)
        record_module.write_record(SPEAKERS, -4.0, LOWERED_DB, now=NOW + 5)

        record = record_module.read_record()
        assert (record.pre_hold_db, record.written_at) == (-4.0, NOW + 5)

    def test_the_write_is_synced_to_a_file_beside_the_record_and_then_replaces_it(
        self, app_data_dir, monkeypatch
    ):
        """A process lost part way through a write must leave the previous
        record or the new one, never a partial file."""
        steps = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fsync(descriptor):
            steps.append("fsync")
            real_fsync(descriptor)

        def replace(source, destination):
            steps.append((
                "replace",
                os.path.dirname(source) == str(app_data_dir),
                destination == str(app_data_dir / record_module.RECORD_FILE_NAME),
            ))
            real_replace(source, destination)

        monkeypatch.setattr(record_module.os, "fsync", fsync)
        monkeypatch.setattr(record_module.os, "replace", replace)

        assert record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB, now=NOW) is True
        assert steps == ["fsync", ("replace", True, True)]

    def test_a_failed_replace_keeps_the_previous_record_and_leaves_no_temporary_file(
        self, app_data_dir, monkeypatch
    ):
        record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB, now=NOW)
        real_replace = os.replace

        def refuse(source, destination):
            raise PermissionError("the record is locked")

        # os.replace is put back at once, and not with monkeypatch.undo(),
        # because undo() would also remove the app-data redirect above.
        monkeypatch.setattr(record_module.os, "replace", refuse)
        refused = record_module.write_record(SPEAKERS, -4.0, LOWERED_DB, now=NOW + 5)
        monkeypatch.setattr(record_module.os, "replace", real_replace)

        assert refused is False
        assert record_module.read_record().pre_hold_db == PRE_HOLD_DB
        assert sorted(p.name for p in app_data_dir.iterdir()) == [record_module.RECORD_FILE_NAME]

    def test_a_write_that_cannot_reach_the_directory_returns_false(self, tmp_path, monkeypatch):
        not_a_directory = tmp_path / "not_a_directory"
        not_a_directory.write_text("")
        monkeypatch.setattr(system, "get_app_data_path", lambda: str(not_a_directory))

        assert record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB) is False

    def test_a_write_whose_directory_lookup_raises_returns_false(self, monkeypatch):
        def no_directory():
            raise OSError("no app-data directory")

        monkeypatch.setattr(system, "get_app_data_path", no_directory)

        assert record_module.write_record(SPEAKERS, PRE_HOLD_DB, LOWERED_DB) is False

    @pytest.mark.parametrize(
        "endpoint_id, pre_hold_db, lowered_db",
        [
            ("", PRE_HOLD_DB, LOWERED_DB),
            (None, PRE_HOLD_DB, LOWERED_DB),
            (SPEAKERS, math.nan, LOWERED_DB),
            (SPEAKERS, PRE_HOLD_DB, math.inf),
            (SPEAKERS, "loud", LOWERED_DB),
        ],
    )
    def test_a_record_the_restore_could_not_act_on_is_not_written(
        self, app_data_dir, endpoint_id, pre_hold_db, lowered_db
    ):
        assert record_module.write_record(endpoint_id, pre_hold_db, lowered_db, now=NOW) is False
        assert list(app_data_dir.iterdir()) == []

    def test_reading_with_no_record_gives_none(self):
        assert record_module.read_record() is None

    def test_delete_removes_the_record(self, app_data_dir):
        path = place_record(app_data_dir)

        assert record_module.delete_record() is True
        assert not path.exists()

    def test_deleting_a_record_that_is_not_there_succeeds(self):
        assert record_module.delete_record() is True


# ---------------------------------------------------------------------------
# Restoring a lost hold
# ---------------------------------------------------------------------------


class TestRestoringALostHold:
    def test_a_level_still_at_the_lowered_level_is_restored_and_the_record_deleted(
        self, app_data_dir, speakers, com_calls
    ):
        path = place_record(app_data_dir)

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert speakers.volume.writes == [PRE_HOLD_DB]
        assert outcome == record_module.OUTCOME_RESTORED
        assert not path.exists()
        assert speakers.enumerator.requested == [SPEAKERS]
        assert com_calls == ["initialize", "uninitialize"]

    def test_a_level_the_user_changed_is_left_alone_and_the_record_deleted(
        self, app_data_dir, speakers
    ):
        path = place_record(app_data_dir)
        speakers.volume.level_db = -30.0

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert not path.exists()
        assert speakers.volume.writes == []
        assert speakers.enumerator.requested == [SPEAKERS]
        assert outcome == record_module.OUTCOME_LEVEL_CHANGED

    def test_an_absent_endpoint_keeps_the_record(self, app_data_dir, speakers):
        path = place_record(app_data_dir, endpoint_id=HEADSET)

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_ENDPOINT_ABSENT
        assert path.exists()
        assert speakers.enumerator.requested == [HEADSET]
        assert speakers.volume.writes == []

    def test_a_record_older_than_24_hours_is_deleted_without_a_write(
        self, app_data_dir, speakers, com_calls
    ):
        path = place_record(app_data_dir, written_at=NOW)

        outcome = record_module.restore_orphaned_volume(now=NOW + DAY_S + 1)

        assert not path.exists()
        assert speakers.volume.writes == []
        assert speakers.enumerator.requested == []
        assert com_calls == []
        assert outcome == record_module.OUTCOME_STALE

    def test_a_record_just_under_24_hours_old_is_still_restored(self, app_data_dir, speakers):
        place_record(app_data_dir, written_at=NOW)

        outcome = record_module.restore_orphaned_volume(now=NOW + DAY_S - 1)

        assert speakers.volume.writes == [PRE_HOLD_DB]
        assert outcome == record_module.OUTCOME_RESTORED

    def test_a_record_exactly_24_hours_old_is_still_restored(self, app_data_dir, speakers):
        place_record(app_data_dir, written_at=NOW)

        outcome = record_module.restore_orphaned_volume(now=NOW + DAY_S)

        assert speakers.volume.writes == [PRE_HOLD_DB]
        assert outcome == record_module.OUTCOME_RESTORED

    @pytest.mark.parametrize("offset_db", [0.9, -0.9, 1.0, -1.0])
    def test_a_level_within_1_db_of_the_lowered_level_counts_as_still_lowered(
        self, app_data_dir, speakers, offset_db
    ):
        place_record(app_data_dir)
        speakers.volume.level_db = LOWERED_DB + offset_db

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert speakers.volume.writes == [PRE_HOLD_DB]
        assert outcome == record_module.OUTCOME_RESTORED

    @pytest.mark.parametrize("offset_db", [1.1, -1.1])
    def test_a_level_more_than_1_db_from_the_lowered_level_counts_as_changed(
        self, app_data_dir, speakers, offset_db
    ):
        path = place_record(app_data_dir)
        speakers.volume.level_db = LOWERED_DB + offset_db

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert speakers.volume.writes == []
        assert outcome == record_module.OUTCOME_LEVEL_CHANGED
        assert not path.exists()

    def test_an_endpoint_that_is_not_active_keeps_the_record(self, app_data_dir, speakers):
        path = place_record(app_data_dir)
        speakers.device.state = DEVICE_STATE_NOTPRESENT

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_ENDPOINT_ABSENT
        assert path.exists()
        assert speakers.volume.writes == []

    def test_an_endpoint_removed_during_the_restore_keeps_the_record(self, app_data_dir, speakers):
        path = place_record(app_data_dir)
        speakers.volume.read_error = COMError(AUDCLNT_E_DEVICE_INVALIDATED, "Device invalidated.", None)

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_ENDPOINT_ABSENT
        assert path.exists()

    def test_a_write_that_fails_still_deletes_the_record(self, app_data_dir, speakers, com_calls):
        path = place_record(app_data_dir)
        speakers.volume.write_error = COMError(E_INVALIDARG, "The parameter is incorrect.", None)

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_FAILED
        assert not path.exists()
        assert com_calls == ["initialize", "uninitialize"]

    def test_no_record_asks_nothing_of_com(self, speakers, com_calls):
        outcome = record_module.restore_orphaned_volume(now=NOW)

        assert outcome == record_module.OUTCOME_NO_RECORD
        assert com_calls == []
        assert speakers.enumerator.requested == []

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            json.dumps(["a", "list"]),
            json.dumps({"endpoint_id": SPEAKERS, "pre_hold_db": PRE_HOLD_DB}),
            json.dumps({"endpoint_id": "", "pre_hold_db": PRE_HOLD_DB,
                        "lowered_db": LOWERED_DB, "written_at": NOW}),
            '{"endpoint_id": "x", "pre_hold_db": NaN, "lowered_db": -65.25, "written_at": 1}',
        ],
    )
    def test_a_malformed_record_is_deleted_without_a_write(
        self, app_data_dir, speakers, com_calls, content
    ):
        path = app_data_dir / record_module.RECORD_FILE_NAME
        path.write_text(content, encoding="utf-8")

        outcome = record_module.restore_orphaned_volume(now=NOW)

        assert not path.exists()
        assert outcome == record_module.OUTCOME_MALFORMED
        assert com_calls == []
        assert speakers.volume.writes == []

    def test_com_that_cannot_start_keeps_the_record(self, app_data_dir, speakers, monkeypatch):
        path = place_record(app_data_dir)
        monkeypatch.setattr(record_module, "_initialize_com", lambda: None)

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_COM_UNAVAILABLE
        assert path.exists()
        assert speakers.enumerator.requested == []

    def test_an_enumerator_that_cannot_be_created_keeps_the_record(
        self, app_data_dir, com_calls, monkeypatch
    ):
        path = place_record(app_data_dir)

        def no_enumerator():
            raise OSError("the audio service is not running")

        monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", no_enumerator)

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_COM_UNAVAILABLE
        assert path.exists()
        assert com_calls == ["initialize", "uninitialize"]

    def test_com_this_call_did_not_start_is_not_uninitialised(
        self, app_data_dir, speakers, monkeypatch
    ):
        calls = []

        def already_started_in_another_mode():
            calls.append("initialize")
            return False

        place_record(app_data_dir)
        monkeypatch.setattr(record_module, "_initialize_com", already_started_in_another_mode)
        monkeypatch.setattr(record_module, "_uninitialize_com", lambda: calls.append("uninitialize"))

        outcome = record_module.restore_orphaned_volume(now=NOW + 60)

        assert outcome == record_module.OUTCOME_RESTORED
        assert calls == ["initialize"]

    def test_an_unexpected_error_is_returned_as_an_outcome_and_not_raised(self, monkeypatch):
        def broken_directory_lookup():
            raise RuntimeError("unexpected")

        monkeypatch.setattr(system, "get_app_data_path", broken_directory_lookup)

        assert record_module.restore_orphaned_volume(now=NOW) == record_module.OUTCOME_FAILED


# ---------------------------------------------------------------------------
# A restore that goes on after the launcher's time limit
# ---------------------------------------------------------------------------


class TestARestoreCancelledAtTheLaunchersTimeLimit:
    """The launcher waits for the restore at most SHUTDOWN_GRACE_PERIOD_S and
    then sets the cancel flag. A restore that goes on after that must not
    write the level or delete the record: by then a new Logic process can
    hold the speakers down and have its own record on disk. Each test sets
    the flag inside the last Core Audio call before the step it checks."""

    @staticmethod
    def _cancel_after(cancel, method):
        """Wrap a fake endpoint method so that the flag is set when the call ends, also when it raises."""
        def call_then_cancel(*args):
            try:
                return method(*args)
            finally:
                cancel.set()

        return call_then_cancel

    def test_a_flag_set_before_the_write_stops_the_write_and_keeps_the_record(
        self, app_data_dir, speakers, com_calls, caplog
    ):
        """The flag is set inside the level read, the last Core Audio call before the write."""
        path = place_record(app_data_dir)
        cancel = threading.Event()
        speakers.volume.GetMasterVolumeLevel = self._cancel_after(
            cancel, speakers.volume.GetMasterVolumeLevel
        )

        with caplog.at_level(logging.WARNING, logger=record_module.logger.name):
            outcome = record_module.restore_orphaned_volume(now=NOW + 60, cancel=cancel)

        assert speakers.volume.writes == [], "the restore wrote the level after the cancel flag was set"
        assert path.exists(), "the restore deleted the record after the cancel flag was set"
        assert outcome == record_module.OUTCOME_CANCELLED
        assert "the launcher's time limit" in caplog.text
        assert com_calls == ["initialize", "uninitialize"]

    @pytest.mark.parametrize(
        ("level_db", "write_error", "cancelling_call"),
        [
            pytest.param(LOWERED_DB, None, "SetMasterVolumeLevel", id="restored"),
            pytest.param(-30.0, None, "GetMasterVolumeLevel", id="level-changed"),
            pytest.param(
                LOWERED_DB, COMError(E_INVALIDARG, "The parameter is incorrect.", None),
                "SetMasterVolumeLevel", id="failed",
            ),
        ],
    )
    def test_a_flag_set_before_the_delete_keeps_the_record(
        self, app_data_dir, speakers, caplog, level_db, write_error, cancelling_call
    ):
        """Each attempt that deletes the record: a write, a level the user
        changed, and a write that raises. The flag is set inside the last Core
        Audio call of the attempt, after the check before the write."""
        path = place_record(app_data_dir)
        cancel = threading.Event()
        speakers.volume.level_db = level_db
        speakers.volume.write_error = write_error
        setattr(
            speakers.volume, cancelling_call,
            self._cancel_after(cancel, getattr(speakers.volume, cancelling_call)),
        )

        with caplog.at_level(logging.WARNING, logger=record_module.logger.name):
            outcome = record_module.restore_orphaned_volume(now=NOW + 60, cancel=cancel)

        assert path.exists(), "the restore deleted the record after the cancel flag was set"
        assert outcome == record_module.OUTCOME_CANCELLED
        assert "the launcher's time limit" in caplog.text


# ---------------------------------------------------------------------------
# COM initialisation
# ---------------------------------------------------------------------------


class TestComInitialisation:
    """The restore starts COM on its own thread, and gives back only what it took."""

    @pytest.mark.parametrize(
        "hresult, owes_uninitialize",
        [
            (0, True),  # S_OK
            (1, True),  # S_FALSE: already started in this mode, still counted
            (RPC_E_CHANGED_MODE, False),  # started in the other mode, usable, not counted
            (E_OUTOFMEMORY, None),  # not started
        ],
    )
    def test_the_result_says_whether_the_call_owes_an_uninitialise(self, hresult, owes_uninitialize):
        ole32 = SimpleNamespace(CoInitializeEx=Mock(return_value=hresult))

        assert REAL_INITIALIZE_COM(ole32) is owes_uninitialize
        ole32.CoInitializeEx.assert_called_once_with(None, 0x2)  # COINIT_APARTMENTTHREADED

    def test_an_ole32_that_cannot_be_called_means_com_is_unavailable(self):
        ole32 = SimpleNamespace(CoInitializeEx=Mock(side_effect=OSError("no ole32")))

        assert REAL_INITIALIZE_COM(ole32) is None

    def test_uninitialise_calls_couninitialize(self):
        ole32 = SimpleNamespace(CoUninitialize=Mock())

        REAL_UNINITIALIZE_COM(ole32)

        ole32.CoUninitialize.assert_called_once_with()
