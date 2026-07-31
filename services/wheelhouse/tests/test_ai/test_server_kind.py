# test_server_kind.py - Reading [ai.server] kind (wh-ai-kind-validation)
#
# Four places compared this setting against a literal, and they did not all
# compare against the same one. Two asked "is it cloud?" and two asked "is it
# local?", so a value that is neither -- a typo like "remote" or "Cloud" --
# took the local path in one place and the cloud path in another. The result
# was not a wrong setting but an incoherent one: the model-list refresh ran
# while the loop that drives it never started.
#
# These tests are about the reading, not about which behaviour is better. The
# contract they hold is that every site gets the SAME answer, that an
# unreadable value announces itself instead of being absorbed, and that the
# announcement is a returned complaint rather than a log call, so a caller can
# decide when to say it.

import asyncio
import sys
from pathlib import Path
from queue import Queue
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from ai.server_kind import CLOUD, LOCAL, normalize_server_kind
from ai.service import AIService


class TestTheTwoValuesThatAreSpelledRight:

    def test_local_is_read_as_local_with_nothing_to_complain_about(self):
        assert normalize_server_kind("local") == (LOCAL, None)

    def test_cloud_is_read_as_cloud_with_nothing_to_complain_about(self):
        assert normalize_server_kind("cloud") == (CLOUD, None)


class TestTheKindnessesForAHandEditedFile:
    """config.toml is edited by hand. Capitalisation and a stray space are not
    the user getting it wrong; the meaning is not in doubt."""

    @pytest.mark.parametrize("raw", ["Cloud", "CLOUD", " cloud", "cloud ", "\tcloud\n"])
    def test_a_recognisable_cloud_is_accepted_quietly(self, raw):
        kind, complaint = normalize_server_kind(raw)
        assert kind == CLOUD
        assert complaint is None, "a spelling we understood is not worth a warning"

    @pytest.mark.parametrize("raw", ["Local", "LOCAL", " local ", "\nlocal"])
    def test_a_recognisable_local_is_accepted_quietly(self, raw):
        kind, complaint = normalize_server_kind(raw)
        assert kind == LOCAL
        assert complaint is None


class TestAValueThatIsNeither:
    """The bug this bead is about: a value that is neither was absorbed."""

    @pytest.mark.parametrize("raw", ["remote", "clould", "locl", "hosted", "gemini", "", "  "])
    def test_it_is_not_absorbed_silently(self, raw):
        kind, complaint = normalize_server_kind(raw)
        assert complaint, f"{raw!r} must produce something to say"

    @pytest.mark.parametrize("raw", ["remote", "clould", "locl", "hosted", "", "  "])
    def test_it_falls_back_to_local(self, raw):
        """The same answer a missing key gives. A key that cannot be read and a
        key that is not there should not behave differently -- that difference
        is the thing that made the original bug hard to see."""
        kind, _ = normalize_server_kind(raw)
        assert kind == LOCAL

    def test_the_complaint_quotes_the_value_and_names_both_spellings(self):
        """The user has to be able to find what they typed and read what they
        should have typed. A complaint that says only "invalid" sends them
        back to the documentation."""
        _, complaint = normalize_server_kind("remote")
        assert complaint is not None
        assert "remote" in complaint
        assert "local" in complaint
        assert "cloud" in complaint

    def test_the_complaint_says_which_setting_it_is_about(self):
        """It is logged next to unrelated startup lines."""
        _, complaint = normalize_server_kind("remote")
        assert complaint is not None
        assert "kind" in complaint


class TestAValueThatIsNotText:
    """ConfigService hands back whatever TOML parsed. kind = true is a boolean,
    kind = 3 an integer, and a missing key can arrive as None."""

    @pytest.mark.parametrize("raw", [None, True, False, 3, 0, 1.5, [], ["local"], {}])
    def test_it_falls_back_to_local_without_raising(self, raw):
        kind, _ = normalize_server_kind(raw)
        assert kind == LOCAL

    @pytest.mark.parametrize("raw", [True, 3, ["local"], {}])
    def test_a_value_of_the_wrong_type_is_complained_about(self, raw):
        _, complaint = normalize_server_kind(raw)
        assert complaint

    def test_a_missing_key_is_not_complained_about(self):
        """None is what a caller passes when the key is absent. That is the
        documented default, not a mistake, and warning about it would put a
        line in every log of every default installation."""
        assert normalize_server_kind(None) == (LOCAL, None)


class TestTheShapeOfTheAnswer:

    def test_it_always_returns_one_of_the_two_names(self):
        for raw in ["local", "cloud", "remote", None, True, 7, "", "CLOUD"]:
            kind, _ = normalize_server_kind(raw)
            assert kind in (LOCAL, CLOUD), raw

    def test_the_names_are_the_words_that_go_in_the_file(self):
        """Callers write these into config.toml and compare against them. If
        they were internal symbols the comparison sites would drift back to
        string literals, which is what caused the bug."""
        assert LOCAL == "local"
        assert CLOUD == "cloud"

    def test_it_does_not_log(self, caplog):
        """The complaint is returned, not logged. Only the caller knows whether
        this is the first read of the setting or the four-hundredth -- a log
        call in here would repeat the same line on every menu rebuild."""
        with caplog.at_level(0):
            normalize_server_kind("remote")
        assert not caplog.records


# ---------------------------------------------------------------------------
# The four sites that read the setting (the bug wh-ai-kind-validation is about)
# ---------------------------------------------------------------------------

#: Passed as the kind to build a config in which the key is absent entirely.
#: Distinct from None, which is a key that is present and holds no value.
ABSENT = object()


def _config(kind, base_url="http://localhost:8781/v1", model="qwen3.5:9b"):
    """A ConfigService mock carrying an [ai.server] block with this kind."""
    values = {
        "ai.enabled": True,
        "ai.knowledge_base": "knowledge/wheelhouse_help.md",
        "ai.server.enabled": True,
        "ai.server.base_url": base_url,
        "ai.server.model": model,
        "ai.server.api_key": "",
        "ai.server.timeout_s": 30,
        "ai.server.kind": kind,
        "stt.mode": "remote",
        "stt.last_provider": "google_stt",
        "FLOATING_BUTTON_VISIBLE": True,
        "FLOATING_BUTTON_SIZE": 50,
        "FLOATING_BUTTON_POS": [100, 100],
        "SHOW_SPEECH_PULSE": True,
    }
    if kind is ABSENT:
        # A config.toml with no kind line at all. The reading site's own
        # default is what answers, which is the thing a call site can get
        # wrong without any test noticing -- values.get returns the caller's
        # default only for a key that is not in the table.
        del values["ai.server.kind"]
    config = MagicMock()
    config.get = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    return config


def _menu_for(loop, kind):
    """What the tray menu offers for this kind: the fourth reading site."""
    from state_manager import StateManager

    ai = MagicMock()
    ai._model_name = "qwen3.5:9b"
    ai.cached_models = MagicMock(return_value=["live-a"])
    sm = StateManager(
        config_service=_config(kind),
        event_bus=MagicMock(),
        loop=loop,
        state_to_gui_queue=Queue(),
        websocket_manager=None,
    )
    sm.set_ai_service(ai)
    sm.send_state_update()
    return sm.state_to_gui_queue.get_nowait()["ai_providers_available"]


@pytest.fixture
def sm_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


class TestEverySiteAgrees:
    """The defect: four sites compared against a literal, two against "cloud"
    and two against "local". A value that was neither took the local path at
    two of them and the cloud path at the other two, so the model-list refresh
    ran while the loop meant to drive it never started, and the menu showed the
    cloud shape for a server being built as a local one. Nothing said so.

    Each test below reads one machine's worth of behaviour at every site and
    asserts they tell the same story."""

    def _sites(self, kind):
        """What each of the three AIService sites decides for this kind.

        Every value here comes from RUNNING the site. Restating its condition
        -- `service._server_kind() == LOCAL` in place of launching the probes
        and looking for the loop -- is a test of the reader that only looks
        like a test of the site: the author-time mutation gate put the old
        bare literal back at two of these sites and the earlier version of
        this method stayed green, because it never called them.
        """
        service = AIService(_config(kind))
        provider = service._build_server_provider()

        # Site 2: does refresh_models reach the server, or return early?
        transport = MagicMock()
        transport.list_models = AsyncMock(return_value=["live-a"])
        transport.is_available = AsyncMock(return_value=True)
        service._provider = transport
        asyncio.run(service.refresh_models())

        # Site 3: is the model-list refresh loop among the launched tasks?
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            service._provider = transport
            service._launch_background_probes()
            launched = list(service._bg_tasks)
            names = {t.get_coro().__qualname__ for t in launched}
            for task in launched:
                task.cancel()
            # Run the loop once so the cancelled tasks actually unwind. Without
            # this the coroutines are collected having never been awaited, and
            # the run ends in RuntimeWarnings that would bury a real one.
            loop.run_until_complete(asyncio.gather(*launched, return_exceptions=True))
            service._bg_tasks = []
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        return {
            "provider is built as cloud": provider._is_cloud,
            "refresh_models reached the server": transport.list_models.called,
            "refresh loop is started": any("_refresh_loop" in n for n in names),
        }

    def test_a_value_that_is_neither_reads_as_local_everywhere(self, sm_loop):
        sites = self._sites("remote")
        assert sites["provider is built as cloud"] is False
        assert sites["refresh_models reached the server"] is True
        assert sites["refresh loop is started"] is True, (
            "the loop that does the refreshing must start wherever the "
            "refresh itself is allowed to run"
        )
        assert "live-a" in _menu_for(sm_loop, "remote"), (
            "the menu must show the live list it would show for local"
        )

    def test_a_value_that_is_neither_reads_the_same_as_a_missing_key(self, sm_loop):
        assert self._sites("remote") == self._sites(None)
        assert _menu_for(sm_loop, "remote") == _menu_for(sm_loop, None)

    def test_a_key_that_is_not_in_the_file_at_all_reads_as_local(self, sm_loop):
        """Distinct from a key holding None. Each site passes its own default
        into the reader, and a site that passes "cloud" there sends a default
        installation down the cloud path -- which no test that always writes
        the key can see."""
        assert self._sites(ABSENT) == self._sites("local")
        assert _menu_for(sm_loop, ABSENT) == _menu_for(sm_loop, "local")

    def test_cloud_still_reads_as_cloud_everywhere(self, sm_loop):
        sites = self._sites("cloud")
        assert sites["provider is built as cloud"] is True
        assert sites["refresh_models reached the server"] is False
        assert sites["refresh loop is started"] is False
        assert _menu_for(sm_loop, "cloud") == ["qwen3.5:9b"], (
            "cloud shows the configured model only, not the live list"
        )

    def test_a_capitalised_cloud_reads_as_cloud_everywhere(self, sm_loop):
        """The reading is shared, so the forgiveness has to be too. Before,
        "Cloud" would have been cloud at no site at all."""
        assert self._sites("Cloud") == self._sites("cloud")
        assert _menu_for(sm_loop, "Cloud") == _menu_for(sm_loop, "cloud")


class TestTheComplaintReachesTheLog:

    def test_the_service_says_it_once_however_many_times_it_reads(self, caplog):
        """_server_kind() is called on every probe launch and every refresh
        tick. Without the latch the same line would print all day."""
        service = AIService(_config("remote"))
        with caplog.at_level("WARNING"):
            for _ in range(5):
                service._server_kind()
        said = [r for r in caplog.records if "kind" in r.getMessage()]
        assert len(said) == 1, f"expected one complaint, got {len(said)}"
        assert "remote" in said[0].getMessage()

    def test_a_readable_value_says_nothing(self, caplog):
        service = AIService(_config("cloud"))
        with caplog.at_level("WARNING"):
            service._server_kind()
        assert not [r for r in caplog.records if "kind" in r.getMessage()]

    def test_the_menu_does_not_repeat_the_complaint(self, sm_loop, caplog):
        """The tray menu is rebuilt on every state update. It reads the same
        setting and deliberately drops the complaint -- AIService owns saying
        it."""
        with caplog.at_level("WARNING"):
            for _ in range(3):
                _menu_for(sm_loop, "remote")
        assert not [r for r in caplog.records if "[ai.server] kind" in r.getMessage()]
