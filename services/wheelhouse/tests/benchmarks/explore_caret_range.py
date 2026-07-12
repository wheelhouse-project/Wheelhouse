"""Explore TextPattern2.GetCaretRange() via raw COM interface.

Run with Notepad focused:
    uv run python tests/benchmarks/explore_caret_range.py
"""
import time
import sys
import os
import ctypes
from ctypes import POINTER, HRESULT, c_int, c_bool, byref, c_void_p

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import uiautomation as auto
import _ctypes

print("Switch to Notepad in 3 seconds...")
for i in range(3, 0, -1):
    print(f"  {i}...")
    time.sleep(1)

with auto.UIAutomationInitializerInThread(debug=False):
    focused = auto.GetFocusedControl()
    print(f"Focused: {focused.Name} ({focused.ClassName})")

    # Get TextPattern (old way) for comparison
    tp = focused.GetPattern(auto.PatternId.TextPattern)
    print(f"TextPattern: {tp is not None}")

    # Get TextPattern2
    tp2 = focused.GetPattern(auto.PatternId.TextPattern2)
    print(f"TextPattern2: {tp2 is not None}")

    if tp2:
        raw = tp2.pattern
        print(f"Raw pattern type: {type(raw)}")
        attrs = [x for x in dir(raw) if not x.startswith('_')]
        print(f"Raw attributes: {attrs}")

        # Check if GetCaretRange is accessible via comtypes interface
        if hasattr(raw, 'QueryInterface'):
            print("Has QueryInterface - trying comtypes approach...")
            try:
                import comtypes
                import comtypes.client
                from comtypes import GUID

                # IUIAutomationTextPattern2 GUID
                IID_IUIAutomationTextPattern2 = GUID('{506A921A-FCC9-409F-B3B6-2DA36AE11FF5}')

                # Try QueryInterface for TextPattern2
                tp2_raw = raw.QueryInterface(
                    comtypes.gen.UIAutomationClient.IUIAutomationTextPattern2
                )
                print(f"Got IUIAutomationTextPattern2: {tp2_raw}")
                print(f"tp2_raw type: {type(tp2_raw)}")
                print(f"Has GetCaretRange: {hasattr(tp2_raw, 'GetCaretRange')}")

                if hasattr(tp2_raw, 'GetCaretRange'):
                    t0 = time.perf_counter()
                    is_active, caret_range = tp2_raw.GetCaretRange()
                    t1 = time.perf_counter()
                    print(f"GetCaretRange: {(t1-t0)*1000:.2f}ms, active={is_active}")
                    print(f"Caret range: {caret_range}")
                    if caret_range:
                        text = caret_range.GetText(10)
                        print(f"Text at caret (10 chars): '{text}'")

            except Exception as e:
                print(f"comtypes approach failed: {e}")
                import traceback
                traceback.print_exc()

        # Alternative: try accessing via the uiautomation wrapper internals
        print("\nTrying uiautomation wrapper internals...")
        try:
            # The uiautomation lib might have it as a method on the wrapper
            wrapper_attrs = [x for x in dir(tp2) if 'aret' in x.lower() or 'caret' in x.lower()]
            print(f"Caret-related on wrapper: {wrapper_attrs}")

            all_attrs = [x for x in dir(tp2) if not x.startswith('_')]
            print(f"All wrapper attrs: {all_attrs}")
        except Exception as e:
            print(f"Wrapper inspection failed: {e}")

    # Benchmark the old approach for comparison
    print("\n--- Old MoveEndpointByRange approach ---")
    t0 = time.perf_counter()
    sel = tp.GetSelection()
    doc = tp.DocumentRange
    cr = doc.Clone()
    cr.MoveEndpointByRange(
        auto.TextPatternRangeEndpoint.End,
        sel[0],
        auto.TextPatternRangeEndpoint.Start,
    )
    cursor_pos = len(cr.GetText(-1))
    t1 = time.perf_counter()
    print(f"Old range_calc: {(t1-t0)*1000:.2f}ms, cursor_pos={cursor_pos}")
