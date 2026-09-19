"""Tell WheelHouse why a provider is quitting before it has a forwarder.

Every STT provider builds its audio capture BEFORE it builds the
WSForwarder that carries notices to WheelHouse. Parakeet builds the
capture in ParakeetServer.__init__ and the forwarder a few lines later
in the same __init__; distil has the same shape; google builds the
capture in main() about seventy lines before the forwarder, and starts
that forwarder later still. The capture factory now refuses to build
anything when winsdk is missing (shared_audio/capture/factory.py,
wh-capture-winrt-required), so a provider has to deliver one notice at
a moment when its own forwarder does not exist.

This module opens a forwarder that lives only for that notice, sends
it, and stops. The alternative was to move each provider's forwarder
construction ahead of its capture; that reorders three different
startup sequences -- google's forwarder needs a dozen callbacks that
are defined between the capture and the forwarder -- to serve a path
that ends in sys.exit either way. A short-lived connection changes no
startup order.

WHY THE PROVIDER NAME IS NOT OPTIONAL
-------------------------------------
WheelHouse DROPS a startup failure that arrives on a connection which
never said which provider it is: integrations/websocket_manager.py logs
"Dropping a startup failure from a connection that never declared its
provider" and records it against a guessed launch instead of ending the
real one. WSForwarder announces the name in the capabilities frame it
sends on every connect, so the name has to reach this constructor.
"""
from __future__ import annotations

import logging
import threading
import time

from shared_stt.ws_forwarder import WSForwarder

logger = logging.getLogger(__name__)

# How long to wait for WheelHouse to accept the connection before giving
# up on delivering the notice. The provider is already exiting, so this
# is the whole cost of the attempt. WSForwarder retries with a 0.5s
# backoff, which leaves room for several tries.
CONNECT_TIMEOUT_S = 5.0

_CONNECT_POLL_S = 0.05

# The process exit status a provider uses when it refuses to start: the
# capture factory would not build a capture (A3), or the capture built
# and never opened (A9). It lives here, next to the notice that explains
# the refusal to the user, because the two always travel together.
#
# It exists because a normal WheelHouse launch does not run main.py: it
# runs the provider's launcher.py, which is run_launcher() in
# shared_stt/launcher.py. That supervisor restarts any nonzero exit that
# happens inside crash_threshold_s (15 seconds), up to max_crashes (3),
# so a refusal on a machine whose model is already in the file cache was
# started again three times -- after WheelHouse had already read the
# first startup_failed notice and recorded the launch as stopped
# (wh-capture-winrt-required.1.5).
#
# NOT 0: that reads as a clean stop in the launcher log and to a person
# running the provider from a console, and this run produced nothing.
# NOT 1: every other failure in these services already exits 1, so a
# supervisor that treated 1 as a refusal would stop restarting real
# crashes. NOT 2 either: google_stt_server/config_loader.py exits 2 on a
# bad configuration file.
REFUSAL_EXIT_CODE = 3


def send_startup_failed_notice(
    title: str,
    message: str,
    ws_host: str,
    ws_port: int,
    provider_name: str,
    emits_eos: bool = False,
    connect_timeout: float = CONNECT_TIMEOUT_S,
) -> bool:
    """Deliver one kind="startup_failed" notice over a throwaway forwarder.

    Args:
        title: Notification title, the same display name the provider
            uses for its own notices.
        message: The notice body. Callers pass str(exc) so the wording
            stays in the one place that owns it.
        ws_host: WheelHouse WebSocket host.
        ws_port: WheelHouse WebSocket port.
        provider_name: The provider identity for the capabilities frame.
            Without it WheelHouse drops the failure (see module
            docstring).
        emits_eos: Echoed into the capabilities frame so the throwaway
            connection declares what the real one would have.
        connect_timeout: Seconds to wait for the connection.

    Returns:
        True when the notice was handed to a live connection, False when
        no connection opened in time. The caller exits nonzero either
        way -- the answer is for logging and for tests, not for deciding
        whether to keep running.
    """
    # Logged before anything is attempted, because a developer running
    # the provider from a console has no WheelHouse to receive the
    # notice and this line is the only copy of the message they get.
    logger.error(message)

    forwarder = None
    try:
        # Constructed INSIDE the try. The caller is inside an except
        # handler on its way to sys.exit, so an exception raised here
        # would replace the refusal with a WebSocket traceback and skip
        # the exit that follows the call.
        forwarder = WSForwarder(
            host=ws_host,
            port=ws_port,
            transcription_enabled_event=threading.Event(),
            provider_name=provider_name,
            emits_eos=emits_eos,
        )
        forwarder.start()
        if not wait_for_notice_connection(
                forwarder, f"ws://{ws_host}:{ws_port}", connect_timeout):
            return False
        forwarder.send_notification(title, message, kind="startup_failed")
        return True
    except Exception:
        # Nothing may escape this function. The caller is a provider
        # partway through reporting a missing audio package, and a
        # traceback about a WebSocket would replace the one message the
        # user can act on with one they cannot. start() creates an event
        # loop and a thread; send_notification schedules work on both.
        logger.exception("The startup failure could not be delivered")
        return False
    finally:
        # stop() is what delivers it: send_notification only schedules a
        # queue put on the sender loop, and stop() gives already-queued
        # frames a bounded 2.0 second chance to go out before it stops
        # that loop. Exiting without it abandons the notice. It is
        # called on the failure paths too, so a forwarder that started
        # and then failed does not leave its thread behind.
        # None when the construction itself failed, which is the one
        # case with no forwarder and so nothing to stop.
        if forwarder is not None:
            try:
                forwarder.stop()
            except Exception:
                logger.exception("The startup-failure forwarder did not stop")


def wait_for_notice_connection(
    forwarder,
    address: str,
    connect_timeout: float | None = None,
) -> bool:
    """Give a queued refusal a connection to leave on, then say whether
    it got one.

    Two refusal paths need this, and the second one is why this is a
    function of its own rather than four lines inside
    send_startup_failed_notice.

    The constructor refusal (A3) has no forwarder yet, so it opens one
    and calls this before its send. The capture refusal (A9) already has
    the provider's real forwarder, has already queued the notice on it,
    and calls this before it lets the run end
    (wh-capture-winrt-required.1.4). Both have the same problem:
    send_notification only schedules a queue put on the sender loop, and
    WSForwarder.stop() drains that queue ONLY when a connection is
    already live -- otherwise it sets the stop event at once and the
    sender loop never reads the queue again. A refusal that happened
    before the provider's first handshake finished was therefore
    discarded, even when WheelHouse accepted the connection a fraction
    of a second later.

    Args:
        forwarder: The forwarder the notice is queued on, or is about to
            be queued on.
        address: The address the connection is being attempted on, for
            the log line. Provider call sites pass the forwarder's own
            ``uri``; send_startup_failed_notice builds it from the host
            and port it was given.
        connect_timeout: Seconds to wait. None means CONNECT_TIMEOUT_S,
            read at call time so a test can shorten the wait without
            reaching into a default argument bound at import.

    Returns:
        True when a connection was live before the timeout. False after
        a bounded wait that never saw one, with one log line naming the
        address. The caller exits nonzero either way -- the answer is
        for logging and for tests, not for deciding whether to keep
        running.
    """
    if connect_timeout is None:
        connect_timeout = CONNECT_TIMEOUT_S
    if _wait_connected(forwarder, connect_timeout):
        return True
    # The address is what makes this line actionable: a developer
    # reading it needs to know which host and port were tried before
    # they can say whether WheelHouse was listening somewhere else.
    logger.error(
        "Could not deliver the startup failure: nothing accepted a "
        f"connection on {address} within {connect_timeout:g} seconds"
    )
    return False


def _wait_connected(forwarder, timeout: float) -> bool:
    """Whether the forwarder connected within the timeout.

    The wait exists because WSForwarder.stop() only drains its queue
    when a connection is live. Sending and stopping straight after
    start() would set the stop event while the sender loop was still on
    its first connect attempt, and the notice would never leave the
    process.

    A timeout of 0 still asks once: the caller wants one answer, not no
    answer.
    """
    deadline = time.monotonic() + timeout
    while True:
        if forwarder.is_connected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_CONNECT_POLL_S)
