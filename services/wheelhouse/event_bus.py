"""A simple asynchronous event bus for the WheelHouse application.

This module provides the EventBus, which facilitates communication between
different services using a publish-subscribe model. It forms the "push"
part of the application's hybrid configuration strategy:

- **Static "Pull" Configuration (ConfigService):** For startup-critical
  parameters that are read once and rarely change.

- **Dynamic "Push" Configuration (This Service):** For runtime-changeable
  settings. The EventBus is used to push notifications (events) to
  interested services when these settings are modified, allowing them to
  react dynamically without polling for changes.
"""
from collections import defaultdict
import asyncio
import logging
from typing import Callable, Any, Dict, List

logger = logging.getLogger(__name__)


class EventBus:
    """A simple asynchronous event bus."""

    def __init__(self):
        """Initializes the EventBus."""
        self._subscribers: Dict[type, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: type, callback: Callable):
        """
        Subscribe a callback to an event type.

        :param event_type: The type of event to subscribe to.
        :param callback: The asynchronous function to call when the event is published.
        """
        self._subscribers[event_type].append(callback)

    async def publish(self, event: Any):
        """
        Publish an event to all subscribed callbacks.

        Handler exceptions are isolated -- a failing handler is logged but
        never disrupts the caller or prevents sibling handlers from running.

        :param event: The event object to publish.
        """
        event_type = type(event)
        if event_type in self._subscribers:
            tasks = []
            for callback in self._subscribers[event_type]:
                try:
                    tasks.append(callback(event))
                except Exception:
                    logger.exception(
                        "EventBus: failed to invoke handler %r for %s",
                        callback,
                        event_type.__name__,
                    )
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(
                            "EventBus: handler raised for %s",
                            event_type.__name__,
                            exc_info=result,
                        )
