"""Probe: Qt QPlainTextEdit UIA TextPattern fidelity under realistic load.

Validates Phase 1 of wh-u3tj2 (terminal editor UIA refactor). Runs a
PySide6 QPlainTextEdit through six stress scenarios while a separate
worker thread reads the widget via Windows UI Automation TextPattern.
Mirrors the real Input-Process / GUI-Process split: the Qt main thread
owns the widget, the worker thread runs UIA queries.

Usage:
    uv run python tests/benchmarks/probe_qpte_uia_fidelity.py

Output:
    Prints scenario-by-scenario pass/fail and latency stats. On exit,
    writes a markdown fidelity report to:
        ../../docs/design/benchmarks/<timestamp>-qpte-uia-fidelity.md
    relative to the repo root.

Scenarios:
    1. Multi-line content (1 KB / 10 KB / 100 KB)
    2. Unicode (combining marks, emoji, CJK, RTL)
    3. Rapid Qt appends interleaved with SendInput keystrokes
    4. IME composition state visibility
    5. Rapid programmatic caret moves (SetSelection cycling)
    6. Latency p50/p95/p99 for GetText and preceding-text-before-caret

Acceptance (per wh-w55k5):
    - All six scenarios pass, OR a failure is documented.
    - p99 preceding-text-before-caret <= 5 ms at 10 KB.
"""
import ctypes
import json
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# Add wheelhouse service root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import _ctypes  # noqa: E402
import uiautomation as auto  # noqa: E402
from PySide6.QtCore import Qt, QTimer, QObject, Signal  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QPlainTextEdit, QDialog, QVBoxLayout, QLabel,
)


REPORT_DIR = Path(__file__).resolve().parents[4] / "docs" / "design" / "benchmarks"
PRECEDING_LATENCY_BUDGET_MS = 5.0  # acceptance criterion at 10 KB


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------

@dataclass
class LatencyStats:
    n: int
    min_ms: float
    p50_ms: float
    mean_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @classmethod
    def from_samples_ms(cls, samples: list[float]) -> "LatencyStats":
        s = sorted(samples)
        n = len(s)
        def pct(p: float) -> float:
            if n == 0:
                return 0.0
            idx = min(n - 1, int(round(p * (n - 1))))
            return s[idx]
        return cls(
            n=n,
            min_ms=s[0] if n else 0.0,
            p50_ms=pct(0.50),
            mean_ms=statistics.mean(s) if n else 0.0,
            p95_ms=pct(0.95),
            p99_ms=pct(0.99),
            max_ms=s[-1] if n else 0.0,
        )

    def fmt(self) -> str:
        return (
            f"n={self.n} min={self.min_ms:.2f}ms p50={self.p50_ms:.2f}ms "
            f"p95={self.p95_ms:.2f}ms p99={self.p99_ms:.2f}ms max={self.max_ms:.2f}ms"
        )


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    latency: LatencyStats | None = None

    def add_note(self, msg: str) -> None:
        self.notes.append(msg)

    def fail(self, msg: str) -> None:
        self.passed = False
        self.notes.append(f"FAIL: {msg}")


# ---------------------------------------------------------------------------
# Worker thread: runs UIA queries in its own COM apartment
# ---------------------------------------------------------------------------

class UiaWorker:
    """Direct in-process UIA caller.

    The original design used a dedicated worker thread to mirror the
    production GUI-Process / Input-Process split. That deadlocked: the
    worker thread (MTA COM apartment) called ControlFromHandle on an HWND
    owned by the Qt main thread (STA). COM marshaled the call back to the
    STA, but the STA was blocked waiting for the worker to return.

    Production avoids this because the GUI Process and Input Process pump
    message loops independently. Within a single process we cannot mirror
    that without pumping Qt events while waiting -- which adds its own
    races. The probe uses direct calls instead. Cross-process fidelity is
    a separate concern that scenario 7 (Phase 3 end-to-end) will cover.
    """

    def __init__(self) -> None:
        # COM initialization happens lazily on first call so any failure
        # surfaces from main(), not from constructor.
        self._initialized = False

    def _ensure_init(self) -> None:
        if not self._initialized:
            # uiautomation initializes COM lazily when first called; we do
            # not need an explicit init here because all calls run on the
            # Qt main thread which Qt has already initialized as STA.
            self._initialized = True

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        self._ensure_init()
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            return e

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# UIA query primitives (run inside worker thread)
# ---------------------------------------------------------------------------

def _hwnd_text_pattern(hwnd: int):
    """Return the (control, TextPattern) for an HWND, or (None, None)."""
    ctrl = auto.ControlFromHandle(hwnd)
    if not ctrl:
        return None, None
    tp = ctrl.GetPattern(auto.PatternId.TextPattern)
    return ctrl, tp


def uia_get_full_text(hwnd: int) -> str | None:
    _, tp = _hwnd_text_pattern(hwnd)
    if tp is None:
        return None
    return tp.DocumentRange.GetText(-1)


def uia_get_preceding_text(hwnd: int, max_chars: int = -1) -> str | None:
    """Return text from doc start to caret. -1 means no truncation."""
    _, tp = _hwnd_text_pattern(hwnd)
    if tp is None:
        return None
    try:
        sel_ranges = tp.GetSelection()
    except _ctypes.COMError:
        return None
    if not sel_ranges:
        return None
    sel_range = sel_ranges[0]
    pre_range = tp.DocumentRange.Clone()
    pre_range.MoveEndpointByRange(
        auto.TextPatternRangeEndpoint.End,
        sel_range,
        auto.TextPatternRangeEndpoint.Start,
    )
    text = pre_range.GetText(-1)
    if max_chars >= 0:
        return text[-max_chars:]
    return text


def uia_get_selection_text(hwnd: int) -> tuple[str, bool] | None:
    _, tp = _hwnd_text_pattern(hwnd)
    if tp is None:
        return None
    try:
        sel = tp.GetSelection()
    except _ctypes.COMError:
        return None
    if not sel:
        return None
    text = sel[0].GetText(-1)
    return text, len(text) > 0


def uia_target_class(hwnd: int) -> str | None:
    ctrl = auto.ControlFromHandle(hwnd)
    return ctrl.ClassName if ctrl else None


# ---------------------------------------------------------------------------
# SendInput helper for scenario 3 (independent of WheelHouse code paths)
# ---------------------------------------------------------------------------

# Minimal SendInput shim. We only need Unicode keystrokes here -- no
# virtual key mapping, no modifier handling.
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class _KbdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KbdInput)]


class _Input(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _InputUnion),
    ]


def send_unicode_char(ch: str) -> None:
    code = ord(ch)
    down = _Input(type=INPUT_KEYBOARD)
    down.u.ki = _KbdInput(0, code, KEYEVENTF_UNICODE, 0, None)
    up = _Input(type=INPUT_KEYBOARD)
    up.u.ki = _KbdInput(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
    n = ctypes.windll.user32.SendInput(2, ctypes.byref((_Input * 2)(down, up)),
                                        ctypes.sizeof(_Input))
    if n != 2:
        raise OSError(f"SendInput returned {n}")


# ---------------------------------------------------------------------------
# Probe harness (runs inside the Qt event loop)
# ---------------------------------------------------------------------------

class Probe(QObject):
    finished = Signal()

    def __init__(self, dialog: QDialog, edit: QPlainTextEdit, worker: UiaWorker):
        super().__init__()
        self.dialog = dialog
        self.edit = edit
        self.worker = worker
        self.hwnd: int = 0
        self.results: list[ScenarioResult] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._steps = [
            self._scenario_1_multiline,
            self._scenario_2_unicode,
            self._scenario_6_latency,    # run latency before concurrency stress
            self._scenario_5_setselect,
            self._scenario_3_concurrent,
            self._scenario_4_ime,
            self._wrap_up,
        ]
        self._step_idx = 0

    def start(self) -> None:
        self.dialog.show()
        # winId() materializes the native HWND when WA_NativeWindow is set
        self.hwnd = int(self.edit.winId())
        # Smoke-check: verify we can find the QPlainTextEdit via UIA before
        # starting scenarios. Surfaces target-binding failures early.
        cls = self.worker.call(uia_target_class, self.hwnd)
        print(f"UIA target class for editor HWND {self.hwnd}: {cls!r}", flush=True)
        QTimer.singleShot(300, self._run_next)

    def _run_next(self) -> None:
        if self._step_idx >= len(self._steps):
            self.finished.emit()
            return
        step = self._steps[self._step_idx]
        self._step_idx += 1
        try:
            step()
        except Exception as e:  # noqa: BLE001
            self.results.append(ScenarioResult(
                name=getattr(step, "__name__", "unknown"),
                passed=False,
                notes=[f"EXCEPTION: {e!r}"],
            ))
        # Write report after every scenario so partial runs leave evidence
        try:
            write_report(self.results, partial=(self._step_idx < len(self._steps) - 1))
        except Exception as e:  # noqa: BLE001
            print(f"  [report-write-failed] {e!r}", flush=True)
        QTimer.singleShot(50, self._run_next)

    @staticmethod
    def _print_result(r: ScenarioResult) -> None:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}", flush=True)
        for note in r.notes:
            print(f"    {note}", flush=True)
        if r.latency:
            print(f"    latency: {r.latency.fmt()}", flush=True)

    # ------- helpers -----------------------------------------------------

    def _set_text(self, text: str) -> None:
        self.edit.setPlainText(text)
        QApplication.processEvents()

    def _move_caret_to(self, pos: int) -> None:
        cur = self.edit.textCursor()
        cur.setPosition(pos)
        self.edit.setTextCursor(cur)
        QApplication.processEvents()

    def _refocus(self) -> None:
        self.dialog.raise_()
        self.dialog.activateWindow()
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        QApplication.processEvents()
        time.sleep(0.05)

    # ------- scenario 1: multi-line content -----------------------------

    def _scenario_1_multiline(self) -> None:
        r = ScenarioResult(name="1. Multi-line content (1 KB / 10 KB / 100 KB)", passed=True)
        sizes = [(1_024, "1 KB"), (10_240, "10 KB"), (102_400, "100 KB")]
        for nbytes, label in sizes:
            line = "The quick brown fox jumps over the lazy dog. "
            text = (line * (nbytes // len(line) + 1))[:nbytes]
            # Insert newlines every ~80 chars to make it multi-line
            text = "\n".join(text[i:i+80] for i in range(0, len(text), 80))
            self._set_text(text)
            mid = len(text) // 2
            self._move_caret_to(mid)
            self._refocus()

            full = self.worker.call(uia_get_full_text, self.hwnd)
            preceding = self.worker.call(uia_get_preceding_text, self.hwnd)

            if not isinstance(full, str):
                r.fail(f"{label}: GetText returned {full!r}")
                continue
            if full != text:
                # Newlines may be normalized to \r\n by UIA
                norm = full.replace("\r\n", "\n")
                if norm != text:
                    r.fail(
                        f"{label}: GetText body differs "
                        f"(len got={len(full)} want={len(text)})"
                    )
                    continue
                r.add_note(f"{label}: GetText OK (UIA normalized \\n -> \\r\\n)")
            else:
                r.add_note(f"{label}: GetText OK ({len(full)} chars)")

            if not isinstance(preceding, str):
                r.fail(f"{label}: preceding returned {preceding!r}")
                continue
            expected_pre = text[:mid]
            got_pre = preceding.replace("\r\n", "\n")
            if got_pre != expected_pre:
                r.fail(
                    f"{label}: preceding mismatch "
                    f"(len got={len(got_pre)} want={len(expected_pre)})"
                )
            else:
                r.add_note(f"{label}: preceding-text-before-caret OK")

        self.results.append(r)
        self._print_result(r)

    # ------- scenario 2: Unicode ----------------------------------------

    def _scenario_2_unicode(self) -> None:
        r = ScenarioResult(name="2. Unicode (combining / emoji / CJK / RTL)", passed=True)
        cases = [
            ("combining", "Café vs Café"),                 # NFD vs NFC
            ("emoji_bmp", "smile ☺ end"),                         # BMP emoji
            ("emoji_supp", "rocket \U0001F680 end"),                   # surrogate pair
            ("zwj_family", "family \U0001F468‍\U0001F469‍\U0001F466 end"),
            ("cjk", "中文文本 hello"),
            ("rtl_hebrew", "shalom שלום end"),
            ("rtl_arabic", "salam السلام end"),
        ]
        for label, text in cases:
            self._set_text(text)
            self._move_caret_to(len(text))
            self._refocus()
            full = self.worker.call(uia_get_full_text, self.hwnd)
            preceding = self.worker.call(uia_get_preceding_text, self.hwnd)
            if not isinstance(full, str):
                r.fail(f"{label}: GetText returned {full!r}")
                continue
            full_norm = full.replace("\r\n", "\n").rstrip("\r\n")
            if full_norm != text:
                r.fail(
                    f"{label}: full text mismatch "
                    f"(got len={len(full_norm)}, want len={len(text)}; "
                    f"got_repr={full_norm!r})"
                )
                continue
            if not isinstance(preceding, str):
                r.fail(f"{label}: preceding returned {preceding!r}")
                continue
            pre_norm = preceding.replace("\r\n", "\n")
            if pre_norm != text:
                # Caret may report at end-1 for trailing newline; trim and recheck
                if pre_norm.rstrip("\n") != text.rstrip("\n"):
                    r.fail(
                        f"{label}: preceding mismatch "
                        f"(got len={len(pre_norm)}, want len={len(text)})"
                    )
                    continue
            r.add_note(f"{label}: OK (Python len={len(text)}, UIA len={len(full_norm)})")
        self.results.append(r)
        self._print_result(r)

    # ------- scenario 3: rapid concurrent appends + keystrokes ----------

    def _scenario_3_concurrent(self) -> None:
        r = ScenarioResult(
            name="3. Concurrent Qt appends + SendInput keystrokes + UIA reads",
            passed=True,
        )
        # Empty the buffer; the test will fill it.
        self._set_text("")
        self._refocus()

        truncated_or_interleaved = 0
        decreasing = 0
        latencies_ms: list[float] = []
        last_len = -1

        # Qt-thread append driver: appends one numbered word every 100 ms
        append_count = {"n": 0}

        def append_word():
            n = append_count["n"]
            self.edit.moveCursor(QTextCursor.MoveOperation.End)
            self.edit.insertPlainText(f"q{n} ")
            append_count["n"] = n + 1

        timer = QTimer()
        timer.timeout.connect(append_word)
        timer.start(100)  # 10 Hz appends

        # SendInput driver thread: types a character every ~150 ms
        kb_stop = threading.Event()

        def kb_loop():
            while not kb_stop.is_set():
                try:
                    send_unicode_char("k")
                except Exception:
                    pass
                kb_stop.wait(0.15)

        kb_thread = threading.Thread(target=kb_loop, daemon=True)
        kb_thread.start()

        # Run for ~3 seconds. Pump the Qt event loop, do UIA reads via worker.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            QApplication.processEvents()
            t0 = time.perf_counter()
            text = self.worker.call(uia_get_full_text, self.hwnd)
            elapsed = (time.perf_counter() - t0) * 1000
            latencies_ms.append(elapsed)
            if not isinstance(text, str):
                truncated_or_interleaved += 1
                continue
            cur_len = len(text)
            if last_len >= 0 and cur_len < last_len - 5:
                # tolerate tiny shrinkage from \r\n normalization
                decreasing += 1
            last_len = cur_len
            time.sleep(0.02)

        timer.stop()
        kb_stop.set()
        kb_thread.join(timeout=1.0)

        stats = LatencyStats.from_samples_ms(latencies_ms)
        r.latency = stats
        r.metrics["truncated_or_none_reads"] = truncated_or_interleaved
        r.metrics["non_monotonic_reads"] = decreasing
        r.metrics["final_len"] = last_len
        r.metrics["qt_appends"] = append_count["n"]

        if truncated_or_interleaved > 0:
            r.fail(f"{truncated_or_interleaved} reads returned None/error")
        if decreasing > 0:
            r.fail(f"{decreasing} reads showed non-monotonic shrinkage")
        if stats.p99_ms > PRECEDING_LATENCY_BUDGET_MS * 4:
            # 4x budget is a soft ceiling for full-text reads under contention
            r.add_note(
                f"WARN: full-text p99={stats.p99_ms:.2f}ms exceeds 4x "
                f"preceding-text budget"
            )
        r.add_note(f"latency: {stats.fmt()}")
        self.results.append(r)
        self._print_result(r)

    # ------- scenario 4: IME composition --------------------------------

    def _scenario_4_ime(self) -> None:
        r = ScenarioResult(
            name="4. IME composition state visibility",
            passed=True,
        )
        # We cannot programmatically force a real IME composition from
        # Python in a portable way without driving IMM32 directly. We
        # exercise QInputMethodEvent so Qt sees a composition string;
        # whether UIA exposes it is what we want to know.
        from PySide6.QtGui import QInputMethodEvent

        self._set_text("prefix ")
        self._move_caret_to(len("prefix "))
        self._refocus()

        ev = QInputMethodEvent("こんに", [])  # "konni" hiragana
        QApplication.sendEvent(self.edit, ev)
        QApplication.processEvents()

        full_during = self.worker.call(uia_get_full_text, self.hwnd)
        pre_during = self.worker.call(uia_get_preceding_text, self.hwnd)

        # Commit the composition
        commit = QInputMethodEvent()
        commit.setCommitString("こんにちは")  # "konnichiwa"
        QApplication.sendEvent(self.edit, commit)
        QApplication.processEvents()

        full_after = self.worker.call(uia_get_full_text, self.hwnd)

        r.metrics["full_during_composition"] = repr(full_during)
        r.metrics["preceding_during_composition"] = repr(pre_during)
        r.metrics["full_after_commit"] = repr(full_after)
        r.add_note(
            "Composition visibility documented; pass/fail depends on "
            "downstream design. Findings recorded in metrics."
        )
        # Soft-pass: this scenario records behaviour, does not assert correctness
        self.results.append(r)
        self._print_result(r)

    # ------- scenario 5: rapid SetSelection updates ---------------------

    def _scenario_5_setselect(self) -> None:
        r = ScenarioResult(
            name="5. Rapid programmatic caret moves (SetSelection cycling)",
            passed=True,
        )
        text = ("abcdefghij " * 100).strip()  # ~1100 chars
        self._set_text(text)
        self._refocus()

        mismatches = 0
        timeouts = 0
        latencies_ms: list[float] = []
        positions = [10, 250, 500, 750, 1000, 50, 800, 200, 999, 1]
        # Bail out after 5 timeouts to avoid 10-second hangs piling up
        TIMEOUT_BUDGET = 5
        TIMEOUT_THRESHOLD_MS = 1000.0
        for _ in range(5):  # 50 total reads instead of 200
            if timeouts >= TIMEOUT_BUDGET:
                r.add_note(f"early-bailed after {timeouts} read timeouts")
                break
            for pos in positions:
                self._move_caret_to(pos)
                t0 = time.perf_counter()
                pre = self.worker.call(uia_get_preceding_text, self.hwnd)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                latencies_ms.append(elapsed_ms)
                if elapsed_ms > TIMEOUT_THRESHOLD_MS:
                    timeouts += 1
                if not isinstance(pre, str):
                    mismatches += 1
                    continue
                pre_norm = pre.replace("\r\n", "\n")
                expected = text[:pos]
                if pre_norm != expected:
                    mismatches += 1
        stats = LatencyStats.from_samples_ms(latencies_ms)
        r.latency = stats
        r.metrics["mismatches"] = mismatches
        r.metrics["timeouts_over_1s"] = timeouts
        r.metrics["total_reads"] = len(latencies_ms)
        if mismatches > 0:
            r.fail(f"{mismatches}/{len(latencies_ms)} reads mismatched expected preceding text")
        if timeouts > 0:
            r.fail(f"{timeouts}/{len(latencies_ms)} reads exceeded 1s (UIA TextPattern timeout under SetSelection cycling)")
        r.add_note(f"latency: {stats.fmt()}")
        self.results.append(r)
        self._print_result(r)

    # ------- scenario 6: latency under load -----------------------------

    def _scenario_6_latency(self) -> None:
        r = ScenarioResult(
            name="6. Latency p50/p95/p99 for GetText and preceding-text-before-caret",
            passed=True,
        )
        per_size: dict[str, dict[str, LatencyStats]] = {}
        for nbytes, label in [(1_024, "1 KB"), (10_240, "10 KB"), (102_400, "100 KB")]:
            text = ("abcdefghij " * (nbytes // 11 + 1))[:nbytes]
            text = "\n".join(text[i:i+80] for i in range(0, len(text), 80))
            self._set_text(text)
            self._move_caret_to(len(text) // 2)
            self._refocus()
            full_samples: list[float] = []
            pre_samples: list[float] = []
            for _ in range(100):
                t0 = time.perf_counter()
                self.worker.call(uia_get_full_text, self.hwnd)
                full_samples.append((time.perf_counter() - t0) * 1000)
                t0 = time.perf_counter()
                self.worker.call(uia_get_preceding_text, self.hwnd)
                pre_samples.append((time.perf_counter() - t0) * 1000)
            full_stats = LatencyStats.from_samples_ms(full_samples)
            pre_stats = LatencyStats.from_samples_ms(pre_samples)
            per_size[label] = {"GetText": full_stats, "preceding": pre_stats}
            print(f"  {label}: GetText p99={full_stats.p99_ms:.2f}ms, "
                  f"preceding p99={pre_stats.p99_ms:.2f}ms")
            if label == "10 KB" and pre_stats.p99_ms > PRECEDING_LATENCY_BUDGET_MS:
                r.fail(
                    f"10 KB preceding-text-before-caret p99={pre_stats.p99_ms:.2f}ms "
                    f"exceeds {PRECEDING_LATENCY_BUDGET_MS}ms budget"
                )
        r.metrics["per_size"] = {
            label: {
                op: {
                    "p50_ms": s.p50_ms, "p95_ms": s.p95_ms,
                    "p99_ms": s.p99_ms, "max_ms": s.max_ms, "n": s.n,
                }
                for op, s in ops.items()
            }
            for label, ops in per_size.items()
        }
        self.results.append(r)
        self._print_result(r)

    # ------- final --------------------------------------------------------

    def _wrap_up(self) -> None:
        write_report(self.results)
        self.finished.emit()


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

_REPORT_PATH: Path | None = None


def write_report(results: list[ScenarioResult], partial: bool = False) -> Path:
    global _REPORT_PATH
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if _REPORT_PATH is None:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        _REPORT_PATH = REPORT_DIR / f"{ts}-qpte-uia-fidelity.md"
    path = _REPORT_PATH
    ts = path.stem.split("-qpte")[0]

    n_pass = sum(1 for r in results if r.passed)
    n_fail = len(results) - n_pass
    overall = "PASS" if n_fail == 0 else "FAIL"
    if partial:
        overall += " (partial -- not all scenarios run)"

    lines: list[str] = []
    lines.append(f"# QPlainTextEdit UIA Fidelity Report -- {ts}")
    lines.append("")
    lines.append(f"**Overall: {overall}** ({n_pass}/{len(results)} scenarios passed)")
    lines.append("")
    lines.append(f"Bead: wh-w55k5 (Phase 1 stress test, parent wh-u3tj2)")
    lines.append(f"Probe: services/wheelhouse/tests/benchmarks/probe_qpte_uia_fidelity.py")
    lines.append(f"Acceptance budget: preceding-text-before-caret p99 <= "
                 f"{PRECEDING_LATENCY_BUDGET_MS} ms at 10 KB")
    lines.append("")
    lines.append("## Scenarios")
    lines.append("")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"### [{status}] {r.name}")
        lines.append("")
        if r.latency:
            lines.append(f"- Latency: {r.latency.fmt()}")
        for note in r.notes:
            lines.append(f"- {note}")
        if r.metrics:
            lines.append("- Metrics:")
            lines.append("```json")
            lines.append(json.dumps(r.metrics, indent=2, default=str))
            lines.append("```")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    if not partial:
        print(f"\nReport written to: {path}", flush=True)
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    dialog = QDialog()
    dialog.setWindowTitle("QPTE UIA Fidelity Probe")
    dialog.resize(700, 400)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("Probe is running -- do not interact with this window."))
    edit = QPlainTextEdit()
    edit.setAccessibleName("ProbeQPlainTextEdit")
    # Force a native HWND so we can target the QPlainTextEdit by handle
    # instead of relying on OS-level focus.
    edit.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
    layout.addWidget(edit)

    worker = UiaWorker()
    probe = Probe(dialog, edit, worker)

    rc = {"code": 0}

    def on_done():
        worker.stop()
        n_fail = sum(1 for r in probe.results if not r.passed)
        rc["code"] = 1 if n_fail else 0
        QApplication.quit()

    probe.finished.connect(on_done)
    probe.start()
    app.exec()
    return rc["code"]


if __name__ == "__main__":
    sys.exit(main())
