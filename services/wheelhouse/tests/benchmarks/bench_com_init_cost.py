"""Benchmark: COM initialization cost breakdown for ShadowBuffer.synchronize().

Measures where the ~420ms first-word penalty actually goes:
  1. Cold COM init (UIAutomationInitializerInThread enter)
  2. Warm COM init (already initialized on same thread)
  3. GetFocusedControl()
  4. GetPattern(TextPattern)
  5. GetSelection() + GetText() + range comparison
  6. Full synchronize() cold vs warm

Run with a text editor focused (Notepad recommended):
    uv run python tests/benchmarks/bench_com_init_cost.py

Switch to the editor within 3 seconds of running.
"""

import statistics
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import uiautomation as auto
import _ctypes


def countdown(seconds=3):
    print(f"\nSwitch to a text editor within {seconds} seconds...")
    for i in range(seconds, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    print()


def bench_cold_com_init(iterations=5):
    """Measure COM init cost from a clean state.

    We can't truly uninitialize COM mid-process reliably,
    so this measures the context manager overhead when COM
    is already initialized (warm). The first iteration captures
    any residual cold-start cost.
    """
    times = []
    for i in range(iterations):
        t0 = time.perf_counter()
        ctx = auto.UIAutomationInitializerInThread(debug=False)
        ctx.__enter__()
        t1 = time.perf_counter()
        ctx.__exit__(None, None, None)
        t2 = time.perf_counter()
        times.append({
            'enter': (t1 - t0) * 1000,
            'exit': (t2 - t1) * 1000,
        })
    return times


def bench_uia_components(iterations=10):
    """Measure each UIA operation independently inside a single COM session."""
    results = {
        'get_focused': [],
        'get_pattern': [],
        'get_selection': [],
        'get_text': [],
        'range_calc': [],
        'total_inside_com': [],
    }

    with auto.UIAutomationInitializerInThread(debug=False):
        for i in range(iterations):
            t0 = time.perf_counter()
            focused = auto.GetFocusedControl()
            t1 = time.perf_counter()

            if not focused:
                print(f"  [!] Iteration {i}: No focused control")
                continue

            text_pattern = focused.GetPattern(auto.PatternId.TextPattern)
            t2 = time.perf_counter()

            if not text_pattern:
                print(f"  [!] Iteration {i}: No TextPattern on {focused.Name}")
                continue

            try:
                sel_ranges = text_pattern.GetSelection()
            except _ctypes.COMError:
                print(f"  [!] Iteration {i}: GetSelection failed")
                continue
            t3 = time.perf_counter()

            if not sel_ranges:
                print(f"  [!] Iteration {i}: Empty selection array")
                continue

            sel_range = sel_ranges[0]
            doc_range = text_pattern.DocumentRange
            full_text = doc_range.GetText(-1)
            t4 = time.perf_counter()

            cursor_range = doc_range.Clone()
            cursor_range.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                sel_range,
                auto.TextPatternRangeEndpoint.Start,
            )
            cursor_pos = len(cursor_range.GetText(-1))
            t5 = time.perf_counter()

            results['get_focused'].append((t1 - t0) * 1000)
            results['get_pattern'].append((t2 - t1) * 1000)
            results['get_selection'].append((t3 - t2) * 1000)
            results['get_text'].append((t4 - t3) * 1000)
            results['range_calc'].append((t5 - t4) * 1000)
            results['total_inside_com'].append((t5 - t0) * 1000)

    return results


def bench_get_caret_range(iterations=20):
    """Measure TextPattern2.GetCaretRange() as alternative to range_calc.

    GetCaretRange returns a zero-length range at the caret position directly,
    potentially avoiding the expensive MoveEndpointByRange cross-process call.
    """
    results = {
        'get_caret_range': [],
        'get_preceding_chars': [],
        'total': [],
        'get_pattern2': [],
    }

    with auto.UIAutomationInitializerInThread(debug=False):
        for i in range(iterations):
            t0 = time.perf_counter()
            focused = auto.GetFocusedControl()
            if not focused:
                print(f"  [!] Iteration {i}: No focused control")
                continue

            text_pattern2 = focused.GetPattern(auto.PatternId.TextPattern2)
            t1 = time.perf_counter()

            if not text_pattern2:
                print(f"  [!] Iteration {i}: No TextPattern2 on {focused.Name}")
                continue

            try:
                caret_range = text_pattern2.GetCaretRange()
                t2 = time.perf_counter()
            except (AttributeError, _ctypes.COMError) as e:
                # GetCaretRange might not be exposed by the Python wrapper
                # Try raw COM call via ctypes
                print(f"  [!] GetCaretRange not available via wrapper: {e}")
                print(f"      TextPattern2 type: {type(text_pattern2)}")
                print(f"      TextPattern2 dir: {[x for x in dir(text_pattern2) if not x.startswith('_')][:20]}")
                break

            if caret_range is None:
                print(f"  [!] Iteration {i}: GetCaretRange returned None")
                continue

            # Now get preceding 2 chars by reading text from the caret range
            # Clone the caret range and expand backward
            try:
                pre_range = caret_range.Clone()
                pre_range.MoveEndpointByUnit(
                    auto.TextPatternRangeEndpoint.Start,
                    auto.TextUnit.Character,
                    -2,
                )
                preceding = pre_range.GetText(2)
                t3 = time.perf_counter()
            except _ctypes.COMError as e:
                print(f"  [!] Iteration {i}: range ops failed: {e}")
                continue

            results['get_pattern2'].append((t1 - t0) * 1000)
            results['get_caret_range'].append((t2 - t1) * 1000)
            results['get_preceding_chars'].append((t3 - t2) * 1000)
            results['total'].append((t3 - t0) * 1000)

    return results


def bench_full_synchronize(iterations=5):
    """Measure full synchronize() equivalent with COM init included."""
    times = []
    for i in range(iterations):
        t0 = time.perf_counter()
        with auto.UIAutomationInitializerInThread(debug=False):
            focused = auto.GetFocusedControl()
            if not focused:
                continue
            tp = focused.GetPattern(auto.PatternId.TextPattern)
            if not tp:
                continue
            try:
                sel = tp.GetSelection()
            except _ctypes.COMError:
                continue
            if not sel:
                continue
            doc = tp.DocumentRange
            doc.GetText(-1)
            sel[0].GetText(-1)
            cr = doc.Clone()
            cr.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                sel[0],
                auto.TextPatternRangeEndpoint.Start,
            )
            cr.GetText(-1)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    return times


def fmt(values, label):
    if not values:
        return f"  {label}: no data"
    med = statistics.median(values)
    mn = min(values)
    mx = max(values)
    return f"  {label}: median={med:.2f}ms  min={mn:.2f}ms  max={mx:.2f}ms  (n={len(values)})"


def main():
    countdown(3)

    print("=" * 60)
    print("1. COM Init/Uninit Cost (context manager overhead)")
    print("=" * 60)
    com_times = bench_cold_com_init(10)
    for i, t in enumerate(com_times):
        tag = "COLD" if i == 0 else "warm"
        print(f"  [{tag}] enter={t['enter']:.2f}ms  exit={t['exit']:.2f}ms")

    print()
    print("=" * 60)
    print("2. UIA Component Breakdown (inside persistent COM)")
    print("=" * 60)
    comp = bench_uia_components(20)
    for key in ['get_focused', 'get_pattern', 'get_selection', 'get_text', 'range_calc', 'total_inside_com']:
        print(fmt(comp[key], key))

    print()
    print("=" * 60)
    print("3. TextPattern2.GetCaretRange() (alternative to range_calc)")
    print("=" * 60)
    caret = bench_get_caret_range(20)
    for key in ['get_pattern2', 'get_caret_range', 'get_preceding_chars', 'total']:
        print(fmt(caret[key], key))
    if caret['total']:
        old_range = statistics.median(comp['range_calc']) if comp['range_calc'] else 0
        new_total = statistics.median(caret['total'])
        print(f"\n  Compare: range_calc={old_range:.1f}ms vs GetCaretRange total={new_total:.1f}ms")
        if old_range > 0:
            print(f"  Speedup: {old_range/new_total:.0f}x faster" if new_total > 0 else "  Speedup: infinite")

    print()
    print("=" * 60)
    print("4. Full Synchronize - old method (COM init + all UIA queries)")
    print("=" * 60)
    sync_times = bench_full_synchronize(10)
    for i, t in enumerate(sync_times):
        tag = "COLD" if i == 0 else "warm"
        print(f"  [{tag}] {t:.2f}ms")
    print()
    print(fmt(sync_times[1:], "warm iterations"))

    print()
    print("=" * 60)
    print("5. Verdict")
    print("=" * 60)
    if comp.get('range_calc') and caret.get('total'):
        old_range = statistics.median(comp['range_calc'])
        new_caret = statistics.median(caret['total'])
        warm_sync = statistics.median(sync_times[1:]) if len(sync_times) > 1 else sync_times[0]
        projected = warm_sync - old_range + new_caret
        print(f"  Old range_calc:              {old_range:.2f}ms")
        print(f"  New GetCaretRange total:     {new_caret:.2f}ms")
        print(f"  Old full sync:               {warm_sync:.2f}ms")
        print(f"  Projected sync w/ caret:     {projected:.2f}ms")
        print(f"  Projected first-word saving: {warm_sync - projected:.0f}ms")
    elif comp.get('range_calc'):
        print("  GetCaretRange not available -- no comparison possible")
        warm_sync = statistics.median(sync_times[1:]) if len(sync_times) > 1 else sync_times[0]
        print(f"  Current full sync: {warm_sync:.2f}ms (dominated by range_calc)")


if __name__ == "__main__":
    main()
