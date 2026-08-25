"""WebSocket endpoint (§8, §9).

Each client gets its own bounded queue from the event bus. A client that
stops reading loses old events rather than blocking the publisher.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.event_bus import EVENT_SYSTEM, bus

logger = logging.getLogger(__name__)

router = APIRouter()

# Long enough to be well under a typical 60s proxy idle timeout.
PING_INTERVAL_SECONDS = 25


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = bus.subscribe()

    await websocket.send_json({
        "event": EVENT_SYSTEM, "level": "info", "message": "connected",
    })

    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    queue.get(), timeout=PING_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                # Keeps idle connections alive through proxies and lets a
                # dead socket be detected rather than hanging forever.
                await websocket.send_json({"event": "ping"})
                continue

            await websocket.send_json(message)

    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected.")
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket connection error")
    finally:
        bus.unsubscribe(queue)
