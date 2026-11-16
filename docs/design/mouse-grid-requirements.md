# Mouse grid and pointer action requirements (place 3: areas L5 and U7)

Bead: `wh-full-refactor.6.1` (stage A of place 3 of the epic `wh-full-refactor`).
Extracted on 2026-11-14 from the code at dev `76295fd0`.

This document covers two areas of `docs/design/refactor-area-map.md`.
Area L5 is the mouse grid and the pointer actions of the Logic process.
Since wh-full-refactor.6.2 the code is in `MouseGridController`
(`services/wheelhouse/mouse_grid/controller.py`), which `LogicController` builds as `mouse_grid`.
Area U7 is the four pointer action handlers of the Input process:
`click_point`, `click_at_pointer`, `move_pointer` and `perform_drag`.
Since wh-full-refactor.6.2 the code is in `MousePointerActions` (`services/wheelhouse/ui/mouse_pointer_actions.py`);
`UIActionHandler` keeps the four methods as delegators.

Each row states one rule as an input at a boundary of the class and the output that leaves the class.
For L5 the inputs are the calls of `handle_grid_command` and `handle_pointer_click`
(the speech action path in `speech/actions.py` makes them when the user speaks a grid word or a click word),
and the replies of the Input process on the Input response queue.
For U7 the inputs are the four messages from Logic.
The outputs are messages and replies on the queues, events to the GUI process
(`paint_grid`, `paint_grid_pin`, `clear_grid`, `show_click_notice`),
and calls on a fake operating system seam (the SendInput primitives, the pointer position and the monitor list).

## Status values

The status values and their meaning are those of `docs/design/overlay-requirements.md`, section "Status values":
CONFIRMED, OPEN, NOT_REQUIRED and NOT_WANTED.
Under the refactor rule (David, 2026-11-06), today's shipped behavior is the requirement.
A row with the source "today's shipped behavior (refactor rule, David 2026-11-06)" is CONFIRMED for that reason.
A row with no test gives its reason in the Tests or Note column.
The reason "waits for the injected clock" means that the test needs a real wait of more than 1 second.

## Rows that other requirements documents already have

This document does not repeat those rows. It cites them by their identifiers.

- `docs/design/overlay-requirements.md` LC-5: "show numbers" while the grid pointer channel is unconfirmed
  gives the notice grid_pointer_pending and no walk.
- `docs/design/overlay-requirements.md` LC-6 and AO-4: "show numbers" and the auto-open on an ambiguous click
  close an open mouse grid first.
- `docs/design/overlay-requirements.md` AO-5: an ambiguous click while the grid pointer channel is unconfirmed
  skips the auto-open.
- `docs/design/overlay-requirements.md` AO-30: with the overlay off, "show numbers" leaves an open mouse grid open.
- `docs/design/overlay-requirements.md` CK-33: a badge click during a grid open is refused (NOT_REQUIRED there).
- `docs/design/overlay-requirements.md` LC-39: "escape" hides the mouse grid.
- `docs/design/overlay-requirements.md` CK-48: "click" and a bare number 1..9 while the grid is open goes to the grid.
- `docs/design/overlay-stack-requirements.md` BN-12 and BN-13: a grid number word while badges show
  and the grid is closed clicks the badge.

## Decision: `_close_grid_for_numbered_overlay` is not in place 3

The area map counted 13 methods for L5 at commit 99f3ad49.
Place 1 moved `_close_grid_for_numbered_overlay` into `NumberedOverlayController` (`numbered_overlay/controller.py`).
This stage keeps that method out of place 3, for these reasons:

1. Its callers are the "show" branch of `NumberedOverlayController.handle_overlay_command`
   and the auto-open on an ambiguous click. Both are overlay inputs, not grid inputs.
2. Rows LC-6 and AO-4 of `docs/design/overlay-requirements.md` already state its rule.
3. It reaches the grid only through the public host methods of `LogicController`
   (`grid_overlay_state` and `put_grid_gui_event`), so a later change to L5 does not need to change it.

## Area L5: mouse grid and pointer actions (`MouseGridController`)

Tests: `services/wheelhouse/tests/test_mouse_grid_requirements_l5.py`.
Harness: `services/wheelhouse/tests/mouse_grid_boundary_harness.py`, built on `services/wheelhouse/tests/overlay_boundary_harness.py`.
Mutation gate: `services/wheelhouse/tests/mutation_gate_mouse_grid_l5.py`.

Every CONFIRMED row has the source "today's shipped behavior (refactor rule, David 2026-11-06)".
The Source column adds the symbols and each bead that the code cites and that `bd show` finds
(`wh-grid-speech-routing`, `wh-voice-access-parity.2.5`).
The tests enter through spoken words on the real speech path, the Input replies, the `[click]` config,
and a fake of the monitor list (`EnumDisplayMonitors`, `GetMonitorInfoW`, `GetDpiForMonitor`,
`SetThreadDpiAwarenessContext`, `GetWindowRect`).

| ID | When | Then | Status | Source | Tests | Note |
|---|---|---|---|---|---|---|
| MG-1 | The user says "show grid". | Logic sends paint_grid for the full monitor that holds the focused window. | CONFIRMED | wh-grid-speech-routing; MouseGridController._grid_monitor_context, MouseGridController.handle_grid_command | test_mg_1 (2 cases) | |
| MG-2 | The user says "show grid" while numbers are painted. | Logic sends clear_overlay before paint_grid. | CONFIRMED | wh-grid-speech-routing; MouseGridController._perform_grid_effects | test_mg_2 | The batch is awaited so the order holds. |
| MG-3 | The focused window's monitor is unknown or off every monitor. | The grid opens on the monitor at (0, 0), else on the first monitor listed. | CONFIRMED | GridOverlayStateMachine.apply (primary_monitor), MouseGridController._grid_monitor_context | test_mg_3 (3 cases) | |
| MG-4 | No monitor is listed, the per-monitor DPI context is refused, or enumeration raises. | Logic shows the grid_no_monitor notice and paints no grid. | CONFIRMED | MouseGridController._grid_monitor_context, MouseGridController.handle_grid_command | test_mg_4 (3 cases) | |
| MG-5 | The grid is disabled (click enabled false, or a bad grid key) and the user says "show grid". | Logic shows one disabled_by_config notice; later "show grid" utterances show nothing. | CONFIRMED | MouseGridController.handle_grid_command | test_mg_5 (2 cases) | Gate is grid_enabled_effective. |
| MG-6 | The by-name click already showed the disabled notice, then the user says "show grid". | Logic shows no second notice: the two share one flag. | CONFIRMED | MouseGridController.handle_grid_command, LogicController.take_click_disabled_notice | test_mg_6 | Uses the safety word: without it the by-name click types the words and sets no flag. |
| MG-7 | The grid is disabled and the user says "hide grid" or "grid next screen". | Logic sends nothing and types nothing. | CONFIRMED | MouseGridController.handle_grid_command | test_mg_7 (2 cases) | |
| MG-8 | The user says "hide grid", with the grid open or closed. | Logic sends clear_grid. | CONFIRMED | wh-grid-speech-routing; GridOverlayStateMachine.apply (DISMISS), MouseGridController.handle_grid_command | test_mg_8 (2 cases) | The clear while closed is the machine's defensive clear. |
| MG-9 | The user says "grid next screen" with the grid closed. | Logic sends nothing and types nothing. This holds also when an earlier pointer action timed out: Logic sends no channel_probe and shows no notice. | CONFIRMED | MouseGridController.handle_grid_command | test_mg_9_next_screen_with_the_grid_closed_does_nothing, test_mg_9_next_screen_with_the_grid_closed_does_not_probe_a_suspect_channel | The closed check comes before the pointer-channel check, which would send channel_probe after a timeout. Mutation mg-9-closed-check-skipped is caught. |
| MG-10 | The user says "grid next screen" with the grid open. | Logic paints the next monitor in full, wrapping after the last; one monitor restarts. | CONFIRMED | GridOverlayStateMachine.apply (NEXT_MONITOR), MouseGridController.handle_grid_command | test_mg_10 (2 cases), test_mg_10b | MG-10b: a monitor that is gone restarts on the first monitor. |
| MG-11 | The user says "show grid" or "grid next screen" with a mark set. | Logic paints the full monitor, then repaints the pin at the mark. | CONFIRMED | GridOverlayStateMachine.apply | test_mg_11 (2 cases) | |
| MG-12 | "show grid" or "grid next screen" finds no usable monitor while the grid is open. | Logic shows grid_no_monitor and the open grid keeps its cell. | CONFIRMED | GridOverlayStateMachine.apply, MouseGridController.handle_grid_command | test_mg_12 (2 cases) | |
| MG-13 | The grid is closed and the user says "mark", "drag", "move here", "go here" or a number. | Logic sends nothing; the words are typed. | CONFIRMED | wh-grid-speech-routing; MouseGridController.handle_grid_command | test_mg_13 (5 cases) | |
| MG-14 | The grid is open and the user says a number 1 to 9. | Logic paints that cell of the current rectangle. | CONFIRMED | GridOverlayStateMachine.apply (REFINE), MouseGridController._perform_grid_effects | test_mg_14 (3 cases) | |
| MG-15 | The current cell is already at the minimum size and the user says a number. | Logic consumes the number silently: no paint, no notice, nothing typed. | CONFIRMED | GridOverlayStateMachine.apply (IGNORED_MIN_CELL), MouseGridController.handle_grid_command | test_mg_15 | |
| MG-16 | The grid is open and the user says "mark". | Logic paints the pin at the cell center, then the full monitor. | CONFIRMED | GridOverlayStateMachine.apply (MARK), MouseGridController._perform_grid_effects | test_mg_16 | |
| MG-17 | The grid is open, no mark is set, and the user says "drag". | Logic shows the drag_without_mark notice, sends no input, and keeps the grid open. | CONFIRMED | GridOverlayStateMachine.apply, MouseGridController._perform_grid_effects | test_mg_17 | |
| MG-18 | A mark is set and the user says "drag". | Logic sends clear_grid, then perform_drag from the mark to the cell center with the configured drag duration. | CONFIRMED | GridOverlayStateMachine.apply, MouseGridController._perform_grid_effects | test_mg_18 (2 cases) | |
| MG-19 | The grid closes by "hide grid", a click or "move here". | Logic forgets the mark; a later "drag" gets the drag_without_mark notice. | CONFIRMED | GridOverlayStateMachine.apply | test_mg_19 (3 cases) | |
| MG-20 | The grid is open and the user says click, tap, right, double or triple click. | Logic sends clear_grid, then click_point at the cell center with the gesture's button and count. | CONFIRMED | wh-voice-access-parity.2.5; MouseGridController._grid_gesture_buttons, MouseGridController.handle_grid_command | test_mg_20 (5 cases) | right: (right, 1); double: (left, 2); triple: (left, 3); other: (left, 1). |
| MG-21 | The grid is open and the user says "click N" or "right click N". | Logic clicks the center of cell N, without painting that cell. | CONFIRMED | MouseGridController.handle_grid_command | test_mg_21 (2 cases) | Routing to the grid is CK-48. |
| MG-22 | The cell is at the minimum size and the user says "click N". | Logic clicks the unchanged center. | CONFIRMED | MouseGridController.handle_grid_command | test_mg_22 | |
| MG-23 | The grid is open and the user says "move here" or "go here". | Logic sends clear_grid, then move_pointer to the cell center. | CONFIRMED | MouseGridController.handle_grid_command | test_mg_23 (2 cases) | |
| MG-24 | The grid is closed and the user says a bare click word. | Logic sends click_at_pointer with the gesture's button and count; nothing is typed. | CONFIRMED | wh-voice-access-parity.2.5; MouseGridController.handle_pointer_click | test_mg_24 (5 cases) | Includes "tap." with STT punctuation. |
| MG-25 | Clicking is off (click enabled false) and the user says a bare click word. | Logic sends nothing; the words are typed. | CONFIRMED | wh-voice-access-parity.2.5; MouseGridController.handle_pointer_click | test_mg_25 | |
| MG-26 | A grid key is invalid and the user says a bare click word. | Logic still sends click_at_pointer. | CONFIRMED | MouseGridController.handle_pointer_click | test_mg_26 | The pointer click reads enabled, not grid_enabled_effective. |
| MG-27 | Input answers a pointer action with outcome ok. | Logic shows no notice. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_27 (4 cases) | |
| MG-28 | Input answers a pointer action with a failure reason. | Logic shows the action's notice: grid_click_failed, grid_move_failed, grid_drag_failed or pointer_click_failed. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_28 (4 cases) | |
| MG-29 | Input answers with reason release_failed. | Logic shows grid_right_button_release_failed for the right button, else grid_button_release_failed. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_29 (5 cases) | |
| MG-30 | Input answers with reason disabled_by_config. | Logic shows the disabled_by_config notice. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_30 (2 cases) | |
| MG-31 | Input's reply does not parse as a MouseActionResponse. | Logic shows the action's failure notice. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_31 (2 cases) | |
| MG-32 | Input answers a pointer action with an error reply. | Logic shows the failure notice, and the next pointer action probes the channel first. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_32 | |
| MG-33 | Input does not answer within response_timeout_ms (plus the duration for a drag). | Logic sends channel_probe with the action's trace_id. | CONFIRMED | MouseGridController._send_mouse_action, MouseGridController._confirm_grid_mouse_channel | test_mg_33, test_mg_33b | |
| MG-34 | The probe after a timeout is answered. | Logic reads last_mouse_action: same trace and succeeded gives no notice; release_failed gives the button notice; else the failure notice. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_34 (6 cases) | |
| MG-35 | The probe after a timeout is not answered. | Logic shows the action's failure notice and keeps the channel suspect. | CONFIRMED | MouseGridController._send_mouse_action, MouseGridController._confirm_grid_mouse_channel | test_mg_35, test_mg_36 | |
| MG-36 | The channel is suspect and the user asks for a pointer action. | Logic probes first; an answered probe sends the action and clears the suspicion; an unanswered one shows the failure notice. | CONFIRMED | MouseGridController._send_mouse_action, MouseGridController._confirm_grid_mouse_channel | test_mg_36 (2 cases) | |
| MG-37 | The channel is suspect and the user says "show grid". | Logic probes first; answered, it paints; unanswered, it shows grid_pointer_pending and paints nothing. | CONFIRMED | MouseGridController.grid_pointer_channel_quiescent, MouseGridController.handle_grid_command | test_mg_37 (2 cases) | Same fence as LC-5. |
| MG-38 | A pointer action waits for its reply and the user says "show grid". | Logic paints the grid only after that reply arrives. | CONFIRMED | MouseGridController.grid_pointer_channel_quiescent | test_mg_38 (3 cases) | Includes click_at_pointer. |
| MG-39 | A pointer action waits for its reply and the user asks for another. | Logic sends the second action only after the first reply arrives. | CONFIRMED | MouseGridController._send_mouse_action | test_mg_39 | |
| MG-40 | Putting a grid message on the GUI queue fails. | Logic logs a warning and drops that message. | NOT_REQUIRED | MouseGridController.put_grid_gui_event | none | Needs a failed put on a live multiprocessing queue: an extremely unlikely series of events. |
| MG-41 | One grid effect raises. | Logic logs it and performs the remaining effects. | NOT_REQUIRED | MouseGridController._perform_grid_effects | none | Needs a defect in the schema classes or the overlay: an extremely unlikely series of events. |
| MG-42 | An effect lacks its fields, a kind or command is unknown, the machine or the center is missing, or a refine is outside 1 to 9. | Logic logs and skips it, or lets the words dictate. | NOT_REQUIRED | MouseGridController.handle_grid_command, MouseGridController._perform_grid_effects | none | The machine and the speech patterns never produce these inputs. |
| MG-43 | response_timeout_ms is below 100. | Logic waits at least 0.1 s. | NOT_REQUIRED | MouseGridController._send_mouse_action | none | A number of milliseconds decides it. |
| MG-44 | A dispatched pointer task has not yet taken the pointer lock when "show grid" runs. | Logic waits for that task before it paints. | NOT_REQUIRED | MouseGridController.grid_pointer_channel_quiescent, MouseGridController.track_pending_pointer_task | none | Needs a one-loop-turn timing window; MG-38 covers the lock. Survivor mg-38-fence-skips-the-pending-set. |
| MG-45 | The send raises asyncio.TimeoutError outside the inner handler. | Logic shows the failure notice. | NOT_REQUIRED | MouseGridController._send_mouse_action | none | The inner handler catches every send timeout and the probe never raises; no production input reaches the outer handler. |
| MG-46 | The thread for the monitor read cannot start. | Logic treats it as no monitor. | NOT_REQUIRED | MouseGridController._grid_monitor_context_off_loop | none | Needs asyncio.to_thread itself to fail: an extremely unlikely series of events. |
| MG-47 | The user says "show grid" on a scaled desktop. | Logic reads the monitors and the focused window inside the per-monitor DPI context and paints physical pixels. | CONFIRMED | MouseGridController._grid_monitor_context, monitor_geometry.per_monitor_dpi_context | test_mg_47 (2 cases) | The fake returns rectangles divided by 2 while the thread is outside the per-monitor context. |

Status counts: CONFIRMED 40, NOT_REQUIRED 7 (total 47).

## Area U7: pointer action handlers (`MousePointerActions`)

Tests: `services/wheelhouse/tests/test_mouse_grid_requirements_u7.py`.
Harness: `services/wheelhouse/tests/mouse_pointer_input_harness.py`, built on `services/wheelhouse/tests/overlay_stack_input_harness.py`.
Mutation gate: `services/wheelhouse/tests/mutation_gate_mouse_grid_u7.py`.

Every CONFIRMED row has the source "today's shipped behavior (refactor rule, David 2026-11-06)".
The Source column adds the symbols and each bead that the code cites and that `bd show` finds
(`wh-voice-access-parity.2.5`, `wh-input-mouse-primitives`).
The tests call each message as `input_proc.py` calls it, and fake the public SendInput primitives
(`utils.win_input_sender.click_point`, `move_pointer_to`, `drag_pointer`) and `win32gui.GetCursorPos`.

| ID | When | Then | Status | Source | Tests | Note |
|---|---|---|---|---|---|---|
| MP-1 | Logic sends click_point, click_at_pointer, move_pointer or perform_drag and the primitive succeeds. | Input puts one reply on the response queue: status ok, outcome ok, reason None, the trace_id, the request_id and the action name. | CONFIRMED | MousePointerActions._emit_mouse_action_response | test_mp_1 (4 cases) | Reply shape of the helper (area U1) as the four methods put it on the queue. |
| MP-2 | The primitive refuses with (False, reason). | The reply is status error, outcome execution_failed, and that reason. | CONFIRMED | MousePointerActions.click_point / click_at_pointer / move_pointer / perform_drag | test_mp_2 (4) | |
| MP-3 | The primitive returns (False, None). | The reply has reason unexpected_error. | CONFIRMED | MousePointerActions._emit_mouse_action_response | test_mp_3 (4) | |
| MP-4 | The primitive raises. | The reply has reason sendinput_error. The SendInput wrapper catches the raise. | CONFIRMED | ui_action_handler module functions _win32_click_point, _win32_move_pointer, _win32_drag_pointer; wh-input-mouse-primitives | test_mp_4 (4) | |
| MP-5 | The primitive returns something that is not a (succeeded, reason) pair. | The reply has reason unexpected_error. The handler catches the error. | CONFIRMED | MousePointerActions.click_point / click_at_pointer / move_pointer / perform_drag | test_mp_5 (4) | |
| MP-6 | The message carries a trace_id that is not a string. | The reply has trace_id "". | CONFIRMED | MousePointerActions._emit_mouse_action_response | test_mp_6 (4) | |
| MP-7 | The primitive refuses with release_failed (a button may still be held). | The reply carries release_failed unchanged. | CONFIRMED | MousePointerActions.perform_drag | test_mp_7 (4) | |
| MP-8 | Any message of the four, in any outcome (success, refusal, raise, malformed return, disabled). | Exactly one reply reaches the response queue. | CONFIRMED | MousePointerActions._emit_mouse_action_response | test_mp_8 (4) | |
| MP-9 | The message carries a key that the method does not name. | The method ignores it and acts as without it. | CONFIRMED | MousePointerActions.click_point / click_at_pointer / move_pointer / perform_drag (the **kwargs parameter) | test_mp_9 (4) | Needed because input_proc binds the params. |
| MP-10 | click_point, move_pointer or perform_drag arrives and the grid is off: [click] enabled false, enabled of the wrong type, or a bad grid_min_cell_px or drag_duration_ms. | The reply is execution_failed with disabled_by_config. No primitive is called. | CONFIRMED | MousePointerActions.click_point / move_pointer / perform_drag; ClickConfig.grid_enabled_effective | test_mp_10 (12 cases) | |
| MP-11 | The same three arrive with [click] enabled true and only overlay_enabled false. | They run: an overlay opt-out does not turn the grid off. | CONFIRMED | ClickConfig.grid_enabled_effective | test_mp_11 (3) | |
| MP-12 | click_at_pointer arrives with [click] enabled false or of the wrong type. | The reply is execution_failed with disabled_by_config. No pointer read, no click. | CONFIRMED | MousePointerActions.click_at_pointer; wh-voice-access-parity.2.5 | test_mp_12 (2) | |
| MP-13 | click_at_pointer arrives with [click] enabled true and a bad grid key. | It still clicks at the pointer. | CONFIRMED | MousePointerActions.click_at_pointer; wh-voice-access-parity.2.5 | test_mp_13 (2) | |
| MP-14 | No [click] block is configured. | All four actions run. | CONFIRMED | ClickConfig.from_raw (defaults), UIActionHandler.get_validated_click_config | test_mp_14 | |
| MP-15 | click_point arrives with x, y, button, click_count. | The click primitive gets x, y, button, click_count unconverted (a string y reaches it as a string). | CONFIRMED | MousePointerActions.click_point; _win32_click_point | test_mp_15 | |
| MP-16 | click_point arrives without button and click_count. | The primitive gets left and 1. | CONFIRMED | MousePointerActions.click_point | test_mp_16 | |
| MP-17 | click_at_pointer arrives. | Input reads the pointer position (GetCursorPos) and sends the click primitive that point, the button and the count. | CONFIRMED | MousePointerActions.click_at_pointer; _win32_pointer_position; wh-voice-access-parity.2.5 | test_mp_17 | The int() coercion of the read is an expected survivor (the OS answers integers). |
| MP-18 | click_at_pointer arrives and the pointer position cannot be read. | The reply is execution_failed with pointer_unavailable. No click. | CONFIRMED | MousePointerActions.click_at_pointer; _win32_pointer_position | test_mp_18 (2) | |
| MP-19 | move_pointer arrives with x, y. | The move primitive gets x, y. No button or drag primitive is called. | CONFIRMED | MousePointerActions.move_pointer; _win32_move_pointer | test_mp_19 | |
| MP-20 | perform_drag arrives with start, end and duration_ms. | The drag primitive gets start_x, start_y, end_x, end_y and the duration. | CONFIRMED | MousePointerActions.perform_drag; _win32_drag_pointer | test_mp_20 | |
| MP-21 | perform_drag arrives without duration_ms. | The drag primitive gets 250. | CONFIRMED | MousePointerActions.perform_drag | test_mp_21 | The handler does not read the drag_duration_ms config key; Logic sends the value. |
| MP-22 | channel_probe arrives before any pointer action. | Reply: status ok, the trace_id, request_id, action channel_probe, last_mouse_action None. No input call. | CONFIRMED | UIActionHandler.channel_probe | test_mp_22 | Probe is in _HANDLES_OWN_RESPONSE in input_proc. |
| MP-23 | channel_probe arrives with a trace_id that is not a string. | The reply has trace_id "". | CONFIRMED | UIActionHandler.channel_probe | test_mp_23 | |
| MP-24 | channel_probe arrives after a pointer action. | last_mouse_action is {action, succeeded, reason, trace_id} of that action. | CONFIRMED | MousePointerActions._emit_mouse_action_response, channel_probe | test_mp_24 (4) | |
| MP-25 | The last action succeeded. | The record has succeeded true and reason None. | CONFIRMED | MousePointerActions._emit_mouse_action_response, UIActionHandler.channel_probe | test_mp_25 | |
| MP-26 | Two actions ran before the probe. | The record holds only the second. | CONFIRMED | MousePointerActions._emit_mouse_action_response, UIActionHandler.channel_probe | test_mp_26 | |
| MP-27 | The last action was refused by config. | The record is written: succeeded false, reason disabled_by_config. | CONFIRMED | MousePointerActions._emit_mouse_action_response, UIActionHandler.channel_probe | test_mp_27 (4) | |
| MP-28 | The last action had a trace_id that is not a string. | The record has trace_id "". | CONFIRMED | MousePointerActions._emit_mouse_action_response, UIActionHandler.channel_probe | test_mp_28 | |
| MP-29 | The reply to an action cannot be queued (the queue raises). | No reply and no raise from the handler; the record is still written and the next probe reports it. | CONFIRMED | MousePointerActions._emit_mouse_action_response | test_mp_29 (4) | The fake queue is the other process side. |
| MP-30 | The reply to channel_probe cannot be queued. | The handler drops it without raising; the next probe answers. | CONFIRMED | UIActionHandler.channel_probe | test_mp_30 | |
| MP-31 | click_point, click_at_pointer or perform_drag runs the primitive. | The shadow buffer is invalidated before the dispatch. move_pointer does not invalidate. | CONFIRMED | MousePointerActions.click_point / click_at_pointer / perform_drag | none. Reason: no boundary observes the invalidation (a later dictation message re-syncs through a live UI Automation focus control) | See item 12 of the bd comment DECISIONS KEPT on wh-full-refactor.6.1. |

Status counts: CONFIRMED 31 (total 31).

## Other production inputs

The acceptance field names the inputs of each area. The stage found these other production inputs.

- `channel_probe` (Logic to Input). Logic sends it after a pointer action gets no reply in time,
  and before the next pointer action or "show grid" while the channel is suspect.
  `UIActionHandler.channel_probe` answers it with the record of the last pointer action.
  `input_proc.py` lists it with the four pointer actions as a handler that sends its own reply.
  Rows MG-33 to MG-37 state the Logic side, and rows MP-22 to MP-30 state the Input side.
- An Input error reply (a reply with an `error` key). `WheelHouseApp.send_request` (`app.py`) raises for it. Row MG-32.
- Calls from `NumberedOverlayController` through `numbered_overlay/host.py`:
  - `handle_grid_command` from `forward_click_element` ("click N" with the grid open, CK-48);
  - `mark_grid_mouse_channel_suspect` after a by-name click or a badge click times out or fails;
  - `grid_pointer_channel_quiescent` from "show numbers" and the auto-open (LC-5, AO-5);
  - `grid_session_in_transition` and `track_pending_pointer_task` from the badge click (CK-33);
  - `put_grid_gui_event` from `_close_grid_for_numbered_overlay` (LC-6, AO-4);
  - `take_click_disabled_notice` from the by-name click (MG-6).
- The escape command calls `grid_dismiss_command` (LC-39).
- `input_proc.py` refuses a message whose parameters do not match the handler, before the handler runs.
  The four pointer handlers and `channel_probe` accept extra keys, so every message matches (MP-9).

## Totals

Rows: 78. CONFIRMED 71 (L5 40, U7 31), NOT_REQUIRED 7 (all L5), OPEN 0, NOT_WANTED 0.
Rows with no boundary test: MG-40 to MG-46 (NOT_REQUIRED) and MP-31 (CONFIRMED; no boundary output shows the buffer invalidation).
