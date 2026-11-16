# Overlay and grid window requirements of the GUI process (place 4: areas G2, P4 and P5)

Bead: `wh-full-refactor.7.1` (stage A of place 4 of the epic `wh-full-refactor`).
Extracted on 2026-11-15 from the code at dev `bcb17373`.

This document covers three areas of `docs/design/refactor-area-map.md`.
Area G2 is the 13 methods of `GuiManager` (`services/wheelhouse/gui.py`)
that handle the overlay and grid messages from Logic, the overlay lease timer and the teardown retry timer.
Area P4 is the 9 methods of `OverlayPaintWindowManager` (`services/wheelhouse/overlay_paint_window.py`)
for clear, lease expiry and teardown.
Area P5 is the 14 methods of `OverlayPaintWindowManager` that draw the mouse grid and its pin.

Stage B of place 4 (`wh-full-refactor.7.2`) moved the three areas without a change in logic.
G2 is now the class `OverlayGuiMessages` in `services/wheelhouse/overlay_gui/messages.py`; `GuiManager` builds it as `overlay_messages`.
P4 is now the class `OverlayPaintTeardown` in `services/wheelhouse/overlay_paint/teardown.py`.
P5 is now the class `OverlayGridPainter` in `services/wheelhouse/overlay_paint/grid.py`.
`OverlayPaintWindowManager` keeps the 10 public names of P4 and P5 as delegators, so the inputs below are unchanged.
The Source cells and the section "Methods and rows" name the new classes.
Stage C of place 4 (`wh-full-refactor.7.3`) gave the entry methods public names (for example `handle_paint_overlay`
and `OverlayPaintTeardown.destroy_all`) and gave the host Protocols in `overlay_gui/host.py` and `overlay_paint/host.py`
public members only. The behavior did not change.

Each row states one rule as an input at a boundary of the class and the output that leaves the class.
For G2 the inputs are the seven messages of Logic that the queue pump of `GuiManager` reads
(`paint_overlay`, `clear_overlay`, `overlay_lease_renew`, `reset_overlay`,
`paint_grid`, `paint_grid_pin`, `clear_grid`), and the expiry of the two timers that the area arms.
For P4 and P5 the inputs are the calls of the public methods of `OverlayPaintWindowManager` that `GuiManager` makes:
`clear`, `expire_lease`, `reset`, `retry_teardown`, `teardown_pending`, `has_deferred_teardown_ack`,
`clear_all`, `paint_grid`, `paint_grid_pin` and `clear_grid`.
The outputs are the `overlay_state_changed` commands on the command queue to Logic,
the values that the public methods return,
and calls on a fake operating system seam (the Win32 functions that the code reaches through `ctypes`,
`composite_layered_window`, and the monitor list).

In this document, "pair" means (`overlay_session_id`, `paint_generation`).
"Ack" means the `overlay_state_changed` report that a teardown returns
(`OverlayStateChangedEvent.to_dict()`, with `monitor_ids` `[]` and `snapshot_id` `None`).
"Survivor" means a window whose `DestroyWindow` call returned 0 or raised.
"Debt" means that `teardown_pending` is True.

## Status values

The status values and their meaning are those of `docs/design/overlay-requirements.md`, section "Status values":
CONFIRMED, OPEN, NOT_REQUIRED and NOT_WANTED.
Under the refactor rule (David, 2026-11-06), today's shipped behavior is the requirement.
A rule that only a number of milliseconds decides, or that needs an extremely unlikely series of events,
is NOT_REQUIRED and has no test.
In this document, NOT_REQUIRED also marks the style values that decision 1 excludes (row GP-17).
A row with no test gives its reason in the Tests or Note column.

## Rows that other requirements documents already have

This document does not repeat those rows. It cites them by their identifiers.

- `docs/design/overlay-requirements.md` LC-17: a failed paint, or no acknowledgement, makes Logic clear.
- `docs/design/overlay-requirements.md` LC-18: Logic ignores an `overlay_state_changed` report for an old pair.
- `docs/design/overlay-requirements.md` LC-26: no clear acknowledgement within 5000 ms gives an ERROR in Logic.
  OT-5 is the GUI half: the GUI holds the ack back while a window survives.
- `docs/design/overlay-requirements.md` LC-27: Logic sends a lease renewal about every 15 s with `lease_ms` of 60000 or more.
- `docs/design/overlay-requirements.md` LC-28: the GUI reports "expired", then Logic clears.
  OT-1 gives the ack with state "expired".
- `docs/design/overlay-requirements.md` LC-29: at its start Logic sends `reset_overlay`;
  the GUI removes leftover windows, resets the generation gate and cancels the lease.
  OT-12 and OT-13 test the manager half.
- `docs/design/overlay-requirements.md` LC-30: the GUI removes the badges itself when the lease runs out
  (first lease 90 s, a renewal gives at least 60 s).
- `docs/design/overlay-requirements.md` LC-47: the ERROR and WARNING log lines of a lease expiry.
- `docs/design/mouse-grid-requirements.md` MG-1, MG-8, MG-11, MG-14, MG-16, MG-18, MG-20 and MG-23:
  Logic sends `paint_grid`, `paint_grid_pin` and `clear_grid`.
  GP-1 to GP-16 are the drawing half. No MG row states a drawing rule
  (search: `grep -n -i "boundar|label|draw"` over that file found only the header and MG-40 and MP-31).

## Decisions kept

1. The P5 rows check what the user can see: the window geometry, the composite origin and size,
   the number of windows, which monitor has a window, and the pixel alpha of lines, labels and the pin.
   Colors, stroke widths, the label size and its limits, the pin size and the shadow offset are style (GP-17, NOT_REQUIRED).
   Reason: a change of style changes the look, not what the user can do,
   and no requirements document states a number for them.
   GP-4 measures the label size from the drawing and matches each digit against a mask that the test draws with Qt,
   so a change of size or font does not break the test.
2. Under pytest, `shared.monitor_geometry` and `services.wheelhouse.shared.monitor_geometry` are two module objects.
   `overlay_paint_window` imports the first one, so the harness patches the `ctypes` attribute of that object.
3. The harness `tests/overlay_paint_seam_harness.py` uses `FakeMonitors` and `Monitor`
   of `tests/mouse_grid_boundary_harness.py`, but not `FakeMonitors.install`,
   because `install` patches the other module object (decision 2).
4. OT-11 keeps the shipped "late-bookkeeping residual": an older parked ack can arrive after the ack of a newer clean clear.
   The code comment in `retry_teardown` names it accepted, and Logic drops an ack below its mark (LC-18).
5. The fake `composite_layered_window` reads the DIB through the public `LayeredDib.read_pixels()`.
   The real `composite_layered_window` does not run, so no `UpdateLayeredWindow` call occurs.
6. The pattern strings of the mutation gates quote production source text, which holds private names.
   The gates are evidence scripts in the form of `tests/mutation_gate_mouse_grid_l5.py`, which does the same.
   The private-name scan gives 0 hits for each gate file.
7. GP-5 asserts only that the shadow adds ink. At a label size of 72 px the outline covers the 2 px shadow offset,
   so the ink edges do not move.

8. The G2 tests use the real `OverlayPaintWindowManager` above the fake seam.
   `GuiManager` builds three managers (badges, grid, working badge), and all three use the fake seam.
9. The G2 tests replace the module attribute `gui.QTimer` with a fake timer class before `GuiManager()` runs.
   A fake clock in `tests/overlay_gui_g2_harness.py` fires each due timer in deadline order.
   A test does not tell the lease timer from the retry timer. It moves the clock and asserts the effects
   (reports and `DestroyWindow` calls). The only time values in the tests are the 90 s of LC-30
   and the `lease_ms` that a renew message carries.
10. The queue pump runs through the public attribute `GuiManager.queue_timer`.
    The rig puts the messages on the state queue and fires that fake timer once.
    The rig does not call `GuiManager.start`, because `start` starts the tray thread, shows the button
    and sends `request_initial_state`.
11. The rig replaces `gui.pystray` (a wrapper of the Windows notification area, an operating system seam) with `MagicMock`.
    The conftest fixture `mock_editor_window` replaces the editor window (`TerminalDictationEditorWindow`).
    That class is not an operating system seam. The fixture docstring gives the reason:
    `GuiManager.__init__` builds a real native dialog for it, and such dialogs caused access-violation crashes
    in full-suite runs under pytest (wh-pytest-flaky-segfault).
    The editor window is outside areas G2, P4 and P5, and no row asserts on it.
    The floating button and the working dialog are the real widgets.
    `GuiManager` builds them but does not show them, and the rig asserts at the end of each test that both are not visible.
12. Like PySide6, the fake `QTimer.start` raises `OverflowError` for an interval above 2**31 - 1 ms
    and leaves the timer as it was (measured with PySide6 in this venv).
    So OG-14 can tell a clamped interval from an interval that is not clamped.
13. Five mutation-gate survivors are declared, each with a reason that a read of the code confirms:
    - `og-5-malformed-paint-not-dropped` and `og-10-malformed-clear-not-dropped`:
      without the `if event is None: return` guard, the next line raises inside the same `try`,
      and the broad `except` logs it. The outputs do not change.
    - `og-15-renew-without-a-lease-goes-on`: without the "no lease" guard, the unpack of `None` raises `TypeError`
      inside the same `try`. The outputs do not change.
    - `og-20b-retry-never-stopped`: a retry timer that is not stopped calls `retry_teardown`,
      which returns None and destroys nothing when no debt and no parked ack exist (OT-10).
    - `og-23-full-queue-raises`: each caller sends the report as the last action that can have an output
      inside its own broad `try`. In `on_overlay_teardown_retry` the re-arm after the send runs only when a debt remains,
      and `retry_teardown` returns a report only when no debt remains.
    So OG-5, OG-10 and OG-15 have tests but no caught mutation.
    The test of OG-20b catches `og-18-reset-not-passed-on`, and the test of OG-23 catches `og-21-paint-grid-not-passed-on`
    (sweep output of `mutation_gate_overlay_gui_g2.py` at 45616c15).
14. The tests that let a survivor fail move the clock 10 s and require at least two `DestroyWindow` calls for it.
    This holds for each retry time up to 5 s. The 2000 ms value is NOT_REQUIRED (OG-24).

## How the tests keep the desktop clean

The harness replaces `overlay_paint_window.ctypes` with a stand-in.
Its fake `user32` returns a counter handle from `CreateWindowExW` and records `ShowWindow` and `DestroyWindow`.
No real window is made, so no window shows on the screen and no keyboard focus moves.
The only real Win32 objects are GDI memory DIBs from `shared/overlay_bitmap.build_layered_dib`, which have no window.
The tests use the session fixture `qapp` of `tests/conftest.py`.

The G2 rig (`tests/overlay_gui_g2_harness.py`) installs the same fake seam before `GuiManager()` runs,
so the three managers of `GuiManager` make no real window.
`gui.QTimer` is the fake timer class, so no Qt timer runs and no test waits in real time.
The rig does not call `GuiManager.start`, so `GuiManager` shows neither the floating button nor the working dialog.
At the end of each test the rig asserts that both are not visible.

## Area G2: overlay and grid messages (`OverlayGuiMessages`; before stage B of place 4, `GuiManager`)

Tests: `services/wheelhouse/tests/test_overlay_gui_requirements_g2.py`.
Mutation gate: `services/wheelhouse/tests/mutation_gate_overlay_gui_g2.py`.
"Report" means an `overlay_state_changed` command on `commands_to_logic_queue`.
"Lease" means the badge lease (first lease 90 s, LC-30). "Retry" means the teardown retry timer.
The walk cue (`button.set_walk_cue` in the queue pump) is area G1 and has no row here.

| ID | When | Then | Status | Source | Tests | Note |
|---|---|---|---|---|---|---|
| OG-1 | `paint_overlay` at a pair that the gate accepts. | The badges show (one window for each monitor). One report with state "painted" at the pair goes to Logic, with the `snapshot_id` and the painted `monitor_ids`. | CONFIRMED | OverlayGuiMessages.handle_paint_overlay | test_og_1 | wh-n29v.53. Logic half: LC-18. |
| OG-2 | `paint_overlay` whose paint fails (`CreateWindowExW` fails). | One report with state "failed" at the pair goes to Logic. The lease starts all the same (a failed paint can leave windows), so an "expired" report arrives 90 s later. | CONFIRMED | OverlayGuiMessages.handle_paint_overlay | test_og_2 | wh-overlay-slow-uia-stale-badges.9. Logic half: LC-17. |
| OG-3 | An accepted paint, and no renew, clear or reset for 90 s. | At 90 s (not 1 ms before) the badges are destroyed and one report with state "expired" at the painted pair goes to Logic. Nothing follows. | CONFIRMED | OverlayGuiMessages.on_overlay_lease_expired | test_og_3 | LC-30 gives the 90 s. LC-28 gives the "expired" state. |
| OG-3b | An accepted paint at P1, then an accepted paint at P2 50 s later. | No expiry occurs 90 s after P1. 90 s after P2, one "expired" report at P2 goes to Logic and the badges go. | CONFIRMED | OverlayGuiMessages.handle_paint_overlay | test_og_3b | A refresh starts the lease again at the new pair. |
| OG-4 | `paint_overlay` at a pair that the gate refuses (older generation). | No report and no new window. The lease keeps its pair and its end time. | CONFIRMED | OverlayGuiMessages.handle_paint_overlay | test_og_4 | wh-overlay-slow-uia-stale-badges.9. |
| OG-5 | A malformed `paint_overlay` (no summary, or a bool generation). | Nothing shows, no report, no lease. The pump handles the next message of the same run. | CONFIRMED | OverlayGuiMessages.handle_paint_overlay | test_og_5 (2 cases) | wh-uf54. No caught mutation: decision 13. |
| OG-6 | `clear_overlay` at a pair that the gate accepts, and each window destroys. | The badges go. One report with state "cleared" at the pair of the clear goes to Logic. The lease ends (no "expired" report later). | CONFIRMED | OverlayGuiMessages.handle_clear_overlay | test_og_6 | wh-n29v.53. |
| OG-7 | `clear_overlay` at a pair that the gate refuses (older generation). | No report. The badges stay. The lease continues (an "expired" report at the painted pair arrives at 90 s). | CONFIRMED | OverlayGuiMessages.handle_clear_overlay | test_og_7 | wh-overlay-slow-uia-stale-badges.9. |
| OG-8 | An accepted `clear_overlay` leaves a survivor. | No report now, and the survivor stays. The retry calls `DestroyWindow` for it again and again. When it destroys, one "cleared" report at the pair of the clear goes to Logic, the lease ends, and the retry stops. | CONFIRMED | OverlayGuiMessages.handle_clear_overlay, OverlayGuiMessages.on_overlay_teardown_retry | test_og_8 | wh-overlay-slow-uia-stale-badges.18.4 and .18.6. Logic half: LC-26. Manager half: OT-5 to OT-7. |
| OG-9 | An incomplete clear at P1, then an accepted paint at P2, then the retry destroys the survivor. | The late "cleared" report at P1 arrives. The P2 badges stay. The P2 lease stays (an "expired" report at P2 arrives 90 s after the P2 paint). | CONFIRMED | OverlayGuiMessages.on_overlay_teardown_retry | test_og_9 | .18.6: only a "cleared" report at exactly the leased pair ends the lease. |
| OG-10 | A malformed `clear_overlay` (no generation). | Nothing is destroyed and no report goes to Logic. The lease continues. | CONFIRMED | OverlayGuiMessages.handle_clear_overlay | test_og_10 | wh-uf54. No caught mutation: decision 13. |
| OG-11 | `overlay_lease_renew` at the leased pair, or at an older generation of the same session, with `lease_ms`. | The lease starts again with the `lease_ms` of the renew. At its end, the "expired" report carries the leased pair, not the pair of the renew. | CONFIRMED | OverlayGuiMessages.handle_overlay_lease_renew | test_og_11 (2 cases) | wh-overlay-slow-uia-stale-badges.18.5. Logic half: LC-27, CK-55. |
| OG-12 | `overlay_lease_renew` for another session (newer or older), for a newer generation, or with a session or generation that is a bool or text. | The GUI ignores it: the lease ends at its old end time. | CONFIRMED | OverlayGuiMessages.handle_overlay_lease_renew | test_og_12 (7 cases) | .18.5. |
| OG-13 | `overlay_lease_renew` at an accepted pair whose `lease_ms` is missing, zero, negative, a bool, text or a float. | The renew still starts the lease again, with the default length. The badges stay past the old end time. | CONFIRMED | OverlayGuiMessages.handle_overlay_lease_renew | test_og_13 (6 cases) | Docstring: a healthy Logic is talking, so the lease stays alive. The default length as a number: OG-25. |
| OG-14 | `overlay_lease_renew` with `lease_ms` above the Qt timer limit (2**31 - 1 ms). | The renew starts the lease again without an error (the interval is clamped to the limit). The badges stay. | CONFIRMED | OverlayGuiMessages._arm_overlay_lease | test_og_14 (2 cases) | Decision 12. |
| OG-15 | `overlay_lease_renew` when no lease runs (no paint yet, or after a clear). | Nothing: no report, no destroy, no lease. | CONFIRMED | OverlayGuiMessages.handle_overlay_lease_renew | test_og_15 (2 cases) | No caught mutation: decision 13. |
| OG-16 | The lease runs out and a badge window survives. | No report at the expiry. The retry repeats. When the survivor destroys, one "expired" report at the leased pair goes to Logic. Nothing follows. | CONFIRMED | OverlayGuiMessages.on_overlay_lease_expired, OverlayGuiMessages.on_overlay_teardown_retry | test_og_16 | .18.4. Manager half: OT-5, OT-7. |
| OG-17 | The lease (pair P1) runs out after an accepted but incomplete clear at a newer pair P2. | No report at the expiry. When the retry destroys the survivors, one "cleared" report at P2 goes to Logic, and nothing at P1. | CONFIRMED | OverlayGuiMessages.on_overlay_lease_expired | test_og_17 | Docstring of `on_overlay_lease_expired`. Manager half: OT-3. |
| OG-18 | `reset_overlay`. | Each badge window goes. No report. The lease ends. A paint at a low pair from a new Logic shows and reports "painted". | CONFIRMED | OverlayGuiMessages.handle_reset_overlay | test_og_18 | LC-29 states the rule; OG-18 is its GUI boundary test. Manager half: OT-12, OT-13. |
| OG-19 | `reset_overlay` and a badge window survives. | The retry repeats until the survivor destroys. No report at any time. Then the retry stops. | CONFIRMED | OverlayGuiMessages.handle_reset_overlay, OverlayGuiMessages.on_overlay_teardown_retry | test_og_19 | .18.4. Manager half: OT-14, OT-14c. |
| OG-20 | An incomplete clear at P1, then a clean clear at P2. | The "cleared" report at P2 goes to Logic at once. Later the retry sends the "cleared" report at P1 once (the retry stays on while a report is parked). | CONFIRMED | OverlayGuiMessages._arm_teardown_retry_if_pending, OverlayGuiMessages.on_overlay_teardown_retry | test_og_20 | .18.8. Logic drops the late P1 report (LC-18). Manager half: OT-11. |
| OG-20b | An incomplete clear, then a clean reset, then a paint. | The retry time passes and the new badges stay. Nothing is destroyed or sent. | CONFIRMED | OverlayGuiMessages._arm_teardown_retry_if_pending | test_og_20b | .18.8. No caught mutation: decision 13. |
| OG-21 | `paint_grid`, then `paint_grid_pin`, then `clear_grid`. | The grid window shows (full monitor), the pin draws on it, and `clear_grid` removes it. Nothing goes to Logic at any step. | CONFIRMED | OverlayGuiMessages.handle_paint_grid, OverlayGuiMessages.handle_paint_grid_pin, OverlayGuiMessages.handle_clear_grid | test_og_21 | Logic half: MG rows in section "Rows that other requirements documents already have". Drawing: GP-1, GP-6, GP-15. |
| OG-21b | `paint_grid_pin` with no grid. | A pin window shows around the point. Nothing goes to Logic. | CONFIRMED | OverlayGuiMessages.handle_paint_grid_pin | test_og_21b | GP-6. |
| OG-21c | `clear_grid` with an extra key. | The grid is cleared all the same. | CONFIRMED | OverlayGuiMessages.handle_clear_grid | test_og_21c | Docstring of `ClearGridEvent.from_dict`: extra keys are ignored, so a clear is never dropped. |
| OG-21d | A malformed `paint_grid` (keys missing) or `paint_grid_pin` (text x) while a grid and a pin show. | Nothing changes (no window made or destroyed). The pump handles the next message of the same run. | CONFIRMED | OverlayGuiMessages.handle_paint_grid, OverlayGuiMessages.handle_paint_grid_pin | test_og_21d (2 cases) | wh-uf54. |
| OG-22 | A grid and badges show; then `clear_overlay`, the lease expiry or `reset_overlay`. | The badges go. The grid window stays. | CONFIRMED | OverlayGuiMessages.handle_clear_overlay, OverlayGuiMessages.on_overlay_lease_expired, OverlayGuiMessages.handle_reset_overlay | test_og_22 (3 cases) | A code comment in `GuiManager.__init__` gives the reason for a separate grid manager. Manager half: OT-17. |
| OG-22b | Badges and a grid show; then `clear_grid`. | The grid goes. The badges stay. Nothing goes to Logic. | CONFIRMED | OverlayGuiMessages.handle_clear_grid | test_og_22b | |
| OG-23 | A report while `commands_to_logic_queue` is full. | The report is dropped without an error. The pump handles the next message. The lease runs, so its later "expired" report arrives when the queue has space. | CONFIRMED | OverlayGuiMessages._emit_overlay_state_changed | test_og_23 | Docstring. No caught mutation: decision 13. |
| OG-24 | A teardown leaves a survivor. | The retry runs every 2000 ms. | NOT_REQUIRED | OverlayGuiMessages._arm_teardown_retry_if_pending | none. Reason: a number of milliseconds only | No requirements document states it. Decision 14. P4 row OT-18 points here. |
| OG-25 | A renew with a bad `lease_ms`. | The lease length is 90000 ms (the default). | NOT_REQUIRED | OverlayGuiMessages.handle_overlay_lease_renew | none. Reason: a number of milliseconds only | OG-13 holds the rule (the renew still renews). |
| OG-26 | The badge manager or the grid manager could not be built at GUI start. | Each G2 handler returns at once: no report, no window, no error. | NOT_REQUIRED | OverlayGuiMessages.handle_paint_overlay, OverlayGuiMessages.handle_clear_overlay, OverlayGuiMessages.on_overlay_lease_expired, OverlayGuiMessages.handle_reset_overlay, OverlayGuiMessages._arm_teardown_retry_if_pending, OverlayGuiMessages.on_overlay_teardown_retry (the `overlay_manager is None` guard on the host); OverlayGuiMessages.handle_paint_grid, OverlayGuiMessages.handle_paint_grid_pin, OverlayGuiMessages.handle_clear_grid (the `grid_overlay is None` guard on the host) | none. Reason: it needs a Win32 window class failure at GUI start (an extremely unlikely series of events) | The input is the construction, which is not a G2 input. Each guard is inside or next to a broad `except` with the same effect. |

Status counts: CONFIRMED 29, NOT_REQUIRED 3 (total 32).

## Area P4: clear, lease expiry and teardown (`OverlayPaintTeardown`; before stage B of place 4, `OverlayPaintWindowManager`)

Tests: `services/wheelhouse/tests/test_overlay_gui_requirements_p4.py`.
Mutation gate: `services/wheelhouse/tests/mutation_gate_overlay_gui_p4.py`.

| ID | When | Then | Status | Source | Tests | Note |
|---|---|---|---|---|---|---|
| OT-1 | `clear` or `expire_lease` at a pair at or above the mark (same pair, newer generation, newer session), and each `DestroyWindow` succeeds. | Each window of the manager is destroyed. The method returns the ack with state "cleared" (`clear`) or "expired" (`expire_lease`) at that pair. | CONFIRMED | OverlayPaintTeardown.clear, OverlayPaintTeardown.expire_lease, GenerationGate.accept_clear | test_ot_1 (6 cases) | LC-28, LC-30. |
| OT-2 | `clear` or `expire_lease` is accepted and the manager has no window. | It returns the ack. | CONFIRMED | OverlayPaintTeardown.clear, OverlayPaintTeardown.expire_lease | test_ot_2 (2 cases) | |
| OT-3 | `clear` or `expire_lease` at a pair strictly below the mark (older generation, older session), or below the pair of an accepted clear. | It returns None and destroys nothing. | CONFIRMED | GenerationGate.accept_clear | test_ot_3 (4 cases), test_ot_3b (2 cases) | wh-n29v.55.1. |
| OT-4 | An accepted clear or expiry at pair P, then a paint at a pair at or below P. | The paint shows nothing (no window). A paint above P shows. | CONFIRMED | GenerationGate.accept_clear, GenerationGate.accept_paint | test_ot_4 (4 cases), test_ot_4b (2 cases) | The subject is what the teardown records. The paint gate belongs to place 5, which can cite this row. |
| OT-5 | `clear` or `expire_lease` is accepted and one or more windows survive. | It returns None. `teardown_pending` is True. `has_deferred_teardown_ack` is True. The survivors stay on the screen. | CONFIRMED | OverlayPaintTeardown.clear, OverlayPaintTeardown.expire_lease | test_ot_5 (4 cases) | wh-overlay-slow-uia-stale-badges.18.4. Logic then reports the ERROR of LC-26. |
| OT-6 | `retry_teardown` while a survivor still fails to destroy. | It returns None. It calls `DestroyWindow` only for the survivor. The debt and the parked ack stay. | CONFIRMED | OverlayPaintTeardown.retry_teardown | test_ot_6 (2 cases) | |
| OT-7 | `retry_teardown` destroys the last survivor. | It returns the parked ack once. The debt goes False and the parked ack goes. A further retry returns None and destroys nothing. | CONFIRMED | OverlayPaintTeardown.retry_teardown | test_ot_7 (2 cases) | |
| OT-8 | Two incomplete teardowns in sequence (`clear`, then `expire_lease` at a newer pair). | The second teardown tries the survivors of the first again. The retry that ends the debt returns only the ack of the second teardown. | CONFIRMED | OverlayPaintTeardown.expire_lease | test_ot_8 | The newest incomplete teardown owns the ack. |
| OT-9 | An incomplete teardown, then an accepted paint, then `retry_teardown`. | The retry calls `DestroyWindow` only for the survivor. The window of the later paint stays. | CONFIRMED | OverlayPaintTeardown.retry_teardown | test_ot_9 | wh-overlay-slow-uia-stale-badges.18.10. |
| OT-9b | An incomplete teardown, the survivor becomes destroyable, then an accepted paint, then `retry_teardown`. | The paint destroys the survivor, but the debt stays True. The retry destroys nothing, returns the parked ack, and the debt goes False. | CONFIRMED | OverlayPaintTeardown.retry_teardown | test_ot_9b | A paint does not end the debt. |
| OT-10 | `retry_teardown` with no debt and no parked ack. | It returns None and destroys nothing. | CONFIRMED | OverlayPaintTeardown.retry_teardown | test_ot_10 | wh-overlay-slow-uia-stale-badges.18.8. |
| OT-11 | An incomplete clear at P1, then a clean clear at P2. | The clean clear destroys the survivors, returns its own ack, and ends the debt. The ack of P1 stays parked. The next retry returns it and destroys nothing. | CONFIRMED | OverlayPaintTeardown.clear, OverlayPaintTeardown.retry_teardown | test_ot_11 | The P1 ack arrives after the P2 ack. Decision 4. |
| OT-12 | `reset` with windows that destroy. | Each window is destroyed. It returns None. | CONFIRMED | OverlayPaintTeardown.reset | test_ot_12 | LC-29. |
| OT-13 | `reset` after the mark went high. | A paint at a low pair (a new Logic counts from zero) shows. | CONFIRMED | OverlayPaintTeardown.reset | test_ot_13 | LC-29. |
| OT-14 | `reset` while an ack is parked and a survivor still fails. | It returns None. The parked ack is dropped (`has_deferred_teardown_ack` False). The debt stays True. | CONFIRMED | OverlayPaintTeardown.reset | test_ot_14 | |
| OT-14b | `reset` while a debt exists and each window destroys. | The debt goes False. | CONFIRMED | OverlayPaintTeardown.reset | test_ot_14b | |
| OT-14c | `reset` with no debt before it, and a window survives. | It returns None. `teardown_pending` is True and no ack is parked. A later retry destroys the survivor and ends the debt. | CONFIRMED | OverlayPaintTeardown.reset | test_ot_14c | |
| OT-15 | `clear_all`. | Each window is destroyed. It returns None. The generation gate does not change (a paint below the mark still shows nothing). | CONFIRMED | OverlayPaintTeardown.clear_all | test_ot_15 | No production caller (section "Other production inputs"). |
| OT-16 | `clear_all` with a survivor. | It records no debt (`teardown_pending` False) and parks no ack. | CONFIRMED | OverlayPaintTeardown.clear_all | test_ot_16 | |
| OT-17 | Two managers each own windows (`GuiManager` has up to three), and one runs `clear`, `expire_lease`, `reset` or `clear_all`. | Only the windows of that manager are destroyed. | CONFIRMED | OverlayPaintWindowManager.__init__ | test_ot_17 (4 cases) | |
| OT-18 | A `DestroyWindow` fails and a retry runs after a time. | The time of the retry (2000 ms) is in `gui.py`, not in this class. | NOT_REQUIRED | OverlayGuiMessages._arm_teardown_retry_if_pending and OverlayGuiMessages.on_overlay_teardown_retry (they start the retry timer with the `gui.py` constant `_OVERLAY_TEARDOWN_RETRY_MS`) | none. Reason: a number of milliseconds only | Area G2 owns the timer. |

Status counts: CONFIRMED 20, NOT_REQUIRED 1 (total 21).

## Area P5: mouse grid drawing (`OverlayGridPainter`; before stage B of place 4, `OverlayPaintWindowManager`)

Tests: `services/wheelhouse/tests/test_overlay_gui_requirements_p5.py`.
Mutation gate: `services/wheelhouse/tests/mutation_gate_overlay_gui_p5.py`.
The monitors of the tests are PRIMARY (0, 0, 1920, 1080, 96 dpi) and SECOND (1920, 0, 2560, 1440, 144 dpi).
Geometry means (left, top, width, height) of the window in physical pixels.

| ID | When | Then | Status | Source | Tests | Note |
|---|---|---|---|---|---|---|
| GP-1 | `paint_grid` names a monitor and a rectangle. | It returns True. One window is on that monitor. Its geometry is the rectangle grown by ceil(4 * dpr) on each side and clamped to the monitor. The composite is at the window origin with the window size. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_1 (4 cases) | MG-1, MG-14. A grid over the full monitor gives a window of exactly the monitor. |
| GP-2 | A grid window is made. | The window is layered, topmost, click-through, no-activate and a tool window. It is shown with `SW_SHOWNOACTIVATE`. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_2 | The numbered overlay windows use the same style; place 5 can cite this row. |
| GP-3 | A grid is drawn. | Four vertical and four horizontal lines lie on the integer cell edges of `grid_overlay_state.cell_rects`, with the stroke centered on the edge. Away from the labels, nothing is drawn between the lines. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_3 | wh-mouse-grid.1.13: the line is where the click arithmetic puts the cell edge. |
| GP-4 | A grid is drawn. | Nine labels show, one on `cell_center` of each cell, digits 1 to 9 in keypad order (1, 2, 3 on the top row). | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_4 | Decision 1. |
| GP-4b | A grid whose rectangle does not divide by 3 (for example 1366 by 770). | Each label sits on the integer cell center that `cell_center` gives, the same point the pointer moves to for the spoken number. The ink has the same shape and the same offset from that center as on a grid that divides by 3. | CONFIRMED | OverlayGridPainter._draw_grid_cells | test_gp_4b (2 cases) | wh-mouse-grid.1.13 (fixed in commit 7a2d971c2). Added in stage C of place 4 (wh-full-refactor.7.3) to replace three deleted old tests of `tests/test_grid_paint_window.py`. |
| GP-5 | The grid manager has `badge_shadow` True. | Each label has more ink than with `badge_shadow` False. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_5 | `GuiManager` passes the accessibility shadow setting. Decision 7. |
| GP-6 | `paint_grid_pin` with a point on a monitor and no grid. | It returns True. One window of size 2m+1 is around the point, with m = ceil(17 * dpr), clamped to the monitor. The point and the crosshair arms have ink. A diagonal point outside the ring has none. | CONFIRMED | OverlayGridPainter.paint_grid_pin | test_gp_6 (3 cases) | MG-11, MG-16. |
| GP-7 | A grid and a pin on the same monitor, in either order. | Both calls return True. One window has the union of the two regions. The pin and the grid lines both show. | CONFIRMED | OverlayGridPainter.paint_grid, OverlayGridPainter.paint_grid_pin | test_gp_7 (4 cases) | Each call draws the kept grid and the kept pin together. |
| GP-8 | A grid on one monitor and a pin on another. | Two windows show, one on each monitor. The pin shows on its own monitor. | CONFIRMED | OverlayGridPainter.paint_grid_pin | test_gp_8 | A drag from one screen to another. |
| GP-9 | `paint_grid_pin` with a point on no monitor. | With no grid, it returns False and makes no window. A later grid draws the grid alone. | CONFIRMED | OverlayGridPainter.paint_grid_pin | test_gp_9 (2 cases) | The case (-3, 500) is off each monitor but near the primary monitor, so a fallback to the first monitor would draw. |
| GP-10 | `paint_grid` names a monitor rectangle that no monitor matches exactly (the resolution changed, or the monitor moved). | The grid draws on the monitor with the largest overlap with the named rectangle. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_10 (2 cases) | |
| GP-10b | Two monitors overlap, one matches the named rectangle exactly, and another overlaps more. | The exact match wins. | NOT_REQUIRED | OverlayGridPainter.paint_grid | none. Reason: Windows does not report monitors that overlap (an extremely unlikely series of events) | Declared survivor `gp-10-exact-match-skipped`. |
| GP-11 | `paint_grid` names a monitor that overlaps no monitor (it was disconnected). | It returns False. The existing grid window is destroyed. Nothing is drawn on another monitor. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_11 (2 cases) | One case is a monitor that touched the left edge of the primary monitor: the grid margin reaches 4 px onto the primary monitor, so a fallback would draw. |
| GP-12 | `paint_grid` with a changed rectangle (a refinement). | The old window is destroyed and a new window with the new geometry is made. The same rectangle again uses the same window: no new window, no destroy, one composite on that window. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_12 | No old surface of a different size stays in use. |
| GP-13 | The composite of a grid window fails (new or reused window). | It returns False and destroys the window. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_13 (2 cases) | An old grid would point at wrong cells. |
| GP-14 | `CreateWindowExW` fails during a grid draw. | It returns False. Each window of the manager, the pin window included, is destroyed. | CONFIRMED | OverlayGridPainter.paint_grid | test_gp_14 | |
| GP-15 | `clear_grid`. | It returns None and destroys each window. The manager forgets the grid and the pin: a later pin draws alone, and a later grid draws no pin. | CONFIRMED | OverlayGridPainter.clear_grid | test_gp_15 | MG-8, MG-18, MG-20, MG-23. |
| GP-16 | A window survives `clear_grid`. | The next `paint_grid` calls `DestroyWindow` for it again before it draws. | CONFIRMED | OverlayGridPainter.paint_grid, OverlayGridPainter.clear_grid | test_gp_16 | `clear_grid` records no debt and the grid has no retry timer. The next draw is the only retry. |
| GP-17 | The label size (the smaller cell side * 0.4, limited to 11 to 72 logical px), the outline width, the pin arm (14 logical px) and ring (5 logical px), the line widths (3 and 1.25 logical px), and the colors. | The grid draws with those sizes and colors. | NOT_REQUIRED | OverlayGridPainter.paint_grid | none. Reason: style, decision 1 | Declared survivor `gp-4-label-size-cap-lowered`. |

Status counts: CONFIRMED 17, NOT_REQUIRED 2 (total 19).

## Methods and rows

For each of the 36 methods of areas G2, P4 and P5, this table gives the rows that cover it.
"Source rows" are the rows that name the method in their Source column.
"Mutation rows" are the rows whose mutations change code inside the method;
a row identifier comes from the name of the mutation (for example `og-18-...` is row OG-18).
"Survivor" marks a mutation that the gate declares as an expected survivor.
Method: the script `method_map.py` in the stage scratchpad (it is not in the repository). It loads the `MUTATIONS` list of
`mutation_gate_overlay_gui_g2.py`, `mutation_gate_overlay_gui_p4.py` and `mutation_gate_overlay_gui_p5.py`,
finds the one match of each `old` text in the production file, and gives the method whose `ast` line span holds
the lines that the mutation changes. A `difflib` comparison of `old` and `new` gives those lines;
the unchanged context lines of a pattern (for example a trailing `def` line) do not count.
Seven mutations change code outside the 36 methods (the paint path of the numbered overlay, the window creation,
and the module constant of the first lease time in `gui.py`);
they have no line here.

### Area G2 (`OverlayGuiMessages`, `overlay_gui/messages.py`)

| Method | Source rows | Mutation rows |
|---|---|---|
| `handle_paint_overlay` | OG-1, OG-2, OG-3b, OG-4, OG-5, OG-26 | OG-1, OG-2, OG-3, OG-4; OG-5 survivor |
| `handle_clear_overlay` | OG-6, OG-7, OG-8, OG-10, OG-22 | OG-6, OG-7, OG-8; OG-10 survivor |
| `_arm_overlay_lease` | OG-14 | OG-14 |
| `_cancel_overlay_lease` | none | OG-6 |
| `handle_overlay_lease_renew` | OG-11, OG-12, OG-13, OG-15, OG-25 | OG-11, OG-12, OG-13; OG-15 survivor |
| `on_overlay_lease_expired` | OG-3, OG-16, OG-17, OG-22 | OG-3, OG-16 |
| `handle_reset_overlay` | OG-18, OG-19, OG-22 | OG-18, OG-19 |
| `_arm_teardown_retry_if_pending` | OG-20, OG-20b, OG-24 | OG-20; OG-20b survivor |
| `on_overlay_teardown_retry` | OG-8, OG-9, OG-16, OG-19, OG-20 | OG-8, OG-9 |
| `handle_paint_grid` | OG-21, OG-21d | OG-21, OG-21d, OG-22 |
| `handle_paint_grid_pin` | OG-21, OG-21b, OG-21d | OG-21, OG-21d |
| `handle_clear_grid` | OG-21, OG-21c, OG-22b | OG-21, OG-22b |
| `_emit_overlay_state_changed` | OG-23 | OG-17; OG-23 survivor |

`_cancel_overlay_lease` has no source row. `handle_clear_overlay`, `handle_reset_overlay` and
`on_overlay_teardown_retry` call it. Rows OG-6, OG-18 and OG-8 show its effect: the lease ends, and no later
"expired" report comes. Its mutation `og-6-cancel-lease-does-nothing` makes the body do nothing;
the tests of OG-6, OG-8 and OG-18 catch it.

### Area P4 (`OverlayPaintTeardown`, `overlay_paint/teardown.py`)

| Method | Source rows | Mutation rows |
|---|---|---|
| `clear` | OT-1, OT-2, OT-5, OT-11 | OT-1, OT-3, OT-5 |
| `clear_all` | OT-15, OT-16 | OT-15, OT-16 |
| `expire_lease` | OT-1, OT-2, OT-5, OT-8 | OT-1, OT-3, OT-5 |
| `reset` | OT-12, OT-13, OT-14, OT-14b, OT-14c | OT-12, OT-13, OT-14 |
| `retry_teardown` | OT-6, OT-7, OT-9, OT-9b, OT-10, OT-11 | OT-6, OT-7, OT-9, OT-10 |
| `teardown_pending` | none | OT-5 |
| `has_deferred_teardown_ack` | none | OT-5 |
| `destroy_all` | none | OT-5, OT-8, OT-11 |
| `_destroy_pending` | none | OT-7 |

`teardown_pending` and `has_deferred_teardown_ack` are public properties.
Outside the class, only `OverlayGuiMessages._arm_teardown_retry_if_pending` and `OverlayGuiMessages.on_overlay_teardown_retry` read them
(search: `git grep -n -w` for the two names over the `*.py` files outside `services/wheelhouse/tests` and `.tdd-evidence`).
The G2 rows of the retry timer (OG-8, OG-9, OG-19, OG-20) and the P4 row OT-5 show their answers.
`destroy_all` is the destroy step of `clear`, `expire_lease`, `reset` and `clear_all`, and also of `paint`, `clear_grid`
and `_render_grid`. `_destroy_pending` is the destroy step of `retry_teardown`. The rows of those methods cover them.

### Area P5 (`OverlayGridPainter`, `overlay_paint/grid.py`)

| Method | Source rows | Mutation rows |
|---|---|---|
| `paint_grid` | GP-1, GP-2, GP-3, GP-4, GP-5, GP-7, GP-10, GP-10b, GP-11, GP-12, GP-13, GP-14, GP-16, GP-17 | none |
| `paint_grid_pin` | GP-6, GP-7, GP-8, GP-9 | none |
| `clear_grid` | GP-15, GP-16 | GP-15 |
| `_render_grid` | none | GP-14 |
| `_do_render_grid` | none | GP-1, GP-6, GP-7, GP-8, GP-11, GP-12, GP-13, GP-16 |
| `_render_grid_surface` | none | none |
| `_draw_grid_cells` | GP-4b | GP-3, GP-4, GP-4b |
| `_draw_grid_label` | none | GP-4, GP-5 |
| `_draw_grid_pin` | none | GP-6 |
| `_grid_ink_margin_phys` | none | GP-1 |
| `_grid_label_pixel_size` | none | GP-4 survivor (row GP-17) |
| `_grid_monitor` | none | GP-10, GP-11 |
| `_grid_pin_ink_margin_phys` | none | GP-6 |
| `_pin_monitor` | none | GP-8, GP-9 |

`paint_grid` and `paint_grid_pin` only store the request and call `_render_grid`,
so their mutations are in the helper methods that they call.
The 11 private helpers have no source row because the rows enter through `paint_grid`, `paint_grid_pin` and `clear_grid`.
`_render_grid_surface` has no row and no mutation. It makes the transparent surface and calls
`_draw_grid_cells` and `_draw_grid_pin`. The pixel rows GP-3, GP-4 and GP-6 read what it composes,
and the mutations of the two called methods are in those rows.
`_grid_label_pixel_size` has only the survivor `gp-4-label-size-cap-lowered`: the label size is a style value (decision 1, row GP-17).
Its call is still covered: GP-4 measures the drawn label and finds each digit.

## Other production inputs

The acceptance field names the inputs of each area. The stage found these other production inputs.
Search method: `git grep` for each public method name over the `*.py` files outside `services/wheelhouse/tests`.

- `GuiManager._hide_working_badge` (`gui.py`) calls `clear` on the working-badge manager,
  the third `OverlayPaintWindowManager`, which `GuiManager` makes only when the working indicator is enabled.
  The OT rows apply to it. The working badge itself belongs to place 5.
- `clear_all` has no production caller. `git grep "clear_all\b"` over the `*.py` files outside `services/wheelhouse/tests`
  found only the definition in `overlay_paint_window.py` (since stage B of place 4 the definition is in
  `overlay_paint/teardown.py`, and `overlay_paint_window.py` keeps a delegator), two code comments, and the mutation script
  `services/wheelhouse/.tdd-evidence/overlay-journeys-mutation-gate.py`.
  The docstring of `clear_all` says "Used on GUI teardown", but `gui.py` does not call it.
  No user can see this difference, so it is not a bug. OT-15 and OT-16 keep its behavior for the test fixtures that use it.

- No other caller of the 13 G2 methods. Search: `git grep -n -w <name>` for each of the 13 names
  over the `*.py` files outside `services/wheelhouse/tests` and `.tdd-evidence`.
  The calls are the queue pump (the seven handlers), the two timer connections in `GuiManager.__init__`,
  and the G2 methods themselves. The other hits are comments, docstrings
  and the area map scripts under `docs/design/refactor-area-map-scripts`.

## Totals

Rows: 72. CONFIRMED 66 (G2 29, P4 20, P5 17), NOT_REQUIRED 6 (G2 3, P4 1, P5 2), OPEN 0, NOT_WANTED 0.
Rows with no boundary test: the 6 NOT_REQUIRED rows (OG-24, OG-25, OG-26, OT-18, GP-10b, GP-17).
Rows with a test but no caught mutation: OG-5, OG-10 and OG-15 (decision 13).
Count command (from the repository root): `grep -E "^\| (OG|OT|GP)-" docs/design/overlay-gui-requirements.md | awk -F'|' '{gsub(/ /,"",$2); gsub(/ /,"",$5); split($2,a,"-"); print a[1], $5}' | sort | uniq -c`.
Output: 17 GP CONFIRMED, 2 GP NOT_REQUIRED, 29 OG CONFIRMED, 3 OG NOT_REQUIRED, 20 OT CONFIRMED, 1 OT NOT_REQUIRED.
