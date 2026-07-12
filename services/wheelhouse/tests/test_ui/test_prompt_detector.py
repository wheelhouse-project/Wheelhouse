"""Tests for PromptDetector after the console-probe-helper refactor.

The AttachConsole/GetConsoleMode probe was moved out of the Logic process and
into a persistent supervised helper subprocess (see
``ui/console_probe_helper.py`` and ``ui/console_probe_client.py``). The Logic
process must NEVER bind to a foreign terminal's console, so ``PromptDetector``
no longer touches the Win32 console API directly: it delegates to a
``ConsoleProbeClient`` injected at construction (or lazily constructed).

These tests assert:
  * the delegating contract: ``is_at_prompt`` forwards (process_name, pid) to
    the client and returns the client's bool;
  * the main-process path is clean -- the module no longer references
    AttachConsole, no longer defines ``_has_interactive_child``, and exposes
    no module-level console lock.
"""

import inspect
import io
import json

from ui.prompt_detector import PromptDetector
from ui import prompt_detector as prompt_detector_module
# ``ui.console_probe_client`` and ``services.wheelhouse.ui.console_probe_client``
# are DISTINCT module objects under the test sys.path (service-dir vs
# project-root both on the path). ``_get_shared_client`` lazy-imports
# ConsoleProbeClient from the ``services.*`` path, so that is the only patchable
# lookup target; patching the ``ui.*`` alias would miss it entirely (wh-jvrs.3.2).
from ui import console_probe_client as console_probe_client_module  # noqa: F401
from services.wheelhouse.ui import (
    console_probe_client as shared_client_import_target,
)


class _FakeClient:
    """Records calls and returns a canned result."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def is_at_prompt(self, process_name, pid):
        self.calls.append((process_name, pid))
        return self._result


class TestPromptDetectorDelegation:
    def test_delegates_true_to_client(self):
        client = _FakeClient(True)
        detector = PromptDetector(client=client)

        assert detector.is_at_prompt("WindowsTerminal.exe", 100) is True
        assert client.calls == [("WindowsTerminal.exe", 100)]

    def test_delegates_false_to_client(self):
        client = _FakeClient(False)
        detector = PromptDetector(client=client)

        assert detector.is_at_prompt("WindowsTerminal.exe", 100) is False

    def test_client_failure_degrades_to_false(self):
        class _RaisingClient:
            def is_at_prompt(self, process_name, pid):
                raise RuntimeError("helper unreachable")

        detector = PromptDetector(client=_RaisingClient())

        # An UNEXPECTED client failure must not propagate -- fail closed to
        # False so a genuine bug never crashes the speech path.
        assert detector.is_at_prompt("WindowsTerminal.exe", 100) is False

    def test_console_probe_error_propagates(self):
        # A ConsoleProbeError is a TRANSPORT failure, distinct from a busy
        # verdict. It must PROPAGATE so FocusRedirectPolicy routes it to
        # prompt_detector_error rather than caching a False as terminal_busy
        # and suppressing the redirect for the whole utterance (wh-jvrs.3.1).
        import pytest

        from ui.console_probe_client import ConsoleProbeError

        class _StallingClient:
            def is_at_prompt(self, process_name, pid):
                raise ConsoleProbeError("helper read timed out")

        detector = PromptDetector(client=_StallingClient())

        with pytest.raises(ConsoleProbeError):
            detector.is_at_prompt("WindowsTerminal.exe", 100)


class TestDefaultPromptDetectorCallSeam:
    """The production prompt_detector_call seam must not re-swallow a
    ConsoleProbeError back into a False (wh-jvrs.3.1).

    ``speech_handler._default_prompt_detector_call`` is the callable the
    production ``FocusRedirectPolicy`` injects as ``prompt_detector_call``. If
    it caught ConsoleProbeError and returned False, the whole 3.1 fix would be
    defeated at the outermost layer: the policy would see a False and cache it
    as terminal_busy.
    """

    def test_console_probe_error_propagates_through_default_seam(
        self, monkeypatch
    ):
        import pytest

        from services.wheelhouse.speech import speech_handler
        from ui.console_probe_client import ConsoleProbeError

        class _StallingDetector:
            def is_at_prompt(self, process_name, pid):
                raise ConsoleProbeError("helper read timed out")

        # Patch PromptDetector where _default_prompt_detector_call imports it.
        monkeypatch.setattr(
            "services.wheelhouse.ui.prompt_detector.PromptDetector",
            lambda *a, **k: _StallingDetector(),
        )

        with pytest.raises(ConsoleProbeError):
            speech_handler._default_prompt_detector_call("pwsh.exe", 100)

    def test_unexpected_error_still_fails_closed_through_default_seam(
        self, monkeypatch
    ):
        from services.wheelhouse.speech import speech_handler

        class _BuggyDetector:
            def is_at_prompt(self, process_name, pid):
                raise RuntimeError("genuine bug")

        monkeypatch.setattr(
            "services.wheelhouse.ui.prompt_detector.PromptDetector",
            lambda *a, **k: _BuggyDetector(),
        )

        # A non-transport bug must NOT crash the speech path.
        assert speech_handler._default_prompt_detector_call("pwsh.exe", 100) is False


class TestConsoleProbeErrorSubclassPropagation:
    """A ConsoleProbeError SUBCLASS is still a transport failure and must
    propagate, not be swallowed into a False busy verdict.

    Regression guard for wh-jvrs.3.6: the old guards used a leaf-name-only
    check (``type(exc).__name__ == "ConsoleProbeError"``) as the fallback, so a
    ``DerivedProbeError(ConsoleProbeError)`` -- whose ``__name__`` differs --
    was treated as an unexpected exception and returned False. The policy would
    then cache that False as terminal_busy, exactly the 3.1 failure mode the
    transport-failure contract exists to prevent. The shared classifier matches
    ConsoleProbeError anywhere in the MRO across both import-path copies.
    """

    def test_subclass_from_ui_path_propagates_through_detector(self):
        import pytest

        from ui.console_probe_client import ConsoleProbeError as UiProbeError

        class _DerivedProbeError(UiProbeError):
            pass

        class _RaisingClient:
            def is_at_prompt(self, process_name, pid):
                raise _DerivedProbeError("subclass transport failure")

        detector = PromptDetector(client=_RaisingClient())

        with pytest.raises(_DerivedProbeError):
            detector.is_at_prompt("WindowsTerminal.exe", 100)

    def test_subclass_from_services_path_propagates_through_detector(self):
        import pytest

        from services.wheelhouse.ui.console_probe_client import (
            ConsoleProbeError as ServicesProbeError,
        )

        class _DerivedProbeError(ServicesProbeError):
            pass

        class _RaisingClient:
            def is_at_prompt(self, process_name, pid):
                raise _DerivedProbeError("subclass transport failure")

        detector = PromptDetector(client=_RaisingClient())

        with pytest.raises(_DerivedProbeError):
            detector.is_at_prompt("WindowsTerminal.exe", 100)

    def test_classifier_recognises_both_paths_and_subclasses(self):
        from ui.prompt_detector import is_console_probe_error
        from ui.console_probe_client import ConsoleProbeError as UiProbeError
        from services.wheelhouse.ui.console_probe_client import (
            ConsoleProbeError as ServicesProbeError,
        )

        class _DerivedFromUi(UiProbeError):
            pass

        class _DerivedFromServices(ServicesProbeError):
            pass

        assert is_console_probe_error(UiProbeError("x")) is True
        assert is_console_probe_error(ServicesProbeError("x")) is True
        assert is_console_probe_error(_DerivedFromUi("x")) is True
        assert is_console_probe_error(_DerivedFromServices("x")) is True
        # A genuinely unrelated error must NOT be misclassified.
        assert is_console_probe_error(RuntimeError("x")) is False
        assert is_console_probe_error(ValueError("x")) is False


class TestConsoleProbeErrorSubclassThroughDefaultSeam:
    """The production prompt_detector_call seam must also propagate subclasses
    of ConsoleProbeError, not just the exact base class (wh-jvrs.3.6)."""

    def _patch_detector(self, monkeypatch, exc):
        class _RaisingDetector:
            def is_at_prompt(self, process_name, pid):
                raise exc

        monkeypatch.setattr(
            "services.wheelhouse.ui.prompt_detector.PromptDetector",
            lambda *a, **k: _RaisingDetector(),
        )

    def test_subclass_from_ui_path_propagates_through_default_seam(
        self, monkeypatch
    ):
        import pytest

        from services.wheelhouse.speech import speech_handler
        from ui.console_probe_client import ConsoleProbeError as UiProbeError

        class _DerivedProbeError(UiProbeError):
            pass

        self._patch_detector(monkeypatch, _DerivedProbeError("subclass stall"))

        with pytest.raises(_DerivedProbeError):
            speech_handler._default_prompt_detector_call("pwsh.exe", 100)

    def test_subclass_from_services_path_propagates_through_default_seam(
        self, monkeypatch
    ):
        import pytest

        from services.wheelhouse.speech import speech_handler
        from services.wheelhouse.ui.console_probe_client import (
            ConsoleProbeError as ServicesProbeError,
        )

        class _DerivedProbeError(ServicesProbeError):
            pass

        self._patch_detector(monkeypatch, _DerivedProbeError("subclass stall"))

        with pytest.raises(_DerivedProbeError):
            speech_handler._default_prompt_detector_call("pwsh.exe", 100)


class TestClassifierImportFallback:
    """The MRO-name fallback in ``_classify_console_probe_error`` must still
    recognise ConsoleProbeError subclasses when the import of the shared
    ``is_console_probe_error`` classifier fails (wh-jvrs.4.3).

    The fallback exists so a future refactor that renames or breaks the
    ``prompt_detector`` import path does not silently re-create the wh-jvrs.3.6
    bug: a derived ConsoleProbeError would otherwise be misclassified as an
    unexpected error and swallowed into a False busy verdict. These tests force
    the import to fail (by removing the symbol from the imported module) and
    assert the fallback's MRO walk still recognises the class and its
    subclasses, while an unrelated error is NOT misclassified.
    """

    def _break_shared_classifier(self, monkeypatch):
        # ``_classify_console_probe_error`` does
        # ``from services.wheelhouse.ui.prompt_detector import is_console_probe_error``
        # inside its try-block. Removing the attribute makes that import raise
        # ImportError, exercising the except-branch MRO fallback.
        import services.wheelhouse.ui.prompt_detector as pd_mod

        monkeypatch.delattr(pd_mod, "is_console_probe_error", raising=False)

    def test_fallback_recognises_base_class(self, monkeypatch):
        from services.wheelhouse.speech import speech_handler
        from ui.console_probe_client import ConsoleProbeError

        self._break_shared_classifier(monkeypatch)

        assert speech_handler._classify_console_probe_error(
            ConsoleProbeError("transport")
        ) is True

    def test_fallback_recognises_subclass(self, monkeypatch):
        from services.wheelhouse.speech import speech_handler
        from ui.console_probe_client import ConsoleProbeError

        class _DerivedProbeError(ConsoleProbeError):
            pass

        self._break_shared_classifier(monkeypatch)

        # The MRO walk must find ConsoleProbeError among the subclass's bases,
        # NOT just check the leaf type -- the wh-jvrs.3.6 regression shape.
        assert speech_handler._classify_console_probe_error(
            _DerivedProbeError("transport")
        ) is True

    def test_fallback_rejects_unrelated_error(self, monkeypatch):
        from services.wheelhouse.speech import speech_handler

        self._break_shared_classifier(monkeypatch)

        assert speech_handler._classify_console_probe_error(
            RuntimeError("unrelated")
        ) is False

    def test_fallback_subclass_propagates_through_default_seam(
        self, monkeypatch
    ):
        # End-to-end through the production seam: with the shared classifier
        # import broken, a derived ConsoleProbeError raised by the detector must
        # STILL propagate (not be swallowed into a False) via the MRO fallback.
        import pytest

        from services.wheelhouse.speech import speech_handler
        from ui.console_probe_client import ConsoleProbeError

        class _DerivedProbeError(ConsoleProbeError):
            pass

        class _StallingDetector:
            def is_at_prompt(self, process_name, pid):
                raise _DerivedProbeError("subclass stall")

        monkeypatch.setattr(
            "services.wheelhouse.ui.prompt_detector.PromptDetector",
            lambda *a, **k: _StallingDetector(),
        )
        self._break_shared_classifier(monkeypatch)

        with pytest.raises(_DerivedProbeError):
            speech_handler._default_prompt_detector_call("pwsh.exe", 100)


class TestMainProcessPathIsClean:
    """The Logic-process module must not call AttachConsole on foreign pids."""

    def test_module_source_has_no_attach_console(self):
        src = inspect.getsource(prompt_detector_module)
        assert "AttachConsole" not in src

    def test_no_has_interactive_child_method(self):
        # The method that performed FreeConsole()/AttachConsole() in-process
        # must be gone from PromptDetector entirely.
        assert not hasattr(PromptDetector, "_has_interactive_child")

    def test_no_module_console_lock(self):
        # The process-global console lock existed only to serialise in-process
        # AttachConsole calls; with the probe out of process it is dead.
        assert not hasattr(prompt_detector_module, "_console_lock")

    def test_no_kernel32_binding(self):
        # The Win32 kernel32 binding lived only to drive the in-process probe.
        assert not hasattr(prompt_detector_module, "_kernel32")


class TestSharedClientSingleton:
    """The production caller builds a fresh PromptDetector per call; the default
    client must be a process-wide singleton so the helper/cache persist.

    Regression guard for wh-jvrs.1.1.1: a per-call client would spawn a cold
    subprocess on every probe (hot-path latency), never hit the cache, and make
    the respawn branch unreachable.
    """

    def setup_method(self):
        # Reset the module singleton so each test starts clean.
        prompt_detector_module._shared_client = None

    def teardown_method(self):
        prompt_detector_module._shared_client = None

    def test_default_detectors_share_one_client(self, monkeypatch):
        spawn_count = {"n": 0}

        class _Stdin:
            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, data):
                return self.buffer.write(data)

            def flush(self):
                pass

            def close(self):
                pass

        class _Stdout:
            """Echoes the last requested pid as an at-prompt=True response so the
            round-trip validates and the probe returns a genuine bool. A real
            transport (not None pipes) keeps this test about singleton SHARING
            and spawn COUNT, independent of the transport-failure semantics."""

            def __init__(self, stdin):
                self._stdin = stdin

            def readline(self):
                written = self._stdin.buffer.getvalue().decode("utf-8").strip()
                if not written:
                    return b""
                pid = json.loads(written.splitlines()[-1])["pid"]
                return (
                    json.dumps({"pid": pid, "result": True}) + "\n"
                ).encode("utf-8")

            def close(self):
                pass

        class _FakeHelper:
            def __init__(self):
                self.stdin = _Stdin()
                self.stdout = _Stdout(self.stdin)

            def poll(self):
                return None

        def _counting_factory():
            spawn_count["n"] += 1
            return _FakeHelper()

        # Make the singleton's real client use a counting helper factory.
        # CRITICAL (wh-jvrs.3.2): patch the SAME module object that
        # ``_get_shared_client`` resolves -- it lazy-imports ConsoleProbeClient
        # from ``services.wheelhouse.ui.console_probe_client``
        # (``shared_client_import_target``), which is a DISTINCT module object
        # from ``ui.console_probe_client`` (``console_probe_client_module``)
        # under the test sys.path. Patching the latter would not intercept the
        # class the module under test actually instantiates, so the real
        # singleton could spawn an actual helper subprocess while spawn_count
        # stayed 0 and the assertion passed vacuously.
        RealClient = shared_client_import_target.ConsoleProbeClient
        monkeypatch.setattr(
            shared_client_import_target,
            "ConsoleProbeClient",
            lambda: RealClient(proc_factory=_counting_factory),
        )

        # Several fresh default-constructed detectors, like the production
        # per-call wiring. They must all resolve to ONE shared client.
        d1 = PromptDetector()
        d2 = PromptDetector()
        d3 = PromptDetector()
        c1 = d1._get_client()
        c2 = d2._get_client()
        c3 = d3._get_client()

        assert c1 is c2 is c3
        # Probing through the shared client spawns the helper EXACTLY once even
        # across multiple default detectors. The == 1 lower bound is the
        # wh-jvrs.3.2 regression guard: if the patch missed the real lookup
        # target, the counting factory would never run and spawn_count would
        # stay 0 (passing a <= 1 assertion vacuously while a real subprocess
        # spawned). Asserting the fake factory ran proves the patch intercepts.
        r1 = d1.is_at_prompt("pwsh.exe", 100)
        d2.is_at_prompt("pwsh.exe", 100)
        assert r1 is True  # the patched fake transport handled the probe
        assert spawn_count["n"] == 1

    def test_explicit_client_bypasses_singleton(self):
        class _FakeClient:
            def is_at_prompt(self, name, pid):
                return True

        explicit = _FakeClient()
        detector = PromptDetector(client=explicit)

        assert detector._get_client() is explicit
        # The singleton must not have been built by an explicit-client detector.
        assert prompt_detector_module._shared_client is None


class TestSharedClientTeardown:
    """The shared helper subprocess must be reapable on a clean process exit.

    Regression guard for wh-jvrs.2.2: nothing called close() on the singleton,
    so the helper relied entirely on the OS closing its stdin pipe at Logic
    exit. The atexit-registered teardown explicitly reaps it (belt-and-
    suspenders against an orphaned, console-attaching helper).
    """

    def setup_method(self):
        prompt_detector_module._shared_client = None
        prompt_detector_module._atexit_registered = False

    def teardown_method(self):
        prompt_detector_module._shared_client = None
        prompt_detector_module._atexit_registered = False

    def test_building_singleton_registers_atexit_once(self, monkeypatch):
        registered = []
        monkeypatch.setattr(
            prompt_detector_module.atexit,
            "register",
            lambda fn: registered.append(fn),
        )

        class _NoStdHelper:
            stdin = None
            stdout = None

            def poll(self):
                return None

        RealClient = shared_client_import_target.ConsoleProbeClient
        monkeypatch.setattr(
            shared_client_import_target,
            "ConsoleProbeClient",
            lambda: RealClient(proc_factory=lambda: _NoStdHelper()),
        )

        # Build the singleton twice; the atexit hook registers exactly once.
        prompt_detector_module._get_shared_client()
        prompt_detector_module._get_shared_client()

        assert registered == [prompt_detector_module._close_shared_client]

    def test_close_shared_client_terminates_and_clears(self, monkeypatch):
        closed = {"n": 0}

        class _ClosableClient:
            def close(self):
                closed["n"] += 1

        monkeypatch.setattr(
            shared_client_import_target,
            "ConsoleProbeClient",
            lambda: _ClosableClient(),
        )

        client = prompt_detector_module._get_shared_client()
        assert isinstance(client, _ClosableClient)

        prompt_detector_module._close_shared_client()

        assert closed["n"] == 1
        # The singleton is dropped so a later use rebuilds cleanly (the
        # idempotent disposal a future in-process restart path needs).
        assert prompt_detector_module._shared_client is None

    def test_close_shared_client_is_noop_when_never_built(self):
        # No singleton was ever built: teardown must not raise.
        assert prompt_detector_module._shared_client is None
        prompt_detector_module._close_shared_client()  # no exception
        assert prompt_detector_module._shared_client is None
