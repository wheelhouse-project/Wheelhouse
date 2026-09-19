"""The punctuation-name gate must not read a captured log line as an ERROR.

wh-spaced-punctuation-names-unresolved.3. ``run_defect`` refuses a run a
verdict when ``error_names`` finds anything, which is right: pytest
reports an ERROR rather than a FAILED when a fixture raises or a
collection fails, and either would otherwise be read as a verdict.

The reader was too loose. It matched every line starting with ``ERROR ``,
and a captured log record starts that way too::

    ERROR    speech.command_engine:command_engine.py:574 Rule execution
    error: object MagicMock can't be used in 'await' expression

``_test_id`` splits on ``::``, found none, and returned the first token,
which is the word ERROR itself. The full sweep on 2026-09-05 reported

    ERROR the-new-utterance-guard-has-no-exception:
        pytest reported ERROR records: ['ERROR']

for a mutation whose six expected catchers had all failed on their own
assertions in that same run. The mutation was caught; only the verdict
was withheld. An isolated measurement could not see it, because that
probe read only ``failed_names``.

``_is_error_summary`` is what separates the two, so it is guard code and
it is tested here. The discriminator: pytest's summary forms name a path
carrying ``.py`` and either use ``::`` between node ids or carry no colon
at all, while a captured log record is always
``<logger>:<file>:<lineno>`` and so has single colons and no ``::``.

These tests call the gate's own functions with prepared strings. They
run no pytest, apply no mutation, and write no file.
"""

import importlib.util
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parent / (
    "mutation_gate_punctuation_name_body.py"
)

# The exact line from the 2026-09-05 sweep, kept verbatim. A paraphrase
# would not prove the case that actually happened.
_CAPTURED_LOG_LINE = (
    "ERROR    speech.command_engine:command_engine.py:574 Rule execution "
    "error: object MagicMock can't be used in 'await' expression"
)


def _load_gate():
    """Import the gate script by path.

    The file deliberately has no ``test_`` prefix, so pytest never
    collects it and a plain import statement cannot reach it either.
    """
    spec = importlib.util.spec_from_file_location(
        "_punctuation_name_gate_error_summary", _GATE_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"could not build an import spec for {_GATE_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


class TestTheErrorSummaryDiscriminator:
    """Every line pytest really reports, and every one it does not."""

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(
                "ERROR tests/test_x.py::TestC::test_y - Exception: msg",
                id="node-id-with-reason",
            ),
            pytest.param(
                "ERROR tests/test_x.py::TestC::test_y",
                id="node-id-alone",
            ),
            pytest.param(
                "ERROR tests/test_x.py",
                id="collection-error-bare-path",
            ),
            pytest.param(
                "ERROR tests/test_x.py - ImportError: no module named q",
                id="collection-error-with-reason",
            ),
        ],
    )
    def test_a_real_pytest_error_line_is_accepted(self, gate, line):
        """Both summary forms must still be read, with or without a reason.

        This is the half that must not be lost. Tightening the reader to
        silence a false error would be worse than the false error, because
        a genuine fixture or collection failure would then read as a
        verdict.
        """
        assert gate._is_error_summary(line) is True, line

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(_CAPTURED_LOG_LINE, id="captured-log-record"),
            pytest.param(
                "ERROR    wheelhouse.pipeline:speech_processor.py:1202 held",
                id="captured-log-record-pipeline",
            ),
            pytest.param("ERRORS", id="word-that-merely-starts-the-same"),
            pytest.param(
                "FAILED tests/test_x.py::test_y", id="a-failed-line"
            ),
            pytest.param("", id="empty-line"),
        ],
    )
    def test_a_line_that_is_not_a_pytest_error_is_refused(self, gate, line):
        assert gate._is_error_summary(line) is False, line

    def test_the_captured_log_line_yields_no_test_id(self, gate):
        """The whole point, stated through the function run_defect calls.

        Before the fix this returned {'ERROR'}, and run_defect turned that
        into "pytest reported ERROR records: ['ERROR']".
        """
        assert gate.error_names(_CAPTURED_LOG_LINE) == set()

    def test_a_real_error_line_still_yields_its_test_id(self, gate):
        assert gate.error_names(
            "ERROR tests/test_x.py::TestC::test_y - Boom"
        ) == {"test_y"}

    def test_the_per_test_form_is_untouched(self, gate):
        """The -v per-test line is the reader's other clause.

        It matches on ``::`` already, so no captured log record can reach
        it. It is asserted here so a later tightening of the summary
        clause cannot quietly take this one with it.
        """
        assert gate.error_names(
            "tests/test_x.py::TestC::test_y ERROR    [ 50%]"
        ) == {"test_y"}

    def test_a_run_with_only_a_captured_log_line_earns_a_verdict(self, gate):
        """run_defect is the caller that matters, so assert through it."""

        class _Result:
            returncode = 1
            stdout = (
                "tests/test_x.py::test_y FAILED    [100%]\n"
                + _CAPTURED_LOG_LINE
                + "\n=== short test summary info ===\n"
                "FAILED tests/test_x.py::test_y - AssertionError\n"
            )
            stderr = ""

        assert gate.run_defect(_Result()) is None

    def test_a_run_with_a_real_error_record_earns_no_verdict(self, gate):
        """The same shape, with a genuine ERROR record, must still refuse."""

        class _Result:
            returncode = 1
            stdout = (
                "=== short test summary info ===\n"
                "ERROR tests/test_x.py::TestC::test_y - RuntimeError\n"
            )
            stderr = ""

        defect = gate.run_defect(_Result())
        assert defect is not None and "test_y" in defect, defect
