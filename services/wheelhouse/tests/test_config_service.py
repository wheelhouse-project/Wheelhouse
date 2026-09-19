"""Tests for ConfigService - TOML configuration management.

Covers:
- Loading from a TOML file
- Dot-notation access (get)
- Default value when key missing
- set() updates in-memory config (flat and nested)
- save() writes to disk
- Error handling for missing/invalid TOML files
"""

import asyncio
import builtins
import logging
from pathlib import Path

import pytest
import pytest_asyncio

from config_service import ConfigService


class TestStagedSettingsSave:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('succeeds', [True, False])
    async def test_staged_cancel_all_owns_filesystem_until_publication(self, service, monkeypatch, succeeds):
        """Cancel outer and its writer during replace; a second save must wait."""
        import threading
        import tomllib
        import config_service
        loop = asyncio.get_running_loop()
        started, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        real_replace = config_service.os.replace
        replacements = []
        def held_replace(*args):
            replacements.append(service._loaded['size'])
            if len(replacements) == 1:
                loop.call_soon_threadsafe(started.set)
                try:
                    if not release.wait(10):
                        raise TimeoutError('test barrier not released')
                    if not succeeds:
                        raise OSError('disk full')
                    return real_replace(*args)
                finally:
                    loop.call_soon_threadsafe(finished.set)
            return real_replace(*args)
        monkeypatch.setattr(config_service.os, 'replace', held_replace)
        prior_tasks = asyncio.all_tasks()
        outer = asyncio.create_task(service.save(values={'size': 80}))
        competitor = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            for task in asyncio.all_tasks() - prior_tasks:
                task.cancel()
            # Scheduling barrier: cancellation callbacks run before this probe.
            probe = loop.create_future()
            loop.call_soon(lambda: loop.call_soon(probe.set_result, None))
            await probe
            early = outer.done()
            held = service._save_lock.locked()
            competitor_started = asyncio.Event()
            async def competing_save():
                competitor_started.set()
                return await service.save(values={'position': [20, 30]})
            competitor = asyncio.create_task(competing_save())
            await competitor_started.wait()
            assert not early, 'cancelled save escaped before filesystem completion'
            assert held, 'save lock released before filesystem completion'
            assert not competitor.done()
            assert service.get('size') == 50
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            with pytest.raises(asyncio.CancelledError):
                await outer
            if competitor is not None:
                assert await competitor
        expected = 80 if succeeds else 50
        assert replacements == [50, expected], 'competing save ran before publication'
        assert service.get('size') == expected
        on_disk = tomllib.loads(Path(service.config_path).read_text())
        assert on_disk['size'] == expected
        assert on_disk['position'] == [20, 30]

    @pytest.fixture
    def service(self, tmp_path):
        path = tmp_path / 'staged.toml'
        path.write_text('size = 50\nposition = [100, 100]\n[panel]\nsize = 50\ncolor = "blue"\nobsolete = true\n')
        return ConfigService(str(path))

    @pytest_asyncio.fixture
    async def held_write(self, monkeypatch):
        import threading
        import config_service
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        import tempfile
        real_mkstemp = tempfile.mkstemp
        def hold(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(10):
                raise TimeoutError('test barrier not released')
            return real_mkstemp(*args, **kwargs)
        monkeypatch.setattr(tempfile, 'mkstemp', hold)
        yield started, release
        release.set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('succeeds', [True, False])
    async def test_staged_settings_stay_private_until_disk_success(self, service, held_write, monkeypatch, succeeds):
        import tomllib
        import tomli_w
        started, release = held_write
        if not succeeds:
            def fail(*args):
                raise OSError('disk full')
            monkeypatch.setattr(tomli_w, 'dump', fail)
        task = asyncio.create_task(service.save(values={'size': 80, 'position': [85, 85]}))
        await started.wait()
        before_size = service.get('size')
        before_position = list(service.get('position'))
        release.set()
        assert await task is succeeds
        assert before_size == 50
        assert before_position == [100, 100]
        assert service.get('size') == (80 if succeeds else 50)
        assert tomllib.loads(Path(service.config_path).read_text())['size'] == (80 if succeeds else 50)

    @pytest.mark.asyncio
    async def test_staged_settings_copy_caller_values(self, service, held_write):
        started, release = held_write
        values = {'position': [85, 85]}
        task = asyncio.create_task(service.save(values=values))
        await started.wait()
        values['position'][0] = 999
        release.set()
        assert await task
        assert service.get('position') == [85, 85]

    @pytest.mark.asyncio
    async def test_staged_settings_keep_newer_live_edit_for_next_save(self, service, held_write):
        import tomllib
        started, release = held_write
        task = asyncio.create_task(service.save(values={'size': 80}))
        await started.wait()
        service.set('size', 90)
        release.set()
        assert await task
        assert service.get('size') == 90
        assert tomllib.loads(Path(service.config_path).read_text())['size'] == 80
        assert await service.save()
        assert tomllib.loads(Path(service.config_path).read_text())['size'] == 90

    @pytest.mark.asyncio
    async def test_staged_settings_nested_edit_preserves_sibling_and_identity(self, service, held_write):
        started, release = held_write
        held_panel = service.get('panel')
        task = asyncio.create_task(service.save(values={'panel.size': 80}))
        await started.wait()
        service.set('panel.color', 'red')
        release.set()
        assert await task
        assert service.get('panel') is held_panel
        assert service.get('panel.size') == 80
        assert service.get('panel.color') == 'red'

    @pytest.mark.asyncio
    async def test_staged_settings_do_not_pollute_newer_replaced_table(self, service, held_write):
        started, release = held_write
        task = asyncio.create_task(service.save(values={'panel.size': 80}))
        await started.wait()
        service.set('panel', {'replacement': True})
        release.set()
        assert await task
        assert service.get('panel') == {'replacement': True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize('succeeds', [True, False])
    async def test_staged_settings_table_removals_and_hand_edits(self, service, monkeypatch, succeeds):
        import tomllib
        import tomli_w
        path = Path(service.config_path)
        path.write_text(path.read_text() + 'hand_added = "keep"\n')
        if not succeeds:
            def fail(*args):
                raise OSError('disk full')
            monkeypatch.setattr(tomli_w, 'dump', fail)
        assert await service.save(values={'panel': {'size': 80}}) is succeeds
        if succeeds:
            assert service.get('panel') == {'size': 80, 'hand_added': 'keep'}
        else:
            assert service.get('panel') == {'size': 50, 'color': 'blue', 'obsolete': True}
            assert service._removed == set()
        assert tomllib.loads(path.read_text())['panel']['hand_added'] == 'keep'

    @pytest.mark.asyncio
    async def test_staged_settings_cancel_waits_for_disk_outcome(self, service, held_write):
        import tomllib
        started, release = held_write
        task = asyncio.create_task(service.save(values={'size': 80}))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        finished_early = task.done()
        pending_size = service.get('size')
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished_early
        assert pending_size == 50
        assert service.get('size') == 80
        assert tomllib.loads(Path(service.config_path).read_text())['size'] == 80


@pytest.fixture
def config_toml(tmp_path):
    """Create a minimal config.toml for testing."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'SPEECH_ENABLED_ON_STARTUP = true\n'
        'LOG_LEVEL = "INFO"\n'
        '\n'
        '[speech]\n'
        'timeout_ms = 700\n'
        'model = "default"\n'
        '\n'
        '[plugins.bravia]\n'
        'device_name = "Living Room TV"\n'
        'ip = "192.168.1.100"\n'
    )
    return config_file


@pytest.fixture
def config_svc(config_toml):
    """ConfigService loaded from test config file."""
    return ConfigService(config_path=str(config_toml))


# -----------------------------------------------------------------------
# Loading
# -----------------------------------------------------------------------

class TestLoading:

    def test_loads_from_toml_file(self, config_svc):
        assert config_svc.get("LOG_LEVEL") == "INFO"

    def test_loads_nested_values(self, config_svc):
        assert config_svc.get("speech.timeout_ms") == 700

    def test_get_config_returns_full_dict(self, config_svc):
        full = config_svc.get_config()
        assert isinstance(full, dict)
        assert "speech" in full

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ConfigService(config_path=str(tmp_path / "nonexistent.toml"))

    def test_invalid_toml_raises_value_error(self, tmp_path):
        bad_file = tmp_path / "bad.toml"
        bad_file.write_text("this is not [ valid toml {{{{")
        with pytest.raises(ValueError, match="Invalid TOML"):
            ConfigService(config_path=str(bad_file))


# -----------------------------------------------------------------------
# get() with dot notation
# -----------------------------------------------------------------------

class TestGet:

    def test_simple_key(self, config_svc):
        assert config_svc.get("SPEECH_ENABLED_ON_STARTUP") is True

    def test_nested_key_dot_notation(self, config_svc):
        assert config_svc.get("speech.model") == "default"

    def test_deeply_nested_key(self, config_svc):
        assert config_svc.get("plugins.bravia.device_name") == "Living Room TV"

    def test_missing_key_returns_default(self, config_svc):
        assert config_svc.get("nonexistent", "fallback") == "fallback"

    def test_missing_key_returns_none_by_default(self, config_svc):
        assert config_svc.get("nonexistent") is None

    def test_missing_nested_key_returns_default(self, config_svc):
        assert config_svc.get("speech.nonexistent", 42) == 42

    def test_partial_nested_path_returns_default(self, config_svc):
        assert config_svc.get("nonexistent.deep.path", "nope") == "nope"

    def test_intermediate_non_dict_returns_default(self, config_svc):
        # speech.timeout_ms is an int, not a dict - accessing deeper should return default
        assert config_svc.get("speech.timeout_ms.deeper", "default") == "default"


# -----------------------------------------------------------------------
# set()
# -----------------------------------------------------------------------

class TestSet:

    def test_set_simple_key(self, config_svc):
        config_svc.set("NEW_KEY", "new_value")
        assert config_svc.get("NEW_KEY") == "new_value"

    def test_set_overwrites_existing(self, config_svc):
        config_svc.set("LOG_LEVEL", "DEBUG")
        assert config_svc.get("LOG_LEVEL") == "DEBUG"

    def test_set_nested_key(self, config_svc):
        config_svc.set("speech.timeout_ms", 500)
        assert config_svc.get("speech.timeout_ms") == 500

    def test_set_creates_nested_structure(self, config_svc):
        config_svc.set("new_section.subsection.key", "value")
        assert config_svc.get("new_section.subsection.key") == "value"

    def test_set_deeply_nested(self, config_svc):
        config_svc.set("plugins.bravia.ip", "10.0.0.1")
        assert config_svc.get("plugins.bravia.ip") == "10.0.0.1"
        # Ensure other keys in same section are preserved
        assert config_svc.get("plugins.bravia.device_name") == "Living Room TV"


class TestUnset:
    """Taking a setting back out, for a change that has to be undone.

    Putting None back in its place is not the same thing. A read would treat
    it as missing, so it looks right, but the next write has to turn every
    setting into a line of the settings file and there is no way to write
    None -- so that write fails, and every later one with it.
    """

    def test_unset_removes_a_simple_key(self, config_svc):
        config_svc.unset("LOG_LEVEL")
        assert config_svc.get("LOG_LEVEL", "absent") == "absent"
        assert "LOG_LEVEL" not in config_svc.get_config()

    def test_unset_removes_a_nested_key(self, config_svc):
        config_svc.unset("speech.timeout_ms")
        assert "timeout_ms" not in config_svc.get_config()["speech"]

    def test_unset_leaves_its_neighbours_alone(self, config_svc):
        config_svc.unset("speech.timeout_ms")
        assert config_svc.get("speech.model") == "default"

    def test_unset_of_a_key_that_is_not_there_does_nothing(self, config_svc):
        config_svc.unset("speech.never_set")
        config_svc.unset("no_such_section.key")
        assert config_svc.get("speech.model") == "default"

    @pytest.mark.asyncio
    async def test_a_write_after_unset_still_succeeds(self, config_svc):
        """The reason unset exists rather than setting None."""
        config_svc.unset("LOG_LEVEL")
        assert await config_svc.save() is True


# -----------------------------------------------------------------------
# save()
# -----------------------------------------------------------------------

class TestSave:

    @pytest.mark.asyncio
    async def test_save_persists_to_disk(self, config_svc, config_toml):
        config_svc.set("LOG_LEVEL", "DEBUG")
        await config_svc.save()

        # Reload from same file
        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("LOG_LEVEL") == "DEBUG"

    @pytest.mark.asyncio
    async def test_save_preserves_nested_structure(self, config_svc, config_toml):
        config_svc.set("speech.timeout_ms", 999)
        await config_svc.save()

        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("speech.timeout_ms") == 999
        assert reloaded.get("speech.model") == "default"

    @pytest.mark.asyncio
    async def test_save_new_keys_persist(self, config_svc, config_toml):
        config_svc.set("brand_new", "value")
        await config_svc.save()

        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("brand_new") == "value"


# -----------------------------------------------------------------------
# Class variable isolation
# -----------------------------------------------------------------------

class TestDefaultPath:

    def test_default_path_resolves_to_config_toml(self, monkeypatch, tmp_path):
        """When no config_path given, defaults to config.toml next to the module."""
        # Create config.toml in the config_service.py directory
        import config_service as cs_mod
        module_dir = Path(cs_mod.__file__).parent
        config_file = module_dir / "config.toml"

        # Only run if the real config.toml exists (it should in the wheelhouse dir)
        if config_file.exists():
            svc = ConfigService()
            assert svc.config_path == str(config_file)
        else:
            pytest.skip("No config.toml in wheelhouse directory")


class TestSaveErrorHandling:

    @pytest.mark.asyncio
    async def test_save_handles_write_error(self, config_svc):
        """Save logs error but doesn't raise on write failure."""
        # Point to a path that can't be written
        config_svc.config_path = "/nonexistent/dir/config.toml"
        # Should not raise
        await config_svc.save()


class TestIsolation:

    def test_instances_share_class_config(self):
        """ConfigService uses a class variable _config - verify behavior.

        This documents the current behavior rather than prescribing it.
        Two instances pointing at different files will overwrite each other's
        _config since it's a class variable. Tests use fresh fixtures so this
        doesn't cause issues in practice.
        """
        # Just verify _config exists as a class attribute
        assert hasattr(ConfigService, "_config")


# -----------------------------------------------------------------------
# Adversarial: empty string key
# -----------------------------------------------------------------------

class TestEmptyKey:

    def test_get_empty_string_key_returns_default(self, config_svc):
        """get('') falls through to dict.get('', default) since '' has no dot.
        No config entry has an empty-string key, so default is returned."""
        assert config_svc.get("") is None
        assert config_svc.get("", "fallback") == "fallback"

    def test_set_empty_string_key_stores_value(self, config_svc):
        """set('') stores a value under the empty-string key in the dict.
        This is technically valid Python dict behavior, even if no real
        caller would do it."""
        config_svc.set("", "empty_key_value")
        assert config_svc.get("") == "empty_key_value"

    def test_set_empty_string_key_does_not_corrupt_existing(self, config_svc):
        """Setting an empty-string key should not affect other config values."""
        original_log_level = config_svc.get("LOG_LEVEL")
        config_svc.set("", "empty_key_value")
        assert config_svc.get("LOG_LEVEL") == original_log_level


# -----------------------------------------------------------------------
# Adversarial: concurrent async get/set
# -----------------------------------------------------------------------

class TestSequentialSetAndSave:

    @pytest.mark.asyncio
    async def test_interleaved_set_and_save(self, config_svc, config_toml):
        """Multiple save() calls interleaved with set() should not lose data.
        Since save() runs file I/O in a thread, verify that the final state
        on disk reflects all set() calls made before the last save()."""
        config_svc.set("LOG_LEVEL", "DEBUG")
        await config_svc.save()

        config_svc.set("LOG_LEVEL", "WARNING")
        await config_svc.save()

        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("LOG_LEVEL") == "WARNING"


# -----------------------------------------------------------------------
# wh-config-save-clobbers-hand-edits: a save must not discard an edit made
# to the file by hand while the program is running
# -----------------------------------------------------------------------
#
# ConfigService.save used to write the whole file from its in-memory copy
# without ever re-reading the file, so any hand edit made after startup was
# replaced the next time anything saved. Saves are easy to trigger without
# meaning to: moving, resizing or hiding the floating button, and switching
# the microphone interaction mode, each write the whole file.
#
# David's ruling (QUESTIONS-2026-09-02 item 48, option 1): re-read the file
# immediately before each save and merge. A key the running program has not
# changed keeps whatever is on disk; a key the program did change takes the
# in-memory value.
#
# Every test below writes only to a pytest tmp_path file. None of them reads
# or writes any real config.toml.


ORIGINAL_FILE = (
    'LOG_LEVEL = "INFO"\n'
    "\n"
    "[ai.help]\n"
    'gem_url = ""\n'
    "\n"
    "[speech]\n"
    'model = "default"\n'
)

HAND_EDITED_URL = "https://example.invalid/assistant"
LOADED_LEVEL = "a-value-nobody-should-read-in-a-log"


@pytest.fixture
def merge_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL_FILE)
    return path


def _hand_edit(path, text):
    """Stand in for the user editing the file while the program runs."""
    path.write_text(text)


class TestSaveMergesHandEdits:

    @pytest.mark.asyncio
    async def test_a_hand_edited_key_survives_a_save_of_another_key(
        self, merge_file
    ):
        """Criterion 2(a): the case recorded on the bead.

        Loaded with an empty ai.help.gem_url, the URL written by hand, then
        an unrelated key saved. The URL must still be there.
        """
        svc = ConfigService(config_path=str(merge_file))
        assert svc.get("ai.help.gem_url") == ""

        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{HAND_EDITED_URL}"'),
        )
        svc.set("speech.model", "from_the_program")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("ai.help.gem_url") == HAND_EDITED_URL
        assert reloaded.get("speech.model") == "from_the_program"

    @pytest.mark.asyncio
    async def test_the_program_wins_when_both_sides_changed_one_key(
        self, merge_file
    ):
        """Criterion 2(b)."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('model = "default"', 'model = "from_the_hand"'),
        )
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.model") == "from_the_program"

    @pytest.mark.asyncio
    async def test_a_table_added_by_hand_survives(self, merge_file):
        """Criterion 2(c): a table the program never loaded."""
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file,
            ORIGINAL_FILE + '\n[plugins.bravia]\ndevice_name = "Living Room TV"\n',
        )
        svc.set("LOG_LEVEL", "DEBUG")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("plugins.bravia.device_name") == "Living Room TV"
        assert reloaded.get("LOG_LEVEL") == "DEBUG"

    @pytest.mark.asyncio
    async def test_a_key_added_by_hand_to_a_loaded_table_survives(
        self, merge_file
    ):
        """Criterion 2(c), the other half: a new key inside a table the
        program did load."""
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(merge_file, ORIGINAL_FILE + "timeout_ms = 700\n")
        svc.set("LOG_LEVEL", "DEBUG")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.timeout_ms") == 700

    @pytest.mark.asyncio
    async def test_a_line_deleted_by_hand_stays_deleted(self, merge_file):
        """Not named in the acceptance criteria; decided while building.

        Deleting a line is a hand edit as much as changing one, and this bead
        exists because hand edits were discarded. The rule is the same rule:
        the program did not touch this key, so the file decides. A key the
        program HAS changed is unaffected, because the program wins there.
        """
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(merge_file, ORIGINAL_FILE.replace('LOG_LEVEL = "INFO"\n', ""))
        svc.set("speech.model", "from_the_program")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None
        assert reloaded.get("speech.model") == "from_the_program"

    @pytest.mark.asyncio
    async def test_a_line_deleted_by_hand_loses_to_a_program_change(
        self, merge_file
    ):
        """The other side of the same rule, so the deletion case cannot be
        read as "the file always wins"."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("LOG_LEVEL", "DEBUG")
        _hand_edit(merge_file, ORIGINAL_FILE.replace('LOG_LEVEL = "INFO"\n', ""))
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "DEBUG"

    @pytest.mark.asyncio
    async def test_an_unparsable_file_saves_from_memory_and_warns(
        self, merge_file, caplog
    ):
        """Criterion 2(d): no crash, the save still happens, and the WARNING
        names the file that would not parse."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(merge_file, "this is not [ valid toml {{{{")

        with caplog.at_level(logging.WARNING, logger="config_service"):
            assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.model") == "from_the_program"
        assert reloaded.get("LOG_LEVEL") == "INFO"

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert warnings, "an unreadable settings file must be reported"
        assert any(str(merge_file) in message for message in warnings)

    @pytest.mark.asyncio
    async def test_a_file_that_cannot_be_opened_saves_from_memory_and_warns(
        self, merge_file, caplog, monkeypatch
    ):
        """The other unreadable case: the file parses fine, when it opens."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")

        real_open = builtins.open

        def refusing_open(file, *args, **kwargs):
            if str(file) == str(merge_file) and args and "r" in str(args[0]):
                raise OSError(13, "the file is in use")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", refusing_open)

        with caplog.at_level(logging.WARNING, logger="config_service"):
            assert await svc.save() is True

        monkeypatch.setattr(builtins, "open", real_open)
        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.model") == "from_the_program"
        assert reloaded.get("LOG_LEVEL") == "INFO"

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.name == "config_service"
        ]
        assert warnings, "a settings file that will not open must be reported"
        assert any(str(merge_file) in message for message in warnings)

    @pytest.mark.asyncio
    async def test_a_missing_file_saves_from_memory_without_a_warning(
        self, merge_file, caplog
    ):
        """A file that is not there yet is the ordinary first save, not a
        fault, so it says nothing."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        merge_file.unlink()

        with caplog.at_level(logging.WARNING, logger="config_service"):
            assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.model") == "from_the_program"

        assert not [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.name == "config_service"
        ], "a settings file that is simply not there is not a fault"

    @pytest.mark.asyncio
    async def test_a_file_that_is_not_utf8_saves_from_memory_and_warns(
        self, merge_file, caplog
    ):
        """Review finding .1.7. TOML is UTF-8 by definition, and tomllib
        decodes the bytes before it parses them, so a file an editor saved
        as UTF-16 raises a decoding error rather than a parse error. That
        error escaped the save, which then raised instead of returning its
        answer and never wrote the warning naming the file.
        """
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        # A UTF-16 byte-order mark followed by UTF-16 text. Byte 0xFF is not
        # valid UTF-8 in any position.
        merge_file.write_bytes(b"\xff\xfeL\x00O\x00G\x00")

        with caplog.at_level(logging.WARNING, logger="config_service"):
            try:
                saved = await svc.save()
            except Exception as raised:
                pytest.fail(
                    "the save raised instead of answering: "
                    f"{type(raised).__name__}: {raised}"
                )
        assert saved is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.model") == "from_the_program"
        assert reloaded.get("LOG_LEVEL") == "INFO"

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.name == "config_service"
        ]
        assert warnings, "a settings file that will not decode must be reported"
        assert any(str(merge_file) in message for message in warnings)

    @pytest.mark.asyncio
    async def test_a_hand_edit_survives_a_second_save(self, merge_file):
        """The snapshot must be refreshed to the MERGED result, not to what
        the program held in memory.

        If a save refreshes the snapshot from memory alone, the second save
        reads the kept on-disk value as a deliberate change back to the old
        one and writes the hand edit away. One save is not enough to prove
        the fix; the settings window saves on every button move.
        """
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{HAND_EDITED_URL}"'),
        )
        svc.set("speech.model", "first")
        assert await svc.save() is True

        svc.set("speech.model", "second")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("ai.help.gem_url") == HAND_EDITED_URL
        assert reloaded.get("speech.model") == "second"

    @pytest.mark.asyncio
    async def test_a_second_hand_edit_also_survives(self, merge_file):
        """What the program records after a save must be what reached the
        FILE, not what it was holding in memory.

        Recording the in-memory copy looks harmless for one edit, because the
        save also brings memory up to the kept value. It only shows on the
        NEXT hand edit: memory now holds the first kept value while the record
        still holds the value from before it, so the comparison reads memory
        as a deliberate change and the second hand edit is written away. The
        mutation gate found this; one hand edit could not see it.
        """
        first_url = "https://example.invalid/first"
        second_url = "https://example.invalid/second"

        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file, ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{first_url}"')
        )
        svc.set("speech.model", "first")
        assert await svc.save() is True

        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{second_url}"'),
        )
        svc.set("speech.model", "second")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("ai.help.gem_url") == second_url
        assert reloaded.get("speech.model") == "second"


KEPT_LINE_PHRASE = "kept the value already in the file for"


class TestSaveReportsWhatItKept:

    @pytest.mark.asyncio
    async def test_one_info_line_names_the_kept_keys(self, merge_file, caplog):
        """Criterion 3: names the keys, never the values."""
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{HAND_EDITED_URL}"'),
        )
        svc.set("speech.model", "from_the_program")

        with caplog.at_level(logging.INFO, logger="config_service"):
            assert await svc.save() is True

        kept_lines = [
            record.getMessage()
            for record in caplog.records
            if KEPT_LINE_PHRASE in record.getMessage()
        ]
        assert len(kept_lines) == 1, (
            f"expected exactly one line naming the kept key, got {kept_lines}"
        )
        assert "ai.help.gem_url" in kept_lines[0]
        assert HAND_EDITED_URL not in kept_lines[0], (
            "the settings file can hold private values, so the line names "
            f"keys only: {kept_lines[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_a_save_that_kept_nothing_says_nothing(
        self, merge_file, caplog
    ):
        """A line on every save would train the reader to ignore it."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")

        with caplog.at_level(logging.INFO, logger="config_service"):
            assert await svc.save() is True

        # Matched on the phrase, not the bare word: pytest names the
        # temporary directory after the test, so the ordinary "Configuration
        # saved to <path>" line carries this test's own name.
        assert not [
            record.getMessage()
            for record in caplog.records
            if KEPT_LINE_PHRASE in record.getMessage()
        ]
class TestSaveDoesNotUndoAChangeMadeWhileItWrote:
    """Boss ruling, 2026-09-03: the write-back must respect the concurrency
    contract the save() docstring already states.

    save() deep-copies the settings before its first await, so a caller can
    change a key while the write runs in its worker thread, and that change
    "reaches the file in its own write". Writing the disk's value back into
    memory afterwards would undo such a change. So a key is written back only
    when its current value still equals the value the save started from.
    """

    @pytest.mark.asyncio
    async def test_a_change_made_during_the_write_survives(
        self, merge_file, monkeypatch
    ):
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{HAND_EDITED_URL}"'),
        )

        real_to_thread = asyncio.to_thread
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_to_thread(func, *args, **kwargs):
            started.set()
            await release.wait()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", blocking_to_thread)

        svc.set("speech.model", "first")
        saving = asyncio.create_task(svc.save())
        await started.wait()

        # The user picks a different assistant while the write is in flight.
        svc.set("ai.help.gem_url", "set_while_the_write_ran")
        release.set()
        assert await saving is True

        assert svc.get("ai.help.gem_url") == "set_while_the_write_ran", (
            "the write-back overwrote a change made after the save started"
        )

        # And it reaches the file on the next save, as the docstring promises.
        monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
        assert await svc.save() is True
        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("ai.help.gem_url") == "set_while_the_write_ran"

    @pytest.mark.asyncio
    async def test_the_kept_value_reaches_memory_in_place(self, merge_file):
        """Ruling 3a: callers hold the object get_config() returned.

        service_manager.py and speech/speech_handler.py each store that object
        at construction and read it for the life of the process, so a save that
        REPLACED the settings object would leave both reading a dictionary
        nothing updates again. Nested tables have the same problem, since a
        caller can hold a sub-table.
        """
        svc = ConfigService(config_path=str(merge_file))
        held = svc.get_config()
        held_help = held["ai"]["help"]

        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', f'gem_url = "{HAND_EDITED_URL}"'),
        )
        svc.set("speech.model", "from_the_program")
        assert await svc.save() is True

        assert held is svc.get_config(), "the settings object was replaced"
        assert held_help is svc.get_config()["ai"]["help"], (
            "a nested table was replaced"
        )
        assert held_help["gem_url"] == HAND_EDITED_URL

    @pytest.mark.asyncio
    async def test_an_unset_made_during_the_write_survives(
        self, merge_file, monkeypatch
    ):
        """Review finding .1.1. The same contract, for a REMOVAL.

        A caller can remove a key while the write runs, and the write-back
        must not put it back. The key is absent from memory at write-back
        time, so the branch that adds a missing key is the one that has to
        refuse it.
        """
        svc = ConfigService(config_path=str(merge_file))

        real_to_thread = asyncio.to_thread
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_to_thread(func, *args, **kwargs):
            started.set()
            await release.wait()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", blocking_to_thread)

        svc.set("speech.model", "first")
        saving = asyncio.create_task(svc.save())
        await started.wait()

        svc.unset("LOG_LEVEL")
        release.set()
        assert await saving is True

        assert svc.get("LOG_LEVEL") is None, (
            "the write-back undid a removal made after the save started"
        )

        monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
        assert await svc.save() is True
        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None

    @pytest.mark.asyncio
    async def test_a_table_replaced_during_the_write_is_not_polluted(
        self, merge_file, monkeypatch
    ):
        """Review finding .1.1. The same contract, for a whole TABLE.

        Walking into a table the caller has just replaced puts the old
        table's keys into the new one. The new table is not the table the
        save started from, so the write-back leaves it alone.

        The hand-added key is what makes this test able to see the defect.
        Every key the table already held is blocked one level down by the
        guard on the add branch, which refuses to put back a key that was
        in the table the save started from. A key the file gained by hand
        was never in that table, so only the guard here keeps it out.
        """
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(merge_file, ORIGINAL_FILE + "added_by_hand = 99\n")

        real_to_thread = asyncio.to_thread
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_to_thread(func, *args, **kwargs):
            started.set()
            await release.wait()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", blocking_to_thread)

        svc.set("speech.model", "first")
        saving = asyncio.create_task(svc.save())
        await started.wait()

        svc.set("speech", {"brand_new": 1})
        release.set()
        assert await saving is True

        assert svc.get_config()["speech"] == {"brand_new": 1}, (
            "the write-back poured keys into the table the caller had "
            "just replaced"
        )

        # And the replacement reaches the file in its own write, as the
        # docstring of save promises. Review finding .1.6 asked for this
        # half: skipping the table keeps memory right, and without the
        # whole-table rule the next save wrote the old keys back and left
        # memory and the file disagreeing for good. added_by_hand stays,
        # because the program never held it.
        monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
        assert await svc.save() is True
        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get_config()["speech"] == {
            "brand_new": 1,
            "added_by_hand": 99,
        }, "the next save wrote the replaced table's old keys back"

    @pytest.mark.asyncio
    async def test_a_top_level_key_changed_during_the_write_survives(
        self, merge_file, monkeypatch
    ):
        """The same contract, for a key that is not inside a table.

        The table-level guard added for finding .1.1 stops the write-back
        before it reaches any key under a table the caller touched, so the
        per-key guard is reachable only at the top level. Without a
        top-level case nothing can see that guard removed.
        """
        svc = ConfigService(config_path=str(merge_file))

        real_to_thread = asyncio.to_thread
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_to_thread(func, *args, **kwargs):
            started.set()
            await release.wait()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", blocking_to_thread)

        svc.set("speech.model", "first")
        saving = asyncio.create_task(svc.save())
        await started.wait()

        svc.set("LOG_LEVEL", "DEBUG")
        release.set()
        assert await saving is True

        assert svc.get("LOG_LEVEL") == "DEBUG", (
            "the write-back overwrote a top-level key changed after the "
            "save started"
        )

    @pytest.mark.asyncio
    async def test_a_hand_added_key_survives_a_change_made_during_the_write(
        self, merge_file, monkeypatch
    ):
        """The two halves of this change must not delete a key between them.

        A change made while the write ran leaves the live table different
        from the table the save started from, so the write-back steps over
        it and the key the file gained never reaches memory. The next save
        then sees a key that is in the file and in the record of the last
        write, but not in memory. Reading that as a removal deletes a hand
        edit -- exactly what this bead exists to stop -- so only a removal
        the program actually asked for counts as one.
        """
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace(
                'model = "default"', 'model = "default"\ntimeout_ms = 700'
            ),
        )

        real_to_thread = asyncio.to_thread
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_to_thread(func, *args, **kwargs):
            started.set()
            await release.wait()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", blocking_to_thread)

        saving = asyncio.create_task(svc.save())
        await started.wait()

        svc.set("speech.model", "changed_while_the_write_ran")
        release.set()
        assert await saving is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.timeout_ms") == 700

        monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
        assert await svc.save() is True

        again = ConfigService(config_path=str(merge_file))
        assert again.get("speech.timeout_ms") == 700, (
            "the second save deleted a key the user added by hand"
        )
        assert again.get("speech.model") == "changed_while_the_write_ran"

DROPPED_LINE_PHRASE = "dropped, since the file no longer carries them"


class TestSaveKeepsARemovalTheProgramMade:
    """Review finding .1.2. The other half of the deletion rule.

    A key the program removed is absent from memory, so the merge used to
    read it as a key the program had never held and copy it back out of the
    file. main.py depends on the opposite: after a failed save it rolls back
    by removing the key and saves again, and the comment on that second save
    says the file converges on the rolled-back state.
    """

    @pytest.mark.asyncio
    async def test_a_key_the_program_removed_stays_removed(self, merge_file):
        svc = ConfigService(config_path=str(merge_file))
        svc.unset("LOG_LEVEL")
        assert await svc.save() is True

        assert svc.get("LOG_LEVEL") is None
        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None

    @pytest.mark.asyncio
    async def test_a_removal_survives_the_next_save(self, merge_file):
        """The save after it is an ordinary one -- a floating-button move."""
        svc = ConfigService(config_path=str(merge_file))
        svc.unset("LOG_LEVEL")
        assert await svc.save() is True

        svc.set("speech.model", "moved_the_button")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None
        assert reloaded.get("speech.model") == "moved_the_button"

    @pytest.mark.asyncio
    async def test_the_rollback_save_removes_a_refused_value(self, merge_file):
        """main.py lines 2617-2630 and 2847-2863, as they are written.

        The key was not in the file. The program set it, its own write
        failed, and a save running at the same time had already put the
        refused value on disk. main.py removes the key and saves again so
        the file converges. That second save has to drop the value.
        """
        svc = ConfigService(config_path=str(merge_file))
        svc.set("stt.credentials_path", "C:/refused.json")
        _hand_edit(
            merge_file,
            ORIGINAL_FILE + '\n[stt]\ncredentials_path = "C:/refused.json"\n',
        )

        svc.unset("stt.credentials_path")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("stt.credentials_path") is None

    @pytest.mark.asyncio
    async def test_a_key_set_again_after_a_removal_is_written(self, merge_file):
        """The removal is forgotten as soon as the caller changes its mind."""
        svc = ConfigService(config_path=str(merge_file))
        svc.unset("LOG_LEVEL")
        svc.set("LOG_LEVEL", "DEBUG")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "DEBUG"

    @pytest.mark.asyncio
    async def test_a_key_added_back_by_hand_after_a_removal_survives(
        self, merge_file
    ):
        """The record of a removal lasts exactly one write.

        Once the removal has reached the file, the key is an ordinary key
        again. A user who then puts it back by hand must keep it, so the
        record cannot be allowed to outlive the write that carried it out.
        """
        svc = ConfigService(config_path=str(merge_file))
        svc.unset("LOG_LEVEL")
        assert await svc.save() is True

        _hand_edit(merge_file, ORIGINAL_FILE)
        svc.set("speech.model", "moved_the_button")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "INFO", (
            "the removal was applied a second time, to a key the user "
            "had written back into the file"
        )


class TestSaveRefusesAnEmptiedFile:
    """Review finding .1.3. A file with nothing left in it is an accident.

    Every key the program has not changed counts as deleted by hand, so one
    save would write a file holding only the changed keys and drop the rest
    from memory as well. A file emptied by a select-all in an editor, by a
    crash, or by a synchronisation tool is far more likely than a user who
    means to delete every setting, so this is treated the way a file that
    will not parse is treated: the save goes ahead from memory and says so.
    """

    @pytest.mark.asyncio
    async def test_an_emptied_file_saves_from_memory_and_warns(
        self, merge_file, caplog
    ):
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(merge_file, "")

        with caplog.at_level(logging.WARNING, logger="config_service"):
            assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "INFO"
        assert reloaded.get("speech.model") == "from_the_program"

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert warnings, "an emptied settings file must be reported"
        assert any(str(merge_file) in message for message in warnings)

    @pytest.mark.asyncio
    async def test_a_file_holding_only_comments_is_treated_the_same_way(
        self, merge_file, caplog
    ):
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(merge_file, "# everything below was deleted\n")

        with caplog.at_level(logging.WARNING, logger="config_service"):
            assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "INFO"

        assert [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ], "a settings file holding only comments must be reported"

    @pytest.mark.asyncio
    async def test_one_section_left_is_still_read_as_hand_deletion(
        self, merge_file
    ):
        """The rule ends at nothing left. A file that still holds a section
        is a file the user edited, and the deletions in it stand."""
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(merge_file, "[speech]\nmodel = \"default\"\n")

        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None
        assert reloaded.get("speech.model") == "from_the_program"


class TestTheHeldObjectMatchesWhatReachedTheFile:
    """Review finding .1.4. Ruling 3a, checked through the held reference.

    A test that reloads the file cannot see the live settings object at all,
    so the two halves of the write-back -- adding a key the file gained and
    removing a key the file lost -- were pinned by nothing.
    """

    @pytest.mark.asyncio
    async def test_a_hand_deletion_survives_a_second_save(self, merge_file):
        """The removal half. Without it the deleted key is still in memory,
        and the next ordinary save writes it back into the file."""
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(merge_file, ORIGINAL_FILE.replace('LOG_LEVEL = "INFO"\n', ""))
        svc.set("speech.model", "first")
        assert await svc.save() is True

        svc.set("speech.model", "second")
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None
        assert reloaded.get("speech.model") == "second"

    @pytest.mark.asyncio
    async def test_a_hand_added_table_reaches_the_held_object(self, merge_file):
        """The adding half, read through the reference a caller holds."""
        svc = ConfigService(config_path=str(merge_file))
        held = svc.get_config()

        _hand_edit(
            merge_file,
            ORIGINAL_FILE + '\n[remote]\nname = "Living Room TV"\n',
        )
        svc.set("speech.model", "from_the_program")
        assert await svc.save() is True

        assert held is svc.get_config(), "the settings object was replaced"
        assert held.get("remote", {}).get("name") == "Living Room TV", (
            "a table added to the file by hand never reached the held object"
        )


class TestSaveReportsWhatItDropped:

    @pytest.mark.asyncio
    async def test_one_info_line_names_the_deleted_keys(
        self, merge_file, caplog
    ):
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(merge_file, ORIGINAL_FILE.replace('LOG_LEVEL = "INFO"\n', ""))
        svc.set("speech.model", "from_the_program")

        with caplog.at_level(logging.INFO, logger="config_service"):
            assert await svc.save() is True

        dropped_lines = [
            record.getMessage()
            for record in caplog.records
            if DROPPED_LINE_PHRASE in record.getMessage()
        ]
        assert len(dropped_lines) == 1, (
            f"expected exactly one line naming the deleted key, got "
            f"{dropped_lines}"
        )
        assert "LOG_LEVEL" in dropped_lines[0]

    @pytest.mark.asyncio
    async def test_the_dropped_line_names_no_values(self, merge_file, caplog):
        """Review finding .1.8. The kept half of the report is proven to
        name no values; the dropped half had only a presence check.

        This save reports both halves in the one line, so it covers the
        combined case as well. The settings file holds a URL, and the log is
        read by other people.
        """
        merge_file.write_text(
            ORIGINAL_FILE.replace('"INFO"', f'"{LOADED_LEVEL}"')
        )
        svc = ConfigService(config_path=str(merge_file))
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace(
                f'LOG_LEVEL = "INFO"\n', ""
            ).replace('gem_url = ""', f'gem_url = "{HAND_EDITED_URL}"'),
        )
        svc.set("speech.model", "from_the_program")

        with caplog.at_level(logging.INFO, logger="config_service"):
            assert await svc.save() is True

        dropped_lines = [
            record.getMessage()
            for record in caplog.records
            if DROPPED_LINE_PHRASE in record.getMessage()
        ]
        assert len(dropped_lines) == 1
        assert "LOG_LEVEL" in dropped_lines[0]
        assert "ai.help.gem_url" in dropped_lines[0], (
            "this save must report a kept key too, or it does not cover the "
            "line that carries both halves"
        )
        assert HAND_EDITED_URL not in dropped_lines[0], (
            "the report carried the value of a settings key"
        )
        assert "from_the_program" not in dropped_lines[0], (
            "the report carried the value of a settings key"
        )
        assert LOADED_LEVEL not in dropped_lines[0], (
            "the report carried the value of the key it dropped"
        )


class TestAQueuedSaveUsesTheCurrentBaseline:
    """Review finding .1.5.

    The lock serializes the writes, but a save that queues behind another
    one copies its baseline before it waits for the lock. The save in front
    then replaces that baseline, and the waiting save merges against values
    the file no longer holds.

    state_manager schedules a save for the microphone interaction mode
    without waiting for it, and main.py's rollback paths save while such a
    save may still be running, so two saves really do queue.
    """

    @staticmethod
    def _gate_the_second_save(monkeypatch):
        """Hold the SECOND save at its worker thread and hand back the
        events that watch it.

        The first save reaches asyncio.to_thread first because it holds the
        lock; the second reaches it only after the first has finished its
        write-back, so gating the second call is enough to place a hand edit
        between the two writes.
        """
        real_to_thread = asyncio.to_thread
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def gated_to_thread(func, *args, **kwargs):
            calls.append(func)
            if len(calls) == 2:
                started.set()
                await release.wait()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", gated_to_thread)
        return started, release

    @pytest.mark.asyncio
    async def test_the_second_save_compares_against_what_the_first_wrote(
        self, merge_file, monkeypatch
    ):
        svc = ConfigService(config_path=str(merge_file))
        svc.set("LOG_LEVEL", "from_the_program")

        started, release = self._gate_the_second_save(monkeypatch)
        first = asyncio.create_task(svc.save())
        second = asyncio.create_task(svc.save())

        await started.wait()
        assert await first is True

        # The user edits the file between the two writes. The program has
        # not touched LOG_LEVEL since the first write put it there, so the
        # second write must keep the hand edit.
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('LOG_LEVEL = "INFO"', 'LOG_LEVEL = "by_hand"'),
        )
        release.set()
        assert await second is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "by_hand", (
            "the queued save compared memory against a baseline the first "
            "save had already replaced, so it read an untouched key as a "
            "program change and wrote the hand edit away"
        )

    @pytest.mark.asyncio
    async def test_a_removal_the_first_save_carried_out_is_not_repeated(
        self, merge_file, monkeypatch
    ):
        svc = ConfigService(config_path=str(merge_file))
        svc.unset("LOG_LEVEL")

        started, release = self._gate_the_second_save(monkeypatch)
        first = asyncio.create_task(svc.save())
        second = asyncio.create_task(svc.save())

        await started.wait()
        assert await first is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None

        # The user puts the key back by hand. The record of the removal
        # lasts exactly one successful write, and the first save used it up.
        _hand_edit(merge_file, ORIGINAL_FILE)
        release.set()
        assert await second is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "INFO", (
            "the queued save applied a removal the save in front had "
            "already carried out, to a key the user had written back"
        )

    @pytest.mark.asyncio
    async def test_a_removal_survives_a_first_save_that_failed(
        self, merge_file, monkeypatch
    ):
        """The other half of the same rule, and the reason the fix cannot
        simply throw a queued record away: a save that fails carries out
        nothing, so the removal is still owed."""
        import os as os_module

        svc = ConfigService(config_path=str(merge_file))
        svc.unset("LOG_LEVEL")

        started, release = self._gate_the_second_save(monkeypatch)
        real_replace = os_module.replace
        replaced = []

        def failing_replace(src, dst, *args, **kwargs):
            replaced.append(dst)
            if len(replaced) == 1:
                raise OSError(5, "the first write failed")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os_module, "replace", failing_replace)

        first = asyncio.create_task(svc.save())
        second = asyncio.create_task(svc.save())

        await started.wait()
        assert await first is False

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "INFO", (
            "the failed save must have left the file alone"
        )

        release.set()
        assert await second is True

        monkeypatch.setattr(os_module, "replace", real_replace)
        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") is None, (
            "the removal was still owed, because the save in front carried "
            "out nothing"
        )

    @pytest.mark.asyncio
    async def test_the_second_save_uses_the_value_the_first_save_kept(
        self, merge_file, monkeypatch
    ):
        """The third baseline the queued save copies is the settings
        themselves, and the first save changes those too: a hand edit it
        keeps is written into memory.

        A copy taken before the wait holds the value from before the first
        write, so the second save reads the kept edit as a program change
        and writes the next hand edit away.
        """
        svc = ConfigService(config_path=str(merge_file))
        svc.set("LOG_LEVEL", "from_the_program")
        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace('model = "default"', 'model = "by_hand_one"'),
        )

        started, release = self._gate_the_second_save(monkeypatch)
        first = asyncio.create_task(svc.save())
        second = asyncio.create_task(svc.save())

        await started.wait()
        assert await first is True
        assert svc.get("speech.model") == "by_hand_one"

        _hand_edit(
            merge_file,
            ORIGINAL_FILE.replace(
                'LOG_LEVEL = "INFO"', 'LOG_LEVEL = "from_the_program"'
            ).replace('model = "default"', 'model = "by_hand_two"'),
        )
        release.set()
        assert await second is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("speech.model") == "by_hand_two", (
            "the queued save compared the file against the settings as "
            "they were before the first write, so the edit the first save "
            "kept read as a program change"
        )


TWO_KEY_SPEECH = (
    'LOG_LEVEL = "INFO"\n'
    "\n"
    "[speech]\n"
    'model = "default"\n'
    "timeout_ms = 700\n"
)


class TestATableIsMergedAtTheRightLevel:
    """Review finding .1.6.

    The merge decides key by key, which is right for a leaf and wrong at a
    table boundary, where the question is about the table itself.
    """

    @pytest.fixture
    def table_file(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(TWO_KEY_SPEECH)
        return path

    @pytest.mark.asyncio
    async def test_a_table_the_program_replaced_reaches_the_file(
        self, table_file
    ):
        """A whole table set by the program wins for that table.

        Both sides hold a table, so the merge walks into it, and every child
        the program dropped looked like a key the file had gained by hand.
        The replacement never reached the file.
        """
        svc = ConfigService(config_path=str(table_file))
        svc.set("speech", {"brand_new": 1})
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(table_file))
        assert reloaded.get_config()["speech"] == {"brand_new": 1}, (
            "the keys the program dropped came back out of the file"
        )

    @pytest.mark.asyncio
    async def test_a_table_deleted_by_hand_keeps_only_the_changed_key(
        self, table_file
    ):
        """A hand deletion of a whole table follows the same rule as a hand
        deletion of one line: the key the program changed survives, and a
        key it never touched goes.

        The file no longer holds a table at that key, so the merge compared
        the whole table instead, found it changed, and wrote every untouched
        sibling back.
        """
        svc = ConfigService(config_path=str(table_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(table_file, 'LOG_LEVEL = "INFO"\n')
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(table_file))
        assert reloaded.get("speech.model") == "from_the_program"
        assert reloaded.get("speech.timeout_ms") is None, (
            "a key the program never touched came back after the user "
            "deleted the table that held it"
        )

    @pytest.mark.asyncio
    async def test_a_table_the_user_emptied_by_hand_disappears(
        self, table_file
    ):
        """The end of the same rule. Nothing in the table was changed by the
        program, so every key in it is dropped and the table goes with them
        rather than staying behind as an empty heading."""
        svc = ConfigService(config_path=str(table_file))
        svc.set("LOG_LEVEL", "from_the_program")
        _hand_edit(table_file, 'LOG_LEVEL = "INFO"\n')
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(table_file))
        assert "speech" not in reloaded.get_config(), (
            "the deleted table came back, empty or otherwise"
        )

    @pytest.mark.asyncio
    async def test_a_hand_added_key_survives_its_table_being_replaced(
        self, table_file
    ):
        """The limit of the rule above, and it is deliberate.

        Replacing a table takes out the keys the PROGRAM held in it. A key
        the file gained by hand was never one of those, and this bead exists
        because hand edits were being discarded, so it stays.
        """
        svc = ConfigService(config_path=str(table_file))
        _hand_edit(table_file, TWO_KEY_SPEECH + "added_by_hand = 99\n")
        svc.set("speech", {"brand_new": 1})
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(table_file))
        assert reloaded.get_config()["speech"] == {
            "brand_new": 1,
            "added_by_hand": 99,
        }

    @pytest.mark.asyncio
    async def test_a_table_retyped_by_hand_loses_to_a_program_change(
        self, table_file
    ):
        """Recorded here because it is the one table case this finding
        raised that is NOT changed: the file holds a scalar where memory
        holds a table.

        There is no per-key comparison to make, so the key is compared
        whole, and the program wins because it changed a descendant. That is
        the same rule a leaf conflict follows.
        """
        svc = ConfigService(config_path=str(table_file))
        svc.set("speech.model", "from_the_program")
        _hand_edit(table_file, 'LOG_LEVEL = "INFO"\nspeech = "off"\n')
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(table_file))
        assert reloaded.get("speech.model") == "from_the_program"

    @pytest.mark.asyncio
    async def test_a_table_nobody_changed_keeps_what_the_file_says(
        self, table_file
    ):
        """The other side of the retyping case: with no program change, the
        file's scalar wins, exactly as a leaf would."""
        svc = ConfigService(config_path=str(table_file))
        svc.set("LOG_LEVEL", "from_the_program")
        _hand_edit(table_file, 'LOG_LEVEL = "INFO"\nspeech = "off"\n')
        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(table_file))
        assert reloaded.get("speech") == "off"


class TestAHandEditMadeWhileTheWriteRanSurvives:
    """Review finding .1.9.

    The file is read at the start of the write and put in place at the end
    of it. An edit saved between those two points was never read, so the
    file that goes in place would carry the settings without it.
    """

    @staticmethod
    def _edit_while_the_write_runs(monkeypatch, path, text, once=True):
        """Save a hand edit from inside the write, after the file was read.

        The dump is the first thing that happens after the merge, so an
        edit made there lands after the read it has to beat. The returned
        list carries one entry per write to the temporary file.
        """
        import tomli_w

        real_dump = tomli_w.dump
        calls = []

        def dump_after_a_hand_edit(data, fp, *args, **kwargs):
            calls.append(len(calls) + 1)
            if len(calls) == 1 or not once:
                _hand_edit(path, text)
            return real_dump(data, fp, *args, **kwargs)

        monkeypatch.setattr(tomli_w, "dump", dump_after_a_hand_edit)
        return calls

    @pytest.mark.asyncio
    async def test_an_edit_saved_after_the_read_is_not_written_over(
        self, merge_file, monkeypatch
    ):
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        self._edit_while_the_write_runs(
            monkeypatch,
            merge_file,
            ORIGINAL_FILE.replace(
                'LOG_LEVEL = "INFO"', 'LOG_LEVEL = "by_hand"'
            ),
        )

        assert await svc.save() is True

        reloaded = ConfigService(config_path=str(merge_file))
        assert reloaded.get("LOG_LEVEL") == "by_hand", (
            "the file that went in place had been prepared before the edit "
            "was saved, so it wrote the edit away"
        )
        assert reloaded.get("speech.model") == "from_the_program"

    @pytest.mark.asyncio
    async def test_the_file_is_read_again_exactly_once(
        self, merge_file, monkeypatch
    ):
        """An editor that keeps saving must not hold the write open.

        The file is read again once and no more, so the number of writes to
        the temporary file settles at two however often the file changes.
        """
        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        calls = self._edit_while_the_write_runs(
            monkeypatch,
            merge_file,
            ORIGINAL_FILE.replace('gem_url = ""', 'gem_url = "by_hand"'),
            once=False,
        )

        assert await svc.save() is True
        assert calls == [1, 2], (
            "the write either never read the file again or read it more "
            "than once"
        )

    @pytest.mark.asyncio
    async def test_a_file_nobody_touched_is_written_once(
        self, merge_file, monkeypatch
    ):
        """The ordinary save does no extra work: nothing changed the file
        while it ran, so there is nothing to read again."""
        import tomli_w

        real_dump = tomli_w.dump
        calls = []

        def count_the_writes(data, fp, *args, **kwargs):
            calls.append(len(calls) + 1)
            return real_dump(data, fp, *args, **kwargs)

        monkeypatch.setattr(tomli_w, "dump", count_the_writes)

        svc = ConfigService(config_path=str(merge_file))
        svc.set("speech.model", "from_the_program")
        assert await svc.save() is True
        assert calls == [1]


class TestASectionThatHoldsAValueInstead:
    """A settings file can carry a plain value where a section belongs, and
    main.py answers that by catching TypeError from set() and reporting the
    refusal (_store_setting, main.py). The record of what a replaced table
    carried, added for review finding .1.6, reads the old value before the
    assignment runs, so it must not raise a different error first and walk
    past that handler.
    """

    @pytest.fixture
    def scalar_section(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('LOG_LEVEL = "INFO"\nstt = "whisper"\n')
        return path

    @staticmethod
    def _refusal(svc):
        """The name of what set() raises, or "nothing".

        By name rather than by pytest.raises, so that a wrong error is an
        assertion this test can report rather than an exception that leaves
        the reader to work out what happened.
        """
        try:
            svc.set("stt.provider", "google")
        except Exception as raised:
            return type(raised).__name__
        return "nothing"

    def test_setting_a_key_under_a_value_raises_type_error(
        self, scalar_section
    ):
        svc = ConfigService(config_path=str(scalar_section))
        assert self._refusal(svc) == "TypeError", (
            "main.py catches TypeError from set() and reports the refusal to "
            "the user; any other error walks past that handler and closes "
            "Wheelhouse"
        )

    def test_the_refused_write_leaves_the_settings_alone(
        self, scalar_section
    ):
        """The other half of the refusal, and it holds whichever error is
        raised, because nothing is written before the raise."""
        svc = ConfigService(config_path=str(scalar_section))
        self._refusal(svc)
        assert svc.get("stt") == "whisper"
