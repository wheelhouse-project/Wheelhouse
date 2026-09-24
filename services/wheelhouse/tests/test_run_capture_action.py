"""Tests for the ``run_capture`` pattern action.

The subprocess cases deliberately run ``helpers/run_capture_helper.py`` via
the real Python interpreter. The helper makes command invocation, timeout,
and byte-decoding behavior testable while observing real asyncio subprocess launches.
"""

from __future__ import annotations

import asyncio
import logging
import pickle
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from speech.action_catalog import ACTION_CATALOG
from speech.actions import ActionFunctions
from speech.actions_config import OUTPUT_CAP_CEILING_CHARS
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog


HELPER = Path(__file__).parent / "helpers" / "run_capture_helper.py"


def _action_functions(actions_config: dict[str, object] | None = None) -> ActionFunctions:
    handler = MagicMock()
    handler.config_service.get.return_value = actions_config or {}
    return ActionFunctions(handler)


@pytest_asyncio.fixture
async def started_processes(monkeypatch: pytest.MonkeyPatch):
    """Observe parent-owned PIDs; clean up even when a termination assertion fails."""
    processes: list[asyncio.subprocess.Process] = []
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def record_launch(*args, **kwargs):
        process = await create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        assert process.pid > 0
        assert process.returncode is None, "child must be alive at launch"
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_launch)
    try:
        yield processes
    finally:
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()


@pytest.fixture
def evidence_tmp_dir() -> Path:
    """Use the writable, ignored TDD area instead of the sandbox temp root."""
    directory = (
        Path(".tdd-evidence") / "test-runtime" / uuid.uuid4().hex
    ).resolve()
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def catalog() -> PatternCatalog:
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def mock_app() -> MagicMock:
    app = MagicMock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog: PatternCatalog, mock_app: MagicMock) -> TextParser:
    handler = MagicMock()
    handler.app = mock_app
    handler.config_service.get.return_value = {}
    handler.speech_processor = None
    return TextParser(handler, catalog)


class TestRunCaptureUnit:
    @pytest.mark.asyncio
    async def test_returns_stdout_with_only_trailing_newline_removed(self):
        result = await _action_functions().run_capture(
            sys.executable, str(HELPER), "emit", "result text\n"
        )
        assert result == "result text"

    @pytest.mark.asyncio
    async def test_passes_a_spaces_quotes_and_ampersand_argument_as_one_value(self):
        argument = 'two words "quoted" & unchanged'
        result = await _action_functions().run_capture(
            sys.executable, str(HELPER), "emit", argument
        )
        assert result == argument

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("argument", "expected"),
        [(5, "5"), (0.5, "0.5")],
    )
    async def test_numeric_arguments_are_stringified_and_passed_through(
        self, argument: int | float, expected: str
    ):
        result = await _action_functions().run_capture(
            sys.executable, str(HELPER), "emit", argument
        )
        assert result == expected

    @pytest.mark.asyncio
    async def test_empty_stdout_succeeds_with_empty_stored_value(self):
        result = await _action_functions().run_capture(
            sys.executable, str(HELPER), "emit", ""
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_replaces_undecodable_stdout_bytes(self):
        result = await _action_functions().run_capture(
            sys.executable, str(HELPER), "emit_bytes"
        )
        assert result == "\ufffd\ufffdoutput"

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_a_failure(self):
        with pytest.raises(RuntimeError, match="code 7"):
            await _action_functions().run_capture(
                sys.executable, str(HELPER), "exit", "7"
            )

    @pytest.mark.asyncio
    async def test_program_not_found_is_a_failure(self, evidence_tmp_dir: Path):
        missing_program = evidence_tmp_dir / "sensitive-command-that-does-not-exist.exe"
        with pytest.raises(FileNotFoundError):
            await _action_functions().run_capture(str(missing_program))

    @pytest.mark.asyncio
    async def test_none_program_is_a_failure(self):
        with pytest.raises(ValueError, match="program path is required"):
            await _action_functions().run_capture(None)

    @pytest.mark.asyncio
    async def test_none_argument_is_rejected_without_starting_a_child(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        start_child = AsyncMock(side_effect=AssertionError("child started"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", start_child)

        with pytest.raises(ValueError, match="unresolved parameter"):
            await _action_functions().run_capture(sys.executable, None)

        start_child.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bool_argument_is_rejected_without_starting_a_child(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        start_child = AsyncMock(side_effect=AssertionError("child started"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", start_child)

        with pytest.raises(ValueError, match="str, int, or float"):
            await _action_functions().run_capture(sys.executable, True)

        start_child.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_output_over_configured_cap_is_a_failure(self):
        with pytest.raises(ValueError, match="output exceeds"):
            await _action_functions({"output_cap_chars": 5}).run_capture(
                sys.executable, str(HELPER), "emit", "sixsix"
            )

    @pytest.mark.asyncio
    async def test_full_cap_stdout_with_trailing_lf_is_stored(self):
        result = await _action_functions({"output_cap_chars": 5}).run_capture(
            sys.executable, str(HELPER), "emit", "hello\n"
        )
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_full_cap_stdout_with_trailing_crlf_is_stored(self):
        result = await _action_functions({"output_cap_chars": 5}).run_capture(
            sys.executable, str(HELPER), "emit", "hello\r\n"
        )
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_newline_only_stdout_over_raw_cap_stores_empty_value(self):
        result = await _action_functions({"output_cap_chars": 5}).run_capture(
            sys.executable, str(HELPER), "emit", "\n" * 7
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_mid_text_newline_counts_toward_stored_output_cap(self):
        with pytest.raises(ValueError, match="output exceeds"):
            await _action_functions({"output_cap_chars": 5}).run_capture(
                sys.executable, str(HELPER), "emit", "hell\nx"
            )

    @pytest.mark.asyncio
    async def test_folded_mid_text_newlines_count_toward_stored_output_cap(self):
        # Text-mode emit turns "\n" into "\r\n", so the stored value would be
        # "hell\r\nx" (7 chars): the two mid-text newline characters fit the
        # cap-6 headroom and must still count once content follows them.
        with pytest.raises(ValueError, match="output exceeds"):
            await _action_functions({"output_cap_chars": 6}).run_capture(
                sys.executable, str(HELPER), "emit", "hell\nx"
            )

    @pytest.mark.asyncio
    async def test_stdout_flood_is_terminated_before_helper_reaches_eof(
        self, evidence_tmp_dir: Path
    ):
        completion_file = evidence_tmp_dir / "flood-complete.txt"
        with pytest.raises(ValueError, match="output exceeds"):
            await _action_functions({"output_cap_chars": 64}).run_capture(
                sys.executable,
                str(HELPER),
                "flood_stdout_until_complete",
                "4096",
                "16384",
                str(completion_file),
            )
        assert not completion_file.exists()

    @pytest.mark.asyncio
    async def test_stderr_flood_is_truncated_before_logging(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("WHEELHOUSE_LOG_TRANSCRIPTS", "1")
        with caplog.at_level(logging.WARNING, logger="speech.actions"):
            with pytest.raises(RuntimeError, match="code 7"):
                await _action_functions().run_capture(
                    sys.executable, str(HELPER), "flood_stderr", "1024", "32", "7"
                )
        message = next(
            record.getMessage()
            for record in caplog.records
            if "run_capture exited with code" in record.getMessage()
        )
        assert "stderr output truncated" in message
        assert len(message) < 6_000

    @pytest.mark.asyncio
    async def test_timeout_terminates_the_started_process(self, started_processes):
        with pytest.raises(TimeoutError):
            await _action_functions().run_capture(
                0.5, sys.executable, str(HELPER), "sleep", "2"
            )
        assert len(started_processes) == 1
        process = started_processes[0]
        assert process.returncode is not None, f"child {process.pid} is still running"
        assert process.returncode != 0, "child exited naturally instead of being terminated"

    @pytest.mark.asyncio
    async def test_unexpected_collector_error_terminates_the_started_process(
        self, started_processes, monkeypatch: pytest.MonkeyPatch
    ):
        import speech.actions as actions_module

        async def raise_unexpected_collector_error(*_args: object) -> None:
            raise OSError("simulated pipe read failure")

        monkeypatch.setattr(
            actions_module, "_collect_run_capture_output", raise_unexpected_collector_error
        )

        with pytest.raises(OSError, match="simulated pipe read failure"):
            await _action_functions().run_capture(
                sys.executable, str(HELPER), "sleep", "2"
            )

        assert len(started_processes) == 1
        process = started_processes[0]
        assert process.returncode is not None, f"child {process.pid} is still running"
        assert process.returncode != 0, "child exited naturally instead of being terminated"

    @pytest.mark.asyncio
    async def test_leading_float_parameter_is_consumed_as_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async def verify_capture_deadline(awaitable, *, timeout):
            # This test checks argument routing, not interpreter startup speed.
            # The real-clock termination test above separately enforces 0.5s.
            try:
                assert timeout == 0.5, "leading float must set the capture deadline"
            finally:
                # Consume the real collector even if the assertion fails, so
                # a mutation cannot leave an unawaited coroutine or live child.
                result = await awaitable
            return result

        monkeypatch.setattr(asyncio, "wait_for", verify_capture_deadline)
        result = await _action_functions().run_capture(
            0.5, sys.executable, str(HELPER), "emit", "completed"
        )
        assert result == "completed"

    @pytest.mark.asyncio
    async def test_huge_positive_timeout_int_clamps_to_ceiling_and_runs(self):
        result = await _action_functions().run_capture(
            10**400, sys.executable, str(HELPER), "emit", "ceiling clamped"
        )
        assert result == "ceiling clamped"

    @pytest.mark.asyncio
    async def test_huge_negative_timeout_int_clamps_to_floor(self):
        with pytest.raises(TimeoutError):
            await _action_functions().run_capture(
                -(10**400), sys.executable, str(HELPER), "sleep", "0.25"
            )

    @pytest.mark.asyncio
    async def test_leading_numeric_string_is_the_program_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PATH", "")
        for program in ("5", "0.5"):
            with pytest.raises(FileNotFoundError, match="program was not found"):
                await _action_functions().run_capture(
                    program, sys.executable, str(HELPER), "emit", "must not run"
                )

    @pytest.mark.asyncio
    async def test_boolean_first_parameter_is_rejected_as_an_invalid_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """TOML true is never converted to the text ``True`` for a child."""
        start_child = AsyncMock(side_effect=AssertionError("child started"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", start_child)
        with pytest.raises(ValueError, match="str, int, or float"):
            await _action_functions().run_capture(
                True, sys.executable, str(HELPER), "emit", "must not run"
            )
        start_child.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_numeric_first_parameter_is_the_program(self):
        result = await _action_functions().run_capture(
            sys.executable, str(HELPER), "emit", "program first"
        )
        assert result == "program first"

    @pytest.mark.asyncio
    async def test_leading_timeout_clamps_to_floor(self):
        with pytest.raises(TimeoutError):
            await _action_functions().run_capture(
                0, sys.executable, str(HELPER), "sleep", "0.25"
            )

    @pytest.mark.asyncio
    async def test_leading_timeout_clamps_to_ceiling_without_rejecting_program(self):
        result = await _action_functions().run_capture(
            61, sys.executable, str(HELPER), "emit", "ceiling accepted"
        )
        assert result == "ceiling accepted"

    @pytest.mark.asyncio
    async def test_stderr_is_redacted_in_success_and_failure_logs(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ):
        secret = "sensitive-captured-output"
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        with caplog.at_level(logging.DEBUG, logger="speech.actions"):
            await _action_functions().run_capture(
                sys.executable, str(HELPER), "stderr_success", secret
            )
            with pytest.raises(RuntimeError):
                await _action_functions().run_capture(
                    sys.executable, str(HELPER), "stderr_failure", secret, "3"
                )
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert secret not in messages
        assert "<redacted:" in messages


class TestRunCaptureEngine:
    @pytest.mark.asyncio
    async def test_stores_capture_under_custom_result_for_later_insert(
        self, parser: TextParser, mock_app: MagicMock
    ):
        match = re.fullmatch(r"capture", "capture")
        steps = [
            {
                "function": "run_capture",
                "params": [sys.executable, str(HELPER), "emit", "stored value\n"],
                "result": "answer",
            },
            {"function": "insert_text", "params": ["answer"]},
        ]
        assert await parser._execute_rule(match, steps, validation_group=None) is True
        payload = mock_app.send_command.call_args.args[0]
        assert payload["params"]["insertion_string"] == "stored value"

    @pytest.mark.asyncio
    async def test_failed_run_capture_stops_remaining_steps(
        self, parser: TextParser, mock_app: MagicMock, evidence_tmp_dir: Path
    ):
        missing_program = str(evidence_tmp_dir / "not-present.exe")
        match = re.fullmatch(r"capture", "capture")
        steps = [
            {"function": "run_capture", "params": [missing_program]},
            {"function": "insert_text", "params": ["must not send"]},
        ]
        assert await parser._execute_rule(match, steps, validation_group=None) is False
        mock_app.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_unmatched_optional_capture_stops_later_steps(
        self, parser: TextParser, mock_app: MagicMock
    ):
        match = re.fullmatch(r"look up(?: (.+))?", "look up")
        assert match is not None
        steps = [
            {
                "function": "run_capture",
                "params": [sys.executable, str(HELPER), "emit", "g1"],
                "result": "answer",
            },
            {"function": "insert_text", "params": ["answer"]},
        ]

        assert await parser._execute_rule(match, steps, validation_group=None) is False
        mock_app.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_repeated_high_utf8_capture_over_final_payload_is_step_failure(
        self, catalog: PatternCatalog
    ):
        class FinalPayloadApp:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            async def send_command(self, payload: dict[str, object]) -> None:
                if len(pickle.dumps(payload)) > 65_532:
                    raise ValueError("final UI payload exceeds shared memory capacity")
                self.sent.append(payload)

            async def send_request(self, *args: object, **kwargs: object) -> dict[str, str]:
                return {"status": "success"}

        app = FinalPayloadApp()
        handler = MagicMock()
        handler.app = app
        handler.config_service.get.return_value = {
            "output_cap_chars": OUTPUT_CAP_CEILING_CHARS
        }
        handler.speech_processor = None
        parser = TextParser(handler, catalog)
        match = re.fullmatch(r"capture", "capture")
        steps = [
            {
                "function": "run_capture",
                "params": [
                    sys.executable,
                    str(HELPER),
                    "unicode",
                    str(OUTPUT_CAP_CEILING_CHARS),
                ],
                "result": "answer",
            },
            {"function": "insert_text", "params": ["answeranswer"]},
            {"function": "insert_text", "params": ["must not send"]},
        ]
        assert await parser._execute_rule(match, steps, validation_group=None) is False
        assert app.sent == []


class TestRunCaptureCatalog:
    def test_catalog_describes_variadic_capture_command(self):
        entry = next(item for item in ACTION_CATALOG if item["name"] == "run_capture")
        assert entry["audience"] == "advanced"
        assert [param["name"] for param in entry["params"]] == [
            "timeout", "program", "argument",
        ]
        assert "repeatable" in entry["params"][2]["summary"].lower()
        assert "timeout" in entry["summary"].lower()
        assert "toml number" in entry["params"][0]["summary"].lower()
        timeout_summary = entry["params"][0]["summary"].lower()
        # actions.py pops the first parameter only for an unquoted int or
        # finite float; a quoted number stays and becomes the program path.
        assert "a quoted number is not a timeout" in timeout_summary
        assert "program path" in timeout_summary
        assert "passed to the program as an argument" not in timeout_summary
