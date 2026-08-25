"""In-process pub/sub feeding the WebSocket endpoint (§8, §9).

Publishers (trade loop, training, heartbeat) call `publish` without knowing
or caring whether anyone is listening. `/ws` subscribes each connected
client to its own bounded queue.

Two deliberate properties:

* **Publishing never blocks and never raises.** A slow or dead browser tab
  must not be able to stall the trade loop. A subscriber whose queue is
  full loses the oldest event instead.
* **Events carry no secrets.** Anything published here reaches every
  connected client.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Event names the frontend subscribes to (§8.3).
EVENT_TRADE = "trade_event"
EVENT_TRAINING_PROGRESS = "training_progress"
EVENT_COMPONENT_STATUS = "component_status_change"
EVENT_DATA_DOWNLOAD = "data_download_progress"
EVENT_WALLET = "wallet_update"
EVENT_SYSTEM = "system_event"

# Per-subscriber buffer. Enough to ride out a brief stall; small enough that
# a tab left open in the background cannot grow memory without bound.
QUEUE_SIZE = 100


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Fan out to every subscriber. Never blocks, never raises."""
        message = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(payload or {}),
        }

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop the oldest so a stalled client loses history rather
                # than blocking the publisher.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.debug("Dropped %s for a saturated subscriber.", event)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected error publishing %s", event)


bus = EventBus()
