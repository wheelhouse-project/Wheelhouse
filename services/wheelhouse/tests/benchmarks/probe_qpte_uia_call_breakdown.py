"""Micro-benchmark: per-call latency breakdown of preceding-text-before-caret on QPlainTextEdit.

Scenario 6 of the Phase 1 fidelity probe (wh-w55k5) showed a flat ~500 ms
latency for the GetSelection + DocumentRange.Clone + MoveEndpointByRange +
GetText sequence on Qt's UIA backend, at every document size. The same
sequence against Notepad runs in 0.4 ms.

This script times each COM call individually so we can see which one is
the 500 ms culprit, and compares against TextPattern2.GetCaretRange as
an alternative caret-locating method.

Usage:
    uv run python tests/benchmarks/probe_qpte_uia_call_breakdown.py
"""
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import _ctypes  # noqa: E402
import uiautomation as auto  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QPlainTextEdit, QDialog, QVBoxLayout, QLabel,
)


ITER = 30


def stats(label: str, samples_ms: list[float]) -> str:
    if not samples_ms:
        return f"  {label}: no samples"
    s = sorted(samples_ms)
    n = len(s)
    p50 = s[n // 2]
    p95 = s[min(n - 1, int(0.95 * (n - 1)))]
    p99 = s[min(n - 1, int(0.99 * (n - 1)))]
    return (f"  {label:38s}  n={n}  min={min(s):.3f}ms  p50={p50:.3f}ms  "
            f"mean={statistics.mean(s):.3f}ms  p95={p95:.3f}ms  p99={p99:.3f}ms  max={max(s):.3f}ms")


def time_call(label: str, fn, results: dict[str, list[float]]) -> "tuple[float, object]":
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001
        out = e
    elapsed_ms = (time.perf_counter() - t0) * 1000
    results.setdefault(label, []).append(elapsed_ms)
    return elapsed_ms, out


def run_benchmark(hwnd: int) -> None:
    print(f"\nMicro-benchmark on HWND {hwnd}\n")

    results: dict[str, list[float]] = {}

    for _ in range(ITER):
        # Each iteration walks the full sequence and times each call.
        _, ctrl = time_call("ControlFromHandle", lambda: auto.ControlFromHandle(hwnd), results)
        if ctrl is None or isinstance(ctrl, Exception):
            print(f"ControlFromHandle returned: {ctrl!r}")
            return

        _, tp = time_call("GetPattern(TextPattern)",
                          lambda: ctrl.GetPattern(auto.PatternId.TextPattern), results)
        if tp is None or isinstance(tp, Exception):
            print(f"GetPattern returned: {tp!r}")
            return

        _, sel_ranges = time_call("GetSelection()", lambda: tp.GetSelection(), results)
        if isinstance(sel_ranges, Exception) or not sel_ranges:
            print(f"GetSelection returned: {sel_ranges!r}")
            return
        sel_range = sel_ranges[0]

        _, doc_range = time_call("DocumentRange (property)",
                                 lambda: tp.DocumentRange, results)
        if isinstance(doc_range, Exception):
            print(f"DocumentRange: {doc_range!r}")
            return

        _, pre_range = time_call("DocumentRange.Clone()",
                                 lambda: doc_range.Clone(), results)
        if isinstance(pre_range, Exception):
            print(f"Clone: {pre_range!r}")
            return

        _, _ = time_call(
            "MoveEndpointByRange(End->sel.Start)",
            lambda: pre_range.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                sel_range,
                auto.TextPatternRangeEndpoint.Start,
            ),
            results,
        )

        _, text = time_call("preceding range.GetText(-1)",
                            lambda: pre_range.GetText(-1), results)
        if isinstance(text, Exception):
            print(f"GetText preceding: {text!r}")
            return

        _, _full = time_call("DocumentRange.GetText(-1)",
                             lambda: doc_range.GetText(-1), results)

        # TextPattern2 alternative path (for comparison)
        _, tp2 = time_call("GetPattern(TextPattern2)",
                           lambda: ctrl.GetPattern(auto.PatternId.TextPattern2), results)
        caret_range = None
        if tp2 is not None and not isinstance(tp2, Exception):
            raw = tp2.pattern  # raw COM pointer
            if hasattr(raw, "GetCaretRange"):
                try:
                    _, caret_result = time_call(
                        "TextPattern2.GetCaretRange",
                        lambda: raw.GetCaretRange(),
                        results,
                    )
                    if isinstance(caret_result, tuple) and len(caret_result) == 2:
                        _, caret_range = caret_result
                except _ctypes.COMError as e:
                    print(f"TextPattern2.GetCaretRange COM error: {e}")

        # Variant A: MoveEndpointByRange against caret_range (not sel_range).
        # Tests whether the slowness is in the operation or in sel_range
        # specifically.
        if caret_range is not None and not isinstance(caret_range, Exception):
            pre_b = doc_range.Clone()
            _, _ = time_call(
                "MoveEndpointByRange(End->caret.Start)",
                lambda: pre_b.MoveEndpointByRange(
                    auto.TextPatternRangeEndpoint.End,
                    caret_range,
                    auto.TextPatternRangeEndpoint.Start,
                ),
                results,
            )

            # Variant B: production-friendly path. Clone the caret range
            # and move ITS Start endpoint backward N characters via
            # MoveEndpointByUnit. No range-to-range arithmetic.
            small_pre = caret_range.Clone()
            _, _ = time_call(
                "MoveEndpointByUnit(Start,Char,-2)",
                lambda: small_pre.MoveEndpointByUnit(
                    auto.TextPatternRangeEndpoint.Start,
                    auto.TextUnit.Character,
                    -2,
                ),
                results,
            )
            _, _ = time_call(
                "small_pre.GetText(-1)",
                lambda: small_pre.GetText(-1),
                results,
            )

    print("Per-call latency:\n")
    # Print in the order operations appear in the sequence
    order = [
        "ControlFromHandle",
        "GetPattern(TextPattern)",
        "GetSelection()",
        "DocumentRange (property)",
        "DocumentRange.Clone()",
        "MoveEndpointByRange(End->sel.Start)",
        "MoveEndpointByRange(End->caret.Start)",
        "MoveEndpointByUnit(Start,Char,-2)",
        "small_pre.GetText(-1)",
        "preceding range.GetText(-1)",
        "DocumentRange.GetText(-1)",
        "GetPattern(TextPattern2)",
        "TextPattern2.GetCaretRange",
    ]
    for label in order:
        if label in results:
            print(stats(label, results[label]))


def _build_pre_via_tp2(doc_range, caret_range):
    """Mimic preceding-text via TextPattern2: clone doc, end at caret."""
    pre = doc_range.Clone()
    pre.MoveEndpointByRange(
        auto.TextPatternRangeEndpoint.End,
        caret_range,
        auto.TextPatternRangeEndpoint.Start,
    )
    return pre.GetText(-1)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    dialog = QDialog()
    dialog.setWindowTitle("QPTE UIA Call Breakdown")
    dialog.resize(700, 400)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("Benchmark running -- do not interact."))
    edit = QPlainTextEdit()
    edit.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
    layout.addWidget(edit)

    # Load 10 KB content with caret in the middle (matches the slow case)
    text = ("abcdefghij " * 1000)[:10240]
    text = "\n".join(text[i:i+80] for i in range(0, len(text), 80))
    edit.setPlainText(text)
    cur = edit.textCursor()
    cur.setPosition(len(text) // 2)
    edit.setTextCursor(cur)

    dialog.show()
    QApplication.processEvents()
    hwnd = int(edit.winId())

    def go():
        run_benchmark(hwnd)
        QApplication.quit()

    QTimer.singleShot(300, go)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
