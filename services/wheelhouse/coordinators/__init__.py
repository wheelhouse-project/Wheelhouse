"""Coordinators for orchestrating multi-component workflows in WheelHouse.

This package contains coordinator classes that orchestrate interactions between
multiple services, plugins, or handlers. Coordinators implement complex business
logic that spans multiple components, following the event-driven architecture.

Key Coordinators:
  - BrightnessCoordinator: Multi-stage brightness control with hardware/software cascade
"""
from services.wheelhouse.coordinators.brightness_coordinator import BrightnessCoordinator

__all__ = ["BrightnessCoordinator"]
