"""Tests for the background update check (wh-release-update-check, stage A)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import tomllib
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

import update_check


@pytest.mark.parametrize(
    "newer, older",
    [
        ("1.2.4", "1.2.3"),
        ("v1.3.0", "1.2.9"),
        ("2.0", "1.9.9"),
        ("1.2.3.1", "1.2.3"),
        ("1.10.0", "1.9.0"),
    ],
)
def test_is_newer_version_true_when_candidate_is_newer(newer, older):
    assert update_check.is_newer_version(newer, older) is True


@pytest.mark.parametrize(
    "candidate, running",
    [("1.2.3", "1.2.3"), ("v1.2.3", "1.2.3"), ("1.2", "1.2.0"), ("1.2.3.0", "1.2.3")],
)
def test_is_newer_version_false_when_equal(candidate, running):
    assert update_check.is_newer_version(candidate, running) is False


@pytest.mark.parametrize(
    "candidate, running", [("1.2.2", "1.2.3"), ("v0.9", "1.0"), ("1.9.0", "1.10.0")]
)
def test_is_newer_version_false_when_older(candidate, running):
    assert update_check.is_newer_version(candidate, running) is False


@pytest.mark.parametrize(
    "bad",
    ["", "v", "abc", "1.2.x", "1..2", "1.2.3.4.5", "1.2.3-beta", "unknown", " 1.2", "1.-2", "\u0661.2"],
)
def test_malformed_version_is_never_newer_and_never_parses(bad):
    assert update_check.parse_version(bad) is None
    assert update_check.is_newer_version(bad, "1.0.0") is False
    assert update_check.is_newer_version("9.9.9", bad) is False


def test_parse_version_strips_one_leading_v_and_reads_up_to_four_parts():
    assert update_check.parse_version("v1.2.3.4") == (1, 2, 3, 4)
    assert update_check.parse_version("7") == (7,)
    assert update_check.parse_version("vv1.2") is None


# --- record file ---------------------------------------------------------

DAY = 24 * 3600


def test_state_round_trips_through_the_record_file(tmp_path):
    path = tmp_path / "sub" / "update_check_state.toml"
    state = update_check.UpdateCheckState(
        last_check=1000.5, notified_version="1.2.3", notified_at=2000.25
    )
    assert update_check.save_state(path, state) is True
    assert update_check.load_state(path) == state
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_load_state_missing_file_gives_empty_state(tmp_path):
    assert update_check.load_state(tmp_path / "none.toml") == update_check.UpdateCheckState()


@pytest.mark.parametrize(
    "content",
    [
        b"this is = = not toml",
        b'last_check = "yesterday"\nnotified_version = 5\nnotified_at = "x"\n',
        b'last_check = "unterminated\n',
        b"\xff\xfe\x80 not utf-8",
    ],
)
def test_load_state_unreadable_file_gives_empty_state(tmp_path, content):
    path = tmp_path / "state.toml"
    path.write_bytes(content)
    assert update_check.load_state(path) == update_check.UpdateCheckState()


@pytest.mark.parametrize(
    "content, reaches_the_error_handler",
    [
        (b"this is = = not toml", True),
        (b'last_check = "unterminated\n', True),
        (b"\xff\xfe\x80 not utf-8", True),
        (b"", False),  # an empty file is valid TOML: empty state, no error
    ],
)
def test_load_state_logs_only_when_the_file_cannot_be_parsed(
    tmp_path, content, reaches_the_error_handler, caplog
):
    path = tmp_path / "state.toml"
    path.write_bytes(content)
    with caplog.at_level(logging.DEBUG, logger="update_check"):
        update_check.load_state(path)
    assert bool(caplog.records) is reaches_the_error_handler


def test_load_state_read_error_gives_empty_state_and_logs_one_line(tmp_path, caplog):
    path = tmp_path / "state.toml"
    path.write_text('last_check = 5.0\n', encoding="utf-8")
    with patch("builtins.open", side_effect=OSError("denied")), caplog.at_level(
        logging.DEBUG, logger="update_check"
    ):
        state = update_check.load_state(path)
    assert state == update_check.UpdateCheckState()
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING


def test_save_state_unserializable_value_returns_false_and_leaves_old_file(
    tmp_path, caplog
):
    path = tmp_path / "state.toml"
    update_check.save_state(path, update_check.UpdateCheckState(last_check=1.0))
    bad = update_check.UpdateCheckState(notified_version=None)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger="update_check"):
        ok = update_check.save_state(path, bad)
    assert ok is False
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    assert tomllib.loads(path.read_text("utf-8"))["last_check"] == 1.0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.toml"]


def test_state_file_path_resolves_through_get_user_data_dir(tmp_path):
    with patch("utils.system.get_user_data_dir", return_value=tmp_path):
        assert update_check.state_file_path() == tmp_path / "update_check_state.toml"


def test_default_record_path_is_the_user_data_dir_file(tmp_path):
    with patch("utils.system.get_user_data_dir", return_value=tmp_path):
        result = update_check.check_for_update(
            _FakeConfig(),
            get_version=lambda: "1.0.0",
            fetch=lambda version: "v1.0.0",
            clock=lambda: START,
        )
        assert result is None
        record = tmp_path / "update_check_state.toml"
        assert update_check.load_state().last_check == START
    assert tomllib.loads(record.read_text("utf-8"))["last_check"] == START


def test_save_state_failure_returns_false_and_leaves_old_file(tmp_path):
    path = tmp_path / "state.toml"
    update_check.save_state(path, update_check.UpdateCheckState(last_check=1.0))
    with patch.object(update_check.os, "replace", side_effect=OSError("denied")):
        ok = update_check.save_state(path, update_check.UpdateCheckState(last_check=2.0))
    assert ok is False
    assert tomllib.loads(path.read_text("utf-8"))["last_check"] == 1.0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.toml"]


# --- schedule ------------------------------------------------------------


def test_schedule_constants_match_the_acceptance_field():
    assert update_check.CHECK_INTERVAL_SECONDS == DAY
    assert update_check.FIRST_CHECK_DELAY_SECONDS == 60
    assert update_check.NOTICE_DEFER_SECONDS == 7 * DAY
    assert update_check.REQUEST_TIMEOUT_SECONDS == 15
    assert update_check.UPDATE_CHECK_SETTING == "CHECK_FOR_UPDATES"
    assert update_check.UPDATE_CHECK_DEFAULT is True


def test_first_check_waits_sixty_seconds_when_never_checked():
    assert update_check.seconds_until_first_check(0.0, now=5000.0) == 60


def test_first_check_waits_sixty_seconds_when_last_check_is_old():
    # The recorded time must be positive. A negative time returns through
    # the "never checked" branch, which the test above already covers, and
    # then this test never reaches the 60-second floor.
    now = 4.0 * DAY
    assert update_check.seconds_until_first_check(now - 3 * DAY, now=now) == 60


def test_first_check_waits_for_the_24_hour_mark_after_a_recent_check():
    now = 100000.0
    last = now - 3600  # one hour ago
    assert update_check.seconds_until_first_check(last, now) == DAY - 3600


def test_first_check_never_waits_longer_than_24_hours_if_clock_moved_back():
    now = 100000.0
    assert update_check.seconds_until_first_check(now + 10 * DAY, now) == DAY


# --- notice schedule -----------------------------------------------------


def _state(version="", at=0.0):
    return update_check.UpdateCheckState(notified_version=version, notified_at=at)


def test_notice_due_when_never_shown():
    assert update_check.notice_is_due(_state(), "1.2.3", now=100.0) is True


def test_notice_not_due_again_for_same_version_inside_seven_days():
    state = _state("1.2.3", at=1000.0)
    assert update_check.notice_is_due(state, "1.2.3", now=1000.0 + 7 * DAY - 1) is False
    assert update_check.notice_is_due(state, "v1.2.3", now=1000.0 + 1) is False


def test_notice_due_again_for_same_version_after_seven_days():
    state = _state("1.2.3", at=1000.0)
    assert update_check.notice_is_due(state, "1.2.3", now=1000.0 + 7 * DAY) is True


def test_notice_due_at_once_for_a_version_newer_than_the_deferred_one():
    state = _state("1.2.3", at=1000.0)
    assert update_check.notice_is_due(state, "1.2.4", now=1001.0) is True


def test_notice_not_due_for_an_older_version_inside_seven_days():
    state = _state("1.2.3", at=1000.0)
    assert update_check.notice_is_due(state, "1.2.2", now=1001.0) is False


def test_notice_due_when_recorded_version_is_unreadable():
    assert update_check.notice_is_due(_state("garbage", at=1000.0), "1.2.3", now=1001.0) is True


def test_notice_due_again_when_the_recorded_time_is_in_the_future():
    # The clock was set back, or the record was edited: the recorded time is
    # after now. A wait measured from a future time must not hold the notice.
    state = _state("1.2.3", at=5000.0)
    assert update_check.notice_is_due(state, "1.2.3", now=1000.0) is True


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_notice_due_again_when_the_recorded_time_is_not_a_finite_number(bad):
    state = _state("1.2.3", at=bad)
    assert update_check.notice_is_due(state, "1.2.3", now=1000.0) is True


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "9e18"])
def test_check_shows_the_notice_when_the_record_file_holds_a_bad_time(tmp_path, text):
    path = tmp_path / "state.toml"
    path.write_text(
        f'notified_version = "1.2.3"\nnotified_at = {text}\n', encoding="utf-8"
    )
    result = update_check.check_for_update(
        _FakeConfig(),
        state_path=path,
        get_version=lambda: "1.0.0",
        fetch=lambda version: "v1.2.3",
        clock=lambda: START,
    )
    assert result == ("1.2.3", "v1.2.3")


# --- the request ---------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release_body(tag="v1.2.3"):
    return json.dumps({"tag_name": tag, "name": "x"}).encode()


def test_fetch_sends_one_get_with_only_a_wheelhouse_user_agent():
    with patch.object(
        update_check.urllib.request, "urlopen", return_value=_FakeResponse(_release_body())
    ) as urlopen:
        tag = update_check.fetch_latest_tag("1.0.0")
    assert tag == "v1.2.3"
    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "https://api.github.com/repos/wheelhouse-project/Wheelhouse/releases/latest"
    )
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.header_items() == [("User-agent", "Wheelhouse/1.0.0")]
    assert urlopen.call_args.kwargs == {"timeout": 15}


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"name": "no tag"}', b'{"tag_name": 5}', b'{"tag_name": ""}', b"\xff\xfe"],
)
def test_fetch_raises_on_an_answer_it_cannot_read(body):
    with patch.object(
        update_check.urllib.request, "urlopen", return_value=_FakeResponse(body)
    ):
        with pytest.raises(update_check.UpdateCheckError):
            update_check.fetch_latest_tag("1.0.0")


def test_fetch_raises_on_an_oversized_answer():
    big = b" " * (update_check.MAX_RESPONSE_BYTES + 1)
    with patch.object(
        update_check.urllib.request, "urlopen", return_value=_FakeResponse(big)
    ):
        with pytest.raises(update_check.UpdateCheckError):
            update_check.fetch_latest_tag("1.0.0")


def test_fetch_wraps_network_errors():
    import urllib.error

    with patch.object(
        update_check.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
    ):
        with pytest.raises(update_check.UpdateCheckError):
            update_check.fetch_latest_tag("1.0.0")


# --- the check and the runner -------------------------------------------

START = 1_800_000_000.0


class _FakeConfig:
    """Stands in for ConfigService: only ``get(key, default)``."""

    def __init__(self, **values):
        self.values = values
        self.asked = []

    def get(self, key, default=None):
        self.asked.append(key)
        return self.values.get(key, default)


class _Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


def _fetcher(tags):
    """A fetch fake that returns tags[i] on call i (the last one repeats)."""
    calls = []

    def fetch(version):
        calls.append(version)
        return tags[min(len(calls) - 1, len(tags) - 1)]

    fetch.calls = calls
    return fetch


def _check(config, tmp_path, *, fetch, version="1.0.0", clock=None):
    return update_check.check_for_update(
        config,
        state_path=tmp_path / "state.toml",
        get_version=lambda: version,
        fetch=fetch,
        clock=clock or _Clock(),
    )


def test_check_returns_version_and_tag_for_a_newer_release(tmp_path):
    fetch = _fetcher(["v1.2.3"])
    assert _check(_FakeConfig(), tmp_path, fetch=fetch) == ("1.2.3", "v1.2.3")
    assert fetch.calls == ["1.0.0"]


@pytest.mark.parametrize("tag", ["v1.0.0", "v0.9.0", "weekly-build", "v1.0.0-rc1"])
def test_check_shows_nothing_for_equal_older_or_malformed_tags(tmp_path, tag):
    assert _check(_FakeConfig(), tmp_path, fetch=_fetcher([tag])) is None


def test_key_off_makes_no_request_and_writes_no_record(tmp_path):
    fetch = MagicMock(name="fetch")
    with patch.object(update_check.urllib.request, "urlopen") as urlopen:
        result = _check(_FakeConfig(CHECK_FOR_UPDATES=False), tmp_path, fetch=fetch)
        # Also through the real fetch function, not only the injected one.
        result_real = update_check.check_for_update(
            _FakeConfig(CHECK_FOR_UPDATES=False),
            state_path=tmp_path / "state.toml",
            get_version=lambda: "1.0.0",
            clock=_Clock(),
        )
    assert result is None and result_real is None
    fetch.assert_not_called()
    urlopen.assert_not_called()
    assert not (tmp_path / "state.toml").exists()


def test_config_without_the_key_behaves_as_on(tmp_path):
    config = _FakeConfig()  # get() returns the default it is given
    fetch = _fetcher(["v2.0.0"])
    assert _check(config, tmp_path, fetch=fetch) == ("2.0.0", "v2.0.0")
    assert config.asked == ["CHECK_FOR_UPDATES"]


def test_unknown_running_version_logs_one_line_and_skips(tmp_path, caplog):
    fetch = MagicMock(name="fetch")
    with caplog.at_level(logging.DEBUG, logger="update_check"):
        assert _check(_FakeConfig(), tmp_path, fetch=fetch, version="unknown") is None
    fetch.assert_not_called()
    assert [r.name for r in caplog.records] == ["update_check"]
    assert "unknown" in caplog.records[0].getMessage()


def test_network_failure_shows_nothing_logs_one_line_and_still_counts_as_a_try(
    tmp_path, caplog
):
    clock = _Clock()
    with patch.object(
        update_check.urllib.request, "urlopen", side_effect=OSError("offline")
    ), caplog.at_level(logging.DEBUG, logger="update_check"):
        result = update_check.check_for_update(
            _FakeConfig(),
            state_path=tmp_path / "state.toml",
            get_version=lambda: "1.0.0",
            clock=clock,
        )
    assert result is None
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    # The attempt is recorded, so the next try is the next 24-hour check.
    assert update_check.load_state(tmp_path / "state.toml").last_check == START


def test_failed_last_check_write_makes_no_release_request(tmp_path):
    # The last_check record is the only thing that holds the 24-hour limit
    # across restarts. When it cannot be made durable, no request goes out.
    fetch = _fetcher(["v1.2.3"])
    with patch.object(update_check, "save_state", return_value=False):
        result = _check(_FakeConfig(), tmp_path, fetch=fetch)
    assert fetch.calls == [], "a release request went out with no durable last_check"
    assert result is None


def test_failed_last_check_write_makes_no_request_when_a_directory_holds_the_record_path(
    tmp_path,
):
    (tmp_path / "state.toml").mkdir()
    fetch = _fetcher(["v1.2.3"])
    result = _check(_FakeConfig(), tmp_path, fetch=fetch)
    assert fetch.calls == [], "a release request went out with no durable last_check"
    assert result is None


def test_failed_last_check_write_logs_exactly_one_line(tmp_path, caplog):
    fetch = _fetcher(["v1.2.3"])
    with patch.object(update_check, "save_state", return_value=False), caplog.at_level(
        logging.DEBUG, logger="update_check"
    ):
        _check(_FakeConfig(), tmp_path, fetch=fetch)
    assert len(caplog.records) == 1, "expected exactly one log line"
    assert caplog.records[0].levelno == logging.WARNING


def test_successful_last_check_write_still_makes_the_release_request(tmp_path):
    fetch = _fetcher(["v1.2.3"])
    assert _check(_FakeConfig(), tmp_path, fetch=fetch) == ("1.2.3", "v1.2.3")
    assert fetch.calls == ["1.0.0"]
    assert update_check.load_state(tmp_path / "state.toml").last_check == START


async def test_runner_waits_the_ordinary_24_hours_after_a_failed_last_check_write(
    tmp_path,
):
    fetch = _fetcher(["v1.2.3"])
    notify = MagicMock()
    with patch.object(update_check, "save_state", return_value=False):
        delays = await _run(_FakeConfig(), notify, tmp_path, fetch=fetch, cycles=2)
    assert delays == [60, DAY]
    assert fetch.calls == [], "a release request went out with no durable last_check"
    notify.assert_not_called()


def test_request_carries_no_config_value_or_identifier(tmp_path):
    config = _FakeConfig(SECRET_TOKEN="sentinel-token-123", user_name="sentinel-name")
    with patch.object(
        update_check.urllib.request,
        "urlopen",
        return_value=_FakeResponse(_release_body("v9.9.9")),
    ) as urlopen:
        update_check.check_for_update(
            config,
            state_path=tmp_path / "state.toml",
            get_version=lambda: "1.0.0",
            clock=_Clock(),
        )
    request = urlopen.call_args.args[0]
    seen = repr((request.full_url, request.header_items(), request.data))
    assert "sentinel" not in seen
    # Acceptance item 3: the request itself. No body, no query string, no
    # fragment, and User-Agent is the only header the module sets.
    assert request.data is None
    assert request.get_method() == "GET"
    assert request.full_url == update_check.RELEASES_URL
    parts = urllib.parse.urlsplit(request.full_url)
    assert parts.query == "" and parts.fragment == ""
    assert request.header_items() == [("User-agent", "Wheelhouse/1.0.0")]


async def _run(config, notify, tmp_path, *, fetch, cycles, clock=None, version="1.0.0"):
    """Run the loop for ``cycles`` sleeps; the fake sleep advances the clock."""
    clock = clock or _Clock()
    delays = []

    async def fake_sleep(delay):
        if len(delays) == cycles:
            raise asyncio.CancelledError
        delays.append(delay)
        clock.now += delay

    with pytest.raises(asyncio.CancelledError):
        await update_check.run_update_check(
            config,
            notify,
            state_path=tmp_path / "state.toml",
            get_version=lambda: version,
            fetch=fetch,
            clock=clock,
            sleep=fake_sleep,
        )
    return delays


async def test_runner_first_check_waits_60_seconds_then_every_24_hours(tmp_path):
    delays = await _run(
        _FakeConfig(), MagicMock(), tmp_path, fetch=_fetcher(["v1.0.0"]), cycles=3
    )
    assert delays == [60, DAY, DAY]


async def test_runner_after_a_recent_check_waits_for_the_24_hour_mark(tmp_path):
    update_check.save_state(
        tmp_path / "state.toml", update_check.UpdateCheckState(last_check=START - 3600)
    )
    delays = await _run(
        _FakeConfig(), MagicMock(), tmp_path, fetch=_fetcher(["v1.0.0"]), cycles=1
    )
    assert delays == [DAY - 3600]


def _recording_notify(tmp_path, clock, shown):
    """A notify fake that records the version as the Logic process does.

    The runner no longer writes the shown-notice record. The GUI process
    reports that the notice appeared, and the Logic process then calls
    ``record_notice_shown`` (wh-blocked-mic-message.1.2). This fake stands in
    for that report, delivered at once.
    """

    def notify(version, tag):
        shown.append((clock.now - START, version, tag))
        update_check.record_notice_shown(tmp_path / "state.toml", version, clock.now)

    return notify


async def test_runner_notice_appears_once_per_version_and_again_after_7_days(tmp_path):
    shown = []
    clock = _Clock()
    notify = _recording_notify(tmp_path, clock, shown)

    # 8 cycles: day 0 (60 s in) through day 7.
    await _run(
        _FakeConfig(), notify, tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=8, clock=clock
    )
    assert shown == [(60, "1.2.3", "v1.2.3"), (60 + 7 * DAY, "1.2.3", "v1.2.3")]


async def test_runner_newer_version_shows_at_the_next_check_inside_the_wait(tmp_path):
    shown = []
    clock = _Clock()
    recording = _recording_notify(tmp_path, clock, [])

    def notify(version, tag):
        shown.append(tag)
        recording(version, tag)

    await _run(
        _FakeConfig(),
        notify,
        tmp_path,
        fetch=_fetcher(["v1.2.3", "v1.2.3", "v1.3.0"]),
        cycles=5,
        clock=clock,
    )
    assert shown == ["v1.2.3", "v1.3.0"]


async def test_runner_leaves_the_notice_fields_alone_when_it_calls_notify(tmp_path):
    # The record of a shown notice is written when the GUI process reports
    # that the notice appeared. A notice that waits for a place on the screen
    # must not count as shown (wh-blocked-mic-message.1.2).
    await _run(_FakeConfig(), MagicMock(), tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=1)
    state = update_check.load_state(tmp_path / "state.toml")
    assert state.notified_version == "", (
        "the runner recorded a notice as shown before the GUI reported it: "
        f"{state.notified_version!r}"
    )
    assert state.notified_at == 0.0
    assert state.last_check == START + 60


async def test_runner_has_written_no_notice_record_when_notify_is_called(tmp_path):
    path = tmp_path / "state.toml"
    seen = []

    def notify(version, tag):
        seen.append(update_check.load_state(path))

    await _run(_FakeConfig(), notify, tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=1)
    assert len(seen) == 1
    assert seen[0].notified_version == "", (
        f"the record named {seen[0].notified_version!r} when notify was called"
    )
    assert seen[0].notified_at == 0.0


async def test_runner_offers_the_version_again_at_every_check_until_a_report_arrives(
    tmp_path,
):
    # A notice that never appeared on the screen leaves no record, so the next
    # check, at most CHECK_INTERVAL_SECONDS (24 hours) later, offers the same
    # version again (wh-blocked-mic-message.1.2).
    notify = MagicMock()
    await _run(_FakeConfig(), notify, tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=3)
    assert notify.call_count == 3, (
        f"notify was called {notify.call_count} times in 3 checks, not 3"
    )


async def test_runner_accepts_an_async_callback(tmp_path):
    shown = []

    async def notify(version, tag):
        shown.append(tag)

    await _run(_FakeConfig(), notify, tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=1)
    assert shown == ["v1.2.3"]


async def test_runner_survives_a_failing_callback_and_does_not_record_it(tmp_path):
    notify = MagicMock(side_effect=RuntimeError("gui gone"))
    await _run(_FakeConfig(), notify, tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=2)
    assert notify.call_count == 2  # tried again at the next check
    assert update_check.load_state(tmp_path / "state.toml").notified_version == ""


async def test_runner_survives_any_exception_from_the_check(tmp_path):
    def broken(version):
        raise RuntimeError("anything")

    notify = MagicMock()
    delays = await _run(_FakeConfig(), notify, tmp_path, fetch=broken, cycles=2)
    assert delays == [60, DAY]
    notify.assert_not_called()


async def test_runner_survives_an_unusable_record_file_path(tmp_path):
    (tmp_path / "state.toml").mkdir()  # a directory where the file should be
    delays = await _run(
        _FakeConfig(), MagicMock(), tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=2
    )
    assert delays == [60, DAY]


async def test_runner_key_off_makes_no_request_in_any_cycle(tmp_path):
    fetch = MagicMock(name="fetch")
    notify = MagicMock()
    with patch.object(update_check.urllib.request, "urlopen") as urlopen:
        await _run(
            _FakeConfig(CHECK_FOR_UPDATES=False), notify, tmp_path, fetch=fetch, cycles=3
        )
    fetch.assert_not_called()
    urlopen.assert_not_called()
    notify.assert_not_called()


async def test_runner_does_not_block_the_event_loop_during_the_request(tmp_path):
    release = threading.Event()
    in_fetch = threading.Event()

    def slow_fetch(version):
        in_fetch.set()
        release.wait(5)
        return "v1.0.0"

    sleeps = []

    async def fake_sleep(delay):
        if sleeps:
            raise asyncio.CancelledError
        sleeps.append(delay)

    task = asyncio.create_task(
        update_check.run_update_check(
            _FakeConfig(),
            MagicMock(),
            state_path=tmp_path / "state.toml",
            get_version=lambda: "1.0.0",
            fetch=slow_fetch,
            clock=_Clock(),
            sleep=fake_sleep,
        )
    )
    for _ in range(500):
        if in_fetch.is_set():
            break
        await asyncio.sleep(0.01)
    assert in_fetch.is_set()
    # The event loop runs other work while the request is pending.
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_runner_record_reads_and_writes_do_not_block_the_event_loop(tmp_path):
    # Every record read and write the runner owns blocks here for up to half
    # a second, or until an independent task on the same loop has run three
    # times. A call made ON the loop thread cannot see that task run.
    ticks = [0]
    outcomes = []

    async def ticker():
        while True:
            ticks[0] += 1
            await asyncio.sleep(0.005)

    def blocking(name, real):
        def wrapper(*args, **kwargs):
            start = ticks[0]
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and ticks[0] - start < 3:
                time.sleep(0.005)
            outcomes.append((name, ticks[0] - start >= 3))
            return real(*args, **kwargs)

        return wrapper

    notify = MagicMock()
    ticker_task = asyncio.create_task(ticker())
    try:
        with (
            patch.object(
                update_check, "load_state", blocking("load", update_check.load_state)
            ),
            patch.object(
                update_check, "save_state", blocking("save", update_check.save_state)
            ),
        ):
            await _run(
                _FakeConfig(), notify, tmp_path, fetch=_fetcher(["v1.2.3"]), cycles=1
            )
    finally:
        ticker_task.cancel()
    notify.assert_called_once()
    stalled = [name for name, progressed in outcomes if not progressed]
    assert not stalled, (
        f"record I/O blocked the event loop: {len(stalled)} of "
        f"{len(outcomes)} record calls ran with the loop stalled"
    )
    # first-delay load, then the check's own load, save and load.
    assert len(outcomes) >= 4
