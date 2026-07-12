"""WebSocket server for Speech-to-Text transcript reception and processing.

This module implements the WebSocket server that receives transcript data from
the Google STT server and coordinates with the WheelHouse speech processing
pipeline. It handles client connection management, message parsing, and
integration with the WebSocketManager for centralized connection state.

Key Functions:
  - start_websocket_server: Main server initialization and lifecycle management.
  - connection_handler: Individual client connection processing.

Key Features:
  - WebSocket server with automatic client connection handling
  - JSON message parsing and validation
  - Integration with WebSocketManager for connection state
  - Status message broadcasting to newly connected clients
  - Error handling for malformed messages and connection issues
  - Transcript forwarding to speech processing pipeline

Message Flow:
  - Receives transcript strings from Google STT server
  - Validates and processes incoming messages
  - Forwards transcripts to text processing pipeline
  - Manages connection lifecycle events

Integration Points:
  - WebSocketManager for connection state management
  - Speech processing pipeline via text_handler_callback
  - Asyncio WebSocket server infrastructure

Typical Usage:
  from integrations.speech_to_text_server import start_websocket_server
  from integrations.websocket_manager import WebSocketManager
  
  manager = WebSocketManager()
  
  async def handle_transcript(text: str):
      # Process received transcript
      process_speech_input(text)
  
  # Start WebSocket server
  await start_websocket_server(
      host="localhost",
      port=<port>,
      text_handler_callback=handle_transcript,
      manager=manager
  )
"""
# Purpose: WebSocket server for receiving speech-to-text data.

import asyncio
import json
import logging
import websockets
from typing import Callable, Awaitable, Any

from .websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

async def start_websocket_server(
    host: str,
    port: int,
    text_handler_callback: Callable[[str], Awaitable[None]],
    manager: WebSocketManager
):
    """
    Starts the WebSocket server and manages client connections using a WebSocketManager.

    :flow: WebSocket Communication
    :step: 1a
    :description: Entry point for server startup, delegating to WebSocketManager
    :data_in: host, port, callback, manager
    :data_out: Running server loop
    :notes: Wraps websockets.serve with connection handler.

    Args:
        host: The host to bind the server to.
        port: The port to bind the server to.
        text_handler_callback: Async function to call with received text.
        manager: The WebSocketManager instance to handle client state.
    """
    async def connection_handler(websocket: Any):
        """Handles a new WebSocket connection."""
        await manager.add_client(websocket)
        try:
            # Send current status immediately upon connection
            status_message = manager.get_current_status_message()
            await websocket.send(json.dumps(status_message))

            async for message in websocket:
                if isinstance(message, str):
                    if not message.strip():
                        logger.debug("STT: Received and discarded empty/whitespace-only message.")
                        continue
                    
                    await text_handler_callback(message)
                else:
                    logger.warning(f"STT: Received non-text data: {type(message)}. Ignoring.")
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"STT: Connection closed cleanly for {websocket.remote_address}")
        except Exception as e:
            logger.error(f"STT: An error occurred with client {websocket.remote_address}: {e}", exc_info=True)
        finally:
            manager.remove_client(websocket)

    server = None
    try:
        server = await websockets.serve(connection_handler, host, port)
        logger.info(f"STT: WebSocket server started on {host}:{port}")
        await server.wait_closed()
    except asyncio.CancelledError:
        logger.info("STT: Server task cancelled. Initiating shutdown.")
    except OSError as e:
        logger.critical(f"STT: Failed to start server on {host}:{port}. OS Error: {e}")
    except Exception as e:
        logger.critical(f"STT: Unexpected error starting server: {e}", exc_info=True)
    finally:
        if server:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
                logger.info("STT: Server shutdown complete.")
            except asyncio.TimeoutError:
                logger.warning("STT: Server shutdown timed out waiting for connections to close.")