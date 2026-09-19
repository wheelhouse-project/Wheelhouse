"""One command that runs the STT CPU-load test (wh-load-test-script).

docs/testing/stt-cpu-load-test-procedure.md is the reading guide; this
package is the run. Invoke it from services/stt_providers/shared:

    uv run python -m tools.stt_load_test
    uv run python -m tools.stt_load_test --baseline

The split is deliberate. script.py, logparse.py, judge.py and report.py are
pure: they hold the six fixed sentences, turn the provider's log lines back
into numbers, judge each sentence and compute the verdict, and none of them
touches hardware, the clock, or a subprocess. record.py and run.py hold
everything that does.
"""
