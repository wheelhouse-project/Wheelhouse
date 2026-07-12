"""Benchmark: Projected ShadowBuffer.synchronize() using GetCaretRange.

Compares the full sync flow with:
  OLD: GetSelection + DocumentRange + Clone + MoveEndpointByRange + GetText
  NEW: GetCaretRange + DocumentRange.GetText + preceding chars via MoveEndpointByUnit

Run with a text editor focused (Notepad recommended, some text + cursor mid-doc):
    uv run python tests/benchmarks/bench_caret_range_sync.py
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


def fmt(values, label):
    if not values:
        return f"  {label}: no data"
    med = statistics.median(values)
    mn = min(values)
    mx = max(values)
    return f"  {label}: median={med:.2f}ms  min={mn:.2f}ms  max={mx:.2f}ms  (n={len(values)})"


def old_sync(iterations=10):
    """Current synchronize() approach: GetSelection + MoveEndpointByRange."""
    times = []
    with auto.UIAutomationInitializerInThread(debug=False):
        for _ in range(iterations):
            t0 = time.perf_counter()

            focused = auto.GetFocusedControl()
            if not focused:
                continue

            text_pattern = focused.GetPattern(auto.PatternId.TextPattern)
            if not text_pattern:
                continue

            try:
                sel_ranges = text_pattern.GetSelection()
            except _ctypes.COMError:
                continue
            if not sel_ranges:
                continue
            sel_range = sel_ranges[0]

            doc_range = text_pattern.DocumentRange
            full_text = doc_range.GetText(-1)
            sel_text = sel_range.GetText(-1)
            sel_len = len(sel_text)

            # THE EXPENSIVE PART: range endpoint comparison for cursor pos
            cursor_range = doc_range.Clone()
            cursor_range.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.End,
                sel_range,
                auto.TextPatternRangeEndpoint.Start,
            )
            cursor_pos = len(cursor_range.GetText(-1))

            t1 = time.perf_counter()
            times.append({
                'total': (t1 - t0) * 1000,
                'cursor_pos': cursor_pos,
                'text_len': len(full_text),
                'sel_len': sel_len,
            })
    return times


def new_sync(iterations=10):
    """Proposed approach: TextPattern2.GetCaretRange() for cursor position."""
    times = []
    details = {
        'get_focused': [],
        'get_pattern2': [],
        'get_caret_range': [],
        'get_full_text': [],
        'get_selection': [],
        'get_preceding': [],
        'total': [],
    }

    with auto.UIAutomationInitializerInThread(debug=False):
        for _ in range(iterations):
            t0 = time.perf_counter()

            focused = auto.GetFocusedControl()
            t1 = time.perf_counter()
            if not focused:
                continue

            tp2 = focused.GetPattern(auto.PatternId.TextPattern2)
            t2 = time.perf_counter()
            if not tp2:
                continue

            # Access raw COM pointer for GetCaretRange
            raw = tp2.pattern
            if not hasattr(raw, 'GetCaretRange'):
                print("  [!] GetCaretRange not on raw pattern")
                continue

            try:
                is_active, caret_range = raw.GetCaretRange()
                t3 = time.perf_counter()
            except _ctypes.COMError:
                continue

            if not caret_range:
                continue

            # Get full document text (same as old approach)
            tp1 = focused.GetPattern(auto.PatternId.TextPattern)
            doc_range = tp1.DocumentRange
            full_text = doc_range.GetText(-1)
            t4 = time.perf_counter()

            # Get selection state
            try:
                sel_ranges = tp1.GetSelection()
                sel_text = sel_ranges[0].GetText(-1) if sel_ranges else ""
                sel_len = len(sel_text)
            except _ctypes.COMError:
                sel_len = 0
            t5 = time.perf_counter()

            # Get preceding chars from caret position
            # Clone caret range, move start back 2 chars, read
            try:
                pre_range = caret_range.Clone()
                pre_range.MoveEndpointByUnit(
                    auto.TextPatternRangeEndpoint.Start,
                    auto.TextUnit.Character,
                    -2,
                )
                preceding = pre_range.GetText(2)
            except _ctypes.COMError:
                preceding = ""
            t6 = time.perf_counter()

            # Calculate cursor_pos from caret range text
            # Must use raw comtypes API since caret_range is a raw pointer,
            # not a uiautomation TextRange wrapper
            try:
                raw_doc = tp1.DocumentRange.textRange  # get raw comtypes range
                pos_range = raw_doc.Clone()
                pos_range.MoveEndpointByRange(
                    auto.TextPatternRangeEndpoint.End,
                    caret_range,
                    auto.TextPatternRangeEndpoint.Start,
                )
                cursor_pos = len(pos_range.GetText(-1))
            except (_ctypes.COMError, AttributeError) as e:
                print(f"  [!] cursor_pos calc failed: {e}")
                cursor_pos = -1
            t7 = time.perf_counter()

            details['get_focused'].append((t1 - t0) * 1000)
            details['get_pattern2'].append((t2 - t1) * 1000)
            details['get_caret_range'].append((t3 - t2) * 1000)
            details['get_full_text'].append((t4 - t3) * 1000)
            details['get_selection'].append((t5 - t4) * 1000)
            details['get_preceding'].append((t6 - t5) * 1000)
            details['total'].append((t6 - t0) * 1000)

            times.append({
                'total': (t6 - t0) * 1000,
                'total_with_cursorpos': (t7 - t0) * 1000,
                'cursor_pos': cursor_pos,
                'preceding': preceding,
                'text_len': len(full_text),
                'sel_len': sel_len,
                'cursorpos_cost': (t7 - t6) * 1000,
            })
    return times, details


def new_sync_minimal(iterations=10):
    """Minimal sync: only what ShadowBuffer actually NEEDS.

    ShadowBuffer needs: full_text, cursor_pos, selection_len.
    If we can get cursor_pos from GetCaretRange without MoveEndpointByRange,
    we skip the 500ms. But we still need cursor_pos as an integer offset...

    Alternative: store the caret_range itself and compute preceding_chars
    directly from it, without needing an integer cursor_pos at all.
    """
    times = []
    with auto.UIAutomationInitializerInThread(debug=False):
        for _ in range(iterations):
            t0 = time.perf_counter()

            focused = auto.GetFocusedControl()
            if not focused:
                continue

            tp2 = focused.GetPattern(auto.PatternId.TextPattern2)
            if not tp2:
                continue

            raw = tp2.pattern
            try:
                is_active, caret_range = raw.GetCaretRange()
            except _ctypes.COMError:
                continue
            if not caret_range:
                continue

            # Get full text
            tp1 = focused.GetPattern(auto.PatternId.TextPattern)
            full_text = tp1.DocumentRange.GetText(-1)

            # Get selection length
            try:
                sel_ranges = tp1.GetSelection()
                sel_text = sel_ranges[0].GetText(-1) if sel_ranges else ""
                sel_len = len(sel_text)
            except _ctypes.COMError:
                sel_len = 0

            # Get preceding 2 chars (for context) WITHOUT computing cursor_pos
            try:
                pre_range = caret_range.Clone()
                pre_range.MoveEndpointByUnit(
                    auto.TextPatternRangeEndpoint.Start,
                    auto.TextUnit.Character,
                    -2,
                )
                preceding = pre_range.GetText(2)
            except _ctypes.COMError:
                preceding = ""

            # Compute cursor_pos from preceding chars + full text
            # Find where preceding appears in full_text near the end of
            # a prefix. This is approximate but avoids MoveEndpointByRange.
            # Actually: we can compute it from len(full_text) - len(text_after_caret)
            # But getting text_after_caret also needs range ops...
            #
            # Simplest: search for preceding in full_text.
            # But this is fragile for repeated substrings.
            #
            # For ShadowBuffer, cursor_pos is used in:
            #   get_context(): buffer[max(0, cursor_pos-2) : cursor_pos]
            #   update_after_insertion(): buffer[:start] + text + buffer[end:]
            #
            # If we store preceding_chars directly, get_context() works.
            # update_after_insertion needs cursor_pos for splicing.
            # Could we use len(full_text) and work backward?
            #
            # ALTERNATIVE: just don't compute cursor_pos here.
            # Store full_text + preceding_chars + sel_len.
            # get_context() returns preceding_chars directly.
            # update_after_insertion: cursor_pos = full_text.index(preceding) + len(preceding)
            #   (fragile) or skip buffer update and re-sync next word.

            t1 = time.perf_counter()
            times.append({
                'total': (t1 - t0) * 1000,
                'preceding': preceding,
                'text_len': len(full_text),
                'sel_len': sel_len,
            })
    return times


def main():
    countdown(3)

    print("=" * 60)
    print("1. OLD: Full synchronize (MoveEndpointByRange)")
    print("=" * 60)
    old = old_sync(10)
    old_times = [r['total'] for r in old]
    print(fmt(old_times, "total"))
    if old:
        print(f"  cursor_pos={old[0]['cursor_pos']}, text_len={old[0]['text_len']}, sel_len={old[0]['sel_len']}")

    print()
    print("=" * 60)
    print("2. NEW: Full sync with GetCaretRange (includes cursor_pos)")
    print("=" * 60)
    new, new_details = new_sync(10)
    for key in ['get_focused', 'get_pattern2', 'get_caret_range', 'get_full_text', 'get_selection', 'get_preceding', 'total']:
        print(fmt(new_details[key], key))
    if new:
        print(f"\n  cursor_pos={new[0]['cursor_pos']}, preceding='{new[0]['preceding']}', sel_len={new[0]['sel_len']}")
        print(f"  cursor_pos cost (MoveEndpointByRange still): {fmt([r['cursorpos_cost'] for r in new], 'cursorpos_cost')}")
        print(f"  total WITH cursor_pos: {fmt([r['total_with_cursorpos'] for r in new], 'with_cursorpos')}")

    print()
    print("=" * 60)
    print("3. MINIMAL: Sync without integer cursor_pos")
    print("=" * 60)
    minimal = new_sync_minimal(10)
    minimal_times = [r['total'] for r in minimal]
    print(fmt(minimal_times, "total"))
    if minimal:
        print(f"  preceding='{minimal[0]['preceding']}', text_len={minimal[0]['text_len']}, sel_len={minimal[0]['sel_len']}")

    print()
    print("=" * 60)
    print("4. Verdict")
    print("=" * 60)
    if old_times and minimal_times:
        old_med = statistics.median(old_times)
        min_med = statistics.median(minimal_times)
        new_med = statistics.median(new_details['total']) if new_details['total'] else 0
        print(f"  OLD (MoveEndpointByRange):     {old_med:.1f}ms")
        print(f"  NEW (GetCaretRange + cursorpos): {new_med:.1f}ms")
        print(f"  MINIMAL (no integer cursorpos):  {min_med:.1f}ms")
        print()
        if old_med > 0 and min_med > 0:
            print(f"  MINIMAL speedup: {old_med/min_med:.0f}x faster")
            print(f"  MINIMAL saving:  {old_med - min_med:.0f}ms per first word")
            print()
            if new_med > 50:
                print("  [!] NEW still slow -- MoveEndpointByRange for cursor_pos is the bottleneck")
                print("  --> Must redesign ShadowBuffer to avoid integer cursor_pos")
                print("  --> Store preceding_chars directly from GetCaretRange")
            else:
                print("  [+] NEW is fast too -- GetCaretRange avoids the 500ms penalty")


if __name__ == "__main__":
    main()
