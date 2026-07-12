"""Event type definitions for WheelHouse EventBus communication.

This module defines the event dataclasses used throughout WheelHouse for
intra-process communication via EventBus. Events are immutable data carriers
that enable loose coupling between services.

NOTE: This module defines event TYPES, not flow implementations. For flow
documentation showing how these events are used in system workflows, see
individual service modules with :flow: docstrings or docs/developers/generated/flows.json.

EVENT CATEGORIES:
- State Change Events: Notify of system state transitions (audio, Sonos)
- Hardware Layer Events: Mouse/HID input processing 
- Plugin Command Events: Commands for plugin-based integrations
- Brightness Coordination Events: Multi-stage brightness control (hardware → software)
- Configuration Error Events: Fail-fast validation with user notifications
"""

from dataclasses import dataclass, field
from typing import Optional
import time

@dataclass
class SonosStateChangedEvent:
    """
    Event published when the Sonos player state changes.

    :param is_playing: True if music is playing, False otherwise.
    """
    is_playing: bool

@dataclass
class AudioStateChangedEvent:
    """
    Event published when the system audio playback state changes.

    :param is_playing: True if system audio is playing, False otherwise.
    """
    is_playing: bool

@dataclass
class SystemIdleStateChangedEvent:
    """
    Event published when system transitions between idle and active states.

    Published by IdleMonitorPlugin when idle timeout threshold is crossed.
    Consumed by StateManager to suppress/restore speech transcription.

    :param is_idle: True if system has become idle, False if active
    :param idle_duration_seconds: Current idle duration (0 if active)
    """
    is_idle: bool
    idle_duration_seconds: float

@dataclass
class WakeWordDetectedEvent:
    """
    Event published when a wake word is detected by the STT server.

    Received via WebSocket from the STT process. Consumed by StateManager
    to clear idle suppression and re-enable transcription.

    :param keyword: The wake word that was detected (e.g., "computer")
    """
    keyword: str

@dataclass
class SystemConfigurationErrorEvent:
    """
    Event published when a service encounters a configuration error during initialization.
    
    Used for fail-fast validation with user-friendly notifications instead of silent 
    degradation. Services can disable problematic features and continue running.
    
    :param service_name: Name of the service reporting the error (e.g., "AudioMonitor")
    :param error_message: User-friendly description of the configuration problem
    :param user_action: Actionable guidance for fixing the issue
    """
    service_name: str
    error_message: str
    user_action: str

# ============================================================================
# HARDWARE LAYER EVENTS
# EventBus contracts for hardware input processing and device control
# ============================================================================

@dataclass
class ThumbWheelEvent:
    """
    Event published when a HID thumb wheel input is detected.
    
    Published by HIDListener when raw HID data is processed into a thumb wheel event.
    Consumed by MouseHandler to determine context-appropriate actions (volume/brightness).
    
    :param delta: Scroll delta amount (positive=down/right, negative=up/left)
    :param timestamp: Event timestamp for debouncing and aggregation
    """
    delta: int
    timestamp: float

@dataclass
class VolumeChangeRequest:
    """
    Event published when a volume change is requested via hardware input.
    
    Published by MouseHandler when thumb wheel events occur in volume control zones.
    Consumed by SonosControl to adjust speaker volume over network.
    
    :param volume_delta: Volume change amount (positive=increase, negative=decrease)
    :param source: Source of the request ("thumb_wheel", "hotkey", etc.)
    """
    volume_delta: int
    source: str

@dataclass
class DimmingRequest:
    """
    Event published when software dimming is specifically requested.
    
    Published by MouseHandler or other services when software-based dimming is needed.
    Consumed by SoftwareDimmer to apply screen dimming effects.
    
    :param action: "dim", "brighten", "toggle", or "reset"
    :param level: Optional specific dimming level (0.0-1.0, where 1.0 is normal brightness)
    """
    action: str
    level: Optional[float] = None

# ============================================================================
# PLUGIN ARCHITECTURE EVENTS
# Command events for plugin-based integrations (Sonos, Bravia, etc.)
# ============================================================================

@dataclass
class VolumeAdjustCommand:
    """
    Command event for volume control plugins.
    
    Published by MouseHandler when thumb wheel is rotated in volume zone.
    Consumed by volume control plugins (Sonos, System Volume, etc.).
    
    :param delta: Volume change delta (positive=increase, negative=decrease)
    """
    delta: int

@dataclass
class BrightnessAdjustCommand:
    """
    Command event for brightness control plugins.
    
    Published by MouseHandler when thumb wheel is rotated in brightness zone.
    Consumed by BrightnessCoordinator for routing to hardware/software plugins.
    
    :param delta: Brightness change delta (positive=brighter, negative=dimmer)
    """
    delta: int

@dataclass
class HardwareBrightnessCommand:
    """
    Hardware-specific brightness command from coordinator to hardware plugins.
    
    Published by BrightnessCoordinator when hardware should adjust brightness.
    This enables the coordinator to gate hardware commands during software dimming
    unwinding, preventing double-adjustment (hardware + software simultaneously).
    
    :param delta: Brightness change delta (positive=brighter, negative=dimmer)
    """
    delta: int

# ============================================================================
# BRIGHTNESS COORDINATION EVENTS
# State and overflow events for multi-stage brightness control
# ============================================================================

@dataclass
class BrightnessStateChanged:
    """
    Published by brightness plugins when state changes.
    
    Enables BrightnessCoordinator to cache current state for fast decision-making
    during overflow cascade (hardware → software dimming).
    
    :param level: Current brightness (0-100 normalized)
    :param at_min: True if at hardware minimum (0)
    :param at_max: True if at hardware maximum (100)
    :param source_plugin: Plugin name ("bravia", "laptop", etc.)
    :param timestamp: Event timestamp for debugging
    """
    level: int
    at_min: bool
    at_max: bool
    source_plugin: str
    timestamp: float = field(default_factory=time.time)

@dataclass  
class BrightnessOverflowEvent:
    """
    Published when hardware can't adjust further (cascade trigger).
    
    This event signals that hardware brightness has reached its limit and
    additional adjustment should cascade to software dimming (f.lux, overlay, etc.).
    The delta indicates how much adjustment couldn't be applied.
    
    Events flow naturally - no debouncing at plugin level. BrightnessCoordinator
    handles any rate limiting if needed.
    
    :param delta: Remaining adjustment needed (positive or negative)
    :param source_plugin: Plugin that hit limit ("bravia", "laptop", etc.)
    :param reason: Why overflow occurred ("at_hardware_limit", "device_offline")
    :param timestamp: Event timestamp for debugging
    """
    delta: int
    source_plugin: str
    reason: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class AtmosActivationRequest:
    """
    Event published when Atmos audio processing should be activated.
    
    Published by MouseHandler when sustained top-edge hover is detected.
    Consumed by AudioMonitor to initiate Atmos audio processing.
    
    :param trigger_source: Source of activation ("mouse_hover", "command", etc.)
    :param debounce_key: Key for debouncing repeated requests
    """
    trigger_source: str
    debounce_key: str

# ============================================================================
# WINDOW POSITIONING EVENTS
# Events for automatic window repositioning (on-screen keyboard, etc.)
# ============================================================================

@dataclass
class WindowFocusChangedEvent:
    """
    Published when a window gains focus that might need keyboard repositioning.
    
    Published by WindowPositioningPlugin when Windows accessibility events
    indicate a significant window change (foreground, create, show, menu popup).
    Used internally by the plugin to trigger overlap detection.
    
    :param hwnd: Window handle (HWND)
    :param title: Window title text
    :param class_name: Window class name
    :param rect: Window rectangle as (x, y, width, height)
    """
    hwnd: int
    title: str
    class_name: str
    rect: tuple  # (x, y, width, height)

@dataclass
class WindowRepositionCommand:
    """
    Command to reposition a specific window.
    
    Published by WindowPositioningPlugin when repositioning is needed.
    Used internally by the plugin to execute the SetWindowPos API call.
    
    :param hwnd: Window handle (HWND) to reposition
    :param target_x: Target X coordinate
    :param target_y: Target Y coordinate
    :param reason: Reason for repositioning ("overlap_detected", "initial_position", etc.)
    """
    hwnd: int
    target_x: int
    target_y: int
    reason: str


# ============================================================================
# PUSH-TO-TALK EVENTS
# Events for push-to-talk mode audio muting coordination
# ============================================================================

@dataclass
class PTTStartedEvent:
    """Published when push-to-talk hold begins.

    Consumed by SystemVolumePlugin to mute system audio during PTT.

    :param source: Where PTT was triggered ("floating_button", "tray_icon")
    """
    source: str

@dataclass
class PTTStoppedEvent:
    """Published when push-to-talk hold ends (release or safety timeout).

    Consumed by SystemVolumePlugin to restore system audio after PTT.

    :param reason: Why PTT stopped ("released", "safety_timeout")
    """
    reason: str


# ============================================================================
# TEXT-TARGET RETRY EVENTS (wh-9weum Phase 4)
# Verified-retry signal for the click counter and three-strikes follow-up
# ============================================================================

@dataclass
class RetryVerified:
    """Published when a Try-it-anyway click produced a verified paste (wh-mv5ih).

    The Logic process publishes this after ``forward_retry_dictation_by_token``
    receives a success response with ``retry_outcome="verified"`` and
    successfully resolves the click's correlation_token to a
    ``RejectionTuple`` via the Logic-side rejection-token cache.

    Subscribers:
      * wh-82lnx (click counter): increments the per-tuple counter.
      * wh-bqv9c (three-strikes follow-up toast): reads the same
        counter to decide whether to surface the persistence prompt.

    Privacy contract: this event carries the platform identity triple
    plus the application's friendly name. No dictation text, no
    correlation_token, no user content. The fields match the
    ``RejectionTuple`` the cache holds and the soft-allow persistence
    file stores; they are platform metadata, not user data.

    A verified retry that misses the Logic-side cache (TTL elapsed,
    or the Logic process was restarted between the rejection and the
    click) does NOT produce this event -- the publisher fails closed
    so a counter that depends on this signal cannot increment without
    a confirmed identity tuple.

    :param process_name: Short name of the process owning the focused
        control (e.g. ``"zed.exe"``).
    :param class_name: UIA ClassName of the control. May be empty for
        the browser-empty-ClassName case (wh-zndq).
    :param control_type: UIA ControlType (e.g. ``"Pane"``, ``"Edit"``).
    :param app_friendly_name: Human-readable application name resolved
        at rejection time via ``GetFileVersionInfo`` and cached on the
        ``RejectionTuple`` so the wh-bqv9c three-strikes prompt does
        not have to re-resolve it (wh-vbvgf.4.1).
    """

    process_name: str
    class_name: str
    control_type: str
    app_friendly_name: str


@dataclass
class RetryThresholdReached:
    """Published when the click counter reaches or exceeds the soft-allow
    threshold for a given identity tuple (wh-82lnx).

    The click counter (wh-82lnx) publishes this on every verified retry
    that brings the per-tuple counter to or above the threshold. The
    three-strikes follow-up toast (wh-bqv9c) subscribes and renders the
    "Save <App> as an allowed target?" prompt; wh-bqv9c is responsible
    for per-tuple per-session dedup, including the "user dismissed without
    clicking" reset rule. The counter does NOT dedup on its side.

    Privacy contract matches RetryVerified: platform metadata only. The
    triple plus app_friendly_name is the same set of fields the
    RejectionTuple cache holds and the soft-allow persistence file
    stores. The per-tuple count is a counter value, not user content.

    :param process_name: Short name of the process owning the focused
        control.
    :param class_name: UIA ClassName of the control.
    :param control_type: UIA ControlType.
    :param app_friendly_name: Human-readable application name resolved
        at rejection time and threaded through RetryVerified.
    :param count: Current per-tuple counter value at the moment the
        threshold check fired. Always >= the configured threshold.
    """

    process_name: str
    class_name: str
    control_type: str
    app_friendly_name: str
    count: int
