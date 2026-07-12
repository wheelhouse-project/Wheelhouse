"""Benchmark: UIA vs Clipboard context-gathering timing.

Run this with a text editor focused (Notepad, VS Code, etc.)
and some text in the editor. Switch to the editor within 3 seconds
of running.

Usage:
    uv run python tests/benchmarks/bench_uia_vs_clipboard.py

Tests:
    1. GetFocusedControl() - what capture_context() already does
    2. TextPattern text retrieval - reading text around cursor via UIA
    3. ValuePattern read - reading full control value via UIA
    4. Clipboard round-trip - Ctrl+C and read (the current gather_context approach)
    5. GetClipboardSequenceNumber() - the proposed polling primitive
    6. Win32 GetClassName via ctypes - lightweight alternative to UIA for detection
"""

import ctypes
import ctypes.wintypes
import statistics
import time
import sys
import os

# ---------------------------------------------------------------------------
# Add project root to path so imports work
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def bench(label: str, func, iterations: int = 50):
    """Run func() `iterations` times and print timing stats."""
    times = []
    errors = 0
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        try:
            result = func()
        except Exception as e:
            errors += 1
            result = f"ERR: {e}"
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        times.append(elapsed_us)

    if times:
        p50 = statistics.median(times)
        p95 = sorted(times)[int(len(times) * 0.95)]
        p_max = max(times)
        p_min = min(times)
        mean = statistics.mean(times)
        print(f"\n  {label}")
        print(f"    n={iterations}, errors={errors}")
        print(f"    min={p_min:.0f}us  median={p50:.0f}us  mean={mean:.0f}us  p95={p95:.0f}us  max={p_max:.0f}us")
    return result


def main():
    print("=" * 70)
    print("UIA vs Clipboard Benchmark")
    print("=" * 70)
    print("\nSwitch to a text editor with some text and cursor in the middle.")
    print("You have 3 seconds...")
    time.sleep(3)
    print("\nRunning benchmarks against the focused window...\n")

    # ------------------------------------------------------------------
    # Test 1: GetFocusedControl (already used in capture_context)
    # ------------------------------------------------------------------
    import uiautomation as auto

    last_control = [None]

    def test_get_focused():
        ctrl = auto.GetFocusedControl()
        last_control[0] = ctrl
        return ctrl.ClassName if ctrl else "None"

    result = bench("1. UIA GetFocusedControl()", test_get_focused)
    print(f"    -> ClassName: {result}")

    # ------------------------------------------------------------------
    # Test 2: TextPattern text retrieval
    # ------------------------------------------------------------------
    focused = last_control[0]
    has_text_pattern = False

    if focused:
        try:
            tp = focused.GetTextPattern()
            if tp:
                has_text_pattern = True
                print(f"\n  [+] TextPattern IS available on this control")

                def test_text_pattern():
                    pattern = focused.GetTextPattern()
                    selection = pattern.GetSelection()
                    # Try to get document range for surrounding text
                    doc_range = pattern.DocumentRange
                    text = doc_range.GetText(200)
                    return text[:50] if text else "(empty)"

                result = bench("2. UIA TextPattern.DocumentRange.GetText()", test_text_pattern)
                print(f"    -> Text sample: {result!r}")

                # Also test getting selection/caret range specifically
                def test_text_pattern_selection():
                    pattern = focused.GetTextPattern()
                    selection = pattern.GetSelection()
                    if selection:
                        first = selection.GetElement(0)
                        # Expand around caret
                        first.ExpandToEnclosingUnit(auto.TextUnit.Line)
                        return first.GetText(200)
                    return "(no selection)"

                result = bench("2b. UIA TextPattern selection + expand to line", test_text_pattern_selection)
                print(f"    -> Line text: {result!r}")
        except Exception as e:
            print(f"\n  [x] TextPattern not available: {e}")
    else:
        print(f"\n  [x] No focused control found")

    if not has_text_pattern:
        print("  2. TextPattern: SKIPPED (not available)")

    # ------------------------------------------------------------------
    # Test 3: ValuePattern read
    # ------------------------------------------------------------------
    has_value_pattern = False
    if focused:
        try:
            vp = focused.GetValuePattern()
            if vp:
                has_value_pattern = True
                print(f"\n  [+] ValuePattern IS available on this control")

                def test_value_pattern():
                    pattern = focused.GetValuePattern()
                    return pattern.Value[:50] if pattern.Value else "(empty)"

                result = bench("3. UIA ValuePattern.Value", test_value_pattern)
                print(f"    -> Value sample: {result!r}")
        except Exception as e:
            print(f"\n  [x] ValuePattern not available: {e}")

    if not has_value_pattern:
        print("  3. ValuePattern: SKIPPED (not available)")

    # ------------------------------------------------------------------
    # Test 4: Clipboard round-trip (simulates gather_context)
    # ------------------------------------------------------------------
    import pyperclip
    from utils.win_input_sender import press_keys

    # Place a sentinel so we can detect the copy
    sentinel = "__bench_sentinel__"
    pyperclip.copy(sentinel)

    def test_clipboard_roundtrip():
        """Select word left, copy, read, deselect - full gather_context cycle."""
        pyperclip.copy(sentinel)
        press_keys('ctrl', 'shift', 'left')  # select word left
        press_keys('ctrl', 'c')               # copy
        time.sleep(0.05)                       # the current 50ms hope
        text = pyperclip.paste()
        press_keys('right')                    # deselect
        return text[:50] if text != sentinel else "(copy failed - got sentinel)"

    result = bench("4. Clipboard round-trip (with 50ms sleep)", test_clipboard_roundtrip, iterations=20)
    print(f"    -> Copied text: {result!r}")

    # ------------------------------------------------------------------
    # Test 5: GetClipboardSequenceNumber (proposed polling primitive)
    # ------------------------------------------------------------------
    user32 = ctypes.windll.user32

    # GetClipboardSequenceNumber is actually in user32
    try:
        GetClipboardSequenceNumber = user32.GetClipboardSequenceNumber
        GetClipboardSequenceNumber.restype = ctypes.wintypes.DWORD

        def test_seq_number():
            return GetClipboardSequenceNumber()

        result = bench("5. GetClipboardSequenceNumber() call", test_seq_number, iterations=200)
        print(f"    -> Current sequence: {result}")
    except Exception as e:
        print(f"\n  [x] GetClipboardSequenceNumber not available: {e}")

    # ------------------------------------------------------------------
    # Test 6: Clipboard round-trip with sequence polling (proposed approach)
    # ------------------------------------------------------------------
    def test_clipboard_seq_polling():
        """Like test 4 but polls sequence number instead of sleeping 50ms."""
        pyperclip.copy(sentinel)
        seq_before = GetClipboardSequenceNumber()
        press_keys('ctrl', 'shift', 'left')
        press_keys('ctrl', 'c')
        # Poll for sequence change instead of fixed sleep
        deadline = time.perf_counter() + 0.5  # 500ms timeout
        while GetClipboardSequenceNumber() == seq_before:
            if time.perf_counter() > deadline:
                break
            time.sleep(0.002)  # 2ms poll
        text = pyperclip.paste()
        press_keys('right')
        return text[:50] if text != sentinel else "(copy failed - got sentinel)"

    result = bench("6. Clipboard round-trip (seq polling, no fixed sleep)", test_clipboard_seq_polling, iterations=20)
    print(f"    -> Copied text: {result!r}")

    # ------------------------------------------------------------------
    # Test 7: Win32 GetClassName + GetForegroundWindow (lightweight)
    # ------------------------------------------------------------------
    GetForegroundWindow = user32.GetForegroundWindow
    GetForegroundWindow.restype = ctypes.wintypes.HWND
    GetClassNameW = user32.GetClassNameW
    GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
    GetClassNameW.restype = ctypes.c_int

    def test_win32_classname():
        hwnd = GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, buf, 256)
        return buf.value

    result = bench("7. Win32 GetForegroundWindow + GetClassName (no UIA)", test_win32_classname, iterations=200)
    print(f"    -> Window class: {result}")

    # ------------------------------------------------------------------
    # Test 8: Just pyperclip.paste() (clipboard read cost alone)
    # ------------------------------------------------------------------
    def test_clipboard_read():
        return pyperclip.paste()[:20]

    result = bench("8. pyperclip.paste() alone (clipboard read cost)", test_clipboard_read, iterations=100)
    print(f"    -> Content: {result!r}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("""
Key questions answered:
  - How does UIA GetFocusedControl compare to Win32 GetForegroundWindow?
  - Is TextPattern fast enough for hot-path context gathering?
  - How much time does the fixed 50ms sleep add vs. seq polling?
  - What's the raw cost of clipboard read/write?

If TextPattern median is under ~5ms, it's viable for context gathering.
If seq polling saves >20ms vs fixed sleep, it's a clear win.
""")


if __name__ == "__main__":
    main()
